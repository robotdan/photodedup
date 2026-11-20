"""Measure how a Google `-edited` copy differs from its original.

Both images are decoded small (JPEG draft mode), turned upright, and compared
at a common size. The numbers are on a 0-255 scale:

- mad: mean absolute pixel difference; recompression alone stays near 0-3.
- shift: the largest per-channel change in mean brightness.
- contrast: the edit's pixel spread over the original's (1.0 is unchanged).
- reshaped: the picture's dimensions changed beyond a rotation (a crop).

`visible` is the cut used to decide which pairs a person should look at.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

SIZE = (512, 384)
MAD_VISIBLE = 10.0
SHIFT_VISIBLE = 10.0
CONTRAST_VISIBLE = 0.08


@dataclass(frozen=True)
class EditDiff:
    mad: float
    shift: float
    contrast: float
    reshaped: bool
    rotated: bool
    gps_lost: bool

    @property
    def visible(self) -> bool:
        return (self.reshaped or self.rotated or self.mad >= MAD_VISIBLE or self.shift >= SHIFT_VISIBLE
                or abs(self.contrast - 1) >= CONTRAST_VISIBLE)

    @property
    def score(self) -> float:
        return 1000.0 if (self.reshaped or self.rotated) else self.mad + self.shift

    def row(self) -> dict:
        return {**asdict(self), "visible": self.visible, "score": round(self.score, 2)}


def _load(path: Path) -> tuple[Image.Image, bool, tuple[int, int]]:
    """Small upright RGB image, whether it carries GPS, and its full upright size."""
    im = Image.open(path)
    exif = im.getexif()
    w, h = im.size
    full = (h, w) if exif.get(0x0112) in (5, 6, 7, 8) else (w, h)
    im.draft("RGB", (SIZE[0] * 2, SIZE[1] * 2))
    return ImageOps.exif_transpose(im).convert("RGB"), bool(exif.get_ifd(0x8825)), full


def measure(original: Path, edited: Path) -> EditDiff:
    a, gps_a, dim_a = _load(original)
    b, gps_b, dim_b = _load(edited)
    landscape = lambda d: d[0] >= d[1]  # noqa: E731
    rotated = landscape(dim_a) != landscape(dim_b)
    ra, rb = dim_a[0] / dim_a[1], dim_b[0] / dim_b[1]
    reshaped = not rotated and (abs(ra - rb) > 0.01 or abs(dim_a[0] - dim_b[0]) > 0.02 * dim_a[0])
    size = SIZE if landscape(a.size) else SIZE[::-1]
    x = np.asarray(a.resize(size), dtype=float)
    y = np.asarray(b.resize(size), dtype=float)
    return EditDiff(
        mad=round(float(np.abs(x - y).mean()), 2),
        shift=round(float(np.abs(y.mean((0, 1)) - x.mean((0, 1))).max()), 2),
        contrast=round(float(y.std() / max(x.std(), 1e-6)), 3),
        reshaped=bool(reshaped),
        rotated=bool(rotated),
        gps_lost=gps_a and not gps_b,
    )
