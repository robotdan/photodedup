import json
import shutil
from html.parser import HTMLParser
from pathlib import Path

import pytest
from PIL import Image

from photodedup.arbitrate import run_arbitrate
from photodedup.catalog import StageIncomplete, open_catalog, set_stage
from photodedup.review import write_review


class Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.imgs: list[str] = []
        self.classes: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "img":
            self.imgs.append(a.get("src", ""))
        if a.get("class"):
            self.classes.extend(a["class"].split())

    def handle_data(self, data):
        self.text.append(data)


def parse(path: Path) -> Collector:
    c = Collector()
    c.feed(path.read_text())
    return c


@pytest.fixture
def conn(tmp_path):
    conn = open_catalog(tmp_path / "catalog.sqlite")
    set_stage(conn, "group", "complete")
    return conn


def thumb(tmp_path, name):
    p = tmp_path / "thumbs_src" / name
    p.parent.mkdir(exist_ok=True)
    Image.new("RGB", (8, 8), "blue").save(p)
    return str(p)


def add_asset(conn, uuid, *, size=1000, photos_date="2014-05-12T17:00:00",
              thumb_path=None, phash="0000000000000000", exif=None,
              added="2020-01-01T00:00:00", hash_status="ok"):
    conn.execute(
        """INSERT INTO asset (uuid, kind, uti, file_size, date_added, photos_date,
                              albums, keywords, survey_status, original_filename, thumb_path)
           VALUES (?, 'image', 'public.jpeg', ?, ?, ?, '[]', '[]', 'ok', ?, ?)""",
        (uuid, size, added, photos_date, f"{uuid}.jpg", thumb_path),
    )
    conn.execute(
        "INSERT INTO asset_hash (uuid, status, phash) VALUES (?, ?, ?)", (uuid, hash_status, phash)
    )
    conn.execute(
        "INSERT INTO date_candidate (uuid, source, value) VALUES (?, 'photos_db', ?)",
        (uuid, photos_date),
    )
    if exif:
        conn.execute(
            "INSERT INTO date_candidate (uuid, source, value) VALUES (?, 'exif', ?)", (uuid, exif)
        )
    conn.commit()


def add_set(conn, set_id, uuids, *, tier="exact"):
    conn.execute(
        "INSERT INTO dup_set (id, tier, kind, size) VALUES (?, ?, 'image', ?)",
        (set_id, tier, len(uuids)),
    )
    conn.executemany(
        "INSERT INTO dup_set_member (set_id, uuid, tier, signals) VALUES (?, ?, ?, 'scan')",
        [(set_id, u, tier) for u in uuids],
    )
    conn.commit()


@pytest.fixture
def populated(conn, tmp_path):
    # Set 1: exact pair, date rescued from the loser's EXIF.
    add_asset(conn, "a1", size=20, thumb_path=thumb(tmp_path, "a1.jpg"),
              photos_date="2020-12-18T09:10:00", added="2020-12-18T18:00:00")
    add_asset(conn, "a2", size=10, thumb_path=thumb(tmp_path, "a2.jpg"),
              exif="2014-05-12T17:00:00")
    add_set(conn, 1, ["a1", "a2"])
    # Set 2: size-spread triple, one thumbnail missing.
    add_asset(conn, "b1", size=3_000_000, thumb_path=thumb(tmp_path, "b1.jpg"))
    add_asset(conn, "b2", size=1_000_000, thumb_path=None)
    add_asset(conn, "b3", size=1_000_000, thumb_path=thumb(tmp_path, "b3.jpg"))
    add_set(conn, 2, ["b1", "b2", "b3"])
    # Set 3: candidate pair with a phash distance of 1.
    add_asset(conn, "c1", thumb_path=thumb(tmp_path, "c1.jpg"), phash="0000000000000000")
    add_asset(conn, "c2", thumb_path=thumb(tmp_path, "c2.jpg"), phash="0000000000000001")
    add_set(conn, 3, ["c1", "c2"], tier="candidate")
    run_arbitrate(conn)
    return conn


def test_refuses_before_arbitrate(conn, tmp_path):
    with pytest.raises(StageIncomplete):
        write_review(conn, tmp_path / "out")


def test_writes_index_pages_and_thumbs(populated, tmp_path):
    out = tmp_path / "out"
    result = write_review(populated, out, per_page=1)
    assert (out / "index.html").exists()
    assert (out / "review.css").exists()
    assert sorted(p.name for p in (out / "exact").iterdir()) == ["001.html", "002.html"]
    assert (out / "candidate" / "001.html").exists()
    assert result.sets == 3 and result.pages >= 3
    assert result.thumbs == 6 and result.placeholders == 1


def test_every_img_resolves_to_a_file(populated, tmp_path):
    out = tmp_path / "out"
    write_review(populated, out)
    for page in out.rglob("*.html"):
        for src in parse(page).imgs:
            assert (page.parent / src).exists(), (page, src)


def test_keeper_is_marked_and_reason_shown(populated, tmp_path):
    out = tmp_path / "out"
    write_review(populated, out)
    page = parse(out / "exact" / "001.html")
    assert "keeper" in page.classes
    text = " ".join(page.text)
    assert "file_size" in text
    assert "a1.jpg" in text and "a2.jpg" in text


def test_missing_thumb_renders_a_placeholder_not_a_broken_img(populated, tmp_path):
    out = tmp_path / "out"
    write_review(populated, out)
    page = parse(out / "exact" / "001.html")
    assert "placeholder" in page.classes
    assert all(src for src in page.imgs)


def test_index_totals_match_the_decision_table(populated, tmp_path):
    out = tmp_path / "out"
    write_review(populated, out)
    text = " ".join(parse(out / "index.html").text)
    d = populated.execute(
        "SELECT SUM(resolvable) AS r, SUM(date_changes) AS c, COUNT(*) AS n FROM decision"
    ).fetchone()
    assert f"{d['n']} sets" in text
    assert f"{d['r']} resolvable" in text
    assert f"{d['c']} dates rescued" in text
    assert "0, as required" in text


def test_sample_pages_exist_and_carry_their_sets(populated, tmp_path):
    out = tmp_path / "out"
    write_review(populated, out)
    dates = " ".join(parse(out / "samples" / "dates.html").text)
    assert "a1.jpg" in dates and "b1.jpg" not in dates
    spread = " ".join(parse(out / "samples" / "size_spread.html").text)
    assert "b1.jpg" in spread
    cand = " ".join(parse(out / "samples" / "candidate.html").text)
    assert "c1.jpg" in cand and "phash distance 1" in cand


def test_flags_and_carries_appear(populated, tmp_path):
    out = tmp_path / "out"
    write_review(populated, out)
    text = " ".join(parse(out / "exact" / "001.html").text)
    assert "2014-05-12T17:00:00" in text
    assert "exif" in text


def test_rerun_removes_stale_pages_and_thumbs(populated, tmp_path):
    out = tmp_path / "out"
    write_review(populated, out, per_page=1)
    assert (out / "exact" / "002.html").exists()
    stale = out / "thumbs" / "zzz-stale.jpg"
    stale.write_bytes(b"x")
    write_review(populated, out, per_page=250)
    assert not (out / "exact" / "002.html").exists()
    assert not stale.exists()
    assert (out / "exact" / "001.html").exists()


def test_rerun_does_not_touch_thumbnail_sources(populated, tmp_path):
    out = tmp_path / "out"
    write_review(populated, out)
    src = Path(populated.execute("SELECT thumb_path FROM asset WHERE uuid='a1'").fetchone()["thumb_path"])
    assert src.exists()
    write_review(populated, out)
    assert src.exists() and src.stat().st_size > 0


def test_refuses_when_sets_have_no_decision(populated, tmp_path):
    populated.execute("DELETE FROM decision")
    populated.commit()
    with pytest.raises(StageIncomplete) as exc:
        write_review(populated, tmp_path / "out")
    assert "arbitrate" in str(exc.value)


def test_force_reports_only_decided_sets(populated, tmp_path):
    populated.execute("DELETE FROM decision WHERE set_id = 3")
    populated.commit()
    result = write_review(populated, tmp_path / "out", force=True)
    assert result.sets == 2


def test_date_class_uses_the_margin_not_string_equality(conn, tmp_path):
    """photos_date is compared as parsed instants against the margin, not as
    strings: a timezone rendering of the arbitrated date must not paint
    date-conflict just because the text differs."""
    add_asset(conn, "k1", size=20, thumb_path=thumb(tmp_path, "k1.jpg"),
              photos_date="2014-05-12T17:00:00")
    add_asset(conn, "l1", size=10, thumb_path=thumb(tmp_path, "l1.jpg"),
              photos_date="2014-05-12T20:00:00")
    add_set(conn, 1, ["k1", "l1"])

    add_asset(conn, "k2", size=20, thumb_path=thumb(tmp_path, "k2.jpg"),
              photos_date="2014-06-01T00:00:00")
    add_asset(conn, "l2", size=10, thumb_path=thumb(tmp_path, "l2.jpg"),
              photos_date="2014-06-04T00:00:00")
    add_set(conn, 2, ["k2", "l2"])

    run_arbitrate(conn)
    out = tmp_path / "out"
    write_review(conn, out)
    html = (out / "exact" / "001.html").read_text()
    # l1's photos_date is 3 hours from the arbitrated date (k1's): agreement.
    assert "<dd class='date-agree'>2014-05-12T20:00:00</dd>" in html
    # l2's photos_date is 3 days from the arbitrated date (k2's): a conflict.
    assert "<dd class='date-conflict'>2014-06-04T00:00:00</dd>" in html


def test_refuses_to_clear_a_non_empty_out_dir_with_no_previous_report(populated, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "someone_elses_file.txt").write_text("do not touch")
    with pytest.raises(ValueError):
        write_review(populated, out)
    # Nothing was removed.
    assert (out / "someone_elses_file.txt").exists()


def test_rerun_into_a_previous_reports_directory_succeeds(populated, tmp_path):
    out = tmp_path / "out"
    write_review(populated, out)
    result = write_review(populated, out)
    assert result.sets == 3
    assert (out / "index.html").exists()


def test_symlink_at_thumbs_is_replaced_and_its_target_is_untouched(populated, tmp_path):
    out = tmp_path / "out"
    write_review(populated, out)

    target_dir = tmp_path / "elsewhere"
    target_dir.mkdir()
    (target_dir / "keep.txt").write_text("do not touch")

    shutil.rmtree(out / "thumbs")
    (out / "thumbs").symlink_to(target_dir, target_is_directory=True)
    assert (out / "thumbs").is_symlink()

    write_review(populated, out)

    assert not (out / "thumbs").is_symlink()
    assert (out / "thumbs").is_dir()
    assert (target_dir / "keep.txt").read_text() == "do not touch"
