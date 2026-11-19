"""Command line entry point."""

from __future__ import annotations

import csv
from datetime import timedelta
from pathlib import Path

import click

from photodedup.apply import (
    plan_lines, require_archive, run_dates, stale_date_targets, file_date_targets, run_merge, run_stage, self_test, write_losers_csv,
)
from photodedup.arbitrate import run_arbitrate
from photodedup.archive import ArchiveDestination, ArchiveSpace, run_archive
from photodedup.catalog import (
    get_run,
    StageIncomplete,
    get_stage,
    open_catalog,
    require_stage_complete,
    set_stage,
)
from photodedup.grouping import HashStageIncomplete, run_group
from photodedup.hash_stage import HashStageInterrupted, run_hash
from photodedup.library import LibraryUnavailable, album_members, iter_assets, iter_paths, read_states
from photodedup.pairarchive import archived, read_pairs_rows, run_pair_archive
from photodedup.pairplan import plan_catalog
from photodedup.pairs import PAIRS_ALBUM, PairResult, read_pairs, resolve_pairs
from photodedup.photos_write import PhotosUnavailable, PhotoscriptWriter
from photodedup.report import duplicate_report, survey_report
from photodedup.review import write_review
from photodedup.sets import run_sets
from photodedup.survey import (
    MISSING_ORIGINALS_TOLERANCE,
    MissingOriginalsError,
    run_survey,
)
from photodedup.verify import run_verify, verify_report

DEFAULT_LIBRARY = Path.home() / "Pictures" / "Photos Library.photoslibrary"
DEFAULT_CATALOG = Path("catalog.sqlite")
DEFAULT_ARCHIVE = Path("/Volumes/Extreme Pro/photodedup")


class _NullWriter:
    """Stands in for Photos on a dry run; every method raises if reached."""

    def __getattr__(self, name: str):
        raise AssertionError(f"dry run must not call writer.{name}")


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


@main.command()
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
@click.option("--workers", type=int, default=None, help="Parallel workers. Defaults to CPU count minus one.")
@click.option("--limit", type=int, default=None, help="Hash at most this many assets, for a trial run.")
def hash(catalog: Path, workers: int | None, limit: int | None) -> None:
    """Compute content hashes for every surveyed asset. Read-only, resumable."""
    conn = open_catalog(catalog)
    try:
        result = run_hash(
            conn,
            workers=workers,
            limit=limit,
            progress=lambda done, total: click.echo(f"  {done}/{total}", err=True),
        )
    except HashStageInterrupted as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"{result.total} assets to hash "
        f"({result.hashed} hashed, {result.truncated} truncated, {result.no_original} without originals, "
        f"{result.errors} errors); {result.already_done} already complete"
    )


@main.command()
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
@click.option(
    "--force",
    is_flag=True,
    help="Group even though the hash stage has not reported complete.",
)
def group(catalog: Path, force: bool) -> None:
    """Form duplicate groups from the content hashes. Decides nothing."""
    conn = open_catalog(catalog)
    try:
        result = run_group(conn, force=force)
    except HashStageIncomplete as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"{result.groups} groups covering {result.members} assets; "
        f"{result.exact_sets} exact sets, {result.candidate_sets} candidate sets "
        f"(see `photodedup duplicates` for removable counts)"
    )


@main.command()
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=Path("reports/duplicates.md"),
    show_default=True,
)
def duplicates(catalog: Path, out: Path) -> None:
    """Write the Markdown duplicate report."""
    conn = open_catalog(catalog)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(duplicate_report(conn))
    click.echo(f"wrote {out}")


@main.command()
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
@click.option("--force", is_flag=True, help="Rebuild sets even though the group stage has not reported complete.")
def sets(catalog: Path, force: bool) -> None:
    """Rebuild duplicate sets from the groups. `group` does this itself; use
    this after a manual fix to the groups."""
    conn = open_catalog(catalog)
    try:
        require_stage_complete(conn, "group", force=force)
    except StageIncomplete as exc:
        raise click.ClickException(str(exc)) from exc
    set_stage(conn, "sets", "running")
    result = run_sets(conn)
    notes = (
        f"{result.exact_sets} exact sets over {result.exact_assets} assets; "
        f"{result.candidate_sets} candidate sets over {result.candidate_assets} assets"
    )
    set_stage(conn, "sets", "complete", notes=notes)
    click.echo(notes)


@main.command()
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
@click.option(
    "--margin-hours", type=float, default=26.0, show_default=True,
    help="Dates closer than this are one date rendered in two timezones, not a conflict.",
)
@click.option("--force", is_flag=True, help="Arbitrate even though the group stage has not reported complete.")
def arbitrate(catalog: Path, margin_hours: float, force: bool) -> None:
    """Decide the keeper and capture date for every duplicate set. Writes only
    to the catalog; re-run freely after changing rules."""
    conn = open_catalog(catalog)
    try:
        result = run_arbitrate(conn, margin=timedelta(hours=margin_hours), force=force)
    except StageIncomplete as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"{result.sets} sets: {result.resolvable} resolvable, {result.review} need review, "
        f"{result.dates_rescued} dates rescued"
    )


@main.command()
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
@click.option(
    "--out", type=click.Path(path_type=Path), default=Path("reports/review"), show_default=True
)
@click.option("--per-page", type=int, default=250, show_default=True)
@click.option("--sample", type=int, default=300, show_default=True, help="Sets per sampled section.")
@click.option(
    "--margin-hours", type=float, default=26.0, show_default=True,
    help="Dates closer than this are one date rendered in two timezones, not a conflict.",
)
@click.option("--force", is_flag=True, help="Write the report even though arbitrate has not reported complete.")
def review(
    catalog: Path, out: Path, per_page: int, sample: int, margin_hours: float, force: bool
) -> None:
    """Write the static HTML review report. Open <out>/index.html in a browser."""
    conn = open_catalog(catalog)
    try:
        result = write_review(
            conn, out, per_page=per_page, sample=sample, force=force,
            margin=timedelta(hours=margin_hours),
        )
    except (StageIncomplete, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"wrote {out / 'index.html'}: {result.sets} sets on {result.pages} pages, "
        f"{result.thumbs} thumbnails, {result.placeholders} placeholders"
    )


@main.command()
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
@click.option(
    "--root", type=click.Path(path_type=Path), default=DEFAULT_ARCHIVE, show_default=True
)
@click.option("--limit", type=int, default=None, help="Copy at most this many losers, for a trial run.")
def archive(catalog: Path, root: Path, limit: int | None) -> None:
    """Copy every loser's original to the archive and verify it. Read-only against Photos; resumable."""
    conn = open_catalog(catalog)
    try:
        result = run_archive(
            conn, root, limit=limit,
            progress=lambda done, total: click.echo(f"  {done}/{total}", err=True),
        )
    except (StageIncomplete, ArchiveSpace, ArchiveDestination, LibraryUnavailable) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"{result.copied} copied ({result.bytes} bytes), {result.skipped} already archived, "
        f"{result.mismatched} mismatched (will retry), {result.missing} missing sources"
    )
    stage = get_stage(conn, "archive")
    status = stage["status"] if stage is not None else "unknown"
    click.echo(f"archive stage: {status}")
    if status != "complete":
        click.echo("re-run `photodedup archive` once every file copies and verifies clean")


@main.command()
@click.option("--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True)
@click.option("--merge", "do_merge", is_flag=True, help="Merge loser albums, keywords, favorite and captions onto keepers.")
@click.option("--dates", "do_dates", is_flag=True, help="Correct keeper capture dates.")
@click.option("--redo-dates", is_flag=True, help="Re-apply dates already recorded done (after a verify found them wrong).")
@click.option("--fix-stale-dates", is_flag=True, help="Re-write keeper dates recorded done that the current date rules no longer choose (implies --dates).")
@click.option("--dates-from", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="CSV of uuid,date (the time Photos should show): write exactly those dates (implies --dates).")
@click.option("--sets", "date_sets", default=None, help="Comma-separated set ids: write the set's date to EVERY member (reviewed look-alike sets).")
@click.option("--stage", "do_stage", is_flag=True, help="Put losers in 'Dedup Delete' albums and write losers.csv.")
@click.option("--all", "do_all", is_flag=True, help="merge, dates, stage in that order.")
@click.option("--include-disagreeing-dates", is_flag=True, help="Also correct dates in sets flagged dates_disagree (sampled by you).")
@click.option("--dry-run", is_flag=True, help="Print every action without writing anything.")
@click.option("--batch", type=int, default=500, show_default=True)
@click.option(
    "--losers-csv", type=click.Path(path_type=Path), default=Path("losers.csv"), show_default=True,
    help="Where to write the staged-loser manifest.",
)
@click.option("--self-test", "test_uuid", default=None, help="UUID of one existing photo: prove the write path on it and restore it.")
def apply(catalog: Path, do_merge: bool, do_dates: bool, do_stage: bool, do_all: bool,
          include_disagreeing_dates: bool, dry_run: bool, batch: int, losers_csv: Path, redo_dates: bool,
          date_sets: str | None, fix_stale_dates: bool, dates_from: Path | None,
          test_uuid: str | None) -> None:
    """Apply the decisions to Photos. Never deletes anything. Interactive terminal only."""
    conn = open_catalog(catalog)
    if do_all:
        do_merge = do_dates = do_stage = True
    stale = None
    if fix_stale_dates and dates_from:
        raise click.UsageError("use --fix-stale-dates or --dates-from, not both")
    if dates_from:
        do_dates = True
        try:
            stale = file_date_targets(dates_from)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"{len(stale)} dates from {dates_from}:")
        for t in stale:
            click.echo(f"  {t.keeper} -> {t.value.isoformat()}")
    if fix_stale_dates:
        do_dates = True
        stale = stale_date_targets(conn)
        click.echo(f"{len(stale)} stale keeper dates:")
        for t in stale:
            click.echo(f"  {t.keeper} (set {t.set_id}) -> {t.value.isoformat()}")
    if test_uuid:
        if dry_run:
            raise click.UsageError("--self-test performs real (restored) writes; drop --dry-run")
        try:
            for line in self_test(PhotoscriptWriter(), test_uuid):
                click.echo(line)
        except (PhotosUnavailable, RuntimeError) as exc:
            raise click.ClickException(str(exc)) from exc
        return
    if not (do_merge or do_dates or do_stage):
        try:
            for line in plan_lines(conn, include_disagreeing=include_disagreeing_dates):
                click.echo(line)
        except StageIncomplete as exc:
            raise click.ClickException(str(exc)) from exc
        return
    try:
        require_archive(conn)
        writer = _NullWriter() if dry_run else PhotoscriptWriter()
        notes: list[str] = []
        if do_merge:
            r = run_merge(conn, writer, dry_run=dry_run)
            line = f"merge: {r.done} done, {r.failed} failed, {r.skipped} already done"
            click.echo(line)
            notes.append(line)
        if do_dates:
            run_row = get_run(conn)
            library = Path(run_row["library_path"]) if run_row else None

            def displayed(uuids: list[str]) -> dict:
                # The date Photos SHOWS, read the way `verify` reads it.
                return {u: s.date for u, s in read_states(library, uuids).items()}

            r = run_dates(
                conn, writer, include_disagreeing=include_disagreeing_dates, dry_run=dry_run,
                displayed=displayed if (library and not dry_run) else None, redo=redo_dates,
                sets=[int(x) for x in date_sets.split(",")] if date_sets else None,
                targets=stale,
            )
            line = f"dates: {r.done} done, {r.failed} failed, {r.skipped} already done"
            click.echo(line)
            notes.append(line)
        if do_stage:
            run_row = get_run(conn)
            library = Path(run_row["library_path"]) if run_row else None

            def membership(names: list[str]) -> dict:
                # What Photos' own database says is in each album.
                return album_members(library, names)

            r = run_stage(
                conn, writer, batch=batch, dry_run=dry_run,
                membership=membership if (library and not dry_run) else None,
            )
            line = f"stage: {r.done} done, {r.failed} failed, {r.skipped} already done"
            if not dry_run:
                n = write_losers_csv(conn, losers_csv)
                line += f"; {losers_csv} lists {n}"
            click.echo(line)
            notes.append(line)
        if dry_run:
            for line in plan_lines(conn, include_disagreeing=include_disagreeing_dates):
                click.echo(line)
        elif notes:
            set_stage(conn, "apply", "complete", notes="; ".join(notes))
    except StageIncomplete as exc:
        raise click.ClickException(str(exc)) from exc
    except PhotosUnavailable as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.option("--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True)
@click.option("--out", type=click.Path(path_type=Path), default=Path("reports/verify.md"), show_default=True)
def verify(catalog: Path, out: Path) -> None:
    """Re-read the library and report deletion progress, keeper safety, and dates. Read-only."""
    conn = open_catalog(catalog)
    try:
        result = run_verify(conn)
    except LibraryUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(verify_report(result))
    click.echo(
        f"wrote {out}: {len(result.losers_present)} losers still present, "
        f"{len(result.losers_trashed)} in Recently Deleted, {len(result.losers_gone)} gone, "
        f"{len(result.keepers_missing)} keepers missing, {len(result.dates_wrong)} dates wrong, "
        f"{len(result.stale_staged)} staged photos no longer losers"
    )


@main.command()
@click.argument("pairs_csv", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--dry-run", is_flag=True, help="Show what each keeper would receive; write nothing.")
@click.option("--log", type=click.Path(path_type=Path), default=Path("reports/pairs_result.csv"), show_default=True)
@click.option("--archived-in", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
              help="Catalog whose archive must hold each loser's verified original; losers without one are skipped.")
def pairs(pairs_csv: Path, dry_run: bool, log: Path, archived_in: Path | None) -> None:
    """Resolve hand-picked duplicate pairs (CSV with keeper,loser columns):
    copy each loser's albums, keywords, favorite and captions onto its keeper,
    confirm, then put the loser in the 'Dedup Delete pairs' album. Never
    deletes anything. Interactive terminal only."""
    try:
        chosen = read_pairs(pairs_csv)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    unarchived: list[PairResult] = []
    if archived_in is not None:
        have = archived(open_catalog(archived_in), [p.loser for p in chosen])
        unarchived = [PairResult(p, "skipped", "loser not archived") for p in chosen if p.loser not in have]
        chosen = [p for p in chosen if p.loser in have]
    try:
        results = unarchived + resolve_pairs(PhotoscriptWriter(), chosen, dry_run=dry_run)
    except PhotosUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["keeper", "loser", "status", "detail"])
        for r in results:
            w.writerow([r.pair.keeper, r.pair.loser, r.status, r.detail])
            if r.status != "staged":
                click.echo(f"  {r.status}: {r.pair.loser} -> {r.pair.keeper}: {r.detail}")
    counts = {s: sum(r.status == s for r in results) for s in ("staged", "failed", "skipped")}
    click.echo(
        f"pairs: {counts['staged']} staged in '{PAIRS_ALBUM}', {counts['failed']} failed, "
        f"{counts['skipped']} skipped; log in {log}"
    )


@main.command("plan-pairs")
@click.option("--catalog", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--out", type=click.Path(path_type=Path), required=True, help="Pairs CSV for `archive-pairs` and `pairs`.")
@click.option("--dates-out", type=click.Path(path_type=Path), required=True, help="uuid,date CSV for `apply --dates-from`.")
@click.option("--review-out", type=click.Path(path_type=Path), required=True, help="Sets left for a person to decide.")
@click.option("--exclude", "excludes", multiple=True, type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="A pairs CSV already decided; sets touching its photos are left out.")
def plan_pairs(catalog: Path, out: Path, dates_out: Path, review_out: Path, excludes: tuple[Path, ...]) -> None:
    """Plan keepers and losers for sets that are one shot saved more than once. Read-only."""
    conn = open_catalog(catalog)
    skip = {u for f in excludes for r in read_pairs_rows(f) for u in (r["keeper"], r["loser"])}
    plans = plan_catalog(conn, exclude=skip)
    planned = [p for p in plans if p.status == "planned"]
    for f in (out, dates_out, review_out):
        f.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["keeper", "loser", "filename", "why", "review_original"])
        for p in planned:
            k = p.keeper
            for m in p.losers:
                why = (f"set {p.set_id}; keeper {k.filename} {k.width}x{k.height} added {k.date_added[:10]}; "
                       f"loser {m.width}x{m.height} dated {m.photos_date} added {m.date_added[:10]}")
                w.writerow([k.uuid, m.uuid, m.filename, why, "1" if m is p.review_original else ""])
    with dates_out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["uuid", "date", "filename", "was"])
        for p in planned:
            if p.date is not None:
                w.writerow([p.keeper.uuid, p.date.isoformat(), p.keeper.filename, p.keeper.photos_date.isoformat()])
    with review_out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["set_id", "reason", "uuid", "filename", "date", "added", "size", "pixels"])
        rows = conn.execute(
            "SELECT m.set_id, a.uuid, a.original_filename, a.photos_date, a.date_added, a.file_size, a.width, a.height "
            "FROM dup_set_member m JOIN asset a ON a.uuid = m.uuid ORDER BY m.set_id"
        ).fetchall()
        reasons = {p.set_id: p.reason for p in plans if p.status == "review"}
        for r in rows:
            if r["set_id"] in reasons:
                w.writerow([r["set_id"], reasons[r["set_id"]], r["uuid"], r["original_filename"], r["photos_date"],
                            (r["date_added"] or "")[:10], r["file_size"], f"{r['width']}x{r['height']}"])
    kept = sum(len(p.kept_also) for p in planned)
    click.echo(
        f"{len(planned)} sets planned: {sum(len(p.losers) for p in planned)} losers in {out}, "
        f"{sum(p.date is not None for p in planned)} keeper dates in {dates_out}, "
        f"{sum(p.review_original is not None for p in planned)} edit pairs for side-by-side review, "
        f"{kept} copies kept because they were edited in Photos"
    )
    click.echo(f"{len(plans) - len(planned)} sets left for review in {review_out}")


@main.command("archive-pairs")
@click.argument("pairs_csv", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--catalog", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--root", type=click.Path(path_type=Path), default=DEFAULT_ARCHIVE, show_default=True)
def archive_pairs(pairs_csv: Path, catalog: Path, root: Path) -> None:
    """Copy each pairs-CSV loser's original to the archive and verify it; also
    put edit pairs side by side under edits-review/. Read-only against Photos; resumable."""
    conn = open_catalog(catalog)
    rows = read_pairs_rows(pairs_csv)
    paths = None
    wanted = {r["loser"] for r in rows}
    extras = conn.execute(
        f"SELECT COUNT(*) FROM asset WHERE (has_adjustments = 1 OR is_live = 1) AND uuid IN ({','.join('?' * len(wanted))})",
        list(wanted),
    ).fetchone()[0] if wanted else 0
    run_row = get_run(conn)
    try:
        if extras and run_row is not None:
            paths = {p.uuid: p for p in iter_paths(Path(run_row["library_path"])) if p.uuid in wanted}
        result = run_pair_archive(
            conn, rows, root, manifest_name=f"manifest-{catalog.stem}.csv", paths=paths,
            progress=lambda done, total: click.echo(f"  {done}/{total}", err=True),
        )
    except (ArchiveSpace, ArchiveDestination, LibraryUnavailable) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"{result.copied} files copied, {result.skipped} losers already archived, "
        f"{result.mismatched} mismatched (will retry), {len(result.missing)} missing sources, "
        f"{len(result.unknown)} not in the catalog; {result.review_copies} side-by-side copies in {root}/edits-review"
    )
    for u in result.missing[:20] + result.unknown[:20]:
        click.echo(f"  not archived: {u}")
    stage = get_stage(conn, "archive")
    click.echo(f"archive stage: {stage['status'] if stage is not None else 'unknown'}")
