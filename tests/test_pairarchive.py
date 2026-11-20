import hashlib

from photodedup.catalog import get_stage
from photodedup.pairarchive import REVIEW_DIR, archived, run_pair_archive
from tests.test_archive import add_asset, conn  # noqa: F401  (fixture)

BIG = lambda root: 10**12  # noqa: E731


def test_losers_are_archived_with_their_own_manifest_and_review_copies(conn, tmp_path):  # noqa: F811
    add_asset(conn, "keep", tmp_path, name="IMG_1-edited.jpg")
    src = add_asset(conn, "lose", tmp_path, name="IMG_1.jpg")
    root = tmp_path / "drive"
    (root).mkdir()
    (root / "manifest.csv").write_text("the old catalog's manifest\n")
    rows = [{"keeper": "keep", "loser": "lose", "why": "test", "edit_pair": "1"}]
    r = run_pair_archive(conn, rows, root, manifest_name="manifest-new.csv", free_bytes=BIG)
    assert r.copied == 1 and not r.missing and r.review_copies == 2
    dest = root / "2014/05/lose_IMG_1.jpg"
    assert hashlib.sha256(dest.read_bytes()).digest() == hashlib.sha256(src.read_bytes()).digest()
    assert (root / "2014/05/lose_IMG_1.jpg.json").exists()
    assert (root / "manifest.csv").read_text() == "the old catalog's manifest\n"
    assert "lose" in (root / "manifest-new.csv").read_text()
    review = root / REVIEW_DIR / "2014-05-12_IMG_1"
    assert sorted(p.name for p in review.iterdir()) == ["edited_IMG_1-edited.jpg", "original_IMG_1.jpg"]
    assert archived(conn, ["lose", "keep"]) == {"lose"}
    assert get_stage(conn, "archive")["status"] == "complete"


def test_rerun_copies_nothing(conn, tmp_path):  # noqa: F811
    add_asset(conn, "keep", tmp_path)
    add_asset(conn, "lose", tmp_path)
    rows = [{"keeper": "keep", "loser": "lose", "why": "", "edit_pair": ""}]
    run_pair_archive(conn, rows, tmp_path / "d", manifest_name="m.csv", free_bytes=BIG)
    r = run_pair_archive(conn, rows, tmp_path / "d", manifest_name="m.csv", free_bytes=BIG)
    assert r.copied == 0 and r.skipped == 1


def test_unknown_or_missing_loser_leaves_the_stage_open(conn, tmp_path):  # noqa: F811
    add_asset(conn, "keep", tmp_path)
    add_asset(conn, "lose", tmp_path).unlink()
    rows = [{"keeper": "keep", "loser": "lose", "why": "", "edit_pair": ""},
            {"keeper": "keep", "loser": "ghost", "why": "", "edit_pair": ""}]
    r = run_pair_archive(conn, rows, tmp_path / "d", manifest_name="m.csv", free_bytes=BIG)
    assert r.missing == ["lose"] and r.unknown == ["ghost"]
    assert get_stage(conn, "archive")["status"] == "running"
