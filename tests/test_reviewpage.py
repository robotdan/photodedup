import json

from photodedup.pairarchive import REVIEW_DIR, run_pair_archive
from photodedup.reviewpage import write_review_page
from tests.test_archive import add_asset, conn  # noqa: F401  (fixture)


def test_page_lists_each_edit_pair_with_paths_that_exist(conn, tmp_path):  # noqa: F811
    add_asset(conn, "keep", tmp_path, name="IMG_1-edited.jpg")
    add_asset(conn, "lose", tmp_path, name="IMG_1.jpg")
    add_asset(conn, "k2", tmp_path, name="IMG_2.jpg")
    add_asset(conn, "l2", tmp_path, name="IMG_2(1).jpg")
    rows = [{"keeper": "keep", "loser": "lose", "why": "", "review_original": "1"},
            {"keeper": "k2", "loser": "l2", "why": "", "review_original": ""}]
    root = tmp_path / "drive"
    run_pair_archive(conn, rows, root, manifest_name="m.csv", free_bytes=lambda r: 10**12)
    dest, total, missing = write_review_page(conn, rows, root)
    assert dest == root / REVIEW_DIR / "index.html" and total == 1 and missing == 0
    html = dest.read_text()
    data = json.loads(html.split("const DATA = ", 1)[1].split(";\n", 1)[0])
    [e] = data
    assert e["loser"] == "lose" and e["keeper"] == "keep"
    assert (root / REVIEW_DIR / e["orig"]).exists() and (root / REVIEW_DIR / e["edit"]).exists()
