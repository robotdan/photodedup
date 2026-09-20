import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from photodedup.archive import ArchiveSpace, dest_for, pending_losers, run_archive
from photodedup.arbitrate import run_arbitrate
from photodedup.catalog import StageIncomplete, get_stage, open_catalog, set_stage
from photodedup.library import AssetPaths

NOW = datetime(2026, 9, 20, 12, 0, 0)


@pytest.fixture
def conn(tmp_path):
    conn = open_catalog(tmp_path / "catalog.sqlite")
    set_stage(conn, "group", "complete")
    return conn


def add_asset(conn, uuid, tmp_path, *, size=10, photos_date="2014-05-12T17:00:00",
              albums=(), favorite=0, name=None):
    path = tmp_path / "lib" / f"{uuid}.jpg"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(bytes([uuid.encode()[0]]) * size)
    conn.execute(
        """INSERT INTO asset (uuid, kind, uti, file_size, path, date_added, photos_date,
                              is_favorite, albums, keywords, survey_status, original_filename)
           VALUES (?, 'image', 'public.jpeg', ?, ?, '2020-01-01T00:00:00', ?, ?, ?, '[]', 'ok', ?)""",
        (uuid, size, str(path), photos_date, favorite, json.dumps(list(albums)), name or f"{uuid}.jpg"),
    )
    conn.execute("INSERT INTO asset_hash (uuid, status) VALUES (?, 'ok')", (uuid,))
    conn.execute(
        "INSERT INTO date_candidate (uuid, source, value) VALUES (?, 'photos_db', ?)", (uuid, photos_date)
    )
    conn.commit()
    return path


def add_set(conn, set_id, uuids, *, tier="exact"):
    conn.execute("INSERT INTO dup_set (id, tier, kind, size) VALUES (?, ?, 'image', ?)", (set_id, tier, len(uuids)))
    conn.executemany(
        "INSERT INTO dup_set_member (set_id, uuid, tier, signals) VALUES (?, ?, ?, 'scan')",
        [(set_id, u, tier) for u in uuids],
    )
    conn.commit()


@pytest.fixture
def decided(conn, tmp_path):
    """Set 1: keeper 'a' (bigger), loser 'b'. Set 2: candidate pair, never archived."""
    add_asset(conn, "a", tmp_path, size=20)
    add_asset(conn, "b", tmp_path, size=10, albums=("Trips",), photos_date="2003-02-23T09:08:08")
    add_set(conn, 1, ["a", "b"])
    add_asset(conn, "c", tmp_path)
    add_asset(conn, "d", tmp_path)
    add_set(conn, 2, ["c", "d"], tier="candidate")
    run_arbitrate(conn, now=NOW)
    return conn


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_pending_losers_are_resolvable_exact_losers_only(decided):
    assert [r["uuid"] for r in pending_losers(decided)] == ["b"]


def test_dest_uses_photos_date_and_original_filename():
    row = {"uuid": "b", "photos_date": "2003-02-23T09:08:08", "original_filename": "Picture 046.jpg"}
    assert dest_for(row, "original", "/lib/x.jpeg") == "2003/02/b_Picture 046.jpg"
    assert dest_for(row, "edited", "/lib/x_edited.heic") == "2003/02/b_edited.heic"
    assert dest_for(row, "live", "/lib/x.mov") == "2003/02/b_live.mov"
    assert dest_for({"uuid": "z", "photos_date": None, "original_filename": None}, "original", "/lib/z.jpg") == "undated/z_z.jpg"


def test_archive_copies_verifies_and_writes_sidecar(decided, tmp_path):
    root = tmp_path / "archive"
    result = run_archive(decided, root, free_bytes=lambda p: 10**9)
    assert (result.copied, result.skipped, result.mismatched) == (1, 0, 0)
    dest = root / "2003" / "02" / "b_b.jpg"
    assert dest.read_bytes() == b"b" * 10
    row = decided.execute("SELECT * FROM archive_file WHERE uuid='b'").fetchone()
    assert row["kind"] == "original" and row["dest"] == "2003/02/b_b.jpg"
    assert row["sha256"] == sha(dest) and row["bytes"] == 10
    side = json.loads((root / "2003" / "02" / "b_b.jpg.json").read_text())
    assert side["role"] == "loser" and side["keeper_uuid"] == "a" and side["set_id"] == 1
    assert side["albums"] == ["Trips"] and side["sha256"] == row["sha256"]
    manifest = list(csv.DictReader((root / "manifest.csv").open()))
    assert manifest[0]["uuid"] == "b" and manifest[0]["kind"] == "original"


def test_archive_is_resumable(decided, tmp_path):
    root = tmp_path / "archive"
    run_archive(decided, root, free_bytes=lambda p: 10**9)
    second = run_archive(decided, root, free_bytes=lambda p: 10**9)
    assert (second.copied, second.skipped) == (0, 1)


def test_archive_copies_edited_and_live_files_when_present(decided, tmp_path):
    edited = tmp_path / "lib" / "b_edited.jpg"; edited.write_bytes(b"E" * 5)
    live = tmp_path / "lib" / "b.mov"; live.write_bytes(b"L" * 7)
    paths = {"b": AssetPaths("b", str(tmp_path / "lib" / "b.jpg"), str(edited), str(live))}
    root = tmp_path / "archive"
    result = run_archive(decided, root, paths=paths, free_bytes=lambda p: 10**9)
    assert result.copied == 3
    kinds = {r["kind"] for r in decided.execute("SELECT kind FROM archive_file WHERE uuid='b'")}
    assert kinds == {"original", "edited", "live"}
    assert (root / "2003" / "02" / "b_live.mov").read_bytes() == b"L" * 7


def test_mismatch_is_not_recorded_and_is_retried(decided, tmp_path, monkeypatch):
    import photodedup.archive as arc

    root = tmp_path / "archive"
    real = arc._sha256_file
    calls = {"n": 0}

    def corrupt_once(path):
        calls["n"] += 1
        return "bogus" if calls["n"] == 1 else real(path)

    monkeypatch.setattr(arc, "_sha256_file", corrupt_once)
    first = run_archive(decided, root, free_bytes=lambda p: 10**9)
    assert first.mismatched == 1 and first.copied == 0
    assert decided.execute("SELECT COUNT(*) AS n FROM archive_file").fetchone()["n"] == 0
    assert not (root / "2003" / "02" / "b_b.jpg").exists()
    second = run_archive(decided, root, free_bytes=lambda p: 10**9)
    assert second.copied == 1


def test_refuses_without_enough_space(decided, tmp_path):
    with pytest.raises(ArchiveSpace) as exc:
        run_archive(decided, tmp_path / "archive", free_bytes=lambda p: 5)
    assert "10" in str(exc.value)


def test_refuses_before_arbitrate(conn, tmp_path):
    with pytest.raises(StageIncomplete):
        run_archive(conn, tmp_path / "archive", free_bytes=lambda p: 10**9)


def test_missing_source_is_reported_not_fatal(decided, tmp_path):
    Path(decided.execute("SELECT path FROM asset WHERE uuid='b'").fetchone()["path"]).unlink()
    result = run_archive(decided, tmp_path / "archive", free_bytes=lambda p: 10**9)
    assert result.copied == 0 and result.mismatched == 0 and result.missing == 1


def test_limit_caps_the_run(decided, tmp_path):
    result = run_archive(decided, tmp_path / "archive", limit=0, free_bytes=lambda p: 10**9)
    assert result.copied == 0


def test_limit_zero_reports_nothing_skipped(decided, tmp_path):
    result = run_archive(decided, tmp_path / "archive", limit=0, free_bytes=lambda p: 10**9)
    assert (result.copied, result.skipped) == (0, 0)


def test_edited_file_missing_on_first_run_is_archived_on_the_next(decided, tmp_path):
    edited = tmp_path / "lib" / "b_edited.jpg"
    paths = {"b": AssetPaths("b", str(tmp_path / "lib" / "b.jpg"), str(edited), None)}
    root = tmp_path / "archive"

    first = run_archive(decided, root, paths=paths, free_bytes=lambda p: 10**9)
    assert first.copied == 1 and first.missing == 1
    assert get_stage(decided, "archive")["status"] != "complete"

    edited.write_bytes(b"E" * 5)
    second = run_archive(decided, root, paths=paths, free_bytes=lambda p: 10**9)
    assert second.copied == 1 and second.missing == 0
    assert get_stage(decided, "archive")["status"] == "complete"
    kinds = {r["kind"] for r in decided.execute("SELECT kind FROM archive_file WHERE uuid='b'")}
    assert kinds == {"original", "edited"}


def test_dest_sanitises_filename():
    row = {"uuid": "z", "photos_date": None, "original_filename": "../evil/../x.jpg"}
    result = dest_for(row, "original", "/lib/z.jpg")
    assert ".." not in result.split("/")
    assert "/" not in result.rsplit("/", 1)[-1]

    hidden = {"uuid": "z", "photos_date": None, "original_filename": ".hidden.jpg"}
    assert dest_for(hidden, "original", "/lib/z.jpg") == "undated/z_hidden.jpg"


def test_refuses_when_the_volume_is_not_mounted(decided, tmp_path, monkeypatch):
    fake_volume = Path("/Volumes/Nope")
    real_exists = Path.exists

    def fake_exists(self):
        return True if self == fake_volume else real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr("os.path.ismount", lambda p: False)

    with pytest.raises(ArchiveSpace):
        run_archive(decided, fake_volume / "x", free_bytes=lambda p: 10**9)


def test_limited_run_leaves_the_stage_running(conn, tmp_path):
    add_asset(conn, "a", tmp_path, size=20)
    add_asset(conn, "b", tmp_path, size=10)
    add_set(conn, 1, ["a", "b"])
    add_asset(conn, "e", tmp_path, size=20)
    add_asset(conn, "f", tmp_path, size=10)
    add_set(conn, 2, ["e", "f"])
    run_arbitrate(conn, now=NOW)

    root = tmp_path / "archive"
    first = run_archive(conn, root, limit=1, free_bytes=lambda p: 10**9)
    assert first.copied == 1
    stage = get_stage(conn, "archive")
    assert stage["status"] == "running"
    assert "remaining" in stage["notes"] and "1" in stage["notes"]

    second = run_archive(conn, root, free_bytes=lambda p: 10**9)
    assert second.copied == 1
    assert get_stage(conn, "archive")["status"] == "complete"
