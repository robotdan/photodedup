# Photo De-duplication, Phase 1: Foundation and Survey — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the project foundation and a read-only `survey` stage that characterises the Photos library — every asset, every candidate capture date with its source, and a report showing where the bad dates came from.

**Architecture:** A `uv`-managed Python package with a Click CLI. All durable state lives in one SQLite catalog; every stage is idempotent and resumable by re-reading row status. Photos library access is isolated behind a thin adapter that yields plain dataclasses, so every stage above it is testable without a real library. Date logic is pure functions over candidate sets with no I/O.

**Tech Stack:** Python 3.13, uv, Click, SQLite (stdlib `sqlite3`), osxphotos, exiftool 13.55, pytest, Pillow.

**Spec:** `docs/superpowers/specs/2026-09-18-photo-dedup-design.md`

## Global Constraints

- Repository root: `/Users/robotdan/dev/tools`. Package lives at `src/photodedup/`.
- Photos library: `/Users/robotdan/Pictures/Photos Library.photoslibrary` (329 GB).
- Archive destination: `/Volumes/Extreme Pro` (1 TB, APFS). Not used in Phase 1.
- Python 3.13. Dependencies managed by `uv`, pinned in `pyproject.toml`.
- Phase 1 is strictly read-only with respect to the Photos library. No stage in this plan may write to it, launch Photos for a write operation, or modify any file inside the library bundle.
- Full Disk Access must be granted to the terminal. Without it every library read fails with `Operation not permitted`.
- Catalog path defaults to `./catalog.sqlite` at the repository root. It is gitignored.
- `STATE.md` at the repository root is updated at the end of every task and committed.
- Dates are stored in the catalog as ISO 8601 strings, naive local time, second precision (`YYYY-MM-DDTHH:MM:SS`). Timezone offsets are stored separately as integer seconds when known.
- Never use a whole-file hash as a duplicate signal. Phase 1 computes no hashes at all.

---

### Task 0: Spikes

This task is investigation, not TDD. Its deliverable is a written findings section in `STATE.md`. Do not start Task 1 until spike 1 has an answer, because a genuine osxphotos failure changes the library adapter design.

**Files:**
- Create: `STATE.md`
- Create: `docs/superpowers/spikes/2026-09-18-spike-findings.md`

- [ ] **Step 1: Spike 1 — osxphotos viability**

In a normal interactive terminal (not a sandboxed agent shell), run:

```bash
osxphotos query --count
```

Record the exact output. If it prints a count, osxphotos works and the earlier
`applescript.ScriptError: ... found plural class name. (-2741)` was an artifact
of the sandbox's inability to reach `com.apple.hiservices-xpcservice` for
AppleScript terminology lookup.

If it genuinely fails, also run:

```bash
osascript -e 'tell application "Photos" to count media items'
```

If that also fails outside the sandbox, AppleScript access to Photos is broken
on this macOS version. Record this. It does not block Phase 1, because the
library adapter in Task 6 falls back to reading `Photos.sqlite` directly, but
it does block the `apply` stage in a later phase, and that must be flagged
prominently in `STATE.md`.

- [ ] **Step 2: Spike 2 — osxphotos timewarp**

Only if spike 1 succeeded. Create two throwaway photos in Photos (screenshot
anything twice). Select one in Photos, then run:

```bash
osxphotos timewarp --date 2003-02-23 --time 09:08:08 --verbose
```

Record whether the date changed in Photos. Then delete the throwaway photos.
This gates the keeper-date-correction step in a later phase.

- [ ] **Step 3: Spike 3 — Apple's Duplicates list**

Determine whether Photos' own duplicate detection is readable. Run:

```bash
osxphotos albums | grep -i duplicate
sqlite3 "/Users/robotdan/Pictures/Photos Library.photoslibrary/database/Photos.sqlite" ".tables" | tr ' ' '\n' | grep -i dup
```

Record what, if anything, is found. Nothing in this plan depends on the answer.
If nothing is found, the signal is dropped.

- [ ] **Step 4: Spike 4 — Google Takeout sidecars**

Check the flash drive and any backup for the original Takeout archives. Run:

```bash
mdfind -name 'takeout' 2>/dev/null | head -20
ls /Volumes
```

If found, record the path and confirm `.json` sidecars are present next to the
media files. If not found, record that and move on. Phase 1 does not block on
this; the `takeout_json` date source simply yields no candidates.

- [ ] **Step 5: Confirm iCloud originals are downloaded**

In Photos, Settings → iCloud, confirm "Download Originals to this Mac" is
selected and that the status line reports the download complete. Record the
setting and the reported status. If originals are still downloading, Phase 1
can still be written and unit-tested, but the survey must not be run for real
until it finishes, because assets without local originals yield no EXIF.

- [ ] **Step 6: Write findings and commit**

Write every result into `docs/superpowers/spikes/2026-09-18-spike-findings.md`
with the exact commands and their exact output. Create `STATE.md` with the
template below, filling in the spike answers.

```markdown
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

(fill in from docs/superpowers/spikes/2026-09-18-spike-findings.md)

- osxphotos works outside sandbox: YES. `osxphotos query --count` -> 65971.
  Inside an agent-spawned shell it fails at `import photoscript` because
  AppleScript compilation needs a GUI session. `library.py` installs a
  stub for photoscript, which is read-only-safe. Verified: 65971 assets
  load in ~13s.
- osxphotos timewarp can set dates: ?
- Apple Duplicates list readable: ?
- Takeout sidecars located: ?
- iCloud originals fully downloaded: ?

## Progress

- [x] Task 0: Spikes
- [ ] Task 1: Project scaffold
- [ ] Task 2: Catalog schema
- [ ] Task 3: Date parsing
- [ ] Task 4: Plausibility and selection
- [ ] Task 5: exiftool batch wrapper
- [ ] Task 6: Photos library adapter
- [ ] Task 7: Survey orchestration
- [ ] Task 8: Survey report and CLI

## Next concrete action

Start Task 1.

## Open questions

(none yet)
```

```bash
git add STATE.md docs/superpowers/spikes/
git commit -m "docs: record phase 1 spike findings"
```

Note: Task 1 creates the git repository. If it does not exist yet, run Task 1
Step 1 before this commit.

---

### Task 1: Project scaffold

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `src/photodedup/__init__.py`
- Test: `tests/test_scaffold.py`

**Interfaces:**
- Consumes: nothing.
- Produces: an installed `photodedup` package importable in tests, and a `photodedup` console script entry point.

- [ ] **Step 1: Initialise the repository**

```bash
cd /Users/robotdan/dev/tools
git init
```

- [ ] **Step 2: Write `.gitignore`**

```
catalog.sqlite
catalog.sqlite-wal
catalog.sqlite-shm
.venv/
__pycache__/
*.pyc
.pytest_cache/
reports/
```

- [ ] **Step 3: Write `pyproject.toml`**

```toml
[project]
name = "photodedup"
version = "0.1.0"
description = "De-duplicate an Apple Photos library while preserving the most accurate metadata"
requires-python = ">=3.13"
dependencies = [
    "click>=8.1",
    "osxphotos>=0.76.1",
    "pillow>=11.0",
    "pillow-heif>=0.20",
]

[project.scripts]
photodedup = "photodedup.cli:main"

[dependency-groups]
dev = [
    "pytest>=8.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/photodedup"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 4: Write the failing test**

`tests/test_scaffold.py`:

```python
def test_package_imports():
    import photodedup

    assert photodedup.__version__ == "0.1.0"
```

- [ ] **Step 5: Run the test to verify it fails**

```bash
uv sync
uv run pytest tests/test_scaffold.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'photodedup'` or
`AttributeError: module 'photodedup' has no attribute '__version__'`.

- [ ] **Step 6: Write the minimal implementation**

`src/photodedup/__init__.py`:

```python
__version__ = "0.1.0"
```

- [ ] **Step 7: Run the test to verify it passes**

```bash
uv run pytest tests/test_scaffold.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add .gitignore pyproject.toml uv.lock src/photodedup/__init__.py tests/test_scaffold.py
git commit -m "feat: scaffold photodedup package"
```

---

### Task 2: Catalog schema

**Files:**
- Create: `src/photodedup/catalog.py`
- Test: `tests/test_catalog.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `open_catalog(path: Path) -> sqlite3.Connection` — opens, enables WAL and foreign keys, applies the schema if absent, returns a connection with `row_factory = sqlite3.Row`.
  - `SCHEMA_VERSION: int`
  - `set_run(conn, *, library_path: str, cutoff_date_added: str) -> None`
  - `get_run(conn) -> sqlite3.Row | None`
  - `set_stage(conn, name: str, status: str, notes: str | None = None) -> None`
  - `get_stage(conn, name: str) -> sqlite3.Row | None`

- [ ] **Step 1: Write the failing test**

`tests/test_catalog.py`:

```python
import sqlite3

import pytest

from photodedup.catalog import (
    SCHEMA_VERSION,
    get_run,
    get_stage,
    open_catalog,
    set_run,
    set_stage,
)


@pytest.fixture
def conn(tmp_path):
    return open_catalog(tmp_path / "catalog.sqlite")


def test_schema_is_created(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"asset", "date_candidate", "run", "stage", "schema_meta"} <= tables


def test_schema_version_is_recorded(conn):
    assert conn.execute("SELECT version FROM schema_meta").fetchone()["version"] == SCHEMA_VERSION


def test_open_is_idempotent(tmp_path):
    path = tmp_path / "catalog.sqlite"
    open_catalog(path).close()
    conn = open_catalog(path)
    assert conn.execute("SELECT version FROM schema_meta").fetchone()["version"] == SCHEMA_VERSION


def test_run_row_is_singleton(conn):
    set_run(conn, library_path="/a", cutoff_date_added="2026-09-18T00:00:00")
    set_run(conn, library_path="/b", cutoff_date_added="2026-09-19T00:00:00")
    assert conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"] == 1
    assert get_run(conn)["library_path"] == "/b"


def test_get_run_is_none_when_unset(conn):
    assert get_run(conn) is None


def test_stage_status_round_trips(conn):
    set_stage(conn, "survey", "running")
    set_stage(conn, "survey", "complete", notes="42 assets")
    row = get_stage(conn, "survey")
    assert row["status"] == "complete"
    assert row["notes"] == "42 assets"
    assert row["started_at"] is not None
    assert row["finished_at"] is not None


def test_stage_finished_at_only_set_on_terminal_status(conn):
    set_stage(conn, "hash", "running")
    assert get_stage(conn, "hash")["finished_at"] is None


def test_date_candidate_requires_known_source(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO date_candidate (uuid, source, value) VALUES ('u1', 'astrology', NULL)"
        )


def test_date_candidate_cascades_on_asset_delete(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    conn.execute(
        "INSERT INTO date_candidate (uuid, source, value) VALUES ('u1', 'exif', '2003-02-23T09:08:08')"
    )
    conn.execute("DELETE FROM asset WHERE uuid = 'u1'")
    assert conn.execute("SELECT COUNT(*) AS n FROM date_candidate").fetchone()["n"] == 0
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_catalog.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'photodedup.catalog'`.

- [ ] **Step 3: Write the implementation**

`src/photodedup/catalog.py`:

```python
"""SQLite catalog: the durable spine every pipeline stage reads and writes."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1

DATE_SOURCES = ("takeout_json", "exif", "filename", "photos_db", "file_mtime")

TERMINAL_STAGE_STATUSES = ("complete", "failed")

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS run (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    library_path      TEXT NOT NULL,
    cutoff_date_added TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stage (
    name        TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS asset (
    uuid              TEXT PRIMARY KEY,
    filename          TEXT,
    original_filename TEXT,
    uti               TEXT,
    kind              TEXT,
    width             INTEGER,
    height            INTEGER,
    file_size         INTEGER,
    path              TEXT,
    date_added        TEXT,
    photos_date       TEXT,
    photos_tz_offset  INTEGER,
    file_mtime        TEXT,
    is_live           INTEGER NOT NULL DEFAULT 0,
    has_raw           INTEGER NOT NULL DEFAULT 0,
    has_adjustments   INTEGER NOT NULL DEFAULT 0,
    is_favorite       INTEGER NOT NULL DEFAULT 0,
    is_hidden         INTEGER NOT NULL DEFAULT 0,
    in_trash          INTEGER NOT NULL DEFAULT 0,
    title             TEXT,
    description       TEXT,
    latitude          REAL,
    longitude         REAL,
    keywords          TEXT,
    albums            TEXT,
    import_uuid       TEXT,
    survey_status     TEXT NOT NULL DEFAULT 'pending',
    survey_error      TEXT
);

CREATE INDEX IF NOT EXISTS asset_survey_status ON asset (survey_status);
CREATE INDEX IF NOT EXISTS asset_date_added ON asset (date_added);

CREATE TABLE IF NOT EXISTS date_candidate (
    uuid            TEXT NOT NULL REFERENCES asset (uuid) ON DELETE CASCADE,
    source          TEXT NOT NULL CHECK (source IN {DATE_SOURCES!r}),
    value           TEXT,
    raw             TEXT,
    rejected_reason TEXT,
    PRIMARY KEY (uuid, source)
);
"""


def open_catalog(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the catalog at *path* with the schema applied."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    if conn.execute("SELECT COUNT(*) AS n FROM schema_meta").fetchone()["n"] == 0:
        conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def set_run(conn: sqlite3.Connection, *, library_path: str, cutoff_date_added: str) -> None:
    conn.execute(
        """
        INSERT INTO run (id, library_path, cutoff_date_added, created_at)
        VALUES (1, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            library_path = excluded.library_path,
            cutoff_date_added = excluded.cutoff_date_added
        """,
        (library_path, cutoff_date_added, _now()),
    )
    conn.commit()


def get_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM run WHERE id = 1").fetchone()


def set_stage(
    conn: sqlite3.Connection, name: str, status: str, notes: str | None = None
) -> None:
    finished = _now() if status in TERMINAL_STAGE_STATUSES else None
    conn.execute(
        """
        INSERT INTO stage (name, status, started_at, finished_at, notes)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (name) DO UPDATE SET
            status = excluded.status,
            finished_at = excluded.finished_at,
            notes = excluded.notes
        """,
        (name, status, _now(), finished, notes),
    )
    conn.commit()


def get_stage(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM stage WHERE name = ?", (name,)).fetchone()
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_catalog.py -v
```

Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/catalog.py tests/test_catalog.py
git commit -m "feat: add SQLite catalog schema and accessors"
```

---

### Task 3: Date parsing

**Files:**
- Create: `src/photodedup/dates.py`
- Test: `tests/test_dates_parsing.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `parse_exif_datetime(raw: str | None) -> datetime | None` — parses EXIF-style `YYYY:MM:DD HH:MM:SS`, tolerating a subsecond suffix and a trailing timezone offset, returning naive local-time `datetime`. Returns `None` for zeroed or unparseable values.
  - `parse_filename_datetime(filename: str) -> datetime | None`
  - `FILENAME_PATTERNS: tuple[re.Pattern, ...]`

- [ ] **Step 1: Write the failing test**

`tests/test_dates_parsing.py`:

```python
from datetime import datetime

import pytest

from photodedup.dates import parse_exif_datetime, parse_filename_datetime


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2003:02:23 09:08:08", datetime(2003, 2, 23, 9, 8, 8)),
        ("2003:02:23 09:08:08.123", datetime(2003, 2, 23, 9, 8, 8)),
        ("2003:02:23 09:08:08-07:00", datetime(2003, 2, 23, 9, 8, 8)),
        ("2003:02:23 09:08:08.45+02:00", datetime(2003, 2, 23, 9, 8, 8)),
        ("2020-12-18 09:12:48", datetime(2020, 12, 18, 9, 12, 48)),
    ],
)
def test_parse_exif_datetime_accepts_real_values(raw, expected):
    assert parse_exif_datetime(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "    ",
        "0000:00:00 00:00:00",
        "0000:00:00",
        "not a date",
        "2003:02:30 09:08:08",
    ],
)
def test_parse_exif_datetime_rejects_junk(raw):
    assert parse_exif_datetime(raw) is None


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("IMG_20140612_123456.jpg", datetime(2014, 6, 12, 12, 34, 56)),
        ("VID_20140612_123456.mp4", datetime(2014, 6, 12, 12, 34, 56)),
        ("PXL_20210103_180520123.jpg", datetime(2021, 1, 3, 18, 5, 20)),
        ("Screenshot_20200101-123456.png", datetime(2020, 1, 1, 12, 34, 56)),
        ("2014-06-12 12.34.56.jpg", datetime(2014, 6, 12, 12, 34, 56)),
        ("20140612_123456.jpg", datetime(2014, 6, 12, 12, 34, 56)),
        ("IMG-20140612-WA0001.jpg", datetime(2014, 6, 12, 0, 0, 0)),
    ],
)
def test_parse_filename_datetime_finds_embedded_dates(filename, expected):
    assert parse_filename_datetime(filename) == expected


@pytest.mark.parametrize(
    "filename",
    [
        "slickrock mike and dan(1).jpg",
        "DSC_0123.jpg",
        "IMG_1234.HEIC",
        "photo.jpg",
        "IMG_20149999_123456.jpg",
    ],
)
def test_parse_filename_datetime_returns_none_without_a_date(filename):
    assert parse_filename_datetime(filename) is None


def test_parse_filename_datetime_ignores_implausible_year():
    assert parse_filename_datetime("IMG_18140612_123456.jpg") is None
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_dates_parsing.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'photodedup.dates'`.

- [ ] **Step 3: Write the implementation**

`src/photodedup/dates.py`:

```python
"""Pure date extraction and arbitration. No I/O lives in this module."""

from __future__ import annotations

import re
from datetime import datetime

# Years outside this range are never a real capture date for this library.
MIN_PLAUSIBLE_YEAR = 1900
MAX_PLAUSIBLE_YEAR = 2100

_EXIF_RE = re.compile(
    r"^\s*(?P<y>\d{4})[:\-](?P<mo>\d{2})[:\-](?P<d>\d{2})"
    r"[ T](?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})"
    r"(?:\.\d+)?"
    r"(?:\s*(?:Z|[+\-]\d{2}:?\d{2}))?\s*$"
)

FILENAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    # IMG_20140612_123456, VID_20140612_123456, PXL_20210103_180520123
    re.compile(r"(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})[_\-](?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})"),
    # Screenshot_20200101-123456
    re.compile(r"(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})-(?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})"),
    # 2014-06-12 12.34.56
    re.compile(
        r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[ _](?P<h>\d{2})\.(?P<mi>\d{2})\.(?P<s>\d{2})"
    ),
    # IMG-20140612-WA0001 — date only, no time
    re.compile(r"(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})"),
)


def _build(parts: dict[str, str]) -> datetime | None:
    try:
        value = datetime(
            int(parts["y"]),
            int(parts["mo"]),
            int(parts["d"]),
            int(parts.get("h") or 0),
            int(parts.get("mi") or 0),
            int(parts.get("s") or 0),
        )
    except ValueError:
        return None
    if not MIN_PLAUSIBLE_YEAR <= value.year <= MAX_PLAUSIBLE_YEAR:
        return None
    return value


def parse_exif_datetime(raw: str | None) -> datetime | None:
    """Parse an EXIF or QuickTime datetime string into naive local time."""
    if not raw or not raw.strip():
        return None
    match = _EXIF_RE.match(raw)
    if match is None:
        return None
    return _build(match.groupdict())


def parse_filename_datetime(filename: str) -> datetime | None:
    """Extract a capture datetime embedded in *filename*, if one is present."""
    for pattern in FILENAME_PATTERNS:
        match = pattern.search(filename)
        if match is None:
            continue
        value = _build(match.groupdict())
        if value is not None:
            return value
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_dates_parsing.py -v
```

Expected: PASS. If `test_parse_filename_datetime_returns_none_without_a_date`
fails on `IMG_20149999_123456.jpg`, confirm `_build` is returning `None` for the
invalid month `99` and that the loop continues rather than returning early.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/dates.py tests/test_dates_parsing.py
git commit -m "feat: parse EXIF and filename capture dates"
```

---

### Task 4: Plausibility filtering and winner selection

**Files:**
- Modify: `src/photodedup/dates.py`
- Test: `tests/test_dates_arbitration.py`

**Interfaces:**
- Consumes: `photodedup.dates` from Task 3.
- Produces:
  - `Candidate` frozen dataclass with fields `source: str`, `value: datetime | None`, `raw: str | None`.
  - `Decision` frozen dataclass with fields `value: datetime | None`, `source: str | None`, `rejected: tuple[tuple[str, str], ...]`, `needs_review: bool`, `review_reason: str | None`.
  - `SOURCE_RANK: dict[str, int]`
  - `SENTINEL_DATES: tuple[datetime, ...]`
  - `rejection_reason(candidate, *, date_added, file_mtime, floor, now) -> str | None`
  - `choose_date(candidates, *, date_added, file_mtime, floor=None, now=None, margin=timedelta(hours=26)) -> Decision`

- [ ] **Step 1: Write the failing test**

`tests/test_dates_arbitration.py`:

```python
from datetime import datetime, timedelta

import pytest

from photodedup.dates import Candidate, choose_date, rejection_reason

ADDED = datetime(2020, 12, 18, 9, 12, 48)
MTIME = datetime(2019, 5, 5, 5, 5, 5)
NOW = datetime(2026, 9, 18, 12, 0, 0)


def reason(value, **kwargs):
    kwargs.setdefault("date_added", ADDED)
    kwargs.setdefault("file_mtime", MTIME)
    kwargs.setdefault("floor", None)
    kwargs.setdefault("now", NOW)
    return rejection_reason(Candidate("exif", value, None), **kwargs)


def test_plausible_date_has_no_rejection_reason():
    assert reason(datetime(2003, 2, 23, 9, 8, 8)) is None


def test_rejects_date_at_or_after_import():
    assert reason(ADDED) == "at_or_after_date_added"
    assert reason(ADDED + timedelta(seconds=1)) == "at_or_after_date_added"


def test_accepts_date_one_second_before_import():
    assert reason(ADDED - timedelta(seconds=1)) is None


def test_rejects_future_date():
    assert reason(datetime(2030, 1, 1), date_added=None) == "future"


@pytest.mark.parametrize(
    "value",
    [
        datetime(1970, 1, 1, 0, 0, 0),
        datetime(1970, 1, 1, 23, 59, 59),
        datetime(1904, 1, 1, 6, 0, 0),
        datetime(1980, 1, 1, 0, 0, 0),
        datetime(2001, 1, 1, 0, 0, 0),
        datetime(1899, 12, 30, 0, 0, 0),
    ],
)
def test_rejects_sentinel_dates(value):
    assert reason(value) == "sentinel"


def test_rejects_date_equal_to_file_mtime():
    assert reason(MTIME) == "equals_file_mtime"


def test_rejects_date_below_floor():
    assert reason(datetime(1950, 1, 1), floor=datetime(1990, 1, 1)) == "below_floor"


def test_none_value_is_rejected_as_unparseable():
    assert reason(None) == "unparseable"


def test_earliest_surviving_candidate_wins():
    decision = choose_date(
        [
            Candidate("photos_db", ADDED, None),
            Candidate("exif", datetime(2003, 2, 23, 9, 8, 8), None),
        ],
        date_added=ADDED,
        file_mtime=MTIME,
        now=NOW,
    )
    assert decision.value == datetime(2003, 2, 23, 9, 8, 8)
    assert decision.source == "exif"
    assert ("photos_db", "at_or_after_date_added") in decision.rejected


def test_source_rank_breaks_exact_ties():
    when = datetime(2003, 2, 23, 9, 8, 8)
    decision = choose_date(
        [Candidate("filename", when, None), Candidate("takeout_json", when, None)],
        date_added=ADDED,
        file_mtime=MTIME,
        now=NOW,
    )
    assert decision.source == "takeout_json"


def test_no_surviving_candidate_yields_empty_decision():
    decision = choose_date(
        [Candidate("photos_db", ADDED, None)],
        date_added=ADDED,
        file_mtime=MTIME,
        now=NOW,
    )
    assert decision.value is None
    assert decision.source is None
    assert decision.needs_review is True
    assert decision.review_reason == "no_plausible_candidate"


def test_wide_disagreement_flags_review_but_still_picks():
    decision = choose_date(
        [
            Candidate("exif", datetime(2003, 2, 23, 9, 8, 8), None),
            Candidate("filename", datetime(2011, 7, 4, 10, 0, 0), None),
        ],
        date_added=ADDED,
        file_mtime=MTIME,
        now=NOW,
    )
    assert decision.value == datetime(2003, 2, 23, 9, 8, 8)
    assert decision.needs_review is True
    assert decision.review_reason == "candidates_disagree"


def test_timezone_sized_disagreement_does_not_flag_review():
    decision = choose_date(
        [
            Candidate("exif", datetime(2003, 2, 23, 9, 8, 8), None),
            Candidate("photos_db", datetime(2003, 2, 23, 17, 8, 8), None),
        ],
        date_added=ADDED,
        file_mtime=MTIME,
        now=NOW,
    )
    assert decision.needs_review is False


def test_single_surviving_candidate_does_not_flag_review():
    decision = choose_date(
        [Candidate("exif", datetime(2003, 2, 23, 9, 8, 8), None)],
        date_added=ADDED,
        file_mtime=MTIME,
        now=NOW,
    )
    assert decision.needs_review is False
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_dates_arbitration.py -v
```

Expected: FAIL with `ImportError: cannot import name 'Candidate' from 'photodedup.dates'`.

- [ ] **Step 3: Write the implementation**

Append to `src/photodedup/dates.py`:

```python
from dataclasses import dataclass
from datetime import timedelta

SOURCE_RANK: dict[str, int] = {
    "takeout_json": 0,
    "exif": 1,
    "filename": 2,
    "photos_db": 3,
    "file_mtime": 4,
}

# Values that software substitutes when it has no real date. Any candidate
# landing within SENTINEL_WINDOW of one of these is discarded.
SENTINEL_DATES: tuple[datetime, ...] = (
    datetime(1970, 1, 1),   # Unix epoch
    datetime(1904, 1, 1),   # QuickTime epoch
    datetime(1980, 1, 1),   # FAT epoch
    datetime(2001, 1, 1),   # Apple reference epoch
    datetime(1899, 12, 30), # OLE automation epoch
)
SENTINEL_WINDOW = timedelta(days=1)

DEFAULT_DISAGREEMENT_MARGIN = timedelta(hours=26)


@dataclass(frozen=True)
class Candidate:
    source: str
    value: datetime | None
    raw: str | None = None


@dataclass(frozen=True)
class Decision:
    value: datetime | None
    source: str | None
    rejected: tuple[tuple[str, str], ...]
    needs_review: bool
    review_reason: str | None


def rejection_reason(
    candidate: Candidate,
    *,
    date_added: datetime | None,
    file_mtime: datetime | None,
    floor: datetime | None = None,
    now: datetime | None = None,
) -> str | None:
    """Return why *candidate* cannot be trusted, or None if it survives."""
    value = candidate.value
    if value is None:
        return "unparseable"
    for sentinel in SENTINEL_DATES:
        if abs(value - sentinel) < SENTINEL_WINDOW:
            return "sentinel"
    if (now or datetime.now()) < value:
        return "future"
    # A capture always predates its own import into the library.
    if date_added is not None and value >= date_added:
        return "at_or_after_date_added"
    if file_mtime is not None and value == file_mtime:
        return "equals_file_mtime"
    if floor is not None and value < floor:
        return "below_floor"
    return None


def choose_date(
    candidates: list[Candidate],
    *,
    date_added: datetime | None,
    file_mtime: datetime | None,
    floor: datetime | None = None,
    now: datetime | None = None,
    margin: timedelta = DEFAULT_DISAGREEMENT_MARGIN,
) -> Decision:
    """Pick the winning capture date: earliest survivor, source rank breaking ties."""
    survivors: list[Candidate] = []
    rejected: list[tuple[str, str]] = []
    for candidate in candidates:
        why = rejection_reason(
            candidate, date_added=date_added, file_mtime=file_mtime, floor=floor, now=now
        )
        if why is None:
            survivors.append(candidate)
        else:
            rejected.append((candidate.source, why))

    if not survivors:
        return Decision(None, None, tuple(rejected), True, "no_plausible_candidate")

    winner = min(
        survivors,
        key=lambda c: (c.value, SOURCE_RANK.get(c.source, len(SOURCE_RANK))),
    )
    spread = max(c.value for c in survivors) - min(c.value for c in survivors)
    disagree = spread > margin
    return Decision(
        winner.value,
        winner.source,
        tuple(rejected),
        disagree,
        "candidates_disagree" if disagree else None,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_dates_arbitration.py tests/test_dates_parsing.py -v
```

Expected: PASS, both files green.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/dates.py tests/test_dates_arbitration.py
git commit -m "feat: add date plausibility filtering and winner selection"
```

---

### Task 5: exiftool batch wrapper

**Files:**
- Create: `src/photodedup/exif.py`
- Test: `tests/test_exif.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `EXIF_TAGS: tuple[str, ...]`
  - `read_metadata(paths: Sequence[Path], *, chunk_size: int = 400, exiftool: str = "exiftool") -> dict[str, dict[str, object]]` — maps absolute path string to the tag dictionary exiftool returned. Paths that exiftool could not read are absent from the result rather than raising.

**Why chunking:** exiftool's startup cost is ~200 ms. Passing hundreds of files
per invocation amortises it. A single invocation with 100,000 paths would blow
the argument limit, so paths are passed through a temporary `-@` argfile.

- [ ] **Step 1: Write the failing test**

`tests/test_exif.py`:

```python
import shutil
import subprocess

import pytest
from PIL import Image

from photodedup.exif import read_metadata

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)


@pytest.fixture
def jpeg_with_date(tmp_path):
    path = tmp_path / "shot.jpg"
    Image.new("RGB", (16, 16), "red").save(path)
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-DateTimeOriginal=2003:02:23 09:08:08",
            "-GPSLatitude=38.5733",
            "-GPSLatitudeRef=N",
            "-GPSLongitude=109.5498",
            "-GPSLongitudeRef=W",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_reads_datetime_original(jpeg_with_date):
    result = read_metadata([jpeg_with_date])
    tags = result[str(jpeg_with_date)]
    assert tags["EXIF:DateTimeOriginal"] == "2003:02:23 09:08:08"


def test_reads_gps_as_signed_decimal(jpeg_with_date):
    tags = read_metadata([jpeg_with_date])[str(jpeg_with_date)]
    assert tags["Composite:GPSLatitude"] == pytest.approx(38.5733, abs=1e-4)
    assert tags["Composite:GPSLongitude"] == pytest.approx(-109.5498, abs=1e-4)


def test_missing_file_is_omitted_not_raised(tmp_path, jpeg_with_date):
    result = read_metadata([jpeg_with_date, tmp_path / "nope.jpg"])
    assert str(jpeg_with_date) in result
    assert str(tmp_path / "nope.jpg") not in result


def test_empty_input_returns_empty_dict():
    assert read_metadata([]) == {}


def test_chunking_covers_every_file(tmp_path):
    paths = []
    for i in range(7):
        path = tmp_path / f"img{i}.jpg"
        Image.new("RGB", (8, 8), "blue").save(path)
        paths.append(path)
    result = read_metadata(paths, chunk_size=2)
    assert len(result) == 7
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_exif.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'photodedup.exif'`.

- [ ] **Step 3: Write the implementation**

`src/photodedup/exif.py`:

```python
"""Batched exiftool reads. One subprocess per chunk of files, not per file."""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

EXIF_TAGS: tuple[str, ...] = (
    "EXIF:DateTimeOriginal",
    "EXIF:CreateDate",
    "EXIF:OffsetTimeOriginal",
    "EXIF:SubSecTimeOriginal",
    "QuickTime:CreateDate",
    "QuickTime:CreationDate",
    "XMP:DateCreated",
    "File:FileModifyDate",
    "File:MIMEType",
    "File:FileSize",
    "Composite:GPSLatitude",
    "Composite:GPSLongitude",
    "EXIF:ImageWidth",
    "EXIF:ImageHeight",
)


def _run_chunk(chunk: Sequence[Path], exiftool: str) -> list[dict[str, object]]:
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False) as handle:
        for path in chunk:
            handle.write(f"{path}\n")
        argfile = handle.name
    try:
        completed = subprocess.run(
            [
                exiftool,
                "-json",
                "-G",
                "-n",             # numeric values, so GPS comes back as signed decimal
                "-charset",
                "filename=utf8",
                "-m",             # ignore minor errors
                *(f"-{tag}" for tag in EXIF_TAGS),
                "-@",
                argfile,
            ],
            capture_output=True,
            text=True,
        )
    finally:
        Path(argfile).unlink(missing_ok=True)

    if not completed.stdout.strip():
        return []
    return json.loads(completed.stdout)


def read_metadata(
    paths: Sequence[Path],
    *,
    chunk_size: int = 400,
    exiftool: str = "exiftool",
) -> dict[str, dict[str, object]]:
    """Read EXIF_TAGS for every readable path. Unreadable paths are omitted."""
    result: dict[str, dict[str, object]] = {}
    for start in range(0, len(paths), chunk_size):
        chunk = paths[start : start + chunk_size]
        for record in _run_chunk(chunk, exiftool):
            source = record.get("SourceFile")
            if source is None:
                continue
            result[str(Path(source))] = record
    return result
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_exif.py -v
```

Expected: PASS, 5 tests. Note that exiftool returns `SourceFile` as the path it
was given; the test compares against `str(path)` for a `tmp_path`-rooted
absolute path, which matches.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/exif.py tests/test_exif.py
git commit -m "feat: add batched exiftool metadata reader"
```

---

### Task 6: Photos library adapter

**Files:**
- Create: `src/photodedup/library.py`
- Test: `tests/test_library.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `AssetRecord` frozen dataclass with fields: `uuid: str`, `filename: str | None`, `original_filename: str | None`, `uti: str | None`, `kind: str` (`"image"` or `"video"`), `width: int | None`, `height: int | None`, `path: str | None`, `date_added: datetime | None`, `photos_date: datetime | None`, `photos_tz_offset: int | None`, `is_live: bool`, `has_raw: bool`, `has_adjustments: bool`, `is_favorite: bool`, `is_hidden: bool`, `in_trash: bool`, `title: str | None`, `description: str | None`, `latitude: float | None`, `longitude: float | None`, `keywords: tuple[str, ...]`, `albums: tuple[str, ...]`, `import_uuid: str | None`.
  - `iter_assets(library_path: Path) -> Iterator[AssetRecord]`
  - `LibraryUnavailable(RuntimeError)` — raised when the library cannot be opened, carrying a message that names Full Disk Access as the likely cause.

**Design note:** everything downstream consumes `AssetRecord`, never an
osxphotos object. That keeps Task 7 testable with hand-built records and means a
future fallback that reads `Photos.sqlite` directly only has to satisfy this one
interface.

- [ ] **Step 1: Write the failing test**

`tests/test_library.py`:

```python
from datetime import datetime
from pathlib import Path

import pytest

from photodedup.library import AssetRecord, LibraryUnavailable, iter_assets


def test_asset_record_is_hashable_and_frozen():
    record = AssetRecord(uuid="u1", kind="image")
    assert hash(record) is not None
    with pytest.raises(AttributeError):
        record.uuid = "u2"


def test_asset_record_defaults_are_safe():
    record = AssetRecord(uuid="u1", kind="image")
    assert record.keywords == ()
    assert record.albums == ()
    assert record.path is None
    assert record.is_live is False


def test_missing_library_raises_library_unavailable(tmp_path):
    with pytest.raises(LibraryUnavailable) as excinfo:
        list(iter_assets(tmp_path / "Nope.photoslibrary"))
    assert "Full Disk Access" in str(excinfo.value)


def test_photoscript_stub_lets_osxphotos_import():
    """osxphotos must import even where AppleScript compilation is impossible."""
    from photodedup.library import _install_photoscript_stub

    _install_photoscript_stub()
    import osxphotos  # noqa: F401


def test_asset_record_accepts_full_payload():
    record = AssetRecord(
        uuid="u1",
        filename="IMG_0001.jpg",
        original_filename="slickrock mike and dan(1).jpg",
        uti="public.jpeg",
        kind="image",
        width=2048,
        height=1536,
        path="/tmp/IMG_0001.jpg",
        date_added=datetime(2020, 12, 18, 9, 12, 48),
        photos_date=datetime(2003, 2, 23, 9, 8, 8),
        photos_tz_offset=-25200,
        keywords=("utah", "moab"),
        albums=("Trips",),
    )
    assert record.original_filename == "slickrock mike and dan(1).jpg"
    assert record.keywords == ("utah", "moab")
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_library.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'photodedup.library'`.

- [ ] **Step 3: Write the implementation**

`src/photodedup/library.py`:

```python
"""Read-only adapter over the Photos library.

Downstream stages consume AssetRecord and never touch osxphotos directly, so
they stay testable without a real library and a future direct-SQLite fallback
only has to satisfy this interface.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


class LibraryUnavailable(RuntimeError):
    """The Photos library could not be opened."""


@dataclass(frozen=True)
class AssetRecord:
    uuid: str
    kind: str
    filename: str | None = None
    original_filename: str | None = None
    uti: str | None = None
    width: int | None = None
    height: int | None = None
    path: str | None = None
    date_added: datetime | None = None
    photos_date: datetime | None = None
    photos_tz_offset: int | None = None
    is_live: bool = False
    has_raw: bool = False
    has_adjustments: bool = False
    is_favorite: bool = False
    is_hidden: bool = False
    in_trash: bool = False
    title: str | None = None
    description: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    keywords: tuple[str, ...] = field(default=())
    albums: tuple[str, ...] = field(default=())
    import_uuid: str | None = None


def _install_photoscript_stub() -> None:
    """Neutralise photoscript's import-time AppleScript compilation.

    osxphotos imports photoscript unconditionally, and photoscript compiles its
    AppleScript at module load. That compilation needs a GUI session context,
    which a headless or agent-spawned shell does not have, so the import raises
    `applescript.ScriptError ... found plural class name (-2741)` and takes all
    of osxphotos down with it, including the purely read-only database access
    this module needs.

    photoscript is only used for write operations, and Phase 1 performs none, so
    a stub whose every attribute raises on instantiation is safe. If the real
    module already imported successfully, leave it alone.
    """
    import sys
    import types

    if "photoscript" in sys.modules:
        return

    class _Unavailable:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError(
                "photoscript is unavailable in this process. Phase 1 is read-only "
                "and does not need it; a stage that writes to Photos must run "
                "from an interactive terminal session."
            )

    for name in ("photoscript", "photoscript.script_loader"):
        module = types.ModuleType(name)
        module.__getattr__ = lambda _attr: _Unavailable  # type: ignore[method-assign]
        sys.modules[name] = module
    sys.modules["photoscript.script_loader"].run_script = lambda *a, **k: None


def _naive(value: datetime | None) -> datetime | None:
    """Drop tzinfo, keeping wall-clock time as the library displays it."""
    if value is None:
        return None
    return value.replace(tzinfo=None, microsecond=0)


def _tz_offset(value: datetime | None) -> int | None:
    if value is None or value.utcoffset() is None:
        return None
    return int(value.utcoffset().total_seconds())


def iter_assets(library_path: Path) -> Iterator[AssetRecord]:
    """Yield an AssetRecord for every asset in the library at *library_path*."""
    if not Path(library_path).exists():
        raise LibraryUnavailable(
            f"Cannot open {library_path}. If the path is correct, this is almost "
            "always missing Full Disk Access for the terminal application. Grant "
            "it in System Settings, Privacy & Security, Full Disk Access, then "
            "quit and reopen the terminal."
        )

    _install_photoscript_stub()
    try:
        import osxphotos
    except Exception as exc:  # pragma: no cover - import-time environment failure
        raise LibraryUnavailable(f"osxphotos could not be imported: {exc}") from exc

    try:
        db = osxphotos.PhotosDB(str(library_path))
    except Exception as exc:
        raise LibraryUnavailable(
            f"osxphotos could not open {library_path}: {exc}. Check Full Disk Access."
        ) from exc

    for photo in db.photos(movies=True, intrash=False):
        yield AssetRecord(
            uuid=photo.uuid,
            kind="video" if photo.ismovie else "image",
            filename=photo.filename,
            original_filename=photo.original_filename,
            uti=photo.uti,
            width=photo.width,
            height=photo.height,
            path=photo.path,
            date_added=_naive(photo.date_added),
            photos_date=_naive(photo.date),
            photos_tz_offset=_tz_offset(photo.date),
            is_live=bool(photo.live_photo),
            has_raw=bool(photo.has_raw),
            has_adjustments=bool(photo.hasadjustments),
            is_favorite=bool(photo.favorite),
            is_hidden=bool(photo.hidden),
            in_trash=bool(photo.intrash),
            title=photo.title,
            description=photo.description,
            latitude=photo.location[0] if photo.location else None,
            longitude=photo.location[1] if photo.location else None,
            keywords=tuple(photo.keywords or ()),
            albums=tuple(photo.albums or ()),
            import_uuid=getattr(photo, "import_uuid", None),
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_library.py -v
```

Expected: PASS, 4 tests. None of them import osxphotos, so they pass regardless
of the spike 1 outcome.

- [ ] **Step 5: Smoke-test against the real library**

```bash
uv run python -c "
from pathlib import Path
from itertools import islice
from photodedup.library import iter_assets
for a in islice(iter_assets(Path('/Users/robotdan/Pictures/Photos Library.photoslibrary')), 3):
    print(a.uuid, a.kind, a.original_filename, a.photos_date, a.date_added, a.path is not None)
"
```

Expected: three asset lines. If this raises `LibraryUnavailable`, stop and
resolve Full Disk Access or the spike 1 osxphotos problem before continuing.
Record the outcome in `STATE.md`.

- [ ] **Step 6: Commit**

```bash
git add src/photodedup/library.py tests/test_library.py
git commit -m "feat: add read-only Photos library adapter"
```

---

### Task 7: Survey orchestration

**Files:**
- Create: `src/photodedup/survey.py`
- Test: `tests/test_survey.py`

**Interfaces:**
- Consumes: `photodedup.catalog`, `photodedup.dates`, `photodedup.exif`, `photodedup.library`.
- Produces:
  - `SurveyResult` frozen dataclass with fields `total: int`, `written: int`, `skipped_after_cutoff: int`, `missing_originals: int`, `errors: int`.
  - `run_survey(conn, records, *, library_path, cutoff=None, read_exif=read_metadata, batch_size=400) -> SurveyResult` — `records` is any iterable of `AssetRecord`. `read_exif` is injected so tests do not need exiftool.
  - `MissingOriginalsError(RuntimeError)` — raised when more than `MISSING_ORIGINALS_TOLERANCE` of assets have `path is None`.
  - `MISSING_ORIGINALS_TOLERANCE: float = 0.01`

**Behaviour to implement:**

1. On first run, compute the cutoff as the maximum `date_added` across all records and store it via `set_run`. On a resumed run, reuse the stored cutoff.
2. Skip any record whose `date_added` is greater than the cutoff. Those are photos added after the survey started and are out of scope.
3. Abort with `MissingOriginalsError` if the fraction of in-scope records with `path is None` exceeds the tolerance, because that means iCloud originals are not downloaded and every EXIF read would be meaningless.
4. For each in-scope record, insert an `asset` row and one `date_candidate` row per source, with `rejected_reason` filled in from `rejection_reason`.
5. Resumability: skip records whose `asset` row already has `survey_status = 'ok'`.
6. Per-asset failures set `survey_status = 'error'` with the message in `survey_error` and do not abort the run.

- [ ] **Step 1: Write the failing test**

`tests/test_survey.py`:

```python
import json
from datetime import datetime

import pytest

from photodedup.catalog import get_run, open_catalog
from photodedup.library import AssetRecord
from photodedup.survey import MissingOriginalsError, run_survey

LIBRARY = "/tmp/Fake.photoslibrary"


@pytest.fixture
def conn(tmp_path):
    return open_catalog(tmp_path / "catalog.sqlite")


def record(uuid, *, added, photos_date=None, path="/tmp/x.jpg", filename="x.jpg"):
    return AssetRecord(
        uuid=uuid,
        kind="image",
        filename=filename,
        original_filename=filename,
        path=path,
        date_added=added,
        photos_date=photos_date,
    )


def no_exif(paths, **kwargs):
    return {}


def test_writes_one_asset_row_per_record(conn):
    records = [
        record("u1", added=datetime(2020, 12, 18, 9, 12, 48)),
        record("u2", added=datetime(2020, 12, 18, 9, 12, 49)),
    ]
    result = run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    assert result.written == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM asset").fetchone()["n"] == 2


def test_cutoff_is_max_date_added(conn):
    records = [
        record("u1", added=datetime(2020, 12, 18, 9, 12, 48)),
        record("u2", added=datetime(2026, 9, 18, 8, 0, 0)),
    ]
    run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    assert get_run(conn)["cutoff_date_added"] == "2026-09-18T08:00:00"


def test_records_after_stored_cutoff_are_skipped(conn):
    first = [record("u1", added=datetime(2020, 12, 18, 9, 12, 48))]
    run_survey(conn, first, library_path=LIBRARY, read_exif=no_exif)

    second = first + [record("u2", added=datetime(2026, 9, 18, 8, 0, 0))]
    result = run_survey(conn, second, library_path=LIBRARY, read_exif=no_exif)
    assert result.skipped_after_cutoff == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM asset").fetchone()["n"] == 1


def test_resume_does_not_rewrite_completed_rows(conn):
    records = [record("u1", added=datetime(2020, 12, 18, 9, 12, 48))]
    run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    result = run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    assert result.written == 0
    assert result.total == 1


def test_photos_db_date_becomes_a_candidate(conn):
    records = [
        record(
            "u1",
            added=datetime(2020, 12, 18, 9, 12, 48),
            photos_date=datetime(2003, 2, 23, 9, 8, 8),
        )
    ]
    run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    row = conn.execute(
        "SELECT * FROM date_candidate WHERE uuid='u1' AND source='photos_db'"
    ).fetchone()
    assert row["value"] == "2003-02-23T09:08:08"
    assert row["rejected_reason"] is None


def test_import_time_date_is_recorded_but_rejected(conn):
    added = datetime(2020, 12, 18, 9, 12, 48)
    records = [record("u1", added=added, photos_date=added)]
    run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    row = conn.execute(
        "SELECT * FROM date_candidate WHERE uuid='u1' AND source='photos_db'"
    ).fetchone()
    assert row["rejected_reason"] == "at_or_after_date_added"


def test_filename_date_becomes_a_candidate(conn):
    records = [
        record(
            "u1",
            added=datetime(2020, 12, 18, 9, 12, 48),
            filename="IMG_20140612_123456.jpg",
        )
    ]
    run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    row = conn.execute(
        "SELECT * FROM date_candidate WHERE uuid='u1' AND source='filename'"
    ).fetchone()
    assert row["value"] == "2014-06-12T12:34:56"


def test_exif_date_becomes_a_candidate(conn):
    def fake_exif(paths, **kwargs):
        return {"/tmp/x.jpg": {"EXIF:DateTimeOriginal": "2003:02:23 09:08:08"}}

    records = [record("u1", added=datetime(2020, 12, 18, 9, 12, 48))]
    run_survey(conn, records, library_path=LIBRARY, read_exif=fake_exif)
    row = conn.execute(
        "SELECT * FROM date_candidate WHERE uuid='u1' AND source='exif'"
    ).fetchone()
    assert row["value"] == "2003-02-23T09:08:08"


def test_keywords_and_albums_are_stored_as_json(conn):
    records = [
        AssetRecord(
            uuid="u1",
            kind="image",
            filename="x.jpg",
            path="/tmp/x.jpg",
            date_added=datetime(2020, 12, 18, 9, 12, 48),
            keywords=("utah", "moab"),
            albums=("Trips",),
        )
    ]
    run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    row = conn.execute("SELECT keywords, albums FROM asset WHERE uuid='u1'").fetchone()
    assert json.loads(row["keywords"]) == ["utah", "moab"]
    assert json.loads(row["albums"]) == ["Trips"]


def test_too_many_missing_originals_aborts(conn):
    records = [
        record("u1", added=datetime(2020, 12, 18, 9, 12, 48), path=None),
        record("u2", added=datetime(2020, 12, 18, 9, 12, 49), path=None),
    ]
    with pytest.raises(MissingOriginalsError) as excinfo:
        run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    assert "Download Originals" in str(excinfo.value)


def test_a_few_missing_originals_are_tolerated(conn):
    records = [
        record(f"u{i}", added=datetime(2020, 12, 18, 9, 12, 48)) for i in range(200)
    ]
    records.append(record("missing", added=datetime(2020, 12, 18, 9, 12, 48), path=None))
    result = run_survey(conn, records, library_path=LIBRARY, read_exif=no_exif)
    assert result.missing_originals == 1
    assert result.written == 201
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_survey.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'photodedup.survey'`.

- [ ] **Step 3: Write the implementation**

`src/photodedup/survey.py`:

```python
"""The survey stage: read-only characterisation of the library."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection

from photodedup.catalog import get_run, set_run, set_stage
from photodedup.dates import (
    Candidate,
    parse_exif_datetime,
    parse_filename_datetime,
    rejection_reason,
)
from photodedup.exif import read_metadata
from photodedup.library import AssetRecord

MISSING_ORIGINALS_TOLERANCE = 0.01

# Ordered by descending trust; the first non-null wins for a given source.
_EXIF_DATE_TAGS = (
    "EXIF:DateTimeOriginal",
    "QuickTime:CreationDate",
    "QuickTime:CreateDate",
    "XMP:DateCreated",
    "EXIF:CreateDate",
)


class MissingOriginalsError(RuntimeError):
    """Too many assets have no local original to read."""


@dataclass(frozen=True)
class SurveyResult:
    total: int
    written: int
    skipped_after_cutoff: int
    missing_originals: int
    errors: int


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.replace(microsecond=0).isoformat()


def _exif_candidate(tags: dict[str, object]) -> Candidate:
    for tag in _EXIF_DATE_TAGS:
        raw = tags.get(tag)
        if raw is None:
            continue
        parsed = parse_exif_datetime(str(raw))
        if parsed is not None:
            return Candidate("exif", parsed, str(raw))
    return Candidate("exif", None, None)


def _file_mtime_candidate(tags: dict[str, object]) -> Candidate:
    raw = tags.get("File:FileModifyDate")
    if raw is None:
        return Candidate("file_mtime", None, None)
    return Candidate("file_mtime", parse_exif_datetime(str(raw)), str(raw))


def _candidates(record: AssetRecord, tags: dict[str, object]) -> list[Candidate]:
    name = record.original_filename or record.filename or ""
    return [
        _exif_candidate(tags),
        Candidate("filename", parse_filename_datetime(name), name or None),
        Candidate("photos_db", record.photos_date, _iso(record.photos_date)),
        _file_mtime_candidate(tags),
    ]


def _write_asset(
    conn: Connection, record: AssetRecord, tags: dict[str, object]
) -> None:
    mtime = _file_mtime_candidate(tags).value
    conn.execute(
        """
        INSERT INTO asset (
            uuid, filename, original_filename, uti, kind, width, height,
            file_size, path, date_added, photos_date, photos_tz_offset,
            file_mtime, is_live, has_raw, has_adjustments, is_favorite,
            is_hidden, in_trash, title, description, latitude, longitude,
            keywords, albums, import_uuid, survey_status, survey_error
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, 'ok', NULL
        )
        ON CONFLICT (uuid) DO UPDATE SET survey_status = 'ok', survey_error = NULL
        """,
        (
            record.uuid,
            record.filename,
            record.original_filename,
            record.uti,
            record.kind,
            record.width,
            record.height,
            tags.get("File:FileSize"),
            record.path,
            _iso(record.date_added),
            _iso(record.photos_date),
            record.photos_tz_offset,
            _iso(mtime),
            int(record.is_live),
            int(record.has_raw),
            int(record.has_adjustments),
            int(record.is_favorite),
            int(record.is_hidden),
            int(record.in_trash),
            record.title,
            record.description,
            record.latitude,
            record.longitude,
            json.dumps(list(record.keywords)),
            json.dumps(list(record.albums)),
            record.import_uuid,
        ),
    )

    for candidate in _candidates(record, tags):
        why = rejection_reason(
            candidate, date_added=record.date_added, file_mtime=mtime
        )
        conn.execute(
            """
            INSERT INTO date_candidate (uuid, source, value, raw, rejected_reason)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (uuid, source) DO UPDATE SET
                value = excluded.value,
                raw = excluded.raw,
                rejected_reason = excluded.rejected_reason
            """,
            (record.uuid, candidate.source, _iso(candidate.value), candidate.raw, why),
        )


def run_survey(
    conn: Connection,
    records: Iterable[AssetRecord],
    *,
    library_path: str,
    cutoff: datetime | None = None,
    read_exif: Callable[..., dict[str, dict[str, object]]] = read_metadata,
    batch_size: int = 400,
) -> SurveyResult:
    """Populate the catalog from *records*. Idempotent and resumable."""
    set_stage(conn, "survey", "running")
    all_records = list(records)

    existing_run = get_run(conn)
    if cutoff is not None:
        cutoff_iso = _iso(cutoff)
    elif existing_run is not None:
        cutoff_iso = existing_run["cutoff_date_added"]
    else:
        added = [r.date_added for r in all_records if r.date_added is not None]
        cutoff_iso = _iso(max(added)) if added else _iso(datetime.now())
    set_run(conn, library_path=library_path, cutoff_date_added=cutoff_iso)

    in_scope = []
    skipped = 0
    for record in all_records:
        if record.date_added is not None and _iso(record.date_added) > cutoff_iso:
            skipped += 1
        else:
            in_scope.append(record)

    missing = sum(1 for r in in_scope if r.path is None)
    if in_scope and missing / len(in_scope) > MISSING_ORIGINALS_TOLERANCE:
        set_stage(conn, "survey", "failed", notes=f"{missing} assets lack originals")
        raise MissingOriginalsError(
            f"{missing} of {len(in_scope)} assets have no local original. Enable "
            "Download Originals to this Mac in Photos, wait for it to finish, "
            "then re-run the survey."
        )

    done = {
        row["uuid"]
        for row in conn.execute("SELECT uuid FROM asset WHERE survey_status = 'ok'")
    }
    todo = [r for r in in_scope if r.uuid not in done]

    written = 0
    errors = 0
    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        paths = [Path(r.path) for r in batch if r.path]
        tags_by_path = read_exif(paths) if paths else {}
        for record in batch:
            tags = tags_by_path.get(str(record.path), {}) if record.path else {}
            try:
                _write_asset(conn, record, tags)
                written += 1
            except Exception as exc:
                errors += 1
                conn.execute(
                    """
                    INSERT INTO asset (uuid, survey_status, survey_error)
                    VALUES (?, 'error', ?)
                    ON CONFLICT (uuid) DO UPDATE SET
                        survey_status = 'error', survey_error = excluded.survey_error
                    """,
                    (record.uuid, str(exc)),
                )
        conn.commit()

    set_stage(
        conn,
        "survey",
        "complete",
        notes=f"{written} written, {skipped} after cutoff, {errors} errors",
    )
    return SurveyResult(
        total=len(in_scope),
        written=written,
        skipped_after_cutoff=skipped,
        missing_originals=missing,
        errors=errors,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_survey.py -v
```

Expected: PASS, 11 tests.

- [ ] **Step 5: Run the whole suite**

```bash
uv run pytest -v
```

Expected: PASS, everything green.

- [ ] **Step 6: Commit**

```bash
git add src/photodedup/survey.py tests/test_survey.py
git commit -m "feat: add resumable read-only survey stage"
```

---

### Task 8: Survey report and CLI

**Files:**
- Create: `src/photodedup/report.py`
- Create: `src/photodedup/cli.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `photodedup.catalog`, `photodedup.survey`, `photodedup.library`.
- Produces:
  - `survey_report(conn) -> str` — a Markdown report.
  - `date_added_clusters(conn, *, window_minutes: int = 60, min_size: int = 50) -> list[tuple[str, int]]` — `date_added` values bucketed into windows, returning `(bucket_start_iso, count)` for buckets at or above `min_size`, largest first. These are the import spikes that identify cohort A.
  - `main()` — the Click entry point, exposing `photodedup survey` and `photodedup report`.

- [ ] **Step 1: Write the failing test**

`tests/test_report.py`:

```python
from datetime import datetime, timedelta

import pytest

from photodedup.catalog import open_catalog
from photodedup.library import AssetRecord
from photodedup.report import date_added_clusters, survey_report
from photodedup.survey import run_survey


@pytest.fixture
def conn(tmp_path):
    return open_catalog(tmp_path / "catalog.sqlite")


# An explicit far-future cutoff keeps a second bulk() call from being skipped
# as "added after the cutoff the first call stored".
FAR_FUTURE = datetime(2030, 1, 1)


def bulk(conn, n, *, added, photos_date=None, prefix="u"):
    records = [
        AssetRecord(
            uuid=f"{prefix}{i}",
            kind="image",
            filename="x.jpg",
            original_filename="x.jpg",
            path="/tmp/x.jpg",
            date_added=added + timedelta(seconds=i),
            photos_date=photos_date,
        )
        for i in range(n)
    ]
    run_survey(
        conn,
        records,
        library_path="/tmp/L",
        cutoff=FAR_FUTURE,
        read_exif=lambda p, **k: {},
    )


def test_import_spike_is_detected(conn):
    bulk(conn, 60, added=datetime(2020, 12, 18, 9, 0, 0))
    clusters = date_added_clusters(conn, min_size=50)
    assert clusters
    assert clusters[0][0].startswith("2020-12-18T09:00")
    assert clusters[0][1] == 60


def test_small_clusters_are_ignored(conn):
    bulk(conn, 5, added=datetime(2020, 12, 18, 9, 0, 0))
    assert date_added_clusters(conn, min_size=50) == []


def test_clusters_are_sorted_largest_first(conn):
    bulk(conn, 60, added=datetime(2020, 12, 18, 9, 0, 0), prefix="a")
    bulk(conn, 90, added=datetime(2021, 3, 1, 14, 0, 0), prefix="b")
    clusters = date_added_clusters(conn, min_size=50)
    assert [c[1] for c in clusters] == [90, 60]


def test_report_mentions_totals_and_rejections(conn):
    added = datetime(2020, 12, 18, 9, 0, 0)
    bulk(conn, 10, added=added, photos_date=added)
    report = survey_report(conn)
    assert "# Survey Report" in report
    assert "at_or_after_date_added" in report
    assert "10" in report


def test_report_on_empty_catalog_does_not_crash(conn):
    assert "# Survey Report" in survey_report(conn)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_report.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'photodedup.report'`.

- [ ] **Step 3: Write the report implementation**

`src/photodedup/report.py`:

```python
"""Markdown reporting over the catalog."""

from __future__ import annotations

from sqlite3 import Connection

from photodedup.catalog import get_run


def date_added_clusters(
    conn: Connection, *, window_minutes: int = 60, min_size: int = 50
) -> list[tuple[str, int]]:
    """Bucket date_added into windows and return the crowded ones, largest first.

    A bulk import shows up here as hundreds or thousands of assets sharing one
    window, which is the signature that identifies the bad-metadata cohort.
    """
    seconds = window_minutes * 60
    rows = conn.execute(
        """
        SELECT
            datetime((strftime('%s', date_added) / :sec) * :sec, 'unixepoch') AS bucket,
            COUNT(*) AS n
        FROM asset
        WHERE date_added IS NOT NULL
        GROUP BY bucket
        HAVING n >= :min_size
        ORDER BY n DESC, bucket ASC
        """,
        {"sec": seconds, "min_size": min_size},
    ).fetchall()
    return [(row["bucket"].replace(" ", "T"), row["n"]) for row in rows]


def _table(header: tuple[str, ...], rows: list[tuple]) -> str:
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out) + "\n"


def survey_report(conn: Connection) -> str:
    run = get_run(conn)
    total = conn.execute("SELECT COUNT(*) AS n FROM asset").fetchone()["n"]

    by_kind = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM asset GROUP BY kind ORDER BY n DESC"
    ).fetchall()
    by_uti = conn.execute(
        "SELECT uti, COUNT(*) AS n FROM asset GROUP BY uti ORDER BY n DESC LIMIT 15"
    ).fetchall()
    flags = conn.execute(
        """
        SELECT
            SUM(is_live) AS live,
            SUM(has_raw) AS raw,
            SUM(has_adjustments) AS edited,
            SUM(is_favorite) AS favorite,
            SUM(CASE WHEN path IS NULL THEN 1 ELSE 0 END) AS no_original,
            SUM(CASE WHEN survey_status = 'error' THEN 1 ELSE 0 END) AS errors
        FROM asset
        """
    ).fetchone()
    rejections = conn.execute(
        """
        SELECT source, COALESCE(rejected_reason, 'accepted') AS reason, COUNT(*) AS n
        FROM date_candidate
        GROUP BY source, reason
        ORDER BY source, n DESC
        """
    ).fetchall()
    disagreements = conn.execute(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT uuid
            FROM date_candidate
            WHERE rejected_reason IS NULL AND value IS NOT NULL
            GROUP BY uuid
            HAVING MAX(value) > MIN(value)
        )
        """
    ).fetchone()["n"]
    no_candidate = conn.execute(
        """
        SELECT COUNT(*) AS n FROM asset a
        WHERE NOT EXISTS (
            SELECT 1 FROM date_candidate c
            WHERE c.uuid = a.uuid AND c.rejected_reason IS NULL AND c.value IS NOT NULL
        )
        """
    ).fetchone()["n"]

    clusters = date_added_clusters(conn)

    parts = [
        "# Survey Report\n",
        f"- Library: `{run['library_path'] if run else 'unknown'}`",
        f"- Cutoff (max date_added at survey time): `{run['cutoff_date_added'] if run else 'unset'}`",
        f"- Assets surveyed: **{total}**\n",
        "## Asset kinds\n",
        _table(("kind", "count"), [(r["kind"], r["n"]) for r in by_kind]),
        "\n## File types\n",
        _table(("uti", "count"), [(r["uti"], r["n"]) for r in by_uti]),
        "\n## Flags\n",
        _table(
            ("flag", "count"),
            [
                ("live photos", flags["live"] or 0),
                ("has raw", flags["raw"] or 0),
                ("edited", flags["edited"] or 0),
                ("favorite", flags["favorite"] or 0),
                ("no local original", flags["no_original"] or 0),
                ("survey errors", flags["errors"] or 0),
            ],
        ),
        "\n## Date candidates by source and outcome\n",
        _table(
            ("source", "outcome", "count"),
            [(r["source"], r["reason"], r["n"]) for r in rejections],
        ),
        "\n## Date agreement\n",
        _table(
            ("measure", "count"),
            [
                ("assets whose surviving candidates disagree", disagreements),
                ("assets with no surviving candidate", no_candidate),
            ],
        ),
        "\n## Import spikes (date_added clustered hourly, 50+ assets)\n",
        "Large clusters are bulk imports. The cohort whose dates collapsed to",
        "import time will appear here.\n",
        _table(("hour", "assets"), clusters[:40]),
    ]
    return "\n".join(parts)
```

- [ ] **Step 4: Run the report tests to verify they pass**

```bash
uv run pytest tests/test_report.py -v
```

Expected: PASS, 5 tests.

- [ ] **Step 5: Write the CLI**

`src/photodedup/cli.py`:

```python
"""Command line entry point."""

from __future__ import annotations

from pathlib import Path

import click

from photodedup.catalog import open_catalog
from photodedup.library import LibraryUnavailable, iter_assets
from photodedup.report import survey_report
from photodedup.survey import MissingOriginalsError, run_survey

DEFAULT_LIBRARY = Path.home() / "Pictures" / "Photos Library.photoslibrary"
DEFAULT_CATALOG = Path("catalog.sqlite")


@click.group()
def main() -> None:
    """De-duplicate an Apple Photos library while preserving accurate metadata."""


@main.command()
@click.option(
    "--library",
    type=click.Path(path_type=Path),
    default=DEFAULT_LIBRARY,
    show_default=True,
)
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
def survey(library: Path, catalog: Path) -> None:
    """Read the library into the catalog. Read-only, resumable."""
    conn = open_catalog(catalog)
    try:
        result = run_survey(conn, iter_assets(library), library_path=str(library))
    except (LibraryUnavailable, MissingOriginalsError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"surveyed {result.total} assets: {result.written} written, "
        f"{result.skipped_after_cutoff} after cutoff, "
        f"{result.missing_originals} without originals, {result.errors} errors"
    )


@main.command()
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=Path("reports/survey.md"),
    show_default=True,
)
def report(catalog: Path, out: Path) -> None:
    """Write the Markdown survey report."""
    conn = open_catalog(catalog)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(survey_report(conn))
    click.echo(f"wrote {out}")
```

- [ ] **Step 6: Verify the CLI loads**

```bash
uv run photodedup --help
uv run photodedup survey --help
```

Expected: both print usage without error.

- [ ] **Step 7: Run the full suite**

```bash
uv run pytest -v
```

Expected: PASS, everything green.

- [ ] **Step 8: Commit**

```bash
git add src/photodedup/report.py src/photodedup/cli.py tests/test_report.py
git commit -m "feat: add survey report and CLI"
```

- [ ] **Step 9: Run the survey for real**

Only once spike 5 confirms iCloud originals are fully downloaded.

```bash
time uv run photodedup survey
uv run photodedup report
```

Expected: the survey takes a while on 329 GB. It is resumable, so interrupting
it is safe. Read `reports/survey.md`.

- [ ] **Step 10: Update STATE.md and commit the report**

Record in `STATE.md`: the cutoff watermark, the asset total, the import spikes
found, how often EXIF and Photos dates disagree, which rejection reasons fired
and how often, and how many assets have no surviving date candidate.

The two questions Phase 2 needs answered from this report:

1. Do cohort A assets still carry a correct `EXIF:DateTimeOriginal`? If the
   `exif` source has a high accepted count among assets in the import spikes,
   the good dates were in the files all along and are recoverable even for
   assets with no duplicate twin.
2. Which sentinel values actually occur, and is the `SENTINEL_DATES` list in
   `dates.py` missing any? Look for rejection reasons that did not fire and for
   suspicious repeated values among accepted candidates.

```bash
git add STATE.md reports/survey.md
git commit -m "docs: record survey findings"
```

---

## Phase 2 (not planned yet)

Phase 2 covers `hash`, `group`, `arbitrate`, the HTML review report, `archive`,
`apply`, and `verify`. It is deliberately unplanned: the design states that the
arbitration thresholds and sentinel list are set from the survey's findings, and
writing those tasks now would mean inventing numbers this plan exists to
measure. Write the Phase 2 plan after reading `reports/survey.md`.
