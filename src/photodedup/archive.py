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


class ArchiveDestination(RuntimeError):
    """Writing to or reading back from the archive root failed.

    Distinct from a missing source: a source we cannot read is one file to
    retry next run, while a destination we cannot write is the drive going
    away underneath us, and every subsequent copy would fail the same way.
    """


@dataclass(frozen=True)
class ArchiveResult:
    copied: int
    skipped: int
    mismatched: int
    missing: int
    bytes: int


_PENDING_SQL = """
    SELECT a.uuid, a.original_filename, a.path, a.file_size, a.photos_date,
           a.date_added, a.albums, a.keywords, a.is_favorite, a.title,
           a.description, a.has_adjustments, a.is_live,
           l.set_id, l.lost_on, l.carries,
           d.keeper_uuid, d.date_value, d.review_flags, 'loser' AS role
    FROM decision_loser l
    JOIN dup_set s ON s.id = l.set_id AND s.tier = 'exact'
    JOIN decision d ON d.set_id = l.set_id AND d.resolvable = 1
    JOIN asset a ON a.uuid = l.uuid
    UNION ALL
    SELECT a.uuid, a.original_filename, a.path, a.file_size, a.photos_date,
           a.date_added, a.albums, a.keywords, a.is_favorite, a.title,
           a.description, a.has_adjustments, a.is_live,
           d.set_id, NULL, NULL,
           d.keeper_uuid, d.date_value, d.review_flags, 'blank_keeper' AS role
    FROM decision d
    JOIN dup_set s ON s.id = d.set_id AND s.tier = 'exact'
    JOIN asset a ON a.uuid = d.keeper_uuid
    WHERE ',' || d.review_flags || ',' LIKE '%,blank_image,%'
    ORDER BY uuid
"""


def pending_archive(conn: Connection) -> list[Row]:
    """Everything `apply` will stage for deletion, with the sidecar fields.

    Two populations, not one: the losers of resolvable exact sets, and the
    KEEPERS of blank-image sets, which `apply --stage` puts in the separate
    `Dedup Delete blank` album. Both end up in a delete album, so both need
    an archived copy before `apply` is allowed to run.
    """
    seen: set[str] = set()
    rows: list[Row] = []
    for row in conn.execute(_PENDING_SQL):
        # A blank-image keeper could in principle also be a loser elsewhere.
        # One archived original per uuid, or the insert would collide.
        if row["uuid"] in seen:
            continue
        seen.add(row["uuid"])
        rows.append(row)
    return rows


def pending_losers(conn: Connection) -> list[Row]:
    """Just the losers, for callers that mean losers rather than everything."""
    return [r for r in pending_archive(conn) if r["role"] == "loser"]


def _sanitize_name(name: str) -> str:
    """Neutralise a name that came from Photos metadata, not this filesystem.

    `original_filename` is free text an import (or a synced device) supplied;
    treating it as a path component without cleaning it up would let a stray
    "/" nest it into a directory we did not create, or a leading "." hide it.
    """
    name = name.replace("/", "_")
    name = name.lstrip(".")
    return name or "_"


def dest_for(row: Mapping, kind: str, source: str) -> str:
    """Relative destination path: <YYYY>/<MM>/<uuid>_<name>."""
    date = row["photos_date"]
    folder = f"{date[0:4]}/{date[5:7]}" if date else "undated"
    uuid = row["uuid"]
    suffix = PurePath(source).suffix.lower()
    if kind == "original":
        raw_name = row["original_filename"]
        name = _sanitize_name(raw_name) if raw_name else f"{uuid}{suffix}"
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


def _check_volume(root: Path) -> None:
    """Refuse a root under an unmounted /Volumes entry.

    Walking up to an existing ancestor (as `_default_free_bytes` does) is
    exactly wrong for an external drive: when it is unplugged, `/Volumes/<name>`
    does not exist, the walk-up lands on the boot disk, and the "free space"
    check happily reports gigabytes free on the wrong disk. A root elsewhere
    is unaffected and keeps the walk-up behaviour.
    """
    parts = Path(root).parts
    if len(parts) < 3 or parts[1] != "Volumes":
        return
    volume = Path(parts[0], parts[1], parts[2])
    if not volume.exists() or not os.path.ismount(str(volume)):
        raise ArchiveSpace(f"{volume} is not a mounted volume; is the drive plugged in?")


def _sidecar(row: Row, sha: str, size: int, stamp: str) -> dict:
    return {
        "uuid": row["uuid"],
        "original_filename": row["original_filename"],
        "role": row["role"],
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


MISSING_LISTED = 20


def _notes(copied: int, skipped: int, mismatched: int, missing: int, total_bytes: int,
           missing_uuids: list[str]) -> str:
    notes = (
        f"{copied} copied, {skipped} already done, {mismatched} mismatched, "
        f"{missing} missing, {total_bytes} bytes"
    )
    if missing_uuids:
        listed = ", ".join(missing_uuids[:MISSING_LISTED])
        more = len(missing_uuids) - MISSING_LISTED
        notes += f"; missing sources: {listed}" + (f" and {more} more" if more > 0 else "")
    return notes


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
    _check_volume(root)

    pending = pending_archive(conn)
    done = {
        (r["uuid"], r["kind"]) for r in conn.execute("SELECT uuid, kind FROM archive_file")
    }

    # Build the path map for every pending row, not just the ones still
    # missing an original: a loser whose original is already archived but
    # whose edited render is not must still be looked up here, or its edited
    # kind can never be found and the run reports clean when it is not.
    #
    # Opening osxphotos costs a full read of a 66,000-asset library, and the
    # map only ever yields anything for an asset that HAS an edited render or
    # a Live Photo movie. When nothing pending has either, skip the open.
    extras = any(r["has_adjustments"] or r["is_live"] for r in pending)
    if paths is None and extras:
        run_row = get_run(conn)
        if run_row is not None:
            wanted = {r["uuid"] for r in pending}
            paths = {
                p.uuid: p for p in iter_paths(Path(run_row["library_path"])) if p.uuid in wanted
            }
    paths = paths or {}

    def sources_for(row: Row) -> list[tuple[str, str | None]]:
        extra = paths.get(row["uuid"])
        if extra is None:
            return [("original", row["path"])]
        srcs: list[tuple[str, str | None]] = [("original", extra.path or row["path"])]
        if extra.path_edited:
            srcs.append(("edited", extra.path_edited))
        if extra.path_live:
            srcs.append(("live", extra.path_live))
        return srcs

    needs_work = [
        r for r in pending
        if any((r["uuid"], kind) not in done for kind, _source in sources_for(r))
    ]
    skipped = len(pending) - len(needs_work)
    todo = needs_work[:limit] if limit is not None else needs_work

    raw_bytes = sum((r["file_size"] or 0) for r in todo)
    need = int(raw_bytes * (1 + HEADROOM))
    free = (free_bytes or _default_free_bytes)(root)
    if need > free:
        raise ArchiveSpace(
            f"{len(todo)} files need {raw_bytes} bytes ({need} with headroom); "
            f"{free} bytes free at {root}."
        )

    set_stage(conn, "archive", "running")
    root.mkdir(parents=True, exist_ok=True)
    copied = mismatched = missing = total_bytes = 0
    missing_uuids: list[str] = []

    for index, row in enumerate(todo, start=1):
        for kind, source in sources_for(row):
            if not source or (row["uuid"], kind) in done:
                continue
            src = Path(source)
            # Read the source before touching the destination, so a source we
            # cannot read is one skipped file and never mistaken for the drive
            # going away.
            try:
                with open(src, "rb") as probe:
                    probe.read(1)
            except OSError:
                missing += 1
                missing_uuids.append(row["uuid"])
                continue
            rel = dest_for(row, kind, source)
            dst = root / rel
            tmp = dst.with_name(dst.name + ".part")
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                sha, size = _copy_hashing(src, tmp)
                check = _sha256_file(tmp)
                if check != sha:
                    tmp.unlink(missing_ok=True)
                    mismatched += 1
                    continue
                os.replace(tmp, dst)
                stamp = datetime.now().replace(microsecond=0).isoformat()
                if kind == "original":
                    (root / (rel + ".json")).write_text(
                        json.dumps(_sidecar(row, sha, size, stamp), indent=2)
                    )
            except OSError as exc:
                # Everything in this block writes to or reads back from the
                # archive root. Failing here means the destination is gone or
                # full, and every remaining copy would fail the same way, so
                # stop with what is already verified committed and kept.
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                conn.commit()
                set_stage(
                    conn, "archive", "running",
                    notes=_notes(copied, skipped, mismatched, missing, total_bytes, missing_uuids)
                          + f", stopped on a destination error at {dst}",
                )
                raise ArchiveDestination(
                    f"destination error at {dst}: {exc}; the drive may have unmounted "
                    "or filled; everything verified so far is kept. Re-run "
                    "`photodedup archive` once the drive is back."
                ) from exc
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

    # Completion is judged against every pending loser, not just this run's
    # (possibly `--limit`-truncated) batch: a clean but partial batch must not
    # report done while losers past the limit still have nothing archived.
    remaining_done = {
        (r["uuid"], r["kind"]) for r in conn.execute("SELECT uuid, kind FROM archive_file")
    }
    remaining = [
        r for r in pending
        if any((r["uuid"], kind) not in remaining_done for kind, _source in sources_for(r))
    ]
    notes = _notes(copied, skipped, mismatched, missing, total_bytes, missing_uuids)
    if remaining:
        notes += f", {len(remaining)} photos remaining"
    status = "complete" if not mismatched and not missing and not remaining else "running"
    set_stage(conn, "archive", status, notes=notes)
    return ArchiveResult(copied, skipped, mismatched, missing, total_bytes)
