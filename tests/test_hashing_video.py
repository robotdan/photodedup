import shutil
import subprocess

import pytest

from photodedup.hashing import video_stream_hash

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def make_video(path, *, seconds=1, colour="red", metadata=None):
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c={colour}:s=64x48:d={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
    ]
    for key, value in (metadata or {}).items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def test_same_stream_different_container_metadata_hashes_identically(tmp_path):
    """The video equivalent of the JPEG EXIF-rewrite case."""
    a = make_video(tmp_path / "a.mp4")
    b = tmp_path / "b.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(a),
         "-c", "copy", "-metadata", "title=Something", str(b)],
        check=True, capture_output=True,
    )
    assert a.read_bytes() != b.read_bytes()
    assert video_stream_hash(a) == video_stream_hash(b)


def test_different_videos_differ(tmp_path):
    a = make_video(tmp_path / "a.mp4", colour="red")
    b = make_video(tmp_path / "b.mp4", colour="blue")
    assert video_stream_hash(a) != video_stream_hash(b)


def test_non_video_returns_none(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00" * 500)
    assert video_stream_hash(bad) is None


def test_missing_file_returns_none(tmp_path):
    assert video_stream_hash(tmp_path / "nope.mp4") is None


def test_bad_ffmpeg_binary_returns_none(tmp_path):
    a = make_video(tmp_path / "a.mp4")
    assert video_stream_hash(a, ffmpeg="/usr/bin/false") is None
