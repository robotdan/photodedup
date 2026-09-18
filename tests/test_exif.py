import os
import shutil
import subprocess

import pytest
from PIL import Image

from photodedup.exif import read_metadata, ExiftoolError

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)


@pytest.fixture
def jpeg_with_date(tmp_path):
    path = tmp_path / "shot.jpg"
    Image.new("RGB", (16, 16), "red").save(path)
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-DateTimeOriginal=2003:02:23 09:08:08",
            "-GPSLatitude=38.5733",
            "-GPSLatitudeRef=N",
            "-GPSLongitude=109.5498",
            "-GPSLongitudeRef=W",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_reads_datetime_original(jpeg_with_date):
    result = read_metadata([jpeg_with_date])
    tags = result[str(jpeg_with_date)]
    assert tags["EXIF:DateTimeOriginal"] == "2003:02:23 09:08:08"


def test_reads_gps_as_signed_decimal(jpeg_with_date):
    tags = read_metadata([jpeg_with_date])[str(jpeg_with_date)]
    assert tags["Composite:GPSLatitude"] == pytest.approx(38.5733, abs=1e-4)
    assert tags["Composite:GPSLongitude"] == pytest.approx(-109.5498, abs=1e-4)


def test_missing_file_is_omitted_not_raised(tmp_path, jpeg_with_date):
    result = read_metadata([jpeg_with_date, tmp_path / "nope.jpg"])
    assert str(jpeg_with_date) in result
    assert str(tmp_path / "nope.jpg") not in result


def test_empty_input_returns_empty_dict():
    assert read_metadata([]) == {}


def test_chunking_covers_every_file(tmp_path):
    paths = []
    for i in range(7):
        path = tmp_path / f"img{i}.jpg"
        Image.new("RGB", (8, 8), "blue").save(path)
        paths.append(path)
    result = read_metadata(paths, chunk_size=2)
    assert len(result) == 7


def test_corrupt_file_in_chunk_with_valid_file(tmp_path, jpeg_with_date):
    corrupt_path = tmp_path / "corrupt.jpg"
    with open(corrupt_path, "wb") as f:
        f.write(os.urandom(600))
    result = read_metadata([corrupt_path, jpeg_with_date])
    assert str(corrupt_path) in result
    assert "File:FileSize" in result[str(corrupt_path)]
    assert "EXIF:DateTimeOriginal" not in result[str(corrupt_path)]
    assert str(jpeg_with_date) in result
    assert result[str(jpeg_with_date)]["EXIF:DateTimeOriginal"] == "2003:02:23 09:08:08"


def test_exiftool_failure_raises_error(tmp_path, jpeg_with_date):
    with pytest.raises(ExiftoolError):
        read_metadata([jpeg_with_date], exiftool="false")


def test_nonexistent_binary_raises_file_not_found_error(jpeg_with_date):
    with pytest.raises(FileNotFoundError):
        read_metadata([jpeg_with_date], exiftool="/nonexistent/path/to/exiftool")
