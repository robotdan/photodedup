# Phase 1 Spike Findings — 2026-09-18

## Spike 1: osxphotos viability

Already answered prior to this session, in the user's interactive terminal:

```
$ osxphotos query --count
65971
```

`osxphotos query --count` returns 65971 when run in the user's interactive
terminal. Inside an agent-spawned shell it fails at `import photoscript` with
`applescript.ScriptError ... found plural class name (-2741)`, because
photoscript compiles AppleScript at import time and that needs a GUI session
context. A later task installs a stub for photoscript, which is safe because
Phase 1 is entirely read-only. With that stub, all 65971 assets load in about
13 seconds.

Reproduced the agent-shell failure directly in this session for the record:

```
$ osxphotos query --count
2026-09-18 10:08:55.142 Python[66138:23147776] Connection Invalid error for service com.apple.hiservices-xpcservice.
2026-09-18 10:08:55.142 Python[66138:23147753] Error received in message reply handler: Connection invalid
2026-09-18 10:08:55.143 Python[66138:23147753] Error received in message reply handler: Connection invalid
2026-09-18 10:08:55.143 Python[66138:23147753] Error received in message reply handler: Connection invalid
2026-09-18 10:08:55.143 Python[66138:23147753] Error received in message reply handler: Connection invalid
2026-09-18 10:08:55.143 Python[66138:23147753] Error received in message reply handler: Connection invalid
Traceback (most recent call last):
  File "/opt/homebrew/bin/osxphotos", line 3, in <module>
    from osxphotos.__main__ import cli_main
  File "/opt/homebrew/Cellar/osxphotos/0.76.1/libexec/lib/python3.13/site-packages/osxphotos/__init__.py", line 17, in <module>
    from .iphoto import (
    ...<6 lines>...
    )
  File "/opt/homebrew/Cellar/osxphotos/0.76.1/libexec/lib/python3.13/site-packages/osxphotos/iphoto.py", line 62, in <module>
    from .photoexporter import PhotoExporter
  File "/opt/homebrew/Cellar/osxphotos/0.76.1/libexec/lib/python3.13/site-packages/osxphotos/photoexporter.py", line 42, in <module>
    import photoscript
  File "/opt/homebrew/Cellar/osxphotos/0.76.1/libexec/lib/python3.13/site-packages/photoscript/__init__.py", line 19, in <module>
    from .script_loader import run_script
  File "/opt/homebrew/Cellar/osxphotos/0.76.1/libexec/lib/python3.13/site-packages/photoscript/script_loader.py", line 102, in <module>
    SCRIPT_OBJ = load_applescript("photoscript")
  File "/opt/homebrew/Cellar/osxphotos/0.76.1/libexec/lib/python3.13/site-packages/photoscript/script_loader.py", line 99, in load_applescript
    return AppleScript(script)
  File "/opt/homebrew/Cellar/osxphotos/0.76.1/libexec/lib/python3.13/site-packages/applescript/__init__.py", line 49, in __init__
    raise ScriptError(errorinfo)
applescript.ScriptError: Expected "given", "with", "without", other parameter name, etc. but found plural class name. (-2741) range=979-984
```

This confirms the sandbox-only nature of the failure and matches the
already-recorded answer exactly.

**Status: resolved.** osxphotos works in the interactive terminal. The
Phase 1 library adapter (Task 6) will stub `photoscript` at import time so
osxphotos's read-only query paths work from an agent shell too.

## Spike 2: osxphotos timewarp (user-owned, not attempted)

Not attempted in this session. This spike requires creating throwaway photos
in Photos.app, selecting one in the GUI, and running a command that depends
on that GUI selection — none of which an agent shell can do. Recorded as an
open item for the user. Exact steps from the brief:

1. Create two throwaway photos in Photos (screenshot anything twice).
2. Select one of them in Photos.
3. Run:
   ```bash
   osxphotos timewarp --date 2003-02-23 --time 09:08:08 --verbose
   ```
4. Record whether the date changed in Photos.
5. Delete the throwaway photos.

This gates the keeper-date-correction step in a later phase. It does not
block Phase 1.

## Spike 3: Apple's Duplicates list

Ran the osxphotos half of the check, which fails for the same sandbox reason
as spike 1:

```
$ osxphotos albums | grep -i duplicate
2026-09-18 10:08:55.562 Python[66142:23147825] Error received in message reply handler: Connection invalid
2026-09-18 10:08:55.562 Python[66142:23147833] Connection Invalid error for service com.apple.hiservices-xpcservice.
2026-09-18 10:08:55.562 Python[66142:23147825] Error received in message reply handler: Connection invalid
2026-09-18 10:08:55.563 Python[66142:23147825] Error received in message reply handler: Connection invalid
2026-09-18 10:08:55.563 Python[66142:23147825] Error received in message reply handler: Connection invalid
2026-09-18 10:08:55.563 Python[66142:23147825] Error received in message reply handler: Connection invalid
Traceback (most recent call last):
  File "/opt/homebrew/bin/osxphotos", line 3, in <module>
    from osxphotos.__main__ import cli_main
  File "/opt/homebrew/Cellar/osxphotos/0.76.1/libexec/lib/python3.13/site-packages/osxphotos/__init__.py", line 17, in <module>
    from .iphoto import (
    ...<6 lines>...
    )
  File "/opt/homebrew/Cellar/osxphotos/0.76.1/libexec/lib/python3.13/site-packages/osxphotos/iphoto.py", line 62, in <module>
    from .photoexporter import PhotoExporter
  File "/opt/homebrew/Cellar/osxphotos/0.76.1/libexec/lib/python3.13/site-packages/osxphotos/photoexporter.py", line 42, in <module>
    import photoscript
  File "/opt/homebrew/Cellar/osxphotos/0.76.1/libexec/lib/python3.13/site-packages/photoscript/__init__.py", line 19, in <module>
    from .script_loader import run_script
  File "/opt/homebrew/Cellar/osxphotos/0.76.1/libexec/lib/python3.13/site-packages/photoscript/script_loader.py", line 102, in <module>
    SCRIPT_OBJ = load_applescript("photoscript")
  File "/opt/homebrew/Cellar/osxphotos/0.76.1/libexec/lib/python3.13/site-packages/photoscript/script_loader.py", line 99, in load_applescript
    return AppleScript(script)
  File "/opt/homebrew/Cellar/osxphotos/0.76.1/libexec/lib/python3.13/site-packages/applescript/__init__.py", line 49, in __init__
    raise ScriptError(errorinfo)
applescript.ScriptError: Expected "given", "with", "without", other parameter name, etc. but found plural class name. (-2741) range=979-984
```

Fell back to reading `Photos.sqlite` directly, as expected and as authorized
(Full Disk Access is granted; read-only queries):

```
$ sqlite3 "/Users/robotdan/Pictures/Photos Library.photoslibrary/database/Photos.sqlite" ".tables" | tr ' ' '\n' | grep -i dup
```

Output: empty (no table named with "dup" in it — there is no dedicated
`ZDUPLICATE...` table).

Then grepped the full schema for duplicate-related columns and indexes, as
suggested:

```
$ sqlite3 "/Users/robotdan/Pictures/Photos Library.photoslibrary/database/Photos.sqlite" ".schema" | grep -i dup
```

Output (columns and indexes found, table definitions truncated to the
relevant fragments):

- `ZASSET.ZDUPLICATEASSETVISIBILITYSTATE` — integer column on the main asset
  table, indexed by `Z_Asset_forAllPhotosPartOfDuplicatedSet` (a partial
  index `WHERE ZDUPLICATEASSETVISIBILITYSTATE <> 0`), and referenced in two
  other composite indexes (`Z_Asset_forUserLibraryAlbumAndPhotoPicker`,
  `Z_Asset_forAllPhotos`, `Z_Asset_forAllPhotosByLibraryScopeParticipationState`,
  `Z_Asset_forAllPhotosPrivateOnly`, `Z_Asset_forSpatialAlbum`,
  `Z_Asset_forAllPhotosHavingCaptureSession`,
  `Z_Asset_forAllPhotosFilteredBySavedAssetType`).
- `ZASSET.ZDUPLICATEMETADATAMATCHINGALBUM` and
  `ZASSET.ZDUPLICATEPERCEPTUALMATCHINGALBUM` — integer foreign-key-like
  columns, each with its own index
  (`ZASSET_ZDUPLICATEMETADATAMATCHINGALBUM_INDEX`,
  `ZASSET_ZDUPLICATEPERCEPTUALMATCHINGALBUM_INDEX`).
- `ZADDITIONALASSETATTRIBUTES.ZDUPLICATEDETECTORPERCEPTUALPROCESSINGSTATE` —
  integer column.
- `ZSCENEPRINT.ZDUPLICATEMATCHINGDATA` and
  `ZSCENEPRINT.ZDUPLICATEMATCHINGALTERNATEDATA` — BLOB columns (perceptual
  hash / fingerprint data used by Photos' own duplicate detector).
- `ZGENERICALBUM.ZDUPLICATETYPE` — integer column on the album table.
- `ZADDITIONALASSETATTRIBUTES.ZSOURCEASSETFORDUPLICATIONIDENTIFIER` and
  `ZSOURCEASSETFORDUPLICATIONSCOPEIDENTIFIER`,
  `ZCLOUDMASTER.ZSOURCEMASTERFORDUPLICATIONIDENTIFIER` and
  `ZSOURCEMASTERFORDUPLICATIONSCOPEIDENTIFIER` — VARCHAR identifiers that
  appear related to iCloud shared-library asset duplication, not local
  dedup.

**Finding:** Photos' own duplicate detector state lives as columns and BLOB
fingerprints on `ZASSET`, `ZADDITIONALASSETATTRIBUTES`, and `ZSCENEPRINT`,
plus a `ZDUPLICATETYPE` marker on `ZGENERICALBUM` (used for the built-in
"Duplicates" smart album), rather than in a single dedicated duplicates
table. This is potentially readable via direct SQLite queries in a future
phase, but it is undocumented internal schema that could change across
Photos versions, and osxphotos itself doesn't expose it as a stable API in
the sandboxed path tested here. As specified, nothing in this plan depends
on this signal; it's recorded for future reference only.

## Spike 4: Google Takeout sidecars

```
$ mdfind -name 'takeout' 2>/dev/null | head -20
```

Output: empty — no results.

```
$ ls /Volumes
ls: /Volumes: Operation not permitted
```

`/Volumes` listing is blocked by macOS at the OS permission level for this
shell (confirmed to persist even with the shell's own sandbox restrictions
disabled, so this is a Terminal/OS-level restriction, not an agent-shell
artifact). No external volumes could be enumerated this way.

**Finding:** No Takeout archives were located by Spotlight search, and the
attached-volumes listing could not be performed from this shell. Per the
brief, this does not block Phase 1 — the `takeout_json` date source simply
yields no candidates until the user locates the archives (if any exist) and
confirms their path. Recorded as an open item for the user to check manually
outside this session (e.g. by browsing `/Volumes` and any backup drives in
Finder or a full Terminal).

## Spike 5: iCloud originals downloaded (user-owned, not attempted)

Not attempted in this session. This requires opening Photos → Settings →
iCloud in the GUI and reading the "Download Originals to this Mac" setting
and status line, which an agent shell cannot do. Recorded as an open item
for the user. Exact steps from the brief:

1. Open Photos → Settings → iCloud.
2. Confirm "Download Originals to this Mac" is selected.
3. Confirm the status line reports the download complete.
4. Record the setting and the reported status.

If originals are still downloading, Phase 1 can still be written and
unit-tested, but the survey must not be run for real until it finishes,
because assets without local originals yield no EXIF. Does not block writing
or testing Phase 1 code.
