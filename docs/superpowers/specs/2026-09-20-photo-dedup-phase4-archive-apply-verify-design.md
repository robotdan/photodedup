# Photo De-duplication — Phase 4 Design: Archive, Apply, Verify

**Date:** 2026-09-20
**Status:** approved in conversation, awaiting implementation plan
**Parent specs:** `2026-09-18-photo-dedup-design.md` (binding),
`2026-09-19-photo-dedup-phase3-arbitrate-report-design.md`
**Inputs:** the Phase 3 decisions the user sampled and accepted on
2026-09-20 (`STATE.md`, "Phase 3 results"), and the timewarp spike the user
ran the same day.

## Scope

Phase 4 acts on the decisions Phase 3 wrote. Three commands:

- `archive` copies every loser's original to the external drive and
  verifies the copy. Read-only against Photos.
- `apply` merges loser metadata onto keepers, corrects keeper dates, and
  stages losers in albums for the user to delete. Writes to Photos through
  its scripting interface. Runs only from an interactive terminal.
- `verify` re-reads the library and reports what took effect. Read-only.

Nothing in Phase 4 deletes a photo. Deletion is the user selecting an album
in Photos and pressing delete, as the parent spec requires.

## Decisions made in this design pass

1. The archive holds losers only: 16,854 files, 62.5 GB. Keeper changes
   are additive (albums, keywords, favorite, captions) or reversible (dates,
   whose previous value is recorded), so they need no archive. Photos'
   Recently Deleted covers the first 30 days; the archive covers after.
2. Archived files are byte-identical copies, verified by re-reading. A
   JSON sidecar beside each carries the catalog's view of the asset. No
   EXIF is stamped.
3. Layout is `<root>/<YYYY>/<MM>/<uuid>_<original_filename>` by the loser's
   Photos date (`undated/` when none), with `<same>.json` beside it and a
   `manifest.csv` at the root. Edited renders and Live Photo movies are
   archived too when present, as `<uuid>_edited.<ext>` and
   `<uuid>_live.<ext>`.
4. Keeper dates are set through `photoscript.Photo.date` with the naive
   local datetime the catalog holds, which is the value Photos displays.
   `osxphotos timewarp` is not used: the user's spike showed it shifting a
   requested 09:08:08 to 10:08:08 through its timezone handling.
5. Date corrections apply by default only to resolvable exact sets with
   `date_changes = 1` and no `dates_disagree` flag (5 sets). The 103 sets
   that also carry `dates_disagree` apply only under
   `--include-disagreeing-dates`, which the user enables having sampled the
   "dates rescued" page. This discharges the Phase 4 obligation in
   `STATE.md` without discarding the rescues.
6. Losers are staged in a Photos folder `Dedup Delete` holding albums
   `Dedup Delete 001` … of at most 500 assets, per the parent spec. The
   keepers of `blank_image` sets (flat images) go in a separate album
   `Dedup Delete blank`, so that the user can confirm those separately.
   Those blank keepers are archived and gated exactly like losers: they end
   up in a delete album too, so `apply` refuses to run while one of them
   has no verified archive copy.
7. A new catalog table may be added without a schema version bump when it
   is purely additive; `open_catalog` creates it on open. Changing an
   existing table still bumps the version and forces a rebuild. Phase 4
   adds tables only.
8. Because the agent shell cannot drive Photos, `apply --self-test` exists:
   in the user's terminal it creates a throwaway album, adds one existing
   photo, sets and restores that photo's title and date, reads both back
   through osxphotos, deletes the album, and reports. It runs before any
   real apply and is the spike that proves the write path.

## Schema additions (v4, additive)

```sql
CREATE TABLE IF NOT EXISTS archive_file (
    uuid        TEXT NOT NULL REFERENCES asset (uuid) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('original', 'edited', 'live')),
    dest        TEXT NOT NULL,           -- path under the archive root
    bytes       INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    PRIMARY KEY (uuid, kind)
);

CREATE TABLE IF NOT EXISTS apply_action (
    uuid        TEXT NOT NULL REFERENCES asset (uuid) ON DELETE CASCADE,
    action      TEXT NOT NULL CHECK (action IN
                  ('merge', 'date', 'stage', 'stage_blank')),
    status      TEXT NOT NULL CHECK (status IN ('done', 'failed', 'skipped')),
    detail      TEXT,                    -- JSON: what was written, or the error
    previous    TEXT,                    -- JSON: the value before, for reversal
    applied_at  TEXT NOT NULL,
    PRIMARY KEY (uuid, action)
);
```

Both live in `catalog.py`. `stage` rows `archive`, `apply`, `verify` are
recorded like every other stage.

## `archive`

`photodedup archive [--root "/Volumes/Extreme Pro/photodedup"] [--limit N]`

Preconditions: `arbitrate` complete; every `dup_set` has a decision (same
guard as `review`); root's volume is mounted and writable (write and delete
a probe file); free space at least the sum of pending losers' sizes plus
10 percent, else refuse with the numbers.

Population: every `decision_loser` row whose set is `exact` and whose
decision is `resolvable = 1`, joined to `asset` for `path`, `photos_date`,
`original_filename`. Rows already in `archive_file` for `kind = 'original'`
are skipped, which makes the stage resumable.

Per loser, in order: read osxphotos for `path_edited` and `path_live_photo`
(the survey did not record them; the archive stage opens the library
read-only once through `library.iter_assets`-style access and builds a
uuid → paths map before copying); copy the original with a streaming SHA-256
of the bytes written; re-open the destination and hash it again; only when
both hashes match insert the `archive_file` row; write the sidecar; repeat
for edited and live files when present. A mismatch deletes the destination
and records nothing, so the next run retries it. Copies go through a
temporary name and are renamed into place, so an interrupted copy never
looks complete.

Sidecar JSON fields: `uuid`, `original_filename`, `role` (`loser`),
`set_id`, `keeper_uuid`, `lost_on`, `carries`, `albums`, `keywords`,
`is_favorite`, `title`, `description`, `photos_date`, `date_added`,
`arbitrated_date`, `review_flags`, `sha256`, `bytes`, `archived_at`.

`manifest.csv` is regenerated from `archive_file` at the end of every run:
`uuid,kind,dest,bytes,sha256,verified_at`.

Progress prints every 200 files. Stage notes record files, bytes, and
mismatches retried.

## `apply`

`photodedup apply [--dry-run] [--merge] [--dates] [--stage] [--all]
[--include-disagreeing-dates] [--self-test] [--batch 500]`

With no sub-step flag it prints the plan and exits; `--all` runs merge,
dates, stage in that order. Preconditions for any real write: `archive`
stage complete AND every pending loser has an `archive_file` row for its
original; otherwise refuse and say how many are missing. `--dry-run` prints
every action it would take, per asset, and writes nothing.

Photos access goes through a thin adapter `photos_write.py` wrapping
photoscript: `open_library()`, `photo(uuid)`, `set_title/description/
keywords/favorite/date`, `album(name)`/`create_album(name, folder)`,
`add_to_album`. It is the only module that imports photoscript, and it
imports it lazily inside functions so the rest of the package still loads
in the agent shell. A missing GUI session surfaces as one clear error.

Merge (`action = 'merge'`), one row per loser whose `carries` is non-empty
(2,241): for the keeper, add albums (creating any album that no longer
exists, at top level), union keywords, set favorite when the loser had it
and the keeper did not, set title/description when `carries` names them.
`previous` records the keeper's values before the write. Idempotent:
adding a photo already in an album or a keyword already present is a
no-op.

Dates (`action = 'date'`), one row per keeper in the date-fix population
(decision 5): read the current date, write the arbitrated naive datetime
via `Photo.date`, read back through osxphotos, and mark `done` only when
the read-back equals the intended value to the second; otherwise `failed`
with both values in `detail`. `previous` holds the old date.

Stage (`action = 'stage'`): losers are sorted by uuid and chunked into
`--batch` albums under folder `Dedup Delete`; an album is filled only with
losers not yet `done`, so a re-run continues the batch numbering rather
than duplicating. `stage_blank` puts the keepers of `blank_image` sets in
`Dedup Delete blank`. After staging, `losers.csv` is written to the
project root: `uuid,original_filename,photos_date,keeper_uuid,set_id,
lost_on,album,archive_dest`.

Every write is wrapped so that one AppleScript failure records a `failed`
row and continues; the stage note counts done/failed/skipped per action.
`--self-test` (decision 8) runs alone and touches nothing else.

## `verify`

`photodedup verify [--out reports/verify.md]`

Reads the library through osxphotos (read-only, stub-safe). Reports:

- Losers with a `stage` row still present in the library (deletion not
  yet done), with their album name, and losers now absent.
- Keepers absent from the library (must be zero; printed as "0, as
  required").
- Date rows marked `done` whose current library date differs from the
  intended value by more than one second.
- Merge rows whose keeper is not in an album the merge added.
- Blank-image keepers still present.

Safe to run repeatedly; it writes only the report and its stage row.

## Error handling

- `archive`: any I/O error on one file is recorded in the stage note and
  the file is retried next run; the run continues. Free-space check at
  start; if the volume disappears mid-run, the next copy fails and the
  run stops with a clear message, everything verified so far kept.
- `apply`: refuses without the archive; refuses in a non-GUI session with
  the photoscript error explained; per-asset failures never abort the
  run; `--dry-run` never opens the write adapter.
- `verify`: never writes to Photos.

## Testing

The photoscript adapter is faked in tests with an in-memory library that
records calls; every `apply` sub-step is tested against the fake for
idempotence, ordering, batch numbering, the date gate, `previous`
recording, and failure isolation. `archive` is tested against temp
directories with an injected fake path map, including hash mismatch
retry, resume, free-space refusal, and sidecar contents. `verify` is
tested with an injected fake library reader. The self-test is the only
thing that touches a real library, and only the user runs it.

## Expected results on this library

- `archive`: 16,854 loser originals, about 62.5 GB, plus the 2 blank-image
  keepers, plus edited renders and live movies for losers that have them
  (few). Zero mismatches after retry.
- `apply --merge`: 518 keepers touched.
- `apply --dates`: 5 by default; 61 with `--include-disagreeing-dates`.
- `apply --stage`: `Dedup Delete 001` through `034` — 33 albums of 500 and
  one of 354 — and `Dedup Delete blank` with 2 keepers.
- `verify` before deletion: all 16,854 losers present in their albums,
  0 keepers missing, 0 date failures. After deletion they move to the
  "in Recently Deleted" line, not to "gone", until the trash is emptied.

The 2,241 merges and 108 date corrections quoted earlier in this document
counted candidate-tier rows that `apply` excludes. `apply` works only on
the resolvable exact tier, which is where the 518 and the 5 / 61 come
from.

## Process

Same as Phases 1 through 3: brief per task with complete code, cheap
implementers, a hostile reviewer per task, a final whole-branch review.
Order: schema and adapter (with fake) → archive → apply merge/dates →
apply stage and self-test → verify and CLI/handoff.
