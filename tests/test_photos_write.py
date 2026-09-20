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
