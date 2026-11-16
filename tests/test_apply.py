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
    stale_date_targets,
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
    # 'bk' is the blank-image keeper: it goes into 'Dedup Delete blank' for
    # the user to delete, so require_archive wants it archived like a loser.
    archived(conn, *[f"l{i}" for i in range(7)], "bl", "bk")
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
    # lands in a fresh album, never in one the user may already have emptied.
    result = run_stage(staged, fake, batch=3, now=NOW)
    assert (result.done, result.skipped) == (1, 9)
    assert fake.album_members("Dedup Delete 003", FOLDER) == ["l5", "l6"]
    assert fake.album_members("Dedup Delete 004", FOLDER) == ["l9"]


def test_stage_never_stages_a_keeper_or_a_candidate(staged):
    add_asset(staged, "c1", size=10); add_asset(staged, "c2", size=10)
    add_set(staged, 20, ["c1", "c2"], tier="candidate"); run_arbitrate(staged, now=NOW)
    fake = fake_for(staged)
    run_stage(staged, fake, batch=500, now=NOW)
    everything = sum((fake.album_members(f"Dedup Delete 00{i}", FOLDER) for i in (1, 2, 3)), [])
    assert not any(u.startswith("k") for u in everything)
    assert "c1" not in everything and "c2" not in everything


def test_stage_failure_is_isolated(staged):
    """Losers go in one add per album, so a failure costs that album's batch
    and nothing else. The batch holding 'l3' is 'Dedup Delete 002'; 001 and
    003 still go in, and the next run retries only the three that failed."""
    fake = FakePhotos({r["uuid"]: {} for r in staged.execute("SELECT uuid FROM asset")}, fail_on={("add_to_album", "l3")})
    result = run_stage(staged, fake, batch=3, now=NOW)
    # 5 losers in the two surviving albums, plus the blank keeper
    assert (result.done, result.failed) == (6, 3)
    statuses = {
        r["uuid"]: r["status"]
        for r in staged.execute("SELECT uuid, status FROM apply_action WHERE action = 'stage'")
    }
    assert sorted(u for u, st in statuses.items() if st == "failed") == ["l2", "l3", "l4"]
    assert fake.album_members("Dedup Delete 001", FOLDER) == ["bl", "l0", "l1"]
    assert fake.album_members("Dedup Delete 003", FOLDER) == ["l5", "l6"]

    fake.fail_on = set()
    fake.calls.clear()
    second = run_stage(staged, fake, batch=3, now=NOW)
    assert (second.done, second.failed, second.skipped) == (3, 0, 6)
    assert [u for op, u in fake.calls if op == "add_to_album"] == ["l2", "l3", "l4"]


def test_stage_adds_each_album_in_one_call(staged):
    """One AppleScript round trip per album, not per photo: 16,854 losers is
    34 calls, not 16,854."""
    calls: list[tuple[str, list[str]]] = []
    fake = fake_for(staged)
    real = fake.add_to_album

    def record(album_id, uuids):
        calls.append((fake.album_name(album_id), list(uuids)))
        return real(album_id, uuids)

    fake.add_to_album = record
    run_stage(staged, fake, batch=3, now=NOW)
    assert calls == [
        ("Dedup Delete 001", ["bl", "l0", "l1"]),
        ("Dedup Delete 002", ["l2", "l3", "l4"]),
        ("Dedup Delete 003", ["l5", "l6"]),
        (BLANK_ALBUM, ["bk"]),
    ]


def test_stage_dry_run_creates_nothing(staged):
    fake = fake_for(staged)
    run_stage(staged, fake, dry_run=True, now=NOW)
    assert fake.albums == {}


def test_stage_ignores_a_non_numeric_album_name_when_resuming(staged):
    """A stray/legacy 'stage' row whose album name doesn't end in a number
    must not blow up the index parsing that finds where to resume."""
    fake = fake_for(staged)
    # 'bk' is a real asset that never gets a 'stage' action (it's a blank-image
    # keeper, staged separately as 'stage_blank'), so this row is a harmless
    # stand-in for a stray/legacy album name with no numeric suffix.
    staged.execute(
        """INSERT INTO apply_action (uuid, action, status, detail, previous, applied_at)
           VALUES ('bk', 'stage', 'done', ?, NULL, ?)""",
        (json.dumps({"album": "Dedup Delete legacy"}), NOW.isoformat()),
    )
    staged.commit()
    result = run_stage(staged, fake, batch=3, now=NOW)
    assert result.failed == 0
    assert result.done == 9


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


def test_merge_records_previous_before_the_first_write(merged):
    """A crash between the first write and the last must not leave the keeper
    changed with nothing on record: `previous` is the only undo there is."""
    fake = fake_for(merged)

    def interrupt(uuid, values):
        raise KeyboardInterrupt("user pressed ctrl-c mid-merge")

    fake.set_keywords = interrupt
    with pytest.raises(KeyboardInterrupt):
        run_merge(merged, fake, now=NOW)

    # the album add already happened; the row proves what 'k' looked like first
    assert fake.album_members("B") == ["k"]
    row = merged.execute("SELECT * FROM apply_action WHERE uuid='k' AND action='merge'").fetchone()
    assert row["status"] == "failed"
    assert json.loads(row["detail"]) == {"in_progress": True}
    assert json.loads(row["previous"]) == {
        "title": "", "description": "", "keywords": ["x"], "favorite": False, "albums": [],
    }


def test_dates_record_the_old_date_before_writing_the_new_one(dated):
    fake = fake_for(dated)

    def interrupt(uuid, value):
        raise KeyboardInterrupt("user pressed ctrl-c mid-write")

    fake.set_date = interrupt
    with pytest.raises(KeyboardInterrupt):
        run_dates(dated, fake, now=NOW)
    row = dated.execute("SELECT * FROM apply_action WHERE uuid='k1' AND action='date'").fetchone()
    assert row["status"] == "failed"
    assert json.loads(row["previous"]) == {"date": "2020-12-18T09:10:00"}


def test_require_archive_names_the_photos_it_is_missing(conn):
    add_asset(conn, "k", size=20); add_asset(conn, "l", size=10)
    add_set(conn, 1, ["k", "l"]); run_arbitrate(conn, now=NOW)
    with pytest.raises(StageIncomplete) as exc:
        require_archive(conn)
    assert "l.jpg (l)" in str(exc.value)


def test_self_test_round_trips_and_restores():
    fake = FakePhotos({"p": {"title": "Original", "date": datetime(2010, 1, 1, 12, 0, 0)}})
    lines = self_test(fake, "p", now=NOW)
    assert lines[0] == "original title: 'Original'"
    assert lines[1] == "original date: 2010-01-01T12:00:00"
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


def test_self_test_restores_date_and_album_even_when_the_title_restore_fails():
    """One failed restore must not skip the others; the photo keeps the marker
    date and the test album otherwise."""
    fake = FakePhotos({"p": {"title": "Original", "date": datetime(2010, 1, 1, 12, 0, 0)}})
    calls = {"n": 0}
    real = fake.set_title

    def fail_on_restore(uuid, value):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("AppleScript timed out")
        real(uuid, value)

    fake.set_title = fail_on_restore
    with pytest.raises(RuntimeError) as exc:
        self_test(fake, "p", now=NOW)
    assert "could not restore title" in str(exc.value)
    assert fake.get("p").date == datetime(2010, 1, 1, 12, 0, 0)
    assert fake.albums == {}


def test_self_test_refuses_a_photo_with_no_capture_date_before_touching_it():
    fake = FakePhotos({"p": {"title": "t", "date": None}})
    with pytest.raises(RuntimeError) as exc:
        self_test(fake, "p", now=NOW)
    assert "no capture date" in str(exc.value)
    assert fake.albums == {}
    assert fake.calls == []


# --- displayed-date correction ---------------------------------------------


def _displayed_from(fake, skew_hours=0):
    from datetime import timedelta

    def displayed(uuids):
        return {u: (fake.get(u).date + timedelta(hours=skew_hours)) if fake.get(u).date else None for u in uuids}

    return displayed


def test_dates_done_when_displayed_matches(dated):
    fake = fake_for(dated)
    result = run_dates(dated, fake, include_disagreeing=True, now=NOW, displayed=_displayed_from(fake))
    assert (result.done, result.failed) == (2, 0)
    d = json.loads(dated.execute("SELECT detail FROM apply_action WHERE uuid='k1'").fetchone()["detail"])
    assert d["date"] == "2014-05-12T17:00:00" and d["corrections"] == 0


def test_dates_correct_a_one_hour_display_drift(dated):
    """Photos shows the written time an hour early; the second write shifts
    an hour late so the shown time lands where the catalog wants it."""
    from datetime import timedelta

    fake = fake_for(dated)
    result = run_dates(dated, fake, now=NOW, displayed=_displayed_from(fake, skew_hours=-1))
    assert (result.done, result.failed) == (1, 0)
    assert fake.get("k1").date == datetime(2014, 5, 12, 18, 0, 0)
    d = json.loads(dated.execute("SELECT detail FROM apply_action WHERE uuid='k1'").fetchone()["detail"])
    assert d["date"] == "2014-05-12T17:00:00" and d["written"] == "2014-05-12T18:00:00" and d["corrections"] == 1


def test_dates_give_up_when_display_never_converges(dated):
    from datetime import timedelta

    fake = fake_for(dated)

    def wandering(uuids):
        # Whatever is written, Photos shows the same unrelated date, so no
        # shift can ever land the wanted value.
        return {u: datetime(1999, 1, 1, 0, 0, 0) for u in uuids}

    result = run_dates(dated, fake, now=NOW, displayed=wandering)
    assert (result.done, result.failed) == (0, 1)
    row = dated.execute("SELECT status, detail FROM apply_action WHERE uuid='k1'").fetchone()
    assert row["status"] == "failed" and "displayed" in json.loads(row["detail"])


def test_redo_reapplies_done_dates_and_keeps_the_original_previous(dated):
    fake = fake_for(dated)
    run_dates(dated, fake, now=NOW)
    fake.photos["k1"]["date"] = datetime(2014, 5, 12, 16, 0, 0)   # what verify found: an hour off
    result = run_dates(dated, fake, now=NOW, displayed=_displayed_from(fake, skew_hours=-1), redo=True)
    assert result.done == 1 and result.skipped == 0
    row = dated.execute("SELECT previous FROM apply_action WHERE uuid='k1'").fetchone()
    assert json.loads(row["previous"]) == {"date": "2020-12-18T09:10:00"}


# --- staging is confirmed against the library ------------------------------


def _membership_from(fake):
    def membership(names):
        return {n: set(fake.album_members(n, FOLDER)) | set(fake.album_members(n)) for n in names}
    return membership


def test_stage_records_done_only_when_the_library_shows_the_photo(staged):
    fake = fake_for(staged)
    result = run_stage(staged, fake, batch=3, now=NOW, membership=_membership_from(fake))
    assert (result.done, result.failed) == (9, 0)


def test_stage_records_failed_when_the_add_did_not_take(staged):
    class LyingPhotos(FakePhotos):
        """Photos' albumAdd returns the requested ids even when nothing was added."""

        def add_to_album(self, album_id, uuids):
            return list(uuids)

    fake = LyingPhotos({r["uuid"]: {} for r in staged.execute("SELECT uuid FROM asset")})
    result = run_stage(staged, fake, batch=3, now=NOW, membership=_membership_from(fake))
    assert result.done == 1   # only the blank keeper path, which is unverified
    assert result.failed == 8
    row = staged.execute("SELECT status, detail FROM apply_action WHERE uuid='l0'").fetchone()
    assert row["status"] == "failed" and "prompt" in json.loads(row["detail"])["error"]
    # The retry goes into fresh albums and, with an honest writer, succeeds.
    honest = fake_for(staged)
    again = run_stage(staged, honest, batch=3, now=NOW, membership=_membership_from(honest))
    assert again.done == 8 and again.failed == 0
    names = {json.loads(r["detail"])["album"] for r in staged.execute(
        "SELECT detail FROM apply_action WHERE action='stage' AND status='done'")}
    assert names and min(names) >= "Dedup Delete 004"


def test_stage_never_tops_up_an_existing_album(staged):
    fake = fake_for(staged)
    run_stage(staged, fake, batch=3, now=NOW)
    staged.execute("DELETE FROM apply_action WHERE uuid IN ('l0', 'l1')")
    staged.commit()
    run_stage(staged, fake, batch=3, now=NOW)
    albums = {json.loads(r["detail"])["album"]: r["uuid"] for r in staged.execute(
        "SELECT detail, uuid FROM apply_action WHERE uuid IN ('l0','l1')")}
    assert set(albums) == {"Dedup Delete 004"}
    from collections import Counter
    sizes = Counter(json.loads(r["detail"])["album"] for r in staged.execute(
        "SELECT detail FROM apply_action WHERE action='stage' AND status='done'"))
    assert max(sizes.values()) <= 3


def test_dates_for_named_sets_target_every_member_regardless_of_flags(dated):
    targets = date_targets(dated, include_disagreeing=False, sets=[2])
    assert sorted(t.keeper for t in targets) == ["k2", "l2"]
    assert all(t.value == datetime(2014, 1, 1, 0, 0, 0) for t in targets)


def test_run_dates_with_sets_writes_every_member(dated):
    fake = fake_for(dated)
    result = run_dates(dated, fake, now=NOW, sets=[2], displayed=_displayed_from(fake))
    assert (result.done, result.failed) == (2, 0)
    assert fake.get("k2").date == fake.get("l2").date == datetime(2014, 1, 1, 0, 0, 0)


# --- stale dates: written under an older rule -----------------------------


@pytest.fixture
def shifted(conn):
    """Keeper 'k' had the right 2012 date; loser 'l' a QuickTime-epoch 1946
    one, which an earlier rule chose and wrote onto 'k'."""
    add_asset(conn, "k", size=20, photos_date="2012-04-21T14:15:37", exif="2012-04-21T21:23:10")
    add_asset(conn, "l", size=10, photos_date="1946-04-21T14:23:10", exif="2012-04-21T21:23:10")
    add_set(conn, 1, ["k", "l"])
    run_arbitrate(conn, now=NOW)
    archived(conn, "l")
    conn.execute(
        """INSERT INTO apply_action (uuid, action, status, detail, previous, applied_at)
           VALUES ('k', 'date', 'done', ?, ?, 'then')""",
        (json.dumps({"date": "1946-04-21T14:23:10"}), json.dumps({"date": "2012-04-21T15:15:37"})),
    )
    conn.commit()
    return conn


def test_stale_targets_restore_the_keepers_own_date(shifted):
    [t] = stale_date_targets(shifted)
    assert (t.keeper, t.value) == ("k", datetime(2012, 4, 21, 14, 15, 37))


def test_stale_targets_ignore_dates_the_rules_still_choose(dated):
    run_dates(dated, fake_for(dated), include_disagreeing=True, now=NOW)
    assert stale_date_targets(dated) == []


def test_run_dates_rewrites_stale_targets_and_keeps_the_first_previous(shifted):
    fake = fake_for(shifted)
    fake.photos["k"]["date"] = datetime(1946, 4, 21, 14, 23, 10)
    result = run_dates(shifted, fake, now=NOW, targets=stale_date_targets(shifted))
    assert (result.done, result.skipped) == (1, 0)
    assert fake.get("k").date == datetime(2012, 4, 21, 14, 15, 37)
    row = shifted.execute("SELECT detail, previous FROM apply_action WHERE uuid='k' AND action='date'").fetchone()
    assert json.loads(row["detail"]) == {"date": "2012-04-21T14:15:37"}
    assert json.loads(row["previous"]) == {"date": "2012-04-21T15:15:37"}
    assert stale_date_targets(shifted) == []


def test_stale_targets_keep_a_date_written_from_a_reviewed_look_alike_set(conn):
    add_asset(conn, "k", size=20, photos_date="2011-01-06T10:08:02", added="2026-04-14T20:59:27")
    add_asset(conn, "l", size=10, photos_date="2020-12-18T09:24:12", added="2020-12-18T23:00:10")
    add_asset(conn, "c", size=30, photos_date="2007-07-05T12:58:40", exif="2007-07-05T12:58:40")
    add_set(conn, 1, ["k", "l"])
    add_set(conn, 2, ["k", "c"], tier="candidate")
    run_arbitrate(conn, now=NOW)
    conn.execute(
        """INSERT INTO apply_action (uuid, action, status, detail, previous, applied_at)
           VALUES ('k', 'date', 'done', ?, NULL, 'then')""",
        (json.dumps({"date": "2007-07-05T12:58:40"}),),
    )
    conn.commit()
    assert stale_date_targets(conn) == []
