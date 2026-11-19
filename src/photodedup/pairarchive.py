"""Archive the losers of a pairs CSV before they are staged for deletion.

Each loser's original (and its Photos-edited render and Live Photo movie, when
it has them) is copied to <root>/<YYYY>/<MM>/<uuid>_<name>, read back and
verified by SHA-256, and recorded in the catalog's `archive_file`, exactly as
`photodedup archive` does. The manifest goes to its own file, manifest-<catalog
stem>.csv: `manifest.csv` belongs to the catalog that wrote it, and rewriting it
from a different catalog would drop every file that catalog does not know.

Rows flagged `review_original` also get a side-by-side copy under
edits-review/<date>_<stem>/ (original_* and edited_*), for spot checks. Those
copies are a convenience and not part of the verified archive.
"""

from __future__ import annotations

import csv
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection

from photodedup.archive import (
    HEADROOM,
    ArchiveDestination,
    ArchiveSpace,
    _check_volume,
    _copy_hashing,
    _default_free_bytes,
    _sanitize_name,
    _sha256_file,
    dest_for,
)
from photodedup.catalog import set_stage
from photodedup.library import AssetPaths

REVIEW_DIR = "edits-review"


@dataclass(frozen=True)
class PairArchiveResult:
    copied: int
    skipped: int
    mismatched: int
    missing: list[str]
    unknown: list[str]
    review_copies: int


def read_pairs_rows(path: Path) -> list[dict]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def archived(conn: Connection, uuids: list[str]) -> set[str]:
    """The uuids whose original is verified in this catalog's archive."""
    out: set[str] = set()
    for i in range(0, len(uuids), 500):
        chunk = uuids[i:i + 500]
        out.update(r[0] for r in conn.execute(
            f"SELECT uuid FROM archive_file WHERE kind = 'original' AND uuid IN ({','.join('?' * len(chunk))})",
            chunk,
        ))
    return out


def _write_manifest(conn: Connection, root: Path, name: str) -> None:
    tmp = root / (name + ".tmp")
    with open(tmp, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["uuid", "kind", "dest", "bytes", "sha256", "verified_at"])
        for r in conn.execute("SELECT uuid, kind, dest, bytes, sha256, verified_at FROM archive_file ORDER BY uuid, kind"):
            w.writerow(list(r))
    os.replace(tmp, root / name)


def run_pair_archive(
    conn: Connection,
    rows: list[dict],
    root: Path,
    *,
    manifest_name: str,
    paths: Mapping[str, AssetPaths] | None = None,
    free_bytes: Callable[[Path], int] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> PairArchiveResult:
    root = Path(root)
    _check_volume(root)
    paths = paths or {}
    assets = {}
    unknown = []
    for row in rows:
        a = conn.execute("SELECT * FROM asset WHERE uuid = ?", (row["loser"],)).fetchone()
        if a is None:
            unknown.append(row["loser"])
        else:
            assets[row["loser"]] = a
    keepers = {
        r["keeper"]: conn.execute("SELECT * FROM asset WHERE uuid = ?", (r["keeper"],)).fetchone()
        for r in rows if r.get("review_original") == "1"
    }
    done = {(r[0], r[1]) for r in conn.execute("SELECT uuid, kind FROM archive_file")}

    def sources(a) -> list[tuple[str, str | None]]:
        extra = paths.get(a["uuid"])
        srcs = [("original", (extra.path if extra else None) or a["path"])]
        if extra and extra.path_edited:
            srcs.append(("edited", extra.path_edited))
        if extra and extra.path_live:
            srcs.append(("live", extra.path_live))
        return srcs

    todo = [a for a in assets.values() if any((a["uuid"], k) not in done for k, _ in sources(a))]
    need = int(sum(a["file_size"] or 0 for a in todo) * (1 + HEADROOM))
    free = (free_bytes or _default_free_bytes)(root)
    if need > free:
        raise ArchiveSpace(f"{len(todo)} files need {need} bytes with headroom; {free} free at {root}.")

    set_stage(conn, "archive", "running")
    root.mkdir(parents=True, exist_ok=True)
    copied = mismatched = review_copies = 0
    missing: list[str] = []
    by_loser = {r["loser"]: r for r in rows}
    for index, a in enumerate(todo, start=1):
        for kind, source in sources(a):
            if not source or (a["uuid"], kind) in done:
                continue
            src = Path(source)
            try:
                with open(src, "rb") as probe:
                    probe.read(1)
            except OSError:
                missing.append(a["uuid"])
                continue
            rel = dest_for(a, kind, source)
            dst = root / rel
            tmp = dst.with_name(dst.name + ".part")
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                sha, size = _copy_hashing(src, tmp)
                if _sha256_file(tmp) != sha:
                    tmp.unlink(missing_ok=True)
                    mismatched += 1
                    continue
                os.replace(tmp, dst)
                stamp = datetime.now().replace(microsecond=0).isoformat()
                if kind == "original":
                    pair = by_loser[a["uuid"]]
                    (root / (rel + ".json")).write_text(json.dumps({
                        "uuid": a["uuid"],
                        "original_filename": a["original_filename"],
                        "role": "pair_loser",
                        "keeper_uuid": pair["keeper"],
                        "why": pair.get("why"),
                        "albums": json.loads(a["albums"] or "[]"),
                        "keywords": json.loads(a["keywords"] or "[]"),
                        "is_favorite": bool(a["is_favorite"]),
                        "title": a["title"],
                        "description": a["description"],
                        "photos_date": a["photos_date"],
                        "date_added": a["date_added"],
                        "sha256": sha,
                        "bytes": size,
                        "archived_at": stamp,
                    }, indent=2))
            except OSError as exc:
                tmp.unlink(missing_ok=True)
                conn.commit()
                raise ArchiveDestination(
                    f"destination error at {dst}: {exc}; everything verified so far is kept. "
                    "Re-run once the drive is back."
                ) from exc
            conn.execute(
                "INSERT INTO archive_file (uuid, kind, dest, bytes, sha256, verified_at) VALUES (?, ?, ?, ?, ?, ?)",
                (a["uuid"], kind, rel, size, sha, stamp),
            )
            copied += 1
        if index % 200 == 0:
            conn.commit()
            if progress:
                progress(index, len(todo))
    conn.commit()
    _write_manifest(conn, root, manifest_name)

    for row in rows:
        if row.get("review_original") != "1" or row["loser"] not in assets:
            continue
        loser, keeper = assets[row["loser"]], keepers.get(row["keeper"])
        if keeper is None or not keeper["path"] or not loser["path"]:
            continue
        date = (loser["photos_date"] or "undated")[:10]
        name = _sanitize_name((loser["original_filename"] or loser["uuid"]).rsplit(".", 1)[0])
        folder = root / REVIEW_DIR / f"{date}_{name}"
        for prefix, a in (("original", loser), ("edited", keeper)):
            dst = folder / f"{prefix}_{_sanitize_name(a['original_filename'] or a['uuid'])}"
            if dst.exists():
                continue
            try:
                folder.mkdir(parents=True, exist_ok=True)
                _copy_hashing(Path(a["path"]), dst)
                review_copies += 1
            except OSError:
                pass

    if not missing and not mismatched and not unknown:
        set_stage(conn, "archive", "complete",
                  notes=f"pairs: {copied} copied, {len(rows)} losers archived, manifest {manifest_name}")
    return PairArchiveResult(copied, len(assets) - len(todo), mismatched, missing, unknown, review_copies)
