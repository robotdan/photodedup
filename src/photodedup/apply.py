"""Apply the decisions to Photos: merge, dates, staging. Never deletes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Connection

from photodedup.catalog import StageIncomplete, set_stage
from photodedup.photos_write import PhotosWriter

BATCH = 500
FOLDER = "Dedup Delete"
BLANK_ALBUM = "Dedup Delete blank"


@dataclass(frozen=True)
class ApplyResult:
    done: int
    failed: int
    skipped: int


@dataclass(frozen=True)
class MergeTarget:
    keeper: str
    loser: str
    carries: dict


@dataclass(frozen=True)
class DateTarget:
    keeper: str
    set_id: int
    value: datetime
    disagreeing: bool


def _now(now: datetime | None) -> str:
    return (now or datetime.now()).replace(microsecond=0).isoformat()


def require_archive(conn: Connection) -> None:
    """Every pending loser must have a verified archived original."""
    missing = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM decision_loser l
        JOIN dup_set s ON s.id = l.set_id AND s.tier = 'exact'
        JOIN decision d ON d.set_id = l.set_id AND d.resolvable = 1
        WHERE NOT EXISTS (
            SELECT 1 FROM archive_file f WHERE f.uuid = l.uuid AND f.kind = 'original'
        )
        """
    ).fetchone()["n"]
    if missing:
        raise StageIncomplete(
            f"{missing} losers have no verified archive copy. Run `photodedup archive` "
            "until it reports 0 mismatched and 0 missing."
        )


def _record(conn: Connection, uuid: str, action: str, status: str, detail: dict, previous: dict | None, now: datetime | None) -> None:
    conn.execute(
        """INSERT INTO apply_action (uuid, action, status, detail, previous, applied_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (uuid, action) DO UPDATE SET
               status = excluded.status, detail = excluded.detail,
               previous = excluded.previous, applied_at = excluded.applied_at""",
        (uuid, action, status, json.dumps(detail, sort_keys=True),
         json.dumps(previous, sort_keys=True) if previous is not None else None, _now(now)),
    )
    conn.commit()


def _done(conn: Connection, action: str) -> set[str]:
    return {
        r["uuid"] for r in conn.execute(
            "SELECT uuid FROM apply_action WHERE action = ? AND status = 'done'", (action,)
        )
    }


# --- merge -----------------------------------------------------------------


def merge_targets(conn: Connection) -> list[MergeTarget]:
    rows = conn.execute(
        """
        SELECT d.keeper_uuid AS keeper, l.uuid AS loser, l.carries AS carries
        FROM decision_loser l
        JOIN dup_set s ON s.id = l.set_id AND s.tier = 'exact'
        JOIN decision d ON d.set_id = l.set_id AND d.resolvable = 1
        WHERE l.carries != '{}'
        ORDER BY d.keeper_uuid, l.uuid
        """
    ).fetchall()
    return [MergeTarget(r["keeper"], r["loser"], json.loads(r["carries"])) for r in rows]


def _merged_carries(targets: list[MergeTarget]) -> dict[str, dict]:
    """One combined carry per keeper: unions for albums/keywords, OR for favorite,
    longest for title/description."""
    out: dict[str, dict] = {}
    for t in targets:
        c = out.setdefault(t.keeper, {"albums": [], "keywords": [], "favorite": False, "title": None, "description": None, "losers": []})
        c["losers"].append(t.loser)
        for key in ("albums", "keywords"):
            for value in t.carries.get(key, []):
                if value not in c[key]:
                    c[key].append(value)
        if t.carries.get("favorite"):
            c["favorite"] = True
        for key in ("title", "description"):
            value = t.carries.get(key)
            if value and (c[key] is None or len(value) > len(c[key])):
                c[key] = value
    return out


def run_merge(conn: Connection, writer: PhotosWriter, *, dry_run: bool = False, now: datetime | None = None) -> ApplyResult:
    require_archive(conn)
    combined = _merged_carries(merge_targets(conn))
    done_before = _done(conn, "merge")
    done = failed = skipped = 0
    if not dry_run:
        set_stage(conn, "apply", "running")
    for keeper in sorted(combined):
        carry = combined[keeper]
        if keeper in done_before:
            skipped += 1
            continue
        if dry_run:
            continue
        try:
            state = writer.get(keeper)
            previous = {
                "title": state.title, "description": state.description,
                "keywords": list(state.keywords), "favorite": state.favorite,
                "albums": list(state.albums),
            }
            written: dict = {}
            albums = [a for a in carry["albums"] if a not in state.albums]
            for album in albums:
                writer.add_to_album(writer.ensure_album(album), [keeper])
            if albums:
                written["albums"] = albums
            keywords = [k for k in carry["keywords"] if k not in state.keywords]
            if keywords:
                writer.set_keywords(keeper, list(state.keywords) + keywords)
                written["keywords"] = keywords
            if carry["favorite"] and not state.favorite:
                writer.set_favorite(keeper, True)
                written["favorite"] = True
            for key in ("title", "description"):
                value = carry[key]
                current = getattr(state, key)
                if value and len(value) > len(current):
                    getattr(writer, f"set_{key}")(keeper, value)
                    written[key] = value
            written["losers"] = carry["losers"]
            _record(conn, keeper, "merge", "done", written, previous, now)
            done += 1
        except Exception as exc:
            _record(conn, keeper, "merge", "failed", {"error": str(exc)}, None, now)
            failed += 1
    return ApplyResult(done, failed, skipped)


# --- dates -----------------------------------------------------------------


def date_targets(conn: Connection, *, include_disagreeing: bool) -> list[DateTarget]:
    rows = conn.execute(
        """
        SELECT d.set_id, d.keeper_uuid, d.date_value, d.review_flags
        FROM decision d JOIN dup_set s ON s.id = d.set_id
        WHERE s.tier = 'exact' AND d.resolvable = 1 AND d.date_changes = 1
          AND d.date_value IS NOT NULL
        ORDER BY d.keeper_uuid
        """
    ).fetchall()
    out = []
    for r in rows:
        disagreeing = "dates_disagree" in (r["review_flags"] or "").split(",")
        if disagreeing and not include_disagreeing:
            continue
        out.append(DateTarget(r["keeper_uuid"], r["set_id"], datetime.fromisoformat(r["date_value"]), disagreeing))
    return out


def run_dates(conn: Connection, writer: PhotosWriter, *, include_disagreeing: bool = False, dry_run: bool = False, now: datetime | None = None) -> ApplyResult:
    require_archive(conn)
    done_before = _done(conn, "date")
    done = failed = skipped = 0
    if not dry_run:
        set_stage(conn, "apply", "running")
    for target in date_targets(conn, include_disagreeing=include_disagreeing):
        if target.keeper in done_before:
            skipped += 1
            continue
        if dry_run:
            continue
        try:
            before = writer.get(target.keeper).date
            writer.set_date(target.keeper, target.value)
            after = writer.get(target.keeper).date
            previous = {"date": before.isoformat() if before else None}
            if after is not None and abs((after - target.value).total_seconds()) <= 1:
                _record(conn, target.keeper, "date", "done", {"date": target.value.isoformat()}, previous, now)
                done += 1
            else:
                _record(conn, target.keeper, "date", "failed",
                        {"wanted": target.value.isoformat(), "read_back": after.isoformat() if after else None, "set_id": target.set_id},
                        previous, now)
                failed += 1
        except Exception as exc:
            _record(conn, target.keeper, "date", "failed", {"error": str(exc), "set_id": target.set_id}, None, now)
            failed += 1
    return ApplyResult(done, failed, skipped)


# --- plan ------------------------------------------------------------------


def plan_lines(conn: Connection, *, include_disagreeing: bool = False) -> list[str]:
    merges = len(_merged_carries(merge_targets(conn)))
    plain = date_targets(conn, include_disagreeing=False)
    every = date_targets(conn, include_disagreeing=True)
    losers = conn.execute(
        """SELECT COUNT(*) AS n FROM decision_loser l
           JOIN dup_set s ON s.id = l.set_id AND s.tier = 'exact'
           JOIN decision d ON d.set_id = l.set_id AND d.resolvable = 1"""
    ).fetchone()["n"]
    blank = conn.execute(
        """SELECT COUNT(*) AS n FROM decision d JOIN dup_set s ON s.id = d.set_id
           WHERE s.tier = 'exact' AND ',' || d.review_flags || ',' LIKE '%,blank_image,%'"""
    ).fetchone()["n"]
    chosen = every if include_disagreeing else plain
    return [
        f"--merge: {merges} keepers receive albums, keywords, favorite or captions from losers",
        f"--dates: {len(chosen)} keeper date{'s' if len(chosen) != 1 else ''} corrected"
        + ("" if include_disagreeing else f" ({len(every) - len(plain)} more with --include-disagreeing-dates)"),
        f"--stage: {losers} losers into '{FOLDER}' albums of {BATCH}, plus {blank} blank-image keepers into '{BLANK_ALBUM}'",
        "Nothing is deleted. Delete the staged albums yourself in Photos.",
    ]
