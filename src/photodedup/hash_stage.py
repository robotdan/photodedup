"""The hash stage: derive content signals for every asset, resumably."""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection

from photodedup.catalog import set_stage
from photodedup.hashing import jpeg_scan_hash, pixel_hashes, video_stream_hash

BATCH = 200


@dataclass(frozen=True)
class HashResult:
    total: int
    hashed: int
    already_done: int
    no_original: int
    errors: int


def hash_one(uuid: str, path: str | None, kind: str, uti: str | None) -> dict:
    """Compute every signal for one asset. Module-level so a pool can pickle it."""
    if path is None:
        return {"uuid": uuid, "status": "no_original"}

    target = Path(path)
    row: dict = {"uuid": uuid, "status": "ok"}

    if kind == "video":
        digest = video_stream_hash(target)
        if digest is None:
            return {"uuid": uuid, "status": "error", "error": "video stream unreadable"}
        row["video_hash"] = digest
        try:
            row["bytes_read"] = target.stat().st_size
        except OSError:
            pass
        return row

    try:
        data = target.read_bytes()
    except OSError as exc:
        return {"uuid": uuid, "status": "error", "error": f"unreadable: {exc}"}

    row["bytes_read"] = len(data)
    row["scan_hash"] = jpeg_scan_hash(data)

    pixels = pixel_hashes(target)
    if pixels is not None:
        row["pixel_hash"] = pixels.pixel_hash
        row["dhash"] = pixels.dhash
        row["phash"] = pixels.phash
        row["pixel_width"] = pixels.width
        row["pixel_height"] = pixels.height

    if row.get("scan_hash") is None and pixels is None:
        return {"uuid": uuid, "status": "error", "error": "no usable hash"}
    return row


_COLUMNS = (
    "status", "error", "scan_hash", "pixel_hash", "dhash", "phash",
    "video_hash", "pixel_width", "pixel_height", "bytes_read", "hashed_at",
)


def _write(conn: Connection, row: dict) -> None:
    row = dict(row)
    row["hashed_at"] = datetime.now().replace(microsecond=0).isoformat()
    values = [row["uuid"]] + [row.get(c) for c in _COLUMNS]
    placeholders = ", ".join("?" * (len(_COLUMNS) + 1))
    updates = ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS)
    conn.execute(
        f"""
        INSERT INTO asset_hash (uuid, {", ".join(_COLUMNS)})
        VALUES ({placeholders})
        ON CONFLICT (uuid) DO UPDATE SET {updates}
        """,
        values,
    )


def run_hash(
    conn: Connection,
    *,
    workers: int | None = None,
    limit: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> HashResult:
    """Hash every surveyed asset that is not already done. Resumable."""
    set_stage(conn, "hash", "running")

    already = conn.execute(
        "SELECT COUNT(*) AS n FROM asset_hash WHERE status IN ('ok', 'no_original')"
    ).fetchone()["n"]

    todo = conn.execute(
        """
        SELECT a.uuid, a.path, a.kind, a.uti
        FROM asset a
        LEFT JOIN asset_hash h ON h.uuid = a.uuid
        WHERE a.survey_status = 'ok'
          AND (h.uuid IS NULL OR h.status = 'error')
        ORDER BY a.uuid
        """
    ).fetchall()
    if limit is not None:
        todo = todo[:limit]

    hashed = no_original = errors = 0
    total = len(todo)
    if total == 0:
        set_stage(conn, "hash", "complete", notes="nothing to do")
        return HashResult(0, 0, already, 0, 0)

    workers = workers or max(1, (os.cpu_count() or 4) - 1)
    done = 0

    def record(row: dict) -> None:
        nonlocal hashed, no_original, errors
        _write(conn, row)
        if row["status"] == "ok":
            hashed += 1
        elif row["status"] == "no_original":
            no_original += 1
        else:
            errors += 1

    if workers == 1:
        for r in todo:
            record(hash_one(r["uuid"], r["path"], r["kind"], r["uti"]))
            done += 1
            if done % BATCH == 0:
                conn.commit()
                if progress:
                    progress(done, total)
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(hash_one, r["uuid"], r["path"], r["kind"], r["uti"])
                for r in todo
            ]
            for future in futures:
                record(future.result())
                done += 1
                if done % BATCH == 0:
                    conn.commit()
                    if progress:
                        progress(done, total)

    conn.commit()
    set_stage(
        conn, "hash", "complete",
        notes=f"{hashed} hashed, {no_original} without originals, {errors} errors",
    )
    return HashResult(total, hashed, already, no_original, errors)
