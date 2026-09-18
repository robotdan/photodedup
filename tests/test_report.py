from datetime import datetime, timedelta

import pytest

from photodedup.catalog import open_catalog
from photodedup.library import AssetRecord
from photodedup.report import date_added_clusters, survey_report
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
