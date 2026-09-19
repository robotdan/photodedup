import pytest

from photodedup.catalog import open_catalog, set_stage
from photodedup.grouping import run_group
from photodedup.sets import SetsInconsistent, run_sets


@pytest.fixture
def conn(tmp_path):
    conn = open_catalog(tmp_path / "catalog.sqlite")
    set_stage(conn, "hash", "complete")
    return conn


def add(conn, uuid, *, kind="image", scan=None, pixel=None, dhash=None, status="ok"):
    conn.execute(
        "INSERT INTO asset (uuid, kind, has_raw, uti, survey_status) VALUES (?, ?, 0, 'public.jpeg', 'ok')",
        (uuid, kind),
    )
    conn.execute(
        "INSERT INTO asset_hash (uuid, status, scan_hash, pixel_hash, dhash) VALUES (?, ?, ?, ?, ?)",
        (uuid, status, scan, pixel, dhash),
    )
    conn.commit()


def sets(conn, tier=None):
    sql = "SELECT * FROM dup_set"
    if tier:
        sql += f" WHERE tier = '{tier}'"
    return conn.execute(sql + " ORDER BY id").fetchall()


def members(conn, set_id):
    return {
        r["uuid"]: r["signals"]
        for r in conn.execute("SELECT uuid, signals FROM dup_set_member WHERE set_id = ?", (set_id,))
    }


def test_pair_found_by_two_signals_is_one_exact_set(conn):
    add(conn, "u1", scan="s", pixel="p")
    add(conn, "u2", scan="s", pixel="p")
    run_group(conn)
    result = run_sets(conn)
    assert (result.exact_sets, result.exact_assets) == (1, 2)
    [s] = sets(conn)
    assert s["tier"] == "exact" and s["kind"] == "image" and s["size"] == 2
    assert members(conn, s["id"]) == {"u1": "pixel,scan", "u2": "pixel,scan"}


def test_chained_groups_merge_into_one_set(conn):
    add(conn, "u1", scan="s")
    add(conn, "u2", scan="s", pixel="p")
    add(conn, "u3", pixel="p")
    run_group(conn)
    result = run_sets(conn)
    assert result.exact_sets == 1 and result.exact_assets == 3
    assert members(conn, sets(conn)[0]["id"]) == {"u1": "scan", "u2": "pixel,scan", "u3": "pixel"}


def test_dhash_only_pair_is_a_candidate_set(conn):
    add(conn, "u1", dhash="d")
    add(conn, "u2", dhash="d")
    run_group(conn)
    result = run_sets(conn)
    assert result.exact_sets == 0
    assert (result.candidate_sets, result.candidate_assets) == (1, 2)
    [s] = sets(conn)
    assert s["tier"] == "candidate"
    assert members(conn, s["id"]) == {"u1": "dhash", "u2": "dhash"}


def test_dhash_linking_two_exact_sets_keeps_them_separate_and_adds_a_candidate(conn):
    add(conn, "a1", scan="sa", dhash="d")
    add(conn, "a2", scan="sa")
    add(conn, "b1", scan="sb", dhash="d")
    add(conn, "b2", scan="sb")
    run_group(conn)
    result = run_sets(conn)
    assert result.exact_sets == 2 and result.exact_assets == 4
    assert result.candidate_sets == 1 and result.candidate_assets == 4
    [cand] = sets(conn, "candidate")
    assert members(conn, cand["id"]) == {
        "a1": "dhash", "a2": "via_exact", "b1": "dhash", "b2": "via_exact",
    }


def test_dhash_entirely_inside_one_exact_set_adds_nothing(conn):
    add(conn, "u1", scan="s", dhash="d")
    add(conn, "u2", scan="s", dhash="d")
    run_group(conn)
    result = run_sets(conn)
    assert result.exact_sets == 1
    assert result.candidate_sets == 0


def test_standalone_asset_joins_a_candidate_set_through_an_exact_member(conn):
    add(conn, "a1", scan="sa", dhash="d")
    add(conn, "a2", scan="sa")
    add(conn, "x", dhash="d")
    run_group(conn)
    result = run_sets(conn)
    assert result.exact_sets == 1
    assert result.candidate_sets == 1 and result.candidate_assets == 3


def test_rerun_is_idempotent(conn):
    add(conn, "u1", scan="s")
    add(conn, "u2", scan="s")
    run_group(conn)
    run_sets(conn)
    run_sets(conn)
    assert len(sets(conn)) == 1


def test_run_group_runs_sets(conn):
    add(conn, "u1", scan="s")
    add(conn, "u2", scan="s")
    result = run_group(conn)
    assert result.exact_sets == 1
    assert len(sets(conn)) == 1


def test_mixed_kinds_in_one_group_trip_the_wire(conn):
    """Grouping keys on kind so this cannot happen through run_group. If it
    ever does, sets must refuse rather than write a set that mixes a video
    with a still."""
    add(conn, "u1", kind="image")
    add(conn, "u2", kind="video")
    conn.execute("INSERT INTO dup_group (id, signal, tier, digest, size) VALUES (1, 'scan', 'exact', 'x', 2)")
    conn.execute("INSERT INTO dup_member VALUES (1, 'u1')")
    conn.execute("INSERT INTO dup_member VALUES (1, 'u2')")
    conn.commit()
    with pytest.raises(SetsInconsistent):
        run_sets(conn)
    assert sets(conn) == []
