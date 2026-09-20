"""SQLite catalog: the durable spine every pipeline stage reads and writes."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 4

DATE_SOURCES = ("takeout_json", "exif", "filename", "photos_db", "file_mtime")

TERMINAL_STAGE_STATUSES = ("complete", "failed")

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS run (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    library_path      TEXT NOT NULL,
    cutoff_date_added TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stage (
    name        TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS asset (
    uuid              TEXT PRIMARY KEY,
    filename          TEXT,
    original_filename TEXT,
    uti               TEXT,
    kind              TEXT,
    width             INTEGER,
    height            INTEGER,
    file_size         INTEGER,
    path              TEXT,
    thumb_path        TEXT,
    date_added        TEXT,
    photos_date       TEXT,
    photos_tz_offset  INTEGER,
    file_mtime        TEXT,
    is_live           INTEGER NOT NULL DEFAULT 0,
    has_raw           INTEGER NOT NULL DEFAULT 0,
    has_adjustments   INTEGER NOT NULL DEFAULT 0,
    is_favorite       INTEGER NOT NULL DEFAULT 0,
    is_hidden         INTEGER NOT NULL DEFAULT 0,
    in_trash          INTEGER NOT NULL DEFAULT 0,
    title             TEXT,
    description       TEXT,
    latitude          REAL,
    longitude         REAL,
    keywords          TEXT,
    albums            TEXT,
    import_uuid       TEXT,
    survey_status     TEXT NOT NULL DEFAULT 'pending',
    survey_error      TEXT,
    exif_error        TEXT
);

CREATE INDEX IF NOT EXISTS asset_survey_status ON asset (survey_status);
CREATE INDEX IF NOT EXISTS asset_date_added ON asset (date_added);

CREATE TABLE IF NOT EXISTS date_candidate (
    uuid            TEXT NOT NULL REFERENCES asset (uuid) ON DELETE CASCADE,
    source          TEXT NOT NULL CHECK (source IN {DATE_SOURCES!r}),
    value           TEXT,
    raw             TEXT,
    rejected_reason TEXT,
    PRIMARY KEY (uuid, source)
);

CREATE TABLE IF NOT EXISTS asset_hash (
    uuid         TEXT PRIMARY KEY REFERENCES asset (uuid) ON DELETE CASCADE,
    status       TEXT NOT NULL CHECK (status IN ('ok', 'error', 'no_original', 'skipped', 'truncated')),
    error        TEXT,
    scan_hash    TEXT,
    pixel_hash   TEXT,
    dhash        TEXT,
    phash        TEXT,
    video_hash   TEXT,
    pixel_width  INTEGER,
    pixel_height INTEGER,
    bytes_read   INTEGER,
    blank_rows   INTEGER,
    flat         INTEGER NOT NULL DEFAULT 0,
    hashed_at    TEXT
);

CREATE INDEX IF NOT EXISTS asset_hash_status ON asset_hash (status);
CREATE INDEX IF NOT EXISTS asset_hash_scan ON asset_hash (scan_hash);
CREATE INDEX IF NOT EXISTS asset_hash_pixel ON asset_hash (pixel_hash);
CREATE INDEX IF NOT EXISTS asset_hash_video ON asset_hash (video_hash);
CREATE INDEX IF NOT EXISTS asset_hash_dhash ON asset_hash (dhash);

CREATE TABLE IF NOT EXISTS dup_group (
    id     INTEGER PRIMARY KEY,
    signal TEXT NOT NULL CHECK (signal IN ('scan', 'pixel', 'video', 'dhash', 'prefix')),
    tier   TEXT NOT NULL CHECK (tier IN ('exact', 'candidate')),
    digest TEXT NOT NULL,
    size   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dup_member (
    group_id INTEGER NOT NULL REFERENCES dup_group (id) ON DELETE CASCADE,
    uuid     TEXT NOT NULL REFERENCES asset (uuid) ON DELETE CASCADE,
    PRIMARY KEY (group_id, uuid)
);

CREATE INDEX IF NOT EXISTS dup_member_uuid ON dup_member (uuid);
CREATE INDEX IF NOT EXISTS dup_group_tier ON dup_group (tier, signal);

CREATE TABLE IF NOT EXISTS dup_set (
    id       INTEGER PRIMARY KEY,
    tier     TEXT NOT NULL CHECK (tier IN ('exact', 'candidate')),
    kind     TEXT NOT NULL,
    size     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dup_set_member (
    set_id   INTEGER NOT NULL REFERENCES dup_set (id) ON DELETE CASCADE,
    uuid     TEXT NOT NULL REFERENCES asset (uuid) ON DELETE CASCADE,
    tier     TEXT NOT NULL CHECK (tier IN ('exact', 'candidate')),
    signals  TEXT NOT NULL,
    PRIMARY KEY (set_id, uuid)
);

CREATE UNIQUE INDEX IF NOT EXISTS dup_set_member_exact
    ON dup_set_member (uuid) WHERE tier = 'exact';
CREATE INDEX IF NOT EXISTS dup_set_member_uuid ON dup_set_member (uuid);
CREATE INDEX IF NOT EXISTS dup_set_tier ON dup_set (tier);

CREATE TABLE IF NOT EXISTS decision (
    set_id         INTEGER PRIMARY KEY REFERENCES dup_set (id) ON DELETE CASCADE,
    keeper_uuid    TEXT NOT NULL REFERENCES asset (uuid),
    keeper_reason  TEXT NOT NULL,
    date_value     TEXT,
    date_source    TEXT,
    date_rejected  TEXT,
    date_changes   INTEGER NOT NULL,
    resolvable     INTEGER NOT NULL,
    review_flags   TEXT NOT NULL,
    decided_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS decision_resolvable ON decision (resolvable);

CREATE TABLE IF NOT EXISTS decision_loser (
    set_id   INTEGER NOT NULL REFERENCES dup_set (id) ON DELETE CASCADE,
    uuid     TEXT NOT NULL REFERENCES asset (uuid),
    lost_on  TEXT NOT NULL,
    carries  TEXT NOT NULL,
    PRIMARY KEY (set_id, uuid)
);
"""


class SchemaVersionMismatch(RuntimeError):
    """The catalog on disk was written by a different schema version."""


def open_catalog(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the catalog at *path* with the schema applied."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    if conn.execute("SELECT COUNT(*) AS n FROM schema_meta").fetchone()["n"] == 0:
        conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()
    found = conn.execute("SELECT version FROM schema_meta").fetchone()["version"]
    if found != SCHEMA_VERSION:
        conn.close()
        raise SchemaVersionMismatch(
            f"catalog at {path} is schema version {found}, this code expects "
            f"{SCHEMA_VERSION}. Delete the catalog and re-run the survey; it is "
            "a derived artifact and takes about five minutes to rebuild."
        )
    return conn


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def set_run(conn: sqlite3.Connection, *, library_path: str, cutoff_date_added: str) -> None:
    conn.execute(
        """
        INSERT INTO run (id, library_path, cutoff_date_added, created_at)
        VALUES (1, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            library_path = excluded.library_path,
            cutoff_date_added = excluded.cutoff_date_added
        """,
        (library_path, cutoff_date_added, _now()),
    )
    conn.commit()


def get_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM run WHERE id = 1").fetchone()


def set_stage(
    conn: sqlite3.Connection, name: str, status: str, notes: str | None = None
) -> None:
    finished = _now() if status in TERMINAL_STAGE_STATUSES else None
    conn.execute(
        """
        INSERT INTO stage (name, status, started_at, finished_at, notes)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (name) DO UPDATE SET
            status = excluded.status,
            finished_at = excluded.finished_at,
            notes = excluded.notes
        """,
        (name, status, _now(), finished, notes),
    )
    conn.commit()


def get_stage(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM stage WHERE name = ?", (name,)).fetchone()


class StageIncomplete(RuntimeError):
    """A stage was asked to run before the stage it depends on finished."""


def require_stage_complete(
    conn: sqlite3.Connection, name: str, *, force: bool = False
) -> None:
    """Refuse unless stage *name* has reported complete, or *force* is set.

    Every stage after `hash` builds numbers that look final. Running one over
    a half-finished predecessor produces a report nobody can tell apart from
    a real one, so the default is to refuse and say why.
    """
    if force:
        return
    stage = get_stage(conn, name)
    status = stage["status"] if stage is not None else "never run"
    if status != "complete":
        raise StageIncomplete(
            f"the {name} stage is '{status}', not 'complete'. Finish "
            f"`photodedup {name}` first, or pass --force if you deliberately "
            "want results over a partial catalog."
        )
