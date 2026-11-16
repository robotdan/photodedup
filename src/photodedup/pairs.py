"""Resolve hand-picked duplicate pairs without Photos' own merge.

Each row of the input CSV names a `keeper` and a `loser`. The loser's albums,
keywords, favorite flag and captions are copied onto the keeper and read back;
only once the keeper is confirmed is the loser put in the staging album for the
user to delete by hand. Nothing is deleted. The catalog is not touched: these
pairs come from photos imported after the survey, which it does not know.
"""

from __future__ import annotations

import csv
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
    seen: set[str] = set()
    for p in pairs:
        if p.keeper == p.loser:
            raise ValueError(f"{p.keeper} is both keeper and loser")
        for u in (p.keeper, p.loser):
            if u in seen:
                raise ValueError(f"{u} appears in more than one pair")
            seen.add(u)
    return pairs


def resolve_pairs(writer: PhotosWriter, pairs: list[Pair], *, dry_run: bool = False) -> list[PairResult]:
    results = []
    staging = None
    for pair in pairs:
        if not (writer.exists(pair.keeper) and writer.exists(pair.loser)):
            results.append(PairResult(pair, "skipped", "keeper or loser no longer in the library"))
            continue
        keeper, loser = writer.get(pair.keeper), writer.get(pair.loser)
        if PAIRS_ALBUM in keeper.albums:
            results.append(PairResult(pair, "failed", "keeper is in the staging album; fix that by hand first"))
            continue
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
            results.append(PairResult(pair, "skipped", f"dry run; {detail}"))
            continue
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
            after = writer.get(pair.keeper)
            missing = [a for a in albums if a not in after.albums] + [k for k in keywords if k not in after.keywords]
            if loser.favorite and not after.favorite:
                missing.append("favorite")
            if missing:
                results.append(PairResult(pair, "failed", f"keeper did not take {missing}; loser not staged"))
                continue
            if staging is None:
                staging = writer.ensure_album(PAIRS_ALBUM, FOLDER)
            writer.add_to_album(staging, [pair.loser])
            if PAIRS_ALBUM not in writer.get(pair.loser).albums:
                results.append(PairResult(pair, "failed", f"{detail}; loser did not land in {PAIRS_ALBUM}"))
                continue
            results.append(PairResult(pair, "staged", detail))
        except Exception as exc:
            results.append(PairResult(pair, "failed", f"{type(exc).__name__}: {exc}"))
    return results
