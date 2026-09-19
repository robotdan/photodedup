import pytest

from photodedup.catalog import open_catalog
from photodedup.grouping import run_group


@pytest.fixture
def conn(tmp_path):
    return open_catalog(tmp_path / "catalog.sqlite")


def add(conn, uuid, *, kind="image", has_raw=0, scan=None, pixel=None,
        video=None, dhash=None, status="ok"):
    conn.execute(
        """INSERT INTO asset (uuid, kind, has_raw, survey_status)
           VALUES (?, ?, ?, 'ok')""",
        (uuid, kind, has_raw),
    )
    conn.execute(
        """INSERT INTO asset_hash (uuid, status, scan_hash, pixel_hash, video_hash, dhash)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (uuid, status, scan, pixel, video, dhash),
    )
    conn.commit()


def groups(conn, signal=None):
    sql = "SELECT * FROM dup_group"
    if signal:
        sql += f" WHERE signal = '{signal}'"
    return conn.execute(sql).fetchall()


def test_identical_scan_hashes_form_one_exact_group(conn):
    add(conn, "u1", scan="aaa")
    add(conn, "u2", scan="aaa")
    result = run_group(conn)
    assert result.groups == 1
    assert result.members == 2
    assert result.removable == 1
    row = groups(conn)[0]
    assert row["signal"] == "scan" and row["tier"] == "exact" and row["size"] == 2


def test_singletons_are_not_groups(conn):
    add(conn, "u1", scan="aaa")
    add(conn, "u2", scan="bbb")
    assert run_group(conn).groups == 0


def test_null_hashes_never_group(conn):
    """Two assets that both failed to hash are not therefore duplicates."""
    add(conn, "u1", scan=None, pixel=None)
    add(conn, "u2", scan=None, pixel=None)
    assert run_group(conn).groups == 0


def test_raw_never_groups_with_non_raw(conn):
    add(conn, "u1", pixel="ppp", has_raw=1)
    add(conn, "u2", pixel="ppp", has_raw=0)
    assert run_group(conn).groups == 0


def test_video_never_groups_with_image(conn):
    add(conn, "u1", kind="video", video="vvv")
    add(conn, "u2", kind="image", video="vvv")
    assert run_group(conn).groups == 0


def test_videos_group_on_stream_hash(conn):
    add(conn, "u1", kind="video", video="vvv")
    add(conn, "u2", kind="video", video="vvv")
    result = run_group(conn)
    assert result.groups == 1
    assert groups(conn)[0]["signal"] == "video"


def test_dhash_groups_are_candidate_tier(conn):
    """Perceptual matches are never auto-resolvable, so they must not be exact."""
    add(conn, "u1", dhash="dddd")
    add(conn, "u2", dhash="dddd")
    run_group(conn)
    assert [r["tier"] for r in groups(conn, "dhash")] == ["candidate"]


def test_scan_and_pixel_groups_coexist(conn):
    add(conn, "u1", scan="aaa", pixel="ppp")
    add(conn, "u2", scan="aaa", pixel="ppp")
    run_group(conn)
    assert {r["signal"] for r in groups(conn)} == {"scan", "pixel"}


def test_rerun_is_idempotent(conn):
    add(conn, "u1", scan="aaa")
    add(conn, "u2", scan="aaa")
    first = run_group(conn)
    second = run_group(conn)
    assert (first.groups, first.members) == (second.groups, second.members)
    assert conn.execute("SELECT COUNT(*) AS n FROM dup_group").fetchone()["n"] == 1


def test_errored_hashes_are_excluded(conn):
    add(conn, "u1", scan="aaa", status="error")
    add(conn, "u2", scan="aaa", status="error")
    assert run_group(conn).groups == 0


def test_group_of_three_reports_two_removable(conn):
    for u in ("u1", "u2", "u3"):
        add(conn, u, scan="aaa")
    result = run_group(conn)
    assert result.members == 3
    assert result.removable == 2
