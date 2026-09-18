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
