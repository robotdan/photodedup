from datetime import datetime, timedelta

import pytest

from photodedup.dates import Candidate, choose_date, rejection_reason

ADDED = datetime(2020, 12, 18, 9, 12, 48)
MTIME = datetime(2019, 5, 5, 5, 5, 5)
NOW = datetime(2026, 9, 18, 12, 0, 0)


def reason(value, **kwargs):
    kwargs.setdefault("date_added", ADDED)
    kwargs.setdefault("file_mtime", MTIME)
    kwargs.setdefault("floor", None)
    kwargs.setdefault("now", NOW)
    return rejection_reason(Candidate("exif", value, None), **kwargs)


def test_plausible_date_has_no_rejection_reason():
    assert reason(datetime(2003, 2, 23, 9, 8, 8)) is None


def test_rejects_date_at_or_after_import():
    assert reason(ADDED) == "at_or_after_date_added"
    assert reason(ADDED + timedelta(seconds=1)) == "at_or_after_date_added"


def test_accepts_date_one_second_before_import():
    assert reason(ADDED - timedelta(seconds=1)) is None


def test_rejects_future_date():
    assert reason(datetime(2030, 1, 1), date_added=None) == "future"


@pytest.mark.parametrize(
    "value",
    [
        datetime(1970, 1, 1, 0, 0, 0),
        datetime(1970, 1, 1, 23, 59, 59),
        datetime(1904, 1, 1, 6, 0, 0),
        datetime(1980, 1, 1, 0, 0, 0),
        datetime(2001, 1, 1, 0, 0, 0),
        datetime(1899, 12, 30, 0, 0, 0),
    ],
)
def test_rejects_sentinel_dates(value):
    assert reason(value) == "sentinel"


def test_rejects_date_equal_to_file_mtime():
    assert reason(MTIME) == "equals_file_mtime"


def test_rejects_date_below_floor():
    assert reason(datetime(1950, 1, 1), floor=datetime(1990, 1, 1)) == "below_floor"


def test_none_value_is_rejected_as_unparseable():
    assert reason(None) == "unparseable"


def test_earliest_surviving_candidate_wins():
    decision = choose_date(
        [
            Candidate("photos_db", ADDED, None),
            Candidate("exif", datetime(2003, 2, 23, 9, 8, 8), None),
        ],
        date_added=ADDED,
        file_mtime=MTIME,
        now=NOW,
    )
    assert decision.value == datetime(2003, 2, 23, 9, 8, 8)
    assert decision.source == "exif"
    assert ("photos_db", "at_or_after_date_added") in decision.rejected


def test_source_rank_breaks_exact_ties():
    when = datetime(2003, 2, 23, 9, 8, 8)
    decision = choose_date(
        [Candidate("filename", when, None), Candidate("takeout_json", when, None)],
        date_added=ADDED,
        file_mtime=MTIME,
        now=NOW,
    )
    assert decision.source == "takeout_json"


def test_no_surviving_candidate_yields_empty_decision():
    decision = choose_date(
        [Candidate("photos_db", ADDED, None)],
        date_added=ADDED,
        file_mtime=MTIME,
        now=NOW,
    )
    assert decision.value is None
    assert decision.source is None
    assert decision.needs_review is True
    assert decision.review_reason == "no_plausible_candidate"


def test_wide_disagreement_flags_review_but_still_picks():
    decision = choose_date(
        [
            Candidate("exif", datetime(2003, 2, 23, 9, 8, 8), None),
            Candidate("filename", datetime(2011, 7, 4, 10, 0, 0), None),
        ],
        date_added=ADDED,
        file_mtime=MTIME,
        now=NOW,
    )
    assert decision.value == datetime(2003, 2, 23, 9, 8, 8)
    assert decision.needs_review is True
    assert decision.review_reason == "candidates_disagree"


def test_timezone_sized_disagreement_does_not_flag_review():
    decision = choose_date(
        [
            Candidate("exif", datetime(2003, 2, 23, 9, 8, 8), None),
            Candidate("photos_db", datetime(2003, 2, 23, 17, 8, 8), None),
        ],
        date_added=ADDED,
        file_mtime=MTIME,
        now=NOW,
    )
    assert decision.needs_review is False


def test_single_surviving_candidate_does_not_flag_review():
    decision = choose_date(
        [Candidate("exif", datetime(2003, 2, 23, 9, 8, 8), None)],
        date_added=ADDED,
        file_mtime=MTIME,
        now=NOW,
    )
    assert decision.needs_review is False
