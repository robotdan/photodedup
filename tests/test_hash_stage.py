from datetime import datetime

import pytest
from PIL import Image

from photodedup.catalog import open_catalog
from photodedup.hash_stage import HashStageInterrupted, hash_one, run_hash
from photodedup.hashing import real_length

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


def make_truncated(path):
    intact = path.with_name("intact_" + path.name)
    Image.effect_noise((96, 64), 64).convert("RGB").save(intact, quality=95)
    data = intact.read_bytes()
    path.write_bytes(data[: data.rfind(b"\xff\xd9") - 200] + b"\x00" * 300)
    return path


def test_hash_one_reports_truncated_with_perceptual_hashes(tmp_path):
    path = make_truncated(tmp_path / "t.jpg")
    row = hash_one("u1", str(path), "image", "public.jpeg")
    assert row["status"] == "truncated"
    assert row.get("scan_hash") is None
    assert row["dhash"] and row["phash"] and row["pixel_hash"]
    assert (row["pixel_width"], row["pixel_height"]) == (96, 64)
    assert row["bytes_read"] == real_length(path.read_bytes())


def test_hash_one_still_errors_on_a_non_image(tmp_path):
    path = tmp_path / "fake.png"
    path.write_bytes(b'{"normalImage": "iVBOR..."}')
    row = hash_one("u1", str(path), "image", "public.png")
    assert row["status"] == "error"
    assert row["error"] == "no usable hash"


def test_run_hash_counts_truncated_and_does_not_rehash_it(conn, tmp_path):
    add_asset(conn, "u1", path=make_truncated(tmp_path / "t.jpg"))
    first = run_hash(conn, workers=1)
    assert first.truncated == 1
    assert first.errors == 0
    second = run_hash(conn, workers=1)
    assert second.total == 0
    assert second.already_done == 1


def test_truncated_unpadded_jpeg_is_reported_truncated(tmp_path):
    """Shape (a): truncated without zero-fill; fail strict, recover with lenient."""
    intact = tmp_path / "intact.jpg"
    Image.effect_noise((96, 64), 64).convert("RGB").save(intact, quality=95)
    data = intact.read_bytes()

    truncated_path = tmp_path / "t.jpg"
    truncated_path.write_bytes(data[: data.rfind(b"\xff\xd9") - 200])

    row = hash_one("u1", str(truncated_path), "image", "public.jpeg")
    assert row["status"] == "truncated"
    assert row.get("scan_hash") is None
    assert row["dhash"] and row["phash"] and row["pixel_hash"]
    assert row["bytes_read"] == len(truncated_path.read_bytes())


def test_intact_jpeg_with_trailing_bytes_stays_ok(tmp_path):
    """Motion photos and zero-padded intact files are still ok, not truncated."""
    intact = tmp_path / "intact.jpg"
    Image.effect_noise((96, 64), 64).convert("RGB").save(intact, quality=95)
    data = intact.read_bytes()

    # Intact with zero padding
    padded_zeros = tmp_path / "padded_zeros.jpg"
    padded_zeros.write_bytes(data + b"\x00" * 300)

    # Intact with motion photo MP4 trailer
    padded_mp4 = tmp_path / "motion_photo.jpg"
    padded_mp4.write_bytes(data + b"ftypmp4 junk" * 50)

    for path in [padded_zeros, padded_mp4]:
        row = hash_one("u1", str(path), "image", "public.jpeg")
        assert row["status"] == "ok", f"Failed for {path.name}"
        assert row.get("scan_hash") is not None, f"No scan hash for {path.name}"
        assert row["dhash"] and row["phash"] and row["pixel_hash"]


def test_render_columns_are_written(conn, tmp_path):
    black = tmp_path / "black.png"
    Image.new("RGB", (16, 16), "black").save(black)
    add_asset(conn, "u1", path=black, uti="public.png")
    deep = tmp_path / "deep.jpg"
    Image.effect_noise((96, 64), 64).convert("RGB").save(deep, quality=95)
    data = deep.read_bytes()
    deep.write_bytes(data[: data.rfind(b"\xff\xd9") - 2000])
    add_asset(conn, "u2", path=deep)
    run_hash(conn, workers=1)
    b = conn.execute("SELECT blank_rows, flat FROM asset_hash WHERE uuid='u1'").fetchone()
    assert (b["blank_rows"], b["flat"]) == (16, 1)
    t = conn.execute("SELECT blank_rows, flat, status FROM asset_hash WHERE uuid='u2'").fetchone()
    assert t["status"] == "truncated" and t["blank_rows"] > 0 and t["flat"] == 0
