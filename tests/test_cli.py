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


def test_apply_real_write_needs_a_gui_session(catalog, tmp_path):
    """In this shell photoscript cannot load; the command must say so plainly."""
    conn = open_catalog(catalog); set_stage(conn, "arbitrate", "complete"); set_stage(conn, "archive", "complete"); conn.close()
    result = run("apply", "--catalog", str(catalog), "--stage")
    assert result.exit_code != 0
    assert "interactive" in result.output or "Photos cannot be scripted" in result.output
