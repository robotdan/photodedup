"""Read-only adapter over the Photos library.

Downstream stages consume AssetRecord and never touch osxphotos directly, so
they stay testable without a real library and a future direct-SQLite fallback
only has to satisfy this interface.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
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
    thumb_path: str | None = None


def _install_photoscript_stub() -> None:
    """Neutralise photoscript's import-time AppleScript compilation.

    osxphotos imports photoscript unconditionally, and photoscript compiles its
    AppleScript at module load. That compilation needs a GUI session context,
    which a headless or agent-spawned shell does not have, so the import raises
    `applescript.ScriptError ... found plural class name (-2741)` and takes all
    of osxphotos down with it, including the purely read-only database access
    this module needs.

    photoscript is only used for write operations, and Phase 1 performs none, so
    a stub whose every attribute raises on instantiation is safe. The real
    module is tried first; the stub is installed only when that import fails,
    so a process that can drive Photos keeps the real one.
    """
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


def _naive(value: datetime | None) -> datetime | None:
    """Drop tzinfo, keeping wall-clock time as the library displays it."""
    if value is None:
        return None
    return value.replace(tzinfo=None, microsecond=0)


def _tz_offset(value: datetime | None) -> int | None:
    if value is None or value.utcoffset() is None:
        return None
    return int(value.utcoffset().total_seconds())


def _thumb_path(photo: object) -> str | None:
    """The smallest derivative Photos keeps for *photo*, or None.

    osxphotos orders `path_derivatives` largest first. The lookup walks the
    library's derivatives directory and can fail for shared or syndicated
    assets; a missing thumbnail only costs the review report a placeholder,
    so failure here must never fail the survey.
    """
    try:
        derivatives = getattr(photo, "path_derivatives", None) or []
    except Exception:
        return None
    return str(derivatives[-1]) if derivatives else None


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
            thumb_path=_thumb_path(photo),
        )


@dataclass(frozen=True)
class AssetState:
    present: bool
    date: datetime | None
    albums: tuple[str, ...]
    # Last, so existing positional constructions keep working. A photo the
    # user has deleted in Photos is still in the library database until
    # Recently Deleted is emptied: it is neither present nor gone, and the
    # difference is the whole point of running `verify` after a deletion.
    in_trash: bool = False


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
        present = photo is not None and not bool(getattr(photo, "intrash", False))
        in_trash = photo is not None and bool(getattr(photo, "intrash", False))
        if photo is None:
            out[uuid] = AssetState(False, None, (), False)
        else:
            out[uuid] = AssetState(
                present, _naive(photo.date), tuple(photo.albums or ()), in_trash
            )
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
