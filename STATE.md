# Photo De-duplication — Working State

**Last updated:** 2026-09-18
**Current phase:** Phase 1 (foundation and survey)
**Plan:** docs/superpowers/plans/2026-09-18-photo-dedup-phase1-survey.md
**Spec:** docs/superpowers/specs/2026-09-18-photo-dedup-design.md

## Paths

- Library: /Users/robotdan/Pictures/Photos Library.photoslibrary
- Catalog: ./catalog.sqlite (gitignored)
- Archive: /Volumes/Extreme Pro (unused in Phase 1)

## Cutoff watermark

Not yet set. The survey stage sets it on first run.

## Spike findings

(full detail in docs/superpowers/spikes/2026-09-18-spike-findings.md)

- osxphotos works outside sandbox: YES. `osxphotos query --count` -> 65971.
  Inside an agent-spawned shell it fails at `import photoscript` because
  AppleScript compilation needs a GUI session. `library.py` installs a
  stub for photoscript, which is read-only-safe. Verified: 65971 assets
  load in ~13s.
- osxphotos timewarp can set dates: NOT TESTED — user-owned open item.
  Requires creating throwaway photos in Photos.app, selecting one in the
  GUI, then running `osxphotos timewarp --date 2003-02-23 --time 09:08:08
  --verbose` and confirming the date changed, then deleting the throwaway
  photos. Gates the keeper-date-correction step in a later phase; does not
  block Phase 1.
- Apple Duplicates list readable: PARTIALLY. `osxphotos albums | grep -i
  duplicate` fails in the agent shell with the same photoscript
  AppleScript error as spike 1. Direct sqlite3 query
  (`sqlite3 "/Users/robotdan/Pictures/Photos Library.photoslibrary/database/Photos.sqlite"
  ".tables" | tr ' ' '\n' | grep -i dup`) found no dedicated duplicates
  table, but `.schema | grep -i dup` found duplicate-detector state spread
  across columns: `ZASSET.ZDUPLICATEASSETVISIBILITYSTATE`,
  `ZASSET.ZDUPLICATEMETADATAMATCHINGALBUM`,
  `ZASSET.ZDUPLICATEPERCEPTUALMATCHINGALBUM`,
  `ZADDITIONALASSETATTRIBUTES.ZDUPLICATEDETECTORPERCEPTUALPROCESSINGSTATE`,
  `ZSCENEPRINT.ZDUPLICATEMATCHINGDATA` /
  `ZDUPLICATEMATCHINGALTERNATEDATA`, and `ZGENERICALBUM.ZDUPLICATETYPE`.
  This is undocumented internal schema; nothing in this plan depends on it.
- Takeout sidecars located: NO. `mdfind -name 'takeout'` returned no
  results. `ls /Volumes` failed with "Operation not permitted" (an OS-level
  permission restriction on this shell, confirmed even with the shell
  sandbox disabled) so attached volumes could not be enumerated. User-owned
  open item: check `/Volumes` and backup drives manually in Finder or a
  full Terminal. Does not block Phase 1 — `takeout_json` source simply
  yields no candidates until located.
- iCloud originals fully downloaded: NOT TESTED — user-owned open item.
  Requires opening Photos → Settings → iCloud in the GUI and confirming
  "Download Originals to this Mac" is selected and the status line reports
  the download complete. If still downloading, Phase 1 code can still be
  written and unit-tested, but the real survey run must wait until it
  finishes, because assets without local originals yield no EXIF.

## Progress

- [x] Task 0: Spikes
- [x] Task 1: Project scaffold
- [ ] Task 2: Catalog schema
- [ ] Task 3: Date parsing
- [ ] Task 4: Plausibility and selection
- [ ] Task 5: exiftool batch wrapper
- [ ] Task 6: Photos library adapter
- [ ] Task 7: Survey orchestration
- [ ] Task 8: Survey report and CLI

## Next concrete action

Start Task 2 (catalog schema).

## Open questions

- User must confirm, outside an agent shell: (1) whether `osxphotos
  timewarp` actually changes dates in Photos (spike 2), (2) the current
  iCloud "Download Originals to this Mac" status (spike 5), and (3) whether
  any Google Takeout archives exist on attached volumes (spike 4 could not
  enumerate `/Volumes`). None of these block starting or unit-testing Phase
  1 code; spike 5 blocks running the real survey against the live library
  until originals finish downloading, and spike 2 blocks the later
  keeper-date-correction step (a later phase), not Phase 1.
- AppleScript access to Photos from an agent-spawned shell remains broken
  (photoscript import-time compilation fails). This is mitigated for
  Phase 1 by the photoscript stub in the library adapter (Task 6), since
  Phase 1 is read-only. It will need re-checking before any later `apply`
  stage that calls osxphotos functions requiring photoscript, from within
  an agent shell.
