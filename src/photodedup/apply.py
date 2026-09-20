"""Apply the decisions to Photos: merge, dates, staging. Never deletes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Connection

from photodedup.catalog import StageIncomplete, require_stage_complete, set_stage
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


MISSING_LISTED = 20


def require_archive(conn: Connection) -> None:
    """Everything staging will touch must have a verified archived original,
    and the archive stage itself must be complete.

    The two checks catch different half-finished states: the count catches a
    photo with no archived original at all; the stage check catches one whose
    original copied but whose edited or live render did not, which the count
    alone (it only looks at `kind='original'`) would miss while the archive
    stage sits at 'running'.

    "Everything" is the losers plus the keepers of blank-image sets: those
    keepers go into `Dedup Delete blank` for the user to delete, so they need
    the same archived fallback a loser gets.
    """
    missing = conn.execute(
        """
        SELECT t.uuid AS uuid, t.original_filename AS original_filename FROM (
            SELECT l.uuid AS uuid, a.original_filename AS original_filename
            FROM decision_loser l
            JOIN dup_set s ON s.id = l.set_id AND s.tier = 'exact'
            JOIN decision d ON d.set_id = l.set_id AND d.resolvable = 1
            JOIN asset a ON a.uuid = l.uuid
            UNION
            SELECT d.keeper_uuid, a.original_filename
            FROM decision d
            JOIN dup_set s ON s.id = d.set_id AND s.tier = 'exact'
            JOIN asset a ON a.uuid = d.keeper_uuid
            WHERE ',' || d.review_flags || ',' LIKE '%,blank_image,%'
        ) t
        WHERE NOT EXISTS (
            SELECT 1 FROM archive_file f WHERE f.uuid = t.uuid AND f.kind = 'original'
        )
        ORDER BY t.uuid
        """
    ).fetchall()
    if missing:
        listed = ", ".join(
            f"{r['original_filename'] or '?'} ({r['uuid']})" for r in missing[:MISSING_LISTED]
        )
        more = len(missing) - MISSING_LISTED
        raise StageIncomplete(
            f"{len(missing)} photo{'s' if len(missing) != 1 else ''} staged for deletion "
            f"{'have' if len(missing) != 1 else 'has'} no verified archive copy. "
            "Run `photodedup archive` until it reports 0 mismatched and 0 missing. "
            f"Missing: {listed}" + (f" and {more} more." if more > 0 else ".")
        )
    require_stage_complete(conn, "archive")


def _record(conn: Connection, uuid: str, action: str, status: str, detail: dict, previous: dict | None, now: datetime | None) -> None:
    """Record an apply attempt. `previous` keeps the EARLIEST non-null value
    ever recorded for this (uuid, action): a retry after a partial failure
    must not overwrite the true pre-write state with the post-partial state
    the retry itself observed, or the undo record is falsified."""
    conn.execute(
        """INSERT INTO apply_action (uuid, action, status, detail, previous, applied_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (uuid, action) DO UPDATE SET
               status = excluded.status, detail = excluded.detail,
               previous = COALESCE(apply_action.previous, excluded.previous),
               applied_at = excluded.applied_at""",
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


def _merge_done_losers(conn: Connection) -> dict[str, list[str]]:
    """uuid -> sorted losers list recorded on its last successful merge.

    A keeper is only truly done when the losers it was merged from match the
    losers it currently has: a re-arbitrate can attach a new loser (with new
    carries) to an already-merged keeper, and that must trigger another
    merge rather than being skipped as already-done.
    """
    return {
        r["uuid"]: sorted(json.loads(r["detail"]).get("losers", []))
        for r in conn.execute(
            "SELECT uuid, detail FROM apply_action WHERE action = 'merge' AND status = 'done'"
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
    done_losers = _merge_done_losers(conn)
    done = failed = skipped = 0
    if not dry_run:
        set_stage(conn, "apply", "running")
    for keeper in sorted(combined):
        carry = combined[keeper]
        if done_losers.get(keeper) == sorted(carry["losers"]):
            skipped += 1
            continue
        if dry_run:
            continue
        state = writer.get(keeper)
        previous = {
            "title": state.title, "description": state.description,
            "keywords": list(state.keywords), "favorite": state.favorite,
            "albums": list(state.albums),
        }
        written: dict = {}
        # Persist the pre-write state BEFORE the first write. A crash, a
        # Ctrl-C or a Photos hang between here and the last write would
        # otherwise leave the keeper changed with no record of what it was,
        # and `previous` is the only undo this tool has.
        _record(conn, keeper, "merge", "failed", {"in_progress": True}, previous, now)
        try:
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
            _record(conn, keeper, "merge", "failed", {"error": str(exc), "written": written}, previous, now)
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
        previous = None
        try:
            before = writer.get(target.keeper).date
            previous = {"date": before.isoformat() if before else None}
            # The old date goes on record before the new one is written; see
            # run_merge for why.
            _record(conn, target.keeper, "date", "failed", {"in_progress": True}, previous, now)
            writer.set_date(target.keeper, target.value)
            after = writer.get(target.keeper).date
            if after is not None and abs((after - target.value).total_seconds()) <= 1:
                _record(conn, target.keeper, "date", "done", {"date": target.value.isoformat()}, previous, now)
                done += 1
            else:
                _record(conn, target.keeper, "date", "failed",
                        {"wanted": target.value.isoformat(), "read_back": after.isoformat() if after else None},
                        previous, now)
                failed += 1
        except Exception as exc:
            _record(conn, target.keeper, "date", "failed", {"error": str(exc)}, previous, now)
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


# --- stage -----------------------------------------------------------------


def _stage_population(conn: Connection) -> tuple[list[str], list[str]]:
    losers = [
        r["uuid"] for r in conn.execute(
            """SELECT l.uuid FROM decision_loser l
               JOIN dup_set s ON s.id = l.set_id AND s.tier = 'exact'
               JOIN decision d ON d.set_id = l.set_id AND d.resolvable = 1
               ORDER BY l.uuid"""
        )
    ]
    blank_keepers = [
        r["keeper_uuid"] for r in conn.execute(
            """SELECT d.keeper_uuid FROM decision d JOIN dup_set s ON s.id = d.set_id
               WHERE s.tier = 'exact' AND ',' || d.review_flags || ',' LIKE '%,blank_image,%'
               ORDER BY d.keeper_uuid"""
        )
    ]
    return losers, blank_keepers


def _album_counts(conn: Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in conn.execute("SELECT detail FROM apply_action WHERE action = 'stage' AND status = 'done'"):
        name = json.loads(r["detail"]).get("album")
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _album_name(index: int) -> str:
    return f"{FOLDER} {index:03d}"


def run_stage(conn: Connection, writer: PhotosWriter, *, batch: int = BATCH, dry_run: bool = False, now: datetime | None = None) -> ApplyResult:
    """Put every loser in a numbered album under the Dedup Delete folder.

    Albums fill in order and a re-run continues in the first album that
    still has room, so numbering never restarts and no loser is staged twice.
    """
    require_archive(conn)
    losers, blank_keepers = _stage_population(conn)
    done_stage = _done(conn, "stage")
    done_blank = _done(conn, "stage_blank")
    done = failed = skipped = 0
    if not dry_run:
        set_stage(conn, "apply", "running")

    counts = _album_counts(conn)
    indices = []
    for name in counts:
        try:
            indices.append(int(name.rsplit(" ", 1)[1]))
        except (ValueError, IndexError):
            continue
    index = max(indices or [1])
    while counts.get(_album_name(index), 0) >= batch:
        index += 1

    # Work out every album assignment first, then add each album's photos in
    # ONE call. Photos takes about as long to add 500 photos as one, and a
    # per-photo call is 16,854 AppleScript round trips.
    pending: list[str] = []
    for uuid in losers:
        if uuid in done_stage:
            skipped += 1
            continue
        pending.append(uuid)

    if not dry_run:
        batches: list[tuple[str, list[str]]] = []
        for uuid in pending:
            while counts.get(_album_name(index), 0) >= batch:
                index += 1
            name = _album_name(index)
            if batches and batches[-1][0] == name:
                batches[-1][1].append(uuid)
            else:
                batches.append((name, [uuid]))
            counts[name] = counts.get(name, 0) + 1

        for name, uuids in batches:
            try:
                writer.add_to_album(writer.ensure_album(name, folder=FOLDER), uuids)
            except Exception as exc:
                # One album's add failed; the whole batch is unrecorded and
                # retried next run. The remaining albums still go in.
                for uuid in uuids:
                    _record(conn, uuid, "stage", "failed", {"error": str(exc), "album": name}, None, now)
                failed += len(uuids)
                continue
            for uuid in uuids:
                _record(conn, uuid, "stage", "done", {"album": name}, None, now)
            done += len(uuids)

    for uuid in blank_keepers:
        if uuid in done_blank:
            skipped += 1
            continue
        if dry_run:
            continue
        try:
            writer.add_to_album(writer.ensure_album(BLANK_ALBUM), [uuid])
            _record(conn, uuid, "stage_blank", "done", {"album": BLANK_ALBUM}, None, now)
            done += 1
        except Exception as exc:
            _record(conn, uuid, "stage_blank", "failed", {"error": str(exc)}, None, now)
            failed += 1
    return ApplyResult(done, failed, skipped)


def write_losers_csv(conn: Connection, path) -> int:
    import csv

    rows = conn.execute(
        """
        SELECT l.uuid, a.original_filename, a.photos_date, d.keeper_uuid, l.set_id, l.lost_on,
               act.detail AS stage_detail, f.dest AS archive_dest
        FROM decision_loser l
        JOIN dup_set s ON s.id = l.set_id AND s.tier = 'exact'
        JOIN decision d ON d.set_id = l.set_id AND d.resolvable = 1
        JOIN asset a ON a.uuid = l.uuid
        LEFT JOIN apply_action act ON act.uuid = l.uuid AND act.action = 'stage' AND act.status = 'done'
        LEFT JOIN archive_file f ON f.uuid = l.uuid AND f.kind = 'original'
        ORDER BY l.uuid
        """
    ).fetchall()
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["uuid", "original_filename", "photos_date", "keeper_uuid", "set_id", "lost_on", "album", "archive_dest"])
        for r in rows:
            album = json.loads(r["stage_detail"]).get("album") if r["stage_detail"] else ""
            writer.writerow([r["uuid"], r["original_filename"], r["photos_date"], r["keeper_uuid"], r["set_id"], r["lost_on"], album, r["archive_dest"] or ""])
    return len(rows)


# --- self test -------------------------------------------------------------


def self_test(writer: PhotosWriter, uuid: str, *, now: datetime | None = None) -> list[str]:
    """Prove the write path on one existing photo, then put everything back.

    Creates a throwaway album, adds the photo, sets a marker title and a
    marker date, reads both back, restores the originals, deletes the album.
    Raises RuntimeError on the first check that fails, after restoring.
    """
    lines: list[str] = []
    if not writer.exists(uuid):
        raise RuntimeError(f"photo {uuid} not found in Photos")
    original = writer.get(uuid)
    if original.date is None:
        raise RuntimeError("photo has no capture date; pick another photo for the self-test")
    # Print what is about to be changed before anything is changed: if the
    # restore at the end fails, these two lines are how the user puts the
    # photo back by hand.
    lines.append(f"original title: {original.title!r}")
    lines.append(f"original date: {original.date.isoformat()}")
    marker_title = f"photodedup self-test {_now(now)}"
    marker_date = datetime(2003, 2, 23, 9, 8, 8)
    album_id = None
    problems: list[str] = []
    try:
        album_id = writer.ensure_album(f"photodedup self-test {_now(now)}")
        added = writer.add_to_album(album_id, [uuid])
        lines.append(f"album add: {'ok' if added == [uuid] else 'FAILED'}")
        if added != [uuid]:
            problems.append("album add did not report the photo")
        writer.set_title(uuid, marker_title)
        got = writer.get(uuid).title
        lines.append(f"title round-trip: {'ok' if got == marker_title else 'FAILED'}")
        if got != marker_title:
            problems.append("title did not round-trip")
        writer.set_date(uuid, marker_date)
        back = writer.get(uuid).date
        drift = None if back is None else abs((back - marker_date).total_seconds())
        lines.append(f"date round-trip: {'ok' if drift == 0 else f'drift of {drift} seconds'} (wrote {marker_date.isoformat()}, read {back.isoformat() if back else None})")
        if drift != 0:
            problems.append(f"date drift of {drift} seconds")
    finally:
        # Each restore stands alone: a failure putting the title back must
        # not leave the marker date on the photo or the test album in the
        # library, which is what one shared try/except would do.
        restored = []
        for what, action in (
            ("title", lambda: writer.set_title(uuid, original.title)),
            ("date", lambda: writer.set_date(uuid, original.date)),
            ("album", lambda: writer.delete_album(album_id)),
        ):
            if what == "album" and album_id is None:
                continue
            try:
                action()
                restored.append(what)
            except Exception as exc:
                problems.append(f"could not restore {what}: {exc}")
                lines.append(f"RESTORE FAILED for {what}: {exc}")
        lines.append("restored: " + (", ".join(restored) if restored else "nothing"))
    if problems:
        raise RuntimeError("self-test failed: " + "; ".join(problems) + "\n" + "\n".join(lines))
    return lines
