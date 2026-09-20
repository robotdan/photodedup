# Photo De-duplication — Working State

**Last updated:** 2026-09-19
**Current phase:** Phase 3 (arbitrate, review report) — built, run once, and
amended after the user's sampling. Schema is now v4; the catalog must be
rebuilt before the amended rules are run.
**Branch:** `main`
**Spec:** docs/superpowers/specs/2026-09-18-photo-dedup-design.md (binding authority)
**Phase 2 plan:** docs/superpowers/plans/2026-09-19-photo-dedup-phase2-hash-group.md
**Phase 3 plan:** docs/superpowers/plans/2026-09-19-photo-dedup-phase3-arbitrate-report.md

## The one outstanding action

Rebuild the catalog at schema v4 and run the pipeline through `review`, in
your own terminal (the agent shell cannot start the process pool):

    cd /Users/robotdan/dev/tools
    rm -f catalog.sqlite catalog.sqlite-shm catalog.sqlite-wal
    uv run photodedup survey --missing-tolerance 0.05     # ~5 min
    uv run photodedup hash --workers 4                    # <15 min
    uv run photodedup group
    uv run photodedup duplicates
    uv run photodedup arbitrate
    uv run photodedup review
    open reports/review/index.html

Then record the numbers under "Phase 3 results" and compare them with the
"Expected results on this library" section of the Phase 3 spec. If they
disagree by more than the prefix rule explains, stop and find out why
before sampling.

`group` will read on the order of a gigabyte during the prefix rule because
seven damaged burst frames share their 4048x3036 dimensions with about
1,200 other assets; that is expected, not a hang.

## Paths

- Library: /Users/robotdan/Pictures/Photos Library.photoslibrary (65,974 assets)
- Catalog: ./catalog.sqlite — schema version 2, all 65,974 assets surveyed.
  Gitignored and derived; rebuilding it is the correct answer to a schema
  change, not something to work around.
- Archive: /Volumes/Extreme Pro (unused so far)

## Everything is READ-ONLY

Nothing in Phase 1 or Phase 2 may write to the Photos library, launch
Photos.app, or modify anything inside the `.photoslibrary` bundle. Reading
`Photos.sqlite` directly is fine. No mutating osxphotos call is permitted.

## Progress

**Phase 1 — COMPLETE, and its survey has been run against the live library.**
All ten tasks committed: `8ad073c`, `f651d74`, `47238c3`, `f97df79`,
`aef7767`, `378e1b2`, `0902a2a` (fixed by `4f7c2d9`, `e14cd2a`, `05cae91`),
`3e6f2f1`, `f51c4c1`, review fixes `89a42b2`. The design document is
`d77d805`. `reports/survey.md` holds the results.

**Phase 2 — BUILD COMPLETE at HEAD.** All seven tasks committed:

- [x] Task 1 — `asset_hash`, `dup_group`, `dup_member` at schema v2 — `6de0891`
- [x] Task 2 — `jpeg_scan_hash` — `a34f4b2`, fix `97c72e4`
- [x] Task 3 — `pixel_hashes` (decoded pixels, dhash, phash) — `07478d7`
- [x] Task 4 — `video_stream_hash` — `3fcd683`
- [x] Task 5 — `hash_stage.py` and `photodedup hash` — `4d0c0af`
- [x] Task 6 — `grouping.py` and `photodedup group` — `e9c8d91`
- [x] Task 7 — duplicate report and `photodedup duplicates` — `8393d07`
- [x] Whole-branch review fixes — `a9dbd52`, plus the docs commit that follows it

`uv run pytest -v` passes. Do not re-implement any of it.

**Phase 3 — BUILD COMPLETE at HEAD.** All nine tasks committed:

- [x] Task 1 — schema v3: `dup_set`, `dup_set_member`, `decision`,
  `decision_loser`, `truncated` hash status, `thumb_path`, stage guard —
  `6b8a8c4`
- [x] Task 2 — hash truncated JPEGs leniently and record them as such —
  `9bb23b6`, fix `510328e`
- [x] Task 3 — byte-prefix exact rule linking truncated files to intact
  twins — `9739a04`
- [x] Task 4 — record the Photos thumbnail path per asset — `6dbec0f`
- [x] Task 5 — import-stamp sentinel and pooled date choice as the single
  authority — `7375293`, fix `f2dc19c`
- [x] Task 6 — merge per-signal groups into exact and candidate sets —
  `7423f87`
- [x] Task 7 — arbitrate keeper and capture date per duplicate set —
  `78c534c`, fix `89dedd6`
- [x] Task 8 — static HTML review report over the arbitrate decisions —
  `ad33f31`, fix `be6e50b`
- [x] Task 9 — `sets`, `arbitrate` and `review` commands; this handoff —
  this commit

`uv run pytest -q` passes (296 tests). Not yet run against the real
catalog; see "The one outstanding action" above.

## Phase 2 results (real run, 2026-09-19)

Hash stage: 64,083 assets hashed ok, 1,875 no local original, 16 errors
("no usable hash": neither scan nor pixel hash succeeded — 15 JPEG, 1 PNG;
13 of the JPEGs are a single 2017 burst sequence, plus `Picture 046(9)`,
`047(8)`, `049(5)`, `IMAG0136/0137`, three `IMG_2016*` and one 2013). These
16 are worth opening by hand; they may be corrupt. Single-threaded run took
~75 minutes in the agent shell; in a real terminal with the process pool
expect well under 15.

Grouping (`photodedup group`): 52,323 groups over 106,714 memberships
(per-signal, over-counting). Full report in `reports/duplicates.md`
(gitignored; regenerate with `uv run photodedup duplicates`).

THE NUMBERS THAT MATTER, de-duplicated across signals:

- Exact-tier removable: **16,871** assets, in 16,846 sets of 33,717 assets.
  This is the count a later phase would stage for deletion on exact evidence
  alone. Group sizes: 50,968 pairs, 839 triples, 413 quads, a tail up to 18.
- Including perceptual (dhash) candidates: 21,088 removable. So perceptual
  matching adds 4,217 more removals but 6,437 assets that ONLY perceptual
  matching finds, every one of which needs human review.
- Date-rescuing exact groups: **570** — exact groups whose members disagree
  on capture date by more than 26 hours. These are the cohort A / cohort B
  pairs where de-duplication also recovers a true date.
- Motion-photo size-spread groups (members differing ≥1 MB): **0**. The
  re-import did not strip MP4 trailers, so the Phase 3 keeper rule is a
  safety net rather than a live case here.
- Exact by signal: scan 16,622 groups, pixel 16,635, video 211. Scan and
  pixel agree almost completely (33,295 assets in both), which is the
  expected shape: nearly every duplicate is a JPEG whose scan data survived.

How this compares with the survey's floor. The survey predicted at least
16,801 removable from identical (file_size, width, height) alone; the
content-based exact count is 16,871. The scan hash therefore recovered on
the order of 70 duplicates that byte-level comparison would have split —
consistent with the 67 the Task 2 review measured on the re-imported
cohort. THE PREMISE HELD, BUT THE MARGIN IS SMALL: on this library the
overwhelming majority of duplicates are byte-identical files imported
twice, and the EXIF-rewritten pairs are a thin layer on top. Phase 3 should
not assume otherwise.

Parallel path VERIFIED 2026-09-19: the user ran `hash --workers 4 --limit
2000` on a catalog copy in a real terminal; all 2,000 rows matched the
single-threaded run exactly on every column (status and all hash values,
0 differing). The process-pool branch is safe to use for full runs.

The 16 hash-error files were inspected on 2026-09-19; see "Hash-error
inspection" below. Nothing is outstanding before Phase 3 design.

## Phase 3 results (real run, 2026-09-19)

Catalog rebuilt at schema v3 in the user's terminal with `--workers 4`.
Hash: 64,081 ok, 17 truncated, 1,875 no original, 1 error (the JSON
"PNG"). The 17 truncated are the 15 inspected earlier plus
`IMG_20161104_125602` and `_125607`, both cut at exactly 65,363 bytes like
`_125605` and previously passed as ok because Pillow decoded their zero
padding; they are unique damaged files, not duplicates.

Group: 52,327 groups; 16,849 exact sets, 3,170 candidate sets. Prefix
rule formed exactly 3 groups, the three FUJI pairs. IMAG0136/0137 differ
from their twins in the final byte and the burst cover and 2013 photo are
different encodes, so those four known duplicates are not byte prefixes
and stay outside the exact tier; handle them by hand in Phase 4.

Arbitrate: 20,019 sets, 16,836 resolvable, 3,183 need review, 108 dates
rescued. Keeper reasons: albums 16,943, file_size 2,763, date_added 96,
adjustments 87, caption 71, favorite 52, intact 4, uuid 3. Sets flagged
dates_disagree: 853 (704 exact). The 108 is smaller than the 570 the
Phase 2 report predicted because that figure counted per-signal groups
whose members disagreed, while date_changes counts only sets whose KEEPER
carries the wrong date; in most disagreeing sets the album-bearing
original wins and already has the right date.

Review: 20,019 sets on 77 pages, 34,314 thumbnails, 0 placeholders, at
`reports/review/index.html`. keeper_smaller: 0, as required. Sampling by
the user is the next step; nothing in Phase 4 starts until the sampled
decisions look right.

## Hash-error inspection (2026-09-19)

All 16 `error` rows were opened by hand. Every one is a damaged file, and
the damage has one shape: the 15 JPEGs are truncated (no EOI marker, so
`jpeg_scan_hash` returns None and Pillow refuses to decode) and the "PNG"
is a JSON document with a base64 PNG inside a `normalImage` key. With
`ImageFile.LOAD_TRUNCATED_IMAGES = True` all 15 JPEGs decode; the missing
tail renders as flat grey.

Nine of the 16 are duplicates with an intact keeper already in the library:

- `Picture 046(9)`, `047(8)`, `049(5)` (FUJI, 2003): each is a strict byte
  prefix of `Picture 046/047/049.jpg` with the remainder zero-filled to the
  identical file size. 42%, 8% and 76% of the real bytes survive. The
  intact twin has an `ok` hash.
- `IMAG0136`, `IMAG0137` (2010): each is a byte prefix of an intact
  same-named twin, missing the last ~90 KB / ~65 KB. Both have three
  same-named twins; two of the three are intact copies.
- `IMG_20130727_191523` (2013): twin is a different encode (1.9 MB vs
  2.8 MB), dhash distance 1, phash distance 2; the truncated one loses the
  bottom ~15%. Twin is complete.
- `00001IMG_00001_BURST..._COVER` (2017): complete 3.65 MB twin with the
  same name; dhash and phash distance 0.
- `IMG_1742.PNG` (2024): the embedded PNG is pixel-identical to
  `IMG_1744.PNG` (912x588). Some app exported the image wrapped in JSON.

Seven are unique and simply damaged:

- The six non-cover frames of the 2017-05-01 burst: complete images that
  lack only the EOI and a few trailing bytes. Visually perfect. Keep.
- `IMG_20161106_174655`: 93% of bytes present, bottom slice grey. Keep.
- `IMG_20161104_125605`: 65 KB of real data, then 4.1 MB of zeros. Only the
  top strip survives. Unrecoverable; user decides whether it stays.

Consequence for Phase 3, which is a real design input and not a tweak:
hash-error assets are currently INVISIBLE to grouping (no scan hash, no
dhash), so nine known duplicates never appear in any group. The hash stage
should record a `truncated` status, decode leniently for the perceptual
signals, and Phase 3 arbitration needs a hard rule that a truncated file
never wins keeper over a complete one, plus a prefix rule (real bytes of A
are a byte prefix of B, and B hashes ok) that promotes such a pair to the
exact tier. The contact sheet used for this inspection was throwaway; the
byte-prefix and hash-distance checks above are reproducible from the
catalog in a few lines of Python.

Note: the `stage` table's `hash` note reads "63604 hashed, 1854 without
originals" because it counts only the final resumed run; the `asset_hash`
table itself holds 64,083 ok / 1,875 no_original / 16 error, which are the
numbers to trust.

## Modules

- `src/photodedup/catalog.py` — SQLite schema and accessors. Tables: `run`,
  `stage`, `asset`, `date_candidate`, `asset_hash`, `dup_group`, `dup_member`,
  `dup_set`, `dup_set_member`, `decision`, `decision_loser`, `schema_meta`.
  `SCHEMA_VERSION` is 3 and `open_catalog` refuses a catalog written by any
  other version. Also holds `StageIncomplete` / `require_stage_complete`,
  the guard every stage past `hash` runs through.
- `src/photodedup/dates.py` — pure date extraction and arbitration, no I/O.
  Now also holds `IMPORT_STAMPS` (the known wrong-import-time sentinels) and
  `choose_date_pooled`, the single date authority used by both `arbitrate`
  and the duplicate report.
- `src/photodedup/exif.py` — batched exiftool wrapper.
- `src/photodedup/library.py` — read-only osxphotos adapter. Installs a
  photoscript stub so it works in an agent shell.
- `src/photodedup/survey.py` — survey orchestration. Resumable, idempotent.
- `src/photodedup/hashing.py` — pure hashing: `jpeg_scan_hash`,
  `pixel_hashes`, `video_stream_hash`. FINISHED.
- `src/photodedup/hash_stage.py` — the parallel resumable hash stage.
- `src/photodedup/grouping.py` — duplicate group formation. Decides nothing.
- `src/photodedup/sets.py` — merges per-signal groups into exact and
  candidate duplicate sets (`run_sets`).
- `src/photodedup/arbitrate.py` — decides the keeper and capture date per
  duplicate set (`run_arbitrate`). Writes only to the catalog.
- `src/photodedup/review.py` — writes the static HTML review report
  (`write_review`). No JavaScript, no network.
- `src/photodedup/report.py` — Markdown survey and duplicate reports.
- `src/photodedup/cli.py` — `survey`, `report`, `hash`, `group`,
  `duplicates`, `sets`, `arbitrate`, `review`.

## Running the survey again

```
uv run photodedup survey --missing-tolerance 0.05
uv run photodedup report
```

The `--missing-tolerance` flag is required. 1,875 assets have no local
original; every one was added in 2025 or 2026 and none belongs to either
damaged batch, so they do not bear on the date question. They cannot be
hashed and are recorded as `no_original`. The default tolerance of 1% would
refuse the run.

## Phase 1 result, in one paragraph

65,974 assets surveyed in about five minutes, zero errors. 56,833 carry a
usable EXIF capture date. Only two batches in the whole library had their
capture dates overwritten with import time: 403 assets stamped
2020-12-18 09:00 and 226 stamped 2023-01-14 11:00, so 629 photos in total
rather than the thousands originally feared. Of those, 31 still carry a
usable EXIF date, all in the 2020 batch; the 2023 batch has none. With the
Google Takeout archives gone, roughly 598 photos have no mechanical recovery
path. 79 files are damaged (exiftool reports a format error rather than
merely absent EXIF) and are worth inspecting separately.

## What the final review measured against the real library

These numbers came from reading the real library and they change Phase 2's
design. Do not lose them.

- **EXIF recovery rate is low.** Of 300 cohort A assets with local originals,
  22 (7%) carry `EXIF:DateTimeOriginal`, 40 (13%) carry a date in the
  filename, and 242 (81%) carry neither and are **unrecoverable**. Those keep
  their wrong import-time date unless a duplicate twin supplies the real one.
- **The spec's `at_or_after_date_added` assumption is FALSE on this library.**
  The spec assumes rejecting candidates at or after `date_added` eliminates
  cohort A. It does not: it catches 22 of 415 cohort A assets, and 4,611 of
  65,974 library-wide. The bad import stamped capture dates at 09:10–09:24
  while `date_added` spread across 11:53 to 23:00, so the wrong date sits
  *before* its own `date_added` and survives the rule. **Phase 2's rule set
  must not be built on that assumption.** Capture-date clustering over
  `photos_date` (now in the report) is what actually isolates the cohort.
- **Timezone noise is the norm.** 74% of assets have a `photos_date` timezone
  offset differing from `date_added`'s, and 518 assets sit less than 17 hours
  after `date_added` — timezone-artifact range, not corruption. The report
  counts sub-24-hour overshoot separately for this reason.
- **Apple's own duplicate detection is readable** from `Photos.sqlite`:
  `ZASSET.ZDUPLICATEMETADATAMATCHINGALBUM` and
  `ZASSET.ZDUPLICATEPERCEPTUALMATCHINGALBUM` work as group keys, with
  perceptual fingerprints in `ZSCENEPRINT.ZDUPLICATEMATCHINGDATA`. This is
  undocumented internal schema — **advisory only**, never authoritative.

## Known Phase 2 obligation

Resolved in Phase 3: `choose_date_pooled` is the single authority; the
report's SQL copy is gone.

Also deferred deliberately, do not "fix" these without deciding they matter:
`_EXIF_RE` accepts mismatched separators like `2003:02-23`; `_iter_records`
takes `db: object`; `run_survey` falls back to `datetime.now()` for the cutoff
when no record has a `date_added`.

## Spike status

- **Spike 1 — osxphotos works outside sandbox: CLOSED, YES.**
  `osxphotos query --count` → 65974. Inside an agent-spawned shell it fails at
  `import photoscript` because AppleScript compilation needs a GUI session.
  `library.py` installs a read-only-safe photoscript stub. 65,974 assets load
  in ~13s.
- **Spike 2 — `osxphotos timewarp` can set dates: OPEN, user-owned.**
  Requires creating throwaway photos in Photos.app, selecting one in the GUI,
  running `osxphotos timewarp --date 2003-02-23 --time 09:08:08 --verbose`,
  confirming the date changed, then deleting the throwaways. **Gates the
  keeper-date-correction step in a later phase.** Does not block Phase 1.
- **Spike 3 — Apple Duplicates list readable: CLOSED, partially.** Not via
  `osxphotos albums` in an agent shell (same photoscript failure), but
  directly readable from `Photos.sqlite` as described above.
- **Spike 4 — Takeout sidecars: CLOSED, they are gone.** `mdfind -name
  'takeout'` found nothing and the user confirmed the original Google Takeout
  archives no longer exist. **`takeout_json` is a dead source.** The schema and
  `SOURCE_RANK` still list it because it is the correct top of the trust ladder
  in principle, but no candidate will ever be written for it. Spend no effort
  on it.
- **Spike 5 — iCloud originals fully downloaded: CLOSED for Phase 1.** The
  survey ran; 1,875 assets have no local original, all added in 2025 or 2026,
  and they are recorded as `no_original` rather than hashed.

## Phase 2 progress

Plan: `docs/superpowers/plans/2026-09-19-photo-dedup-phase2-hash-group.md`
Ledger (gitignored, on disk):
`.superpowers/sdd/2026-09-19-photo-dedup-phase2-hash-group/progress.md`

Every task is complete; the SHAs are in the Progress section at the top.
What the tasks established, which the commit messages do not record:

- Task 2 — the scan hash premise is CONFIRMED ON THIS LIBRARY. On the
  re-imported cohort it merges 66 groups that whole-file SHA-256 splits,
  recovering 67 assets, and all 66 decoded to pixel-identical images. An
  adversarial collision hunt found no false positives.
- Task 3 — orientation is normalised via `exif_transpose`; HEIC decoding
  verified on real files.
- Task 4 — real ffmpeg throughput ~780 MB/s, so all 141.6 GB of video hashes
  in about three minutes.
- Task 6 — animated GIF and WebP are excluded from the `exact` tier
  (`MULTIFRAME_UTIS`), because `pixel_hashes` decodes frame 0 only.
- Task 7 — the duplicate report leads with a coverage table, so a report over
  a half-finished hash stage cannot be mistaken for a final one.

Before the first full parallel run in your own terminal, run
`photodedup hash --workers 4 --limit 2000` on a fresh catalog copy and
compare row/error counts with `--workers 1`; the pool branch was unexercised
in the agent environment.

Tasks 2, 3 and 4 all append to `src/photodedup/hashing.py`, and Tasks 5, 6
and 7 each append a command to `cli.py`, so this phase ran strictly
sequentially. Phase 1 allowed overlapping implementers only because its
tasks touched disjoint files.

## Phase 3 progress

Plan: `docs/superpowers/plans/2026-09-19-photo-dedup-phase3-arbitrate-report.md`
Ledger (gitignored, on disk):
`.superpowers/sdd/2026-09-19-photo-dedup-phase3-arbitrate-report/progress.md`

Every task is complete; the SHAs are in the Progress section at the top.
What the reviews established, which the commit messages do not record:

- Truncation is detected STRUCTURALLY. `jpeg_is_truncated` walks the JPEG
  segments and reports no EOI after SOS; it does not rely on decode
  failure, because Pillow decodes a zero-padded cut file "successfully".
  All 15 real damaged JPEGs from the Phase 2 hash-error inspection were
  confirmed truncated with the right prefix lengths, and the five real
  motion photos checked alongside them were confirmed intact.
- Arbitrate treats only `hash_status == 'ok'` as intact. Any other status
  (`truncated`, `error`, `no_original`, `skipped`) ranks below an intact
  member for keeper purposes and raises a blocking `unhashed_member` flag
  rather than being silently preferred.
- The final review measured the prefix rule against the real catalog and
  found 9,679 partner candidates for the 15 truncated files; a 64 KB head
  comparison now precedes any full-prefix read.

## Phase 4 obligations

`apply` must key date correction on `decision.date_changes`, never on
`date_value` alone, because `date_value` is written for every decided set
and a sub-margin difference there is a timezone rendering, not a
correction.

`review` refuses to run when `group` was re-run after `arbitrate`, because
the decisions cascade away with the old sets. The fix is to re-run
`arbitrate`, not to force past the refusal.

`apply` must not write a capture date for any set whose `review_flags`
contains `dates_disagree`, regardless of `resolvable`. `dates_disagree` is
informational and does not block resolvability, but it means the earliest
survivor won by rule alone and nobody has looked at it; writing that date
back to Photos without a human glance defeats the point of the review step.

`apply` must skip a resolvable decision that has no `decision_loser` rows.
A set can shrink to one member between `arbitrate` and `apply` when an
asset is deleted and the deletion cascades, leaving a decision with a
keeper and nothing to lose. There is nothing to resolve in that case, and
`apply` must not treat it as an error either.

Stage dependencies are checked by status only, so re-running `hash` after
`group` has already run leaves `group` reporting complete over groups that
are now stale, and nothing downstream catches it: `require_stage_complete`
sees "complete" and moves on. This is a structural gap Phase 4 should
close. A generation counter on the `stage` table, compared by each later
stage against the generation its inputs were built from, would close the
whole class of "ran stage B, then re-ran its input A, and B never noticed"
bugs at once.

## Deferred minors from the Phase 3 reviews

None of these changes a decision. Recorded so they are not lost; fix them
when touching the file for another reason.

- `review.write_review` marks the `review` stage running before the
  output-directory guard runs, so a refused run leaves the stage as
  `running` until the next successful run. Reorder the two calls.
- `review._refuse_unsafe_out_dir` tests `review.css` with `exists()`, so a
  directory of that name passes; use `is_file()`.
- `arbitrate`: `commit()` sits outside the try; `set_stage` inside the
  except can mask the original exception; `choose_keeper`'s ValueError
  lacks the set id; `_added_ts` compares local-time epoch seconds (DST
  fall-back can tie two `date_added` values an hour apart); `_has_caption`
  counts a filename-echo title while `_caption_gain` ignores it; an
  equal-length differing caption is dropped from `carries`.
- `sets`: `dup_set.size` is written once and goes stale if an asset row is
  later deleted; no test that the kind tripwire fires on the candidate
  plan; the local variable `exact_components` shadows the report function.
- `cli`: `SetsInconsistent` and `OSError` reach the user as tracebacks.
- `grouping.prefix_groups`: the truncated side does not exclude
  `MULTIFRAME_UTIS` (unreachable, since only a JPEG can be truncated).
- Tests: two schema-version tests in `test_catalog.py`; five
  `jpeg_is_truncated` tests use `tempfile` instead of `tmp_path`; no test of
  the `thumb_path` refresh on a resumed survey.

## The number to watch

The survey found 16,712 groups sharing an exact file size and pixel
dimensions, implying at least 16,801 removable duplicates. That is a floor,
not an estimate: any pair whose EXIF was rewritten differs in byte size and
cannot appear in it. `jpeg_scan_hash` exists precisely to catch those, so a
final count well above 16,801 confirms the premise of the whole approach.
A count at or below it means the scan hash is buying nothing and the design
needs revisiting.

## Phase 2 opportunity: clock-reset cohorts

Investigated 2026-09-18, from the 2001-01-01 sentinel group.

73 photos carry dates on Apple's 2001 reference epoch: 58 on 2001-01-01
(01:14 to 23:39) and 15 on 2001-01-02. All are FUJIFILM FinePix2800ZOOM,
firmware Ver7.00, and their EXIF `DateTimeOriginal`, `CreateDate` and
`ModifyDate` all agree, so the camera genuinely believed that date when it
wrote the files. The user identified the photos as Chicago, 3 August 2002.

The same camera and firmware also produced correctly-dated photos on
2002-08-03 (`Picture 124.jpg` at 16:17:14 through `Picture 134.jpg` at
18:44:42, and more from 19:08 onward). So one camera produced both the
reset run and correctly-dated frames around it.

The recovery technique this suggests, for Phase 2. When a camera clock
resets it returns to its epoch and keeps counting, so within the run the
RELATIVE intervals between photos stay accurate and only the absolute
origin is wrong. A run like this can therefore be corrected by shifting
every member by one constant offset, derived from a single anchor. That
makes these 73 photos recoverable even though nothing in their metadata
is correct.

Detection heuristic worth building: a contiguous run of photos from one
camera model whose timestamps start at or near a known sentinel epoch and
increase monotonically, bounded by correctly-dated photos from the same
camera. Offer the user the run, ask for one anchor date, shift the whole
run.

This is why `sentinel_breakdown` in the report groups by VALUE and not
just by count. Rejecting these dates is correct; discarding the photos'
recoverability is not.

## Phase 3 opportunity: an unexplained capture-hour cluster

A third capture-hour cluster, 218 assets at 2023-05-03 01:00 with no EXIF,
all added within two minutes on 2025-03-12, is unexplained. It is NOT in
`IMPORT_STAMPS`. Look at it before Phase 4 corrects any dates.

## Domain decisions

- 2026-09-20, from the user, after sampling: a copy that lost rows at the
  bottom is damaged even when its structure is complete (IMAG0136's largest
  copy has an EOI and 144 blank rows). The hash stage now measures
  `blank_rows` (uniform-colour rows at the bottom of the decode) and `flat`
  (every row blank); the keeper rule `rendered` prefers fewer blank rows
  right after `intact`; sets with a partially rendered member carry
  `partial_render`, sets of entirely flat images carry `blank_image`
  (nine flat images in this library, to be offered for deletion in Phase
  4), and flat images never join the look-alike tier.
- 2026-09-20, from the user: when everything else ties, keep the copy whose
  name has no " (1)" style suffix (`clean_name`, just before `date_added`).
- 2026-09-20, from the user: the candidate (look-alike) tier is left out of
  Phase 4 entirely; macOS Photos' own Duplicates feature can be used on
  whatever remains. Members of a candidate set differ in bytes and pixels,
  so they are never assumed to be copies.
- 2026-09-20, from the user, after sampling the review report: a copy that
  carries Photos edits (a crop, a colour adjustment) is a distinct
  photograph and is never a loser, whatever its ranking. `split_protected`
  in `arbitrate.py` enforces it; such sets carry the informational flag
  `kept_edits` (8 sets on this library) and the report shows the member as
  "KEEP (edited)". The favorite flag was already safe: a favorite wins the
  keeper slot, and a favorite loser is recorded in `carries` for Phase 4.
- 2026-09-18, from the user: no photo in this library has a legitimate capture
  date of 1970-01-01 or 1980-01-01. Any candidate landing on those dates is a
  software default and is wrong. The sentinel rejection rule in `dates.py`
  (`SENTINEL_DATES`, 1-day `SENTINEL_WINDOW` for those entries) is therefore
  safe for the Unix and FAT epochs and must not be softened.
- Not covered by that decision: the 2001-01-01 sentinel, which is Apple's
  reference epoch. A photo genuinely taken on 1 January 2001 is plausible in a
  way a 1970 one is not, and a full-day window would discard it silently. As of
  `89a42b2` that sentinel uses a **14-hour** window — wide enough for every
  timezone rendering of a UTC-midnight epoch, narrow enough not to reach into
  the neighbouring days. 58 real assets in this library land near it with
  genuinely ambiguous evidence. The report breaks sentinel rejections out by
  value; inspect the 2001 bucket individually before trusting the rule.

## Open questions

- User must confirm, outside an agent shell: (1) whether `osxphotos timewarp`
  actually changes dates in Photos (spike 2, gates a later phase), (2) the
  current iCloud "Download Originals to this Mac" status (spike 5, gates the
  real survey run). Neither blocks writing or unit-testing code.
- AppleScript access to Photos from an agent-spawned shell remains broken
  (photoscript import-time compilation fails). Mitigated for Phase 1 by the
  photoscript stub, since Phase 1 is read-only. It must be re-checked before
  any later `apply` stage that calls osxphotos functions needing photoscript
  from within an agent shell.

## How to resume in a new session

Give the new session this file and nothing else; it names everything.

1. Read this file top to bottom, then the spec at
   `docs/superpowers/specs/2026-09-18-photo-dedup-design.md`, especially its
   "Survey findings (2026-09-19)" and "Motion photos" subsections, which
   amend the original design with what the library actually contains.
2. Confirm the tree: `git log --oneline -5`, `git status` (should be clean),
   `uv run pytest -q` (all green).
3. Check the catalog matches this file's claims:
   `uv run python -c "import sqlite3;c=sqlite3.connect('catalog.sqlite');print(c.execute('SELECT name,status,notes FROM stage').fetchall())"`
   If the `hash` stage is not `complete`, resume it with `uv run photodedup hash`
   before anything else; it picks up where it stopped.
4. Do whatever the "Phase 2 results" section says is outstanding. If it is
   still empty, that means `group` and `duplicates` have not been run and
   their numbers not recorded; do that first.
5. Phase 3 (arbitrate, review report) is BUILD COMPLETE but has not been run
   against the real catalog. Do whatever "The one outstanding action" at the
   top of this file says, then fill in "Phase 3 results". Phase 4 (archive,
   apply, verify) has no plan yet; write it from the Phase 3 numbers, using
   the brainstorming skill, then writing-plans, then subagent-driven
   development, as Phases 1 through 3 did. Its obligations are recorded
   above under "Phase 4 obligations".

What worked in Phases 1 and 2, keep doing: a fresh subagent per task with
the plan carrying complete code; a reviewer per task on a mid-tier model,
instructed to HUNT for false positives rather than confirm the happy path;
the final whole-branch review on the most capable model. The reviews caught
a real defect roughly every other task, several of which would have deleted
photos wrongly. Every fix round so far traced to a defect in the brief, not
the implementer, so cheap implementers are fine when the plan is complete.

Process gotchas in an agent shell: `ProcessPoolExecutor` cannot start
(semaphore PermissionError), `ps` and `/Volumes` are blocked, and osxphotos
needs the `photoscript` stub in `library.py` to import at all. None of
these affect the user's own terminal.

## Running the tool

Always from the repository root, through uv:

    cd /Users/robotdan/dev/tools
    uv run photodedup --help
    uv run photodedup survey --missing-tolerance 0.05   # read the library into the catalog
    uv run photodedup report                            # write reports/survey.md
    uv run photodedup hash                              # content hashes; parallel; resumable
    uv run photodedup group                             # form duplicate groups
    uv run photodedup duplicates                        # write reports/duplicates.md
    uv run photodedup sets                              # rebuild duplicate sets (group does this itself)
    uv run photodedup arbitrate                         # decide keeper and capture date per set
    uv run photodedup review                            # write reports/review/index.html

Every command is read-only against the Photos library and writes only to
`./catalog.sqlite` and `./reports/`. All are safe to interrupt and re-run.
