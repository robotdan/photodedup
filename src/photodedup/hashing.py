"""Content hashing. Every function here is pure over bytes or a file path."""

from __future__ import annotations

import hashlib
import io
import subprocess
from dataclasses import dataclass
from pathlib import Path

import imagehash
from PIL import Image, ImageFile, ImageOps

try:  # HEIC support; the library holds 6,678 of them.
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - absence is reported by the hash stage
    pass

# Metadata segments. APP0-APP15 carry JFIF, EXIF, ICC and XMP; COM is a comment.
# Excluding them is what makes two files holding the same image with different
# metadata hash identically.
_METADATA_MARKERS = frozenset(range(0xE0, 0xF0)) | {0xFE}

# Markers that stand alone, carrying no length field.
_STANDALONE = frozenset(range(0xD0, 0xD8)) | {0x01}

_SOI = 0xD8
_EOI = 0xD9
_SOS = 0xDA


def jpeg_scan_hash(data: bytes) -> str | None:
    """Hash a JPEG's structure and entropy data, ignoring every metadata segment.

    Returns None when *data* is not a JPEG this can parse, which the caller
    treats as "fall back to decoding" rather than as an error.
    """
    if len(data) < 4 or data[0] != 0xFF or data[1] != _SOI:
        return None

    digest = hashlib.sha256()
    digest.update(data[0:2])
    i = 2
    n = len(data)
    saw_scan = False

    while i < n:
        if data[i] != 0xFF:
            return None
        # Fill bytes: any number of 0xFF may pad the gap before a marker.
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            return None
        marker = data[i]
        i += 1

        if marker == _EOI:
            digest.update(b"\xff\xd9")
            return digest.hexdigest() if saw_scan else None

        if marker in _STANDALONE:
            digest.update(bytes((0xFF, marker)))
            continue

        if i + 2 > n:
            return None
        length = int.from_bytes(data[i : i + 2], "big")
        if length < 2 or i + length > n:
            return None
        segment = data[i : i + length]
        i += length

        if marker not in _METADATA_MARKERS:
            digest.update(bytes((0xFF, marker)))
            digest.update(segment)

        if marker == _SOS:
            # Entropy-coded data runs from here to EOI. It contains 0xFF00
            # stuffing and RST markers, never a bare FFD9, and any later
            # segments before EOI are only tables and SOS headers, so the
            # first FFD9 at or after this point is the real EOI. Searching
            # forward also means bytes a second import appended after the
            # true EOI are never absorbed into the digest.
            end = data.find(b"\xff\xd9", i)
            if end < 0:
                return None
            digest.update(data[i:end])
            digest.update(b"\xff\xd9")
            return digest.hexdigest()

    return None


def jpeg_is_truncated(data: bytes) -> bool:
    """True when *data* is a JPEG whose entropy-coded data has no EOI after it.

    Walks the segment structure so the EOI of an embedded EXIF thumbnail
    (inside APP1, before SOS) is never mistaken for the image's own, and so
    bytes after a real EOI (a motion photo's MP4 trailer, zero padding) are
    never mistaken for a missing one. A segment length that overruns the
    buffer is also truncation: the file was cut inside its headers. Anything
    that is not a parseable JPEG returns False; it may be damaged, but it is
    not *this* kind of damaged.
    """
    if len(data) < 4 or data[0] != 0xFF or data[1] != _SOI:
        return False
    i = 2
    n = len(data)
    while i < n:
        if data[i] != 0xFF:
            return False
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            return True
        marker = data[i]
        i += 1
        if marker == _EOI:
            return False
        if marker in _STANDALONE:
            continue
        if i + 2 > n:
            return True
        length = int.from_bytes(data[i : i + 2], "big")
        if length < 2:
            return False
        if i + length > n:
            return True
        i += length
        if marker == _SOS:
            return data.find(b"\xff\xd9", i) < 0
    return True


@dataclass(frozen=True)
class PixelHashes:
    pixel_hash: str
    dhash: str
    phash: str
    width: int
    height: int


def real_length(data: bytes) -> int:
    """Length of *data* with trailing zero bytes stripped.

    A truncated file that was later zero-filled to its original size (the
    2003 FUJI files and both 2016 files in this library) carries its real
    content in a prefix; this is that prefix's length, and it is what the
    group stage compares against an intact partner.
    """
    return len(data.rstrip(b"\x00"))


def pixel_hashes(
    path: Path, *, lenient: bool = False, data: bytes | None = None
) -> PixelHashes | None:
    """Decode *path* once and derive every pixel-based signal from it.

    Returns None when the file cannot be decoded, which the hash stage records
    as an error rather than letting it abort a 65,974-asset run.

    With *lenient* the decoder tolerates a missing tail (no EOI marker), which
    is how a truncated file still yields perceptual hashes. The flag is a
    Pillow process global; it is set and restored around the decode so a
    strict call in the same process stays strict.

    *data*, when given, is decoded instead of the file at *path*; the truncated
    path uses it to decode only the real bytes of a zero-padded file.
    """
    previous = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = lenient
    try:
        source = io.BytesIO(data) if data is not None else path
        with Image.open(source) as image:
            # An orientation flag is metadata. Compare what is displayed.
            image = ImageOps.exif_transpose(image)
            rgb = image.convert("RGB")
            width, height = rgb.size
            digest = hashlib.sha256()
            digest.update(f"{width}x{height}|".encode())
            digest.update(rgb.tobytes())
            return PixelHashes(
                pixel_hash=digest.hexdigest(),
                dhash=str(imagehash.dhash(rgb, hash_size=8)),
                phash=str(imagehash.phash(rgb, hash_size=8)),
                width=width,
                height=height,
            )
    except Exception:
        return None
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = previous


def video_stream_hash(
    path: Path, *, ffmpeg: str = "ffmpeg", timeout: int = 600
) -> str | None:
    """Hash a video's encoded picture stream, ignoring container metadata.

    `-c copy` avoids re-encoding, so this runs at IO speed rather than CPU
    speed, which matters across 141 GB of video. Mapping only `0:v` means an
    added or re-ordered audio track does not change the result.
    """
    try:
        completed = subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-i", str(path),
             "-map", "0:v", "-c", "copy", "-f", "md5", "-"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    if not output.startswith("MD5="):
        return None
    return output[len("MD5=") :]
