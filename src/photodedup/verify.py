"""Re-read the library and report what the apply stage's writes look like now."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection

from photodedup.catalog import get_run, set_stage
from photodedup.library import AssetState, LibraryUnavailable, read_states


@dataclass(frozen=True)
class VerifyResult:
    losers_present: list[tuple[str, str]]     # (uuid, album)
    losers_trashed: list[str]                 # deleted in Photos, still recoverable
    losers_gone: list[str]
    keepers_missing: list[str]
    dates_wrong: list[tuple[str, str, str]]   # (uuid, wanted, found)
    merges_missing: list[tuple[str, str]]     # (uuid, album)
    blank_present: list[str]
    stale_staged: list[tuple[str, str]]       # (uuid, album)


def run_verify(conn: Connection, *, states: Mapping[str, AssetState] | None = None) -> VerifyResult:
    set_stage(conn, "verify", "running")
    staged = {
        r["uuid"]: json.loads(r["detail"]).get("album", "")
        for r in conn.execute("SELECT uuid, detail FROM apply_action WHERE action = 'stage' AND status = 'done'")
    }
    blank = [r["uuid"] for r in conn.execute("SELECT uuid FROM apply_action WHERE action = 'stage_blank' AND status = 'done'")]
    keepers = [
        r["keeper_uuid"] for r in conn.execute(
            "SELECT DISTINCT d.keeper_uuid FROM decision d JOIN dup_set s ON s.id = d.set_id WHERE s.tier = 'exact' AND d.resolvable = 1"
        )
    ]
    current_losers = {
        r["uuid"] for r in conn.execute(
            "SELECT DISTINCT dl.uuid FROM decision_loser dl "
            "JOIN decision d ON d.set_id = dl.set_id "
            "JOIN dup_set s ON s.id = d.set_id "
            "WHERE s.tier = 'exact' AND d.resolvable = 1"
        )
    }
    dates = {
        r["uuid"]: json.loads(r["detail"])["date"]
        for r in conn.execute("SELECT uuid, detail FROM apply_action WHERE action = 'date' AND status = 'done'")
    }
    merges = {
        r["uuid"]: json.loads(r["detail"]).get("albums", [])
        for r in conn.execute("SELECT uuid, detail FROM apply_action WHERE action = 'merge' AND status = 'done'")
    }
    wanted = set(staged) | set(blank) | set(keepers) | set(dates) | set(merges)
    if states is None:
        run_row = get_run(conn)
        if run_row is None:
            raise LibraryUnavailable(
                "This catalog has no run row, so verify does not know which Photos "
                "library to read. Run `photodedup survey` against the library first."
            )
        states = read_states(Path(run_row["library_path"]), sorted(wanted))

    def state(uuid: str) -> AssetState:
        return states.get(uuid, AssetState(False, None, ()))

    # A loser the user has deleted in Photos sits in Recently Deleted for 30
    # days: present() is already False for it, and calling it "gone" would
    # read as success while it is still recoverable, so it gets its own line.
    losers_present = sorted((u, a) for u, a in staged.items() if state(u).present)
    losers_trashed = sorted(u for u in staged if state(u).in_trash)
    losers_gone = sorted(
        u for u in staged if not state(u).present and not state(u).in_trash
    )
    keepers_missing = sorted(u for u in keepers if not state(u).present)
    dates_wrong = []
    for uuid, wanted_iso in sorted(dates.items()):
        s = state(uuid)
        if not s.present:
            continue
        found = s.date
        if found is None or abs((found - datetime.fromisoformat(wanted_iso)).total_seconds()) > 1:
            dates_wrong.append((uuid, wanted_iso, found.isoformat() if found else "missing"))
    merges_missing = sorted(
        (uuid, album) for uuid, albums in merges.items() for album in albums
        if state(uuid).present and album not in state(uuid).albums
    )
    blank_present = sorted(u for u in blank if state(u).present)
    # An arbitrate re-run can flip a staged loser into a keeper (its
    # decision_loser row is gone even though it is still an exact,
    # resolvable set). Such a photo would otherwise sit quietly in a
    # delete album waiting for the user to remove it in Photos, so it is
    # called out on its own rather than folded into losers_present.
    stale_staged = sorted((u, a) for u, a in staged.items() if u not in current_losers)
    result = VerifyResult(
        losers_present, losers_trashed, losers_gone, keepers_missing, dates_wrong,
        merges_missing, blank_present, stale_staged,
    )
    set_stage(
        conn, "verify", "complete",
        notes=f"{len(losers_present)} losers still present, "
              f"{len(losers_trashed)} in Recently Deleted, {len(losers_gone)} gone, "
              f"{len(keepers_missing)} keepers missing, {len(dates_wrong)} dates wrong, "
              f"{len(stale_staged)} stale staged",
    )
    return result


def verify_report(r: VerifyResult) -> str:
    by_album: dict[str, int] = {}
    for _, album in r.losers_present:
        by_album[album] = by_album.get(album, 0) + 1
    lines = [
        "# Verify Report\n",
        f"- Staged losers still in the library: {len(r.losers_present)}",
        f"- Staged losers in Recently Deleted (recoverable for 30 days): {len(r.losers_trashed)}",
        f"- Staged losers now gone: {len(r.losers_gone)}",
        f"- Keepers missing from the library: {len(r.keepers_missing)}"
        + (", as required" if not r.keepers_missing else " — STOP and investigate before deleting anything else"),
        f"- Corrected dates that did not take: {len(r.dates_wrong)}",
        f"- Merged albums the keeper is not in: {len(r.merges_missing)}",
        f"- Blank-image keepers still present: {len(r.blank_present)}",
        f"- Staged photos that are no longer losers: {len(r.stale_staged)}"
        + (", as required" if not r.stale_staged else " — STOP: an arbitrate re-run changed its mind; pull these out of their delete album before removing anything")
        + "\n",
        "## Losers still present, by album\n",
    ]
    for album in sorted(by_album):
        lines.append(f"- {album}: {by_album[album]}")
    if r.keepers_missing:
        lines.append("\n## Missing keepers\n")
        lines.extend(f"- {u}" for u in r.keepers_missing)
    if r.dates_wrong:
        lines.append("\n## Dates that did not take\n")
        lines.extend(f"- {u}: wanted {w}, found {f}" for u, w, f in r.dates_wrong)
    if r.merges_missing:
        lines.append("\n## Merges missing\n")
        lines.extend(f"- {u}: not in album {a}" for u, a in r.merges_missing)
    if r.stale_staged:
        lines.append("\n## Staged but no longer a loser\n")
        lines.extend(f"- {u}: in {a}" for u, a in r.stale_staged)
    return "\n".join(lines) + "\n"
