import json
from datetime import datetime

import pytest

from photodedup.apply import (
    date_targets,
    merge_targets,
    plan_lines,
    require_archive,
    run_dates,
    run_merge,
)
from photodedup.arbitrate import run_arbitrate
from photodedup.catalog import StageIncomplete, open_catalog, set_stage
from tests.fake_photos import FakePhotos

NOW = datetime(2026, 9, 20, 12, 0, 0)


@pytest.fixture
def conn(tmp_path):
    conn = open_catalog(tmp_path / "catalog.sqlite")
    set_stage(conn, "group", "complete")
    return conn


def add_asset(conn, uuid, *, size=10, photos_date="2014-05-12T17:00:00", albums=(),
              keywords=(), favorite=0, title=None, description=None, exif=None,
              added="2020-01-01T00:00:00"):
    conn.execute(
        """INSERT INTO asset (uuid, kind, uti, file_size, path, date_added, photos_date,
                              is_favorite, albums, keywords, title, description,
                              survey_status, original_filename)
           VALUES (?, 'image', 'public.jpeg', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?)""",
        (uuid, size, f"/lib/{uuid}.jpg", added, photos_date, favorite,
         json.dumps(list(albums)), json.dumps(list(keywords)), title, description, f"{uuid}.jpg"),
    )
    conn.execute("INSERT INTO asset_hash (uuid, status) VALUES (?, 'ok')", (uuid,))
    conn.execute("INSERT INTO date_candidate (uuid, source, value) VALUES (?, 'photos_db', ?)", (uuid, photos_date))
    if exif:
        conn.execute("INSERT INTO date_candidate (uuid, source, value) VALUES (?, 'exif', ?)", (uuid, exif))
    conn.commit()


def add_set(conn, set_id, uuids, *, tier="exact"):
    conn.execute("INSERT INTO dup_set (id, tier, kind, size) VALUES (?, ?, 'image', ?)", (set_id, tier, len(uuids)))
    conn.executemany(
        "INSERT INTO dup_set_member (set_id, uuid, tier, signals) VALUES (?, ?, ?, 'scan')",
        [(set_id, u, tier) for u in uuids],
    )
    conn.commit()


def archived(conn, *uuids):
    for u in uuids:
        conn.execute(
            """INSERT OR IGNORE INTO archive_file (uuid, kind, dest, bytes, sha256, verified_at)
               VALUES (?, 'original', ?, 1, 'x', 'now')""", (u, f"2014/05/{u}.jpg"),
        )
    set_stage(conn, "archive", "complete")
    conn.commit()


def fake_for(conn):
    return FakePhotos({
        r["uuid"]: {
            "title": r["title"] or "", "description": r["description"] or "",
            "keywords": json.loads(r["keywords"] or "[]"), "favorite": bool(r["is_favorite"]),
            "date": datetime.fromisoformat(r["photos_date"]) if r["photos_date"] else None,
        }
        for r in conn.execute("SELECT * FROM asset")
    })


@pytest.fixture
def merged(conn):
    """keeper 'k' lacks what loser 'l' carries."""
    add_asset(conn, "k", size=20, albums=("A",), keywords=("x",))
    add_asset(conn, "l", size=10, albums=("A", "B"), keywords=("x", "y"), favorite=1,
              title="Beach", description="sand")
    add_set(conn, 1, ["k", "l"])
    run_arbitrate(conn, now=NOW)
    archived(conn, "l")
    return conn


def test_require_archive_counts_missing_losers(conn):
    add_asset(conn, "k", size=20); add_asset(conn, "l", size=10)
    add_set(conn, 1, ["k", "l"]); run_arbitrate(conn, now=NOW)
    with pytest.raises(StageIncomplete) as exc:
        require_archive(conn)
    assert "1" in str(exc.value)
    archived(conn, "l")
    require_archive(conn)


def test_merge_targets_come_from_carries(merged):
    [t] = merge_targets(merged)
    assert t.keeper == "k" and t.loser == "l"
    assert t.carries == {"albums": ["B"], "keywords": ["y"], "favorite": True, "title": "Beach", "description": "sand"}


def test_merge_writes_everything_and_records_previous(merged):
    fake = fake_for(merged)
    fake.ensure_album("A"); fake.add_to_album(fake.ensure_album("A"), ["k", "l"])
    result = run_merge(merged, fake, now=NOW)
    assert (result.done, result.failed, result.skipped) == (1, 0, 0)
    k = fake.get("k")
    assert k.title == "Beach" and k.description == "sand" and k.favorite is True
    assert set(k.keywords) == {"x", "y"}
    assert fake.album_members("B") == ["k"]
    row = merged.execute("SELECT * FROM apply_action WHERE uuid='k' AND action='merge'").fetchone()
    assert row["status"] == "done"
    assert json.loads(row["previous"]) == {"title": "", "description": "", "keywords": ["x"], "favorite": False, "albums": ["A"]}
    assert json.loads(row["detail"])["albums"] == ["B"]


def test_merge_is_idempotent(merged):
    fake = fake_for(merged)
    run_merge(merged, fake, now=NOW)
    second = run_merge(merged, fake, now=NOW)
    assert (second.done, second.skipped) == (0, 1)
    assert fake.album_members("B") == ["k"]


def test_merge_never_overwrites_a_longer_keeper_caption(conn):
    add_asset(conn, "k", size=20, title="A much longer title")
    add_asset(conn, "l", size=10, title="Short")
    add_set(conn, 1, ["k", "l"]); run_arbitrate(conn, now=NOW); archived(conn, "l")
    fake = fake_for(conn)
    run_merge(conn, fake, now=NOW)
    assert fake.get("k").title == "A much longer title"


def test_merge_failure_is_recorded_and_run_continues(conn):
    add_asset(conn, "k1", size=20); add_asset(conn, "l1", size=10, favorite=1)
    add_asset(conn, "k2", size=20); add_asset(conn, "l2", size=10, favorite=1)
    add_set(conn, 1, ["k1", "l1"]); add_set(conn, 2, ["k2", "l2"])
    run_arbitrate(conn, now=NOW); archived(conn, "l1", "l2")
    fake = FakePhotos({u: {} for u in ("k1", "l1", "k2", "l2")}, fail_on={("set_favorite", "k1")})
    result = run_merge(conn, fake, now=NOW)
    assert (result.done, result.failed) == (1, 1)
    rows = {r["uuid"]: r["status"] for r in conn.execute("SELECT uuid, status FROM apply_action")}
    assert rows == {"k1": "failed", "k2": "done"}
    assert fake.get("k2").favorite is True


def test_merge_dry_run_writes_nothing(merged):
    fake = fake_for(merged)
    result = run_merge(merged, fake, dry_run=True, now=NOW)
    assert result.done == 0
    assert fake.get("k").title == ""
    assert merged.execute("SELECT COUNT(*) AS n FROM apply_action").fetchone()["n"] == 0


def test_merge_refuses_without_archive(conn):
    add_asset(conn, "k", size=20); add_asset(conn, "l", size=10, favorite=1)
    add_set(conn, 1, ["k", "l"]); run_arbitrate(conn, now=NOW)
    with pytest.raises(StageIncomplete):
        run_merge(conn, FakePhotos({}), now=NOW)


@pytest.fixture
def dated(conn):
    """Set 1: rescued date with no disagreement (stamped photos_db, exif twin).
    Set 2: rescued date WITH disagreement (two surviving exif years apart)."""
    add_asset(conn, "k1", size=20, photos_date="2020-12-18T09:10:00", added="2020-12-18T18:00:00")
    add_asset(conn, "l1", size=10, photos_date="2014-05-12T17:00:00", exif="2014-05-12T17:00:00")
    add_set(conn, 1, ["k1", "l1"])
    add_asset(conn, "k2", size=20, photos_date="2016-01-01T00:00:00", exif="2016-01-01T00:00:00")
    add_asset(conn, "l2", size=10, photos_date="2014-01-01T00:00:00", exif="2014-01-01T00:00:00")
    add_set(conn, 2, ["k2", "l2"])
    run_arbitrate(conn, now=NOW)
    archived(conn, "l1", "l2")
    return conn


def test_date_targets_gate_on_disagreement(dated):
    assert [t.keeper for t in date_targets(dated, include_disagreeing=False)] == ["k1"]
    assert [t.keeper for t in date_targets(dated, include_disagreeing=True)] == ["k1", "k2"]


def test_dates_are_written_verified_and_previous_recorded(dated):
    fake = fake_for(dated)
    result = run_dates(dated, fake, include_disagreeing=True, now=NOW)
    assert (result.done, result.failed) == (2, 0)
    assert fake.get("k1").date == datetime(2014, 5, 12, 17, 0, 0)
    assert fake.get("k2").date == datetime(2014, 1, 1, 0, 0, 0)
    row = dated.execute("SELECT * FROM apply_action WHERE uuid='k1' AND action='date'").fetchone()
    assert json.loads(row["previous"]) == {"date": "2020-12-18T09:10:00"}
    assert json.loads(row["detail"]) == {"date": "2014-05-12T17:00:00"}


def test_date_readback_mismatch_is_failed(dated, monkeypatch):
    fake = fake_for(dated)
    real = fake.set_date

    def drift(uuid, value):
        real(uuid, value.replace(hour=value.hour + 1))

    monkeypatch.setattr(fake, "set_date", drift)
    result = run_dates(dated, fake, now=NOW)
    assert result.failed == 1
    row = dated.execute("SELECT * FROM apply_action WHERE uuid='k1' AND action='date'").fetchone()
    assert row["status"] == "failed" and "2014-05-12T18:00:00" in row["detail"]


def test_dates_skip_done_rows(dated):
    fake = fake_for(dated)
    run_dates(dated, fake, now=NOW)
    assert run_dates(dated, fake, now=NOW).skipped == 1


def test_plan_lines_summarise(dated):
    lines = "\n".join(plan_lines(dated, include_disagreeing=False))
    assert "1 keeper date" in lines and "1 more with --include-disagreeing-dates" in lines
