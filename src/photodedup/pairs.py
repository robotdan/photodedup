"""Resolve hand-picked duplicate pairs without Photos' own merge.

Each row of the input CSV names a `keeper` and a `loser`. The loser's albums,
keywords, favorite flag and captions are copied onto the keeper and read back;
only once the keeper is confirmed is the loser put in the staging album for the
user to delete by hand. Nothing is deleted. The catalog is not touched: these
pairs come from photos imported after the survey, which it does not know.
"""

from __future__ import annotations

import csv
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from photodedup.apply import FOLDER
from photodedup.photos_write import PhotosWriter

PAIRS_ALBUM = f"{FOLDER} pairs"


@dataclass(frozen=True)
class Pair:
    keeper: str
    loser: str


@dataclass(frozen=True)
class PairResult:
    pair: Pair
    status: str          # staged | failed | skipped
    detail: str


def read_pairs(path: Path) -> list[Pair]:
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows or not {"keeper", "loser"} <= set(rows[0]):
        raise ValueError(f"{path} needs a header with keeper and loser columns")
    pairs = [Pair(r["keeper"].strip(), r["loser"].strip()) for r in rows]
    _check(pairs)
    return pairs


def _check(pairs: list[Pair]) -> None:
    # A keeper may absorb several losers (a group of three or more copies), but
    # a loser is deleted once and must never be anyone's keeper.
    keepers = {p.keeper for p in pairs}
    losers: set[str] = set()
    for p in pairs:
        if p.loser in keepers:
            raise ValueError(f"{p.loser} is both keeper and loser")
        if p.loser in losers:
            raise ValueError(f"{p.loser} is a loser in more than one pair")
        losers.add(p.loser)


def apply_choices(pairs: list[Pair], choices: dict[str, str]) -> tuple[list[Pair], list[Pair]]:
    """Apply review choices keyed by the planned loser: "both" keeps the two
    photos, "swap" keeps the planned loser and removes the planned keeper
    instead (and the keeper's other losers go to the new keeper). Returns the
    pairs to resolve and the pairs dropped because both stay."""
    bad = {c for c in choices.values() if c not in ("both", "swap")}
    if bad:
        raise ValueError(f"unknown choice {sorted(bad)}; expected both or swap")
    new_keeper: dict[str, str] = {}
    for p in pairs:
        if choices.get(p.loser) == "swap":
            if p.keeper in new_keeper:
                raise ValueError(f"two swaps for keeper {p.keeper}; pick one")
            new_keeper[p.keeper] = p.loser
    out: list[Pair] = []
    dropped: list[Pair] = []
    for p in pairs:
        if choices.get(p.loser) == "both":
            dropped.append(p)
        elif p.keeper in new_keeper:
            k = new_keeper[p.keeper]
            out.append(Pair(k, p.keeper) if k == p.loser else Pair(k, p.loser))
        else:
            out.append(p)
    _check(out)
    return out, dropped


SETTLE_TRIES = 10
SETTLE_PAUSE = 0.5


def _settled(read: Callable[[], bool], sleep: Callable[[float], None]) -> bool:
    """Photos commits an AppleScript write a moment after it returns; when
    reads come from its database, poll briefly before calling a write lost."""
    for attempt in range(SETTLE_TRIES):
        if read():
            return True
        if attempt < SETTLE_TRIES - 1:
            sleep(SETTLE_PAUSE)
    return False


def resolve_pairs(writer: PhotosWriter, pairs: list[Pair], *, dry_run: bool = False,
                  progress: Callable[[int, int, dict[str, int]], None] | None = None,
                  sleep: Callable[[float], None] = time.sleep) -> list[PairResult]:
    results: list[PairResult] = []
    counts: dict[str, int] = {}
    staging: list[str] = []          # the staging album id, once created
    for index, pair in enumerate(pairs, start=1):
        r = _resolve_one(writer, pair, dry_run, staging, sleep)
        results.append(r)
        counts[r.status] = counts.get(r.status, 0) + 1
        if progress and (index % 25 == 0 or index == len(pairs)):
            progress(index, len(pairs), counts)
    return results


def _resolve_one(writer: PhotosWriter, pair: Pair, dry_run: bool, staging: list[str],
                 sleep: Callable[[float], None]) -> PairResult:
    if not (writer.exists(pair.keeper) and writer.exists(pair.loser)):
        return PairResult(pair, "skipped", "keeper or loser no longer in the library")
    keeper, loser = writer.get(pair.keeper), writer.get(pair.loser)
    if PAIRS_ALBUM in keeper.albums:
        return PairResult(pair, "failed", "keeper is in the staging album; fix that by hand first")
    albums = [a for a in loser.albums if a != PAIRS_ALBUM and a not in keeper.albums]
    keywords = [k for k in loser.keywords if k not in keeper.keywords]
    carries = []
    if albums:
        carries.append(f"albums {albums}")
    if keywords:
        carries.append(f"keywords {keywords}")
    if loser.favorite and not keeper.favorite:
        carries.append("favorite")
    if loser.title and not keeper.title:
        carries.append("title")
    if loser.description and not keeper.description:
        carries.append("description")
    detail = "carries " + ", ".join(carries) if carries else "carries nothing"
    if dry_run:
        return PairResult(pair, "skipped", f"dry run; {detail}")
    try:
        for name in albums:
            writer.add_to_album(writer.ensure_album(name), [pair.keeper])
        if keywords:
            writer.set_keywords(pair.keeper, [*keeper.keywords, *keywords])
        if loser.favorite and not keeper.favorite:
            writer.set_favorite(pair.keeper, True)
        if loser.title and not keeper.title:
            writer.set_title(pair.keeper, loser.title)
        if loser.description and not keeper.description:
            writer.set_description(pair.keeper, loser.description)

        missing: list[str] = []

        def keeper_took_everything() -> bool:
            after = writer.get(pair.keeper)
            missing[:] = [a for a in albums if a not in after.albums] + [k for k in keywords if k not in after.keywords]
            if loser.favorite and not after.favorite:
                missing.append("favorite")
            return not missing

        if not _settled(keeper_took_everything, sleep):
            return PairResult(pair, "failed", f"keeper did not take {missing}; loser not staged")
        if not staging:
            staging.append(writer.ensure_album(PAIRS_ALBUM, FOLDER))
        writer.add_to_album(staging[0], [pair.loser])
        if not _settled(lambda: PAIRS_ALBUM in writer.get(pair.loser).albums, sleep):
            return PairResult(pair, "failed", f"{detail}; loser did not land in {PAIRS_ALBUM}")
        return PairResult(pair, "staged", detail)
    except Exception as exc:
        return PairResult(pair, "failed", f"{type(exc).__name__}: {exc}")


def swap_dates(pairs: list[Pair], choices: dict[str, str], dates: dict[str, str],
               shown: dict[str, str]) -> dict[str, str]:
    """The uuid -> date plan after swaps. A swapped-in keeper takes the time the
    old keeper would end up showing (its planned date, else its current one)
    when its own differs; the old keeper's date is dropped, since it goes."""
    out = dict(dates)
    for p in pairs:
        if choices.get(p.loser) != "swap":
            continue
        target = out.pop(p.keeper, None) or shown.get(p.keeper)
        if target and target[:19] != (shown.get(p.loser) or "")[:19]:
            out[p.loser] = target[:19]
    return out
