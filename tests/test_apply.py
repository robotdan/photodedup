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


def test_require_archive_needs_the_archive_stage_complete(conn):
    add_asset(conn, "k", size=20); add_asset(conn, "l", size=10)
    add_set(conn, 1, ["k", "l"]); run_arbitrate(conn, now=NOW)
    conn.execute(
        """INSERT INTO archive_file (uuid, kind, dest, bytes, sha256, verified_at)
           VALUES ('l', 'original', '2014/05/l.jpg', 1, 'x', 'now')"""
    )
    set_stage(conn, "archive", "running")
    conn.commit()
    with pytest.raises(StageIncomplete) as exc:
        require_archive(conn)
    assert "archive stage" in str(exc.value)


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


def test_merge_failure_preserves_previous_across_partial_write_and_retry(conn):
    add_asset(conn, "k", size=20)
    add_asset(conn, "l", size=10, keywords=("y",), favorite=1)
    add_set(conn, 1, ["k", "l"]); run_arbitrate(conn, now=NOW); archived(conn, "l")
    fake = fake_for(conn)
    fake.fail_on = {("set_favorite", "k")}
    pre_write_previous = {"title": "", "description": "", "keywords": [], "favorite": False, "albums": []}

    result = run_merge(conn, fake, now=NOW)
    assert result.failed == 1
    row = conn.execute("SELECT * FROM apply_action WHERE uuid='k' AND action='merge'").fetchone()
    assert row["status"] == "failed"
    assert json.loads(row["previous"]) == pre_write_previous
    assert json.loads(row["detail"])["written"] == {"keywords": ["y"]}

    fake.fail_on = set()
    result2 = run_merge(conn, fake, now=NOW)
    assert result2.done == 1
    row2 = conn.execute("SELECT * FROM apply_action WHERE uuid='k' AND action='merge'").fetchone()
    assert row2["status"] == "done"
    assert json.loads(row2["previous"]) == pre_write_previous


def test_merge_remerges_when_new_carries_appear_after_rearbitrate(merged):
    fake = fake_for(merged)
    run_merge(merged, fake, now=NOW)

    add_asset(merged, "l2", size=5, albums=("C",))
    merged.execute("INSERT INTO dup_set_member (set_id, uuid, tier, signals) VALUES (1, 'l2', 'exact', 'scan')")
    merged.execute("UPDATE dup_set SET size = size + 1 WHERE id = 1")
    merged.commit()
    run_arbitrate(merged, now=NOW)
    archived(merged, "l2")
    fake.photos["l2"] = {"title": "", "description": "", "keywords": [], "favorite": False, "date": None}

    result = run_merge(merged, fake, now=NOW)
    assert result.done == 1
    assert fake.album_members("C") == ["k"]
    row = merged.execute("SELECT * FROM apply_action WHERE uuid='k' AND action='merge'").fetchone()
    assert sorted(json.loads(row["detail"])["losers"]) == ["l", "l2"]


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


from photodedup.apply import BLANK_ALBUM, FOLDER, run_stage, self_test, write_losers_csv


@pytest.fixture
def staged(conn):
    for i in range(7):
        add_asset(conn, f"k{i}", size=20); add_asset(conn, f"l{i}", size=10)
        add_set(conn, i + 1, [f"k{i}", f"l{i}"])
    # a blank-image set: keeper goes to the blank album, loser is a normal loser
    add_asset(conn, "bk", size=5); add_asset(conn, "bl", size=5)
    conn.execute("UPDATE asset_hash SET flat = 1, blank_rows = 1 WHERE uuid IN ('bk', 'bl')")
    add_set(conn, 8, ["bk", "bl"])
    run_arbitrate(conn, now=NOW)
    archived(conn, *[f"l{i}" for i in range(7)], "bl")
    return conn


def test_stage_batches_losers_into_numbered_albums(staged):
    fake = fake_for(staged)
    result = run_stage(staged, fake, batch=3, now=NOW)
    assert (result.done, result.failed, result.skipped) == (9, 0, 0)
    assert fake.album_members("Dedup Delete 001", FOLDER) == ["bl", "l0", "l1"]
    assert fake.album_members("Dedup Delete 002", FOLDER) == ["l2", "l3", "l4"]
    assert fake.album_members("Dedup Delete 003", FOLDER) == ["l5", "l6"]
    assert fake.album_members(BLANK_ALBUM) == ["bk"]
    row = staged.execute("SELECT * FROM apply_action WHERE uuid='l2' AND action='stage'").fetchone()
    assert json.loads(row["detail"]) == {"album": "Dedup Delete 002"}
    blank = staged.execute("SELECT * FROM apply_action WHERE uuid='bk' AND action='stage_blank'").fetchone()
    assert json.loads(blank["detail"]) == {"album": BLANK_ALBUM}


def test_stage_resumes_without_reshuffling(staged):
    fake = fake_for(staged)
    run_stage(staged, fake, batch=3, now=NOW)
    add_asset(staged, "k9", size=20); add_asset(staged, "l9", size=10)
    add_set(staged, 9, ["k9", "l9"]); run_arbitrate(staged, now=NOW); archived(staged, "l9")
    fake.photos["k9"] = fake.photos["l9"] = {"title": "", "description": "", "keywords": [], "favorite": False, "date": None}
    # arbitrate rewrote decisions but apply_action rows survive; the new loser
    # lands in the next partial album.
    result = run_stage(staged, fake, batch=3, now=NOW)
    assert (result.done, result.skipped) == (1, 9)
    assert fake.album_members("Dedup Delete 003", FOLDER) == ["l5", "l6", "l9"]


def test_stage_never_stages_a_keeper_or_a_candidate(staged):
    add_asset(staged, "c1", size=10); add_asset(staged, "c2", size=10)
    add_set(staged, 20, ["c1", "c2"], tier="candidate"); run_arbitrate(staged, now=NOW)
    fake = fake_for(staged)
    run_stage(staged, fake, batch=500, now=NOW)
    everything = sum((fake.album_members(f"Dedup Delete 00{i}", FOLDER) for i in (1, 2, 3)), [])
    assert not any(u.startswith("k") for u in everything)
    assert "c1" not in everything and "c2" not in everything


def test_stage_failure_is_isolated(staged):
    fake = FakePhotos({r["uuid"]: {} for r in staged.execute("SELECT uuid FROM asset")}, fail_on={("add_to_album", "l3")})
    result = run_stage(staged, fake, batch=500, now=NOW)
    assert result.failed == 1 and result.done == 8
    assert staged.execute("SELECT status FROM apply_action WHERE uuid='l3'").fetchone()["status"] == "failed"


def test_stage_dry_run_creates_nothing(staged):
    fake = fake_for(staged)
    run_stage(staged, fake, dry_run=True, now=NOW)
    assert fake.albums == {}


def test_losers_csv(staged, tmp_path):
    fake = fake_for(staged)
    run_stage(staged, fake, batch=3, now=NOW)
    out = tmp_path / "losers.csv"
    assert write_losers_csv(staged, out) == 8
    import csv
    rows = list(csv.DictReader(out.open()))
    assert rows[0].keys() >= {"uuid", "original_filename", "photos_date", "keeper_uuid", "set_id", "lost_on", "album", "archive_dest"}
    by_uuid = {r["uuid"]: r for r in rows}
    assert by_uuid["l2"]["album"] == "Dedup Delete 002" and by_uuid["l2"]["archive_dest"] == "2014/05/l2.jpg"


def test_self_test_round_trips_and_restores():
    fake = FakePhotos({"p": {"title": "Original", "date": datetime(2010, 1, 1, 12, 0, 0)}})
    lines = self_test(fake, "p", now=NOW)
    assert any("date round-trip: ok" in line for line in lines)
    assert fake.get("p").title == "Original"
    assert fake.get("p").date == datetime(2010, 1, 1, 12, 0, 0)
    assert fake.albums == {}


def test_self_test_reports_a_drifting_date():
    fake = FakePhotos({"p": {"title": "t", "date": datetime(2010, 1, 1, 12, 0, 0)}})
    real = fake.set_date
    fake.set_date = lambda uuid, value: real(uuid, value.replace(hour=value.hour + 1))
    with pytest.raises(RuntimeError) as exc:
        self_test(fake, "p", now=NOW)
    assert "drift" in str(exc.value)
    assert fake.get("p").title == "t"
