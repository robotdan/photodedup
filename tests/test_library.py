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


def test_missing_library_raises_eagerly_without_iterating(tmp_path):
    """iter_assets must validate before returning, not on first next()."""
    with pytest.raises(LibraryUnavailable) as excinfo:
        iter_assets(tmp_path / "Nope.photoslibrary")
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


def test_asset_record_thumb_path_defaults_to_none():
    assert AssetRecord(uuid="u1", kind="image").thumb_path is None


def test_iter_records_takes_the_smallest_derivative():
    from photodedup.library import _iter_records

    class Photo:
        uuid = "u1"
        ismovie = False
        filename = original_filename = "a.jpg"
        uti = "public.jpeg"
        width = height = 10
        path = "/tmp/a.jpg"
        date_added = date = None
        live_photo = has_raw = hasadjustments = favorite = hidden = intrash = False
        title = description = None
        location = None
        keywords = albums = ()
        path_derivatives = ["/lib/big.jpeg", "/lib/small.jpeg"]

    class DB:
        def photos(self, **kwargs):
            return [Photo()]

    [record] = list(_iter_records(DB()))
    assert record.thumb_path == "/lib/small.jpeg"


def test_iter_records_tolerates_derivative_lookup_failure():
    from photodedup.library import _iter_records

    class Photo:
        uuid = "u1"
        ismovie = False
        filename = original_filename = "a.jpg"
        uti = "public.jpeg"
        width = height = 10
        path = "/tmp/a.jpg"
        date_added = date = None
        live_photo = has_raw = hasadjustments = favorite = hidden = intrash = False
        title = description = None
        location = None
        keywords = albums = ()

        @property
        def path_derivatives(self):
            raise RuntimeError("no derivatives directory")

    class DB:
        def photos(self, **kwargs):
            return [Photo()]

    [record] = list(_iter_records(DB()))
    assert record.thumb_path is None


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
        def __init__(self, uuid, date, albums, intrash=False):
            self.uuid, self.date, self.albums = uuid, date, albums
            self.intrash = intrash

    class DB:
        def get_photo(self, uuid):
            return {
                "a": Photo("a", datetime(2014, 5, 12, 17, 0, 0), ["Trips"]),
                "trashed": Photo("trashed", datetime(2014, 5, 12, 17, 0, 0), ["Dedup Delete 001"], True),
            }.get(uuid)

    states = _read_states_from_db(DB(), ["a", "gone", "trashed"])
    assert states["a"] == AssetState(True, datetime(2014, 5, 12, 17, 0, 0), ("Trips",), False)
    assert states["gone"] == AssetState(False, None, (), False)


def test_read_states_counts_a_trashed_photo_as_trashed_not_present_or_gone():
    """A photo deleted in Photos stays in the library database until Recently
    Deleted is emptied. Calling it present would read as "the deletion did not
    happen"; calling it gone would read as "the archive fallback is the only
    copy left". It is neither."""
    from datetime import datetime

    from photodedup.library import _read_states_from_db

    class Photo:
        uuid = "t"
        date = datetime(2014, 5, 12, 17, 0, 0)
        albums = ["Dedup Delete 001"]
        intrash = True

    class DB:
        def get_photo(self, uuid):
            return Photo() if uuid == "t" else None

    states = _read_states_from_db(DB(), ["t", "gone"])
    assert states["t"].in_trash is True and states["t"].present is False
    assert states["gone"].in_trash is False and states["gone"].present is False


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
