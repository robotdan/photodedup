import json
from datetime import datetime

import pytest

from photodedup.arbitrate import run_arbitrate
from photodedup.catalog import open_catalog, set_stage
from photodedup.library import AssetState
from photodedup.verify import run_verify, verify_report
from tests.test_apply import add_asset, add_set, archived  # helpers

NOW = datetime(2026, 9, 20, 12, 0, 0)


@pytest.fixture
def applied(tmp_path):
    conn = open_catalog(tmp_path / "catalog.sqlite")
    set_stage(conn, "group", "complete")
    add_asset(conn, "k", size=20, photos_date="2020-12-18T09:10:00", added="2020-12-18T18:00:00")
    add_asset(conn, "l", size=10, photos_date="2014-05-12T17:00:00", exif="2014-05-12T17:00:00", albums=("B",))
    add_set(conn, 1, ["k", "l"])
    run_arbitrate(conn, now=NOW)
    archived(conn, "l")
    for uuid, action, detail in (
        ("l", "stage", {"album": "Dedup Delete 001"}),
        ("k", "date", {"date": "2014-05-12T17:00:00", "set_id": 1}),
        ("k", "merge", {"albums": ["B"], "losers": ["l"]}),
    ):
        conn.execute(
            "INSERT INTO apply_action (uuid, action, status, detail, previous, applied_at) VALUES (?, ?, 'done', ?, '{}', 'now')",
            (uuid, action, json.dumps(detail)),
        )
    conn.commit()
    return conn


def test_everything_in_order_before_deletion(applied):
    states = {
        "k": AssetState(True, datetime(2014, 5, 12, 17, 0, 0), ("A", "B")),
        "l": AssetState(True, datetime(2014, 5, 12, 17, 0, 0), ("B", "Dedup Delete 001")),
    }
    r = run_verify(applied, states=states)
    assert (r.losers_present, r.losers_gone, r.keepers_missing, r.dates_wrong, r.merges_missing) == (
        [("l", "Dedup Delete 001")], [], [], [], []
    )
    text = verify_report(r)
    assert "0, as required" in text and "Dedup Delete 001" in text


def test_after_deletion_and_a_bad_date(applied):
    states = {
        "k": AssetState(True, datetime(2020, 12, 18, 9, 10, 0), ("A",)),
        "l": AssetState(False, None, ()),
    }
    r = run_verify(applied, states=states)
    assert r.losers_gone == ["l"] and r.losers_present == []
    assert r.dates_wrong == [("k", "2014-05-12T17:00:00", "2020-12-18T09:10:00")]
    assert r.merges_missing == [("k", "B")]


def test_a_trashed_loser_counts_as_trashed_not_present_and_not_gone(applied):
    """After the user deletes a delete album, its losers sit in Recently
    Deleted for 30 days. Reporting them as still present would read as a
    failed deletion; reporting them as gone would overstate it."""
    states = {
        "k": AssetState(True, datetime(2014, 5, 12, 17, 0, 0), ("A", "B"), False),
        "l": AssetState(False, datetime(2014, 5, 12, 17, 0, 0), ("Dedup Delete 001",), True),
    }
    r = run_verify(applied, states=states)
    assert r.losers_trashed == ["l"]
    assert r.losers_present == [] and r.losers_gone == []
    assert "Recently Deleted (recoverable for 30 days): 1" in verify_report(r)


def test_missing_keeper_is_reported(applied):
    states = {"k": AssetState(False, None, ()), "l": AssetState(True, None, ())}
    r = run_verify(applied, states=states)
    assert r.keepers_missing == ["k"]
    assert "1 — STOP" in verify_report(r)


def test_stale_staged_loser_is_flagged(applied):
    """arbitrate re-run can flip a staged loser into a keeper; verify must
    not send it to a delete album silently."""
    applied.execute("DELETE FROM decision_loser WHERE uuid = 'l'")
    applied.commit()
    states = {
        "k": AssetState(True, datetime(2014, 5, 12, 17, 0, 0), ("A", "B")),
        "l": AssetState(True, datetime(2014, 5, 12, 17, 0, 0), ("B", "Dedup Delete 001")),
    }
    r = run_verify(applied, states=states)
    assert r.stale_staged == [("l", "Dedup Delete 001")]
    assert "STOP" in verify_report(r)
