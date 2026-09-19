from datetime import datetime

import pytest

from photodedup.catalog import open_catalog, set_stage
from photodedup.grouping import run_group
from photodedup.report import (
    cohort_pairs,
    coverage,
    duplicate_report,
    exact_components,
    groups_with_size_spread,
    signal_overlap,
)


@pytest.fixture
def conn(tmp_path):
    """A catalog whose hash stage has reported complete, as `run_group` demands."""
    conn = open_catalog(tmp_path / "catalog.sqlite")
    set_stage(conn, "hash", "complete")
    return conn


def add(conn, uuid, *, scan=None, pixel=None, dhash=None, photos_date=None,
        filename="x.jpg", size=1000, status="ok", hashed=True):
    conn.execute(
        """INSERT INTO asset (uuid, kind, uti, has_raw, survey_status, original_filename,
                              photos_date, file_size, width, height)
           VALUES (?, 'image', 'public.jpeg', 0, 'ok', ?, ?, ?, 100, 100)""",
        (uuid, filename, photos_date.isoformat() if photos_date else None, size),
    )
    if hashed:
        conn.execute(
            """INSERT INTO asset_hash (uuid, status, scan_hash, pixel_hash, dhash)
               VALUES (?, ?, ?, ?, ?)""",
            (uuid, status, scan, pixel, dhash),
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


def test_coverage_counts_every_state_of_every_asset(conn):
    add(conn, "ok1", scan="aaa")           # hashed ok, grouped
    add(conn, "ok2", scan="aaa")           # hashed ok, grouped
    add(conn, "lonely", scan="zzz")        # hashed ok, in no group
    add(conn, "bad", status="error")       # hash error
    add(conn, "gone", status="no_original")
    add(conn, "later", hashed=False)       # not yet attempted
    run_group(conn)
    assert coverage(conn) == [
        ("assets surveyed ok", 6),
        ("hashed ok", 3),
        ("hashed leniently (truncated)", 0),
        ("hash errors", 1),
        ("no local original", 1),
        ("not yet attempted", 1),
        ("hashed ok but in no group", 1),
    ]


def test_report_leads_with_coverage_before_any_removable_number(conn):
    add(conn, "u1", scan="aaa")
    add(conn, "u2", scan="aaa")
    run_group(conn)
    text = duplicate_report(conn)
    assert text.index("## Coverage") < text.index("Removable on exact signals")
    assert "not yet attempted" in text
    assert "the numbers below are partial" in text


def test_groups_with_size_spread_finds_a_stripped_motion_photo(conn):
    add(conn, "full", scan="aaa", size=5_000_000)
    add(conn, "stripped", scan="aaa", size=1_200_000)
    run_group(conn)
    assert groups_with_size_spread(conn) == 1
    assert groups_with_size_spread(conn, min_spread=10_000_000) == 0


def test_groups_with_size_spread_ignores_same_sized_members(conn):
    add(conn, "u1", scan="aaa", size=1000)
    add(conn, "u2", scan="aaa", size=1000)
    run_group(conn)
    assert groups_with_size_spread(conn) == 0


def test_sample_groups_render_their_size_spread(conn):
    add(conn, "full", scan="aaa", size=5_000_000)
    add(conn, "stripped", scan="aaa", size=1_200_000)
    run_group(conn)
    assert "size spread: 3800000 bytes" in duplicate_report(conn)


def test_exact_components_all_tiers_includes_candidates(conn):
    add(conn, "u1", scan="aaa")
    add(conn, "u2", scan="aaa")
    add(conn, "u3", dhash="dddd")
    add(conn, "u4", dhash="dddd")
    run_group(conn)
    assert exact_components(conn) == (1, 2, 1)
    assert exact_components(conn, all_tiers=True) == (2, 4, 2)


def test_report_pluralises_a_single_set(conn):
    add(conn, "u1", scan="aaa")
    add(conn, "u2", scan="aaa")
    run_group(conn)
    text = duplicate_report(conn)
    assert "in 1 set)" in text
    assert "in 1 sets" not in text


def test_coverage_counts_truncated_rows(conn):
    add(conn, "u1", scan="aaa")
    add(conn, "u2", status="truncated", dhash="d")
    counts = dict(coverage(conn))
    assert counts["hashed ok"] == 1
    assert counts["hashed leniently (truncated)"] == 1


def test_exact_components_reads_sets(conn):
    add(conn, "a1", scan="sa", dhash="d")
    add(conn, "a2", scan="sa")
    add(conn, "b1", scan="sb", dhash="d")
    add(conn, "b2", scan="sb")
    add(conn, "x", dhash="d")
    run_group(conn)
    assert exact_components(conn) == (2, 4, 2)
    assert exact_components(conn, all_tiers=True) == (1, 5, 4)
