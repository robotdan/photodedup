from datetime import datetime

import pytest

from photodedup.catalog import open_catalog
from photodedup.grouping import run_group
from photodedup.report import cohort_pairs, duplicate_report, exact_components, signal_overlap


@pytest.fixture
def conn(tmp_path):
    return open_catalog(tmp_path / "catalog.sqlite")


def add(conn, uuid, *, scan=None, pixel=None, dhash=None, photos_date=None,
        filename="x.jpg", size=1000):
    conn.execute(
        """INSERT INTO asset (uuid, kind, has_raw, survey_status, original_filename,
                              photos_date, file_size, width, height)
           VALUES (?, 'image', 0, 'ok', ?, ?, ?, 100, 100)""",
        (uuid, filename, photos_date.isoformat() if photos_date else None, size),
    )
    conn.execute(
        """INSERT INTO asset_hash (uuid, status, scan_hash, pixel_hash, dhash)
           VALUES (?, 'ok', ?, ?, ?)""",
        (uuid, scan, pixel, dhash),
    )
    conn.commit()


def test_report_renders_on_an_empty_catalog(conn):
    assert "# Duplicate Report" in duplicate_report(conn)


def test_report_counts_exact_groups(conn):
    add(conn, "u1", scan="aaa")
    add(conn, "u2", scan="aaa")
    run_group(conn)
    text = duplicate_report(conn)
    assert "# Duplicate Report" in text
    assert "scan" in text


def test_cohort_pairs_counts_date_disagreement_within_a_group(conn):
    add(conn, "u1", scan="aaa", photos_date=datetime(2003, 2, 23, 9, 8, 8))
    add(conn, "u2", scan="aaa", photos_date=datetime(2020, 12, 18, 9, 12, 48))
    run_group(conn)
    assert cohort_pairs(conn) == 1


def test_cohort_pairs_ignores_agreeing_groups(conn):
    when = datetime(2003, 2, 23, 9, 8, 8)
    add(conn, "u1", scan="aaa", photos_date=when)
    add(conn, "u2", scan="aaa", photos_date=when)
    run_group(conn)
    assert cohort_pairs(conn) == 0


def test_signal_overlap_reports_candidate_only_assets(conn):
    """An asset found only by perceptual hash is the review cost."""
    add(conn, "u1", scan="aaa", dhash="dddd")
    add(conn, "u2", scan="aaa", dhash="dddd")
    add(conn, "u3", dhash="dddd")
    run_group(conn)
    overlap = dict(signal_overlap(conn))
    assert overlap["dhash_only"] == 1


def test_exact_components_merges_a_pair_found_by_two_signals(conn):
    add(conn, "u1", scan="aaa", pixel="bbb")
    add(conn, "u2", scan="aaa", pixel="bbb")
    run_group(conn)
    totals = {}
    for row in conn.execute(
        "SELECT signal, SUM(size) - COUNT(*) AS removable FROM dup_group "
        "WHERE tier = 'exact' GROUP BY signal"
    ):
        totals[row["signal"]] = row["removable"]
    assert sum(totals.values()) == 2
    assert exact_components(conn) == (1, 2, 1)


def test_exact_components_counts_disjoint_pairs_separately(conn):
    add(conn, "u1", scan="aaa")
    add(conn, "u2", scan="aaa")
    add(conn, "u3", scan="ccc")
    add(conn, "u4", scan="ccc")
    run_group(conn)
    assert exact_components(conn) == (2, 4, 2)
