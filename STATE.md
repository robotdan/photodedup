# Photo De-duplication — Working State

**Last updated:** 2026-09-18
**Current phase:** Phase 1 (foundation and survey) — CODE COMPLETE, survey not yet run
**Branch:** `phase1-survey`
**Plan:** docs/superpowers/plans/2026-09-18-photo-dedup-phase1-survey.md
**Spec:** docs/superpowers/specs/2026-09-18-photo-dedup-design.md (binding authority)

## Paths

- Library: /Users/robotdan/Pictures/Photos Library.photoslibrary (65,971 assets)
- Catalog: ./catalog.sqlite (gitignored, not yet created)
- Archive: /Volumes/Extreme Pro (unused in Phase 1)

## Phase 1 is READ-ONLY

Nothing in Phase 1 may write to the Photos library, launch Photos.app, or
modify anything inside the `.photoslibrary` bundle. Reading `Photos.sqlite`
directly is fine. No mutating osxphotos call is permitted anywhere in this
phase.

## Progress — all nine tasks complete and committed

- [x] Task 0: Spikes — `8ad073c`
- [x] Task 1: Project scaffold — `f651d74`
- [x] Task 2: Catalog schema — `47238c3`
- [x] Task 3: Date parsing — `f97df79`
- [x] Task 4: Plausibility and selection — `aef7767`
- [x] Task 5: exiftool batch wrapper — `378e1b2`
- [x] Task 6: Photos library adapter — `0902a2a`, fixed by `4f7c2d9`, `e14cd2a`, `05cae91`
- [x] Task 7: Survey orchestration — `3e6f2f1`
- [x] Task 8: Survey report and CLI — `f51c4c1`
- [x] Task 9: Whole-branch review fixes — `89a42b2`

The design document itself is `d77d805`.

**The branch is not a scaffold.** Every module below exists, is tested, and
`uv run pytest -v` passes. Do not re-implement any of it.

## Modules

- `src/photodedup/catalog.py` — SQLite schema and accessors. Tables: `run`,
  `stage`, `asset`, `date_candidate`, `schema_meta`. `open_catalog` creates and
  migrates. `set_run`/`get_run` hold the cutoff watermark; `set_stage`/
  `get_stage` track pipeline progress.
- `src/photodedup/dates.py` — pure date extraction and arbitration, no I/O.
  EXIF and filename parsing, `SOURCE_RANK` (the trust ladder), `SENTINEL_DATES`
  with per-entry windows, `rejection_reason`, `choose_date`.
- `src/photodedup/exif.py` — batched exiftool wrapper, returns tags per path
  and surfaces `ExifTool:Error` / `ExifTool:Warning`.
- `src/photodedup/library.py` — read-only osxphotos adapter yielding
  `AssetRecord`. Installs a photoscript stub so it works in an agent shell.
- `src/photodedup/survey.py` — orchestration. Resumable, idempotent, batched.
  Writes one `asset` row and up to five `date_candidate` rows per asset.
- `src/photodedup/report.py` — Markdown survey report over the catalog.
- `src/photodedup/cli.py` — `photodedup survey` and `photodedup report`.

Beyond the original plan, the `asset` table carries an **`exif_error`** column
holding the exiftool error or warning string, so damaged files are countable
rather than silently date-less.

## The only outstanding Phase 1 action

Run the real survey, then the report:

```
uv run photodedup survey
uv run photodedup report
```

This is **gated on the user confirming** that Photos → Settings → iCloud →
"Download Originals to this Mac" is selected and the download has finished.
Assets without local originals yield no EXIF, and EXIF is the only remaining
recovery path (see below). The survey aborts if more than 1% of in-scope
assets lack a local original.

Do not run the survey against the live library without that confirmation.

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
  65,971 library-wide. The bad import stamped capture dates at 09:10–09:24
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

`choose_date` in `dates.py` is currently **unused**. Its logic is duplicated in
the report's SQL, and the two have already drifted: the SQL compares dates with
no margin while `choose_date` uses a 26-hour `DEFAULT_DISAGREEMENT_MARGIN`.
Phase 2 must make `choose_date` the single authority and have the report call
it rather than re-express it in SQL. This was deliberately deferred, not
overlooked.

Also deferred deliberately, do not "fix" these without deciding they matter:
`_EXIF_RE` accepts mismatched separators like `2003:02-23`; `_iter_records`
takes `db: object`; `run_survey` falls back to `datetime.now()` for the cutoff
when no record has a `date_added`.

## Spike status

- **Spike 1 — osxphotos works outside sandbox: CLOSED, YES.**
  `osxphotos query --count` → 65971. Inside an agent-spawned shell it fails at
  `import photoscript` because AppleScript compilation needs a GUI session.
  `library.py` installs a read-only-safe photoscript stub. 65,971 assets load
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
- **Spike 5 — iCloud originals fully downloaded: OPEN, user-owned.** Blocks the
  real survey run only. See "The only outstanding Phase 1 action" above.

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

## Domain decisions

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
