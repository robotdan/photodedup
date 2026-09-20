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
