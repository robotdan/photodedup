"""Pure date extraction and arbitration. No I/O lives in this module."""

from __future__ import annotations

import re
from datetime import datetime

# Years outside this range are never a real capture date for this library.
MIN_PLAUSIBLE_YEAR = 1900
MAX_PLAUSIBLE_YEAR = 2100

_EXIF_RE = re.compile(
    r"^\s*(?P<y>\d{4})[:\-](?P<mo>\d{2})[:\-](?P<d>\d{2})"
    r"[ T](?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})"
    r"(?:\.\d+)?"
    r"(?:\s*(?:Z|[+\-]\d{2}:?\d{2}))?\s*$"
)

FILENAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    # IMG_20140612_123456, VID_20140612_123456, PXL_20210103_180520123
    re.compile(r"(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})[_\-](?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})"),
    # Screenshot_20200101-123456
    re.compile(r"(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})-(?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})"),
    # 2014-06-12 12.34.56
    re.compile(
        r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[ _](?P<h>\d{2})\.(?P<mi>\d{2})\.(?P<s>\d{2})"
    ),
    # IMG-20140612-WA0001 — date only, no time
    re.compile(r"(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})"),
)


def _build(parts: dict[str, str]) -> datetime | None:
    try:
        value = datetime(
            int(parts["y"]),
            int(parts["mo"]),
            int(parts["d"]),
            int(parts.get("h") or 0),
            int(parts.get("mi") or 0),
            int(parts.get("s") or 0),
        )
    except ValueError:
        return None
    if not MIN_PLAUSIBLE_YEAR <= value.year <= MAX_PLAUSIBLE_YEAR:
        return None
    return value


def parse_exif_datetime(raw: str | None) -> datetime | None:
    """Parse an EXIF or QuickTime datetime string into naive local time."""
    if not raw or not raw.strip():
        return None
    match = _EXIF_RE.match(raw)
    if match is None:
        return None
    return _build(match.groupdict())


def parse_filename_datetime(filename: str) -> datetime | None:
    """Extract a capture datetime embedded in *filename*, if one is present."""
    for pattern in FILENAME_PATTERNS:
        match = pattern.search(filename)
        if match is None:
            continue
        value = _build(match.groupdict())
        if value is not None:
            return value
    return None
