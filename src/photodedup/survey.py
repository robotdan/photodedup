"""The survey stage: read-only characterisation of the library."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection

from photodedup.catalog import get_run, set_run, set_stage
from photodedup.dates import (
    Candidate,
    parse_exif_datetime,
    parse_filename_datetime,
    rejection_reason,
)
from photodedup.exif import read_metadata
from photodedup.library import AssetRecord

MISSING_ORIGINALS_TOLERANCE = 0.01

# Ordered by descending trust; the first non-null wins for a given source.
_EXIF_DATE_TAGS = (
    "EXIF:DateTimeOriginal",
    "QuickTime:CreationDate",
    "QuickTime:CreateDate",
    "XMP:DateCreated",
    "EXIF:CreateDate",
)


class MissingOriginalsError(RuntimeError):
    """Too many assets have no local original to read."""


@dataclass(frozen=True)
class SurveyResult:
    total: int
    written: int
    skipped_after_cutoff: int
    missing_originals: int
    errors: int


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.replace(microsecond=0).isoformat()


def _exif_candidate(tags: dict[str, object]) -> Candidate:
    for tag in _EXIF_DATE_TAGS:
        raw = tags.get(tag)
        if raw is None:
            continue
        parsed = parse_exif_datetime(str(raw))
        if parsed is not None:
            return Candidate("exif", parsed, str(raw))
    return Candidate("exif", None, None)


def _file_mtime_candidate(tags: dict[str, object]) -> Candidate:
    raw = tags.get("File:FileModifyDate")
    if raw is None:
        return Candidate("file_mtime", None, None)
    return Candidate("file_mtime", parse_exif_datetime(str(raw)), str(raw))


def _candidates(record: AssetRecord, tags: dict[str, object]) -> list[Candidate]:
    name = record.original_filename or record.filename or ""
    return [
        _exif_candidate(tags),
        Candidate("filename", parse_filename_datetime(name), name or None),
        Candidate("photos_db", record.photos_date, _iso(record.photos_date)),
        _file_mtime_candidate(tags),
    ]


def _write_asset(
    conn: Connection, record: AssetRecord, tags: dict[str, object]
) -> None:
    mtime = _file_mtime_candidate(tags).value
    exif_error = tags.get("ExifTool:Error") or tags.get("ExifTool:Warning")
    conn.execute(
        """
        INSERT INTO asset (
            uuid, filename, original_filename, uti, kind, width, height,
            file_size, path, date_added, photos_date, photos_tz_offset,
            file_mtime, is_live, has_raw, has_adjustments, is_favorite,
            is_hidden, in_trash, title, description, latitude, longitude,
            keywords, albums, import_uuid, survey_status, survey_error,
            exif_error
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, 'ok', NULL, ?
        )
        ON CONFLICT (uuid) DO UPDATE SET
            survey_status = 'ok', survey_error = NULL, exif_error = excluded.exif_error
        """,
        (
            record.uuid,
            record.filename,
            record.original_filename,
            record.uti,
            record.kind,
            record.width,
            record.height,
            tags.get("File:FileSize"),
            record.path,
            _iso(record.date_added),
            _iso(record.photos_date),
            record.photos_tz_offset,
            _iso(mtime),
            int(record.is_live),
            int(record.has_raw),
            int(record.has_adjustments),
            int(record.is_favorite),
            int(record.is_hidden),
            int(record.in_trash),
            record.title,
            record.description,
            record.latitude,
            record.longitude,
            json.dumps(list(record.keywords)),
            json.dumps(list(record.albums)),
            record.import_uuid,
            exif_error,
        ),
    )

    for candidate in _candidates(record, tags):
        why = rejection_reason(
            candidate, date_added=record.date_added, file_mtime=mtime
        )
        conn.execute(
            """
            INSERT INTO date_candidate (uuid, source, value, raw, rejected_reason)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (uuid, source) DO UPDATE SET
                value = excluded.value,
                raw = excluded.raw,
                rejected_reason = excluded.rejected_reason
            """,
            (record.uuid, candidate.source, _iso(candidate.value), candidate.raw, why),
        )


def run_survey(
    conn: Connection,
    records: Iterable[AssetRecord],
    *,
    library_path: str,
    cutoff: datetime | None = None,
    read_exif: Callable[..., dict[str, dict[str, object]]] = read_metadata,
    batch_size: int = 400,
) -> SurveyResult:
    """Populate the catalog from *records*. Idempotent and resumable."""
    set_stage(conn, "survey", "running")
    all_records = list(records)

    existing_run = get_run(conn)
    if cutoff is not None:
        cutoff_iso = _iso(cutoff)
    elif existing_run is not None:
        cutoff_iso = existing_run["cutoff_date_added"]
    else:
        added = [r.date_added for r in all_records if r.date_added is not None]
        cutoff_iso = _iso(max(added)) if added else _iso(datetime.now())
    set_run(conn, library_path=library_path, cutoff_date_added=cutoff_iso)

    in_scope = []
    skipped = 0
    for record in all_records:
        if record.date_added is not None and _iso(record.date_added) > cutoff_iso:
            skipped += 1
        else:
            in_scope.append(record)

    missing = sum(1 for r in in_scope if r.path is None)
    if in_scope and missing / len(in_scope) > MISSING_ORIGINALS_TOLERANCE:
        set_stage(conn, "survey", "failed", notes=f"{missing} assets lack originals")
        raise MissingOriginalsError(
            f"{missing} of {len(in_scope)} assets have no local original. Enable "
            "Download Originals to this Mac in Photos, wait for it to finish, "
            "then re-run the survey."
        )

    done = {
        row["uuid"]
        for row in conn.execute("SELECT uuid FROM asset WHERE survey_status = 'ok'")
    }
    todo = [r for r in in_scope if r.uuid not in done]

    written = 0
    errors = 0
    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        paths = [Path(r.path) for r in batch if r.path]
        tags_by_path = read_exif(paths) if paths else {}
        for record in batch:
            tags = tags_by_path.get(str(record.path), {}) if record.path else {}
            try:
                _write_asset(conn, record, tags)
                written += 1
            except Exception as exc:
                errors += 1
                # _write_asset may have inserted candidates before failing; they
                # share this uncommitted transaction, so drop them explicitly or
                # the error row keeps partial candidates alongside it.
                conn.execute(
                    "DELETE FROM date_candidate WHERE uuid = ?", (record.uuid,)
                )
                conn.execute(
                    """
                    INSERT INTO asset (uuid, survey_status, survey_error)
                    VALUES (?, 'error', ?)
                    ON CONFLICT (uuid) DO UPDATE SET
                        survey_status = 'error', survey_error = excluded.survey_error
                    """,
                    (record.uuid, str(exc)),
                )
        conn.commit()

    set_stage(
        conn,
        "survey",
        "complete",
        notes=f"{written} written, {skipped} after cutoff, {errors} errors",
    )
    return SurveyResult(
        total=len(in_scope),
        written=written,
        skipped_after_cutoff=skipped,
        missing_originals=missing,
        errors=errors,
    )
