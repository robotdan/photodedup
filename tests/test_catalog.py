import sqlite3

import pytest

from photodedup.catalog import (
    SCHEMA_VERSION,
    SchemaVersionMismatch,
    get_run,
    get_stage,
    open_catalog,
    set_run,
    set_stage,
)


@pytest.fixture
def conn(tmp_path):
    return open_catalog(tmp_path / "catalog.sqlite")


def test_schema_is_created(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"asset", "date_candidate", "run", "stage", "schema_meta"} <= tables


def test_schema_version_is_recorded(conn):
    assert conn.execute("SELECT version FROM schema_meta").fetchone()["version"] == SCHEMA_VERSION


def test_open_is_idempotent(tmp_path):
    path = tmp_path / "catalog.sqlite"
    open_catalog(path).close()
    conn = open_catalog(path)
    assert conn.execute("SELECT version FROM schema_meta").fetchone()["version"] == SCHEMA_VERSION


def test_run_row_is_singleton(conn):
    set_run(conn, library_path="/a", cutoff_date_added="2026-09-18T00:00:00")
    set_run(conn, library_path="/b", cutoff_date_added="2026-09-19T00:00:00")
    assert conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"] == 1
    assert get_run(conn)["library_path"] == "/b"


def test_get_run_is_none_when_unset(conn):
    assert get_run(conn) is None


def test_stage_status_round_trips(conn):
    set_stage(conn, "survey", "running")
    set_stage(conn, "survey", "complete", notes="42 assets")
    row = get_stage(conn, "survey")
    assert row["status"] == "complete"
    assert row["notes"] == "42 assets"
    assert row["started_at"] is not None
    assert row["finished_at"] is not None


def test_stage_finished_at_only_set_on_terminal_status(conn):
    set_stage(conn, "hash", "running")
    assert get_stage(conn, "hash")["finished_at"] is None


def test_date_candidate_requires_known_source(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO date_candidate (uuid, source, value) VALUES ('u1', 'astrology', NULL)"
        )


def test_date_candidate_cascades_on_asset_delete(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    conn.execute(
        "INSERT INTO date_candidate (uuid, source, value) VALUES ('u1', 'exif', '2003-02-23T09:08:08')"
    )
    conn.execute("DELETE FROM asset WHERE uuid = 'u1'")
    assert conn.execute("SELECT COUNT(*) AS n FROM date_candidate").fetchone()["n"] == 0


def test_asset_hash_table_exists(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "asset_hash" in tables


def test_asset_hash_cascades_on_asset_delete(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    conn.execute(
        "INSERT INTO asset_hash (uuid, status) VALUES ('u1', 'ok')"
    )
    conn.execute("DELETE FROM asset WHERE uuid = 'u1'")
    assert conn.execute("SELECT COUNT(*) AS n FROM asset_hash").fetchone()["n"] == 0


def test_asset_hash_status_is_constrained(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO asset_hash (uuid, status) VALUES ('u1', 'banana')")


def test_asset_hash_is_one_row_per_asset(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    conn.execute("INSERT INTO asset_hash (uuid, status) VALUES ('u1', 'ok')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO asset_hash (uuid, status) VALUES ('u1', 'ok')")


def test_schema_version_is_two(conn):
    assert SCHEMA_VERSION == 2


def test_version_mismatch_is_refused(tmp_path):
    path = tmp_path / "catalog.sqlite"
    open_catalog(path).close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_meta SET version = 1")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaVersionMismatch) as excinfo:
        open_catalog(path)
    assert "re-run the survey" in str(excinfo.value)
