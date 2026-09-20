import itertools
import json
from datetime import datetime

import pytest

from photodedup.arbitrate import (
    KEEPER_RULES,
    Member,
    carries,
    choose_keeper,
    run_arbitrate,
)
from photodedup.catalog import StageIncomplete, open_catalog, set_stage

NOW = datetime(2026, 9, 19, 12, 0, 0)


def member(uuid, **overrides):
    base = dict(
        uuid=uuid, hash_status="ok", file_size=1000, is_favorite=False,
        has_adjustments=False, title=None, description=None, albums=(), keywords=(),
        date_added="2020-01-01T00:00:00", photos_date="2014-05-12T17:00:00",
        file_mtime=None, uti="public.jpeg", original_filename="IMG_0001.jpg",
    )
    base.update(overrides)
    return Member(**base)


def test_rule_names_are_fixed():
    assert KEEPER_RULES == (
        "intact", "rendered", "file_size", "favorite", "adjustments", "caption",
        "albums", "clean_name", "date_added", "uuid",
    )


def test_intact_beats_truncated_no_matter_what():
    """The truncated member is bigger, favourited, edited, captioned, in more
    albums and added earlier. It still loses."""
    trunc = member("a", hash_status="truncated", file_size=9999, is_favorite=True,
                   has_adjustments=True, title="t", albums=("x", "y"),
                   date_added="2000-01-01T00:00:00")
    intact = member("b")
    keeper, reason, losers = choose_keeper([trunc, intact])
    assert keeper.uuid == "b"
    assert reason == "intact"
    assert losers == [(trunc, "intact")]


def test_bigger_file_wins():
    keeper, reason, _ = choose_keeper([member("a", file_size=1), member("b", file_size=2)])
    assert keeper.uuid == "b" and reason == "file_size"


def test_favorite_breaks_a_size_tie():
    keeper, reason, _ = choose_keeper([member("a"), member("b", is_favorite=True)])
    assert keeper.uuid == "b" and reason == "favorite"


def test_adjustments_then_caption_then_albums():
    k, r, _ = choose_keeper([member("a"), member("b", has_adjustments=True)])
    assert (k.uuid, r) == ("b", "adjustments")
    k, r, _ = choose_keeper([member("a"), member("b", description="x")])
    assert (k.uuid, r) == ("b", "caption")
    k, r, _ = choose_keeper([member("a", albums=("one",)), member("b", albums=("one", "two"))])
    assert (k.uuid, r) == ("b", "albums")


def test_whitespace_caption_does_not_count():
    k, r, _ = choose_keeper([member("a", title="   "), member("b")])
    assert r == "uuid" and k.uuid == "a"


def test_earlier_date_added_wins():
    k, r, _ = choose_keeper([
        member("a", date_added="2021-01-01T00:00:00"),
        member("b", date_added="2020-01-01T00:00:00"),
    ])
    assert (k.uuid, r) == ("b", "date_added")


def test_missing_date_added_loses_to_any_date():
    k, r, _ = choose_keeper([member("a", date_added=None), member("b")])
    assert (k.uuid, r) == ("b", "date_added")


def test_lowest_uuid_is_the_final_tie_break():
    k, r, _ = choose_keeper([member("zzz"), member("aaa")])
    assert (k.uuid, r) == ("aaa", "uuid")


def test_each_loser_records_the_rule_that_beat_it():
    a = member("a", file_size=5)
    b = member("b", file_size=5, is_favorite=True)
    c = member("c", file_size=1)
    keeper, reason, losers = choose_keeper([a, b, c])
    assert keeper.uuid == "b" and reason == "favorite"
    assert dict((m.uuid, why) for m, why in losers) == {"a": "favorite", "c": "file_size"}


def test_input_order_never_changes_the_winner():
    ms = [member("a", file_size=3), member("b", file_size=3, has_adjustments=True),
          member("c", file_size=9, hash_status="truncated"), member("d", file_size=3)]
    winners = {choose_keeper(list(p))[0].uuid for p in itertools.permutations(ms)}
    assert winners == {"b"}


def test_keeper_is_never_smaller_than_an_intact_loser():
    ms = [member(str(i), file_size=s) for i, s in enumerate((5, 9, 2, 9, 7))]
    keeper, _, losers = choose_keeper(ms)
    assert all(keeper.file_size >= m.file_size for m, _ in losers)


def test_keeper_smaller_tripwire_fires_on_a_wrong_keeper():
    """Computed from the data, not the rule, so a broken rank_key shows up
    as a blocking flag rather than a deletion."""
    from photodedup.arbitrate import review_flags
    from photodedup.dates import Decision
    small = member("small", file_size=1)
    big = member("big", file_size=2)
    decision = Decision(datetime(2014, 5, 12, 17, 0, 0), "exif", (), False, None)
    flags = review_flags("exact", [small, big], small, decision)
    assert flags == ["keeper_smaller"]


def test_unhashed_member_never_wins_and_blocks():
    errored = member("e", hash_status="error", file_size=9999)
    ok = member("k", file_size=1)
    keeper, reason, _ = choose_keeper([errored, ok])
    assert keeper.uuid == "k" and reason == "intact"


def test_duplicate_album_names_count_once():
    k, r, _ = choose_keeper([member("a", albums=("one", "one")), member("b", albums=("one", "two"))])
    assert (k.uuid, r) == ("b", "albums")


def test_carries_reports_what_the_keeper_lacks():
    keeper = member("k", albums=("A",), keywords=("x",), title=None, description=None)
    loser = member("l", albums=("A", "B"), keywords=("x", "y"), is_favorite=True,
                   title="Beach", description="IMG_0001")
    got = carries(keeper, loser)
    assert got == {
        "albums": ["B"], "keywords": ["y"], "favorite": True, "title": "Beach",
    }


def test_carries_ignores_filename_echoes_and_shorter_captions():
    keeper = member("k", title="A long title")
    loser = member("l", title="Short", description="IMG_0001.jpg")
    assert carries(keeper, loser) == {}


def test_carries_is_empty_when_nothing_is_gained():
    assert carries(member("k", albums=("A",)), member("l", albums=("A",))) == {}


# --- end to end over a small catalog ---------------------------------------


@pytest.fixture
def conn(tmp_path):
    conn = open_catalog(tmp_path / "catalog.sqlite")
    set_stage(conn, "group", "complete")
    return conn


def add_asset(conn, uuid, *, size=1000, added="2020-01-01T00:00:00",
              photos_date="2014-05-12T17:00:00", uti="public.jpeg",
              hash_status="ok", favorite=0, albums=(), exif=None, title=None):
    conn.execute(
        """INSERT INTO asset (uuid, kind, uti, file_size, date_added, photos_date,
                              is_favorite, albums, keywords, title, survey_status,
                              original_filename)
           VALUES (?, 'image', ?, ?, ?, ?, ?, ?, '[]', ?, 'ok', 'IMG_0001.jpg')""",
        (uuid, uti, size, added, photos_date, favorite, json.dumps(list(albums)), title),
    )
    conn.execute("INSERT INTO asset_hash (uuid, status) VALUES (?, ?)", (uuid, hash_status))
    conn.execute(
        "INSERT INTO date_candidate (uuid, source, value) VALUES (?, 'photos_db', ?)",
        (uuid, photos_date),
    )
    if exif:
        conn.execute(
            "INSERT INTO date_candidate (uuid, source, value) VALUES (?, 'exif', ?)",
            (uuid, exif),
        )
    conn.commit()


def add_set(conn, set_id, uuids, *, tier="exact"):
    conn.execute(
        "INSERT INTO dup_set (id, tier, kind, size) VALUES (?, ?, 'image', ?)",
        (set_id, tier, len(uuids)),
    )
    for u in uuids:
        conn.execute(
            "INSERT INTO dup_set_member (set_id, uuid, tier, signals) VALUES (?, ?, ?, 'scan')",
            (set_id, u, tier),
        )
    conn.commit()


def decision(conn, set_id):
    return conn.execute("SELECT * FROM decision WHERE set_id = ?", (set_id,)).fetchone()


def test_refuses_before_group_is_complete(tmp_path):
    fresh = open_catalog(tmp_path / "fresh.sqlite")
    with pytest.raises(StageIncomplete):
        run_arbitrate(fresh)


def test_exact_set_without_flags_is_resolvable(conn):
    add_asset(conn, "a", size=10)
    add_asset(conn, "b", size=20)
    add_set(conn, 1, ["a", "b"])
    result = run_arbitrate(conn, now=NOW)
    assert (result.sets, result.resolvable, result.review) == (1, 1, 0)
    d = decision(conn, 1)
    assert d["keeper_uuid"] == "b" and d["keeper_reason"] == "file_size"
    assert d["resolvable"] == 1 and d["review_flags"] == ""
    loser = conn.execute("SELECT * FROM decision_loser WHERE set_id = 1").fetchone()
    assert loser["uuid"] == "a" and loser["lost_on"] == "file_size"
    assert json.loads(loser["carries"]) == {}


def test_date_is_rescued_from_the_twin(conn):
    """Cohort A keeper with a stamped Photos date; the loser carries EXIF."""
    add_asset(conn, "a", size=20, photos_date="2020-12-18T09:10:00", added="2020-12-18T18:00:00")
    add_asset(conn, "b", size=10, photos_date="2014-05-12T17:00:00", exif="2014-05-12T17:00:00")
    add_set(conn, 1, ["a", "b"])
    result = run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert d["keeper_uuid"] == "a"
    assert d["date_value"] == "2014-05-12T17:00:00" and d["date_source"] == "exif"
    assert d["date_changes"] == 1
    assert ["photos_db", "import_stamp"] in json.loads(d["date_rejected"])
    assert result.dates_rescued == 1
    assert d["resolvable"] == 1


def test_disagreeing_dates_flag_but_do_not_block(conn):
    add_asset(conn, "a", size=20, exif="2014-01-01T00:00:00")
    add_asset(conn, "b", size=10, exif="2016-01-01T00:00:00")
    add_set(conn, 1, ["a", "b"])
    run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert "dates_disagree" in d["review_flags"].split(",")
    assert d["resolvable"] == 1


def test_candidate_tier_is_never_resolvable(conn):
    add_asset(conn, "a")
    add_asset(conn, "b")
    add_set(conn, 1, ["a", "b"], tier="candidate")
    result = run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert d["resolvable"] == 0 and "candidate_tier" in d["review_flags"]
    assert result.review == 1


def test_png_multiframe_and_truncated_flags_block(conn):
    add_asset(conn, "a", uti="public.png")
    add_asset(conn, "b", uti="public.png")
    add_set(conn, 1, ["a", "b"])
    add_asset(conn, "c", uti="com.compuserve.gif")
    add_asset(conn, "d", uti="com.compuserve.gif")
    add_set(conn, 2, ["c", "d"])
    add_asset(conn, "e", size=5)
    add_asset(conn, "f", size=50, hash_status="truncated")
    add_set(conn, 3, ["e", "f"])
    run_arbitrate(conn, now=NOW)
    assert decision(conn, 1)["review_flags"] == "png"
    assert decision(conn, 2)["review_flags"] == "multiframe"
    d3 = decision(conn, 3)
    assert d3["review_flags"] == "truncated_member" and d3["keeper_uuid"] == "e"
    assert all(decision(conn, i)["resolvable"] == 0 for i in (1, 2, 3))


def test_size_spread_flags_but_does_not_block(conn):
    add_asset(conn, "a", size=3_000_000)
    add_asset(conn, "b", size=1_000_000)
    add_set(conn, 1, ["a", "b"])
    run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert d["review_flags"] == "size_spread" and d["resolvable"] == 1
    assert d["keeper_uuid"] == "a"


def test_no_plausible_date_blocks(conn):
    add_asset(conn, "a", photos_date="1970-01-01T00:00:00")
    add_asset(conn, "b", photos_date="1970-01-01T00:00:00")
    add_set(conn, 1, ["a", "b"])
    run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert d["date_value"] is None
    assert "no_plausible_date" in d["review_flags"] and d["resolvable"] == 0


def test_loser_carries_albums_and_favorite(conn):
    add_asset(conn, "a", size=20, albums=("Trips",))
    add_asset(conn, "b", size=10, albums=("Trips", "Best"), favorite=1, title="Beach")
    add_set(conn, 1, ["a", "b"])
    run_arbitrate(conn, now=NOW)
    loser = conn.execute("SELECT carries FROM decision_loser WHERE set_id = 1").fetchone()
    assert json.loads(loser["carries"]) == {"albums": ["Best"], "favorite": True, "title": "Beach"}


def test_rerun_replaces_decisions(conn):
    add_asset(conn, "a", size=10)
    add_asset(conn, "b", size=20)
    add_set(conn, 1, ["a", "b"])
    run_arbitrate(conn, now=NOW)
    run_arbitrate(conn, now=NOW)
    assert conn.execute("SELECT COUNT(*) AS n FROM decision").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM decision_loser").fetchone()["n"] == 1


def test_empty_catalog_completes_with_zero(conn):
    result = run_arbitrate(conn, now=NOW)
    assert result.sets == 0


def test_keeper_smaller_blocks_resolvability(conn, monkeypatch):
    import photodedup.arbitrate as arb
    add_asset(conn, "a", size=10)
    add_asset(conn, "b", size=20)
    add_set(conn, 1, ["a", "b"])
    real = arb.choose_keeper

    def wrong(members):
        keeper, reason, losers = real(members)
        # Swap: hand back the runner-up as keeper.
        loser, why = losers[0]
        return loser, why, [(keeper, why)]

    monkeypatch.setattr(arb, "choose_keeper", wrong)
    run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert d["keeper_uuid"] == "a"
    assert "keeper_smaller" in d["review_flags"].split(",")
    assert d["resolvable"] == 0


def test_unhashed_member_flag_blocks_resolvability(conn):
    add_asset(conn, "a", size=10, hash_status="error")
    add_asset(conn, "b", size=20)
    add_set(conn, 1, ["a", "b"])
    run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert d["keeper_uuid"] == "b"
    assert "unhashed_member" in d["review_flags"].split(",")
    assert d["resolvable"] == 0


def test_decided_at_honours_injected_now(conn):
    add_asset(conn, "a", size=10)
    add_asset(conn, "b", size=20)
    add_set(conn, 1, ["a", "b"])
    run_arbitrate(conn, now=NOW)
    assert decision(conn, 1)["decided_at"] == NOW.isoformat()


# --- edited copies are never losers ----------------------------------------


def test_edited_member_is_never_a_loser():
    from photodedup.arbitrate import split_protected

    plain = member("p", file_size=10)
    crop = member("c", file_size=10, has_adjustments=True)
    keeper, reason, losers = choose_keeper([plain, crop])
    assert keeper.uuid == "c"
    losers, kept = split_protected(losers)
    assert [m.uuid for m, _ in losers] == ["p"]
    assert [m.uuid for m in kept] == []


def test_two_crops_and_two_plain_copies_keep_both_crops(conn):
    """The set the user found: four identical originals, two carrying crops.
    Both crops stay as their own photos; only one plain copy is removable."""
    add_asset(conn, "plain1", size=10, albums=("Others",))
    add_asset(conn, "crop1", size=10)
    add_asset(conn, "fav", size=10, favorite=1)
    add_asset(conn, "crop2", size=10)
    conn.execute("UPDATE asset SET has_adjustments = 1 WHERE uuid IN ('crop1', 'crop2')")
    conn.commit()
    add_set(conn, 1, ["plain1", "crop1", "fav", "crop2"])
    run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert d["keeper_uuid"] == "fav"
    losers = conn.execute("SELECT uuid, carries FROM decision_loser WHERE set_id = 1").fetchall()
    assert [r["uuid"] for r in losers] == ["plain1"]
    assert json.loads(losers[0]["carries"]) == {"albums": ["Others"]}
    assert "kept_edits" in d["review_flags"].split(",")
    assert d["resolvable"] == 1


def test_all_edited_set_removes_nothing(conn):
    add_asset(conn, "a", size=10)
    add_asset(conn, "b", size=10)
    conn.execute("UPDATE asset SET has_adjustments = 1")
    conn.commit()
    add_set(conn, 1, ["a", "b"])
    run_arbitrate(conn, now=NOW)
    assert conn.execute("SELECT COUNT(*) AS n FROM decision_loser").fetchone()["n"] == 0
    assert "kept_edits" in decision(conn, 1)["review_flags"]


def test_edited_keeper_still_records_plain_losers(conn):
    add_asset(conn, "plain", size=10)
    add_asset(conn, "edited", size=10)
    conn.execute("UPDATE asset SET has_adjustments = 1 WHERE uuid = 'edited'")
    conn.commit()
    add_set(conn, 1, ["plain", "edited"])
    run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert d["keeper_uuid"] == "edited"
    assert [r["uuid"] for r in conn.execute("SELECT uuid FROM decision_loser")] == ["plain"]
    assert "kept_edits" not in d["review_flags"]


# --- rendered completeness, blank images, clean names ---------------------


def test_rule_names_include_rendered_and_clean_name():
    assert KEEPER_RULES == (
        "intact", "rendered", "file_size", "favorite", "adjustments", "caption",
        "albums", "clean_name", "date_added", "uuid",
    )


def test_fewer_blank_rows_beats_bigger_file():
    partial = member("p", file_size=2_066_603, blank_rows=144)
    full = member("f", file_size=1_968_457, blank_rows=0)
    keeper, reason, losers = choose_keeper([partial, full])
    assert keeper.uuid == "f" and reason == "rendered"


def test_all_damaged_picks_the_least_damaged():
    a = member("a", file_size=1_259_385, blank_rows=64)
    b = member("b", file_size=1_147_802, blank_rows=79, hash_status="truncated")
    keeper, reason, _ = choose_keeper([a, b])
    assert keeper.uuid == "a"


def test_clean_name_breaks_a_tie_before_date_added():
    suffixed = member("s", original_filename="IMG_0001 (1).jpg", date_added="2019-01-01T00:00:00")
    clean = member("c", original_filename="IMG_0001.jpg", date_added="2021-01-01T00:00:00")
    keeper, reason, _ = choose_keeper([suffixed, clean])
    assert keeper.uuid == "c" and reason == "clean_name"


def test_partial_render_flag_is_informational(conn):
    add_asset(conn, "a", size=20)
    add_asset(conn, "b", size=10)
    conn.execute("UPDATE asset_hash SET blank_rows = 30 WHERE uuid = 'a'")
    conn.execute("UPDATE asset_hash SET blank_rows = 0 WHERE uuid = 'b'")
    conn.commit()
    add_set(conn, 1, ["a", "b"])
    run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert d["keeper_uuid"] == "b" and d["keeper_reason"] == "rendered"
    assert "partial_render" in d["review_flags"].split(",")
    assert d["resolvable"] == 1


def test_blank_image_flag_when_every_member_is_flat(conn):
    add_asset(conn, "a", size=10)
    add_asset(conn, "b", size=10)
    conn.execute("UPDATE asset_hash SET flat = 1, blank_rows = 30")
    conn.commit()
    add_set(conn, 1, ["a", "b"])
    run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert "blank_image" in d["review_flags"].split(",")
    assert d["resolvable"] == 1
