# Photo De-duplication, Phase 4: Archive, Apply, Verify — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Act on the Phase 3 decisions: archive every loser to the external drive with verification, merge loser metadata onto keepers, correct keeper dates, stage losers in Photos albums for the user to delete, and verify the outcome.

**Architecture:** Two additive catalog tables record progress so every stage resumes. A thin `photos_write.py` adapter is the only module that imports photoscript, and it does so lazily, so the package still loads in a shell with no GUI. Tests drive `apply` against an in-memory fake of that adapter and `archive`/`verify` against temp directories and injected readers. The only real-library write path is `apply --self-test`, which the user runs by hand first.

**Tech Stack:** Python 3.13, uv, Click, SQLite, osxphotos (read), photoscript (write), pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-20-photo-dedup-phase4-archive-apply-verify-design.md`. Read it. Its parents are `2026-09-19-photo-dedup-phase3-arbitrate-report-design.md` and `2026-09-18-photo-dedup-design.md`. `STATE.md` holds the measured numbers.

## Global Constraints

- Repository root `/Users/robotdan/dev/tools`; package `src/photodedup/`; run everything with `uv run`. The suite (`uv run pytest -q`, 326 tests) must stay green after every task.
- Nothing in Phase 4 deletes a photo, ever. `apply` writes only: album membership, keywords, favorite, title, description, date. `archive` and `verify` never write to Photos.
- `SCHEMA_VERSION` stays 4. The two new tables are created by `open_catalog`'s `CREATE TABLE IF NOT EXISTS` script; no existing table changes.
- Only `src/photodedup/photos_write.py` may import photoscript, and only inside functions. Importing the package, running tests, and running `archive`/`verify` must all work in the agent shell, where AppleScript compilation fails.
- `library._install_photoscript_stub` currently installs the stub whenever photoscript has not been imported yet, which would hijack a later real import. Task 1 changes it to try the real import first and stub only on failure.
- Every `apply` write records an `apply_action` row with `previous` filled, and a failure on one asset never aborts the run.
- Date corrections default to resolvable exact sets with `date_changes = 1` and no `dates_disagree` flag; `--include-disagreeing-dates` widens to all resolvable exact sets with `date_changes = 1`.
- `archive` copies only losers (resolvable exact sets), through a temporary name, with a streaming SHA-256 verified by re-reading before the row is recorded.
- Tests never touch the real Photos library or `/Volumes`.
- Commit after every task with a conventional message. Never commit `catalog.sqlite*`, `reports/`, `losers.csv`, or anything under `/Volumes`.

---

### Task 1: Schema tables, photoscript stub fix, write adapter and its fake

**Files:**
- Modify: `src/photodedup/catalog.py` (append two tables to `_SCHEMA`)
- Modify: `src/photodedup/library.py` (`_install_photoscript_stub`; add `read_states`, `iter_paths`)
- Create: `src/photodedup/photos_write.py`
- Create: `tests/fake_photos.py`
- Test: `tests/test_catalog.py`, `tests/test_library.py`, `tests/test_photos_write.py`

**Interfaces:**
- Produces: tables `archive_file`, `apply_action` (exact SQL in spec); `library.read_states(library_path, uuids) -> dict[str, AssetState]` with `AssetState(present: bool, date: datetime | None, albums: tuple[str, ...])`; `library.iter_paths(library_path) -> Iterator[AssetPaths]` with `AssetPaths(uuid, path, path_edited, path_live)`; `photos_write.PhotosWriter` (Protocol) with the methods below; `photos_write.PhotoscriptWriter` implementing it; `photos_write.PhotosUnavailable(RuntimeError)`; `tests/fake_photos.FakePhotos` implementing the protocol in memory.

The writer protocol, which Tasks 3 and 4 code against:

```python
class PhotosWriter(Protocol):
    def exists(self, uuid: str) -> bool: ...
    def get(self, uuid: str) -> PhotoState: ...          # title, description, keywords, favorite, date, albums
    def set_title(self, uuid: str, value: str) -> None: ...
    def set_description(self, uuid: str, value: str) -> None: ...
    def set_keywords(self, uuid: str, values: list[str]) -> None: ...
    def set_favorite(self, uuid: str, value: bool) -> None: ...
    def set_date(self, uuid: str, value: datetime) -> None: ...
    def ensure_album(self, name: str, folder: str | None = None) -> str: ...   # returns album id
    def add_to_album(self, album_id: str, uuids: list[str]) -> list[str]: ...  # returns uuids actually added
    def delete_album(self, album_id: str) -> None: ...   # self-test cleanup only
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_catalog.py`:

```python
def test_phase4_tables_exist(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"archive_file", "apply_action"} <= tables


def test_archive_file_kind_and_cascade(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    conn.execute(
        """INSERT INTO archive_file (uuid, kind, dest, bytes, sha256, verified_at)
           VALUES ('u1', 'original', '2014/05/u1_a.jpg', 10, 'abc', '2026-09-20T00:00:00')"""
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO archive_file (uuid, kind, dest, bytes, sha256, verified_at)
               VALUES ('u1', 'thumbnail', 'x', 1, 'a', 'now')"""
        )
    conn.execute("DELETE FROM asset WHERE uuid = 'u1'")
    assert conn.execute("SELECT COUNT(*) AS n FROM archive_file").fetchone()["n"] == 0


def test_apply_action_constraints(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    conn.execute(
        """INSERT INTO apply_action (uuid, action, status, detail, previous, applied_at)
           VALUES ('u1', 'merge', 'done', '{}', '{}', 'now')"""
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO apply_action (uuid, action, status, applied_at)
               VALUES ('u1', 'delete', 'done', 'now')"""
        )
    assert conn.execute("SELECT COUNT(*) AS n FROM apply_action").fetchone()["n"] == 1


def test_schema_version_is_still_4():
    assert SCHEMA_VERSION == 4
```

Append to `tests/test_library.py`:

```python
def test_stub_is_not_installed_when_photoscript_imports(monkeypatch):
    """The stub must never hijack a real photoscript. Simulate a working
    import by planting a module first."""
    import sys
    import types

    from photodedup.library import _install_photoscript_stub

    real = types.ModuleType("photoscript")
    real.PhotosLibrary = object
    monkeypatch.setitem(sys.modules, "photoscript", real)
    _install_photoscript_stub()
    assert sys.modules["photoscript"] is real


def test_stub_is_installed_when_photoscript_import_fails(monkeypatch):
    import builtins
    import sys

    from photodedup.library import _install_photoscript_stub

    monkeypatch.delitem(sys.modules, "photoscript", raising=False)
    monkeypatch.delitem(sys.modules, "photoscript.script_loader", raising=False)
    original_import = builtins.__import__

    def failing(name, *args, **kwargs):
        if name.startswith("photoscript"):
            raise RuntimeError("AppleScript compilation failed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing)
    _install_photoscript_stub()
    stub = sys.modules["photoscript"]
    with pytest.raises(RuntimeError):
        stub.PhotosLibrary()


def test_read_states_reports_presence_date_and_albums():
    from datetime import datetime

    from photodedup.library import AssetState, _read_states_from_db

    class Photo:
        def __init__(self, uuid, date, albums):
            self.uuid, self.date, self.albums = uuid, date, albums

    class DB:
        def get_photo(self, uuid):
            return {"a": Photo("a", datetime(2014, 5, 12, 17, 0, 0), ["Trips"])}.get(uuid)

    states = _read_states_from_db(DB(), ["a", "gone"])
    assert states["a"] == AssetState(True, datetime(2014, 5, 12, 17, 0, 0), ("Trips",))
    assert states["gone"] == AssetState(False, None, ())


def test_iter_paths_reads_edited_and_live_paths():
    from photodedup.library import AssetPaths, _iter_paths_from_db

    class Photo:
        uuid = "a"
        path = "/lib/a.jpg"
        path_edited = "/lib/a_edited.jpg"
        path_live_photo = None

    class DB:
        def photos(self, **kwargs):
            return [Photo()]

    assert list(_iter_paths_from_db(DB())) == [AssetPaths("a", "/lib/a.jpg", "/lib/a_edited.jpg", None)]
```

Create `tests/test_photos_write.py`:

```python
from datetime import datetime

import pytest

from photodedup.photos_write import PhotosUnavailable, PhotoscriptWriter
from tests.fake_photos import FakePhotos


def test_fake_round_trips_every_field():
    fake = FakePhotos({"a": {}})
    fake.set_title("a", "Beach")
    fake.set_description("a", "sand")
    fake.set_keywords("a", ["x", "y"])
    fake.set_favorite("a", True)
    fake.set_date("a", datetime(2014, 5, 12, 17, 0, 0))
    state = fake.get("a")
    assert (state.title, state.description, state.keywords, state.favorite, state.date) == (
        "Beach", "sand", ("x", "y"), True, datetime(2014, 5, 12, 17, 0, 0)
    )


def test_fake_albums_are_created_once_and_membership_is_a_set():
    fake = FakePhotos({"a": {}, "b": {}})
    first = fake.ensure_album("Trips")
    second = fake.ensure_album("Trips")
    assert first == second
    assert fake.add_to_album(first, ["a", "b"]) == ["a", "b"]
    assert fake.add_to_album(first, ["a"]) == []
    assert fake.get("a").albums == ("Trips",)
    assert fake.album_members("Trips") == ["a", "b"]


def test_fake_folders_scope_albums():
    fake = FakePhotos({"a": {}})
    album = fake.ensure_album("Dedup Delete 001", folder="Dedup Delete")
    assert fake.album_name(album) == "Dedup Delete 001"
    assert fake.album_folder(album) == "Dedup Delete"


def test_fake_can_fail_on_demand():
    fake = FakePhotos({"a": {}}, fail_on={("set_title", "a")})
    with pytest.raises(RuntimeError):
        fake.set_title("a", "x")
    assert fake.get("a").title == ""


def test_fake_unknown_photo():
    fake = FakePhotos({})
    assert fake.exists("nope") is False
    with pytest.raises(KeyError):
        fake.get("nope")


def test_real_writer_reports_unavailable_outside_a_gui_session(monkeypatch):
    """In this shell photoscript cannot compile its AppleScript. The adapter
    must turn that into one clear error, not a traceback from deep inside."""
    import builtins

    original_import = builtins.__import__

    def failing(name, *args, **kwargs):
        if name.startswith("photoscript"):
            raise RuntimeError("Expected 'given' but found plural class name (-2741)")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing)
    with pytest.raises(PhotosUnavailable) as exc:
        PhotoscriptWriter()
    assert "interactive" in str(exc.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_catalog.py tests/test_library.py tests/test_photos_write.py -q`
Expected: ImportError / ModuleNotFoundError on the new names.

- [ ] **Step 3: Implement**

In `src/photodedup/catalog.py`, append to `_SCHEMA` before the closing `"""`:

```sql

CREATE TABLE IF NOT EXISTS archive_file (
    uuid        TEXT NOT NULL REFERENCES asset (uuid) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('original', 'edited', 'live')),
    dest        TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    PRIMARY KEY (uuid, kind)
);

CREATE TABLE IF NOT EXISTS apply_action (
    uuid        TEXT NOT NULL REFERENCES asset (uuid) ON DELETE CASCADE,
    action      TEXT NOT NULL CHECK (action IN ('merge', 'date', 'stage', 'stage_blank')),
    status      TEXT NOT NULL CHECK (status IN ('done', 'failed', 'skipped')),
    detail      TEXT,
    previous    TEXT,
    applied_at  TEXT NOT NULL,
    PRIMARY KEY (uuid, action)
);

CREATE INDEX IF NOT EXISTS apply_action_action ON apply_action (action, status);
```

In `src/photodedup/library.py`, replace the body of `_install_photoscript_stub` after its docstring with:

```python
    import sys
    import types

    if "photoscript" in sys.modules:
        return
    try:
        import photoscript  # noqa: F401
        return
    except Exception:
        # AppleScript compilation failed: no GUI session. Fall through to the
        # stub so osxphotos' read-only path still imports.
        pass

    class _Unavailable:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError(
                "photoscript is unavailable in this process. Read-only stages do "
                "not need it; a stage that writes to Photos must run from an "
                "interactive terminal session."
            )

    for name in ("photoscript", "photoscript.script_loader"):
        module = types.ModuleType(name)
        module.__getattr__ = lambda _attr: _Unavailable  # type: ignore[method-assign]
        sys.modules[name] = module
```

Update the docstring's last sentence to: "The real module is tried first; the stub is installed only when that import fails, so a process that can drive Photos keeps the real one."

Append to `src/photodedup/library.py`:

```python
@dataclass(frozen=True)
class AssetState:
    present: bool
    date: datetime | None
    albums: tuple[str, ...]


@dataclass(frozen=True)
class AssetPaths:
    uuid: str
    path: str | None
    path_edited: str | None
    path_live: str | None


def _open_db(library_path: Path) -> object:
    if not Path(library_path).exists():
        raise LibraryUnavailable(f"Cannot open {library_path}.")
    _install_photoscript_stub()
    try:
        import osxphotos
    except Exception as exc:  # pragma: no cover - import-time environment failure
        raise LibraryUnavailable(f"osxphotos could not be imported: {exc}") from exc
    try:
        return osxphotos.PhotosDB(str(library_path))
    except Exception as exc:
        raise LibraryUnavailable(f"osxphotos could not open {library_path}: {exc}") from exc


def _read_states_from_db(db: object, uuids: Iterable[str]) -> dict[str, AssetState]:
    out: dict[str, AssetState] = {}
    for uuid in uuids:
        photo = db.get_photo(uuid)
        if photo is None:
            out[uuid] = AssetState(False, None, ())
        else:
            out[uuid] = AssetState(True, _naive(photo.date), tuple(photo.albums or ()))
    return out


def read_states(library_path: Path, uuids: Iterable[str]) -> dict[str, AssetState]:
    """Presence, displayed date and album names for each uuid. Read-only."""
    return _read_states_from_db(_open_db(library_path), uuids)


def _iter_paths_from_db(db: object) -> Iterator[AssetPaths]:
    for photo in db.photos(movies=True, intrash=False):
        yield AssetPaths(
            uuid=photo.uuid,
            path=photo.path,
            path_edited=getattr(photo, "path_edited", None),
            path_live=getattr(photo, "path_live_photo", None),
        )


def iter_paths(library_path: Path) -> Iterator[AssetPaths]:
    """Original, edited-render and Live Photo movie paths per asset. Read-only."""
    return _iter_paths_from_db(_open_db(library_path))
```

Add `from collections.abc import Iterable, Iterator` to the imports (Iterator is already imported; extend it).

Create `src/photodedup/photos_write.py`:

```python
"""The one place that writes to Photos.

Wraps photoscript behind a small protocol so every apply step can be tested
against an in-memory fake, and so the rest of the package never imports
photoscript: its AppleScript compiles only inside a GUI session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class PhotosUnavailable(RuntimeError):
    """photoscript cannot drive Photos from this process."""


@dataclass(frozen=True)
class PhotoState:
    title: str
    description: str
    keywords: tuple[str, ...]
    favorite: bool
    date: datetime | None
    albums: tuple[str, ...]


class PhotosWriter(Protocol):
    def exists(self, uuid: str) -> bool: ...
    def get(self, uuid: str) -> PhotoState: ...
    def set_title(self, uuid: str, value: str) -> None: ...
    def set_description(self, uuid: str, value: str) -> None: ...
    def set_keywords(self, uuid: str, values: list[str]) -> None: ...
    def set_favorite(self, uuid: str, value: bool) -> None: ...
    def set_date(self, uuid: str, value: datetime) -> None: ...
    def ensure_album(self, name: str, folder: str | None = None) -> str: ...
    def add_to_album(self, album_id: str, uuids: list[str]) -> list[str]: ...
    def delete_album(self, album_id: str) -> None: ...


class PhotoscriptWriter:
    """photoscript-backed writer. Construct only from an interactive session."""

    def __init__(self) -> None:
        try:
            import photoscript
        except Exception as exc:
            raise PhotosUnavailable(
                "Photos cannot be scripted from this process (photoscript failed "
                f"to load: {exc}). Run this command from an interactive terminal "
                "session on the Mac that owns the library."
            ) from exc
        self._ps = photoscript
        self._lib = photoscript.PhotosLibrary()
        self._albums: dict[tuple[str | None, str], str] = {}

    def _photo(self, uuid: str):
        return self._ps.Photo(uuid)

    def exists(self, uuid: str) -> bool:
        try:
            self._photo(uuid)
            return True
        except ValueError:
            return False

    def get(self, uuid: str) -> PhotoState:
        p = self._photo(uuid)
        return PhotoState(
            title=p.title or "",
            description=p.description or "",
            keywords=tuple(p.keywords or ()),
            favorite=bool(p.favorite),
            date=p.date,
            albums=tuple(a.name for a in p.albums),
        )

    def set_title(self, uuid: str, value: str) -> None:
        self._photo(uuid).title = value

    def set_description(self, uuid: str, value: str) -> None:
        self._photo(uuid).description = value

    def set_keywords(self, uuid: str, values: list[str]) -> None:
        self._photo(uuid).keywords = list(values)

    def set_favorite(self, uuid: str, value: bool) -> None:
        self._photo(uuid).favorite = bool(value)

    def set_date(self, uuid: str, value: datetime) -> None:
        self._photo(uuid).date = value.replace(tzinfo=None, microsecond=0)

    def _folder(self, name: str):
        folder = self._lib.folder(name, top_level=True)
        return folder if folder is not None else self._lib.create_folder(name)

    def ensure_album(self, name: str, folder: str | None = None) -> str:
        key = (folder, name)
        if key in self._albums:
            return self._albums[key]
        if folder is None:
            album = self._lib.album(name, top_level=True)
            if album is None:
                album = self._lib.create_album(name)
        else:
            parent = self._folder(folder)
            album = parent.album(name)
            if album is None:
                album = parent.create_album(name)
        self._albums[key] = album.id
        return album.id

    def add_to_album(self, album_id: str, uuids: list[str]) -> list[str]:
        album = self._ps.Album(album_id)
        already = {p.uuid for p in album.photos}
        todo = [self._photo(u) for u in uuids if u not in already]
        if not todo:
            return []
        added = album.add(todo)
        return [p.uuid for p in added]

    def delete_album(self, album_id: str) -> None:
        self._lib.delete_album(self._ps.Album(album_id))
```

Create `tests/fake_photos.py`:

```python
"""In-memory stand-in for photos_write.PhotosWriter."""

from __future__ import annotations

from datetime import datetime

from photodedup.photos_write import PhotoState


class FakePhotos:
    def __init__(self, photos: dict[str, dict], *, fail_on: set[tuple[str, str]] | None = None):
        self.photos: dict[str, dict] = {
            uuid: {
                "title": "", "description": "", "keywords": [], "favorite": False,
                "date": None, **fields,
            }
            for uuid, fields in photos.items()
        }
        self.albums: dict[str, dict] = {}   # id -> {name, folder, members: list}
        self.fail_on = fail_on or set()
        self.calls: list[tuple] = []

    def _check(self, op: str, uuid: str) -> None:
        self.calls.append((op, uuid))
        if (op, uuid) in self.fail_on:
            raise RuntimeError(f"fake failure on {op} {uuid}")

    def exists(self, uuid: str) -> bool:
        return uuid in self.photos

    def get(self, uuid: str) -> PhotoState:
        p = self.photos[uuid]
        albums = tuple(a["name"] for a in self.albums.values() if uuid in a["members"])
        return PhotoState(p["title"], p["description"], tuple(p["keywords"]), p["favorite"], p["date"], albums)

    def set_title(self, uuid: str, value: str) -> None:
        self._check("set_title", uuid); self.photos[uuid]["title"] = value

    def set_description(self, uuid: str, value: str) -> None:
        self._check("set_description", uuid); self.photos[uuid]["description"] = value

    def set_keywords(self, uuid: str, values: list[str]) -> None:
        self._check("set_keywords", uuid); self.photos[uuid]["keywords"] = list(values)

    def set_favorite(self, uuid: str, value: bool) -> None:
        self._check("set_favorite", uuid); self.photos[uuid]["favorite"] = bool(value)

    def set_date(self, uuid: str, value: datetime) -> None:
        self._check("set_date", uuid); self.photos[uuid]["date"] = value.replace(tzinfo=None, microsecond=0)

    def ensure_album(self, name: str, folder: str | None = None) -> str:
        for album_id, a in self.albums.items():
            if a["name"] == name and a["folder"] == folder:
                return album_id
        album_id = f"album-{len(self.albums) + 1}"
        self.albums[album_id] = {"name": name, "folder": folder, "members": []}
        return album_id

    def add_to_album(self, album_id: str, uuids: list[str]) -> list[str]:
        members = self.albums[album_id]["members"]
        added = []
        for u in uuids:
            self._check("add_to_album", u)
            if u not in self.photos:
                raise KeyError(u)
            if u not in members:
                members.append(u); added.append(u)
        return added

    def delete_album(self, album_id: str) -> None:
        del self.albums[album_id]

    # test helpers
    def album_members(self, name: str, folder: str | None = None) -> list[str]:
        for a in self.albums.values():
            if a["name"] == name and a["folder"] == folder:
                return list(a["members"])
        return []

    def album_name(self, album_id: str) -> str:
        return self.albums[album_id]["name"]

    def album_folder(self, album_id: str) -> str | None:
        return self.albums[album_id]["folder"]
```

Make `tests/` importable as a package if it is not already: create an empty `tests/__init__.py` only if `from tests.fake_photos import FakePhotos` fails; otherwise use a conftest-free relative import `from fake_photos import FakePhotos` (pytest adds `tests/` to `sys.path` with rootdir-based collection). Pick whichever works and use it consistently in Tasks 3 and 4.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass. `test_real_writer_reports_unavailable_outside_a_gui_session` passes in this shell because the import fails; it also passes in a GUI shell because the monkeypatch forces the failure.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/catalog.py src/photodedup/library.py src/photodedup/photos_write.py tests/fake_photos.py tests/test_catalog.py tests/test_library.py tests/test_photos_write.py
git commit -m "feat: phase 4 tables, photos write adapter with fake, library state readers"
```

---

### Task 2: Archive

**Files:**
- Create: `src/photodedup/archive.py`
- Modify: `src/photodedup/cli.py` (add `archive`)
- Test: `tests/test_archive.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `archive_file` (Task 1); `library.iter_paths`; `require_stage_complete`, `StageIncomplete`, `set_stage`.
- Produces: `run_archive(conn, root: Path, *, paths: Mapping[str, AssetPaths] | None = None, limit: int | None = None, free_bytes: Callable[[Path], int] | None = None, progress=None) -> ArchiveResult(copied, skipped, mismatched, bytes)`; `pending_losers(conn) -> list[Row]`; `class ArchiveSpace(RuntimeError)`; `dest_for(row, kind, source) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_archive.py`:

```python
import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from photodedup.archive import ArchiveSpace, dest_for, pending_losers, run_archive
from photodedup.arbitrate import run_arbitrate
from photodedup.catalog import StageIncomplete, open_catalog, set_stage
from photodedup.library import AssetPaths

NOW = datetime(2026, 9, 20, 12, 0, 0)


@pytest.fixture
def conn(tmp_path):
    conn = open_catalog(tmp_path / "catalog.sqlite")
    set_stage(conn, "group", "complete")
    return conn


def add_asset(conn, uuid, tmp_path, *, size=10, photos_date="2014-05-12T17:00:00",
              albums=(), favorite=0, name=None):
    path = tmp_path / "lib" / f"{uuid}.jpg"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(bytes([uuid.encode()[0]]) * size)
    conn.execute(
        """INSERT INTO asset (uuid, kind, uti, file_size, path, date_added, photos_date,
                              is_favorite, albums, keywords, survey_status, original_filename)
           VALUES (?, 'image', 'public.jpeg', ?, ?, '2020-01-01T00:00:00', ?, ?, ?, '[]', 'ok', ?)""",
        (uuid, size, str(path), photos_date, favorite, json.dumps(list(albums)), name or f"{uuid}.jpg"),
    )
    conn.execute("INSERT INTO asset_hash (uuid, status) VALUES (?, 'ok')", (uuid,))
    conn.execute(
        "INSERT INTO date_candidate (uuid, source, value) VALUES (?, 'photos_db', ?)", (uuid, photos_date)
    )
    conn.commit()
    return path


def add_set(conn, set_id, uuids, *, tier="exact"):
    conn.execute("INSERT INTO dup_set (id, tier, kind, size) VALUES (?, ?, 'image', ?)", (set_id, tier, len(uuids)))
    conn.executemany(
        "INSERT INTO dup_set_member (set_id, uuid, tier, signals) VALUES (?, ?, ?, 'scan')",
        [(set_id, u, tier) for u in uuids],
    )
    conn.commit()


@pytest.fixture
def decided(conn, tmp_path):
    """Set 1: keeper 'a' (bigger), loser 'b'. Set 2: candidate pair, never archived."""
    add_asset(conn, "a", tmp_path, size=20)
    add_asset(conn, "b", tmp_path, size=10, albums=("Trips",), photos_date="2003-02-23T09:08:08")
    add_set(conn, 1, ["a", "b"])
    add_asset(conn, "c", tmp_path)
    add_asset(conn, "d", tmp_path)
    add_set(conn, 2, ["c", "d"], tier="candidate")
    run_arbitrate(conn, now=NOW)
    return conn


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_pending_losers_are_resolvable_exact_losers_only(decided):
    assert [r["uuid"] for r in pending_losers(decided)] == ["b"]


def test_dest_uses_photos_date_and_original_filename():
    row = {"uuid": "b", "photos_date": "2003-02-23T09:08:08", "original_filename": "Picture 046.jpg"}
    assert dest_for(row, "original", "/lib/x.jpeg") == "2003/02/b_Picture 046.jpg"
    assert dest_for(row, "edited", "/lib/x_edited.heic") == "2003/02/b_edited.heic"
    assert dest_for(row, "live", "/lib/x.mov") == "2003/02/b_live.mov"
    assert dest_for({"uuid": "z", "photos_date": None, "original_filename": None}, "original", "/lib/z.jpg") == "undated/z_z.jpg"


def test_archive_copies_verifies_and_writes_sidecar(decided, tmp_path):
    root = tmp_path / "archive"
    result = run_archive(decided, root, free_bytes=lambda p: 10**9)
    assert (result.copied, result.skipped, result.mismatched) == (1, 0, 0)
    dest = root / "2003" / "02" / "b_b.jpg"
    assert dest.read_bytes() == b"b" * 10
    row = decided.execute("SELECT * FROM archive_file WHERE uuid='b'").fetchone()
    assert row["kind"] == "original" and row["dest"] == "2003/02/b_b.jpg"
    assert row["sha256"] == sha(dest) and row["bytes"] == 10
    side = json.loads((root / "2003" / "02" / "b_b.jpg.json").read_text())
    assert side["role"] == "loser" and side["keeper_uuid"] == "a" and side["set_id"] == 1
    assert side["albums"] == ["Trips"] and side["sha256"] == row["sha256"]
    manifest = list(csv.DictReader((root / "manifest.csv").open()))
    assert manifest[0]["uuid"] == "b" and manifest[0]["kind"] == "original"


def test_archive_is_resumable(decided, tmp_path):
    root = tmp_path / "archive"
    run_archive(decided, root, free_bytes=lambda p: 10**9)
    second = run_archive(decided, root, free_bytes=lambda p: 10**9)
    assert (second.copied, second.skipped) == (0, 1)


def test_archive_copies_edited_and_live_files_when_present(decided, tmp_path):
    edited = tmp_path / "lib" / "b_edited.jpg"; edited.write_bytes(b"E" * 5)
    live = tmp_path / "lib" / "b.mov"; live.write_bytes(b"L" * 7)
    paths = {"b": AssetPaths("b", str(tmp_path / "lib" / "b.jpg"), str(edited), str(live))}
    root = tmp_path / "archive"
    result = run_archive(decided, root, paths=paths, free_bytes=lambda p: 10**9)
    assert result.copied == 3
    kinds = {r["kind"] for r in decided.execute("SELECT kind FROM archive_file WHERE uuid='b'")}
    assert kinds == {"original", "edited", "live"}
    assert (root / "2003" / "02" / "b_live.mov").read_bytes() == b"L" * 7


def test_mismatch_is_not_recorded_and_is_retried(decided, tmp_path, monkeypatch):
    import photodedup.archive as arc

    root = tmp_path / "archive"
    real = arc._sha256_file
    calls = {"n": 0}

    def corrupt_once(path):
        calls["n"] += 1
        return "bogus" if calls["n"] == 1 else real(path)

    monkeypatch.setattr(arc, "_sha256_file", corrupt_once)
    first = run_archive(decided, root, free_bytes=lambda p: 10**9)
    assert first.mismatched == 1 and first.copied == 0
    assert decided.execute("SELECT COUNT(*) AS n FROM archive_file").fetchone()["n"] == 0
    assert not (root / "2003" / "02" / "b_b.jpg").exists()
    second = run_archive(decided, root, free_bytes=lambda p: 10**9)
    assert second.copied == 1


def test_refuses_without_enough_space(decided, tmp_path):
    with pytest.raises(ArchiveSpace) as exc:
        run_archive(decided, tmp_path / "archive", free_bytes=lambda p: 5)
    assert "10" in str(exc.value)


def test_refuses_before_arbitrate(conn, tmp_path):
    with pytest.raises(StageIncomplete):
        run_archive(conn, tmp_path / "archive", free_bytes=lambda p: 10**9)


def test_missing_source_is_reported_not_fatal(decided, tmp_path):
    Path(decided.execute("SELECT path FROM asset WHERE uuid='b'").fetchone()["path"]).unlink()
    result = run_archive(decided, tmp_path / "archive", free_bytes=lambda p: 10**9)
    assert result.copied == 0 and result.mismatched == 0 and result.missing == 1


def test_limit_caps_the_run(decided, tmp_path):
    result = run_archive(decided, tmp_path / "archive", limit=0, free_bytes=lambda p: 10**9)
    assert result.copied == 0
```

Append to `tests/test_cli.py`:

```python
def test_archive_refuses_before_arbitrate(catalog, tmp_path):
    result = run("archive", "--catalog", str(catalog), "--root", str(tmp_path / "arc"))
    assert result.exit_code != 0
    assert "arbitrate stage" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_archive.py tests/test_cli.py -q`
Expected: ModuleNotFoundError on `photodedup.archive`.

- [ ] **Step 3: Implement**

Create `src/photodedup/archive.py`:

```python
"""Copy every loser's original to the external drive and verify the copy."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePath
from sqlite3 import Connection, Row

from photodedup.catalog import get_run, require_stage_complete, set_stage
from photodedup.library import AssetPaths, iter_paths

CHUNK = 8 * 1024 * 1024
HEADROOM = 0.10


class ArchiveSpace(RuntimeError):
    """The destination volume cannot hold what remains to be copied."""


@dataclass(frozen=True)
class ArchiveResult:
    copied: int
    skipped: int
    mismatched: int
    missing: int
    bytes: int


def pending_losers(conn: Connection) -> list[Row]:
    """Losers of resolvable exact sets, with everything the sidecar needs."""
    return conn.execute(
        """
        SELECT a.uuid, a.original_filename, a.path, a.file_size, a.photos_date,
               a.date_added, a.albums, a.keywords, a.is_favorite, a.title,
               a.description, l.set_id, l.lost_on, l.carries,
               d.keeper_uuid, d.date_value, d.review_flags
        FROM decision_loser l
        JOIN dup_set s ON s.id = l.set_id AND s.tier = 'exact'
        JOIN decision d ON d.set_id = l.set_id AND d.resolvable = 1
        JOIN asset a ON a.uuid = l.uuid
        ORDER BY a.uuid
        """
    ).fetchall()


def dest_for(row: Mapping, kind: str, source: str) -> str:
    """Relative destination path: <YYYY>/<MM>/<uuid>_<name>."""
    date = row["photos_date"]
    folder = f"{date[0:4]}/{date[5:7]}" if date else "undated"
    uuid = row["uuid"]
    suffix = PurePath(source).suffix.lower()
    if kind == "original":
        name = row["original_filename"] or f"{uuid}{suffix}"
        return f"{folder}/{uuid}_{name}"
    return f"{folder}/{uuid}_{kind}{suffix}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_hashing(src: Path, dst: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(src, "rb") as reader, open(dst, "wb") as writer:
        for chunk in iter(lambda: reader.read(CHUNK), b""):
            digest.update(chunk)
            writer.write(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _default_free_bytes(root: Path) -> int:
    probe = root
    while not probe.exists():
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _sidecar(row: Row, sha: str, size: int, stamp: str) -> dict:
    return {
        "uuid": row["uuid"],
        "original_filename": row["original_filename"],
        "role": "loser",
        "set_id": row["set_id"],
        "keeper_uuid": row["keeper_uuid"],
        "lost_on": row["lost_on"],
        "carries": json.loads(row["carries"] or "{}"),
        "albums": json.loads(row["albums"] or "[]"),
        "keywords": json.loads(row["keywords"] or "[]"),
        "is_favorite": bool(row["is_favorite"]),
        "title": row["title"],
        "description": row["description"],
        "photos_date": row["photos_date"],
        "date_added": row["date_added"],
        "arbitrated_date": row["date_value"],
        "review_flags": row["review_flags"],
        "sha256": sha,
        "bytes": size,
        "archived_at": stamp,
    }


def _write_manifest(conn: Connection, root: Path) -> None:
    rows = conn.execute(
        "SELECT uuid, kind, dest, bytes, sha256, verified_at FROM archive_file ORDER BY uuid, kind"
    ).fetchall()
    tmp = root / "manifest.csv.tmp"
    with open(tmp, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["uuid", "kind", "dest", "bytes", "sha256", "verified_at"])
        for r in rows:
            writer.writerow([r["uuid"], r["kind"], r["dest"], r["bytes"], r["sha256"], r["verified_at"]])
    os.replace(tmp, root / "manifest.csv")


def run_archive(
    conn: Connection,
    root: Path,
    *,
    paths: Mapping[str, AssetPaths] | None = None,
    limit: int | None = None,
    free_bytes: Callable[[Path], int] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> ArchiveResult:
    require_stage_complete(conn, "arbitrate")
    missing_decisions = conn.execute(
        "SELECT COUNT(*) AS n FROM dup_set s WHERE NOT EXISTS (SELECT 1 FROM decision d WHERE d.set_id = s.id)"
    ).fetchone()["n"]
    if missing_decisions:
        from photodedup.catalog import StageIncomplete
        raise StageIncomplete(
            f"{missing_decisions} duplicate sets have no decision; re-run `photodedup arbitrate`."
        )

    root = Path(root)
    losers = pending_losers(conn)
    done = {
        (r["uuid"], r["kind"]) for r in conn.execute("SELECT uuid, kind FROM archive_file")
    }
    todo = [r for r in losers if (r["uuid"], "original") not in done]
    if limit is not None:
        todo = todo[:limit]

    need = int(sum((r["file_size"] or 0) for r in todo) * (1 + HEADROOM))
    free = (free_bytes or _default_free_bytes)(root)
    if need > free:
        raise ArchiveSpace(
            f"{len(todo)} files need {need} bytes including headroom; {free} free at {root}."
        )

    if paths is None and todo:
        library = get_run(conn)["library_path"]
        wanted = {r["uuid"] for r in todo}
        paths = {p.uuid: p for p in iter_paths(Path(library)) if p.uuid in wanted}
    paths = paths or {}

    set_stage(conn, "archive", "running")
    root.mkdir(parents=True, exist_ok=True)
    copied = skipped = mismatched = missing = total_bytes = 0
    skipped = len(losers) - len([r for r in losers if (r["uuid"], "original") not in done])

    for index, row in enumerate(todo, start=1):
        extra = paths.get(row["uuid"])
        sources = [("original", row["path"])]
        if extra is not None:
            sources = [("original", extra.path or row["path"]), ("edited", extra.path_edited), ("live", extra.path_live)]
        for kind, source in sources:
            if not source or (row["uuid"], kind) in done:
                continue
            src = Path(source)
            if not src.is_file():
                missing += 1
                continue
            rel = dest_for(row, kind, source)
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".part")
            try:
                sha, size = _copy_hashing(src, tmp)
                check = _sha256_file(tmp)
            except OSError:
                tmp.unlink(missing_ok=True)
                missing += 1
                continue
            if check != sha:
                tmp.unlink(missing_ok=True)
                mismatched += 1
                continue
            os.replace(tmp, dst)
            stamp = datetime.now().replace(microsecond=0).isoformat()
            if kind == "original":
                (root / (rel + ".json")).write_text(json.dumps(_sidecar(row, sha, size, stamp), indent=2))
            conn.execute(
                """INSERT INTO archive_file (uuid, kind, dest, bytes, sha256, verified_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (row["uuid"], kind, rel, size, sha, stamp),
            )
            copied += 1
            total_bytes += size
        if index % 200 == 0:
            conn.commit()
            if progress:
                progress(index, len(todo))

    conn.commit()
    _write_manifest(conn, root)
    set_stage(
        conn, "archive", "complete" if not mismatched and not missing else "running",
        notes=f"{copied} copied, {skipped} already done, {mismatched} mismatched, {missing} missing, {total_bytes} bytes",
    )
    return ArchiveResult(copied, skipped, mismatched, missing, total_bytes)
```

Note the stage is left `running` (not `complete`) when anything mismatched or went missing, so `apply` refuses until a clean re-run; the note says why.

In `src/photodedup/cli.py` add:

```python
from photodedup.archive import ArchiveSpace, run_archive

DEFAULT_ARCHIVE = Path("/Volumes/Extreme Pro/photodedup")


@main.command()
@click.option("--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True)
@click.option("--root", type=click.Path(path_type=Path), default=DEFAULT_ARCHIVE, show_default=True)
@click.option("--limit", type=int, default=None, help="Copy at most this many losers, for a trial run.")
def archive(catalog: Path, root: Path, limit: int | None) -> None:
    """Copy every loser's original to the archive and verify it. Read-only against Photos; resumable."""
    conn = open_catalog(catalog)
    try:
        result = run_archive(
            conn, root, limit=limit,
            progress=lambda done, total: click.echo(f"  {done}/{total}", err=True),
        )
    except (StageIncomplete, ArchiveSpace, LibraryUnavailable) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"{result.copied} copied ({result.bytes} bytes), {result.skipped} already archived, "
        f"{result.mismatched} mismatched (will retry), {result.missing} missing sources"
    )
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/archive.py src/photodedup/cli.py tests/test_archive.py tests/test_cli.py
git commit -m "feat: archive losers to the external drive with verification and sidecars"
```

---

### Task 3: Apply — merge metadata and correct dates

**Files:**
- Create: `src/photodedup/apply.py`
- Test: `tests/test_apply.py`

**Interfaces:**
- Consumes: `PhotosWriter` protocol and `PhotoState` (Task 1); `FakePhotos`; `archive_file`, `apply_action`, `decision`, `decision_loser`.
- Produces: `require_archive(conn) -> None` (raises `StageIncomplete` listing missing count); `merge_targets(conn) -> list[MergeTarget]`; `date_targets(conn, *, include_disagreeing: bool) -> list[DateTarget]`; `run_merge(conn, writer, *, dry_run=False, now=None) -> ApplyResult(done, failed, skipped)`; `run_dates(conn, writer, *, include_disagreeing=False, dry_run=False, now=None) -> ApplyResult`; `plan_lines(conn, *, include_disagreeing=False) -> list[str]` (what `apply` prints with no flags).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_apply.py`:

```python
import json
from datetime import datetime

import pytest

from photodedup.apply import (
    date_targets,
    merge_targets,
    plan_lines,
    require_archive,
    run_dates,
    run_merge,
)
from photodedup.arbitrate import run_arbitrate
from photodedup.catalog import StageIncomplete, open_catalog, set_stage
from tests.fake_photos import FakePhotos

NOW = datetime(2026, 9, 20, 12, 0, 0)


@pytest.fixture
def conn(tmp_path):
    conn = open_catalog(tmp_path / "catalog.sqlite")
    set_stage(conn, "group", "complete")
    return conn


def add_asset(conn, uuid, *, size=10, photos_date="2014-05-12T17:00:00", albums=(),
              keywords=(), favorite=0, title=None, description=None, exif=None,
              added="2020-01-01T00:00:00"):
    conn.execute(
        """INSERT INTO asset (uuid, kind, uti, file_size, path, date_added, photos_date,
                              is_favorite, albums, keywords, title, description,
                              survey_status, original_filename)
           VALUES (?, 'image', 'public.jpeg', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?)""",
        (uuid, size, f"/lib/{uuid}.jpg", added, photos_date, favorite,
         json.dumps(list(albums)), json.dumps(list(keywords)), title, description, f"{uuid}.jpg"),
    )
    conn.execute("INSERT INTO asset_hash (uuid, status) VALUES (?, 'ok')", (uuid,))
    conn.execute("INSERT INTO date_candidate (uuid, source, value) VALUES (?, 'photos_db', ?)", (uuid, photos_date))
    if exif:
        conn.execute("INSERT INTO date_candidate (uuid, source, value) VALUES (?, 'exif', ?)", (uuid, exif))
    conn.commit()


def add_set(conn, set_id, uuids, *, tier="exact"):
    conn.execute("INSERT INTO dup_set (id, tier, kind, size) VALUES (?, ?, 'image', ?)", (set_id, tier, len(uuids)))
    conn.executemany(
        "INSERT INTO dup_set_member (set_id, uuid, tier, signals) VALUES (?, ?, ?, 'scan')",
        [(set_id, u, tier) for u in uuids],
    )
    conn.commit()


def archived(conn, *uuids):
    for u in uuids:
        conn.execute(
            """INSERT OR IGNORE INTO archive_file (uuid, kind, dest, bytes, sha256, verified_at)
               VALUES (?, 'original', ?, 1, 'x', 'now')""", (u, f"2014/05/{u}.jpg"),
        )
    set_stage(conn, "archive", "complete")
    conn.commit()


def fake_for(conn):
    return FakePhotos({
        r["uuid"]: {
            "title": r["title"] or "", "description": r["description"] or "",
            "keywords": json.loads(r["keywords"] or "[]"), "favorite": bool(r["is_favorite"]),
            "date": datetime.fromisoformat(r["photos_date"]) if r["photos_date"] else None,
        }
        for r in conn.execute("SELECT * FROM asset")
    })


@pytest.fixture
def merged(conn):
    """keeper 'k' lacks what loser 'l' carries."""
    add_asset(conn, "k", size=20, albums=("A",), keywords=("x",))
    add_asset(conn, "l", size=10, albums=("A", "B"), keywords=("x", "y"), favorite=1,
              title="Beach", description="sand")
    add_set(conn, 1, ["k", "l"])
    run_arbitrate(conn, now=NOW)
    archived(conn, "l")
    return conn


def test_require_archive_counts_missing_losers(conn):
    add_asset(conn, "k", size=20); add_asset(conn, "l", size=10)
    add_set(conn, 1, ["k", "l"]); run_arbitrate(conn, now=NOW)
    with pytest.raises(StageIncomplete) as exc:
        require_archive(conn)
    assert "1" in str(exc.value)
    archived(conn, "l")
    require_archive(conn)


def test_merge_targets_come_from_carries(merged):
    [t] = merge_targets(merged)
    assert t.keeper == "k" and t.loser == "l"
    assert t.carries == {"albums": ["B"], "keywords": ["y"], "favorite": True, "title": "Beach", "description": "sand"}


def test_merge_writes_everything_and_records_previous(merged):
    fake = fake_for(merged)
    fake.ensure_album("A"); fake.add_to_album(fake.ensure_album("A"), ["k", "l"])
    result = run_merge(merged, fake, now=NOW)
    assert (result.done, result.failed, result.skipped) == (1, 0, 0)
    k = fake.get("k")
    assert k.title == "Beach" and k.description == "sand" and k.favorite is True
    assert set(k.keywords) == {"x", "y"}
    assert fake.album_members("B") == ["k"]
    row = merged.execute("SELECT * FROM apply_action WHERE uuid='k' AND action='merge'").fetchone()
    assert row["status"] == "done"
    assert json.loads(row["previous"]) == {"title": "", "description": "", "keywords": ["x"], "favorite": False, "albums": ["A"]}
    assert json.loads(row["detail"])["albums"] == ["B"]


def test_merge_is_idempotent(merged):
    fake = fake_for(merged)
    run_merge(merged, fake, now=NOW)
    second = run_merge(merged, fake, now=NOW)
    assert (second.done, second.skipped) == (0, 1)
    assert fake.album_members("B") == ["k"]


def test_merge_never_overwrites_a_longer_keeper_caption(conn):
    add_asset(conn, "k", size=20, title="A much longer title")
    add_asset(conn, "l", size=10, title="Short")
    add_set(conn, 1, ["k", "l"]); run_arbitrate(conn, now=NOW); archived(conn, "l")
    fake = fake_for(conn)
    run_merge(conn, fake, now=NOW)
    assert fake.get("k").title == "A much longer title"


def test_merge_failure_is_recorded_and_run_continues(conn):
    add_asset(conn, "k1", size=20); add_asset(conn, "l1", size=10, favorite=1)
    add_asset(conn, "k2", size=20); add_asset(conn, "l2", size=10, favorite=1)
    add_set(conn, 1, ["k1", "l1"]); add_set(conn, 2, ["k2", "l2"])
    run_arbitrate(conn, now=NOW); archived(conn, "l1", "l2")
    fake = FakePhotos({u: {} for u in ("k1", "l1", "k2", "l2")}, fail_on={("set_favorite", "k1")})
    result = run_merge(conn, fake, now=NOW)
    assert (result.done, result.failed) == (1, 1)
    rows = {r["uuid"]: r["status"] for r in conn.execute("SELECT uuid, status FROM apply_action")}
    assert rows == {"k1": "failed", "k2": "done"}
    assert fake.get("k2").favorite is True


def test_merge_dry_run_writes_nothing(merged):
    fake = fake_for(merged)
    result = run_merge(merged, fake, dry_run=True, now=NOW)
    assert result.done == 0
    assert fake.get("k").title == ""
    assert merged.execute("SELECT COUNT(*) AS n FROM apply_action").fetchone()["n"] == 0


def test_merge_refuses_without_archive(conn):
    add_asset(conn, "k", size=20); add_asset(conn, "l", size=10, favorite=1)
    add_set(conn, 1, ["k", "l"]); run_arbitrate(conn, now=NOW)
    with pytest.raises(StageIncomplete):
        run_merge(conn, FakePhotos({}), now=NOW)


@pytest.fixture
def dated(conn):
    """Set 1: rescued date with no disagreement (stamped photos_db, exif twin).
    Set 2: rescued date WITH disagreement (two surviving exif years apart)."""
    add_asset(conn, "k1", size=20, photos_date="2020-12-18T09:10:00", added="2020-12-18T18:00:00")
    add_asset(conn, "l1", size=10, photos_date="2014-05-12T17:00:00", exif="2014-05-12T17:00:00")
    add_set(conn, 1, ["k1", "l1"])
    add_asset(conn, "k2", size=20, photos_date="2016-01-01T00:00:00", exif="2016-01-01T00:00:00")
    add_asset(conn, "l2", size=10, photos_date="2014-01-01T00:00:00", exif="2014-01-01T00:00:00")
    add_set(conn, 2, ["k2", "l2"])
    run_arbitrate(conn, now=NOW)
    archived(conn, "l1", "l2")
    return conn


def test_date_targets_gate_on_disagreement(dated):
    assert [t.keeper for t in date_targets(dated, include_disagreeing=False)] == ["k1"]
    assert [t.keeper for t in date_targets(dated, include_disagreeing=True)] == ["k1", "k2"]


def test_dates_are_written_verified_and_previous_recorded(dated):
    fake = fake_for(dated)
    result = run_dates(dated, fake, include_disagreeing=True, now=NOW)
    assert (result.done, result.failed) == (2, 0)
    assert fake.get("k1").date == datetime(2014, 5, 12, 17, 0, 0)
    assert fake.get("k2").date == datetime(2014, 1, 1, 0, 0, 0)
    row = dated.execute("SELECT * FROM apply_action WHERE uuid='k1' AND action='date'").fetchone()
    assert json.loads(row["previous"]) == {"date": "2020-12-18T09:10:00"}
    assert json.loads(row["detail"]) == {"date": "2014-05-12T17:00:00"}


def test_date_readback_mismatch_is_failed(dated, monkeypatch):
    fake = fake_for(dated)
    real = fake.set_date

    def drift(uuid, value):
        real(uuid, value.replace(hour=value.hour + 1))

    monkeypatch.setattr(fake, "set_date", drift)
    result = run_dates(dated, fake, now=NOW)
    assert result.failed == 1
    row = dated.execute("SELECT * FROM apply_action WHERE uuid='k1' AND action='date'").fetchone()
    assert row["status"] == "failed" and "2014-05-12T18:00:00" in row["detail"]


def test_dates_skip_done_rows(dated):
    fake = fake_for(dated)
    run_dates(dated, fake, now=NOW)
    assert run_dates(dated, fake, now=NOW).skipped == 1


def test_plan_lines_summarise(dated):
    lines = "\n".join(plan_lines(dated, include_disagreeing=False))
    assert "1 keeper date" in lines and "1 more with --include-disagreeing-dates" in lines
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_apply.py -q`
Expected: ModuleNotFoundError on `photodedup.apply`.

- [ ] **Step 3: Implement**

Create `src/photodedup/apply.py`:

```python
"""Apply the decisions to Photos: merge, dates, staging. Never deletes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Connection

from photodedup.catalog import StageIncomplete, require_stage_complete, set_stage
from photodedup.photos_write import PhotosWriter

BATCH = 500
FOLDER = "Dedup Delete"
BLANK_ALBUM = "Dedup Delete blank"


@dataclass(frozen=True)
class ApplyResult:
    done: int
    failed: int
    skipped: int


@dataclass(frozen=True)
class MergeTarget:
    keeper: str
    loser: str
    carries: dict


@dataclass(frozen=True)
class DateTarget:
    keeper: str
    set_id: int
    value: datetime
    disagreeing: bool


def _now(now: datetime | None) -> str:
    return (now or datetime.now()).replace(microsecond=0).isoformat()


def require_archive(conn: Connection) -> None:
    """Every pending loser must have a verified archived original."""
    require_stage_complete(conn, "archive")
    missing = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM decision_loser l
        JOIN dup_set s ON s.id = l.set_id AND s.tier = 'exact'
        JOIN decision d ON d.set_id = l.set_id AND d.resolvable = 1
        WHERE NOT EXISTS (
            SELECT 1 FROM archive_file f WHERE f.uuid = l.uuid AND f.kind = 'original'
        )
        """
    ).fetchone()["n"]
    if missing:
        raise StageIncomplete(
            f"{missing} losers have no verified archive copy. Run `photodedup archive` "
            "until it reports 0 mismatched and 0 missing."
        )


def _record(conn: Connection, uuid: str, action: str, status: str, detail: dict, previous: dict | None, now: datetime | None) -> None:
    conn.execute(
        """INSERT INTO apply_action (uuid, action, status, detail, previous, applied_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (uuid, action) DO UPDATE SET
               status = excluded.status, detail = excluded.detail,
               previous = excluded.previous, applied_at = excluded.applied_at""",
        (uuid, action, status, json.dumps(detail, sort_keys=True),
         json.dumps(previous, sort_keys=True) if previous is not None else None, _now(now)),
    )
    conn.commit()


def _done(conn: Connection, action: str) -> set[str]:
    return {
        r["uuid"] for r in conn.execute(
            "SELECT uuid FROM apply_action WHERE action = ? AND status = 'done'", (action,)
        )
    }


# --- merge -----------------------------------------------------------------


def merge_targets(conn: Connection) -> list[MergeTarget]:
    rows = conn.execute(
        """
        SELECT d.keeper_uuid AS keeper, l.uuid AS loser, l.carries AS carries
        FROM decision_loser l
        JOIN dup_set s ON s.id = l.set_id AND s.tier = 'exact'
        JOIN decision d ON d.set_id = l.set_id AND d.resolvable = 1
        WHERE l.carries != '{}'
        ORDER BY d.keeper_uuid, l.uuid
        """
    ).fetchall()
    return [MergeTarget(r["keeper"], r["loser"], json.loads(r["carries"])) for r in rows]


def _merged_carries(targets: list[MergeTarget]) -> dict[str, dict]:
    """One combined carry per keeper: unions for albums/keywords, OR for favorite,
    longest for title/description."""
    out: dict[str, dict] = {}
    for t in targets:
        c = out.setdefault(t.keeper, {"albums": [], "keywords": [], "favorite": False, "title": None, "description": None, "losers": []})
        c["losers"].append(t.loser)
        for key in ("albums", "keywords"):
            for value in t.carries.get(key, []):
                if value not in c[key]:
                    c[key].append(value)
        if t.carries.get("favorite"):
            c["favorite"] = True
        for key in ("title", "description"):
            value = t.carries.get(key)
            if value and (c[key] is None or len(value) > len(c[key])):
                c[key] = value
    return out


def run_merge(conn: Connection, writer: PhotosWriter, *, dry_run: bool = False, now: datetime | None = None) -> ApplyResult:
    require_archive(conn)
    combined = _merged_carries(merge_targets(conn))
    done_before = _done(conn, "merge")
    done = failed = skipped = 0
    if not dry_run:
        set_stage(conn, "apply", "running")
    for keeper in sorted(combined):
        carry = combined[keeper]
        if keeper in done_before:
            skipped += 1
            continue
        if dry_run:
            continue
        try:
            state = writer.get(keeper)
            previous = {
                "title": state.title, "description": state.description,
                "keywords": list(state.keywords), "favorite": state.favorite,
                "albums": list(state.albums),
            }
            written: dict = {}
            albums = [a for a in carry["albums"] if a not in state.albums]
            for album in albums:
                writer.add_to_album(writer.ensure_album(album), [keeper])
            if albums:
                written["albums"] = albums
            keywords = [k for k in carry["keywords"] if k not in state.keywords]
            if keywords:
                writer.set_keywords(keeper, list(state.keywords) + keywords)
                written["keywords"] = keywords
            if carry["favorite"] and not state.favorite:
                writer.set_favorite(keeper, True)
                written["favorite"] = True
            for key in ("title", "description"):
                value = carry[key]
                current = getattr(state, key)
                if value and len(value) > len(current):
                    getattr(writer, f"set_{key}")(keeper, value)
                    written[key] = value
            written["losers"] = carry["losers"]
            _record(conn, keeper, "merge", "done", written, previous, now)
            done += 1
        except Exception as exc:
            _record(conn, keeper, "merge", "failed", {"error": str(exc)}, None, now)
            failed += 1
    return ApplyResult(done, failed, skipped)


# --- dates -----------------------------------------------------------------


def date_targets(conn: Connection, *, include_disagreeing: bool) -> list[DateTarget]:
    rows = conn.execute(
        """
        SELECT d.set_id, d.keeper_uuid, d.date_value, d.review_flags
        FROM decision d JOIN dup_set s ON s.id = d.set_id
        WHERE s.tier = 'exact' AND d.resolvable = 1 AND d.date_changes = 1
          AND d.date_value IS NOT NULL
        ORDER BY d.keeper_uuid
        """
    ).fetchall()
    out = []
    for r in rows:
        disagreeing = "dates_disagree" in (r["review_flags"] or "").split(",")
        if disagreeing and not include_disagreeing:
            continue
        out.append(DateTarget(r["keeper_uuid"], r["set_id"], datetime.fromisoformat(r["date_value"]), disagreeing))
    return out


def run_dates(conn: Connection, writer: PhotosWriter, *, include_disagreeing: bool = False, dry_run: bool = False, now: datetime | None = None) -> ApplyResult:
    require_archive(conn)
    done_before = _done(conn, "date")
    done = failed = skipped = 0
    if not dry_run:
        set_stage(conn, "apply", "running")
    for target in date_targets(conn, include_disagreeing=include_disagreeing):
        if target.keeper in done_before:
            skipped += 1
            continue
        if dry_run:
            continue
        try:
            before = writer.get(target.keeper).date
            writer.set_date(target.keeper, target.value)
            after = writer.get(target.keeper).date
            previous = {"date": before.isoformat() if before else None}
            if after is not None and abs((after - target.value).total_seconds()) <= 1:
                _record(conn, target.keeper, "date", "done", {"date": target.value.isoformat(), "set_id": target.set_id}, previous, now)
                done += 1
            else:
                _record(conn, target.keeper, "date", "failed",
                        {"wanted": target.value.isoformat(), "read_back": after.isoformat() if after else None, "set_id": target.set_id},
                        previous, now)
                failed += 1
        except Exception as exc:
            _record(conn, target.keeper, "date", "failed", {"error": str(exc), "set_id": target.set_id}, None, now)
            failed += 1
    return ApplyResult(done, failed, skipped)


# --- plan ------------------------------------------------------------------


def plan_lines(conn: Connection, *, include_disagreeing: bool = False) -> list[str]:
    merges = len(_merged_carries(merge_targets(conn)))
    plain = date_targets(conn, include_disagreeing=False)
    every = date_targets(conn, include_disagreeing=True)
    losers = conn.execute(
        """SELECT COUNT(*) AS n FROM decision_loser l
           JOIN dup_set s ON s.id = l.set_id AND s.tier = 'exact'
           JOIN decision d ON d.set_id = l.set_id AND d.resolvable = 1"""
    ).fetchone()["n"]
    blank = conn.execute(
        """SELECT COUNT(*) AS n FROM decision d JOIN dup_set s ON s.id = d.set_id
           WHERE s.tier = 'exact' AND ',' || d.review_flags || ',' LIKE '%,blank_image,%'"""
    ).fetchone()["n"]
    chosen = every if include_disagreeing else plain
    return [
        f"--merge: {merges} keepers receive albums, keywords, favorite or captions from losers",
        f"--dates: {len(chosen)} keeper date{'s' if len(chosen) != 1 else ''} corrected"
        + ("" if include_disagreeing else f" ({len(every) - len(plain)} more with --include-disagreeing-dates)"),
        f"--stage: {losers} losers into '{FOLDER}' albums of {BATCH}, plus {blank} blank-image keepers into '{BLANK_ALBUM}'",
        "Nothing is deleted. Delete the staged albums yourself in Photos.",
    ]
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/apply.py tests/test_apply.py
git commit -m "feat: apply merges loser metadata onto keepers and corrects keeper dates"
```

---

### Task 4: Apply — stage losers, self-test, CLI

**Files:**
- Modify: `src/photodedup/apply.py` (append `run_stage`, `write_losers_csv`, `self_test`)
- Modify: `src/photodedup/cli.py` (add `apply`)
- Test: `tests/test_apply.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `run_stage(conn, writer, *, batch=BATCH, dry_run=False, now=None) -> ApplyResult`; `write_losers_csv(conn, path) -> int`; `self_test(writer, uuid: str, *, now=None) -> list[str]` (report lines; raises on any failed check after restoring what it changed).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_apply.py`:

```python
from photodedup.apply import BLANK_ALBUM, FOLDER, run_stage, self_test, write_losers_csv


@pytest.fixture
def staged(conn):
    for i in range(7):
        add_asset(conn, f"k{i}", size=20); add_asset(conn, f"l{i}", size=10)
        add_set(conn, i + 1, [f"k{i}", f"l{i}"])
    # a blank-image set: keeper goes to the blank album, loser is a normal loser
    add_asset(conn, "bk", size=5); add_asset(conn, "bl", size=5)
    conn.execute("UPDATE asset_hash SET flat = 1, blank_rows = 1 WHERE uuid IN ('bk', 'bl')")
    add_set(conn, 8, ["bk", "bl"])
    run_arbitrate(conn, now=NOW)
    archived(conn, *[f"l{i}" for i in range(7)], "bl")
    return conn


def test_stage_batches_losers_into_numbered_albums(staged):
    fake = fake_for(staged)
    result = run_stage(staged, fake, batch=3, now=NOW)
    assert (result.done, result.failed, result.skipped) == (9, 0, 0)
    assert fake.album_members("Dedup Delete 001", FOLDER) == ["bl", "l0", "l1"]
    assert fake.album_members("Dedup Delete 002", FOLDER) == ["l2", "l3", "l4"]
    assert fake.album_members("Dedup Delete 003", FOLDER) == ["l5", "l6"]
    assert fake.album_members(BLANK_ALBUM) == ["bk"]
    row = staged.execute("SELECT * FROM apply_action WHERE uuid='l2' AND action='stage'").fetchone()
    assert json.loads(row["detail"]) == {"album": "Dedup Delete 002"}
    blank = staged.execute("SELECT * FROM apply_action WHERE uuid='bk' AND action='stage_blank'").fetchone()
    assert json.loads(blank["detail"]) == {"album": BLANK_ALBUM}


def test_stage_resumes_without_reshuffling(staged):
    fake = fake_for(staged)
    run_stage(staged, fake, batch=3, now=NOW)
    add_asset(staged, "k9", size=20); add_asset(staged, "l9", size=10)
    add_set(staged, 9, ["k9", "l9"]); run_arbitrate(staged, now=NOW); archived(staged, "l9")
    fake.photos["k9"] = fake.photos["l9"] = {"title": "", "description": "", "keywords": [], "favorite": False, "date": None}
    # arbitrate rewrote decisions but apply_action rows survive; the new loser
    # lands in the next partial album.
    result = run_stage(staged, fake, batch=3, now=NOW)
    assert (result.done, result.skipped) == (1, 9)
    assert fake.album_members("Dedup Delete 003", FOLDER) == ["l5", "l6", "l9"]


def test_stage_never_stages_a_keeper_or_a_candidate(staged):
    add_asset(staged, "c1", size=10); add_asset(staged, "c2", size=10)
    add_set(staged, 20, ["c1", "c2"], tier="candidate"); run_arbitrate(staged, now=NOW)
    fake = fake_for(staged)
    run_stage(staged, fake, batch=500, now=NOW)
    everything = sum((fake.album_members(f"Dedup Delete 00{i}", FOLDER) for i in (1, 2, 3)), [])
    assert not any(u.startswith("k") for u in everything)
    assert "c1" not in everything and "c2" not in everything


def test_stage_failure_is_isolated(staged):
    fake = FakePhotos({r["uuid"]: {} for r in staged.execute("SELECT uuid FROM asset")}, fail_on={("add_to_album", "l3")})
    result = run_stage(staged, fake, batch=500, now=NOW)
    assert result.failed == 1 and result.done == 8
    assert staged.execute("SELECT status FROM apply_action WHERE uuid='l3'").fetchone()["status"] == "failed"


def test_stage_dry_run_creates_nothing(staged):
    fake = fake_for(staged)
    run_stage(staged, fake, dry_run=True, now=NOW)
    assert fake.albums == {}


def test_losers_csv(staged, tmp_path):
    fake = fake_for(staged)
    run_stage(staged, fake, batch=3, now=NOW)
    out = tmp_path / "losers.csv"
    assert write_losers_csv(staged, out) == 8
    import csv
    rows = list(csv.DictReader(out.open()))
    assert rows[0].keys() >= {"uuid", "original_filename", "photos_date", "keeper_uuid", "set_id", "lost_on", "album", "archive_dest"}
    by_uuid = {r["uuid"]: r for r in rows}
    assert by_uuid["l2"]["album"] == "Dedup Delete 002" and by_uuid["l2"]["archive_dest"] == "2014/05/l2.jpg"


def test_self_test_round_trips_and_restores():
    fake = FakePhotos({"p": {"title": "Original", "date": datetime(2010, 1, 1, 12, 0, 0)}})
    lines = self_test(fake, "p", now=NOW)
    assert any("date round-trip: ok" in line for line in lines)
    assert fake.get("p").title == "Original"
    assert fake.get("p").date == datetime(2010, 1, 1, 12, 0, 0)
    assert fake.albums == {}


def test_self_test_reports_a_drifting_date():
    fake = FakePhotos({"p": {"title": "t", "date": datetime(2010, 1, 1, 12, 0, 0)}})
    real = fake.set_date
    fake.set_date = lambda uuid, value: real(uuid, value.replace(hour=value.hour + 1))
    with pytest.raises(RuntimeError) as exc:
        self_test(fake, "p", now=NOW)
    assert "drift" in str(exc.value)
    assert fake.get("p").title == "t"
```

Append to `tests/test_cli.py`:

```python
def test_apply_with_no_flags_prints_the_plan(catalog):
    conn = open_catalog(catalog); set_stage(conn, "arbitrate", "complete"); set_stage(conn, "archive", "complete"); conn.close()
    result = run("apply", "--catalog", str(catalog))
    assert result.exit_code == 0, result.output
    assert "--merge" in result.output and "Nothing is deleted" in result.output


def test_apply_refuses_without_archive(catalog):
    conn = open_catalog(catalog); set_stage(conn, "arbitrate", "complete"); conn.close()
    result = run("apply", "--catalog", str(catalog), "--stage")
    assert result.exit_code != 0
    assert "archive stage" in result.output


def test_apply_real_write_needs_a_gui_session(catalog, tmp_path):
    """In this shell photoscript cannot load; the command must say so plainly."""
    conn = open_catalog(catalog); set_stage(conn, "arbitrate", "complete"); set_stage(conn, "archive", "complete"); conn.close()
    result = run("apply", "--catalog", str(catalog), "--stage")
    assert result.exit_code != 0
    assert "interactive" in result.output or "Photos cannot be scripted" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_apply.py tests/test_cli.py -q`
Expected: ImportError on `run_stage`.

- [ ] **Step 3: Implement**

Append to `src/photodedup/apply.py`:

```python
# --- stage -----------------------------------------------------------------


def _stage_population(conn: Connection) -> tuple[list[str], list[str]]:
    losers = [
        r["uuid"] for r in conn.execute(
            """SELECT l.uuid FROM decision_loser l
               JOIN dup_set s ON s.id = l.set_id AND s.tier = 'exact'
               JOIN decision d ON d.set_id = l.set_id AND d.resolvable = 1
               ORDER BY l.uuid"""
        )
    ]
    blank_keepers = [
        r["keeper_uuid"] for r in conn.execute(
            """SELECT d.keeper_uuid FROM decision d JOIN dup_set s ON s.id = d.set_id
               WHERE s.tier = 'exact' AND ',' || d.review_flags || ',' LIKE '%,blank_image,%'
               ORDER BY d.keeper_uuid"""
        )
    ]
    return losers, blank_keepers


def _album_counts(conn: Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in conn.execute("SELECT detail FROM apply_action WHERE action = 'stage' AND status = 'done'"):
        name = json.loads(r["detail"]).get("album")
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _album_name(index: int) -> str:
    return f"{FOLDER} {index:03d}"


def run_stage(conn: Connection, writer: PhotosWriter, *, batch: int = BATCH, dry_run: bool = False, now: datetime | None = None) -> ApplyResult:
    """Put every loser in a numbered album under the Dedup Delete folder.

    Albums fill in order and a re-run continues in the first album that
    still has room, so numbering never restarts and no loser is staged twice.
    """
    require_archive(conn)
    losers, blank_keepers = _stage_population(conn)
    done_stage = _done(conn, "stage")
    done_blank = _done(conn, "stage_blank")
    done = failed = skipped = 0
    if not dry_run:
        set_stage(conn, "apply", "running")

    counts = _album_counts(conn)
    index = max([int(name.rsplit(" ", 1)[1]) for name in counts] or [1])
    while counts.get(_album_name(index), 0) >= batch:
        index += 1

    for uuid in losers:
        if uuid in done_stage:
            skipped += 1
            continue
        if dry_run:
            continue
        while counts.get(_album_name(index), 0) >= batch:
            index += 1
        name = _album_name(index)
        try:
            writer.add_to_album(writer.ensure_album(name, folder=FOLDER), [uuid])
            _record(conn, uuid, "stage", "done", {"album": name}, None, now)
            counts[name] = counts.get(name, 0) + 1
            done += 1
        except Exception as exc:
            _record(conn, uuid, "stage", "failed", {"error": str(exc), "album": name}, None, now)
            failed += 1

    for uuid in blank_keepers:
        if uuid in done_blank:
            skipped += 1
            continue
        if dry_run:
            continue
        try:
            writer.add_to_album(writer.ensure_album(BLANK_ALBUM), [uuid])
            _record(conn, uuid, "stage_blank", "done", {"album": BLANK_ALBUM}, None, now)
            done += 1
        except Exception as exc:
            _record(conn, uuid, "stage_blank", "failed", {"error": str(exc)}, None, now)
            failed += 1
    return ApplyResult(done, failed, skipped)


def write_losers_csv(conn: Connection, path) -> int:
    import csv

    rows = conn.execute(
        """
        SELECT l.uuid, a.original_filename, a.photos_date, d.keeper_uuid, l.set_id, l.lost_on,
               act.detail AS stage_detail, f.dest AS archive_dest
        FROM decision_loser l
        JOIN dup_set s ON s.id = l.set_id AND s.tier = 'exact'
        JOIN decision d ON d.set_id = l.set_id AND d.resolvable = 1
        JOIN asset a ON a.uuid = l.uuid
        LEFT JOIN apply_action act ON act.uuid = l.uuid AND act.action = 'stage' AND act.status = 'done'
        LEFT JOIN archive_file f ON f.uuid = l.uuid AND f.kind = 'original'
        ORDER BY l.uuid
        """
    ).fetchall()
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["uuid", "original_filename", "photos_date", "keeper_uuid", "set_id", "lost_on", "album", "archive_dest"])
        for r in rows:
            album = json.loads(r["stage_detail"]).get("album") if r["stage_detail"] else ""
            writer.writerow([r["uuid"], r["original_filename"], r["photos_date"], r["keeper_uuid"], r["set_id"], r["lost_on"], album, r["archive_dest"] or ""])
    return len(rows)


# --- self test -------------------------------------------------------------


def self_test(writer: PhotosWriter, uuid: str, *, now: datetime | None = None) -> list[str]:
    """Prove the write path on one existing photo, then put everything back.

    Creates a throwaway album, adds the photo, sets a marker title and a
    marker date, reads both back, restores the originals, deletes the album.
    Raises RuntimeError on the first check that fails, after restoring.
    """
    lines: list[str] = []
    if not writer.exists(uuid):
        raise RuntimeError(f"photo {uuid} not found in Photos")
    original = writer.get(uuid)
    marker_title = f"photodedup self-test {_now(now)}"
    marker_date = datetime(2003, 2, 23, 9, 8, 8)
    album_id = None
    problems: list[str] = []
    try:
        album_id = writer.ensure_album(f"photodedup self-test {_now(now)}")
        added = writer.add_to_album(album_id, [uuid])
        lines.append(f"album add: {'ok' if added == [uuid] else 'FAILED'}")
        if added != [uuid]:
            problems.append("album add did not report the photo")
        writer.set_title(uuid, marker_title)
        got = writer.get(uuid).title
        lines.append(f"title round-trip: {'ok' if got == marker_title else 'FAILED'}")
        if got != marker_title:
            problems.append("title did not round-trip")
        writer.set_date(uuid, marker_date)
        back = writer.get(uuid).date
        drift = None if back is None else abs((back - marker_date).total_seconds())
        lines.append(f"date round-trip: {'ok' if drift == 0 else f'drift of {drift} seconds'} (wrote {marker_date.isoformat()}, read {back.isoformat() if back else None})")
        if drift != 0:
            problems.append(f"date drift of {drift} seconds")
    finally:
        writer.set_title(uuid, original.title)
        if original.date is not None:
            writer.set_date(uuid, original.date)
        if album_id is not None:
            writer.delete_album(album_id)
        lines.append("restored title and date, deleted the test album")
    if problems:
        raise RuntimeError("self-test failed: " + "; ".join(problems) + "\n" + "\n".join(lines))
    return lines
```

In `src/photodedup/cli.py` add:

```python
from photodedup.apply import (
    plan_lines, run_dates, run_merge, run_stage, self_test, write_losers_csv,
)
from photodedup.photos_write import PhotosUnavailable, PhotoscriptWriter


@main.command()
@click.option("--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True)
@click.option("--merge", "do_merge", is_flag=True, help="Merge loser albums, keywords, favorite and captions onto keepers.")
@click.option("--dates", "do_dates", is_flag=True, help="Correct keeper capture dates.")
@click.option("--stage", "do_stage", is_flag=True, help="Put losers in 'Dedup Delete' albums and write losers.csv.")
@click.option("--all", "do_all", is_flag=True, help="merge, dates, stage in that order.")
@click.option("--include-disagreeing-dates", is_flag=True, help="Also correct dates in sets flagged dates_disagree (sampled by you).")
@click.option("--dry-run", is_flag=True, help="Print every action without writing anything.")
@click.option("--batch", type=int, default=500, show_default=True)
@click.option("--self-test", "test_uuid", default=None, help="UUID of one existing photo: prove the write path on it and restore it.")
def apply(catalog: Path, do_merge: bool, do_dates: bool, do_stage: bool, do_all: bool,
          include_disagreeing_dates: bool, dry_run: bool, batch: int, test_uuid: str | None) -> None:
    """Apply the decisions to Photos. Never deletes anything. Interactive terminal only."""
    conn = open_catalog(catalog)
    if do_all:
        do_merge = do_dates = do_stage = True
    if test_uuid:
        try:
            for line in self_test(PhotoscriptWriter(), test_uuid):
                click.echo(line)
        except (PhotosUnavailable, RuntimeError) as exc:
            raise click.ClickException(str(exc)) from exc
        return
    if not (do_merge or do_dates or do_stage):
        try:
            for line in plan_lines(conn, include_disagreeing=include_disagreeing_dates):
                click.echo(line)
        except StageIncomplete as exc:
            raise click.ClickException(str(exc)) from exc
        return
    try:
        require_archive(conn)
        writer = _NullWriter() if dry_run else PhotoscriptWriter()
        if do_merge:
            r = run_merge(conn, writer, dry_run=dry_run)
            click.echo(f"merge: {r.done} done, {r.failed} failed, {r.skipped} already done")
        if do_dates:
            r = run_dates(conn, writer, include_disagreeing=include_disagreeing_dates, dry_run=dry_run)
            click.echo(f"dates: {r.done} done, {r.failed} failed, {r.skipped} already done")
        if do_stage:
            r = run_stage(conn, writer, batch=batch, dry_run=dry_run)
            n = write_losers_csv(conn, Path("losers.csv"))
            click.echo(f"stage: {r.done} done, {r.failed} failed, {r.skipped} already done; losers.csv lists {n}")
        if dry_run:
            for line in plan_lines(conn, include_disagreeing=include_disagreeing_dates):
                click.echo(line)
    except StageIncomplete as exc:
        raise click.ClickException(str(exc)) from exc
    except PhotosUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
```

and add near the top of `cli.py`:

```python
class _NullWriter:
    """Stands in for Photos on a dry run; every method raises if reached."""

    def __getattr__(self, name: str):
        raise AssertionError(f"dry run must not call writer.{name}")
```

Import `require_archive` with the other `photodedup.apply` names. In the `apply` command, `run_*` with `dry_run=True` never touch the writer (they skip before any call), so `_NullWriter` proves it.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass. `test_apply_real_write_needs_a_gui_session` passes because `PhotoscriptWriter()` raises `PhotosUnavailable` in this shell.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/apply.py src/photodedup/cli.py tests/test_apply.py tests/test_cli.py
git commit -m "feat: stage losers in Dedup Delete albums, losers.csv, apply self-test and CLI"
```

---

### Task 5: Verify, CLI, handoff

**Files:**
- Create: `src/photodedup/verify.py`
- Modify: `src/photodedup/cli.py` (add `verify`)
- Modify: `STATE.md`
- Test: `tests/test_verify.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `library.read_states` (Task 1), `apply_action`, `decision`, `decision_loser`.
- Produces: `run_verify(conn, *, states: Mapping[str, AssetState] | None = None) -> VerifyResult(losers_present, losers_gone, keepers_missing, dates_wrong, merges_missing, blank_present)`; `verify_report(result) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_verify.py`:

```python
import json
from datetime import datetime

import pytest

from photodedup.arbitrate import run_arbitrate
from photodedup.catalog import open_catalog, set_stage
from photodedup.library import AssetState
from photodedup.verify import run_verify, verify_report
from tests.test_apply import add_asset, add_set, archived  # helpers

NOW = datetime(2026, 9, 20, 12, 0, 0)


@pytest.fixture
def applied(tmp_path):
    conn = open_catalog(tmp_path / "catalog.sqlite")
    set_stage(conn, "group", "complete")
    add_asset(conn, "k", size=20, photos_date="2020-12-18T09:10:00", added="2020-12-18T18:00:00")
    add_asset(conn, "l", size=10, photos_date="2014-05-12T17:00:00", exif="2014-05-12T17:00:00", albums=("B",))
    add_set(conn, 1, ["k", "l"])
    run_arbitrate(conn, now=NOW)
    archived(conn, "l")
    for uuid, action, detail in (
        ("l", "stage", {"album": "Dedup Delete 001"}),
        ("k", "date", {"date": "2014-05-12T17:00:00", "set_id": 1}),
        ("k", "merge", {"albums": ["B"], "losers": ["l"]}),
    ):
        conn.execute(
            "INSERT INTO apply_action (uuid, action, status, detail, previous, applied_at) VALUES (?, ?, 'done', ?, '{}', 'now')",
            (uuid, action, json.dumps(detail)),
        )
    conn.commit()
    return conn


def test_everything_in_order_before_deletion(applied):
    states = {
        "k": AssetState(True, datetime(2014, 5, 12, 17, 0, 0), ("A", "B")),
        "l": AssetState(True, datetime(2014, 5, 12, 17, 0, 0), ("B", "Dedup Delete 001")),
    }
    r = run_verify(applied, states=states)
    assert (r.losers_present, r.losers_gone, r.keepers_missing, r.dates_wrong, r.merges_missing) == (
        [("l", "Dedup Delete 001")], [], [], [], []
    )
    text = verify_report(r)
    assert "0, as required" in text and "Dedup Delete 001" in text


def test_after_deletion_and_a_bad_date(applied):
    states = {
        "k": AssetState(True, datetime(2020, 12, 18, 9, 10, 0), ("A",)),
        "l": AssetState(False, None, ()),
    }
    r = run_verify(applied, states=states)
    assert r.losers_gone == ["l"] and r.losers_present == []
    assert r.dates_wrong == [("k", "2014-05-12T17:00:00", "2020-12-18T09:10:00")]
    assert r.merges_missing == [("k", "B")]


def test_missing_keeper_is_reported(applied):
    states = {"k": AssetState(False, None, ()), "l": AssetState(True, None, ())}
    r = run_verify(applied, states=states)
    assert r.keepers_missing == ["k"]
    assert "1 — STOP" in verify_report(r)
```

Append to `tests/test_cli.py`:

```python
def test_verify_command_exists():
    assert "verify" in run("--help").output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_verify.py tests/test_cli.py -q`
Expected: ModuleNotFoundError on `photodedup.verify`.

- [ ] **Step 3: Implement**

Create `src/photodedup/verify.py`:

```python
"""Re-read the library and report what the apply stage's writes look like now."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection

from photodedup.catalog import get_run, set_stage
from photodedup.library import AssetState, read_states


@dataclass(frozen=True)
class VerifyResult:
    losers_present: list[tuple[str, str]]     # (uuid, album)
    losers_gone: list[str]
    keepers_missing: list[str]
    dates_wrong: list[tuple[str, str, str]]   # (uuid, wanted, found)
    merges_missing: list[tuple[str, str]]     # (uuid, album)
    blank_present: list[str]


def run_verify(conn: Connection, *, states: Mapping[str, AssetState] | None = None) -> VerifyResult:
    set_stage(conn, "verify", "running")
    staged = {
        r["uuid"]: json.loads(r["detail"]).get("album", "")
        for r in conn.execute("SELECT uuid, detail FROM apply_action WHERE action = 'stage' AND status = 'done'")
    }
    blank = [r["uuid"] for r in conn.execute("SELECT uuid FROM apply_action WHERE action = 'stage_blank' AND status = 'done'")]
    keepers = [
        r["keeper_uuid"] for r in conn.execute(
            "SELECT DISTINCT d.keeper_uuid FROM decision d JOIN dup_set s ON s.id = d.set_id WHERE s.tier = 'exact' AND d.resolvable = 1"
        )
    ]
    dates = {
        r["uuid"]: json.loads(r["detail"])["date"]
        for r in conn.execute("SELECT uuid, detail FROM apply_action WHERE action = 'date' AND status = 'done'")
    }
    merges = {
        r["uuid"]: json.loads(r["detail"]).get("albums", [])
        for r in conn.execute("SELECT uuid, detail FROM apply_action WHERE action = 'merge' AND status = 'done'")
    }
    wanted = set(staged) | set(blank) | set(keepers) | set(dates) | set(merges)
    if states is None:
        states = read_states(Path(get_run(conn)["library_path"]), sorted(wanted))

    def state(uuid: str) -> AssetState:
        return states.get(uuid, AssetState(False, None, ()))

    losers_present = sorted((u, a) for u, a in staged.items() if state(u).present)
    losers_gone = sorted(u for u in staged if not state(u).present)
    keepers_missing = sorted(u for u in keepers if not state(u).present)
    dates_wrong = []
    for uuid, wanted_iso in sorted(dates.items()):
        s = state(uuid)
        if not s.present:
            continue
        found = s.date
        if found is None or abs((found - datetime.fromisoformat(wanted_iso)).total_seconds()) > 1:
            dates_wrong.append((uuid, wanted_iso, found.isoformat() if found else "missing"))
    merges_missing = sorted(
        (uuid, album) for uuid, albums in merges.items() for album in albums
        if state(uuid).present and album not in state(uuid).albums
    )
    blank_present = sorted(u for u in blank if state(u).present)
    result = VerifyResult(losers_present, losers_gone, keepers_missing, dates_wrong, merges_missing, blank_present)
    set_stage(
        conn, "verify", "complete",
        notes=f"{len(losers_present)} losers still present, {len(losers_gone)} gone, "
              f"{len(keepers_missing)} keepers missing, {len(dates_wrong)} dates wrong",
    )
    return result


def verify_report(r: VerifyResult) -> str:
    by_album: dict[str, int] = {}
    for _, album in r.losers_present:
        by_album[album] = by_album.get(album, 0) + 1
    lines = [
        "# Verify Report\n",
        f"- Staged losers still in the library: {len(r.losers_present)}",
        f"- Staged losers now gone: {len(r.losers_gone)}",
        f"- Keepers missing from the library: {len(r.keepers_missing)}"
        + (", as required" if not r.keepers_missing else " — STOP and investigate before deleting anything else"),
        f"- Corrected dates that did not take: {len(r.dates_wrong)}",
        f"- Merged albums the keeper is not in: {len(r.merges_missing)}",
        f"- Blank-image keepers still present: {len(r.blank_present)}\n",
        "## Losers still present, by album\n",
    ]
    for album in sorted(by_album):
        lines.append(f"- {album}: {by_album[album]}")
    if r.keepers_missing:
        lines.append("\n## Missing keepers\n")
        lines.extend(f"- {u}" for u in r.keepers_missing)
    if r.dates_wrong:
        lines.append("\n## Dates that did not take\n")
        lines.extend(f"- {u}: wanted {w}, found {f}" for u, w, f in r.dates_wrong)
    if r.merges_missing:
        lines.append("\n## Merges missing\n")
        lines.extend(f"- {u}: not in album {a}" for u, a in r.merges_missing)
    return "\n".join(lines) + "\n"
```

In `cli.py` add:

```python
from photodedup.verify import run_verify, verify_report


@main.command()
@click.option("--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True)
@click.option("--out", type=click.Path(path_type=Path), default=Path("reports/verify.md"), show_default=True)
def verify(catalog: Path, out: Path) -> None:
    """Re-read the library and report deletion progress, keeper safety, and dates. Read-only."""
    conn = open_catalog(catalog)
    try:
        result = run_verify(conn)
    except LibraryUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(verify_report(result))
    click.echo(
        f"wrote {out}: {len(result.losers_present)} losers still present, {len(result.losers_gone)} gone, "
        f"{len(result.keepers_missing)} keepers missing, {len(result.dates_wrong)} dates wrong"
    )
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Update STATE.md**

- Header: current phase → `Phase 4 (archive, apply, verify) — BUILD COMPLETE at HEAD; self-test and archive not yet run.` Add `**Phase 4 plan:**` and spec lines.
- "The one outstanding action" → the run order, in the user's terminal, from the repo root:

```
uv run photodedup apply --self-test <UUID of any one photo>   # proves the write path; restores the photo
uv run photodedup archive --limit 20                          # trial: 20 losers to the drive
uv run photodedup archive                                     # the rest; re-run until 0 mismatched, 0 missing
uv run photodedup apply                                       # prints the plan, writes nothing
uv run photodedup apply --dry-run --all --include-disagreeing-dates
uv run photodedup apply --merge
uv run photodedup apply --dates --include-disagreeing-dates
uv run photodedup apply --stage
uv run photodedup verify                                      # before deleting: all losers present, 0 keepers missing
```
  then: open Photos, open the folder "Dedup Delete", delete the contents of each album, and "Dedup Delete blank" after checking it; run `verify` again; empty Recently Deleted only after a clean verify and only when you are done with the archive as a fallback.
- A "Phase 4 progress" block with task SHAs (fill from `git log`).
- Under "Phase 4 obligations": mark the two apply gates as implemented (`--include-disagreeing-dates`; losers with no decision_loser rows are never staged because staging reads decision_loser), and keep the generation-counter note as future work.
- Modules: add `photos_write.py`, `archive.py`, `apply.py`, `verify.py`; `library.py` now also reads states and paths; `cli.py` gains `archive`, `apply`, `verify`.
- Note the photoscript stub change: the real module is tried first.

- [ ] **Step 6: Commit**

```bash
git add src/photodedup/verify.py src/photodedup/cli.py tests/test_verify.py tests/test_cli.py STATE.md
git commit -m "feat: verify stage and phase 4 handoff"
```

---

## Self-review against the spec

- Archive: Task 2 covers population, resumability, temp-name copy, streaming hash plus re-read, sidecar fields, manifest, free-space refusal, missing-source handling, `--limit`, edited/live files via `iter_paths`, stage left `running` on any mismatch.
- Apply merge/dates: Task 3 covers `carries` union per keeper, `previous`, idempotence, failure isolation, dry run, archive precondition, date gate, read-back verification.
- Apply stage: Task 4 covers batching under the folder, resume without reshuffling, blank album, `losers.csv`, self-test with restore, CLI including `_NullWriter` for dry runs and the GUI-session error.
- Verify: Task 5 covers every report line in the spec.
- Schema: Task 1, additive, version unchanged. Photoscript stub ordering fixed in Task 1.
- Type consistency: `PhotoState`/`PhotosWriter` (Task 1) match `FakePhotos` and every call in Tasks 3–4; `AssetState`/`read_states` (Task 1) match Task 5; `AssetPaths`/`iter_paths` (Task 1) match Task 2; `require_archive` defined in Task 3 and imported in Task 4's CLI; `archived()` test helper defined in Task 3 and imported by Task 5.
