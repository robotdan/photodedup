from datetime import datetime

import pytest
from PIL import Image

from photodedup.catalog import open_catalog
from photodedup.hash_stage import HashStageInterrupted, run_hash

ADDED = datetime(2020, 12, 18, 9, 12, 48)


@pytest.fixture
def conn(tmp_path):
    return open_catalog(tmp_path / "catalog.sqlite")


def add_asset(conn, uuid, *, path=None, kind="image", uti="public.jpeg", status="ok"):
    conn.execute(
        """
        INSERT INTO asset (uuid, kind, uti, path, survey_status, date_added)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (uuid, kind, uti, str(path) if path else None, status, ADDED.isoformat()),
    )
    conn.commit()


def make_jpeg(path, colour="red"):
    Image.new("RGB", (32, 24), colour).save(path, quality=90)
    return path


def test_hashes_an_image(conn, tmp_path):
    add_asset(conn, "u1", path=make_jpeg(tmp_path / "a.jpg"))
    result = run_hash(conn, workers=1)
    assert result.hashed == 1
    row = conn.execute("SELECT * FROM asset_hash WHERE uuid='u1'").fetchone()
    assert row["status"] == "ok"
    assert row["scan_hash"] and row["pixel_hash"] and row["dhash"] and row["phash"]
    assert (row["pixel_width"], row["pixel_height"]) == (32, 24)


def test_asset_without_original_is_recorded_not_failed(conn):
    add_asset(conn, "u1", path=None)
    result = run_hash(conn, workers=1)
    assert result.no_original == 1
    assert result.errors == 0
    row = conn.execute("SELECT * FROM asset_hash WHERE uuid='u1'").fetchone()
    assert row["status"] == "no_original"


def test_survey_errors_are_not_hashed(conn, tmp_path):
    add_asset(conn, "u1", path=make_jpeg(tmp_path / "a.jpg"), status="error")
    result = run_hash(conn, workers=1)
    assert result.total == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM asset_hash").fetchone()["n"] == 0


def test_resume_skips_completed_rows(conn, tmp_path):
    add_asset(conn, "u1", path=make_jpeg(tmp_path / "a.jpg"))
    run_hash(conn, workers=1)
    result = run_hash(conn, workers=1)
    assert result.hashed == 0
    assert result.already_done == 1


def test_corrupt_file_is_an_error_and_does_not_abort(conn, tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"\x00" * 400)
    add_asset(conn, "u1", path=bad)
    add_asset(conn, "u2", path=make_jpeg(tmp_path / "good.jpg"))
    result = run_hash(conn, workers=1)
    assert result.errors == 1
    assert result.hashed == 1
    assert conn.execute(
        "SELECT status FROM asset_hash WHERE uuid='u1'"
    ).fetchone()["status"] == "error"


def test_errors_are_retried_on_a_later_run(conn, tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"\x00" * 400)
    add_asset(conn, "u1", path=bad)
    run_hash(conn, workers=1)
    make_jpeg(bad)  # the file is repaired
    result = run_hash(conn, workers=1)
    assert result.hashed == 1
    assert conn.execute(
        "SELECT status FROM asset_hash WHERE uuid='u1'"
    ).fetchone()["status"] == "ok"


def test_identical_images_with_different_exif_share_a_scan_hash(conn, tmp_path):
    import shutil
    import subprocess

    if shutil.which("exiftool") is None:
        pytest.skip("exiftool not installed")

    a = make_jpeg(tmp_path / "a.jpg")
    b = tmp_path / "b.jpg"
    shutil.copy2(a, b)
    subprocess.run(
        ["exiftool", "-overwrite_original", "-DateTimeOriginal=2020:12:18 09:12:48", str(b)],
        check=True, capture_output=True,
    )
    add_asset(conn, "u1", path=a)
    add_asset(conn, "u2", path=b)
    run_hash(conn, workers=1)
    hashes = [
        r["scan_hash"]
        for r in conn.execute("SELECT scan_hash FROM asset_hash ORDER BY uuid")
    ]
    assert hashes[0] == hashes[1]


def test_limit_caps_the_work(conn, tmp_path):
    for i in range(5):
        add_asset(conn, f"u{i}", path=make_jpeg(tmp_path / f"{i}.jpg"))
    result = run_hash(conn, workers=1, limit=2)
    assert result.hashed == 2


def test_skipped_rows_are_retried(conn, tmp_path):
    """'skipped' is a legal status no writer sets; it must not be a black hole."""
    add_asset(conn, "u1", path=make_jpeg(tmp_path / "a.jpg"))
    conn.execute("INSERT INTO asset_hash (uuid, status) VALUES ('u1', 'skipped')")
    conn.commit()
    result = run_hash(conn, workers=1)
    assert result.hashed == 1
    assert conn.execute(
        "SELECT status FROM asset_hash WHERE uuid='u1'"
    ).fetchone()["status"] == "ok"


def test_scan_hash_is_only_computed_for_jpeg_utis(conn, tmp_path):
    """A JPEG filed under the wrong uti is a mislabelled file, not a match."""
    add_asset(conn, "u1", path=make_jpeg(tmp_path / "a.jpg"), uti="public.png")
    run_hash(conn, workers=1)
    row = conn.execute("SELECT * FROM asset_hash WHERE uuid='u1'").fetchone()
    assert row["status"] == "ok"
    assert row["scan_hash"] is None
    assert row["pixel_hash"]


def test_a_dead_worker_pool_is_resumable(conn, tmp_path, monkeypatch):
    from concurrent.futures.process import BrokenProcessPool

    from photodedup import hash_stage

    class DeadPool:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def submit(self, *args, **kwargs):
            raise BrokenProcessPool("a worker died")

    monkeypatch.setattr(hash_stage, "ProcessPoolExecutor", DeadPool)
    add_asset(conn, "u1", path=make_jpeg(tmp_path / "a.jpg"))

    with pytest.raises(HashStageInterrupted) as exc:
        run_hash(conn, workers=2)
    assert "resumable" in str(exc.value)

    stage = conn.execute("SELECT * FROM stage WHERE name='hash'").fetchone()
    assert stage["status"] == "failed"

    # The asset was never hashed, so a normal re-run still picks it up.
    assert run_hash(conn, workers=1).hashed == 1
