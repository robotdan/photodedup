# photodedup

Tooling to de-duplicate an Apple Photos library while keeping the most
accurate metadata. Built for a library that was imported twice from a Google
Photos Takeout, once with capture dates lost and once with them preserved, so
the same photos exist as pairs whose bytes differ only in EXIF.

Every command is read-only against the Photos library. The tool writes only
to `./catalog.sqlite` (a derived SQLite database, gitignored) and `./reports/`.
Nothing here deletes, moves, or edits a photo. That comes in a later phase and
will stay a human-driven step in the Photos app.

## Status

Phase 1 (survey) and Phase 2 (content hashing and duplicate grouping) are
complete. Phase 3 (deciding which copy to keep, a review UI, an archive, and
staging deletions) is not started. `STATE.md` is the authoritative record of
where things stand and how to resume; read it before doing anything.

Design and plans live under `docs/superpowers/`.

## Requirements

- macOS with the Photos library at `~/Pictures/Photos Library.photoslibrary`
- Full Disk Access granted to your terminal app (System Settings → Privacy &
  Security → Full Disk Access), then relaunch the terminal
- [uv](https://docs.astral.sh/uv/), `exiftool`, and `ffmpeg` (`brew install
  uv exiftool ffmpeg`)
- Python 3.13 (uv installs it)

If iCloud Photos is on, enable "Download Originals to this Mac" in Photos
settings and let it finish; assets without a local original cannot be hashed.

## Running

Always from the repository root, through `uv run`:

```
uv sync
uv run photodedup --help

uv run photodedup survey --missing-tolerance 0.05   # read the library into the catalog (~5 min)
uv run photodedup report                            # write reports/survey.md
uv run photodedup hash                              # content hashes, parallel, resumable (~15 min)
uv run photodedup group                             # form duplicate groups
uv run photodedup duplicates                        # write reports/duplicates.md
```

Every stage is idempotent and resumable: interrupt it and re-run, and it
continues from where it stopped. Run the stages in that order; `group`
refuses to run until `hash` reports complete.

`--missing-tolerance` is the fraction of assets allowed to lack a local
original before `survey` refuses to run. The default is 1%. Raise it only
once you know the missing assets do not matter for your question.

## How it decides two files are the same photo

Three content signals, never a whole-file checksum (which would miss every
pair whose EXIF was rewritten):

1. **JPEG scan-data hash** — hashes the compressed image data while skipping
   every metadata segment, so two JPEGs holding the same image with different
   EXIF match exactly, with no decoding.
2. **Decoded-pixel hash** — SHA-256 over decoded RGB with dimensions mixed in,
   after normalising EXIF orientation. Covers HEIC, PNG, and container rewrites.
3. **Video stream hash** — `ffmpeg -map 0:v -c copy -f md5`, hashing the
   encoded picture stream while ignoring container metadata and audio.

Groups from those are `exact` tier and are the only ones a later phase may
resolve automatically. A fourth signal, a perceptual dhash, forms
`candidate` groups that are review-only. Hard partitions are never crossed:
RAW never groups with non-RAW, video never with image, and animated GIF/WebP
never enter the exact tier (their pixel hash sees only frame 0).

## Tests

```
uv run pytest
```

Tests build their own images and catalogs under a temp directory and never
touch the real library.

## Layout

```
src/photodedup/
  catalog.py     SQLite schema and accessors; the spine every stage shares
  library.py     read-only adapter over the Photos library (osxphotos)
  exif.py        batched exiftool reader
  dates.py       capture-date parsing, plausibility rules, arbitration
  survey.py      survey stage
  hashing.py     the three content hashes plus perceptual hashes
  hash_stage.py  parallel, resumable hash stage
  grouping.py    duplicate grouping under hard partitions
  report.py      Markdown reports
  cli.py         click entry point
```
