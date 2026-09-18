from datetime import datetime, timedelta

import pytest

from photodedup.catalog import open_catalog
from photodedup.library import AssetRecord
from photodedup.report import (
    date_added_clusters,
    exif_vs_photos_db,
    photos_date_clusters,
    sentinel_breakdown,
    survey_report,
    timezone_artifacts,
)
from photodedup.survey import run_survey


@pytest.fixture
def conn(tmp_path):
    return open_catalog(tmp_path / "catalog.sqlite")


# An explicit far-future cutoff keeps a second bulk() call from being skipped
# as "added after the cutoff the first call stored".
FAR_FUTURE = datetime(2030, 1, 1)


def bulk(conn, n, *, added, photos_date=None, prefix="u"):
    records = [
        AssetRecord(
            uuid=f"{prefix}{i}",
            kind="image",
            filename="x.jpg",
            original_filename="x.jpg",
            path="/tmp/x.jpg",
            date_added=added + timedelta(seconds=i),
            photos_date=photos_date,
        )
        for i in range(n)
    ]
    run_survey(
        conn,
        records,
        library_path="/tmp/L",
        cutoff=FAR_FUTURE,
        read_exif=lambda p, **k: {},
    )


def test_import_spike_is_detected(conn):
    bulk(conn, 60, added=datetime(2020, 12, 18, 9, 0, 0))
    clusters = date_added_clusters(conn, min_size=50)
    assert clusters
    assert clusters[0][0].startswith("2020-12-18T09:00")
    assert clusters[0][1] == 60


def test_small_clusters_are_ignored(conn):
    bulk(conn, 5, added=datetime(2020, 12, 18, 9, 0, 0))
    assert date_added_clusters(conn, min_size=50) == []


def test_clusters_are_sorted_largest_first(conn):
    bulk(conn, 60, added=datetime(2020, 12, 18, 9, 0, 0), prefix="a")
    bulk(conn, 90, added=datetime(2021, 3, 1, 14, 0, 0), prefix="b")
    clusters = date_added_clusters(conn, min_size=50)
    assert [c[1] for c in clusters] == [90, 60]


def test_report_mentions_totals_and_rejections(conn):
    added = datetime(2020, 12, 18, 9, 0, 0)
    bulk(conn, 10, added=added, photos_date=added)
    report = survey_report(conn)
    assert "# Survey Report" in report
    assert "at_or_after_date_added" in report
    assert "10" in report


def test_report_on_empty_catalog_does_not_crash(conn):
    assert "# Survey Report" in survey_report(conn)


def test_report_shows_damaged_exif_count(conn):
    added = datetime(2020, 12, 18, 9, 0, 0)
    records = [
        AssetRecord(
            uuid="d0",
            kind="image",
            filename="bad.jpg",
            original_filename="bad.jpg",
            path="/tmp/bad.jpg",
            date_added=added,
        ),
        AssetRecord(
            uuid="d1",
            kind="image",
            filename="ok.jpg",
            original_filename="ok.jpg",
            path="/tmp/ok.jpg",
            date_added=added,
        ),
    ]

    def read_exif(paths, **k):
        return {"/tmp/bad.jpg": {"ExifTool:Error": "File format error"}}

    run_survey(
        conn,
        records,
        library_path="/tmp/L",
        cutoff=FAR_FUTURE,
        read_exif=read_exif,
    )

    report = survey_report(conn)
    assert "damaged (exiftool error)" in report
    assert "| damaged (exiftool error) | 1 |" in report


def asset(uuid, *, added, photos_date=None, filename="x.jpg", path=None):
    return AssetRecord(
        uuid=uuid,
        kind="image",
        filename=filename,
        original_filename=filename,
        path=path if path is not None else f"/tmp/{uuid}.jpg",
        date_added=added,
        photos_date=photos_date,
    )


def survey(conn, records, exif=None):
    exif = exif or {}
    run_survey(
        conn,
        records,
        library_path="/tmp/L",
        cutoff=FAR_FUTURE,
        read_exif=lambda p, **k: exif,
    )


ADDED = datetime(2020, 12, 18, 11, 53, 0)
TRUE_DATE = datetime(2003, 2, 23, 9, 8, 8)


def test_exif_vs_photos_db_covers_every_combination(conn):
    records = [
        # both usable and identical
        asset("equal", added=ADDED, photos_date=TRUE_DATE),
        # both usable, timezone-sized difference
        asset("close", added=ADDED, photos_date=TRUE_DATE + timedelta(hours=8)),
        # both usable, real conflict
        asset("far", added=ADDED, photos_date=datetime(2011, 7, 4, 10, 0, 0)),
        # cohort A: photos_db is the import stamp and gets rejected, EXIF is good
        asset("cohortA", added=ADDED, photos_date=ADDED),
        # photos_db only, no EXIF
        asset("photosonly", added=ADDED, photos_date=TRUE_DATE),
        # nothing usable at all
        asset("neither", added=ADDED, photos_date=None),
    ]
    exif = {
        f"/tmp/{u}.jpg": {"EXIF:DateTimeOriginal": "2003:02:23 09:08:08"}
        for u in ("equal", "close", "far", "cohortA")
    }
    survey(conn, records, exif)

    counts = dict(exif_vs_photos_db(conn))
    assert counts == {
        "both_equal": 1,
        "both_close": 1,
        "both_far": 1,
        "exif_only_photos_rejected": 1,
        "photos_only": 1,
        "neither": 1,
    }


def test_cohort_a_recovery_is_visible_in_the_report(conn):
    survey(
        conn,
        [asset("cohortA", added=ADDED, photos_date=ADDED)],
        {"/tmp/cohortA.jpg": {"EXIF:DateTimeOriginal": "2003:02:23 09:08:08"}},
    )
    report = survey_report(conn)
    assert "EXIF date vs Photos database date" in report
    assert "| EXIF usable, Photos date rejected (recoverable from EXIF) | 1 |" in report


def test_sentinel_breakdown_splits_by_value_and_source(conn):
    records = [
        asset("s1", added=ADDED, photos_date=datetime(2001, 1, 1, 4, 0, 0)),
        asset("s2", added=ADDED, photos_date=datetime(2001, 1, 1, 5, 0, 0)),
        asset("s3", added=ADDED, photos_date=datetime(1970, 1, 1, 0, 0, 0)),
    ]
    survey(conn, records)
    rows = sentinel_breakdown(conn)
    assert ("photos_db", "1970-01-01T00:00:00", 1) in rows
    assert ("photos_db", "2001-01-01T04:00:00", 1) in rows
    assert [r[2] for r in rows] == sorted([r[2] for r in rows], reverse=True)
    assert "Sentinel rejections by value" in survey_report(conn)


def test_photos_date_clusters_find_collapsed_capture_dates(conn):
    collapsed = datetime(2020, 12, 18, 9, 10, 0)
    records = [
        asset(f"c{i}", added=ADDED + timedelta(hours=i % 6), photos_date=collapsed)
        for i in range(60)
    ]
    survey(conn, records)
    clusters = photos_date_clusters(conn, min_size=50)
    assert clusters
    assert clusters[0][0].startswith("2020-12-18T09:00")
    assert clusters[0][1] == 60
    assert "Capture-date spikes" in survey_report(conn)


def test_photos_date_clusters_ignore_scattered_captures(conn):
    records = [
        asset(f"s{i}", added=ADDED, photos_date=datetime(2003, 2, 23) + timedelta(days=i))
        for i in range(60)
    ]
    survey(conn, records)
    assert photos_date_clusters(conn, min_size=50) == []


def test_timezone_artifacts_counted_separately(conn):
    records = [
        # 8 hours past date_added: timezone rendering, not corruption
        asset("tz", added=ADDED, photos_date=ADDED + timedelta(hours=8)),
        # well past a day: not a timezone artifact
        asset("late", added=ADDED, photos_date=ADDED + timedelta(days=3)),
        asset("fine", added=ADDED, photos_date=TRUE_DATE),
    ]
    survey(conn, records)
    assert timezone_artifacts(conn) == 1
    assert "timezone artifact" in survey_report(conn)


def test_error_rows_are_excluded_from_aggregates(conn):
    import photodedup.survey as survey_module

    real_write = survey_module._write_asset

    def flaky(c, r, tags):
        if r.uuid == "bad":
            raise RuntimeError("boom")
        return real_write(c, r, tags)

    records = [asset("good", added=ADDED, photos_date=TRUE_DATE), asset("bad", added=ADDED)]
    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(survey_module, "_write_asset", flaky)
    try:
        survey(conn, records)
    finally:
        mp.undo()

    report = survey_report(conn)
    assert "- Assets surveyed OK: **1**" in report
    assert "failed to survey (excluded from every count below): **1**" in report
    # The NULL-kind row the error handler creates must not appear.
    kinds = report.split("## Asset kinds")[1].split("##")[0]
    assert "| image | 1 |" in kinds
    assert "None" not in kinds
    assert "| survey errors | 1 |" in report
