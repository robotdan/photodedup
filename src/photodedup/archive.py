"""Copy every loser's original to the external drive and verify the copy."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePath
from sqlite3 import Connection, Row

from photodedup.catalog import get_run, require_stage_complete, set_stage
from photodedup.library import AssetPaths, iter_paths

CHUNK = 8 * 1024 * 1024
HEADROOM = 0.10


class ArchiveSpace(RuntimeError):
    """The destination volume cannot hold what remains to be copied."""


@dataclass(frozen=True)
class ArchiveResult:
    copied: int
    skipped: int
    mismatched: int
    missing: int
    bytes: int


def pending_losers(conn: Connection) -> list[Row]:
    """Losers of resolvable exact sets, with everything the sidecar needs."""
    return conn.execute(
        """
        SELECT a.uuid, a.original_filename, a.path, a.file_size, a.photos_date,
               a.date_added, a.albums, a.keywords, a.is_favorite, a.title,
               a.description, l.set_id, l.lost_on, l.carries,
               d.keeper_uuid, d.date_value, d.review_flags
        FROM decision_loser l
        JOIN dup_set s ON s.id = l.set_id AND s.tier = 'exact'
        JOIN decision d ON d.set_id = l.set_id AND d.resolvable = 1
        JOIN asset a ON a.uuid = l.uuid
        ORDER BY a.uuid
        """
    ).fetchall()


def dest_for(row: Mapping, kind: str, source: str) -> str:
    """Relative destination path: <YYYY>/<MM>/<uuid>_<name>."""
    date = row["photos_date"]
    folder = f"{date[0:4]}/{date[5:7]}" if date else "undated"
    uuid = row["uuid"]
    suffix = PurePath(source).suffix.lower()
    if kind == "original":
        name = row["original_filename"] or f"{uuid}{suffix}"
        return f"{folder}/{uuid}_{name}"
    return f"{folder}/{uuid}_{kind}{suffix}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_hashing(src: Path, dst: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(src, "rb") as reader, open(dst, "wb") as writer:
        for chunk in iter(lambda: reader.read(CHUNK), b""):
            digest.update(chunk)
            writer.write(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _default_free_bytes(root: Path) -> int:
    probe = root
    while not probe.exists():
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _sidecar(row: Row, sha: str, size: int, stamp: str) -> dict:
    return {
        "uuid": row["uuid"],
        "original_filename": row["original_filename"],
        "role": "loser",
        "set_id": row["set_id"],
        "keeper_uuid": row["keeper_uuid"],
        "lost_on": row["lost_on"],
        "carries": json.loads(row["carries"] or "{}"),
        "albums": json.loads(row["albums"] or "[]"),
        "keywords": json.loads(row["keywords"] or "[]"),
        "is_favorite": bool(row["is_favorite"]),
        "title": row["title"],
        "description": row["description"],
        "photos_date": row["photos_date"],
        "date_added": row["date_added"],
        "arbitrated_date": row["date_value"],
        "review_flags": row["review_flags"],
        "sha256": sha,
        "bytes": size,
        "archived_at": stamp,
    }


def _write_manifest(conn: Connection, root: Path) -> None:
    rows = conn.execute(
        "SELECT uuid, kind, dest, bytes, sha256, verified_at FROM archive_file ORDER BY uuid, kind"
    ).fetchall()
    tmp = root / "manifest.csv.tmp"
    with open(tmp, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["uuid", "kind", "dest", "bytes", "sha256", "verified_at"])
        for r in rows:
            writer.writerow([r["uuid"], r["kind"], r["dest"], r["bytes"], r["sha256"], r["verified_at"]])
    os.replace(tmp, root / "manifest.csv")


def run_archive(
    conn: Connection,
    root: Path,
    *,
    paths: Mapping[str, AssetPaths] | None = None,
    limit: int | None = None,
    free_bytes: Callable[[Path], int] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> ArchiveResult:
    require_stage_complete(conn, "arbitrate")
    missing_decisions = conn.execute(
        "SELECT COUNT(*) AS n FROM dup_set s WHERE NOT EXISTS (SELECT 1 FROM decision d WHERE d.set_id = s.id)"
    ).fetchone()["n"]
    if missing_decisions:
        from photodedup.catalog import StageIncomplete

        raise StageIncomplete(
            f"{missing_decisions} duplicate sets have no decision; re-run `photodedup arbitrate`."
        )

    root = Path(root)
    losers = pending_losers(conn)
    done = {
        (r["uuid"], r["kind"]) for r in conn.execute("SELECT uuid, kind FROM archive_file")
    }
    todo = [r for r in losers if (r["uuid"], "original") not in done]
    if limit is not None:
        todo = todo[:limit]

    raw_bytes = sum((r["file_size"] or 0) for r in todo)
    need = int(raw_bytes * (1 + HEADROOM))
    free = (free_bytes or _default_free_bytes)(root)
    if need > free:
        raise ArchiveSpace(
            f"{len(todo)} files need {raw_bytes} bytes ({need} with headroom); "
            f"{free} bytes free at {root}."
        )

    if paths is None and todo:
        run_row = get_run(conn)
        if run_row is not None:
            wanted = {r["uuid"] for r in todo}
            paths = {
                p.uuid: p for p in iter_paths(Path(run_row["library_path"])) if p.uuid in wanted
            }
    paths = paths or {}

    set_stage(conn, "archive", "running")
    root.mkdir(parents=True, exist_ok=True)
    copied = mismatched = missing = total_bytes = 0
    skipped = len(losers) - len(todo)

    for index, row in enumerate(todo, start=1):
        extra = paths.get(row["uuid"])
        sources = [("original", row["path"])]
        if extra is not None:
            sources = [
                ("original", extra.path or row["path"]),
                ("edited", extra.path_edited),
                ("live", extra.path_live),
            ]
        for kind, source in sources:
            if not source or (row["uuid"], kind) in done:
                continue
            src = Path(source)
            if not src.is_file():
                missing += 1
                continue
            rel = dest_for(row, kind, source)
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".part")
            try:
                sha, size = _copy_hashing(src, tmp)
                check = _sha256_file(tmp)
            except OSError:
                tmp.unlink(missing_ok=True)
                missing += 1
                continue
            if check != sha:
                tmp.unlink(missing_ok=True)
                mismatched += 1
                continue
            os.replace(tmp, dst)
            stamp = datetime.now().replace(microsecond=0).isoformat()
            if kind == "original":
                (root / (rel + ".json")).write_text(json.dumps(_sidecar(row, sha, size, stamp), indent=2))
            conn.execute(
                """INSERT INTO archive_file (uuid, kind, dest, bytes, sha256, verified_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (row["uuid"], kind, rel, size, sha, stamp),
            )
            copied += 1
            total_bytes += size
        if index % 200 == 0:
            conn.commit()
            if progress:
                progress(index, len(todo))

    conn.commit()
    _write_manifest(conn, root)
    set_stage(
        conn,
        "archive",
        "complete" if not mismatched and not missing else "running",
        notes=f"{copied} copied, {skipped} already done, {mismatched} mismatched, {missing} missing, {total_bytes} bytes",
    )
    return ArchiveResult(copied, skipped, mismatched, missing, total_bytes)
