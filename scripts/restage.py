"""Put a list of photos back in the pairs staging album.

    uv run python scripts/restage.py reports/restage_2026-11-20.txt KEEPERS.csv...

Refuses any uuid that is a keeper in one of the given pairs CSVs, adds the rest
in batches, then counts the album from Photos.sqlite.
"""

import csv
import sys
from pathlib import Path

from photodedup.pairs import FOLDER, PAIRS_ALBUM
from photodedup.photos_write import DEFAULT_LIBRARY, LibraryIndex, PhotoscriptWriter

# One AppleScript call must finish inside Apple's 2-minute event timeout; an
# add costs about a second a photo in this library.
BATCH = 10

ids = [line.strip() for line in Path(sys.argv[1]).read_text().splitlines() if line.strip()]
keepers = {r["keeper"] for f in sys.argv[2:] for r in csv.DictReader(open(f))}
if bad := [u for u in ids if u in keepers]:
    sys.exit(f"refusing: {len(bad)} uuids are keepers, e.g. {bad[0]}")

index = LibraryIndex(DEFAULT_LIBRARY)
todo = [u for u in ids if index.exists(u) and PAIRS_ALBUM not in index.albums(u)]
print(f"{len(ids)} listed, {len(todo)} to add")
writer = PhotoscriptWriter(DEFAULT_LIBRARY)
# photoscript answers a timeout by force-quitting Photos and retrying; a slow
# batch should just be reported and left for a rerun.
from photoscript.script_loader import RUNSCRIPT_CONFIG  # noqa: E402

RUNSCRIPT_CONFIG["retry_enabled"] = False
album = writer.ensure_album(PAIRS_ALBUM, FOLDER)
failed = 0
for i in range(0, len(todo), BATCH):
    try:
        writer.add_to_album(album, todo[i:i + BATCH])
    except Exception as exc:  # one slow batch should not end the run
        failed += 1
        print(f"  batch at {i} failed: {exc}", flush=True)
    if (i // BATCH) % 10 == 9 or i + BATCH >= len(todo):
        print(f"  {min(i + BATCH, len(todo))}/{len(todo)}", flush=True)
if failed:
    print(f"{failed} batches failed; rerun to add what is missing")

index = LibraryIndex(DEFAULT_LIBRARY)
inside = sum(PAIRS_ALBUM in index.albums(u) for u in ids)
print(f"{inside} of {len(ids)} now in '{PAIRS_ALBUM}'")
