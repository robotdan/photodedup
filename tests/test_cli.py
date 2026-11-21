import pytest
from click.testing import CliRunner

from photodedup.catalog import get_stage, open_catalog, set_stage
from photodedup.cli import main


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "catalog.sqlite"
    conn = open_catalog(path)
    conn.close()
    return path


def run(*args):
    return CliRunner().invoke(main, list(args))


def test_help_lists_phase3_commands():
    out = run("--help").output
    for cmd in ("sets", "arbitrate", "review"):
        assert cmd in out


def test_arbitrate_refuses_before_group(catalog):
    result = run("arbitrate", "--catalog", str(catalog))
    assert result.exit_code != 0
    assert "group stage" in result.output and "--force" in result.output


def test_arbitrate_force_runs_on_an_empty_catalog(catalog):
    result = run("arbitrate", "--catalog", str(catalog), "--force")
    assert result.exit_code == 0, result.output
    assert "0 sets" in result.output


def test_review_refuses_before_arbitrate(catalog, tmp_path):
    result = run("review", "--catalog", str(catalog), "--out", str(tmp_path / "r"))
    assert result.exit_code != 0
    assert "arbitrate stage" in result.output


def test_review_force_writes_an_index(catalog, tmp_path):
    out = tmp_path / "r"
    result = run("review", "--catalog", str(catalog), "--out", str(out), "--force")
    assert result.exit_code == 0, result.output
    assert (out / "index.html").exists()
    assert "wrote" in result.output


def test_sets_refuses_before_group(catalog):
    result = run("sets", "--catalog", str(catalog))
    assert result.exit_code != 0
    assert "group stage" in result.output and "--force" in result.output


def test_sets_runs_on_grouped_catalog(catalog):
    conn = open_catalog(catalog)
    set_stage(conn, "group", "complete")
    conn.close()
    result = run("sets", "--catalog", str(catalog))
    assert result.exit_code == 0, result.output
    assert "0 exact sets" in result.output
    conn = open_catalog(catalog)
    assert get_stage(conn, "sets")["status"] == "complete"


def test_archive_refuses_before_arbitrate(catalog, tmp_path):
    result = run("archive", "--catalog", str(catalog), "--root", str(tmp_path / "arc"))
    assert result.exit_code != 0
    assert "arbitrate stage" in result.output


def test_apply_with_no_flags_prints_the_plan(catalog):
    conn = open_catalog(catalog); set_stage(conn, "arbitrate", "complete"); set_stage(conn, "archive", "complete"); conn.close()
    result = run("apply", "--catalog", str(catalog))
    assert result.exit_code == 0, result.output
    assert "--merge" in result.output and "Nothing is deleted" in result.output


def test_apply_refuses_without_archive(catalog):
    conn = open_catalog(catalog); set_stage(conn, "arbitrate", "complete"); conn.close()
    result = run("apply", "--catalog", str(catalog), "--stage")
    assert result.exit_code != 0
    assert "archive stage" in result.output


def test_apply_real_write_needs_a_gui_session(catalog, monkeypatch):
    """PhotoscriptWriter raises PhotosUnavailable outside a GUI session; simulate
    that refusal instead of depending on whether Photos happens to be scriptable
    from whatever shell runs the suite (in a real GUI session PhotoscriptWriter()
    would succeed and launch Photos)."""
    from photodedup.photos_write import PhotosUnavailable

    conn = open_catalog(catalog); set_stage(conn, "arbitrate", "complete"); set_stage(conn, "archive", "complete"); conn.close()

    def boom(*_):
        raise PhotosUnavailable(
            "Photos cannot be scripted from this process (photoscript failed to load). "
            "Run this command from an interactive terminal session on the Mac that owns the library."
        )

    monkeypatch.setattr("photodedup.cli.PhotoscriptWriter", boom)
    result = run("apply", "--catalog", str(catalog), "--stage")
    assert result.exit_code != 0
    assert "Photos cannot be scripted" in result.output and "interactive" in result.output


def test_apply_dry_run_writes_no_csv(catalog, monkeypatch, tmp_path):
    conn = open_catalog(catalog); set_stage(conn, "arbitrate", "complete"); set_stage(conn, "archive", "complete"); conn.close()
    monkeypatch.chdir(tmp_path)
    result = run("apply", "--catalog", str(catalog), "--stage", "--dry-run")
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "losers.csv").exists()


def test_apply_self_test_refuses_dry_run(catalog):
    result = run("apply", "--catalog", str(catalog), "--self-test", "abc", "--dry-run")
    assert result.exit_code != 0
    assert "--dry-run" in result.output


def test_archive_destination_error_is_a_clean_message(catalog, tmp_path, monkeypatch):
    """The drive unmounting mid-run must read as one sentence, not a traceback."""
    from photodedup.archive import ArchiveDestination

    def boom(*args, **kwargs):
        raise ArchiveDestination(
            "destination error at /Volumes/Extreme Pro/photodedup/x.jpg: [Errno 5] "
            "Input/output error; the drive may have unmounted or filled; everything "
            "verified so far is kept."
        )

    monkeypatch.setattr("photodedup.cli.run_archive", boom)
    result = run("archive", "--catalog", str(catalog), "--root", str(tmp_path / "arc"))
    assert result.exit_code != 0
    assert "destination error at" in result.output and "unmounted" in result.output
    assert "Traceback" not in result.output


def test_verify_command_exists():
    assert "verify" in run("--help").output
