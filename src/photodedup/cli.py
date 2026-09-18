"""Command line entry point."""

from __future__ import annotations

from pathlib import Path

import click

from photodedup.catalog import open_catalog
from photodedup.library import LibraryUnavailable, iter_assets
from photodedup.report import survey_report
from photodedup.survey import MissingOriginalsError, run_survey

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
def survey(library: Path, catalog: Path) -> None:
    """Read the library into the catalog. Read-only, resumable."""
    conn = open_catalog(catalog)
    try:
        result = run_survey(conn, iter_assets(library), library_path=str(library))
    except (LibraryUnavailable, MissingOriginalsError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"surveyed {result.total} assets: {result.written} written, "
        f"{result.skipped_after_cutoff} after cutoff, "
        f"{result.missing_originals} without originals, {result.errors} errors"
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
