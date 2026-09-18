from datetime import datetime

import pytest

from photodedup.dates import parse_exif_datetime, parse_filename_datetime


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2003:02:23 09:08:08", datetime(2003, 2, 23, 9, 8, 8)),
        ("2003:02:23 09:08:08.123", datetime(2003, 2, 23, 9, 8, 8)),
        ("2003:02:23 09:08:08-07:00", datetime(2003, 2, 23, 9, 8, 8)),
        ("2003:02:23 09:08:08.45+02:00", datetime(2003, 2, 23, 9, 8, 8)),
        ("2020-12-18 09:12:48", datetime(2020, 12, 18, 9, 12, 48)),
    ],
)
def test_parse_exif_datetime_accepts_real_values(raw, expected):
    assert parse_exif_datetime(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "    ",
        "0000:00:00 00:00:00",
        "0000:00:00",
        "not a date",
        "2003:02:30 09:08:08",
    ],
)
def test_parse_exif_datetime_rejects_junk(raw):
    assert parse_exif_datetime(raw) is None


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("IMG_20140612_123456.jpg", datetime(2014, 6, 12, 12, 34, 56)),
        ("VID_20140612_123456.mp4", datetime(2014, 6, 12, 12, 34, 56)),
        ("PXL_20210103_180520123.jpg", datetime(2021, 1, 3, 18, 5, 20)),
        ("Screenshot_20200101-123456.png", datetime(2020, 1, 1, 12, 34, 56)),
        ("2014-06-12 12.34.56.jpg", datetime(2014, 6, 12, 12, 34, 56)),
        ("20140612_123456.jpg", datetime(2014, 6, 12, 12, 34, 56)),
        ("IMG-20140612-WA0001.jpg", datetime(2014, 6, 12, 0, 0, 0)),
    ],
)
def test_parse_filename_datetime_finds_embedded_dates(filename, expected):
    assert parse_filename_datetime(filename) == expected


@pytest.mark.parametrize(
    "filename",
    [
        "slickrock mike and dan(1).jpg",
        "DSC_0123.jpg",
        "IMG_1234.HEIC",
        "photo.jpg",
        "IMG_20149999_123456.jpg",
    ],
)
def test_parse_filename_datetime_returns_none_without_a_date(filename):
    assert parse_filename_datetime(filename) is None


def test_parse_filename_datetime_ignores_implausible_year():
    assert parse_filename_datetime("IMG_18140612_123456.jpg") is None
