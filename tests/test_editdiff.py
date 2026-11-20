from PIL import Image

from photodedup.editdiff import measure


def jpg(path, color, size=(800, 600)):
    Image.new("RGB", size, color).save(path, quality=95)
    return path


def test_identical_copy_is_not_visible(tmp_path):
    d = measure(jpg(tmp_path / "a.jpg", (100, 120, 140)), jpg(tmp_path / "b.jpg", (100, 120, 140)))
    assert d.mad < 1 and not d.visible


def test_brightened_or_cropped_edit_is_visible(tmp_path):
    a = jpg(tmp_path / "a.jpg", (100, 120, 140))
    assert measure(a, jpg(tmp_path / "b.jpg", (125, 145, 165))).visible
    crop = measure(a, jpg(tmp_path / "c.jpg", (100, 120, 140), size=(600, 600)))
    assert crop.reshaped and crop.visible
