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

    return _iter_records(db)


def _iter_records(db: object) -> Iterator[AssetRecord]:
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
