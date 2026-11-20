"""The one place that writes to Photos.

Wraps photoscript behind a small protocol so every apply step can be tested
against an in-memory fake, and so the rest of the package never imports
photoscript: its AppleScript compiles only inside a GUI session.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

SHARED_ALBUM = 4
SHARED_WITH_YOU = 12
# Saved-asset types that sit in Photos.sqlite outside the user's own library.
NOT_IN_LIBRARY = frozenset({SHARED_ALBUM, SHARED_WITH_YOU})
DEFAULT_LIBRARY = Path.home() / "Pictures" / "Photos Library.photoslibrary"


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


class LibraryIndex:
    """Read-only lookups in Photos.sqlite that AppleScript makes slow.

    photoscript finds a photo's albums by listing every media item of every
    album in the library, several seconds to minutes per call on a large
    library. The database answers the same question in milliseconds. A fresh
    statement on an autocommit connection sees Photos' latest commit.
    """

    def __init__(self, library: Path) -> None:
        db = Path(library) / "database" / "Photos.sqlite"
        self._conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        self._join, self._album_col, self._asset_col = self._album_join()

    def _album_join(self) -> tuple[str, str, str]:
        # The join table's name carries Core Data entity numbers that move
        # between Photos versions (Z_34ASSETS with Z_34ALBUMS and Z_3ASSETS here).
        for (name,) in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'Z\\_%ASSETS' ESCAPE '\\'"
        ):
            cols = [r[1] for r in self._conn.execute(f"PRAGMA table_info({name})")]
            albums = [c for c in cols if c.endswith("ALBUMS")]
            assets = [c for c in cols if c.endswith("ASSETS")]
            if albums and assets:
                return name, albums[0], assets[0]
        raise PhotosUnavailable("cannot find the album membership table in Photos.sqlite")

    def exists(self, uuid: str) -> bool:
        # Without the hint SQLite walks a trash-state index (13 ms a call).
        try:
            row = self._conn.execute(
                "SELECT ZTRASHEDSTATE, ZSAVEDASSETTYPE FROM ZASSET INDEXED BY Z_Asset_byUuidIndex WHERE ZUUID = ?", (uuid,)
            ).fetchone()
        except sqlite3.OperationalError:
            row = self._conn.execute("SELECT ZTRASHEDSTATE, ZSAVEDASSETTYPE FROM ZASSET WHERE ZUUID = ?",
                                     (uuid,)).fetchone()
        # Type 12 is a "Shared with You" item from Messages, which AppleScript
        # cannot reach. Type 4 is an iCloud Shared Album photo: adding one to an
        # album makes Photos import a new copy and add that instead, and
        # photoscript retries the add five times, so each try is another copy.
        return row is not None and not row[0] and row[1] not in NOT_IN_LIBRARY

    def albums(self, uuid: str) -> tuple[str, ...]:
        """Names of the user albums (any folder) that hold the photo."""
        return tuple(sorted(r[0] for r in self._conn.execute(
            # CROSS JOIN pins the order: start from the one photo, not from
            # every album's member list.
            f"""SELECT g.ZTITLE FROM ZASSET a
                CROSS JOIN {self._join} j ON j.{self._asset_col} = a.Z_PK
                CROSS JOIN ZGENERICALBUM g ON g.Z_PK = j.{self._album_col}
                WHERE a.ZUUID = ? AND g.ZKIND = 2 AND g.ZTRASHEDSTATE = 0 AND g.ZTITLE IS NOT NULL""",
            (uuid,),
        )))


class PhotoscriptWriter:
    """photoscript-backed writer. Construct only from an interactive session.

    With a library path, existence and album membership come from the
    library's database (LibraryIndex) instead of AppleScript.
    """

    def __init__(self, library: Path | None = None) -> None:
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
        self._index = LibraryIndex(library) if library is not None else None

    def _photo(self, uuid: str):
        return self._ps.Photo(uuid)

    def exists(self, uuid: str) -> bool:
        if self._index is not None:
            return self._index.exists(uuid)
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
            albums=self._index.albums(uuid) if self._index is not None else tuple(a.name for a in p.albums),
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
            # No top_level filter on the lookup: an album of this name may
            # already live inside a folder (the user filed it there), and
            # creating a second one with the same name would split the
            # staging across two albums. Creation stays top level.
            album = self._lib.album(name)
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
        """Add *uuids* to the album, returning what Photos says it added.

        The album is never enumerated first. A Photos album is a set, so
        re-adding a photo it already holds is a no-op there, and every
        caller already knows the membership it cares about (merge reads it
        from `state.albums`, staging from its own `apply_action` rows).
        Enumerating instead costs one AppleScript round trip per album over
        an album that can hold 500 photos.
        """
        album = self._ps.Album(album_id)
        photos = [self._photo(u) for u in uuids]
        if not photos:
            return []
        return [p.uuid for p in album.add(photos) or ()]

    def delete_album(self, album_id: str) -> None:
        self._lib.delete_album(self._ps.Album(album_id))
