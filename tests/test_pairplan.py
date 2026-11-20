from datetime import datetime

from photodedup.pairplan import Member, is_edited, merge_overlapping, plan_set, stem

T = datetime(2014, 4, 12, 12, 0, 39)


def m(uuid, name, *, date=T, w=4000, h=3000, size=1000, added="2020-12-17", adj=False, exif=T, fdate=None):
    return Member(uuid, name, w, h, size, date, added, adj, exif, fdate)


def test_stem_and_edited():
    assert stem("IMG_1(1).jpg") == stem("IMG_1-edited.jpg") == "img_1"
    assert is_edited("IMG_1-edited.jpg") and not is_edited("IMG_1.jpg")


def test_original_is_kept_and_the_edit_is_its_pair():
    p = plan_set(1, [m("e", "IMG_1-edited.jpg", size=500), m("o", "IMG_1.jpg")])
    assert p.status == "planned" and p.keeper.uuid == "o"
    assert [x.uuid for x in p.losers] == ["e"] and p.edit_pair.uuid == "e"


def test_hour_shifted_keeper_takes_the_time_the_right_copy_shows():
    p = plan_set(1, [
        m("o", "IMG_1.jpg", date=datetime(2014, 4, 12, 10, 0, 39)),
        m("e", "IMG_1-edited.jpg"),
    ])
    assert p.keeper.uuid == "o" and p.date == T


def test_keeper_already_right_gets_no_date():
    assert plan_set(1, [m("a", "x.jpg"), m("b", "x(1).jpg", date=datetime(2014, 4, 12, 9, 0, 39))]).date is None


def test_photos_edited_copy_is_never_a_loser():
    p = plan_set(1, [m("a", "x.jpg", w=5000), m("b", "x.jpg", adj=True), m("c", "x.jpg")])
    assert [x.uuid for x in p.losers] == ["c"] and [x.uuid for x in p.kept_also] == ["b"]


def test_review_cases():
    assert plan_set(1, [m("a", "x.jpg"), m("b", "y.jpg")]).status == "review"
    odd = datetime(2014, 4, 12, 12, 17, 0)
    assert plan_set(1, [m("a", "x.jpg"), m("b", "x.jpg", date=odd)]).status == "review"
    # A time no copy shows (a UTC video date) is not adopted.
    utc = datetime(2014, 4, 12, 19, 0, 39)
    assert plan_set(1, [m("a", "x.mp4", exif=utc), m("b", "x.mp4", exif=utc)]).reason == \
        "no copy shows the EXIF or filename time"
    assert plan_set(1, [m("a", "x.jpg", adj=True), m("b", "x.jpg", adj=True)]).status == "review"


def test_sets_sharing_a_photo_are_merged():
    ms = {u: m(u, "x.jpg") for u in "abcde"}
    merged = merge_overlapping({1: ["a", "b"], 2: ["b", "c"], 3: ["d", "e"], 4: ["c"]}, ms)
    assert {k: [x.uuid for x in v] for k, v in merged.items()} == {1: ["a", "b", "c"], 3: ["d", "e"]}
