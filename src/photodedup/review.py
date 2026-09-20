"""Static HTML review report over the arbitrate decisions.

Plain HTML and one stylesheet. No JavaScript, no network. Thumbnails are the
derivatives Photos already keeps, linked into the report directory so no
original is ever decoded here.
"""

from __future__ import annotations

import html
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from sqlite3 import Connection, Row

import imagehash

from photodedup.catalog import StageIncomplete, require_stage_complete, set_stage
from photodedup.dates import DEFAULT_DISAGREEMENT_MARGIN

CSS = """
body { font: 14px/1.4 -apple-system, Helvetica, Arial, sans-serif; margin: 1.5rem; color: #222; }
h1, h2 { font-weight: 600; }
table.totals td { padding: 0.15rem 1rem 0.15rem 0; }
.set { border: 1px solid #ddd; border-radius: 6px; padding: 0.75rem; margin: 1rem 0; }
.set h3 { margin: 0 0 0.5rem; font-size: 1rem; }
.flags { color: #a40; }
.resolvable { color: #080; }
.members { display: flex; flex-wrap: wrap; gap: 0.75rem; }
.member { width: 220px; border: 2px solid transparent; padding: 0.4rem; border-radius: 4px; }
.member.keeper { border-color: #080; background: #f2fbf2; }
.member img, .member .placeholder { width: 200px; height: 150px; object-fit: contain; background: #f4f4f4; display: block; }
.member .placeholder { color: #999; text-align: center; line-height: 150px; }
.member dl { margin: 0.4rem 0 0; font-size: 12px; }
.member dt { color: #777; float: left; clear: left; width: 62px; }
.member dd { margin: 0 0 0 66px; word-break: break-all; }
.date-agree { color: #080; } .date-conflict { color: #a40; font-weight: 600; } .date-missing { color: #999; }
nav a { margin-right: 0.75rem; }
"""


@dataclass(frozen=True)
class ReviewResult:
    sets: int
    pages: int
    thumbs: int
    placeholders: int


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _page(title: str, body: str, *, depth: int) -> str:
    css = "../" * depth + "review.css"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_e(title)}</title><link rel='stylesheet' href='{css}'></head>"
        f"<body><h1>{_e(title)}</h1>{body}</body></html>"
    )


def _link_thumb(src: str | None, dest_dir: Path, uuid: str) -> str | None:
    """Return the published thumbnail's file name, or None for a placeholder."""
    if not src or not Path(src).is_file():
        return None
    name = f"{uuid}{Path(src).suffix.lower() or '.jpg'}"
    target = dest_dir / name
    if target.exists() or target.is_symlink():
        return name
    try:
        os.symlink(src, target)
    except OSError:
        shutil.copyfile(src, target)
    return name


def _phash_distance(members: list[Row]) -> int | None:
    hashes = [m["phash"] for m in members if m["phash"]]
    if len(hashes) < 2:
        return None
    parsed = [imagehash.hex_to_hash(h) for h in hashes]
    return max(a - b for i, a in enumerate(parsed) for b in parsed[i + 1 :])


def _load(conn: Connection) -> list[dict]:
    """Every set with its decision, members and losers, ordered by set id."""
    sets = conn.execute(
        """
        SELECT s.id, s.tier, s.size, d.keeper_uuid, d.keeper_reason, d.date_value,
               d.date_source, d.date_rejected, d.date_changes, d.resolvable, d.review_flags
        FROM dup_set s JOIN decision d ON d.set_id = s.id
        ORDER BY s.id
        """
    ).fetchall()
    members = conn.execute(
        """
        SELECT m.set_id, a.uuid, a.original_filename, a.file_size, a.photos_date,
               a.date_added, a.thumb_path, a.uti, COALESCE(h.status, 'missing') AS hash_status,
               h.phash, h.blank_rows, h.flat, l.lost_on, l.carries
        FROM dup_set_member m
        JOIN asset a ON a.uuid = m.uuid
        LEFT JOIN asset_hash h ON h.uuid = a.uuid
        LEFT JOIN decision_loser l ON l.set_id = m.set_id AND l.uuid = m.uuid
        ORDER BY m.set_id, a.uuid
        """
    ).fetchall()
    by_set: dict[int, list[Row]] = {}
    for m in members:
        by_set.setdefault(m["set_id"], []).append(m)
    out = []
    for s in sets:
        ms = by_set.get(s["id"], [])
        out.append({"set": s, "members": ms, "phash_distance": _phash_distance(ms)})
    return out


def _date_class(member: Row, s: Row, margin: timedelta) -> str:
    """Compare parsed instants, not rendered strings: a timezone rendering of
    the same instant must not paint as a conflict."""
    if not member["photos_date"] or not s["date_value"]:
        return "date-missing"
    try:
        member_date = datetime.fromisoformat(member["photos_date"])
        set_date = datetime.fromisoformat(s["date_value"])
    except ValueError:
        return "date-missing"
    return "date-agree" if abs(member_date - set_date) <= margin else "date-conflict"


def _render_set(
    entry: dict, thumbs_dir: Path, *, depth: int, counters: dict, margin: timedelta
) -> str:
    s = entry["set"]
    flags = s["review_flags"]
    status = (
        "<span class='resolvable'>resolvable</span>" if s["resolvable"]
        else "<span class='flags'>needs review</span>"
    )
    head = (
        f"<div class='set' id='set-{s['id']}'><h3>Set {s['id']} · {_e(s['tier'])} · "
        f"{s['size']} members · {status}"
        + (f" · <span class='flags'>flags: {_e(flags)}</span>" if flags else "")
        + "</h3>"
    )
    meta = (
        f"<p>keeper decided by <code>{_e(s['keeper_reason'])}</code>; capture date "
        f"<strong>{_e(s['date_value'] or 'none')}</strong> from <code>{_e(s['date_source'] or '-')}</code>"
        + (" (differs from keeper's Photos date: date rescued)" if s["date_changes"] else "")
        + (f"; phash distance {entry['phash_distance']}" if entry["phash_distance"] is not None else "")
        + "</p>"
    )
    cards = []
    for m in entry["members"]:
        is_keeper = m["uuid"] == s["keeper_uuid"]
        name = _link_thumb(m["thumb_path"], thumbs_dir, m["uuid"])
        if name is None:
            counters["placeholders"].add(m["uuid"])
            img = "<div class='placeholder'>no thumbnail</div>"
        else:
            counters["thumbs"].add(m["uuid"])
            img = f"<img loading='lazy' src='{'../' * depth}thumbs/{_e(name)}' alt=''>"
        carries = json.loads(m["carries"]) if m["carries"] else {}
        cards.append(
            f"<div class='member{' keeper' if is_keeper else ''}'>{img}<dl>"
            f"<dt>file</dt><dd>{_e(m['original_filename'])}</dd>"
            f"<dt>bytes</dt><dd>{_e(m['file_size'])}</dd>"
            f"<dt>photos</dt><dd class='{_date_class(m, s, margin)}'>{_e(m['photos_date'] or 'missing')}</dd>"
            f"<dt>added</dt><dd>{_e(m['date_added'])}</dd>"
            f"<dt>hash</dt><dd>{_e(m['hash_status'])}"
            + (f" · {m['blank_rows']} blank rows" if m["blank_rows"] else "")
            + (" · flat image" if m["flat"] else "")
            + "</dd>"
            f"<dt>{'keeper' if is_keeper else ('lost on' if m['lost_on'] else 'kept')}</dt>"
            f"<dd>{'KEEP' if is_keeper else (_e(m['lost_on']) if m['lost_on'] else 'KEEP (edited)')}</dd>"
            + (f"<dt>carries</dt><dd>{_e(json.dumps(carries))}</dd>" if carries else "")
            + "</dl></div>"
        )
    return head + meta + "<div class='members'>" + "".join(cards) + "</div></div>"


def _paginate(entries: list[dict], per_page: int) -> list[list[dict]]:
    return [entries[i : i + per_page] for i in range(0, len(entries), per_page)] or [[]]


def _nav(prefix: str, count: int, current: int) -> str:
    links = [
        f"<a href='{i:03d}.html'>{i}</a>" if i != current else f"<strong>{i}</strong>"
        for i in range(1, count + 1)
    ]
    return f"<nav><a href='../index.html'>index</a> {prefix}: {' '.join(links)}</nav>"


def _refuse_unsafe_out_dir(out_dir: Path) -> None:
    """Refuse to clear *out_dir* unless it is empty, absent, or a previous
    report (marked by `review.css`). Anything else is someone else's data."""
    if not out_dir.exists():
        return
    if any(out_dir.iterdir()) and not (out_dir / "review.css").exists():
        raise ValueError(
            f"{out_dir} is not empty and does not look like a previous review "
            "report (no review.css); refusing to delete its contents. Point "
            "--out at an empty or new directory."
        )


def write_review(
    conn: Connection,
    out_dir: Path,
    *,
    per_page: int = 250,
    sample: int = 300,
    force: bool = False,
    margin: timedelta = DEFAULT_DISAGREEMENT_MARGIN,
) -> ReviewResult:
    require_stage_complete(conn, "arbitrate", force=force)
    missing = conn.execute(
        """
        SELECT COUNT(*) AS n FROM dup_set s
        WHERE NOT EXISTS (SELECT 1 FROM decision d WHERE d.set_id = s.id)
        """
    ).fetchone()["n"]
    if missing and not force:
        raise StageIncomplete(
            f"{missing} duplicate sets have no decision; the groups were rebuilt "
            "after the last arbitrate run. Re-run `photodedup arbitrate`, or pass "
            "--force to report only the sets that still have one."
        )
    set_stage(conn, "review", "running")

    out_dir = Path(out_dir)
    _refuse_unsafe_out_dir(out_dir)
    # Regenerated in full every run: a shrinking rerun must not leave stale
    # pages or thumbnails behind. thumbs/ holds only symlinks or copies of
    # Photos derivatives, so removing it touches nothing in the library.
    for sub in ("exact", "candidate", "samples", "thumbs"):
        target = out_dir / sub
        if target.is_symlink():
            target.unlink()
        else:
            try:
                shutil.rmtree(target)
            except FileNotFoundError:
                pass
        target.mkdir(parents=True, exist_ok=True)
    (out_dir / "review.css").write_text(CSS)
    thumbs_dir = out_dir / "thumbs"
    # Sets of uuids: a member rendered on a listing page and again on a sample
    # page is still one thumbnail on disk.
    counters: dict[str, set[str]] = {"thumbs": set(), "placeholders": set()}

    entries = _load(conn)
    exact = [e for e in entries if e["set"]["tier"] == "exact"]
    candidate = [e for e in entries if e["set"]["tier"] == "candidate"]
    pages = 0

    def write_listing(name: str, items: list[dict]) -> int:
        chunks = _paginate(items, per_page)
        for i, chunk in enumerate(chunks, start=1):
            body = _nav(name, len(chunks), i) + "".join(
                _render_set(e, thumbs_dir, depth=1, counters=counters, margin=margin)
                for e in chunk
            )
            (out_dir / name / f"{i:03d}.html").write_text(
                _page(f"{name} sets, page {i} of {len(chunks)}", body, depth=1)
            )
        return len(chunks)

    pages += write_listing("exact", exact)
    # 999 is a safe sentinel for "no phash distance": a 64-bit phash distance
    # never exceeds 64, so it always sorts last.
    candidate_sorted = sorted(
        candidate,
        key=lambda e: (e["phash_distance"] if e["phash_distance"] is not None else 999, e["set"]["id"]),
    )
    pages += write_listing("candidate", candidate_sorted[:sample])

    def has_flag(e: dict, flag: str) -> bool:
        return flag in e["set"]["review_flags"].split(",")

    def spread(e: dict) -> int:
        sizes = [m["file_size"] for m in e["members"] if m["file_size"] is not None]
        return max(sizes) - min(sizes) if sizes else 0

    def keeper_gap(e: dict) -> float:
        """Seconds between the arbitrated date and the keeper's own Photos date.

        -1.0 for "unknown" (not inf): sorted by -keeper_gap for "largest
        disagreement first", -1.0 lands after every real (non-negative) gap,
        so a no-date keeper sorts last instead of first.
        """
        s = e["set"]
        keeper = next((m for m in e["members"] if m["uuid"] == s["keeper_uuid"]), None)
        if keeper is None or not keeper["photos_date"] or not s["date_value"]:
            return -1.0
        return abs(
            (datetime.fromisoformat(s["date_value"]) - datetime.fromisoformat(keeper["photos_date"])).total_seconds()
        )

    samples: list[tuple[str, str, list[dict]]] = [
        ("dates", "Dates rescued (largest keeper disagreement first)",
         sorted([e for e in exact if e["set"]["date_changes"]], key=lambda e: (-keeper_gap(e), e["set"]["id"]))[:sample]),
        ("largest", "Largest exact sets",
         sorted(exact, key=lambda e: (-e["set"]["size"], e["set"]["id"]))[:sample]),
        ("truncated", "Sets with a truncated member",
         [e for e in entries if has_flag(e, "truncated_member")][:sample]),
        ("size_spread", "Exact sets whose intact members differ by 1 MB or more",
         sorted([e for e in exact if has_flag(e, "size_spread")], key=lambda e: -spread(e))[:sample]),
        ("partial", "Sets with a partially rendered member (fewest blank rows wins)",
         [e for e in entries if has_flag(e, "partial_render")][:sample]),
        ("blank", "Sets of flat, blank images (offer for deletion in Phase 4)",
         [e for e in entries if has_flag(e, "blank_image")][:sample]),
        ("keeper_smaller", "Keeper smaller than an intact loser (must be empty)",
         [e for e in entries if has_flag(e, "keeper_smaller")]),
        ("candidate", "Candidate-only sets, closest phash first",
         candidate_sorted[:sample]),
    ]
    for key, title, items in samples:
        body = "<nav><a href='../index.html'>index</a></nav>" + (
            "".join(
                _render_set(e, thumbs_dir, depth=1, counters=counters, margin=margin)
                for e in items
            )
            or "<p>none</p>"
        )
        (out_dir / "samples" / f"{key}.html").write_text(_page(title, body, depth=1))
        pages += 1

    flag_counts: dict[str, int] = {}
    for e in entries:
        for f in e["set"]["review_flags"].split(","):
            if f:
                flag_counts[f] = flag_counts.get(f, 0) + 1
    resolvable = sum(1 for e in entries if e["set"]["resolvable"])
    rescued = sum(1 for e in entries if e["set"]["date_changes"])
    exact_assets = sum(e["set"]["size"] for e in exact)
    # Removable is the loser count rather than assets minus sets: an edited
    # copy stays as its own photo and is never a loser.
    removable = conn.execute(
        """
        SELECT COUNT(*) AS n FROM decision_loser l
        JOIN dup_set s ON s.id = l.set_id WHERE s.tier = 'exact'
        """
    ).fetchone()["n"]
    damaged = conn.execute(
        """
        SELECT COUNT(*) AS n FROM asset_hash h
        WHERE h.status = 'truncated'
          AND NOT EXISTS (SELECT 1 FROM dup_set_member m WHERE m.uuid = h.uuid AND m.tier = 'exact')
        """
    ).fetchone()["n"]

    keeper_smaller = flag_counts.get("keeper_smaller", 0)
    totals = [
        ("sets", f"{len(entries)} sets ({len(exact)} exact, {len(candidate)} candidate)"),
        ("exact assets", f"{exact_assets} assets in exact sets, {removable} removable"),
        ("resolvable", f"{resolvable} resolvable"),
        ("dates rescued", f"{rescued} dates rescued"),
        ("keeper smaller than an intact loser", f"{keeper_smaller}, as required" if keeper_smaller == 0 else f"{keeper_smaller} — BUG, do not proceed"),
        ("unique damaged files", f"{damaged} truncated files with no intact twin"),
    ] + [(f"flag {k}", str(v)) for k, v in sorted(flag_counts.items())]
    rows = "".join(f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in totals)
    sample_links = "".join(
        f"<li><a href='samples/{_e(key)}.html'>{_e(title)}</a> ({len(items)})</li>"
        for key, title, items in samples
    )
    exact_pages = len(_paginate(exact, per_page))
    exact_links = " ".join(f"<a href='exact/{i:03d}.html'>{i}</a>" for i in range(1, exact_pages + 1))
    index = (
        f"<table class='totals'>{rows}</table>"
        f"<h2>Samples</h2><ul>{sample_links}</ul>"
        f"<h2>All exact sets</h2><p>{exact_links}</p>"
        f"<h2>Candidate sets (first {sample})</h2><p><a href='candidate/001.html'>open</a></p>"
    )
    (out_dir / "index.html").write_text(_page("Duplicate review", index, depth=0))
    pages += 1

    result = ReviewResult(
        len(entries), pages, len(counters["thumbs"]), len(counters["placeholders"])
    )
    set_stage(
        conn, "review", "complete",
        notes=f"{result.sets} sets on {result.pages} pages, {result.thumbs} thumbnails",
    )
    return result
