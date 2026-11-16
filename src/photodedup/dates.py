"""Pure date extraction and arbitration. No I/O lives in this module."""

from __future__ import annotations

import re
from collections.abc import Sequence
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

# Default half-width for sentinel matching, used by every epoch that cannot
# possibly be a real capture date in this library.
SENTINEL_WINDOW = timedelta(days=1)

# Values that software substitutes when it has no real date. Each sentinel
# carries its own window because they are not equally safe to discard.
#
# 1904, 1899, 1970 and 1980 are pure software defaults; the user has confirmed
# no photo in this library has a legitimate 1970 or 1980 date, so a full day
# either side costs nothing.
#
# 2001-01-01 is Apple's reference epoch, but it is also a date a real photo can
# have been taken on. A +/- 1 day window spans roughly two days of genuinely
# discardable real captures. 14 hours still covers every timezone rendering of
# a UTC-midnight epoch (UTC-12 through UTC+14) without reaching a full day into
# 2000-12-31 or 2001-01-02.
SENTINEL_DATES: tuple[tuple[datetime, timedelta], ...] = (
    (datetime(1970, 1, 1), SENTINEL_WINDOW),    # Unix epoch
    (datetime(1904, 1, 1), SENTINEL_WINDOW),    # QuickTime epoch
    (datetime(1980, 1, 1), SENTINEL_WINDOW),    # FAT epoch
    # Apple reference epoch. Briefly narrowed to 14 hours to avoid discarding a
    # genuine 1 January 2001 capture, then restored: the user identified this
    # library's 2001 group as a Fujifilm FinePix whose clock had reset, really
    # shot in Chicago on 3 August 2002. A narrow window also split that single
    # reset run in half, rejecting the photos before 14:00 and accepting the
    # rest as a real date, which is worse than either whole answer. Recovering
    # such runs is a clock-reset-cohort problem, not a sentinel-window one.
    (datetime(2001, 1, 1), SENTINEL_WINDOW),
    (datetime(1899, 12, 30), SENTINEL_WINDOW),  # OLE automation epoch
)

DEFAULT_DISAGREEMENT_MARGIN = timedelta(hours=26)

# QuickTime counts seconds from 1904; Unix from 1970. Software that writes Unix
# seconds into a QuickTime date field produces a date exactly this much too
# early (a 2012 clip reads as 1946). In this library, 15 phone videos from
# 2012 and one from 2013 carried such a date on one copy, and the earliest-wins
# rule chose it over the correct date on the other copy (2026-11-16).
QUICKTIME_UNIX_SHIFT = datetime(1970, 1, 1) - datetime(1904, 1, 1)

# Import-time stamps. Two bulk imports into this library overwrote capture
# dates with the import clock (STATE.md, "Phase 1 result"). Each interval is
# the earliest and latest photos_date in that batch, measured from the catalog
# on 2026-09-19. Only a `photos_db` candidate is rejected by these: a genuine
# EXIF date inside the interval survives, and one asset in the 2023 batch has
# exactly that.
#
# These are a domain decision, like SENTINEL_DATES, not something derived from
# cluster size: a real hour in this library holds 218 photos, as many as the
# 2023 stamp. The survey report's capture-hour clustering is how a human finds
# stamps on another library; this constant is where they are then recorded.
IMPORT_STAMPS: tuple[tuple[datetime, datetime], ...] = (
    (datetime(2020, 12, 18, 9, 3, 42), datetime(2020, 12, 18, 9, 26, 2)),
    (datetime(2023, 1, 14, 11, 6, 19), datetime(2023, 1, 14, 11, 49, 25)),
)


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
    stamps: Sequence[tuple[datetime, datetime]] = IMPORT_STAMPS,
) -> str | None:
    """Return why *candidate* cannot be trusted, or None if it survives."""
    value = candidate.value
    if value is None:
        return "unparseable"
    for sentinel, window in SENTINEL_DATES:
        if abs(value - sentinel) < window:
            return "sentinel"
    if candidate.source == "photos_db" and any(
        start <= value <= end for start, end in stamps
    ):
        return "import_stamp"
    if (now or datetime.now()) < value:
        return "future"
    # A capture always predates its own import into the library.
    if date_added is not None and value >= date_added:
        return "at_or_after_date_added"
    # A Photos database date that is exactly the file mtime is the mtime echoed
    # back, not independent evidence. This is NOT true of other sources: a
    # camera writes the file at the capture instant, so EXIF:DateTimeOriginal
    # (or a filename date) equalling FileModifyDate is the normal signature of
    # an untouched original and is corroboration, not fabrication.
    if (
        candidate.source == "photos_db"
        and file_mtime is not None
        and value == file_mtime
    ):
        return "equals_file_mtime"
    if floor is not None and value < floor:
        return "below_floor"
    return None


@dataclass(frozen=True)
class Pool:
    """One asset's candidates with the context its rejection rules need.

    Rejection is per asset: `date_added` differs between the two members of a
    cohort A / cohort B pair, and that difference is the whole reason one of
    them can rescue the other's date.
    """

    candidates: tuple[Candidate, ...]
    date_added: datetime | None
    file_mtime: datetime | None


def choose_date_pooled(
    pools: Sequence[Pool],
    *,
    floor: datetime | None = None,
    now: datetime | None = None,
    margin: timedelta = DEFAULT_DISAGREEMENT_MARGIN,
    stamps: Sequence[tuple[datetime, datetime]] = IMPORT_STAMPS,
) -> Decision:
    """Pick one capture date across every pool: earliest survivor, source rank
    breaking ties. This is the single date authority; nothing else may
    re-express its rules."""
    survivors: list[Candidate] = []
    rejected: list[tuple[str, str]] = []
    for pool in pools:
        for candidate in pool.candidates:
            why = rejection_reason(
                candidate,
                date_added=pool.date_added,
                file_mtime=pool.file_mtime,
                floor=floor,
                now=now,
                stamps=stamps,
            )
            if why is None:
                survivors.append(candidate)
            else:
                rejected.append((candidate.source, why))

    # A survivor that lands on another survivor once shifted forward by the
    # QuickTime/Unix gap is that other date written in the wrong epoch. Only a
    # cross-candidate check can see this: a 1946 date is not wrong on its own.
    shifted = [
        c for c in survivors
        if any(abs(c.value + QUICKTIME_UNIX_SHIFT - o.value) <= margin for o in survivors)
    ]
    survivors = [c for c in survivors if c not in shifted]
    rejected.extend((c.source, "epoch_shift") for c in shifted)

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


def choose_date(
    candidates: list[Candidate],
    *,
    date_added: datetime | None,
    file_mtime: datetime | None,
    floor: datetime | None = None,
    now: datetime | None = None,
    margin: timedelta = DEFAULT_DISAGREEMENT_MARGIN,
    stamps: Sequence[tuple[datetime, datetime]] = IMPORT_STAMPS,
) -> Decision:
    """Pick the winning capture date for a single asset."""
    return choose_date_pooled(
        [Pool(tuple(candidates), date_added, file_mtime)],
        floor=floor, now=now, margin=margin, stamps=stamps,
    )
