# Photo De-duplication, Phase 2: Hashing and Grouping — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Identify the duplicate sets in the library by content, and report how many there are and how confident each is, without deciding anything or touching a single photo.

**Architecture:** Three content signals written into the existing SQLite catalog by a resumable, parallel `hash` stage, then a `group` stage that forms duplicate sets from them under hard partitions that may never be crossed. Both are pure reads of files plus writes to the catalog. Nothing is deleted, nothing is arbitrated, and the Photos library stays read-only.

**Tech Stack:** Python 3.13, uv, Click, SQLite, Pillow with pillow-heif, imagehash, ffmpeg, pytest.

**Spec:** `docs/superpowers/specs/2026-09-18-photo-dedup-design.md` — read the "Survey findings (2026-09-19)" section, which amends several of the assumptions the earlier text was written on.

## Global Constraints

- Repository root `/Users/robotdan/dev/tools`; package at `src/photodedup/`.
- Python 3.13, dependencies managed by `uv`, pinned in `pyproject.toml`.
- Phase 2 remains strictly READ-ONLY with respect to the Photos library: it opens originals for reading and writes only to `./catalog.sqlite` and `./reports/`. No stage may write to the library, launch Photos, or call a mutating osxphotos function.
- Every stage reads only assets with `survey_status = 'ok'`. Rows with `survey_status = 'error'` carry a uuid and nothing else.
- 1,875 assets have `path IS NULL` and cannot be hashed. They are recorded as `no_original` and excluded from grouping, never treated as a failure.
- Never use a whole-file hash as a duplicate signal. Cohort B files had EXIF rewritten, so whole-file hashes differ across exactly the pairs that matter.
- Hard partitions that grouping may never cross, regardless of hash agreement: `kind` (image vs video), and `has_raw` (a RAW file never groups with a non-RAW).
- Stages are resumable: re-running processes only rows not already complete. The library has 65,974 assets and 292 GB of originals; a run that cannot resume is unusable.
- Existing modules are complete and must not be modified except where a task says so: `catalog.py`, `dates.py`, `exif.py`, `library.py`, `survey.py`, `report.py`, `cli.py`.

---

### Task 1: Hash storage schema

**Files:**
- Modify: `src/photodedup/catalog.py`
- Test: `tests/test_catalog.py`

**Interfaces:**
- Consumes: `open_catalog` from Phase 1.
- Produces: an `asset_hash` table, and `SCHEMA_VERSION` raised to 2.

The table is separate from `asset` rather than more columns on it, because hashing is a distinct resumable pass whose per-row status is independent of the survey's, and because a row's absence is the natural "not yet hashed" marker.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_catalog.py`:

```python
def test_asset_hash_table_exists(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "asset_hash" in tables


def test_asset_hash_cascades_on_asset_delete(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    conn.execute(
        "INSERT INTO asset_hash (uuid, status) VALUES ('u1', 'ok')"
    )
    conn.execute("DELETE FROM asset WHERE uuid = 'u1'")
    assert conn.execute("SELECT COUNT(*) AS n FROM asset_hash").fetchone()["n"] == 0


def test_asset_hash_status_is_constrained(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO asset_hash (uuid, status) VALUES ('u1', 'banana')")


def test_asset_hash_is_one_row_per_asset(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    conn.execute("INSERT INTO asset_hash (uuid, status) VALUES ('u1', 'ok')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO asset_hash (uuid, status) VALUES ('u1', 'ok')")


def test_schema_version_is_two(conn):
    assert SCHEMA_VERSION == 2
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_catalog.py -v
```

Expected: FAIL on `asset_hash` not existing.

- [ ] **Step 3: Write the implementation**

In `src/photodedup/catalog.py`, raise `SCHEMA_VERSION` to `2` and append this to the `_SCHEMA` string:

```sql
CREATE TABLE IF NOT EXISTS asset_hash (
    uuid         TEXT PRIMARY KEY REFERENCES asset (uuid) ON DELETE CASCADE,
    status       TEXT NOT NULL CHECK (status IN ('ok', 'error', 'no_original', 'skipped')),
    error        TEXT,
    scan_hash    TEXT,
    pixel_hash   TEXT,
    dhash        TEXT,
    phash        TEXT,
    video_hash   TEXT,
    pixel_width  INTEGER,
    pixel_height INTEGER,
    bytes_read   INTEGER,
    hashed_at    TEXT
);

CREATE INDEX IF NOT EXISTS asset_hash_status ON asset_hash (status);
CREATE INDEX IF NOT EXISTS asset_hash_scan ON asset_hash (scan_hash);
CREATE INDEX IF NOT EXISTS asset_hash_pixel ON asset_hash (pixel_hash);
CREATE INDEX IF NOT EXISTS asset_hash_video ON asset_hash (video_hash);
CREATE INDEX IF NOT EXISTS asset_hash_dhash ON asset_hash (dhash);

CREATE TABLE IF NOT EXISTS dup_group (
    id     INTEGER PRIMARY KEY,
    signal TEXT NOT NULL CHECK (signal IN ('scan', 'pixel', 'video', 'dhash')),
    tier   TEXT NOT NULL CHECK (tier IN ('exact', 'candidate')),
    digest TEXT NOT NULL,
    size   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dup_member (
    group_id INTEGER NOT NULL REFERENCES dup_group (id) ON DELETE CASCADE,
    uuid     TEXT NOT NULL REFERENCES asset (uuid) ON DELETE CASCADE,
    PRIMARY KEY (group_id, uuid)
);

CREATE INDEX IF NOT EXISTS dup_member_uuid ON dup_member (uuid);
CREATE INDEX IF NOT EXISTS dup_group_tier ON dup_group (tier, signal);
```

The grouping tables are created here, with the hash table, rather than in
Task 6 where they are first used. One schema version bump means one catalog
rebuild; two bumps would also invalidate this task's own
`test_schema_version_is_two` when Task 6 raised it again.

Because `SCHEMA_VERSION` changes, `open_catalog` must not silently attach to a version-1 catalog. Add this check after the schema is applied and the version row is ensured:

```python
    found = conn.execute("SELECT version FROM schema_meta").fetchone()["version"]
    if found != SCHEMA_VERSION:
        conn.close()
        raise SchemaVersionMismatch(
            f"catalog at {path} is schema version {found}, this code expects "
            f"{SCHEMA_VERSION}. Delete the catalog and re-run the survey; it is "
            "a derived artifact and takes about five minutes to rebuild."
        )
```

and define, above `open_catalog`:

```python
class SchemaVersionMismatch(RuntimeError):
    """The catalog on disk was written by a different schema version."""
```

- [ ] **Step 4: Add the mismatch test**

```python
def test_version_mismatch_is_refused(tmp_path):
    path = tmp_path / "catalog.sqlite"
    open_catalog(path).close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_meta SET version = 1")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaVersionMismatch) as excinfo:
        open_catalog(path)
    assert "re-run the survey" in str(excinfo.value)
```

Add `SchemaVersionMismatch` to the imports at the top of `tests/test_catalog.py`.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_catalog.py -v
```

- [ ] **Step 6: Rebuild the existing catalog**

The repository root already holds a `catalog.sqlite` written at schema version
1. The guard you just added will refuse to open it. Delete it and re-run the
survey, which takes about five minutes and is the reason the catalog is
gitignored and treated as derived rather than precious:

```bash
rm -f catalog.sqlite catalog.sqlite-wal catalog.sqlite-shm
uv run photodedup survey --missing-tolerance 0.05
```

The `--missing-tolerance` flag is needed because 1,875 assets (2.84%) have no
local original. They were all added in 2025 or 2026 and none belong to either
collapsed-date batch, so they do not bear on this work; they simply cannot be
hashed and will be recorded as `no_original`.

- [ ] **Step 7: Commit**

```bash
git add src/photodedup/catalog.py tests/test_catalog.py
git commit -m "feat: add asset_hash and dup tables, plus a schema version guard"
```

---

### Task 2: JPEG scan-data hashing

**Files:**
- Create: `src/photodedup/hashing.py`
- Test: `tests/test_hashing_jpeg.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `jpeg_scan_hash(data: bytes) -> str | None` — SHA-256 hex of the JPEG's structural and entropy-coded segments with every metadata segment removed. Returns `None` if the bytes are not a parseable JPEG.

**Why this is the primary signal.** A JPEG is a sequence of marker segments. Rewriting EXIF replaces the APP1 segment and leaves the compressed image data byte-identical. Hashing everything EXCEPT the metadata segments therefore matches two files that hold the same image with different metadata, which is exactly the cohort A / cohort B pair. It requires no decoding, runs at read speed, and cannot produce a false positive.

**Which segments to exclude:** `APP0` through `APP15` (`0xFFE0`-`0xFFEF`), which carry JFIF, EXIF, ICC and XMP, and `COM` (`0xFFFE`). Everything else is kept, including `SOI`, `DQT`, `DHT`, `SOF0`-`SOF15`, `DRI`, `SOS` and the entropy-coded data that follows it, and `EOI`. The quantization and Huffman tables must be kept because they determine what the entropy data decodes to.

**Parsing rules you need:** after `SOI` (`FFD8`), segments are `FF <marker> <2-byte big-endian length including the length bytes> <payload>`. Standalone markers `FFD0`-`FFD7` (RST) and `FF01` carry no length. After an `SOS` segment's header, entropy-coded data runs to the next marker that is not `FF00` (a stuffed byte) and not an RST marker; simplest correct approach is to scan forward to `FFD9` (`EOI`) and treat everything between as scan data. Fill bytes (`FF FF`) may appear between segments and must be skipped.

- [ ] **Step 1: Write the failing test**

`tests/test_hashing_jpeg.py`:

```python
import shutil
import subprocess

import pytest
from PIL import Image

from photodedup.hashing import jpeg_scan_hash

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)


def make_jpeg(path, colour="red", size=(64, 48)):
    Image.new("RGB", size, colour).save(path, quality=90)
    return path


def set_tags(path, *tags):
    subprocess.run(
        ["exiftool", "-overwrite_original", *tags, str(path)],
        check=True,
        capture_output=True,
    )


def test_same_pixels_different_exif_hash_identically(tmp_path):
    """The whole point: this is the cohort A / cohort B pair."""
    a = make_jpeg(tmp_path / "a.jpg")
    b = tmp_path / "b.jpg"
    shutil.copy2(a, b)
    set_tags(a, "-DateTimeOriginal=2003:02:23 09:08:08")
    set_tags(b, "-DateTimeOriginal=2020:12:18 09:12:48", "-Artist=Someone")

    assert a.read_bytes() != b.read_bytes()
    assert jpeg_scan_hash(a.read_bytes()) == jpeg_scan_hash(b.read_bytes())


def test_whole_file_hash_would_have_missed_that(tmp_path):
    """Guards the reason this function exists rather than hashlib.sha256(data)."""
    import hashlib

    a = make_jpeg(tmp_path / "a.jpg")
    b = tmp_path / "b.jpg"
    shutil.copy2(a, b)
    set_tags(b, "-Artist=Someone")
    assert hashlib.sha256(a.read_bytes()).hexdigest() != hashlib.sha256(
        b.read_bytes()
    ).hexdigest()
    assert jpeg_scan_hash(a.read_bytes()) == jpeg_scan_hash(b.read_bytes())


def test_different_images_hash_differently(tmp_path):
    a = make_jpeg(tmp_path / "a.jpg", colour="red")
    b = make_jpeg(tmp_path / "b.jpg", colour="blue")
    assert jpeg_scan_hash(a.read_bytes()) != jpeg_scan_hash(b.read_bytes())


def test_re_encoding_changes_the_hash(tmp_path):
    """Re-encoding is a perceptual-hash problem, not a scan-hash one."""
    a = make_jpeg(tmp_path / "a.jpg")
    b = tmp_path / "b.jpg"
    Image.open(a).save(b, quality=60)
    assert jpeg_scan_hash(a.read_bytes()) != jpeg_scan_hash(b.read_bytes())


def test_same_bytes_hash_identically(tmp_path):
    a = make_jpeg(tmp_path / "a.jpg")
    assert jpeg_scan_hash(a.read_bytes()) == jpeg_scan_hash(a.read_bytes())


def test_icc_profile_is_excluded(tmp_path):
    """ICC lives in APP2 and is metadata for this purpose."""
    a = make_jpeg(tmp_path / "a.jpg")
    b = tmp_path / "b.jpg"
    shutil.copy2(a, b)
    set_tags(b, "-ICC_Profile<=/System/Library/ColorSync/Profiles/sRGB Profile.icc")
    assert jpeg_scan_hash(a.read_bytes()) == jpeg_scan_hash(b.read_bytes())


@pytest.mark.parametrize(
    "data",
    [b"", b"not a jpeg at all", b"\xff\xd8", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"],
)
def test_non_jpeg_returns_none(data):
    assert jpeg_scan_hash(data) is None


def test_truncated_jpeg_returns_none(tmp_path):
    a = make_jpeg(tmp_path / "a.jpg")
    assert jpeg_scan_hash(a.read_bytes()[:40]) is None
```

Note on `test_icc_profile_is_excluded`: if that sRGB profile path does not exist on this machine, substitute any `.icc` file found under `/System/Library/ColorSync/Profiles/`, or skip the test with a clear reason. Do not delete it.

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_hashing_jpeg.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'photodedup.hashing'`.

- [ ] **Step 3: Write the implementation**

`src/photodedup/hashing.py`:

```python
"""Content hashing. Every function here is pure over bytes or a file path."""

from __future__ import annotations

import hashlib

# Metadata segments. APP0-APP15 carry JFIF, EXIF, ICC and XMP; COM is a comment.
# Excluding them is what makes two files holding the same image with different
# metadata hash identically.
_METADATA_MARKERS = frozenset(range(0xE0, 0xF0)) | {0xFE}

# Markers that stand alone, carrying no length field.
_STANDALONE = frozenset(range(0xD0, 0xD8)) | {0x01}

_SOI = 0xD8
_EOI = 0xD9
_SOS = 0xDA


def jpeg_scan_hash(data: bytes) -> str | None:
    """Hash a JPEG's structure and entropy data, ignoring every metadata segment.

    Returns None when *data* is not a JPEG this can parse, which the caller
    treats as "fall back to decoding" rather than as an error.
    """
    if len(data) < 4 or data[0] != 0xFF or data[1] != _SOI:
        return None

    digest = hashlib.sha256()
    digest.update(data[0:2])
    i = 2
    n = len(data)
    saw_scan = False

    while i < n:
        if data[i] != 0xFF:
            return None
        # Fill bytes: any number of 0xFF may pad the gap before a marker.
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            return None
        marker = data[i]
        i += 1

        if marker == _EOI:
            digest.update(b"\xff\xd9")
            return digest.hexdigest() if saw_scan else None

        if marker in _STANDALONE:
            digest.update(bytes((0xFF, marker)))
            continue

        if i + 2 > n:
            return None
        length = int.from_bytes(data[i : i + 2], "big")
        if length < 2 or i + length > n:
            return None
        segment = data[i : i + length]
        i += length

        if marker not in _METADATA_MARKERS:
            digest.update(bytes((0xFF, marker)))
            digest.update(segment)

        if marker == _SOS:
            # Entropy-coded data runs from here to EOI. Inside it every 0xFF is
            # followed by 0x00 (stuffing) or 0xD0-0xD7 (restart), and between a
            # progressive file's later SOS segments only table markers appear,
            # so the FIRST 0xFFD9 after this point is the real EOI. Searching
            # forward also ignores any bytes a writer appended after it.
            end = data.find(b"\xff\xd9", i)
            if end < 0:
                return None
            digest.update(data[i:end])
            digest.update(b"\xff\xd9")
            return digest.hexdigest()

    return None
```

Note that the `SOS` branch returns directly, so `saw_scan` only guards the pathological case of an `EOI` before any `SOS`. Keep it: a file of `FFD8 FFD9` must return `None` rather than a hash of two markers, which would collide across every such file.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_hashing_jpeg.py -v
```

- [ ] **Step 5: Validate against the real library, read-only**

This is the step that matters. Run the scan hash over a sample of real JPEGs and confirm it finds the duplicate pairs the survey predicted:

```bash
uv run python -c "
import sqlite3, collections
from pathlib import Path
from photodedup.hashing import jpeg_scan_hash
c = sqlite3.connect('catalog.sqlite'); c.row_factory = sqlite3.Row
# Target the re-imported cohort: same filename and dimensions, differing byte
# size. A plain LIMIT in rowid order lands outside it and measures nothing.
rows = c.execute('''
    SELECT a.uuid, a.path, a.original_filename FROM asset a
    JOIN (SELECT original_filename, width, height FROM asset
          WHERE survey_status='ok' AND path IS NOT NULL AND uti='public.jpeg'
          GROUP BY original_filename, width, height
          HAVING COUNT(*) > 1 AND MIN(file_size) <> MAX(file_size)) g
      ON g.original_filename = a.original_filename
     AND g.width = a.width AND g.height = a.height
    WHERE a.survey_status='ok' AND a.path IS NOT NULL AND a.uti='public.jpeg'
    LIMIT 6000
''').fetchall()
h = collections.Counter(); failed = 0
for r in rows:
    try:
        v = jpeg_scan_hash(Path(r['path']).read_bytes())
    except Exception:
        v = None
    if v is None: failed += 1
    else: h[v] += 1
print('sampled', len(rows), 'unparseable', failed)
print('distinct hashes', len(h))
print('groups >1:', sum(1 for v in h.values() if v > 1))
print('assets in those groups:', sum(v for v in h.values() if v > 1))
"
```

Expected: `unparseable` should be a small number (damaged files; the survey counted 79 across the whole library). Duplicate groups should be found. Record the real output in your report. If `unparseable` is more than about 5% of the sample, stop and report BLOCKED — the parser is wrong, not the files.

- [ ] **Step 6: Commit**

```bash
git add src/photodedup/hashing.py tests/test_hashing_jpeg.py
git commit -m "feat: hash JPEG scan data, ignoring metadata segments"
```

---

### Task 3: Decoded-pixel and perceptual hashing

**Files:**
- Modify: `src/photodedup/hashing.py`
- Modify: `pyproject.toml` (add `imagehash`)
- Test: `tests/test_hashing_pixel.py`

**Interfaces:**
- Consumes: `photodedup.hashing` from Task 2.
- Produces:
  - `pixel_hashes(path: Path) -> PixelHashes | None` — decodes the image once and returns all three pixel-derived values. Returns `None` if the file cannot be decoded.
  - `PixelHashes` frozen dataclass: `pixel_hash: str`, `dhash: str`, `phash: str`, `width: int`, `height: int`.

**Why one function rather than three:** decoding is the expensive part, and 6,678 HEIC files plus every JPEG the scan hash cannot parse must not be decoded three times.

**Orientation matters.** Two files holding the same pixels but different EXIF orientation flags should be treated as the same image, because the orientation flag is metadata. Apply `PIL.ImageOps.exif_transpose` before hashing so the comparison is on displayed pixels.

- [ ] **Step 1: Add the dependency**

```bash
uv add imagehash
```

- [ ] **Step 2: Write the failing test**

`tests/test_hashing_pixel.py`:

```python
import pytest
from PIL import Image

from photodedup.hashing import pixel_hashes


def make(path, colour="red", size=(64, 48), fmt=None):
    Image.new("RGB", size, colour).save(path, format=fmt)
    return path


def test_same_image_two_formats_share_a_pixel_hash(tmp_path):
    """PNG and BMP of identical pixels must agree; the container is not the image."""
    a = make(tmp_path / "a.png")
    b = make(tmp_path / "b.bmp")
    assert pixel_hashes(a).pixel_hash == pixel_hashes(b).pixel_hash


def test_different_images_differ(tmp_path):
    a = make(tmp_path / "a.png", colour="red")
    b = make(tmp_path / "b.png", colour="blue")
    assert pixel_hashes(a).pixel_hash != pixel_hashes(b).pixel_hash


def test_dimensions_are_reported(tmp_path):
    a = make(tmp_path / "a.png", size=(123, 45))
    result = pixel_hashes(a)
    assert (result.width, result.height) == (123, 45)


def test_resized_copy_differs_on_pixel_hash_but_matches_perceptually(tmp_path):
    source = Image.new("RGB", (256, 256))
    for x in range(256):
        for y in range(256):
            source.putpixel((x, y), (x, y, (x * y) % 256))
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    source.save(a)
    source.resize((128, 128)).save(b)

    ha, hb = pixel_hashes(a), pixel_hashes(b)
    assert ha.pixel_hash != hb.pixel_hash
    # Perceptual hashes are hex strings of equal length; compare bit distance.
    distance = bin(int(ha.dhash, 16) ^ int(hb.dhash, 16)).count("1")
    assert distance <= 12


def test_undecodable_file_returns_none(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    assert pixel_hashes(bad) is None


def test_missing_file_returns_none(tmp_path):
    assert pixel_hashes(tmp_path / "nope.png") is None


def test_exif_orientation_is_normalised(tmp_path):
    """An orientation flag is metadata; the displayed pixels are the image."""
    import subprocess
    import shutil

    if shutil.which("exiftool") is None:
        pytest.skip("exiftool not installed")

    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    Image.new("RGB", (64, 48), "red").save(a)
    Image.open(a).save(b)
    subprocess.run(
        ["exiftool", "-overwrite_original", "-Orientation=1", "-n", str(a)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["exiftool", "-overwrite_original", "-Orientation=1", "-n", str(b)],
        check=True,
        capture_output=True,
    )
    assert pixel_hashes(a).pixel_hash == pixel_hashes(b).pixel_hash
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
uv run pytest tests/test_hashing_pixel.py -v
```

- [ ] **Step 4: Write the implementation**

Append to `src/photodedup/hashing.py`:

```python
from dataclasses import dataclass
from pathlib import Path

import imagehash
from PIL import Image, ImageOps

try:  # HEIC support; the library holds 6,678 of them.
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - absence is reported by the hash stage
    pass


@dataclass(frozen=True)
class PixelHashes:
    pixel_hash: str
    dhash: str
    phash: str
    width: int
    height: int


def pixel_hashes(path: Path) -> PixelHashes | None:
    """Decode *path* once and derive every pixel-based signal from it.

    Returns None when the file cannot be decoded, which the hash stage records
    as an error rather than letting it abort a 65,974-asset run.
    """
    try:
        with Image.open(path) as image:
            # An orientation flag is metadata. Compare what is displayed.
            image = ImageOps.exif_transpose(image)
            rgb = image.convert("RGB")
            width, height = rgb.size
            digest = hashlib.sha256()
            digest.update(f"{width}x{height}|".encode())
            digest.update(rgb.tobytes())
            return PixelHashes(
                pixel_hash=digest.hexdigest(),
                dhash=str(imagehash.dhash(rgb, hash_size=8)),
                phash=str(imagehash.phash(rgb, hash_size=8)),
                width=width,
                height=height,
            )
    except Exception:
        return None
```

The dimensions are mixed into the pixel hash so that two images with identical byte streams but different shapes cannot collide.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_hashing_pixel.py -v
```

- [ ] **Step 6: Confirm HEIC really decodes**

```bash
uv run python -c "
import sqlite3
from pathlib import Path
from photodedup.hashing import pixel_hashes
c = sqlite3.connect('catalog.sqlite'); c.row_factory = sqlite3.Row
rows = c.execute('''SELECT path FROM asset WHERE survey_status='ok'
                    AND path IS NOT NULL AND uti='public.heic' LIMIT 5''').fetchall()
for r in rows:
    h = pixel_hashes(Path(r['path']))
    print(r['path'].rsplit('/', 1)[-1], '->', 'OK' if h else 'FAILED', h.width if h else '', h.height if h else '')
"
```

Expected: all OK with plausible dimensions. If HEIC fails, `pillow-heif` is not registering; stop and report that rather than working around it, because 6,678 assets depend on it.

- [ ] **Step 7: Commit**

```bash
git add src/photodedup/hashing.py tests/test_hashing_pixel.py pyproject.toml uv.lock
git commit -m "feat: add decoded-pixel and perceptual hashing"
```

---

### Task 4: Video stream hashing

**Files:**
- Modify: `src/photodedup/hashing.py`
- Test: `tests/test_hashing_video.py`

**Interfaces:**
- Consumes: `photodedup.hashing`.
- Produces: `video_stream_hash(path: Path, *, ffmpeg: str = "ffmpeg", timeout: int = 600) -> str | None`

**Why not decode frames.** The library holds 1,719 videos totalling 141.6 GB, which is half the bytes. Decoding them is hours of CPU. `ffmpeg -c copy -f md5` hashes the encoded stream without re-encoding, which runs at IO speed and, crucially, ignores container metadata atoms — the same property the JPEG scan hash relies on. Map only the video stream so an added or re-ordered audio track does not change the answer.

The command:

```
ffmpeg -nostdin -v error -i <path> -map 0:v -c copy -f md5 -
```

- [ ] **Step 1: Write the failing test**

`tests/test_hashing_video.py`:

```python
import shutil
import subprocess

import pytest

from photodedup.hashing import video_stream_hash

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def make_video(path, *, seconds=1, colour="red", metadata=None):
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c={colour}:s=64x48:d={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
    ]
    for key, value in (metadata or {}).items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def test_same_stream_different_container_metadata_hashes_identically(tmp_path):
    """The video equivalent of the JPEG EXIF-rewrite case."""
    a = make_video(tmp_path / "a.mp4")
    b = tmp_path / "b.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(a),
         "-c", "copy", "-metadata", "title=Something", str(b)],
        check=True, capture_output=True,
    )
    assert a.read_bytes() != b.read_bytes()
    assert video_stream_hash(a) == video_stream_hash(b)


def test_different_videos_differ(tmp_path):
    a = make_video(tmp_path / "a.mp4", colour="red")
    b = make_video(tmp_path / "b.mp4", colour="blue")
    assert video_stream_hash(a) != video_stream_hash(b)


def test_non_video_returns_none(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00" * 500)
    assert video_stream_hash(bad) is None


def test_missing_file_returns_none(tmp_path):
    assert video_stream_hash(tmp_path / "nope.mp4") is None


def test_bad_ffmpeg_binary_returns_none(tmp_path):
    a = make_video(tmp_path / "a.mp4")
    assert video_stream_hash(a, ffmpeg="/usr/bin/false") is None
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_hashing_video.py -v
```

- [ ] **Step 3: Write the implementation**

Append to `src/photodedup/hashing.py`:

```python
import subprocess


def video_stream_hash(
    path: Path, *, ffmpeg: str = "ffmpeg", timeout: int = 600
) -> str | None:
    """Hash a video's encoded picture stream, ignoring container metadata.

    `-c copy` avoids re-encoding, so this runs at IO speed rather than CPU
    speed, which matters across 141 GB of video. Mapping only `0:v` means an
    added or re-ordered audio track does not change the result.
    """
    try:
        completed = subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-i", str(path),
             "-map", "0:v", "-c", "copy", "-f", "md5", "-"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    if not output.startswith("MD5="):
        return None
    return output[len("MD5=") :]
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_hashing_video.py -v
```

- [ ] **Step 5: Time it against real video**

```bash
uv run python -c "
import sqlite3, time
from pathlib import Path
from photodedup.hashing import video_stream_hash
c = sqlite3.connect('catalog.sqlite'); c.row_factory = sqlite3.Row
rows = c.execute('''SELECT path, file_size FROM asset WHERE survey_status='ok'
                    AND path IS NOT NULL AND kind='video'
                    ORDER BY file_size DESC LIMIT 5''').fetchall()
for r in rows:
    t = time.time(); h = video_stream_hash(Path(r['path']))
    mb = (r['file_size'] or 0) / 1e6
    print(f\"{mb:8.1f} MB  {time.time()-t:6.2f}s  {'OK' if h else 'FAILED'}\")
"
```

Record the throughput in your report. It sets the expectation for the full run: 141.6 GB of video at the observed rate.

- [ ] **Step 6: Commit**

```bash
git add src/photodedup/hashing.py tests/test_hashing_video.py
git commit -m "feat: hash video streams without re-encoding"
```

---

### Task 5: The hash stage

**Files:**
- Create: `src/photodedup/hash_stage.py`
- Modify: `src/photodedup/cli.py`
- Test: `tests/test_hash_stage.py`

**Interfaces:**
- Consumes: `catalog`, `hashing`.
- Produces:
  - `HashResult` frozen dataclass: `total: int`, `hashed: int`, `already_done: int`, `no_original: int`, `errors: int`.
  - `run_hash(conn, *, workers: int | None = None, limit: int | None = None, progress: Callable[[int, int], None] | None = None) -> HashResult`
  - `hash_one(uuid: str, path: str | None, kind: str, uti: str | None) -> dict` — the unit of work, a module-level function so it is picklable for a process pool.
  - CLI: `photodedup hash [--catalog] [--workers] [--limit]`

**Behaviour:**

1. Select assets with `survey_status = 'ok'` that have no `asset_hash` row, or whose row has `status = 'error'` (so a re-run retries failures).
2. An asset with `path IS NULL` gets a row with `status = 'no_original'` and no hashes. It is not an error and is not retried.
3. Images: try `jpeg_scan_hash` on the raw bytes first. Whether or not it succeeds, also compute `pixel_hashes`. Both are useful and the decode dominates the cost only for non-JPEG.
4. Videos: `video_stream_hash` only.
5. Anything that yields no usable hash at all gets `status = 'error'` with the reason.
6. Parallel across a process pool; commit in batches so an interrupted run keeps its work.
7. Print progress every 500 assets.

- [ ] **Step 1: Write the failing test**

`tests/test_hash_stage.py`:

```python
from datetime import datetime

import pytest
from PIL import Image

from photodedup.catalog import open_catalog
from photodedup.hash_stage import run_hash

ADDED = datetime(2020, 12, 18, 9, 12, 48)


@pytest.fixture
def conn(tmp_path):
    return open_catalog(tmp_path / "catalog.sqlite")


def add_asset(conn, uuid, *, path=None, kind="image", uti="public.jpeg", status="ok"):
    conn.execute(
        """
        INSERT INTO asset (uuid, kind, uti, path, survey_status, date_added)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (uuid, kind, uti, str(path) if path else None, status, ADDED.isoformat()),
    )
    conn.commit()


def make_jpeg(path, colour="red"):
    Image.new("RGB", (32, 24), colour).save(path, quality=90)
    return path


def test_hashes_an_image(conn, tmp_path):
    add_asset(conn, "u1", path=make_jpeg(tmp_path / "a.jpg"))
    result = run_hash(conn, workers=1)
    assert result.hashed == 1
    row = conn.execute("SELECT * FROM asset_hash WHERE uuid='u1'").fetchone()
    assert row["status"] == "ok"
    assert row["scan_hash"] and row["pixel_hash"] and row["dhash"] and row["phash"]
    assert (row["pixel_width"], row["pixel_height"]) == (32, 24)


def test_asset_without_original_is_recorded_not_failed(conn):
    add_asset(conn, "u1", path=None)
    result = run_hash(conn, workers=1)
    assert result.no_original == 1
    assert result.errors == 0
    row = conn.execute("SELECT * FROM asset_hash WHERE uuid='u1'").fetchone()
    assert row["status"] == "no_original"


def test_survey_errors_are_not_hashed(conn, tmp_path):
    add_asset(conn, "u1", path=make_jpeg(tmp_path / "a.jpg"), status="error")
    result = run_hash(conn, workers=1)
    assert result.total == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM asset_hash").fetchone()["n"] == 0


def test_resume_skips_completed_rows(conn, tmp_path):
    add_asset(conn, "u1", path=make_jpeg(tmp_path / "a.jpg"))
    run_hash(conn, workers=1)
    result = run_hash(conn, workers=1)
    assert result.hashed == 0
    assert result.already_done == 1


def test_corrupt_file_is_an_error_and_does_not_abort(conn, tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"\x00" * 400)
    add_asset(conn, "u1", path=bad)
    add_asset(conn, "u2", path=make_jpeg(tmp_path / "good.jpg"))
    result = run_hash(conn, workers=1)
    assert result.errors == 1
    assert result.hashed == 1
    assert conn.execute(
        "SELECT status FROM asset_hash WHERE uuid='u1'"
    ).fetchone()["status"] == "error"


def test_errors_are_retried_on_a_later_run(conn, tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"\x00" * 400)
    add_asset(conn, "u1", path=bad)
    run_hash(conn, workers=1)
    make_jpeg(bad)  # the file is repaired
    result = run_hash(conn, workers=1)
    assert result.hashed == 1
    assert conn.execute(
        "SELECT status FROM asset_hash WHERE uuid='u1'"
    ).fetchone()["status"] == "ok"


def test_identical_images_with_different_exif_share_a_scan_hash(conn, tmp_path):
    import shutil
    import subprocess

    if shutil.which("exiftool") is None:
        pytest.skip("exiftool not installed")

    a = make_jpeg(tmp_path / "a.jpg")
    b = tmp_path / "b.jpg"
    shutil.copy2(a, b)
    subprocess.run(
        ["exiftool", "-overwrite_original", "-DateTimeOriginal=2020:12:18 09:12:48", str(b)],
        check=True, capture_output=True,
    )
    add_asset(conn, "u1", path=a)
    add_asset(conn, "u2", path=b)
    run_hash(conn, workers=1)
    hashes = [
        r["scan_hash"]
        for r in conn.execute("SELECT scan_hash FROM asset_hash ORDER BY uuid")
    ]
    assert hashes[0] == hashes[1]


def test_limit_caps_the_work(conn, tmp_path):
    for i in range(5):
        add_asset(conn, f"u{i}", path=make_jpeg(tmp_path / f"{i}.jpg"))
    result = run_hash(conn, workers=1, limit=2)
    assert result.hashed == 2
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_hash_stage.py -v
```

- [ ] **Step 3: Write the implementation**

`src/photodedup/hash_stage.py`:

```python
"""The hash stage: derive content signals for every asset, resumably."""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection

from photodedup.catalog import set_stage
from photodedup.hashing import jpeg_scan_hash, pixel_hashes, video_stream_hash

BATCH = 200


@dataclass(frozen=True)
class HashResult:
    total: int
    hashed: int
    already_done: int
    no_original: int
    errors: int


def hash_one(uuid: str, path: str | None, kind: str, uti: str | None) -> dict:
    """Compute every signal for one asset. Module-level so a pool can pickle it."""
    if path is None:
        return {"uuid": uuid, "status": "no_original"}

    target = Path(path)
    row: dict = {"uuid": uuid, "status": "ok"}

    if kind == "video":
        digest = video_stream_hash(target)
        if digest is None:
            return {"uuid": uuid, "status": "error", "error": "video stream unreadable"}
        row["video_hash"] = digest
        try:
            row["bytes_read"] = target.stat().st_size
        except OSError:
            pass
        return row

    try:
        data = target.read_bytes()
    except OSError as exc:
        return {"uuid": uuid, "status": "error", "error": f"unreadable: {exc}"}

    row["bytes_read"] = len(data)
    row["scan_hash"] = jpeg_scan_hash(data)

    pixels = pixel_hashes(target)
    if pixels is not None:
        row["pixel_hash"] = pixels.pixel_hash
        row["dhash"] = pixels.dhash
        row["phash"] = pixels.phash
        row["pixel_width"] = pixels.width
        row["pixel_height"] = pixels.height

    if row.get("scan_hash") is None and pixels is None:
        return {"uuid": uuid, "status": "error", "error": "no usable hash"}
    return row


_COLUMNS = (
    "status", "error", "scan_hash", "pixel_hash", "dhash", "phash",
    "video_hash", "pixel_width", "pixel_height", "bytes_read", "hashed_at",
)


def _write(conn: Connection, row: dict) -> None:
    row = dict(row)
    row["hashed_at"] = datetime.now().replace(microsecond=0).isoformat()
    values = [row["uuid"]] + [row.get(c) for c in _COLUMNS]
    placeholders = ", ".join("?" * (len(_COLUMNS) + 1))
    updates = ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS)
    conn.execute(
        f"""
        INSERT INTO asset_hash (uuid, {", ".join(_COLUMNS)})
        VALUES ({placeholders})
        ON CONFLICT (uuid) DO UPDATE SET {updates}
        """,
        values,
    )


def run_hash(
    conn: Connection,
    *,
    workers: int | None = None,
    limit: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> HashResult:
    """Hash every surveyed asset that is not already done. Resumable."""
    set_stage(conn, "hash", "running")

    already = conn.execute(
        "SELECT COUNT(*) AS n FROM asset_hash WHERE status IN ('ok', 'no_original')"
    ).fetchone()["n"]

    todo = conn.execute(
        """
        SELECT a.uuid, a.path, a.kind, a.uti
        FROM asset a
        LEFT JOIN asset_hash h ON h.uuid = a.uuid
        WHERE a.survey_status = 'ok'
          AND (h.uuid IS NULL OR h.status = 'error')
        ORDER BY a.uuid
        """
    ).fetchall()
    if limit is not None:
        todo = todo[:limit]

    hashed = no_original = errors = 0
    total = len(todo)
    if total == 0:
        set_stage(conn, "hash", "complete", notes="nothing to do")
        return HashResult(0, 0, already, 0, 0)

    workers = workers or max(1, (os.cpu_count() or 4) - 1)
    done = 0

    def record(row: dict) -> None:
        nonlocal hashed, no_original, errors
        _write(conn, row)
        if row["status"] == "ok":
            hashed += 1
        elif row["status"] == "no_original":
            no_original += 1
        else:
            errors += 1

    if workers == 1:
        for r in todo:
            record(hash_one(r["uuid"], r["path"], r["kind"], r["uti"]))
            done += 1
            if done % BATCH == 0:
                conn.commit()
                if progress:
                    progress(done, total)
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(hash_one, r["uuid"], r["path"], r["kind"], r["uti"])
                for r in todo
            ]
            for future in futures:
                record(future.result())
                done += 1
                if done % BATCH == 0:
                    conn.commit()
                    if progress:
                        progress(done, total)

    conn.commit()
    set_stage(
        conn, "hash", "complete",
        notes=f"{hashed} hashed, {no_original} without originals, {errors} errors",
    )
    return HashResult(total, hashed, already, no_original, errors)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_hash_stage.py -v
```

- [ ] **Step 5: Wire the CLI**

Add to `src/photodedup/cli.py`, importing `run_hash` from `photodedup.hash_stage`:

```python
@main.command()
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
@click.option("--workers", type=int, default=None, help="Parallel workers. Defaults to CPU count minus one.")
@click.option("--limit", type=int, default=None, help="Hash at most this many assets, for a trial run.")
def hash(catalog: Path, workers: int | None, limit: int | None) -> None:
    """Compute content hashes for every surveyed asset. Read-only, resumable."""
    conn = open_catalog(catalog)
    result = run_hash(
        conn,
        workers=workers,
        limit=limit,
        progress=lambda done, total: click.echo(f"  {done}/{total}", err=True),
    )
    click.echo(
        f"{result.total} assets to hash "
        f"({result.hashed} hashed, {result.no_original} without originals, "
        f"{result.errors} errors); {result.already_done} already complete"
    )
```

Note that `hash` shadows the builtin inside this module. That is acceptable for a Click command name, which must match the CLI verb. If it bothers a linter, name the function `hash_command` and pass `@main.command("hash")`.

- [ ] **Step 6: Verify the CLI and run a trial**

```bash
uv run photodedup hash --help
uv run photodedup hash --limit 500
```

Record the timing. Extrapolate to 65,974 assets and put the estimate in your report.

- [ ] **Step 7: Commit**

```bash
git add src/photodedup/hash_stage.py src/photodedup/cli.py tests/test_hash_stage.py
git commit -m "feat: add parallel resumable hash stage"
```

---

### Task 6: Grouping

**Files:**
- Create: `src/photodedup/grouping.py`
- Modify: `src/photodedup/cli.py`
- Test: `tests/test_grouping.py`

**Interfaces:**
- Consumes: `catalog`, the `asset_hash` table.
- Produces:
  - `GroupResult` frozen dataclass: `groups: int`, `members: int`, `removable: int` (members minus one per group).
  - `run_group(conn) -> GroupResult`
  - Tables `dup_group(id, signal, tier, size)` and `dup_member(group_id, uuid)`.
  - CLI: `photodedup group`

**Signals and tiers:**

| signal | tier | meaning |
|---|---|---|
| `scan` | `exact` | identical JPEG scan data. The cohort A / cohort B case. |
| `pixel` | `exact` | identical decoded pixels. Catches HEIC, PNG, and container rewrites. |
| `video` | `exact` | identical encoded video stream. |
| `dhash` | `candidate` | identical perceptual hash. Review only, never auto-resolved. |

A group is formed per signal. An asset may appear in more than one group, which is expected: a `scan` group and a `pixel` group over the same assets are two views of the same fact. Deduplicate at the reporting layer, not here.

**Hard partitions.** Within every signal, assets are partitioned by `kind` and by `has_raw` before grouping, and a group is only emitted if all its members share both. A RAW file never groups with a non-RAW even if the pixels match, because they are not interchangeable.

**Singletons are not groups.** Only sets of two or more are recorded.

- [ ] **Step 1: Confirm the tables exist**

`dup_group` and `dup_member` were created in Task 1, at schema version 2. This
task adds no schema. Confirm before writing code:

```bash
uv run python -c "
from pathlib import Path
from photodedup.catalog import open_catalog
c = open_catalog(Path('/tmp/schema-check.sqlite'))
print(sorted(r[0] for r in c.execute(
    \"SELECT name FROM sqlite_master WHERE type='table'\")))
" && rm -f /tmp/schema-check.sqlite
```

Expected to include `dup_group` and `dup_member`.

- [ ] **Step 2: Write the failing test**

`tests/test_grouping.py`:

```python
import pytest

from photodedup.catalog import open_catalog
from photodedup.grouping import run_group


@pytest.fixture
def conn(tmp_path):
    return open_catalog(tmp_path / "catalog.sqlite")


def add(conn, uuid, *, kind="image", has_raw=0, scan=None, pixel=None,
        video=None, dhash=None, status="ok"):
    conn.execute(
        """INSERT INTO asset (uuid, kind, has_raw, survey_status)
           VALUES (?, ?, ?, 'ok')""",
        (uuid, kind, has_raw),
    )
    conn.execute(
        """INSERT INTO asset_hash (uuid, status, scan_hash, pixel_hash, video_hash, dhash)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (uuid, status, scan, pixel, video, dhash),
    )
    conn.commit()


def groups(conn, signal=None):
    sql = "SELECT * FROM dup_group"
    if signal:
        sql += f" WHERE signal = '{signal}'"
    return conn.execute(sql).fetchall()


def test_identical_scan_hashes_form_one_exact_group(conn):
    add(conn, "u1", scan="aaa")
    add(conn, "u2", scan="aaa")
    result = run_group(conn)
    assert result.groups == 1
    assert result.members == 2
    assert result.removable == 1
    row = groups(conn)[0]
    assert row["signal"] == "scan" and row["tier"] == "exact" and row["size"] == 2


def test_singletons_are_not_groups(conn):
    add(conn, "u1", scan="aaa")
    add(conn, "u2", scan="bbb")
    assert run_group(conn).groups == 0


def test_null_hashes_never_group(conn):
    """Two assets that both failed to hash are not therefore duplicates."""
    add(conn, "u1", scan=None, pixel=None)
    add(conn, "u2", scan=None, pixel=None)
    assert run_group(conn).groups == 0


def test_raw_never_groups_with_non_raw(conn):
    add(conn, "u1", pixel="ppp", has_raw=1)
    add(conn, "u2", pixel="ppp", has_raw=0)
    assert run_group(conn).groups == 0


def test_video_never_groups_with_image(conn):
    add(conn, "u1", kind="video", video="vvv")
    add(conn, "u2", kind="image", video="vvv")
    assert run_group(conn).groups == 0


def test_videos_group_on_stream_hash(conn):
    add(conn, "u1", kind="video", video="vvv")
    add(conn, "u2", kind="video", video="vvv")
    result = run_group(conn)
    assert result.groups == 1
    assert groups(conn)[0]["signal"] == "video"


def test_dhash_groups_are_candidate_tier(conn):
    """Perceptual matches are never auto-resolvable, so they must not be exact."""
    add(conn, "u1", dhash="dddd")
    add(conn, "u2", dhash="dddd")
    run_group(conn)
    assert [r["tier"] for r in groups(conn, "dhash")] == ["candidate"]


def test_scan_and_pixel_groups_coexist(conn):
    add(conn, "u1", scan="aaa", pixel="ppp")
    add(conn, "u2", scan="aaa", pixel="ppp")
    run_group(conn)
    assert {r["signal"] for r in groups(conn)} == {"scan", "pixel"}


def test_rerun_is_idempotent(conn):
    add(conn, "u1", scan="aaa")
    add(conn, "u2", scan="aaa")
    first = run_group(conn)
    second = run_group(conn)
    assert (first.groups, first.members) == (second.groups, second.members)
    assert conn.execute("SELECT COUNT(*) AS n FROM dup_group").fetchone()["n"] == 1


def test_errored_hashes_are_excluded(conn):
    add(conn, "u1", scan="aaa", status="error")
    add(conn, "u2", scan="aaa", status="error")
    assert run_group(conn).groups == 0


def test_group_of_three_reports_two_removable(conn):
    for u in ("u1", "u2", "u3"):
        add(conn, u, scan="aaa")
    result = run_group(conn)
    assert result.members == 3
    assert result.removable == 2
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
uv run pytest tests/test_grouping.py -v
```

- [ ] **Step 4: Write the implementation**

`src/photodedup/grouping.py`:

```python
"""Form duplicate sets from the content signals. Decides nothing."""

from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection

from photodedup.catalog import set_stage

# (signal, hash column, tier). Order is the trust order and is also the order
# the review report presents them in.
SIGNALS: tuple[tuple[str, str, str], ...] = (
    ("scan", "scan_hash", "exact"),
    ("pixel", "pixel_hash", "exact"),
    ("video", "video_hash", "exact"),
    ("dhash", "dhash", "candidate"),
)


@dataclass(frozen=True)
class GroupResult:
    groups: int
    members: int
    removable: int


def run_group(conn: Connection) -> GroupResult:
    """Rebuild every duplicate group from scratch. Idempotent by construction."""
    set_stage(conn, "group", "running")
    conn.execute("DELETE FROM dup_member")
    conn.execute("DELETE FROM dup_group")

    groups = members = 0

    for signal, column, tier in SIGNALS:
        # Partition by kind and has_raw inside the key itself, so a group can
        # never span the hard partitions no matter what the hashes say.
        rows = conn.execute(
            f"""
            SELECT h.{column} AS digest, a.kind AS kind, a.has_raw AS has_raw,
                   GROUP_CONCAT(a.uuid) AS uuids, COUNT(*) AS n
            FROM asset_hash h
            JOIN asset a ON a.uuid = h.uuid AND a.survey_status = 'ok'
            WHERE h.status = 'ok' AND h.{column} IS NOT NULL
            GROUP BY h.{column}, a.kind, a.has_raw
            HAVING n > 1
            """
        ).fetchall()

        for row in rows:
            cursor = conn.execute(
                "INSERT INTO dup_group (signal, tier, digest, size) VALUES (?, ?, ?, ?)",
                (signal, tier, row["digest"], row["n"]),
            )
            group_id = cursor.lastrowid
            conn.executemany(
                "INSERT INTO dup_member (group_id, uuid) VALUES (?, ?)",
                [(group_id, uuid) for uuid in row["uuids"].split(",")],
            )
            groups += 1
            members += row["n"]

    conn.commit()
    removable = members - groups
    set_stage(
        conn, "group", "complete",
        notes=f"{groups} groups, {members} members, {removable} removable",
    )
    return GroupResult(groups=groups, members=members, removable=removable)
```

`GROUP_CONCAT` is safe here because asset uuids are hex with dashes and contain no commas.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_grouping.py -v
```

- [ ] **Step 6: Wire the CLI**

```python
@main.command()
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
def group(catalog: Path) -> None:
    """Form duplicate groups from the content hashes. Decides nothing."""
    conn = open_catalog(catalog)
    result = run_group(conn)
    click.echo(
        f"{result.groups} groups covering {result.members} assets; "
        f"{result.removable} removable if every group keeps one"
    )
```

- [ ] **Step 7: Commit**

```bash
git add src/photodedup/grouping.py src/photodedup/cli.py tests/test_grouping.py
git commit -m "feat: form duplicate groups under hard partitions"
```

---

### Task 7: Duplicate report

**Files:**
- Modify: `src/photodedup/report.py`
- Modify: `src/photodedup/cli.py`
- Test: `tests/test_report_groups.py`

**Interfaces:**
- Consumes: `dup_group`, `dup_member`, `asset`, `asset_hash`.
- Produces:
  - `duplicate_report(conn) -> str`
  - `signal_overlap(conn) -> list[tuple[str, int]]` — how many assets each signal finds that the exact signals do not, which is the number that says whether perceptual matching is worth the review effort.
  - `cohort_pairs(conn) -> int` — exact groups whose members disagree about capture date by more than 26 hours. These are the cohort A / cohort B pairs, the ones where de-duplication actually rescues a date.
  - CLI: `photodedup duplicates [--out reports/duplicates.md]`

The report must cover: group and member counts by signal and tier; removable totals for exact tiers alone and including candidates; the distribution of group sizes; how many assets appear in more than one group; `signal_overlap`; `cohort_pairs`; and a sample of twenty exact groups rendered with each member's filename, capture date, file size and dimensions, so the numbers can be spot-checked by eye.

- [ ] **Step 1: Write the failing test**

`tests/test_report_groups.py`:

```python
from datetime import datetime

import pytest

from photodedup.catalog import open_catalog
from photodedup.grouping import run_group
from photodedup.report import cohort_pairs, duplicate_report, signal_overlap


@pytest.fixture
def conn(tmp_path):
    return open_catalog(tmp_path / "catalog.sqlite")


def add(conn, uuid, *, scan=None, pixel=None, dhash=None, photos_date=None,
        filename="x.jpg", size=1000):
    conn.execute(
        """INSERT INTO asset (uuid, kind, has_raw, survey_status, original_filename,
                              photos_date, file_size, width, height)
           VALUES (?, 'image', 0, 'ok', ?, ?, ?, 100, 100)""",
        (uuid, filename, photos_date.isoformat() if photos_date else None, size),
    )
    conn.execute(
        """INSERT INTO asset_hash (uuid, status, scan_hash, pixel_hash, dhash)
           VALUES (?, 'ok', ?, ?, ?)""",
        (uuid, scan, pixel, dhash),
    )
    conn.commit()


def test_report_renders_on_an_empty_catalog(conn):
    assert "# Duplicate Report" in duplicate_report(conn)


def test_report_counts_exact_groups(conn):
    add(conn, "u1", scan="aaa")
    add(conn, "u2", scan="aaa")
    run_group(conn)
    text = duplicate_report(conn)
    assert "# Duplicate Report" in text
    assert "scan" in text


def test_cohort_pairs_counts_date_disagreement_within_a_group(conn):
    add(conn, "u1", scan="aaa", photos_date=datetime(2003, 2, 23, 9, 8, 8))
    add(conn, "u2", scan="aaa", photos_date=datetime(2020, 12, 18, 9, 12, 48))
    run_group(conn)
    assert cohort_pairs(conn) == 1


def test_cohort_pairs_ignores_agreeing_groups(conn):
    when = datetime(2003, 2, 23, 9, 8, 8)
    add(conn, "u1", scan="aaa", photos_date=when)
    add(conn, "u2", scan="aaa", photos_date=when)
    run_group(conn)
    assert cohort_pairs(conn) == 0


def test_signal_overlap_reports_candidate_only_assets(conn):
    """An asset found only by perceptual hash is the review cost."""
    add(conn, "u1", scan="aaa", dhash="dddd")
    add(conn, "u2", scan="aaa", dhash="dddd")
    add(conn, "u3", dhash="dddd")
    run_group(conn)
    overlap = dict(signal_overlap(conn))
    assert overlap["dhash_only"] == 1
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_report_groups.py -v
```

- [ ] **Step 3: Implement**

Append to `src/photodedup/report.py`:

```python
def group_totals(conn: Connection) -> list[tuple[str, str, int, int, int]]:
    """Per signal: tier, group count, member count, removable count."""
    rows = conn.execute(
        """
        SELECT signal, tier, COUNT(*) AS groups, SUM(size) AS members
        FROM dup_group GROUP BY signal, tier ORDER BY tier, signal
        """
    ).fetchall()
    return [
        (r["signal"], r["tier"], r["groups"], r["members"], r["members"] - r["groups"])
        for r in rows
    ]


def group_size_distribution(conn: Connection) -> list[tuple[int, int]]:
    rows = conn.execute(
        """
        SELECT size, COUNT(*) AS n FROM dup_group
        GROUP BY size ORDER BY size ASC
        """
    ).fetchall()
    return [(r["size"], r["n"]) for r in rows]


def signal_overlap(conn: Connection) -> list[tuple[str, int]]:
    """How much extra the perceptual signal finds beyond the exact ones.

    `dhash_only` is the review cost: assets no exact signal caught. If it is
    small, perceptual review is cheap; if it is huge, it needs its own policy.
    """
    exact = """
        SELECT DISTINCT m.uuid FROM dup_member m
        JOIN dup_group g ON g.id = m.group_id WHERE g.tier = 'exact'
    """
    dh = """
        SELECT DISTINCT m.uuid FROM dup_member m
        JOIN dup_group g ON g.id = m.group_id WHERE g.signal = 'dhash'
    """
    one = lambda sql: conn.execute(f"SELECT COUNT(*) AS n FROM ({sql})").fetchone()["n"]
    return [
        ("exact_assets", one(exact)),
        ("dhash_assets", one(dh)),
        ("both", one(f"{exact} INTERSECT {dh}")),
        ("dhash_only", one(f"{dh} EXCEPT {exact}")),
    ]


def multi_group_assets(conn: Connection) -> int:
    """Assets appearing in more than one group, which the signals expect."""
    return conn.execute(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT uuid FROM dup_member GROUP BY uuid HAVING COUNT(*) > 1
        )
        """
    ).fetchone()["n"]


def cohort_pairs(conn: Connection) -> int:
    """Exact groups whose members disagree about capture date by over the margin.

    These are the cohort A / cohort B pairs. They are the groups where removing
    a duplicate also rescues a date, which is the whole point of the project.
    """
    return conn.execute(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT g.id
            FROM dup_group g
            JOIN dup_member m ON m.group_id = g.id
            JOIN asset a ON a.uuid = m.uuid
            WHERE g.tier = 'exact' AND a.photos_date IS NOT NULL
            GROUP BY g.id
            HAVING COUNT(a.photos_date) > 1
               AND strftime('%s', MAX(a.photos_date))
                   - strftime('%s', MIN(a.photos_date)) > :margin
        )
        """,
        {"margin": _MARGIN_SECONDS},
    ).fetchone()["n"]


def sample_groups(conn: Connection, *, limit: int = 20) -> list[list[tuple]]:
    """A few exact groups rendered member by member, for checking by eye."""
    ids = [
        r["id"]
        for r in conn.execute(
            """
            SELECT g.id FROM dup_group g WHERE g.tier = 'exact'
            ORDER BY g.size DESC, g.id ASC LIMIT :limit
            """,
            {"limit": limit},
        )
    ]
    out = []
    for group_id in ids:
        rows = conn.execute(
            """
            SELECT g.signal AS signal, a.original_filename AS name,
                   a.photos_date AS captured, a.file_size AS bytes,
                   a.width AS w, a.height AS h
            FROM dup_member m
            JOIN dup_group g ON g.id = m.group_id
            JOIN asset a ON a.uuid = m.uuid
            WHERE m.group_id = :gid
            ORDER BY a.photos_date IS NULL, a.photos_date ASC
            """,
            {"gid": group_id},
        ).fetchall()
        out.append(
            [
                (r["signal"], r["name"], r["captured"], r["bytes"], f"{r['w']}x{r['h']}")
                for r in rows
            ]
        )
    return out


def duplicate_report(conn: Connection) -> str:
    totals = group_totals(conn)
    exact_removable = sum(t[4] for t in totals if t[1] == "exact")
    all_removable = sum(t[4] for t in totals)

    parts = [
        "# Duplicate Report\n",
        "Groups are formed per signal, so one set of assets can appear as both",
        "a `scan` group and a `pixel` group. Those are two views of one fact.",
        "Only `exact` tiers are ever safe to resolve automatically.\n",
        f"- Removable on exact signals alone: **{exact_removable}**",
        f"- Removable including perceptual candidates: **{all_removable}**",
        f"- Assets in more than one group: {multi_group_assets(conn)}\n",
        "## Groups by signal\n",
        _table(
            ("signal", "tier", "groups", "members", "removable"),
            [tuple(t) for t in totals],
        ),
        "\n## Group sizes\n",
        _table(("members in group", "groups"), group_size_distribution(conn)),
        "\n## Signal overlap\n",
        "`dhash_only` counts assets that only perceptual matching found. That",
        "number is the review workload perceptual matching would add.\n",
        _table(("measure", "assets"), signal_overlap(conn)),
        "\n## Groups that rescue a date\n",
        "Exact groups whose members disagree about capture date by more than 26",
        "hours. These are the cohort A / cohort B pairs, where de-duplicating",
        "also recovers the true date.\n",
        _table(("measure", "groups"), [("date-rescuing exact groups", cohort_pairs(conn))]),
        "\n## Sample groups\n",
        "The largest exact groups, member by member, for checking by eye.\n",
    ]

    for index, members in enumerate(sample_groups(conn), start=1):
        parts.append(f"\n**Group {index}**\n")
        parts.append(_table(("signal", "filename", "captured", "bytes", "size"), members))

    return "\n".join(parts)
```

- [ ] **Step 4: Run the tests and the full suite**

```bash
uv run pytest -v
```

- [ ] **Step 5: Wire the CLI and run the whole pipeline for real**

```bash
uv run photodedup hash
uv run photodedup group
uv run photodedup duplicates
```

The hash run is long. It is resumable, so interrupting it is safe.

- [ ] **Step 6: Update STATE.md and commit**

Record the real numbers: how many exact groups and removable assets, how many `cohort_pairs`, how many assets only perceptual matching finds, the hash stage's wall-clock time and error count, and how the exact-group count compares with the survey's floor of 16,801 removable. Note explicitly whether the scan hash found cross-cohort pairs that a whole-file hash would have missed, since that is the premise the whole approach rests on.

```bash
git add STATE.md reports/duplicates.md src/photodedup/report.py src/photodedup/cli.py tests/test_report_groups.py
git commit -m "feat: add duplicate report; record real grouping results"
```

---

## What Phase 3 covers, and why it is not planned yet

Phase 3 is arbitration, the HTML review report, the archive, and the apply and
verify stages. It waits on this plan's numbers for the same reason Phase 2
waited on the survey's: the amount of review work, and therefore whether the
HTML report needs to be paginated at all, depends on how many candidate groups
exist. The survey's floor of 16,801 removable assets is a lower bound derived
from file size alone and could be badly wrong in either direction.

Two obligations carry into Phase 3 from earlier work:

- `choose_date` in `dates.py` is written and tested but unused, and the survey
  report computes date disagreement separately in SQL. The two have already
  drifted: the SQL applies no margin where `choose_date` uses 26 hours. Phase 3
  must make `choose_date` the single authority and reduce the SQL to a view
  over its output.
- Clock-reset cohorts, described in the spec's survey-findings section, are a
  date-recovery mechanism independent of de-duplication. They can be built in
  Phase 3 or deferred, but they should not be forgotten: they are the only
  route back for the 73 photos stranded on the 2001 epoch.
