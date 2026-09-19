# Photo De-duplication — Phase 3 Design: Arbitrate and Review Report

**Date:** 2026-09-19
**Status:** approved in conversation, awaiting implementation plan
**Parent spec:** `2026-09-18-photo-dedup-design.md` (binding; this document
amends and narrows it for Phase 3 only)
**Inputs:** the Phase 2 numbers and the hash-error inspection recorded in
`STATE.md` on 2026-09-19

## Scope

Phase 3 covers the `arbitrate` and `report` stages of the parent spec's
pipeline, plus one correctness fix to `hash` and `group` that the Phase 2
run exposed. It ends with a static HTML report the user can open in a
browser to check keeper and date decisions by sampling.

Phase 3 remains **entirely read-only** against the Photos library. It writes
only to `./catalog.sqlite` and `./reports/`. Archive, apply and verify are
Phase 4 and are not designed here; they start only after the user has
sampled this phase's report and accepted its decisions.

Explicitly out of scope for Phase 3:

- Merging albums, keywords, favorites, titles or captions from losers onto
  keepers. That is an `apply`-time write to Photos and belongs in Phase 4.
  Arbitrate records what *would* merge so the report can show it.
- Correcting dates in Photos (`osxphotos timewarp`). Arbitrate decides the
  date; Phase 4 applies it.
- Any change to perceptual matching thresholds.

## Decisions made in this design pass

1. Phase 3 is arbitrate and report only, so the user can validate the
   algorithm on a sample before any I/O to the external drive or Photos.
2. Keeper tie-break order, in descending priority:
   1. an intact file beats a truncated one, always;
   2. larger `file_size` wins (the parent spec's motion-photo rule);
   3. the member the user has invested in wins: `is_favorite`,
      `has_adjustments`, a non-empty `title` or `description`, then the
      greater number of albums, compared in that order;
   4. earlier `date_added` wins;
   5. lowest `uuid` wins, so re-runs are deterministic.
3. Perceptual-only sets appear in the report in their own section, sampled
   to a few hundred and ordered closest match first. They are never marked
   resolvable.
4. The 16 hash-error files are fixed in this phase, as the first task:
   a `truncated` hash status, lenient perceptual hashing, and a byte-prefix
   exact rule.
5. `choose_date` in `dates.py` becomes the single authority for capture
   dates. The SQL re-expression of its logic in `report.py` is deleted.
6. The two import-time stamp clusters the survey found (403 assets at
   2020-12-18 09:00 and 226 at 2023-01-14 11:00) become survey-derived
   sentinels for `photos_db` candidates. The parent spec's
   `at_or_after_date_added` rule was measured and does not catch them
   (see `STATE.md`, "What the final review measured"); clustering does.

## Schema version 3

`SCHEMA_VERSION` becomes 3. Per the standing rule the catalog is rebuilt
(`survey`, then `hash`, then `group`), not migrated. In the user's terminal
that is roughly five minutes for survey and under fifteen for hash with
`--workers 4`. The rebuild is also the first full exercise of the parallel
hash path.

Changes to existing tables:

- `asset` gains `thumb_path TEXT`: the path of the smallest derivative image
  Photos keeps for the asset inside the library bundle, read from osxphotos
  `path_derivatives` (last element; the list is ordered largest first).
  Null when Photos has none. Read-only; the report uses it for thumbnails
  so it never decodes an original.
- `asset_hash.status` CHECK gains `'truncated'`. A truncated row has
  `dhash`, `phash`, `pixel_hash`, `pixel_width`, `pixel_height` from a
  lenient decode and `scan_hash` NULL. `bytes_read` records the length of
  the file with trailing zero bytes stripped, which is the prefix length
  the group stage compares.
- `dup_group.signal` CHECK gains `'prefix'`, tier `exact`.

New tables:

```sql
CREATE TABLE IF NOT EXISTS dup_set (
    id       INTEGER PRIMARY KEY,
    tier     TEXT NOT NULL CHECK (tier IN ('exact', 'candidate')),
    kind     TEXT NOT NULL,            -- 'image' or 'video'; never mixed
    size     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dup_set_member (
    set_id   INTEGER NOT NULL REFERENCES dup_set (id) ON DELETE CASCADE,
    uuid     TEXT NOT NULL REFERENCES asset (uuid) ON DELETE CASCADE,
    tier     TEXT NOT NULL CHECK (tier IN ('exact', 'candidate')),  -- copied from dup_set
    signals  TEXT NOT NULL,            -- comma-joined signals linking this member
    PRIMARY KEY (set_id, uuid)
);
CREATE UNIQUE INDEX IF NOT EXISTS dup_set_member_exact
    ON dup_set_member (uuid) WHERE tier = 'exact';
CREATE INDEX IF NOT EXISTS dup_set_member_uuid ON dup_set_member (uuid);

CREATE TABLE IF NOT EXISTS decision (
    set_id         INTEGER PRIMARY KEY REFERENCES dup_set (id) ON DELETE CASCADE,
    keeper_uuid    TEXT NOT NULL REFERENCES asset (uuid),
    keeper_reason  TEXT NOT NULL,      -- which tie-break rule decided it
    date_value     TEXT,               -- arbitrated capture date, ISO, naive
    date_source    TEXT,               -- source of the winning candidate
    date_rejected  TEXT,               -- JSON list of [source, reason]
    date_changes   INTEGER NOT NULL,   -- 1 if date_value differs from the keeper's photos_date by > margin
    resolvable     INTEGER NOT NULL,   -- 1 only for exact tier with no review flag
    review_flags   TEXT NOT NULL,      -- comma-joined flags, '' when none
    decided_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_loser (
    set_id   INTEGER NOT NULL REFERENCES dup_set (id) ON DELETE CASCADE,
    uuid     TEXT NOT NULL REFERENCES asset (uuid),
    lost_on  TEXT NOT NULL,            -- the rule that ranked it below the keeper
    carries  TEXT NOT NULL,            -- JSON: albums/keywords/favorite/title the loser has and the keeper lacks
    PRIMARY KEY (set_id, uuid)
);
```

The partial unique index enforces that an asset belongs to at most one
`exact` set. That is the invariant arbitration depends on. An asset may
additionally appear in one `candidate` set, which is how "this exact set
looks like that one" is recorded; `tier` is denormalised onto the member
row only so the index can express the rule.

## Task A — truncated files

### `hashing.py`

`pixel_hashes(path, *, lenient=False)`. With `lenient=True` the decode runs
under `ImageFile.LOAD_TRUNCATED_IMAGES = True`, set and restored around the
call so it never leaks into a strict decode in the same process. Return
type unchanged. `jpeg_scan_hash` is unchanged: a JPEG with no EOI still
returns `None`, and that is the signal.

New pure helper `real_length(data: bytes) -> int`: length after stripping
trailing `\x00` bytes. Zero-filled tails are what the three FUJI files and
both 2016 files carry.

### `hash_stage.py`

`hash_one` gains a third branch. When the strict path yields neither scan
nor pixel hash for an image, retry `pixel_hashes(..., lenient=True)`. If
that succeeds, return `status='truncated'` with the perceptual columns
filled, `scan_hash=None`, and `bytes_read=real_length(data)`. If it still
fails, `status='error'` as today. The JSON-wrapped PNG stays an error: it is
not an image file and nothing in this tool should pretend it is. Its
duplicate is handled by hand in Phase 4 and `STATE.md` records which one.

Video is unaffected.

### `grouping.py`

`SIGNALS` gains `("prefix", None, "exact")`, handled by its own code path
after the hash-keyed signals. For every `asset_hash` row with
`status='truncated'`:

1. Candidate partners are `status='ok'` image assets with the same
   `(kind, has_raw, pixel_width, pixel_height)` and `file_size >=
   bytes_read` of the truncated row. Multi-frame UTIs are excluded exactly
   as for the other exact signals.
2. For each candidate, read both files and test that the first
   `bytes_read` bytes are identical. A single match forms a two-member
   `prefix` group whose `digest` is the partner's `scan_hash` when present,
   else its `pixel_hash`, so overlapping prefix groups (two truncated
   copies of one intact file) merge naturally into one set later.
3. A truncated file matching no candidate forms no group. It is simply a
   damaged unique photo and the report lists it as such.

Truncated rows never join `scan`, `pixel` or `video` groups (their
`status` is not `ok`). They **do** join `dhash` candidate groups: the
query for the `dhash` signal accepts `status IN ('ok', 'truncated')`. That
is how a truncated re-encode of an intact photo still reaches human review.

Cost: 15 truncated rows on this library, each with a handful of candidates.
Reads are sequential and bounded by `file_size` of the partner.

### Tests

- A JPEG fixture cut before EOI: strict `pixel_hashes` returns None,
  lenient returns hashes, `hash_one` reports `truncated`.
- The same fixture zero-padded: `bytes_read` equals the un-padded length.
- Prefix rule: a truncated copy of fixture A must group with A and must
  **not** group with fixture B of identical dimensions whose bytes diverge.
- Adversarial: a truncated file whose `bytes_read` exceeds the candidate's
  `file_size` is never compared; a truncated file with the same leading
  bytes but different dimensions is never compared.

## Task B — sets

New module `sets.py`, one function `run_sets(conn) -> SetResult`, invoked
at the end of `run_group` and by a `photodedup sets` command for re-runs.
It is the union-find in `report.exact_components`, moved and extended;
`exact_components` is then rewritten to read `dup_set` and the union-find
copy in `report.py` is deleted.

Rules:

- Two assets are in the same set if any group contains both.
- Group memberships are unioned exact signals first, then candidate
  signals. A set's tier is `exact` if the set is connected using exact
  edges alone; a set that needs a candidate edge to stay connected is
  split: the exact-connected components become their own `exact` sets,
  and the remaining candidate-only members form `candidate` sets with
  whichever exact component they touch, tier `candidate`, member
  `signals` recording the link. An asset is in at most one exact set and
  at most one candidate set.

  Concretely: run union-find over exact edges to get exact components.
  Then run a second union-find over candidate edges on the *components*
  (not the assets). A candidate-tier set is a component-of-components with
  more than one member, and its members are listed with the exact
  component they came from, so the report can show "these two exact sets
  look alike". The exact sets keep their own `decision` rows; a candidate
  set gets a decision row too, with `resolvable=0`, so the report can show
  what the keeper *would* be.
- Hard partitions are already enforced inside the groups (kind and
  `has_raw` are part of every group key), so a set inherits them. `kind`
  is copied onto the set and a CHECK-style assertion in `run_sets` raises
  if any set mixes kinds. That is a tripwire, not a feature.
- Live Photos need no rule: in Photos the movie is a resource of the
  still, not a separate asset, so it follows the still automatically.

`SetResult` reports `(exact_sets, exact_assets, candidate_sets,
candidate_assets)`. On this library the first two must be 16,846 and
33,717 plus whatever Task A's prefix groups add (nine assets in up to nine
sets). If they are not, the port from `exact_components` is wrong.

## Task C — arbitrate

New module `arbitrate.py`, `run_arbitrate(conn, *, margin=DEFAULT_DISAGREEMENT_MARGIN) -> ArbitrateResult`,
and a `photodedup arbitrate` command. It deletes and rewrites `decision`
and `decision_loser` on every run. It refuses to run unless the `group`
stage is `complete`, mirroring `group`'s guard on `hash`, with the same
`--force`.

### Keeper selection

`choose_keeper(members: list[Member]) -> tuple[Member, str, list[tuple[Member, str]]]`,
a pure function in `arbitrate.py`. `Member` is a frozen dataclass carrying
the asset columns the rules read plus `hash_status`. It sorts members by
the key

```
(hash_status != 'truncated',      # intact first
 file_size,
 is_favorite, has_adjustments, bool(title or description), album_count,
 -date_added_ordinal,             # earlier added first
 reversed uuid ordering)          # lowest uuid first
```

descending, and reports **which component of the key** separated the
winner from the runner-up. That component name is `keeper_reason`, and for
each loser the component that separated it from the keeper is `lost_on`.
The rule names are fixed strings: `intact`, `file_size`, `favorite`,
`adjustments`, `caption`, `albums`, `date_added`, `uuid`.

Invariants tested adversarially:

- A truncated member never wins, even when it is larger, favourited, in
  more albums and added earlier.
- The keeper's `file_size` is never smaller than any intact loser's.
- Reordering the input never changes the winner.

### Date selection

For each set, pool the `date_candidate` rows of every member, tagging each
with its own asset's `date_added` and `file_mtime`, and call `choose_date`
once per member to get per-member survivors, then take the earliest
survivor across the set with `SOURCE_RANK` breaking ties. Rejections are
evaluated against the candidate's *own* asset, because `date_added`
differs per member and that is the whole point of a cohort A / cohort B
pair.

`choose_date` gains one thing: a `stamps: Sequence[datetime]` parameter
(default empty) and a rejection reason `import_stamp` for a `photos_db`
candidate within `STAMP_WINDOW = timedelta(minutes=30)` of any stamp.
`arbitrate` reads the stamp list from a new `run.import_stamps` column
that `survey` writes from its `photos_date_clusters` (clusters of at least
100 assets sharing the same minute). On this library that yields exactly
the two known stamps. The report's `_EXIF_VS_PHOTOS_SQL` and any other SQL
that re-implements `rejection_reason` is deleted and replaced by calls
that go through `choose_date`.

`date_changes = 1` when the arbitrated value differs from the keeper's own
`photos_date` by more than `margin`. This is the count that Phase 4's
timewarp step works from and the count the report calls "dates rescued".

### Review flags

`review_flags` is the comma-joined subset of:

- `candidate_tier` — set tier is `candidate`.
- `dates_disagree` — `choose_date` reported disagreement beyond `margin`.
- `no_plausible_date` — no candidate survived.
- `png` — any member is `public.png` (alpha is collapsed by the pixel hash).
- `multiframe` — any member's UTI is in `MULTIFRAME_UTIS`.
- `truncated_member` — any member is truncated (the prefix rule is new and
  deserves eyes on every instance this first time).
- `size_spread` — intact members differ in `file_size` by 1 MB or more
  (the motion-photo case; the rule handles it, the flag surfaces it).
- `keeper_smaller` — the keeper is smaller than an intact loser. The
  keeper rule makes this impossible; the flag exists so a bug is visible in
  the report rather than silent.

`resolvable = 1` iff tier is `exact` and `review_flags` is empty, with one
exception: `size_spread` and `dates_disagree` do **not** block
resolvability on their own, because the parent spec resolves exact groups
automatically and those two flags describe the keeper rule and the date
rule doing their jobs. They are listed for sampling. Every other flag
blocks.

### Loser carry-over

For each loser, `carries` is the JSON of what it has that the keeper
lacks: albums not on the keeper, keywords not on the keeper,
`favorite: true` when the loser is favourited and the keeper is not, and
`title`/`description` when the loser's is non-empty and longer than the
keeper's after the parent spec's filename-echo filter. Arbitrate computes
it; Phase 4 applies it. The report shows it so the user can see nothing
is silently dropped.

## Task D — HTML review report

`photodedup review [--out reports/review] [--per-page 250] [--sample 300]`.
Writes a directory:

```
reports/review/
  index.html
  exact/001.html … NNN.html
  candidate/001.html … (sampled)
  thumbs/<uuid>.jpg      (symlinks or copies of thumb_path; copies if the
                          library is on a different volume, since a report
                          that breaks when the library moves is useless)
  review.css
```

No JavaScript is required to read the report. Thumbnails are `<img
loading="lazy">` referencing `thumbs/`. The page is plain HTML with one
stylesheet; there is no framework and nothing is fetched from the network.

`index.html` carries, in order:

1. Totals: exact sets, exact assets, removable, resolvable sets, sets
   needing review by flag, dates rescued, unique damaged files.
2. Sampled sections, each a list of set cards linking to the set's page
   anchor: dates rescued (largest disagreement first), largest sets, sets
   with a truncated member, size-spread sets, `keeper_smaller` (expected
   empty, printed as "0, as required"), candidate-only sets (closest dhash
   distance first, up to `--sample`).
3. The page index for the full exact listing.

A set card shows every member as a thumbnail with the keeper outlined,
and beneath each: original filename, `file_size`, `photos_date`, the
arbitrated date if it differs, `lost_on` or `keeper_reason`, and the
`carries` summary. Date fields use three colours: agree, conflict,
missing, as the parent spec asks.

`report.py` is already 625 lines and about to grow. It is split: the
Markdown survey and duplicate reports stay in `report.py`; the HTML
review report is a new `review.py`, and the SQL summary queries both use
move to `queries.py`. Nothing else in `report.py` changes beyond the
deletions Task C requires.

### Tests

- Rendering is tested against a tiny synthetic catalog (three sets: an
  exact pair with a rescued date, a size-spread triple, a candidate pair)
  by parsing the output with `html.parser` and asserting the keeper is
  marked, the flags appear, and every `<img>` resolves to a file.
- A missing `thumb_path` renders a placeholder, never a broken image.
- The index totals must equal the `decision` table counts.

## Task E — CLI and stage bookkeeping

`cli.py` gains `sets`, `arbitrate`, `review`. Each records a `stage` row.
`group` runs `sets` automatically at its end; `sets` as a separate command
exists for re-runs after a manual fix. `arbitrate` refuses unless `group`
is complete; `review` refuses unless `arbitrate` is complete. Both accept
`--force` with the same wording as `group`.

`STATE.md` is updated at the end of the phase with the arbitrate totals
and the review report location, following the Phase 2 pattern.

## Error handling

- A file that vanishes between hash and group (prefix rule reads it) is
  logged and skipped; the truncated row keeps its status and simply forms
  no prefix group.
- A set with zero intact members (all truncated) still gets a keeper by
  the remaining rules and is flagged `truncated_member`; it is never
  resolvable.
- Arbitrate over an empty `dup_set` table completes with zero decisions
  and says so; it does not treat that as an error, because a library with
  no duplicates is a valid outcome.
- Any exception inside per-set arbitration aborts the run and rolls back,
  since decisions are cheap to recompute and a half-written decision table
  is worse than none.

## Testing strategy

Unchanged from Phases 1 and 2: pure functions unit-tested on synthetic
data, every rule with a test that tries to make it choose wrong, one
end-to-end test that builds a fixture catalog through `survey` (stubbed
library) → `hash` → `group` → `arbitrate` → `review` and inspects the
HTML. Fixtures for truncated JPEGs are generated in-test by cutting a
Pillow-produced JPEG before its EOI, so no binary fixtures are committed.

## Expected results on this library

Recorded so the real run can be checked against the design:

- Exact sets: 16,846 + up to 8 prefix sets (the three FUJI, two IMAG, the
  burst cover; the 2013 photo and the JSON PNG are not byte-prefix matches
  and stay out). Removable: 16,871 + those.
- Dates rescued (`date_changes = 1`): on the order of 570.
- `size_spread` sets: 0 by the Phase 2 count; the flag must print zero.
- `keeper_smaller`: must be 0.
- Unique damaged files: 8 (six burst frames, two 2016 photos) plus the
  JSON PNG as an error.

If the real numbers disagree with these by more than the prefix rule can
explain, stop and find out why before sampling the report.

## Process

Subagent-driven development as in Phases 1 and 2: one implementer per
task with the plan carrying complete code; one reviewer per task on a
mid-tier model, instructed to hunt for false positives; a whole-branch
review on the most capable model at the end. Tasks A → B → C → D → E run
sequentially because B depends on A's status value, C on B's tables, D on
C's tables, and E touches `cli.py` for all of them.
