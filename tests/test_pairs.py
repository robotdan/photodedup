from datetime import datetime

import pytest

from photodedup.apply import FOLDER
from photodedup.pairs import PAIRS_ALBUM, Pair, apply_choices, read_pairs, resolve_pairs, swap_dates
from tests.fake_photos import FakePhotos


def library():
    fake = FakePhotos({
        "old": {"keywords": ["wedding"], "favorite": True, "title": "Ceremony",
                "date": datetime(2021, 6, 19, 7, 58, 58)},
        "new": {"date": datetime(2021, 6, 19, 8, 58, 58)},
    })
    fake.add_to_album(fake.ensure_album("Others"), ["old"])
    return fake


def test_keeper_takes_everything_and_loser_is_staged():
    fake = library()
    [r] = resolve_pairs(fake, [Pair("new", "old")])
    assert r.status == "staged"
    new = fake.get("new")
    assert "Others" in new.albums and new.keywords == ("wedding",) and new.favorite and new.title == "Ceremony"
    assert fake.album_members(PAIRS_ALBUM, FOLDER) == ["old"]
    assert fake.get("new").date == datetime(2021, 6, 19, 8, 58, 58)


def test_loser_is_not_staged_when_the_keeper_does_not_take_its_album():
    fake = FakePhotos({"old": {}, "new": {}}, fail_on={("add_to_album", "new")})
    fake.add_to_album(fake.ensure_album("Others"), ["old"])
    [r] = resolve_pairs(fake, [Pair("new", "old")])
    assert r.status == "failed"
    assert fake.album_members(PAIRS_ALBUM, FOLDER) == []


def test_dry_run_writes_nothing():
    fake = library()
    [r] = resolve_pairs(fake, [Pair("new", "old")], dry_run=True)
    assert r.status == "skipped" and "Others" in r.detail
    assert fake.get("new").albums == () and fake.album_members(PAIRS_ALBUM, FOLDER) == []


def test_missing_photo_is_skipped():
    [r] = resolve_pairs(library(), [Pair("new", "gone")])
    assert r.status == "skipped"


def test_rerun_after_staging_is_harmless():
    fake = library()
    resolve_pairs(fake, [Pair("new", "old")])
    [r] = resolve_pairs(fake, [Pair("new", "old")])
    assert r.status == "staged" and fake.album_members(PAIRS_ALBUM, FOLDER) == ["old"]


def test_one_keeper_absorbs_several_losers():
    fake2 = FakePhotos({"old": {}, "new": {}, "older": {}})
    fake2.add_to_album(fake2.ensure_album("Party"), ["old"])
    fake2.add_to_album(fake2.ensure_album("Trip"), ["older"])
    results = resolve_pairs(fake2, [Pair("new", "old"), Pair("new", "older")])
    assert [r.status for r in results] == ["staged", "staged"]
    assert {"Party", "Trip"} <= set(fake2.get("new").albums)
    assert sorted(fake2.album_members(PAIRS_ALBUM, FOLDER)) == ["old", "older"]


def test_read_pairs_allows_a_repeated_keeper(tmp_path):
    f = tmp_path / "p.csv"
    f.write_text("keeper,loser\na,b\na,c\n")
    assert read_pairs(f) == [Pair("a", "b"), Pair("a", "c")]


@pytest.mark.parametrize("body", [
    "keeper,other\na,b\n",
    "keeper,loser\na,a\n",
    "keeper,loser\na,b\nb,c\n",
    "keeper,loser\na,b\nc,b\n",
])
def test_read_pairs_refuses_bad_input(tmp_path, body):
    f = tmp_path / "p.csv"
    f.write_text(body)
    with pytest.raises(ValueError):
        read_pairs(f)


def test_choices_swap_and_keep_both():
    pairs = [Pair("o", "e"), Pair("o", "o1"), Pair("x", "xe")]
    out, both = apply_choices(pairs, {"e": "swap", "xe": "both"})
    assert out == [Pair("e", "o"), Pair("e", "o1")] and both == [Pair("x", "xe")]


def test_choices_refuse_two_swaps_in_one_set():
    with pytest.raises(ValueError):
        apply_choices([Pair("o", "e"), Pair("o", "e2")], {"e": "swap", "e2": "swap"})


def test_swapped_keeper_takes_the_old_keepers_final_time():
    pairs = [Pair("o", "e"), Pair("p", "f"), Pair("q", "g")]
    shown = {"o": "2014-01-01T08:00:00", "e": "2014-01-01T10:00:00",
             "p": "2014-01-01T10:00:00", "f": "2014-01-01T10:00:00", "q": "2014-01-01T09:00:00"}
    dates = {"o": "2014-01-01T10:00:00", "q": "2014-01-01T10:00:00"}
    out = swap_dates(pairs, {"e": "swap", "f": "swap", "g": "both"}, dates, shown)
    assert out == {"q": "2014-01-01T10:00:00"}
    shown["e"] = "2014-01-01T07:00:00"
    assert swap_dates(pairs, {"e": "swap"}, dates, shown)["e"] == "2014-01-01T10:00:00"
