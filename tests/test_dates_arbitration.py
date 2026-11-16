from datetime import datetime, timedelta

import pytest

from photodedup.dates import (
    IMPORT_STAMPS,
    SENTINEL_DATES,
    SENTINEL_WINDOW,
    Candidate,
    Pool,
    choose_date,
    choose_date_pooled,
    rejection_reason,
)

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


def test_photos_db_date_equal_to_file_mtime_is_rejected():
    assert (
        rejection_reason(
            Candidate("photos_db", MTIME, None),
            date_added=ADDED,
            file_mtime=MTIME,
            now=NOW,
        )
        == "equals_file_mtime"
    )


@pytest.mark.parametrize("source", ["exif", "filename", "takeout_json", "file_mtime"])
def test_non_photos_db_date_equal_to_file_mtime_survives(source):
    # A camera writes the file at the capture instant, so FileModifyDate ==
    # DateTimeOriginal exactly is the signature of an untouched original.
    assert (
        rejection_reason(
            Candidate(source, MTIME, None),
            date_added=ADDED,
            file_mtime=MTIME,
            now=NOW,
        )
        is None
    )


def test_file_mtime_candidate_equal_to_itself_survives():
    # survey.py passes the file_mtime candidate's own value as file_mtime, so
    # the old rule made rung 5 of the trust ladder dead by construction.
    assert (
        rejection_reason(
            Candidate("file_mtime", MTIME, None),
            date_added=ADDED,
            file_mtime=MTIME,
            now=NOW,
        )
        is None
    )


def test_true_date_wins_when_it_equals_mtime_and_photos_db_holds_import_time():
    # Regression for the inversion: both good sources sat exactly on mtime and
    # were discarded, leaving the wrong import date as the sole survivor, which
    # then won unflagged because one survivor has zero spread.
    true_date = datetime(2003, 2, 23, 9, 8, 8)
    import_date = ADDED - timedelta(minutes=5)
    decision = choose_date(
        [
            Candidate("exif", true_date, None),
            Candidate("filename", true_date, None),
            Candidate("photos_db", import_date, None),
        ],
        date_added=ADDED,
        file_mtime=true_date,
        now=NOW,
    )
    assert decision.value == true_date
    assert decision.source == "exif"


def test_2001_sentinel_covers_the_whole_day():
    """A clock-reset run spans a full day and must be rejected as one event.

    A narrower window split this library's 2001 run in half, rejecting the
    photos before 14:00 and accepting the rest as a genuine 2001 date.
    """
    assert reason(datetime(2001, 1, 1, 6, 0, 0)) == "sentinel"
    assert reason(datetime(2001, 1, 1, 20, 0, 0)) == "sentinel"
    assert reason(datetime(2001, 1, 2, 12, 0, 0)) is None


def test_rejects_date_below_floor():
    assert reason(datetime(1950, 1, 1), floor=datetime(1990, 1, 1)) == "below_floor"


def test_none_value_is_rejected_as_unparseable():
    assert reason(None) == "unparseable"


def test_earliest_surviving_candidate_wins():
    # A locally-scoped date_added, not the shared ADDED constant: ADDED sits
    # inside IMPORT_STAMPS[0] (which ends at 09:26:02), and this test is about
    # at_or_after_date_added, not the stamp rule.
    added = datetime(2020, 12, 18, 10, 0, 0)
    decision = choose_date(
        [
            Candidate("photos_db", added, None),
            Candidate("exif", datetime(2003, 2, 23, 9, 8, 8), None),
        ],
        date_added=added,
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


def test_sentinel_dates_carry_per_entry_windows():
    """The per-entry structure stays even though every entry now shares a window.

    It is the mechanism for narrowing a single ambiguous sentinel without
    touching the others, which is a judgement this library happened to resolve
    one way and another library may not.
    """
    windows = dict(SENTINEL_DATES)
    assert windows[datetime(1970, 1, 1)] == SENTINEL_WINDOW
    assert windows[datetime(1980, 1, 1)] == SENTINEL_WINDOW
    assert windows[datetime(2001, 1, 1)] == SENTINEL_WINDOW


STAMP_2020 = datetime(2020, 12, 18, 9, 10, 0)


def test_photos_db_inside_an_import_stamp_is_rejected():
    why = rejection_reason(
        Candidate("photos_db", STAMP_2020, None),
        date_added=datetime(2020, 12, 18, 18, 0, 0), file_mtime=MTIME, now=NOW,
    )
    assert why == "import_stamp"


def test_exif_inside_an_import_stamp_survives():
    """One asset in the 2023 batch carries a genuine EXIF date inside the
    interval. Stamps only ever reject what Photos recorded."""
    why = rejection_reason(
        Candidate("exif", datetime(2023, 1, 14, 11, 20, 0), None),
        date_added=datetime(2023, 1, 14, 12, 0, 0), file_mtime=MTIME, now=NOW,
    )
    assert why is None


def test_photos_db_one_second_outside_a_stamp_survives():
    start, end = IMPORT_STAMPS[0]
    added = datetime(2020, 12, 31, 0, 0, 0)
    for value in (start - timedelta(seconds=1), end + timedelta(seconds=1)):
        assert rejection_reason(
            Candidate("photos_db", value, None),
            date_added=added, file_mtime=MTIME, now=NOW,
        ) is None
    for value in (start, end):
        assert rejection_reason(
            Candidate("photos_db", value, None),
            date_added=added, file_mtime=MTIME, now=NOW,
        ) == "import_stamp"


def test_stamps_can_be_switched_off():
    why = rejection_reason(
        Candidate("photos_db", STAMP_2020, None),
        date_added=datetime(2020, 12, 18, 18, 0, 0), file_mtime=MTIME, now=NOW,
        stamps=(),
    )
    assert why is None


def test_choose_date_prefers_exif_over_a_stamped_photos_date():
    decision = choose_date(
        [Candidate("photos_db", STAMP_2020, None),
         Candidate("exif", datetime(2014, 5, 12, 17, 0, 0), None)],
        date_added=datetime(2020, 12, 18, 18, 0, 0), file_mtime=MTIME, now=NOW,
    )
    assert decision.value == datetime(2014, 5, 12, 17, 0, 0)
    assert decision.source == "exif"
    assert ("photos_db", "import_stamp") in decision.rejected
    assert decision.needs_review is False


def test_pooled_choice_rescues_a_date_across_members():
    """Cohort A member has only a stamped Photos date; its twin carries EXIF."""
    stamped = Pool(
        (Candidate("photos_db", STAMP_2020, None),),
        date_added=datetime(2020, 12, 18, 18, 0, 0), file_mtime=MTIME,
    )
    twin = Pool(
        (Candidate("exif", datetime(2014, 5, 12, 17, 0, 0), None),
         Candidate("photos_db", datetime(2014, 5, 12, 17, 0, 0), None)),
        date_added=datetime(2015, 1, 1), file_mtime=MTIME,
    )
    decision = choose_date_pooled([stamped, twin], now=NOW)
    assert decision.value == datetime(2014, 5, 12, 17, 0, 0)
    assert decision.source == "exif"
    assert decision.needs_review is False


def test_pooled_choice_rejects_against_each_members_own_date_added():
    """A value after member A's date_added but before member B's survives via B."""
    value = datetime(2019, 6, 1)
    a = Pool((Candidate("photos_db", value, None),), date_added=datetime(2018, 1, 1), file_mtime=MTIME)
    b = Pool((Candidate("photos_db", value, None),), date_added=datetime(2020, 1, 1), file_mtime=MTIME)
    decision = choose_date_pooled([a, b], now=NOW)
    assert decision.value == value
    assert ("photos_db", "at_or_after_date_added") in decision.rejected


def test_pooled_choice_flags_disagreement_across_members():
    a = Pool((Candidate("exif", datetime(2014, 1, 1), None),), date_added=datetime(2020, 1, 1), file_mtime=MTIME)
    b = Pool((Candidate("exif", datetime(2016, 1, 1), None),), date_added=datetime(2020, 1, 1), file_mtime=MTIME)
    decision = choose_date_pooled([a, b], now=NOW)
    assert decision.value == datetime(2014, 1, 1)
    assert decision.needs_review is True
    assert decision.review_reason == "candidates_disagree"


def test_pooled_choice_with_no_survivors_needs_review():
    a = Pool((Candidate("photos_db", STAMP_2020, None),), date_added=datetime(2020, 12, 18, 18, 0), file_mtime=MTIME)
    decision = choose_date_pooled([a], now=NOW)
    assert decision.value is None
    assert decision.review_reason == "no_plausible_candidate"


def test_choose_date_is_the_one_pool_case():
    candidates = [Candidate("exif", datetime(2014, 1, 1), None)]
    single = choose_date(candidates, date_added=ADDED, file_mtime=MTIME, now=NOW)
    pooled = choose_date_pooled([Pool(tuple(candidates), ADDED, MTIME)], now=NOW)
    assert single == pooled


def _pool(*candidates):
    return Pool(tuple(candidates), ADDED, MTIME)


def test_quicktime_epoch_shifted_date_loses_to_the_date_it_shadows():
    """VID_20120421_161537: one copy's Photos date read 1946, exactly the
    1904-to-1970 gap before the other copy's correct 2012 date."""
    keeper = _pool(Candidate("photos_db", datetime(2012, 4, 21, 14, 15, 37)))
    loser = _pool(Candidate("photos_db", datetime(1946, 4, 21, 14, 23, 10)))
    decision = choose_date_pooled([keeper, loser], now=NOW)
    assert decision.value == datetime(2012, 4, 21, 14, 15, 37)
    assert ("photos_db", "epoch_shift") in decision.rejected
    assert not decision.needs_review


def test_an_old_date_with_no_shifted_twin_survives():
    decision = choose_date_pooled(
        [_pool(Candidate("exif", datetime(1946, 4, 21, 14, 23, 10)),
               Candidate("filename", datetime(1946, 4, 21, 14, 23, 10)))],
        now=NOW,
    )
    assert decision.value == datetime(1946, 4, 21, 14, 23, 10)
