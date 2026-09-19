import shutil
import subprocess

import pytest
from PIL import Image

from photodedup.hashing import jpeg_scan_hash

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)


def make_jpeg(path, colour="red", size=(64, 48)):
    Image.new("RGB", size, colour).save(path, quality=90)
    return path


def set_tags(path, *tags):
    subprocess.run(
        ["exiftool", "-overwrite_original", *tags, str(path)],
        check=True,
        capture_output=True,
    )


def test_same_pixels_different_exif_hash_identically(tmp_path):
    """The whole point: this is the cohort A / cohort B pair."""
    a = make_jpeg(tmp_path / "a.jpg")
    b = tmp_path / "b.jpg"
    shutil.copy2(a, b)
    set_tags(a, "-DateTimeOriginal=2003:02:23 09:08:08")
    set_tags(b, "-DateTimeOriginal=2020:12:18 09:12:48", "-Artist=Someone")

    assert a.read_bytes() != b.read_bytes()
    assert jpeg_scan_hash(a.read_bytes()) == jpeg_scan_hash(b.read_bytes())


def test_whole_file_hash_would_have_missed_that(tmp_path):
    """Guards the reason this function exists rather than hashlib.sha256(data)."""
    import hashlib

    a = make_jpeg(tmp_path / "a.jpg")
    b = tmp_path / "b.jpg"
    shutil.copy2(a, b)
    set_tags(b, "-Artist=Someone")
    assert hashlib.sha256(a.read_bytes()).hexdigest() != hashlib.sha256(
        b.read_bytes()
    ).hexdigest()
    assert jpeg_scan_hash(a.read_bytes()) == jpeg_scan_hash(b.read_bytes())


def test_different_images_hash_differently(tmp_path):
    a = make_jpeg(tmp_path / "a.jpg", colour="red")
    b = make_jpeg(tmp_path / "b.jpg", colour="blue")
    assert jpeg_scan_hash(a.read_bytes()) != jpeg_scan_hash(b.read_bytes())


def test_re_encoding_changes_the_hash(tmp_path):
    """Re-encoding is a perceptual-hash problem, not a scan-hash one."""
    a = make_jpeg(tmp_path / "a.jpg")
    b = tmp_path / "b.jpg"
    Image.open(a).save(b, quality=60)
    assert jpeg_scan_hash(a.read_bytes()) != jpeg_scan_hash(b.read_bytes())


def test_same_bytes_hash_identically(tmp_path):
    a = make_jpeg(tmp_path / "a.jpg")
    assert jpeg_scan_hash(a.read_bytes()) == jpeg_scan_hash(a.read_bytes())


def test_icc_profile_is_excluded(tmp_path):
    """ICC lives in APP2 and is metadata for this purpose."""
    a = make_jpeg(tmp_path / "a.jpg")
    b = tmp_path / "b.jpg"
    shutil.copy2(a, b)
    set_tags(b, "-ICC_Profile<=/System/Library/ColorSync/Profiles/sRGB Profile.icc")
    assert jpeg_scan_hash(a.read_bytes()) == jpeg_scan_hash(b.read_bytes())


@pytest.mark.parametrize(
    "data",
    [b"", b"not a jpeg at all", b"\xff\xd8", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"],
)
def test_non_jpeg_returns_none(data):
    assert jpeg_scan_hash(data) is None


def test_truncated_jpeg_returns_none(tmp_path):
    a = make_jpeg(tmp_path / "a.jpg")
    assert jpeg_scan_hash(a.read_bytes()[:40]) is None


def test_trailing_bytes_after_eoi_are_ignored(tmp_path):
    """A second import may append bytes after EOI; they must not affect the hash."""
    a = make_jpeg(tmp_path / "a.jpg")
    base = a.read_bytes()
    assert jpeg_scan_hash(base) == jpeg_scan_hash(base + b"junk\xff\xd9more")


def test_double_eoi_matches_single(tmp_path):
    a = make_jpeg(tmp_path / "a.jpg")
    base = a.read_bytes()
    assert jpeg_scan_hash(base) == jpeg_scan_hash(base + b"\xff\xd9")


def test_progressive_copies_with_different_exif_match(tmp_path):
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    baseline = tmp_path / "baseline.jpg"
    Image.new("RGB", (64, 48), "red").save(a, quality=90, progressive=True)
    shutil.copy2(a, b)
    set_tags(b, "-DateTimeOriginal=2020:12:18 09:12:48")
    assert jpeg_scan_hash(a.read_bytes()) == jpeg_scan_hash(b.read_bytes())

    Image.new("RGB", (64, 48), "red").save(baseline, quality=90, progressive=False)
    assert jpeg_scan_hash(a.read_bytes()) != jpeg_scan_hash(baseline.read_bytes())


def test_transposed_dimensions_differ(tmp_path):
    a = make_jpeg(tmp_path / "a.jpg", size=(64, 48))
    b = make_jpeg(tmp_path / "b.jpg", size=(48, 64))
    assert jpeg_scan_hash(a.read_bytes()) != jpeg_scan_hash(b.read_bytes())
