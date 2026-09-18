"""Command line entry point."""

from __future__ import annotations

from pathlib import Path

import click

from photodedup.catalog import open_catalog
from photodedup.library import LibraryUnavailable, iter_assets
from photodedup.report import survey_report
from photodedup.survey import (
    MISSING_ORIGINALS_TOLERANCE,
    MissingOriginalsError,
    run_survey,
)

DEFAULT_LIBRARY = Path.home() / "Pictures" / "Photos Library.photoslibrary"
DEFAULT_CATALOG = Path("catalog.sqlite")


@click.group()
def main() -> None:
    """De-duplicate an Apple Photos library while preserving accurate metadata."""


@main.command()
@click.option(
    "--library",
    type=click.Path(path_type=Path),
    default=DEFAULT_LIBRARY,
    show_default=True,
)
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
@click.option(
    "--missing-tolerance",
    type=float,
    default=MISSING_ORIGINALS_TOLERANCE,
    show_default=True,
    help=(
        "Fraction of assets allowed to have no local original before the survey "
        "refuses to run. Such assets yield no EXIF, so raise this only when you "
        "know the missing ones do not bear on your question."
    ),
)
def survey(library: Path, catalog: Path, missing_tolerance: float) -> None:
    """Read the library into the catalog. Read-only, resumable."""
    conn = open_catalog(catalog)
    try:
        result = run_survey(
            conn,
            iter_assets(library),
            library_path=str(library),
            missing_tolerance=missing_tolerance,
        )
    except (LibraryUnavailable, MissingOriginalsError) as exc:
        raise click.ClickException(str(exc)) from exc
    # Lead with the total in scope. On a fully resumed run `written` is 0,
    # which reads as "the survey did nothing" if it comes first.
    click.echo(
        f"{result.total} assets in scope "
        f"({result.written} written this run, "
        f"{result.total - result.written - result.errors} already complete, "
        f"{result.errors} errors)"
    )
    click.echo(
        f"{result.skipped_after_cutoff} skipped after cutoff, "
        f"{result.missing_originals} without local originals"
    )


@main.command()
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=Path("reports/survey.md"),
    show_default=True,
)
def report(catalog: Path, out: Path) -> None:
    """Write the Markdown survey report."""
    conn = open_catalog(catalog)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(survey_report(conn))
    click.echo(f"wrote {out}")
