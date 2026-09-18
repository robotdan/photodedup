"""SQLite catalog: the durable spine every pipeline stage reads and writes."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

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
    survey_error      TEXT
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
"""


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
