import json

from photodedup.pairarchive import REVIEW_DIR, run_pair_archive
from photodedup.reviewpage import write_review_page
from tests.test_archive import add_asset, conn  # noqa: F401  (fixture)


def test_page_lists_each_edit_pair_with_paths_that_exist(conn, tmp_path):  # noqa: F811
    add_asset(conn, "keep", tmp_path, name="IMG_1-edited.jpg")
    add_asset(conn, "lose", tmp_path, name="IMG_1.jpg")
    add_asset(conn, "k2", tmp_path, name="IMG_2.jpg")
    add_asset(conn, "l2", tmp_path, name="IMG_2(1).jpg")
    rows = [{"keeper": "keep", "loser": "lose", "why": "", "edit_pair": "1"},
            {"keeper": "k2", "loser": "l2", "why": "", "edit_pair": ""}]
    root = tmp_path / "drive"
    run_pair_archive(conn, rows, root, manifest_name="m.csv", free_bytes=lambda r: 10**12)
    dest, total, missing = write_review_page(conn, rows, root)
    assert dest == root / REVIEW_DIR / "index.html" and total == 1 and missing == 0
    html = dest.read_text()
    data = json.loads(html.split("const DATA = ", 1)[1].split(";\n", 1)[0])
    [e] = data
    assert e["loser"] == "lose" and e["keeper"] == "keep" and e["kept"] == "edit"
    assert (root / REVIEW_DIR / e["orig"]).exists() and (root / REVIEW_DIR / e["edit"]).exists()


def test_diffs_keep_only_visible_pairs(conn, tmp_path):  # noqa: F811
    add_asset(conn, "orig", tmp_path, name="IMG_1.jpg")
    add_asset(conn, "edit", tmp_path, name="IMG_1-edited.jpg")
    rows = [{"keeper": "orig", "loser": "edit", "why": "", "edit_pair": "1"}]
    root = tmp_path / "drive"
    run_pair_archive(conn, rows, root, manifest_name="m.csv", free_bytes=lambda r: 10**12)
    flat = {"edit": {"visible": "False", "score": "0", "mad": "0", "shift": "0", "contrast": "1"}}
    assert write_review_page(conn, rows, root, flat)[1] == 0
    flat["edit"].update(visible="True", score="12", mad="9", shift="12")
    _, total, missing = write_review_page(conn, rows, root, flat)
    data = json.loads((root / REVIEW_DIR / "index.html").read_text().split("const DATA = ", 1)[1].split(";\n", 1)[0])
    assert total == 1 and missing == 0 and data[0]["kept"] == "orig" and "brightness shift 12.0" in data[0]["diff"]
