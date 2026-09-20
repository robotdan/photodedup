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


def test_photoscript_writer_drives_the_real_api_shapes(monkeypatch):
    """End-to-end PhotoscriptWriter against a stand-in photoscript module whose
    classes mirror the real package's shapes exactly (verified against
    .venv/lib/python3.13/site-packages/photoscript/__init__.py):

    - PhotosLibrary.album (line 289) and .folder (line 357) return None when
      absent; .create_album (line 323) and .create_folder (line 416) create;
      .delete_album (line 349) takes an Album.
    - Folder.album(name) (line 987) is a method returning Album | None;
      Folder.create_album(name) (line 1010) is a method.
    - Album.id (set as a plain attribute in __init__, line ~618) is not a
      property; Album.photos (line 682) is a METHOD, not a property -- this
      is the exact shape that caught the `album.photos` vs `album.photos()`
      bug; Album.add (line 687) is a method returning the added Photo
      objects; Album.name (line 629 @property / 630 def) is a property.
    - Photo(uuid) raises ValueError for an unknown uuid (line 1044); .title
      (1065/1070), .description (1076/1082), .keywords (1088/1096),
      .favorite (1102/1107) and .date (1163/1168) are properties with
      setters; .albums (line 1181 @property / 1182 def) is a property
      returning Album objects.
    """
    import sys
    import types

    photos: dict[str, dict] = {
        "a": {
            "title": "", "description": "", "keywords": [], "favorite": False,
            "date": None, "albums": [],
        }
    }
    albums: dict[str, dict] = {}   # id -> {"name", "folder" (folder id or None), "members": [uuid,...]}
    folders: dict[str, dict] = {}  # id -> {"name"}

    class Photo:
        def __init__(self, uuid):
            if uuid not in photos:
                raise ValueError(f"Invalid photo id: {uuid}")
            self.uuid = uuid

        @property
        def title(self):
            return photos[self.uuid]["title"]

        @title.setter
        def title(self, value):
            photos[self.uuid]["title"] = value

        @property
        def description(self):
            return photos[self.uuid]["description"]

        @description.setter
        def description(self, value):
            photos[self.uuid]["description"] = value

        @property
        def keywords(self):
            return photos[self.uuid]["keywords"]

        @keywords.setter
        def keywords(self, value):
            photos[self.uuid]["keywords"] = list(value)

        @property
        def favorite(self):
            return photos[self.uuid]["favorite"]

        @favorite.setter
        def favorite(self, value):
            photos[self.uuid]["favorite"] = bool(value)

        @property
        def date(self):
            return photos[self.uuid]["date"]

        @date.setter
        def date(self, value):
            photos[self.uuid]["date"] = value

        @property
        def albums(self):
            return [Album(album_id) for album_id in photos[self.uuid]["albums"]]

    class Album:
        def __init__(self, album_id):
            if album_id not in albums:
                raise ValueError(f"Invalid album id: {album_id}")
            self.id = album_id

        @property
        def name(self):
            return albums[self.id]["name"]

        def photos(self):
            return [Photo(uuid) for uuid in albums[self.id]["members"]]

        def add(self, photos_to_add):
            added = []
            for p in photos_to_add:
                if p.uuid not in albums[self.id]["members"]:
                    albums[self.id]["members"].append(p.uuid)
                    photos[p.uuid]["albums"].append(self.id)
                    added.append(p)
            return added

    class Folder:
        def __init__(self, folder_id):
            if folder_id not in folders:
                raise ValueError(f"Invalid folder id: {folder_id}")
            self.id = folder_id

        def album(self, name):
            for album_id, a in albums.items():
                if a["folder"] == self.id and a["name"] == name:
                    return Album(album_id)
            return None

        def create_album(self, name):
            album_id = f"album-{len(albums) + 1}"
            albums[album_id] = {"name": name, "folder": self.id, "members": []}
            return Album(album_id)

    class PhotosLibrary:
        def folder(self, name, top_level=True):
            for folder_id, f in folders.items():
                if f["name"] == name:
                    return Folder(folder_id)
            return None

        def create_folder(self, name):
            folder_id = f"folder-{len(folders) + 1}"
            folders[folder_id] = {"name": name}
            return Folder(folder_id)

        def album(self, name, top_level=True):
            for album_id, a in albums.items():
                if a["folder"] is None and a["name"] == name:
                    return Album(album_id)
            return None

        def create_album(self, name):
            album_id = f"album-{len(albums) + 1}"
            albums[album_id] = {"name": name, "folder": None, "members": []}
            return Album(album_id)

        def delete_album(self, album):
            del albums[album.id]

    stub = types.ModuleType("photoscript")
    stub.PhotosLibrary = PhotosLibrary
    stub.Photo = Photo
    stub.Album = Album
    stub.Folder = Folder
    monkeypatch.setitem(sys.modules, "photoscript", stub)

    writer = PhotoscriptWriter()

    album_id = writer.ensure_album("T", folder="F")
    assert writer.add_to_album(album_id, ["a"]) == ["a"]
    assert writer.add_to_album(album_id, ["a"]) == []

    writer.set_title("a", "Beach")
    writer.set_description("a", "sand")
    writer.set_keywords("a", ["x", "y"])
    writer.set_favorite("a", True)
    writer.set_date("a", datetime(2014, 5, 12, 17, 0, 0))

    state = writer.get("a")
    assert state.title == "Beach"
    assert state.description == "sand"
    assert state.keywords == ("x", "y")
    assert state.favorite is True
    assert state.date == datetime(2014, 5, 12, 17, 0, 0)
    assert state.albums == ("T",)

    writer.delete_album(album_id)
    assert album_id not in albums
