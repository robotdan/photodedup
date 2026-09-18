"""Batched exiftool reads. One subprocess per chunk of files, not per file."""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

EXIF_TAGS: tuple[str, ...] = (
    "EXIF:DateTimeOriginal",
    "EXIF:CreateDate",
    "EXIF:OffsetTimeOriginal",
    "EXIF:SubSecTimeOriginal",
    "QuickTime:CreateDate",
    "QuickTime:CreationDate",
    "XMP:DateCreated",
    "File:FileModifyDate",
    "File:MIMEType",
    "File:FileSize",
    "Composite:GPSLatitude",
    "Composite:GPSLongitude",
    "EXIF:ImageWidth",
    "EXIF:ImageHeight",
)


def _run_chunk(chunk: Sequence[Path], exiftool: str) -> list[dict[str, object]]:
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False) as handle:
        for path in chunk:
            handle.write(f"{path}\n")
        argfile = handle.name
    try:
        completed = subprocess.run(
            [
                exiftool,
                "-json",
                "-G",
                "-n",             # numeric values, so GPS comes back as signed decimal
                "-charset",
                "filename=utf8",
                "-m",             # ignore minor errors
                *(f"-{tag}" for tag in EXIF_TAGS),
                "-@",
                argfile,
            ],
            capture_output=True,
            text=True,
        )
    finally:
        Path(argfile).unlink(missing_ok=True)

    if not completed.stdout.strip():
        return []
    return json.loads(completed.stdout)


def read_metadata(
    paths: Sequence[Path],
    *,
    chunk_size: int = 400,
    exiftool: str = "exiftool",
) -> dict[str, dict[str, object]]:
    """Read EXIF_TAGS for every readable path. Unreadable paths are omitted."""
    result: dict[str, dict[str, object]] = {}
    for start in range(0, len(paths), chunk_size):
        chunk = paths[start : start + chunk_size]
        for record in _run_chunk(chunk, exiftool):
            source = record.get("SourceFile")
            if source is None:
                continue
            result[str(Path(source))] = record
    return result
