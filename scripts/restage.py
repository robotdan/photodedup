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

BATCH = 200

ids = [line.strip() for line in Path(sys.argv[1]).read_text().splitlines() if line.strip()]
keepers = {r["keeper"] for f in sys.argv[2:] for r in csv.DictReader(open(f))}
if bad := [u for u in ids if u in keepers]:
    sys.exit(f"refusing: {len(bad)} uuids are keepers, e.g. {bad[0]}")

index = LibraryIndex(DEFAULT_LIBRARY)
todo = [u for u in ids if index.exists(u) and PAIRS_ALBUM not in index.albums(u)]
print(f"{len(ids)} listed, {len(todo)} to add")
writer = PhotoscriptWriter(DEFAULT_LIBRARY)
album = writer.ensure_album(PAIRS_ALBUM, FOLDER)
for i in range(0, len(todo), BATCH):
    writer.add_to_album(album, todo[i:i + BATCH])
    print(f"  {min(i + BATCH, len(todo))}/{len(todo)}", flush=True)

index = LibraryIndex(DEFAULT_LIBRARY)
inside = sum(PAIRS_ALBUM in index.albums(u) for u in ids)
print(f"{inside} of {len(ids)} now in '{PAIRS_ALBUM}'")
