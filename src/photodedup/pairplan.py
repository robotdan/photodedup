"""Plan keeper/loser pairs for sets that are one shot saved more than once.

A set qualifies only when every member has the same filename stem (ignoring a
Google `-edited` suffix and a `(1)` counter) and the same capture instant, give
or take whole hours: one import shifted the clock by a timezone, and a shift of
exactly N hours is that, not a different moment. Anything else is left for
review. Pure: the caller reads the catalog and writes the files.

The keeper is a copy Google did not edit when there is one: measured over the
library, a Google `-edited` file is the same picture or a light auto-enhance,
re-saved at lower JPEG quality and sometimes without its GPS. Then the largest,
then the one whose date is right, then the oldest import. The edit is archived
and flagged as an edit pair so a visible change can still be reviewed. A member edited in Apple Photos since import is
never a loser: that edit exists nowhere else.

The date is the camera's EXIF time, or the filename's time when there is no
EXIF; a copy within DATE_TOLERANCE of it is right. The earliest-wins rule used
elsewhere is wrong here: the April 2026 import put most copies hours early.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

DATE_TOLERANCE = timedelta(minutes=10)
MAX_SHIFT = timedelta(hours=26)
SHIFT_SLACK = timedelta(seconds=2)

_EDITED = re.compile(r"[-_]edited$", re.I)
_COUNTER = re.compile(r"\s*\(\d+\)$")


def _base(filename: str) -> str:
    return filename.rsplit(".", 1)[0] if "." in filename else filename


def is_edited(filename: str) -> bool:
    return bool(_EDITED.search(_COUNTER.sub("", _base(filename))))


def stem(filename: str) -> str:
    return _EDITED.sub("", _COUNTER.sub("", _base(filename))).lower()


@dataclass(frozen=True)
class Member:
    uuid: str
    filename: str
    width: int
    height: int
    file_size: int
    photos_date: datetime | None
    date_added: str
    has_adjustments: bool = False
    exif: datetime | None = None
    filename_date: datetime | None = None


@dataclass
class SetPlan:
    set_id: int
    status: str                      # planned | review
    reason: str
    keeper: Member | None = None
    losers: list[Member] = field(default_factory=list)
    kept_also: list[Member] = field(default_factory=list)
    date: datetime | None = None     # write this to the keeper, or None to leave it
    edit_pair: Member | None = None   # the edited loser to show beside the keeper


def _whole_hours(delta: timedelta) -> bool:
    off = abs(delta) % timedelta(hours=1)
    return off <= SHIFT_SLACK or timedelta(hours=1) - off <= SHIFT_SLACK


def _spread(values: Sequence[datetime]) -> timedelta:
    return max(values) - min(values)


def plan_set(set_id: int, members: Sequence[Member]) -> SetPlan:
    def review(reason: str) -> SetPlan:
        return SetPlan(set_id, "review", reason)

    if len({stem(m.filename) for m in members}) != 1:
        return review("different filenames")
    dates = [m.photos_date for m in members]
    if any(d is None for d in dates):
        return review("a copy has no date")
    first = dates[0]
    if _spread(dates) > MAX_SHIFT or not all(_whole_hours(d - first) for d in dates):
        return review("capture times differ by more than a timezone shift")

    exifs = [m.exif for m in members if m.exif]
    names = [m.filename_date for m in members if m.filename_date]
    if exifs and _spread(exifs) > DATE_TOLERANCE:
        return review("EXIF dates disagree")
    if names and _spread(names) > DATE_TOLERANCE:
        return review("filename dates disagree")
    truth = min(exifs) if exifs else (min(names) if names else None)
    if exifs and names and abs(truth - min(names)) > DATE_TOLERANCE:
        return review("EXIF and filename dates disagree")
    if truth is None and _spread(dates) > SHIFT_SLACK:
        return review("times differ and there is no EXIF or filename date to choose by")

    def right(m: Member) -> bool:
        return truth is None or abs(m.photos_date - truth) <= DATE_TOLERANCE

    # Only adopt a time some copy already shows. A video's QuickTime date is
    # UTC, so an EXIF-like time no copy displays is a zone mismatch, not truth.
    if truth is not None and not any(right(m) for m in members):
        return review("no copy shows the EXIF or filename time")

    keeper = min(
        members,
        key=lambda m: (is_edited(m.filename), -(m.width * m.height), not right(m),
                       m.date_added, -m.file_size, m.uuid),
    )
    others = [m for m in members if m is not keeper]
    losers = [m for m in others if not m.has_adjustments]
    kept_also = [m for m in others if m.has_adjustments]
    plan = SetPlan(set_id, "planned", "", keeper, losers, kept_also)
    if not right(keeper):
        # The time a correct copy shows, so the keeper matches it exactly.
        plan.date = min((m for m in members if right(m)), key=lambda m: abs(m.photos_date - truth)).photos_date
    edits = [m for m in losers if is_edited(m.filename)]
    if not is_edited(keeper.filename) and edits:
        plan.edit_pair = min(edits, key=lambda m: (-(m.width * m.height), -m.file_size, m.uuid))
    if not losers:
        plan.status, plan.reason = "review", "every other copy was edited in Photos; nothing to remove"
    return plan


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value[:19]) if value else None


def merge_overlapping(sets: dict[int, list[str]], members: dict[str, Member]) -> dict[int, list[Member]]:
    """Join sets that share a photo (an exact set and a look-alike set can
    both hold it), so no photo is a keeper in one plan and a loser in another.
    A merged set takes its lowest id."""
    parent = {sid: sid for sid in sets}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner: dict[str, int] = {}
    for sid, uuids in sorted(sets.items()):
        for u in uuids:
            if u in owner:
                a, b = find(owner[u]), find(sid)
                parent[max(a, b)] = min(a, b)
            else:
                owner[u] = sid
    merged: dict[int, list[str]] = {}
    for sid, uuids in sets.items():
        merged.setdefault(find(sid), []).extend(uuids)
    return {sid: [members[u] for u in sorted(set(uuids))] for sid, uuids in merged.items()}


def plan_catalog(conn, *, exclude: set[str] = frozenset()) -> list[SetPlan]:
    """Plan every duplicate set in the catalog. A set touching an excluded
    uuid (one already decided elsewhere) is skipped, not reviewed."""
    cands: dict[tuple[str, str], datetime] = {}
    for r in conn.execute(
        "SELECT uuid, source, value FROM date_candidate "
        "WHERE source IN ('exif', 'filename') AND rejected_reason IS NULL AND value IS NOT NULL"
    ):
        value = _parse(r["value"])
        # A date-only filename (IMG-20140612-WA0001) parses to midnight and
        # says nothing about the time of day.
        if r["source"] == "filename" and value.time() == datetime.min.time():
            continue
        cands.setdefault((r["uuid"], r["source"]), value)
    members: dict[str, Member] = {}
    sets: dict[int, list[str]] = {}
    for r in conn.execute(
        """SELECT m.set_id, a.uuid, a.original_filename, a.filename, a.width, a.height,
                  a.file_size, a.photos_date, a.date_added, a.has_adjustments
           FROM dup_set_member m JOIN asset a ON a.uuid = m.uuid
           ORDER BY m.set_id, a.uuid"""
    ):
        sets.setdefault(r["set_id"], []).append(r["uuid"])
        members[r["uuid"]] = Member(
            uuid=r["uuid"],
            filename=r["original_filename"] or r["filename"] or "",
            width=r["width"] or 0,
            height=r["height"] or 0,
            file_size=r["file_size"] or 0,
            photos_date=_parse(r["photos_date"]),
            date_added=r["date_added"] or "",
            has_adjustments=bool(r["has_adjustments"]),
            exif=cands.get((r["uuid"], "exif")),
            filename_date=cands.get((r["uuid"], "filename")),
        )
    by_set = merge_overlapping(sets, members)
    return [
        plan_set(set_id, members)
        for set_id, members in sorted(by_set.items())
        if not any(m.uuid in exclude for m in members)
    ]
