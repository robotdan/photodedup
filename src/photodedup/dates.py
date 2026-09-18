"""Pure date extraction and arbitration. No I/O lives in this module."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

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


SOURCE_RANK: dict[str, int] = {
    "takeout_json": 0,
    "exif": 1,
    "filename": 2,
    "photos_db": 3,
    "file_mtime": 4,
}

# Values that software substitutes when it has no real date. Any candidate
# landing within SENTINEL_WINDOW of one of these is discarded.
SENTINEL_DATES: tuple[datetime, ...] = (
    datetime(1970, 1, 1),   # Unix epoch
    datetime(1904, 1, 1),   # QuickTime epoch
    datetime(1980, 1, 1),   # FAT epoch
    datetime(2001, 1, 1),   # Apple reference epoch
    datetime(1899, 12, 30), # OLE automation epoch
)
SENTINEL_WINDOW = timedelta(days=1)

DEFAULT_DISAGREEMENT_MARGIN = timedelta(hours=26)


@dataclass(frozen=True)
class Candidate:
    source: str
    value: datetime | None
    raw: str | None = None


@dataclass(frozen=True)
class Decision:
    value: datetime | None
    source: str | None
    rejected: tuple[tuple[str, str], ...]
    needs_review: bool
    review_reason: str | None


def rejection_reason(
    candidate: Candidate,
    *,
    date_added: datetime | None,
    file_mtime: datetime | None,
    floor: datetime | None = None,
    now: datetime | None = None,
) -> str | None:
    """Return why *candidate* cannot be trusted, or None if it survives."""
    value = candidate.value
    if value is None:
        return "unparseable"
    for sentinel in SENTINEL_DATES:
        if abs(value - sentinel) < SENTINEL_WINDOW:
            return "sentinel"
    if (now or datetime.now()) < value:
        return "future"
    # A capture always predates its own import into the library.
    if date_added is not None and value >= date_added:
        return "at_or_after_date_added"
    if file_mtime is not None and value == file_mtime:
        return "equals_file_mtime"
    if floor is not None and value < floor:
        return "below_floor"
    return None


def choose_date(
    candidates: list[Candidate],
    *,
    date_added: datetime | None,
    file_mtime: datetime | None,
    floor: datetime | None = None,
    now: datetime | None = None,
    margin: timedelta = DEFAULT_DISAGREEMENT_MARGIN,
) -> Decision:
    """Pick the winning capture date: earliest survivor, source rank breaking ties."""
    survivors: list[Candidate] = []
    rejected: list[tuple[str, str]] = []
    for candidate in candidates:
        why = rejection_reason(
            candidate, date_added=date_added, file_mtime=file_mtime, floor=floor, now=now
        )
        if why is None:
            survivors.append(candidate)
        else:
            rejected.append((candidate.source, why))

    if not survivors:
        return Decision(None, None, tuple(rejected), True, "no_plausible_candidate")

    winner = min(
        survivors,
        key=lambda c: (c.value, SOURCE_RANK.get(c.source, len(SOURCE_RANK))),
    )
    spread = max(c.value for c in survivors) - min(c.value for c in survivors)
    disagree = spread > margin
    return Decision(
        winner.value,
        winner.source,
        tuple(rejected),
        disagree,
        "candidates_disagree" if disagree else None,
    )
