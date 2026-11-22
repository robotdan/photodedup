# photodedup

Finds and removes duplicate photos in an Apple Photos library while keeping
the best copy and its metadata. Every copy it removes is first archived to an
external drive and verified by SHA-256. It never deletes anything itself: it
puts the copies to remove in albums, and you delete them in Photos.

It was built for a library assembled from Google Photos Takeout imports. Some
copies lost their capture dates, some came in a timezone off, and Google's
`-edited` files sat beside their originals. The exact-duplicate workflow
works on any library. The same-shot workflow looks for patterns those imports
leave behind, so it finds little in a library that never went through a
Takeout.

## Requirements

- macOS with Apple Photos. The library defaults to
  `~/Pictures/Photos Library.photoslibrary`; `survey --library` takes another.
- Full Disk Access for your terminal app (System Settings > Privacy &
  Security > Full Disk Access), then relaunch the terminal.
- [uv](https://docs.astral.sh/uv/), `exiftool` and `ffmpeg`:
  `brew install uv exiftool ffmpeg`. uv installs Python 3.13.
- An external drive for the archive, with room for every copy removed.
- With iCloud Photos on, turn on "Download Originals to this Mac" and let it
  finish. A photo with no local original can't be hashed.

Tell the archive commands where the archive goes, once per shell:

```
export PHOTODEDUP_ARCHIVE="/Volumes/<your drive>/photodedup"
```

or pass `--root` to `archive`, `archive-pairs` and `review-page`. There is no
default. The tool refuses a path under `/Volumes` whose drive isn't mounted.

## Where things run

Run everything from the repository root through `uv run`. Commands that read
the library work from any shell. The ones that change Photos (`apply` without
`--dry-run`, and `pairs`) drive Photos through AppleScript, so they have to run
in your own Terminal session on the Mac that owns the library. Keep Photos
open and in front while they run: when it's in the background it sometimes
waits for attention (its Dock icon bounces) and the run stalls until you
switch to it.

The tool writes to its catalog (`catalog.sqlite` by default, a SQLite file
you can pass with `--catalog`), to `reports/`, to the archive, and to
`losers.csv`. All of those are gitignored. After `archive` has run, the
catalog records what's on the drive and can't be rebuilt from the library, so
copy it somewhere safe (`backups/` is ignored too).

Every stage is resumable. Interrupt it, run it again, and it continues.

## Workflow 1: exact duplicates

Files holding the same picture, even when their metadata differs.

```
uv run photodedup survey                 # read the library into the catalog (~5 min for 50k photos)
uv run photodedup report                 # reports/survey.md
uv run photodedup hash                   # content hashes, parallel (~15 min)
uv run photodedup group                  # duplicate groups and sets
uv run photodedup duplicates             # reports/duplicates.md
uv run photodedup arbitrate              # pick each set's keeper and capture date
uv run photodedup review                 # reports/review/index.html, check it in a browser
uv run photodedup archive --limit 20     # trial copy
uv run photodedup archive                # rerun until 0 mismatched, 0 missing, stage complete
```

Then, in your own terminal:

```
uv run photodedup apply --self-test <uuid of a photo you don't care about>
uv run photodedup apply                  # prints the plan, writes nothing
uv run photodedup apply --dry-run --all
uv run photodedup apply --merge          # albums, keywords, favorite, captions onto keepers
uv run photodedup apply --dates          # correct keeper capture dates
uv run photodedup verify                 # reports/verify.md: 0 keepers missing, 0 dates wrong
uv run photodedup apply --stage          # losers into albums under the "Dedup Delete" folder
uv run photodedup verify
```

`survey --missing-tolerance` is the fraction of photos allowed to lack a local
original before survey refuses to run (default 1%). `apply --dates` leaves
sets whose dates disagree unless you add `--include-disagreeing-dates`, after
sampling them in `reports/review/samples/dates.html`.

## Workflow 2: one shot saved more than once

Copies that aren't byte-identical but are the same shot: the same filename
stem (ignoring `-edited` and `(1)`) and the same capture time, give or take
whole hours. This needs a fresh survey after workflow 1, into its own catalog:

```
uv run photodedup survey --catalog catalog_new.sqlite
uv run photodedup hash --catalog catalog_new.sqlite
uv run photodedup group --catalog catalog_new.sqlite
uv run photodedup plan-pairs --catalog catalog_new.sqlite \
    --out reports/pairs.csv --dates-out reports/dates.csv --review-out reports/pairs_review.csv
uv run photodedup archive-pairs reports/pairs.csv --catalog catalog_new.sqlite
```

The keeper is the copy Google didn't edit, then the largest, then the one with
the right date. Where Google's `-edited` copy is the loser, `archive-pairs`
also puts both side by side under `<archive>/edits-review/`. To see which
edits change anything visibly and pick per pair:

```
uv run photodedup measure-edits reports/pairs.csv --catalog catalog_new.sqlite --out reports/edit_diffs.csv
uv run photodedup review-page reports/pairs.csv --catalog catalog_new.sqlite --diffs reports/edit_diffs.csv
```

Open `<archive>/edits-review/index.html`, choose original, edit or both for
each pair, and download the choices CSV. With no changes from the defaults,
you don't need the CSV. If you made choices, pass the CSV to `pairs --choices`
and rewrite the dates with `choice-dates`. Then, in your own terminal:

```
uv run photodedup pairs reports/pairs.csv --archived-in catalog_new.sqlite --dry-run
uv run photodedup pairs reports/pairs.csv --archived-in catalog_new.sqlite --log reports/pairs_result.csv
uv run photodedup apply --catalog catalog_new.sqlite --dates-from reports/dates.csv
```

`pairs` copies each loser's albums, keywords, favorite and captions onto its
keeper, confirms they landed, and puts the loser in "Dedup Delete pairs".
`pairs_review.csv` lists the sets it left for you to decide.

## Deleting

Open each album under the "Dedup Delete" folder, check the count, select all,
and press Command+Delete (or Image > Delete N Photos). The dialog must offer
to delete the photos. Inside an album, plain Delete only removes them from
the album and leaves them in the library. Run `verify` afterwards. Deleted
photos stay in Recently Deleted for 30 days. Empty it only once you no longer
want to undo.

## Photos it leaves alone

- Photos in an iCloud Shared Album. Adding one to an album makes Photos import
  a new copy instead, so `pairs` skips them.
- "Shared with You" items saved from Messages. AppleScript can't reach them.
- Photos edited in Apple Photos. That edit exists nowhere else, so such a copy
  is kept alongside the keeper.
- Shared Library copies owned by someone else.

## How it decides two files are the same photo

Three content signals. A whole-file checksum would miss every pair whose
EXIF was rewritten, so it doesn't use one.

1. JPEG scan-data hash: hashes the compressed image data and skips every
   metadata segment, so two JPEGs of the same image with different EXIF match
   without decoding.
2. Decoded-pixel hash: SHA-256 over decoded RGB with the dimensions mixed in,
   after applying EXIF orientation. Covers HEIC, PNG and re-wrapped files.
3. Video stream hash: `ffmpeg -map 0:v -c copy -f md5`, which hashes the
   encoded picture stream and ignores container metadata and audio.

Groups from those are the `exact` tier, the only ones resolved
automatically. A perceptual dhash forms `candidate` groups, which are for
review. RAW never groups with non-RAW, video never with images, and animated
GIF/WebP never enter the exact tier (their pixel hash sees only frame 0).

## Tests

```
uv run pytest
```

Tests build their own images, catalogs and a stand-in Photos.sqlite under a
temp directory and never touch the real library.

## Layout

```
src/photodedup/
  catalog.py      SQLite schema and accessors shared by every stage
  library.py      read-only adapter over the Photos library (osxphotos)
  exif.py         batched exiftool reader
  dates.py        capture-date parsing, plausibility rules, arbitration
  survey.py       survey stage
  hashing.py      the three content hashes plus perceptual hashes
  hash_stage.py   parallel, resumable hash stage
  grouping.py     duplicate grouping under hard partitions
  sets.py         duplicate sets from the groups
  arbitrate.py    keeper and date per set
  archive.py      verified copies to the archive drive
  apply.py        merge, dates and staging through Photos
  verify.py       re-reads the library after writes
  pairplan.py     plan-pairs: same-shot sets
  pairarchive.py  archive-pairs and edits-review copies
  pairs.py        pairs: merge onto keepers and stage losers
  editdiff.py     measure-edits
  reviewpage.py   review-page
  photos_write.py AppleScript writer, with Photos.sqlite for fast reads
  report.py       Markdown reports
  review.py       the static HTML review report
  cli.py          click entry point
one-offs/         scripts written for one cleanup, kept for reference
docs/superpowers/ design specs and plans
STATE.md          working notes from the first cleanup
```

The fast reads in `photos_write.py` depend on Photos.sqlite's internal
schema, which Apple doesn't document and can change with a macOS release. If
`pairs` or `apply` fails on a schema error after an upgrade, that's where to
look.
