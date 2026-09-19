import pytest
from PIL import Image

from photodedup.hashing import pixel_hashes


def make(path, colour="red", size=(64, 48), fmt=None):
    Image.new("RGB", size, colour).save(path, format=fmt)
    return path


def test_same_image_two_formats_share_a_pixel_hash(tmp_path):
    """PNG and BMP of identical pixels must agree; the container is not the image."""
    a = make(tmp_path / "a.png")
    b = make(tmp_path / "b.bmp")
    assert pixel_hashes(a).pixel_hash == pixel_hashes(b).pixel_hash


def test_different_images_differ(tmp_path):
    a = make(tmp_path / "a.png", colour="red")
    b = make(tmp_path / "b.png", colour="blue")
    assert pixel_hashes(a).pixel_hash != pixel_hashes(b).pixel_hash


def test_dimensions_are_reported(tmp_path):
    a = make(tmp_path / "a.png", size=(123, 45))
    result = pixel_hashes(a)
    assert (result.width, result.height) == (123, 45)


def test_resized_copy_differs_on_pixel_hash_but_matches_perceptually(tmp_path):
    source = Image.new("RGB", (256, 256))
    for x in range(256):
        for y in range(256):
            source.putpixel((x, y), (x, y, (x * y) % 256))
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    source.save(a)
    source.resize((128, 128)).save(b)

    ha, hb = pixel_hashes(a), pixel_hashes(b)
    assert ha.pixel_hash != hb.pixel_hash
    # Perceptual hashes are hex strings of equal length; compare bit distance.
    distance = bin(int(ha.dhash, 16) ^ int(hb.dhash, 16)).count("1")
    assert distance <= 12


def test_undecodable_file_returns_none(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    assert pixel_hashes(bad) is None


def test_missing_file_returns_none(tmp_path):
    assert pixel_hashes(tmp_path / "nope.png") is None


def test_exif_orientation_is_normalised(tmp_path):
    """An orientation flag is metadata; the displayed pixels are the image.

    The image has to be asymmetric and the second copy really rotated, or the
    assertion holds for reasons that have nothing to do with the orientation
    tag. PNG rather than JPEG: JPEG recompression leaves DCT noise that makes
    two byte-different encodings of the same pixels hash differently.
    """
    import shutil
    import subprocess

    if shutil.which("exiftool") is None:
        pytest.skip("exiftool not installed")

    upright = Image.new("RGB", (64, 48), "red")
    upright.paste(Image.new("RGB", (32, 48), "blue"), (32, 0))

    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    upright.save(a)
    # Orientation 6 means "rotate 90 CW to display", so the stored pixels are
    # the upright image turned the other way.
    upright.rotate(-90, expand=True).save(b)

    subprocess.run(
        ["exiftool", "-overwrite_original", "-Orientation=6", "-n", str(a)],
        check=True,
        capture_output=True,
    )

    assert pixel_hashes(b).pixel_hash == pixel_hashes(a).pixel_hash
