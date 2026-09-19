"""Decide, per duplicate set, the keeper and the capture date.

Pure catalog-in, catalog-out. Re-running with changed rules costs nothing,
which is the point: every rule here can be revised after the user samples
the review report, without touching a photo.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePath
from sqlite3 import Connection

from photodedup.catalog import require_stage_complete, set_stage
from photodedup.dates import (
    DEFAULT_DISAGREEMENT_MARGIN,
    Candidate,
    Decision,
    Pool,
    choose_date_pooled,
)
from photodedup.grouping import MULTIFRAME_UTIS

# Tie-break order, highest priority first. Each name is the value written to
# `decision.keeper_reason` and `decision_loser.lost_on`.
KEEPER_RULES: tuple[str, ...] = (
    "intact", "file_size", "favorite", "adjustments", "caption", "albums",
    "date_added", "uuid",
)

SIZE_SPREAD_BYTES = 1_000_000

# Flags that describe a rule doing its job rather than a doubt about the
# decision. They are surfaced for sampling and do not block resolvability.
_INFORMATIONAL_FLAGS = frozenset({"size_spread", "dates_disagree"})


@dataclass(frozen=True)
class Member:
    uuid: str
    hash_status: str
    file_size: int | None
    is_favorite: bool
    has_adjustments: bool
    title: str | None
    description: str | None
    albums: tuple[str, ...]
    keywords: tuple[str, ...]
    date_added: str | None
    photos_date: str | None
    file_mtime: str | None
    uti: str | None
    original_filename: str | None


@dataclass(frozen=True)
class ArbitrateResult:
    sets: int
    resolvable: int
    review: int
    dates_rescued: int


def _has_caption(m: Member) -> bool:
    return bool((m.title or "").strip() or (m.description or "").strip())


def _added_ts(value: str | None) -> float:
    return math.inf if value is None else datetime.fromisoformat(value).timestamp()


def rank_key(m: Member) -> tuple:
    """Sort key: ascending order puts the keeper first. One component per
    entry of KEEPER_RULES, in the same order."""
    return (
        1 if m.hash_status == "truncated" else 0,
        -(m.file_size or 0),
        0 if m.is_favorite else 1,
        0 if m.has_adjustments else 1,
        0 if _has_caption(m) else 1,
        -len(m.albums),
        _added_ts(m.date_added),
        m.uuid,
    )


def _separating_rule(a: tuple, b: tuple) -> str:
    for name, x, y in zip(KEEPER_RULES, a, b):
        if x != y:
            return name
    return KEEPER_RULES[-1]


def choose_keeper(
    members: Sequence[Member],
) -> tuple[Member, str, list[tuple[Member, str]]]:
    """Return (keeper, rule that decided it, [(loser, rule it lost on), ...])."""
    if not members:
        raise ValueError("a set has no members")
    ranked = sorted(members, key=rank_key)
    keeper = ranked[0]
    keeper_key = rank_key(keeper)
    losers = [(m, _separating_rule(keeper_key, rank_key(m))) for m in ranked[1:]]
    reason = losers[0][1] if losers else KEEPER_RULES[-1]
    return keeper, reason, losers


def _is_filename_echo(value: str, m: Member) -> bool:
    name = m.original_filename or ""
    stem = PurePath(name).stem
    return value.strip().lower() in {name.lower(), stem.lower()} if name else False


def _caption_gain(keeper: Member, loser: Member, field: str) -> str | None:
    theirs = (getattr(loser, field) or "").strip()
    if not theirs or _is_filename_echo(theirs, loser):
        return None
    ours = (getattr(keeper, field) or "").strip()
    if _is_filename_echo(ours, keeper):
        ours = ""
    return theirs if len(theirs) > len(ours) else None


def carries(keeper: Member, loser: Member) -> dict:
    """What *loser* has that *keeper* lacks. Phase 4 applies it; the report
    shows it so nothing is silently dropped."""
    out: dict = {}
    albums = sorted(set(loser.albums) - set(keeper.albums))
    if albums:
        out["albums"] = albums
    keywords = sorted(set(loser.keywords) - set(keeper.keywords))
    if keywords:
        out["keywords"] = keywords
    if loser.is_favorite and not keeper.is_favorite:
        out["favorite"] = True
    for field in ("title", "description"):
        gained = _caption_gain(keeper, loser, field)
        if gained is not None:
            out[field] = gained
    return out


def review_flags(
    tier: str, members: Sequence[Member], keeper: Member, decision: Decision
) -> list[str]:
    flags: list[str] = []
    if tier == "candidate":
        flags.append("candidate_tier")
    if decision.review_reason == "candidates_disagree":
        flags.append("dates_disagree")
    if decision.review_reason == "no_plausible_candidate":
        flags.append("no_plausible_date")
    if any(m.uti == "public.png" for m in members):
        flags.append("png")
    if any(m.uti in MULTIFRAME_UTIS for m in members):
        flags.append("multiframe")
    if any(m.hash_status == "truncated" for m in members):
        flags.append("truncated_member")
    intact_sizes = [m.file_size for m in members if m.hash_status != "truncated" and m.file_size is not None]
    if len(intact_sizes) > 1 and max(intact_sizes) - min(intact_sizes) >= SIZE_SPREAD_BYTES:
        flags.append("size_spread")
    # Impossible under rank_key. Computed from the data, not the rule, so a
    # bug in the rule shows up in the report instead of in a deletion.
    if keeper.hash_status != "truncated" and any(
        m.hash_status != "truncated" and (m.file_size or 0) > (keeper.file_size or 0)
        for m in members
    ):
        flags.append("keeper_smaller")
    return flags


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def load_members(conn: Connection, set_id: int) -> list[Member]:
    rows = conn.execute(
        """
        SELECT a.uuid, COALESCE(h.status, 'missing') AS hash_status, a.file_size,
               a.is_favorite, a.has_adjustments, a.title, a.description,
               a.albums, a.keywords, a.date_added, a.photos_date, a.file_mtime,
               a.uti, a.original_filename
        FROM dup_set_member m
        JOIN asset a ON a.uuid = m.uuid
        LEFT JOIN asset_hash h ON h.uuid = a.uuid
        WHERE m.set_id = ?
        ORDER BY a.uuid
        """,
        (set_id,),
    ).fetchall()
    return [
        Member(
            uuid=r["uuid"],
            hash_status=r["hash_status"],
            file_size=r["file_size"],
            is_favorite=bool(r["is_favorite"]),
            has_adjustments=bool(r["has_adjustments"]),
            title=r["title"],
            description=r["description"],
            albums=tuple(json.loads(r["albums"]) if r["albums"] else ()),
            keywords=tuple(json.loads(r["keywords"]) if r["keywords"] else ()),
            date_added=r["date_added"],
            photos_date=r["photos_date"],
            file_mtime=r["file_mtime"],
            uti=r["uti"],
            original_filename=r["original_filename"],
        )
        for r in rows
    ]


def _pools(conn: Connection, members: Sequence[Member]) -> list[Pool]:
    by_uuid: dict[str, list[Candidate]] = {m.uuid: [] for m in members}
    placeholders = ", ".join("?" * len(members))
    for r in conn.execute(
        f"SELECT uuid, source, value FROM date_candidate WHERE uuid IN ({placeholders})",
        [m.uuid for m in members],
    ):
        by_uuid[r["uuid"]].append(Candidate(r["source"], _parse(r["value"]), None))
    return [
        Pool(tuple(by_uuid[m.uuid]), _parse(m.date_added), _parse(m.file_mtime))
        for m in members
    ]


def run_arbitrate(
    conn: Connection,
    *,
    margin: timedelta = DEFAULT_DISAGREEMENT_MARGIN,
    force: bool = False,
    now: datetime | None = None,
) -> ArbitrateResult:
    """Rewrite every decision from the current sets and rules."""
    require_stage_complete(conn, "group", force=force)
    set_stage(conn, "arbitrate", "running")
    decided_at = datetime.now().replace(microsecond=0).isoformat()

    sets = conn.execute("SELECT id, tier FROM dup_set ORDER BY id").fetchall()
    resolvable_n = review_n = rescued_n = 0
    try:
        conn.execute("DELETE FROM decision_loser")
        conn.execute("DELETE FROM decision")
        for s in sets:
            members = load_members(conn, s["id"])
            keeper, reason, losers = choose_keeper(members)
            date = choose_date_pooled(_pools(conn, members), margin=margin, now=now)
            flags = review_flags(s["tier"], members, keeper, date)
            blocking = set(flags) - _INFORMATIONAL_FLAGS
            resolvable = s["tier"] == "exact" and not blocking
            keeper_date = _parse(keeper.photos_date)
            changes = (
                date.value is not None
                and (keeper_date is None or abs(date.value - keeper_date) > margin)
            )
            conn.execute(
                """
                INSERT INTO decision (set_id, keeper_uuid, keeper_reason, date_value,
                                      date_source, date_rejected, date_changes,
                                      resolvable, review_flags, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    s["id"], keeper.uuid, reason,
                    date.value.isoformat() if date.value else None,
                    date.source,
                    json.dumps([list(pair) for pair in date.rejected]),
                    int(changes), int(resolvable), ",".join(flags), decided_at,
                ),
            )
            conn.executemany(
                "INSERT INTO decision_loser (set_id, uuid, lost_on, carries) VALUES (?, ?, ?, ?)",
                [
                    (s["id"], m.uuid, why, json.dumps(carries(keeper, m), sort_keys=True))
                    for m, why in losers
                ],
            )
            resolvable_n += int(resolvable)
            review_n += int(not resolvable)
            rescued_n += int(changes)
    except Exception as exc:
        # A half-written decision table is worse than none; decisions are
        # cheap to recompute.
        conn.rollback()
        set_stage(conn, "arbitrate", "failed", notes=str(exc))
        raise

    conn.commit()
    set_stage(
        conn, "arbitrate", "complete",
        notes=(
            f"{len(sets)} sets: {resolvable_n} resolvable, {review_n} need review, "
            f"{rescued_n} dates rescued"
        ),
    )
    return ArbitrateResult(len(sets), resolvable_n, review_n, rescued_n)
