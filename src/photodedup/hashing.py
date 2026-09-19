"""Content hashing. Every function here is pure over bytes or a file path."""

from __future__ import annotations

import hashlib

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
            # stuffing and RST markers, so the only safe terminator is the
            # final EOI; hash everything up to it.
            end = data.rfind(b"\xff\xd9")
            if end < i:
                return None
            digest.update(data[i:end])
            digest.update(b"\xff\xd9")
            return digest.hexdigest()

    return None
