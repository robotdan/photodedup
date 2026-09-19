# Photo Library De-duplication — Design

Date: 2026-09-18
Status: Draft for review

## Problem

The Apple Photos library at `~/Pictures/Photos Library.photoslibrary` (329 GB)
contains a large number of duplicate assets. The duplicates came from two
imports of the same Google Photos Takeout:

- **Cohort A** — an early import where Google's per-file `.json` sidecars were
  ignored. Capture dates collapsed to the import moment. Example: a photo that
  was taken 2003-02-23 09:08:08 shows 2020-12-18 09:12:48.
- **Cohort B** — a later re-import through a process that preserved metadata.
  These carry the true capture date.

Apple Photos' own duplicate detection finds many of these pairs but does not
reliably keep the copy with the better metadata, and it gives no explanation
for its choices.

Goal: remove the duplicates while keeping the most accurate metadata, with an
auditable record of every decision.

## Constraints and environment

- macOS, Darwin 25.6. Full Disk Access is granted to the terminal.
- iCloud Photos is enabled. Originals are being downloaded locally; the
  pipeline must not run until that completes.
- Library: 329 GB. Boot volume free: 448 GB.
- Archive destination: `/Volumes/Extreme Pro`, 1 TB, APFS.
- Available tooling: `exiftool`, `ffmpeg`, `osxphotos` 0.76.1, Python 3.13.
- The library is live. New photos will be added while this work is in progress.
- The work will be paused and resumed, possibly by a different agent session.

## Non-goals

- Perceptual de-duplication of visually similar but distinct photos.
- Reorganising, retitling, or re-albuming the library beyond duplicate removal.
- Any automatic deletion. Deletion is always performed by the user.

## Key decisions

### Operate out-of-place first

The pipeline never mutates the Photos library except in an explicitly invoked
final stage. It reads the library, builds a clean de-duplicated archive on the
external drive, and only then offers to apply its decisions back.

The archive is not re-imported into Photos. Re-importing would create new
assets and discard edits, faces, and albums. The archive serves as the verified
answer key and as a full backup. Merge-back applies decisions to the existing
assets in place.

### Live-library cutoff

The `survey` stage records a watermark equal to the maximum `date_added` across
all assets at survey time, and stores it in the catalog. Every later stage
filters to assets at or below the watermark. Photos added after the survey are
invisible to the run and are simply left alone. A later run can be started with
a new watermark.

### Duplicate matching ladder

Three signals, with different levels of authority:

1. **JPEG scan-data hash.** A JPEG is metadata segments followed by
   entropy-coded scan data. Rewriting EXIF changes the metadata segments and
   leaves the scan data byte-identical. Hashing only the scan data therefore
   identifies cohort A / cohort B pairs exactly, with no decoding, at
   roughly the speed of reading the file. This is the primary signal.
2. **Decoded-pixel hash.** SHA-256 over decoded RGB bytes, for formats where
   the scan-data trick does not apply (HEIC, PNG, TIFF) and for JPEGs whose
   containers were rewritten. Metadata-blind and exact.
3. **Perceptual hash.** dhash and phash, for re-encoded or resized copies.
   Advisory only.

A file-level MD5 or SHA of the whole file is deliberately *not* used as a
primary signal. Cohort B files have EXIF written into them, so whole-file
hashes differ across exactly the pairs that matter.

For video, the same principle applies via `ffmpeg`: hash the video elementary
stream while ignoring container metadata atoms, combined with duration and
dimensions.

Hard partitions that are never crossed regardless of hash agreement:

- RAW never groups with a non-RAW file.
- Video never groups with a still.
- Live Photos are keyed on the still image; the paired movie inherits the
  still's disposition so the pairing cannot break.
- Edited assets are compared on their *original*; the rendered edit travels
  with the keeper.

Apple's own Duplicates list is read if it is reachable from the Photos database
and used as a fourth, advisory signal. It is never authoritative.

### Automatic resolution policy

A group is resolved automatically only when signal 1 or signal 2 says the
images are identical. Groups that exist only because of perceptual similarity
or only because Apple flagged them are staged for human review and are never
acted on automatically.

### Capture date arbitration

Candidate sources, in descending trust:

1. Google Takeout `photoTakenTime` from a `.json` sidecar, if the Takeout
   files are located.
2. EXIF `DateTimeOriginal` / `SubSecDateTimeOriginal`, or the QuickTime
   creation date for video.
3. A date parsed from the filename (`IMG_20140612_...`, `PXL_...`,
   `Screenshot_...`, `VID_...`).
4. The Photos database date.
5. File modification time.

Every candidate must pass plausibility checks before it can compete. A
candidate is rejected if it is:

- at or after the asset's `date_added` — a capture always predates its own
  import, and this single rule is expected to eliminate the cohort A dates
  without any knowledge of which batch they came from;
- in the future;
- a known sentinel: the Unix epoch or within a day of it, the QuickTime 1904
  epoch, the FAT 1980 epoch, Apple's 2001-01-01 reference epoch, a zeroed
  EXIF field (`0000:00:00`), or `1899-12-30`;
- exactly equal to the file's modification time;
- before a plausibility floor established from the survey.

Among surviving candidates the **earliest wins**, on the reasoning that import
and copy events only ever push dates forward. Source rank breaks ties and is
recorded for audit. Any group whose surviving candidates disagree by more than
a configurable margin is flagged for review rather than silently resolved.

The final sentinel list and the plausibility floor are set from the survey's
findings, not guessed in advance.

### Other metadata

- Empty versus non-empty: take the non-empty value.
- Keywords, albums: union.
- Favorite: logical OR.
- Title, caption, description: on genuine conflict take the longer value, after
  discarding values that are merely the filename echoed back, which Google
  exports produce constantly.
- GPS: no merging. Apply the same trust ladder as dates.

Every resolved field records the chosen value, its source, the rejected
alternatives, and a confidence.

## Architecture

A single SQLite catalog is the spine. Each stage reads it, writes its own
columns, and records per-asset status. Every stage is idempotent and resumable:
re-running it processes only rows that are not yet complete. A crash at asset
80,000 costs nothing.

```
survey → hash → group → arbitrate → report → archive → [apply] → verify
```

### `survey` (read-only)

Reads the library through osxphotos and runs `exiftool` in batch mode over the
originals. Writes one row per asset: UUID, filename, UTI, dimensions, file
size, `date_added`, import session, album membership, existing Photos
metadata, and every candidate date with its source. Records the cutoff
watermark.

Produces a survey report covering asset counts by type, the distribution of
`date_added` (where import spikes appear as impossible clusters of hundreds or
thousands of assets sharing a single afternoon), how often EXIF and database
dates disagree, which sentinel values actually occur, and how many assets are
missing originals.

This report answers questions that were deliberately left open in this design.
No arbitration rule is finalised until it has been read.

### `hash`

Computes the scan-data hash, decoded-pixel hash, dhash, phash, and video
stream hash. Parallel across cores, checkpointing to the catalog in batches.

### `group`

Forms duplicate sets from the hash signals plus Apple's list, applying the hard
partitions. Assigns each group a confidence tier: `exact` (auto-resolvable) or
`candidate` (review only).

### `arbitrate`

Selects a winner per group and resolves each field per the rules above. Writes
decisions and reasoning to the catalog. Purely a function of catalog state,
so it can be re-run with changed rules at no cost.

### `report`

Paginated static HTML, a few hundred groups per page with a static index.
Thumbnails written as small JPEGs on disk and lazy-loaded rather than inlined,
to keep the DOM manageable. Per-field colour coding for agreement, conflict,
and missing, with the winning value marked and its source named.

Also offers a sampling mode that selects a few hundred deliberately hard groups
— wide date disagreement, low confidence, perceptual-only matches — for
calibration. The intent is that the user reads the sample to decide whether to
trust the rules, not that the user reviews every group.

### `archive`

Writes winners to `/Volumes/Extreme Pro` with resolved metadata stamped into
the files via `exiftool`, preserving album structure as folders. Writes a
manifest linking every archived file to its catalog row. Verifies each written
file by re-reading it before marking the row complete.

### `apply` (destructive, separately invoked)

Runs only after the archive exists and has been verified. It performs no
deletion itself. AppleScript cannot delete media items from Photos, which is a
limitation worth keeping: the irreversible step stays under human control.

**Staging the losers.** `apply` creates a Photos folder named `Dedup Delete`
containing numbered albums of at most 500 assets each: `Dedup Delete 001`,
`Dedup Delete 002`, and so on. Batching matters — a single album holding tens
of thousands of assets is slow to open and awkward to select in. Each album is
filled with the losing assets from resolved groups only. Nothing from a
`candidate` tier group is ever staged without explicit review.

Alongside the albums, `apply` writes `losers.csv` to the project root: one row
per staged asset with its UUID, filename, capture date, the UUID of the keeper
that replaced it, the group's match signal, and the archive path of the keeper.
This is the record that makes the deletion auditable and recoverable.

**Deleting.** The user opens each album in Photos, selects all, and deletes.
Deleted assets go to Recently Deleted and remain recoverable there for 30 days,
which is the real safety net for this step. Deletion propagates to all devices
through iCloud, so the archive must be complete and verified beforehand.

**Correcting keepers.** Capture dates on keepers that need fixing are set via
`osxphotos timewarp`, driven from the catalog. This runs independently of the
deletion and can be done before or after it.

### `verify` (read-only, run after deletion)

Re-reads the library and checks the outcome against the catalog: every UUID in
`losers.csv` should be absent, every keeper should still be present, and every
corrected keeper should show its resolved date. Reports survivors, missing
keepers, and dates that did not take. Safe to run repeatedly, including partway
through deletion, so progress through the albums can be tracked.

If the deletion mechanism proves unworkable — for instance if the AppleScript
path in spike 1 turns out to be genuinely broken on this macOS version — the
correct response is to stop and reassess rather than improvise. Without the
ability to create and populate an album, there is no acceptable way to hand a
list of tens of thousands of assets to a human for deletion, and spike 1
therefore gates this entire stage.

## Pause, resume, and handoff

All durable state lives in the catalog and in a `STATE.md` at the project root.
`STATE.md` records the current stage, the cutoff watermark, the catalog path,
the archive path, decisions made and why, open questions, and the next
concrete action. It is updated at the end of every working session and
committed.

Every stage prints resumable progress and can be interrupted safely. A new
session or a different agent should be able to read `STATE.md` plus this design
and continue without re-deriving anything.

The project is a git repository. The catalog is not committed; `STATE.md`, the
code, and the survey reports are.

## Error handling

No stage aborts on a per-asset failure. Failures are recorded as a row status
with a reason and excluded from downstream stages. Each stage reports failure
counts at completion. A corrupt file becomes a row with a null hash and a
reason. Missing originals, which iCloud optimised storage can produce, are
detected in `survey` and block the run with a clear message rather than being
silently hashed from derivatives.

## Testing

A synthetic fixture library built from a small set of images deliberately
mangled to reproduce each case: identical pixels with differing EXIF, a
re-encoded copy, sentinel dates, absent dates, a RAW and JPEG pair, a Live
Photo, a video, and a corrupt file. Arbitration rules are unit-tested as pure
functions over candidate sets, with no filesystem involved.

## Spikes, before implementation planning

1. **osxphotos viability.** `osxphotos query --count` currently fails with an
   AppleScript compile error originating in `photoscript`, which osxphotos
   imports unconditionally. The failure appears to be a terminology-lookup
   failure rather than a genuine incompatibility, and is probably an artifact
   of a sandboxed shell. Confirm by running it in a normal terminal. If it
   genuinely fails, the fallback is to read `Photos.sqlite` directly for the
   read stages and to find another mechanism for `apply`.
2. **`osxphotos timewarp`.** Verify it can set the capture date on this macOS
   version, on a handful of disposable test photos, before the design depends
   on it.
3. **Apple's Duplicates list.** Determine whether it is readable from the
   Photos database. If not, drop it as a signal; nothing depends on it.
4. **Takeout sidecars.** The original Takeout files may be on a flash drive.
   If found, they materially improve outcomes for cohort A assets that have no
   duplicate twin. The pipeline does not block on this.

## Survey findings (2026-09-19) — amends the assumptions above

Phase 1 ran against the live library. These measurements correct or confirm
assumptions this design was written on, and Phase 2 must be built on them
rather than on the original text.

**The scale of the damage is far smaller than assumed.** Clustering the
CAPTURE date, which is the measurement that isolates collapsed dates,
finds exactly two affected batches in 65,974 assets: 403 assets stamped
2020-12-18 09:00 and 226 stamped 2023-01-14 11:00. 629 photos in total.
The December 2020 import moved roughly 34,000 assets and the large
majority kept their true dates. Clustering `date_added` instead, as
originally proposed, marks every bulk import including the good ones and
does not isolate anything.

**The `at_or_after_date_added` rule does not do what this design claims.**
It was described as eliminating the cohort A dates on its own. It rejects
4,611 of 65,974 Photos-database candidates, and only 22 of the 403 assets
in the 2020 collapsed batch. The bad import stamped capture dates at
09:10-09:24 while the rows finished being added between 11:53 and 23:00,
so the wrong date precedes its own `date_added` and survives the filter.
The rule is still correct where it fires. It is not a cohort detector.

**Recovery is mostly impossible for the affected photos.** Of the 403
assets in the 2020 batch, 31 carry a usable EXIF date. Of the 226 in the
2023 batch, none do. Neither batch has any usable filename date, and all
of them have local originals, so this is not a download gap. With the
Google Takeout archives gone, roughly 598 photos have no mechanical
recovery path and will keep a wrong date unless identified by hand.

**The library is otherwise healthy.** 56,833 assets carry a usable EXIF
capture date. Only 115 assets have a real conflict, where EXIF and the
Photos date are both usable and differ by more than 26 hours. 19,352
differ by timezone-sized amounts, which is a rendering artifact rather
than corruption; 679 assets have a capture date later than their own
`date_added` by under 24 hours for the same reason. 4,633 assets have
neither source usable.

**`takeout_json` is a dead source.** The user confirmed the original
Takeout archives no longer exist. The rung remains in `SOURCE_RANK` and
in the schema because it is the correct top of the trust ladder in
principle, but no candidate will ever be written for it.

**Duplicate volume, as a floor.** 16,712 groups share an exact file size
and pixel dimensions, covering 33,513 assets, implying at least 16,801
removable duplicates. This is a floor rather than an estimate: any pair
whose EXIF was rewritten differs in byte size and cannot appear in it.
Separately, 4,528 filenames carry Google's `(N)` disambiguation suffix.

**Hashing workload.** 57,114 JPEG (132.9 GB) and 6,678 HEIC (14.8 GB)
dominate by count; 1,719 videos dominate by size at 141.6 GB. 1,875
assets have no local original and cannot be hashed at all — all of them
were added in 2025 or 2026, none belong to either collapsed batch.

**Apple's duplicate detection is readable.** `ZASSET.ZDUPLICATEMETADATA
MATCHINGALBUM` and `ZASSET.ZDUPLICATEPERCEPTUALMATCHINGALBUM` act as
group keys, with perceptual fingerprints in `ZSCENEPRINT.ZDUPLICATE
MATCHINGDATA`. Undocumented internal schema, so advisory only.

**A new recovery technique, not in the original design.** A camera whose
clock resets returns to its epoch and keeps counting, so within such a
run the intervals between photos stay accurate and only the origin is
wrong. This library contains one: 73 photos on the 2001 epoch from a
Fujifilm FinePix2800ZOOM, which the user identified as Chicago on 3
August 2002. Runs like this are correctable by shifting every member by
one constant offset derived from a single anchor. Phase 2 should detect
them: a contiguous run from one camera model starting at a known sentinel
epoch with monotonically increasing timestamps, bracketed by correctly
dated photos from the same camera.

### Motion photos (found in Phase 2 review)

358 `MVIMG_*` files exist in this library. A Google motion photo is a JPEG
with an MP4 appended after the EOI marker. `jpeg_scan_hash` stops at EOI by
design, so a motion photo and a copy whose trailer has been stripped are
exact-identical by scan hash. That is correct for GROUPING — the photograph
really is the same photograph — and it is exactly wrong for ARBITRATION,
because one member still carries a video and the other has lost it.

**The arbitration rule for Phase 3: within an exact group, prefer the member
with the larger `file_size`; never delete the larger member in favour of a
smaller one.** The duplicate report surfaces these as "exact groups whose
members differ by >=1 MB" and prints a per-group size spread under each
sample group, so a group where the rule matters is visible before anything
is deleted.

`pixel_hashes` collapses alpha via `convert("RGB")`, so a fully transparent
PNG and an opaque one of the same RGB hash identically; acceptable for
photographs, and PNG groups should be reviewed rather than auto-resolved.

Related: animated GIF and WebP are excluded from the `exact` tier entirely
(`MULTIFRAME_UTIS` in `grouping.py`), because `pixel_hashes` decodes frame 0
only and two different animations can share a first frame. They still form
`candidate` groups for human review.

## Risks

- iCloud originals not fully downloaded at run time would cause hashing of
  derivatives. Mitigated by an explicit precondition check in `survey`.
- Deletion in Photos propagates to all devices via iCloud. Mitigated by the
  archive existing and being verified first, and by deletion being manual.
- `osxphotos` breakage on this macOS version, per spike 1.
- Perceptual matching producing false groupings. Mitigated by never
  auto-resolving on perceptual evidence alone.
