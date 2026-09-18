import json
from datetime import datetime

import pytest

from photodedup.catalog import get_run, open_catalog
from photodedup.library import AssetRecord
from photodedup.survey import MissingOriginalsError, run_survey

LIBRARY = "/tmp/Fake.photoslibrary"


@pytest.fixture
def conn(tmp_path):
    return open_catalog(tmp_path / "catalog.sqlite")


def record(uuid, *, added, photos_date=None, path="/tmp/x.jpg", filename="x.jpg"):
    return AssetRecord(
        uuid=uuid,
        kind="image",
        filename=filename,
        original_filename=filename,
        path=path,
        date_added=added,
        photos_date=photos_date,
    )


def no_exif(paths, **kwargs):
    return {}


def test_writes_one_asset_row_per_record(conn):
    records = [
        record("u1", added=datetime(2020, 12, 18, 9, 12, 48)),
        record("u2", added=datetime(2020, 12, 18, 9, 12, 49)),
    ]
    result = run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    assert result.written == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM asset").fetchone()["n"] == 2


def test_cutoff_is_max_date_added(conn):
    records = [
        record("u1", added=datetime(2020, 12, 18, 9, 12, 48)),
        record("u2", added=datetime(2026, 9, 18, 8, 0, 0)),
    ]
    run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    assert get_run(conn)["cutoff_date_added"] == "2026-09-18T08:00:00"


def test_records_after_stored_cutoff_are_skipped(conn):
    first = [record("u1", added=datetime(2020, 12, 18, 9, 12, 48))]
    run_survey(conn, first, library_path=LIBRARY, read_exif=no_exif)

    second = first + [record("u2", added=datetime(2026, 9, 18, 8, 0, 0))]
    result = run_survey(conn, second, library_path=LIBRARY, read_exif=no_exif)
    assert result.skipped_after_cutoff == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM asset").fetchone()["n"] == 1


def test_resume_does_not_rewrite_completed_rows(conn):
    records = [record("u1", added=datetime(2020, 12, 18, 9, 12, 48))]
    run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    result = run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    assert result.written == 0
    assert result.total == 1


def test_photos_db_date_becomes_a_candidate(conn):
    records = [
        record(
            "u1",
            added=datetime(2020, 12, 18, 9, 12, 48),
            photos_date=datetime(2003, 2, 23, 9, 8, 8),
        )
    ]
    run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    row = conn.execute(
        "SELECT * FROM date_candidate WHERE uuid='u1' AND source='photos_db'"
    ).fetchone()
    assert row["value"] == "2003-02-23T09:08:08"
    assert row["rejected_reason"] is None


def test_import_time_date_is_recorded_but_rejected(conn):
    added = datetime(2020, 12, 18, 9, 12, 48)
    records = [record("u1", added=added, photos_date=added)]
    run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    row = conn.execute(
        "SELECT * FROM date_candidate WHERE uuid='u1' AND source='photos_db'"
    ).fetchone()
    assert row["rejected_reason"] == "at_or_after_date_added"


def test_filename_date_becomes_a_candidate(conn):
    records = [
        record(
            "u1",
            added=datetime(2020, 12, 18, 9, 12, 48),
            filename="IMG_20140612_123456.jpg",
        )
    ]
    run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    row = conn.execute(
        "SELECT * FROM date_candidate WHERE uuid='u1' AND source='filename'"
    ).fetchone()
    assert row["value"] == "2014-06-12T12:34:56"


def test_exif_date_becomes_a_candidate(conn):
    def fake_exif(paths, **kwargs):
        return {"/tmp/x.jpg": {"EXIF:DateTimeOriginal": "2003:02:23 09:08:08"}}

    records = [record("u1", added=datetime(2020, 12, 18, 9, 12, 48))]
    run_survey(conn, records, library_path=LIBRARY, read_exif=fake_exif)
    row = conn.execute(
        "SELECT * FROM date_candidate WHERE uuid='u1' AND source='exif'"
    ).fetchone()
    assert row["value"] == "2003-02-23T09:08:08"


def test_keywords_and_albums_are_stored_as_json(conn):
    records = [
        AssetRecord(
            uuid="u1",
            kind="image",
            filename="x.jpg",
            path="/tmp/x.jpg",
            date_added=datetime(2020, 12, 18, 9, 12, 48),
            keywords=("utah", "moab"),
            albums=("Trips",),
        )
    ]
    run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    row = conn.execute("SELECT keywords, albums FROM asset WHERE uuid='u1'").fetchone()
    assert json.loads(row["keywords"]) == ["utah", "moab"]
    assert json.loads(row["albums"]) == ["Trips"]


def test_too_many_missing_originals_aborts(conn):
    records = [
        record("u1", added=datetime(2020, 12, 18, 9, 12, 48), path=None),
        record("u2", added=datetime(2020, 12, 18, 9, 12, 49), path=None),
    ]
    with pytest.raises(MissingOriginalsError) as excinfo:
        run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    assert "Download Originals" in str(excinfo.value)


def test_a_few_missing_originals_are_tolerated(conn):
    records = [
        record(f"u{i}", added=datetime(2020, 12, 18, 9, 12, 48)) for i in range(200)
    ]
    records.append(record("missing", added=datetime(2020, 12, 18, 9, 12, 48), path=None))
    result = run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    assert result.missing_originals == 1
    assert result.written == 201


def test_exif_error_column_is_populated_from_exif_tags(conn):
    def fake_exif(paths, **kwargs):
        return {
            "/tmp/broken.jpg": {"ExifTool:Error": "File format error"},
            "/tmp/x.jpg": {"EXIF:DateTimeOriginal": "2003:02:23 09:08:08"},
        }

    records = [
        record(
            "u1",
            added=datetime(2020, 12, 18, 9, 12, 48),
            path="/tmp/broken.jpg",
            filename="broken.jpg",
        ),
        record("u2", added=datetime(2020, 12, 18, 9, 12, 49)),
    ]
    run_survey(conn, records, library_path=LIBRARY, read_exif=fake_exif)

    broken = conn.execute(
        "SELECT exif_error FROM asset WHERE uuid='u1'"
    ).fetchone()
    clean = conn.execute(
        "SELECT exif_error FROM asset WHERE uuid='u2'"
    ).fetchone()
    assert broken["exif_error"] == "File format error"
    assert clean["exif_error"] is None
