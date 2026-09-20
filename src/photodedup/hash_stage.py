"""The hash stage: derive content signals for every asset, resumably."""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection

from photodedup.catalog import set_stage
from photodedup.hashing import (
    jpeg_is_truncated,
    jpeg_scan_hash,
    pixel_hashes,
    real_length,
    video_stream_hash,
)

BATCH = 200


class HashStageInterrupted(RuntimeError):
    """A worker pool died mid-run. Everything already written is kept."""


@dataclass(frozen=True)
class HashResult:
    total: int
    hashed: int
    already_done: int
    no_original: int
    errors: int
    truncated: int = 0


def _truncated_row(
    uuid: str, partial: object, bytes_read: int
) -> dict:
    """Construct a truncated-file row from partial hashes and real byte count."""
    return {
        "uuid": uuid,
        "status": "truncated",
        "pixel_hash": partial.pixel_hash,
        "dhash": partial.dhash,
        "phash": partial.phash,
        "pixel_width": partial.width,
        "pixel_height": partial.height,
        "blank_rows": partial.blank_rows,
        "flat": int(partial.flat),
        "bytes_read": bytes_read,
    }


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

    if jpeg_is_truncated(data):
        # No EOI after the scan: the file was cut short. Zero bytes may have
        # been appended later (the FUJI and 2016 files in this library), and
        # libjpeg will happily decode those as image data, so decode only the
        # real prefix and never trust a strict decode here.
        real = real_length(data)
        partial = pixel_hashes(target, lenient=True, data=data[:real])
        if partial is None:
            return {"uuid": uuid, "status": "error", "error": "no usable hash"}
        return _truncated_row(uuid, partial, real)

    row["bytes_read"] = len(data)
    # Only a JPEG has a scan to hash. Running the parser over a PNG or HEIC
    # just returns None after a wasted pass, and a file whose uti says PNG but
    # whose bytes start with SOI is a mislabelled file, not a JPEG match.
    row["scan_hash"] = jpeg_scan_hash(data) if uti == "public.jpeg" else None

    pixels = pixel_hashes(target)
    if pixels is not None:
        row["pixel_hash"] = pixels.pixel_hash
        row["dhash"] = pixels.dhash
        row["phash"] = pixels.phash
        row["pixel_width"] = pixels.width
        row["pixel_height"] = pixels.height
        row["blank_rows"] = pixels.blank_rows
        row["flat"] = int(pixels.flat)

    if row.get("scan_hash") is None and pixels is None:
        # Neither signal: either the file is not an image at all, or it is a
        # JPEG cut short before its EOI. Only the second decodes leniently.
        partial = pixel_hashes(target, lenient=True)
        if partial is None:
            return {"uuid": uuid, "status": "error", "error": "no usable hash"}
        return _truncated_row(uuid, partial, real_length(data))
    return row


_COLUMNS = (
    "status", "error", "scan_hash", "pixel_hash", "dhash", "phash",
    "video_hash", "pixel_width", "pixel_height", "bytes_read", "blank_rows",
    "flat", "hashed_at",
)


def _write(conn: Connection, row: dict) -> None:
    row = dict(row)
    row["hashed_at"] = datetime.now().replace(microsecond=0).isoformat()
    # A row with no decode (video, no original, error) has nothing to say
    # about flatness; the column is NOT NULL so it must still carry a value.
    row.setdefault("flat", 0)
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
        "SELECT COUNT(*) AS n FROM asset_hash WHERE status IN ('ok', 'no_original', 'truncated')"
    ).fetchone()["n"]

    todo = conn.execute(
        """
        SELECT a.uuid, a.path, a.kind, a.uti
        FROM asset a
        LEFT JOIN asset_hash h ON h.uuid = a.uuid
        WHERE a.survey_status = 'ok'
          AND (h.uuid IS NULL OR h.status IN ('error', 'skipped'))
        ORDER BY a.uuid
        """
    ).fetchall()
    if limit is not None:
        todo = todo[:limit]

    hashed = no_original = errors = truncated = 0
    total = len(todo)
    if total == 0:
        set_stage(conn, "hash", "complete", notes="nothing to do")
        return HashResult(0, 0, already, 0, 0)

    workers = workers or max(1, (os.cpu_count() or 4) - 1)
    done = 0

    def record(row: dict) -> None:
        nonlocal hashed, no_original, errors, truncated
        _write(conn, row)
        if row["status"] == "ok":
            hashed += 1
        elif row["status"] == "truncated":
            truncated += 1
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
        try:
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
        except BrokenProcessPool as exc:
            # A worker died (OOM killer, a segfault in an image decoder). Keep
            # every row already recorded so the next run resumes rather than
            # starting the 65,974-asset pass again.
            conn.commit()
            note = f"worker pool died after {done} of {total}: {exc}"
            set_stage(conn, "hash", "failed", notes=note)
            raise HashStageInterrupted(
                f"the worker pool died after {done} of {total} assets. Everything "
                "hashed so far is committed, so the run is resumable: re-run "
                "`photodedup hash` (optionally with fewer --workers) to continue."
            ) from exc

    conn.commit()
    set_stage(
        conn, "hash", "complete",
        notes=f"{hashed} hashed, {truncated} truncated, {no_original} without originals, {errors} errors",
    )
    return HashResult(total, hashed, already, no_original, errors, truncated)
