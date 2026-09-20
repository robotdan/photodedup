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
        already = {p.uuid for p in album.photos()}
        todo = [self._photo(u) for u in uuids if u not in already]
        if not todo:
            return []
        added = album.add(todo)
        return [p.uuid for p in added]

    def delete_album(self, album_id: str) -> None:
        self._lib.delete_album(self._ps.Album(album_id))
