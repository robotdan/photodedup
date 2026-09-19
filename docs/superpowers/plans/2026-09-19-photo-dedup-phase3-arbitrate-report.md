# Photo De-duplication, Phase 3: Arbitrate and Review Report — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For every duplicate set, decide which copy to keep and what its capture date really is, and produce a static HTML report the user can sample to check those decisions before anything is archived or deleted.

**Architecture:** One correctness fix first (truncated files get a lenient hash and a byte-prefix exact rule), then per-signal groups are merged into one exact set per asset, then a pure `arbitrate` stage writes a `decision` row per set from a fixed keeper tie-break order and a single date authority in `dates.py`, then `review.py` renders the decisions as paginated HTML using thumbnails Photos already keeps. Everything is read-only against the Photos library and writes only to `./catalog.sqlite` and `./reports/`.

**Tech Stack:** Python 3.13, uv, Click, SQLite, Pillow with pillow-heif, imagehash, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-19-photo-dedup-phase3-arbitrate-report-design.md`, which amends and narrows `docs/superpowers/specs/2026-09-18-photo-dedup-design.md`. Read both. Read `STATE.md` for the measured numbers every expected result below comes from.

## Global Constraints

- Repository root `/Users/robotdan/dev/tools`; package at `src/photodedup/`; run everything through `uv run`.
- Phase 3 is strictly READ-ONLY against the Photos library. No task may write to the library, launch Photos, or call a mutating osxphotos function. Writes go only to `./catalog.sqlite` and `./reports/`.
- `SCHEMA_VERSION` becomes 3. The catalog is rebuilt, never migrated. Do not write migration code.
- Every stage reads only assets with `survey_status = 'ok'`.
- Hard partitions from Phase 2 stay: `kind` and `has_raw` are part of every group key and no set may cross them.
- Keeper tie-break order, fixed by the spec: intact beats truncated; larger `file_size`; `is_favorite`; `has_adjustments`; non-empty title or description; more albums; earlier `date_added`; lowest `uuid`. Rule names are the literal strings `intact`, `file_size`, `favorite`, `adjustments`, `caption`, `albums`, `date_added`, `uuid`.
- `choose_date` / `choose_date_pooled` in `dates.py` are the only date authority. No SQL may re-express a rejection rule.
- `IMPORT_STAMPS` is a hand-confirmed constant. Do not derive it from clusters.
- Tests run with `uv run pytest -q`; the suite must stay green after every task. 182 tests pass at the start.
- Tasks run strictly in order; each appends to files the next one reads.
- The agent shell cannot start a `ProcessPoolExecutor`, cannot import osxphotos without the photoscript stub in `library.py`, and cannot see `/Volumes`. Tests must not depend on any of those.
- Commit after every task with a conventional message. Do not commit `catalog.sqlite*` or anything under `reports/` (both gitignored).

---

### Task 1: Schema version 3

**Files:**
- Modify: `src/photodedup/catalog.py`
- Test: `tests/test_catalog.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `SCHEMA_VERSION == 3`; `asset.thumb_path`; `asset_hash.status` accepting `'truncated'`; `dup_group.signal` accepting `'prefix'`; tables `dup_set`, `dup_set_member`, `decision`, `decision_loser`; `class StageIncomplete(RuntimeError)`; `require_stage_complete(conn, name: str, *, force: bool = False) -> None`.

- [ ] **Step 1: Write the failing tests**

Make sure the top of `tests/test_catalog.py` has these imports (add any that are missing, keep the ones already there):

```python
import sqlite3

import pytest

from photodedup.catalog import (
    SCHEMA_VERSION,
    StageIncomplete,
    open_catalog,
    require_stage_complete,
    set_stage,
)
```

Append:

```python
def test_schema_version_is_3():
    assert SCHEMA_VERSION == 3


def test_phase3_tables_exist(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"dup_set", "dup_set_member", "decision", "decision_loser"} <= tables


def test_asset_has_thumb_path(conn):
    conn.execute("INSERT INTO asset (uuid, thumb_path) VALUES ('u1', '/x/thumb.jpg')")
    row = conn.execute("SELECT thumb_path FROM asset WHERE uuid='u1'").fetchone()
    assert row["thumb_path"] == "/x/thumb.jpg"


def test_truncated_is_a_valid_hash_status(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    conn.execute("INSERT INTO asset_hash (uuid, status) VALUES ('u1', 'truncated')")


def test_prefix_is_a_valid_group_signal(conn):
    conn.execute(
        "INSERT INTO dup_group (signal, tier, digest, size) VALUES ('prefix', 'exact', 'd', 2)"
    )


def test_asset_in_two_exact_sets_is_rejected(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    conn.execute("INSERT INTO dup_set (id, tier, kind, size) VALUES (1, 'exact', 'image', 2)")
    conn.execute("INSERT INTO dup_set (id, tier, kind, size) VALUES (2, 'exact', 'image', 2)")
    conn.execute("INSERT INTO dup_set_member VALUES (1, 'u1', 'exact', 'scan')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO dup_set_member VALUES (2, 'u1', 'exact', 'scan')")


def test_asset_may_be_in_one_exact_and_one_candidate_set(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    conn.execute("INSERT INTO dup_set (id, tier, kind, size) VALUES (1, 'exact', 'image', 2)")
    conn.execute("INSERT INTO dup_set (id, tier, kind, size) VALUES (2, 'candidate', 'image', 4)")
    conn.execute("INSERT INTO dup_set_member VALUES (1, 'u1', 'exact', 'scan')")
    conn.execute("INSERT INTO dup_set_member VALUES (2, 'u1', 'candidate', 'dhash')")


def test_decision_cascades_on_set_delete(conn):
    conn.execute("INSERT INTO asset (uuid) VALUES ('u1')")
    conn.execute("INSERT INTO asset (uuid) VALUES ('u2')")
    conn.execute("INSERT INTO dup_set (id, tier, kind, size) VALUES (1, 'exact', 'image', 2)")
    conn.execute(
        """INSERT INTO decision (set_id, keeper_uuid, keeper_reason, date_changes,
                                 resolvable, review_flags, decided_at)
           VALUES (1, 'u1', 'uuid', 0, 1, '', '2026-09-19T00:00:00')"""
    )
    conn.execute(
        "INSERT INTO decision_loser (set_id, uuid, lost_on, carries) VALUES (1, 'u2', 'uuid', '{}')"
    )
    conn.execute("DELETE FROM dup_set WHERE id = 1")
    assert conn.execute("SELECT COUNT(*) AS n FROM decision").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM decision_loser").fetchone()["n"] == 0


def test_require_stage_complete_passes_when_complete(conn):
    set_stage(conn, "group", "complete")
    require_stage_complete(conn, "group")


def test_require_stage_complete_refuses_otherwise(conn):
    set_stage(conn, "group", "running")
    with pytest.raises(StageIncomplete) as exc:
        require_stage_complete(conn, "group")
    assert "running" in str(exc.value)
    assert "--force" in str(exc.value)


def test_require_stage_complete_refuses_when_never_run(conn):
    with pytest.raises(StageIncomplete) as exc:
        require_stage_complete(conn, "group")
    assert "never run" in str(exc.value)


def test_require_stage_complete_force_overrides(conn):
    require_stage_complete(conn, "group", force=True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_catalog.py -q`
Expected: ImportError on `StageIncomplete` (collection fails). That is the failing state.

- [ ] **Step 3: Implement**

In `src/photodedup/catalog.py`:

Change `SCHEMA_VERSION = 2` to `SCHEMA_VERSION = 3`.

In the `asset` table, after the line `    path              TEXT,` add:

```sql
    thumb_path        TEXT,
```

Change the `asset_hash.status` line to:

```sql
    status       TEXT NOT NULL CHECK (status IN ('ok', 'error', 'no_original', 'skipped', 'truncated')),
```

Change the `dup_group.signal` line to:

```sql
    signal TEXT NOT NULL CHECK (signal IN ('scan', 'pixel', 'video', 'dhash', 'prefix')),
```

After the line `CREATE INDEX IF NOT EXISTS dup_group_tier ON dup_group (tier, signal);` and before the closing `"""`, add:

```sql

CREATE TABLE IF NOT EXISTS dup_set (
    id       INTEGER PRIMARY KEY,
    tier     TEXT NOT NULL CHECK (tier IN ('exact', 'candidate')),
    kind     TEXT NOT NULL,
    size     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dup_set_member (
    set_id   INTEGER NOT NULL REFERENCES dup_set (id) ON DELETE CASCADE,
    uuid     TEXT NOT NULL REFERENCES asset (uuid) ON DELETE CASCADE,
    tier     TEXT NOT NULL CHECK (tier IN ('exact', 'candidate')),
    signals  TEXT NOT NULL,
    PRIMARY KEY (set_id, uuid)
);

CREATE UNIQUE INDEX IF NOT EXISTS dup_set_member_exact
    ON dup_set_member (uuid) WHERE tier = 'exact';
CREATE INDEX IF NOT EXISTS dup_set_member_uuid ON dup_set_member (uuid);
CREATE INDEX IF NOT EXISTS dup_set_tier ON dup_set (tier);

CREATE TABLE IF NOT EXISTS decision (
    set_id         INTEGER PRIMARY KEY REFERENCES dup_set (id) ON DELETE CASCADE,
    keeper_uuid    TEXT NOT NULL REFERENCES asset (uuid),
    keeper_reason  TEXT NOT NULL,
    date_value     TEXT,
    date_source    TEXT,
    date_rejected  TEXT,
    date_changes   INTEGER NOT NULL,
    resolvable     INTEGER NOT NULL,
    review_flags   TEXT NOT NULL,
    decided_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS decision_resolvable ON decision (resolvable);

CREATE TABLE IF NOT EXISTS decision_loser (
    set_id   INTEGER NOT NULL REFERENCES dup_set (id) ON DELETE CASCADE,
    uuid     TEXT NOT NULL REFERENCES asset (uuid),
    lost_on  TEXT NOT NULL,
    carries  TEXT NOT NULL,
    PRIMARY KEY (set_id, uuid)
);
```

Append to the end of the module:

```python
class StageIncomplete(RuntimeError):
    """A stage was asked to run before the stage it depends on finished."""


def require_stage_complete(
    conn: sqlite3.Connection, name: str, *, force: bool = False
) -> None:
    """Refuse unless stage *name* has reported complete, or *force* is set.

    Every stage after `hash` builds numbers that look final. Running one over
    a half-finished predecessor produces a report nobody can tell apart from
    a real one, so the default is to refuse and say why.
    """
    if force:
        return
    stage = get_stage(conn, name)
    status = stage["status"] if stage is not None else "never run"
    if status != "complete":
        raise StageIncomplete(
            f"the {name} stage is '{status}', not 'complete'. Finish "
            f"`photodedup {name}` first, or pass --force if you deliberately "
            "want results over a partial catalog."
        )
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass. The existing `test_catalog.py` tests that assert version 2, if any, must be updated to 3; there should be exactly one such assertion at most.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/catalog.py tests/test_catalog.py
git commit -m "feat: schema v3 with sets, decisions, truncated status and stage guard"
```

---

### Task 2: Truncated files hash leniently

**Files:**
- Modify: `src/photodedup/hashing.py`
- Modify: `src/photodedup/hash_stage.py`
- Test: `tests/test_hashing_pixel.py`, `tests/test_hash_stage.py`

**Interfaces:**
- Consumes: `pixel_hashes`, `PixelHashes`, `hash_one`, `run_hash`, `HashResult` from Phase 2.
- Produces: `real_length(data: bytes) -> int`; `pixel_hashes(path: Path, *, lenient: bool = False) -> PixelHashes | None`; `hash_one` returning `status='truncated'` rows with `bytes_read` = real length; `HashResult.truncated: int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hashing_pixel.py` (add `from PIL import Image, ImageFile` and `from photodedup.hashing import pixel_hashes, real_length` to its imports if not present):

```python
def make_noisy_jpeg(path):
    """A JPEG with a large entropy-coded segment, so cutting 200 bytes before
    EOI leaves a decodable but incomplete scan. A flat-colour image has a scan
    only a few bytes long and would be cut into the headers instead."""
    Image.effect_noise((96, 64), 64).convert("RGB").save(path, quality=95)
    return path


def make_truncated_jpeg(path, *, pad=0):
    intact = make_noisy_jpeg(path.with_name("intact_" + path.name))
    data = intact.read_bytes()
    eoi = data.rfind(b"\xff\xd9")
    assert eoi > 400
    path.write_bytes(data[: eoi - 200] + b"\x00" * pad)
    return path


def test_strict_decode_refuses_a_truncated_jpeg(tmp_path):
    assert pixel_hashes(make_truncated_jpeg(tmp_path / "t.jpg")) is None


def test_lenient_decode_hashes_a_truncated_jpeg(tmp_path):
    hashes = pixel_hashes(make_truncated_jpeg(tmp_path / "t.jpg"), lenient=True)
    assert hashes is not None
    assert (hashes.width, hashes.height) == (96, 64)
    assert hashes.dhash and hashes.phash and hashes.pixel_hash


def test_lenient_flag_does_not_leak(tmp_path):
    pixel_hashes(make_truncated_jpeg(tmp_path / "t.jpg"), lenient=True)
    assert ImageFile.LOAD_TRUNCATED_IMAGES is False
    assert pixel_hashes(tmp_path / "t.jpg") is None


def test_real_length_strips_trailing_zeros():
    assert real_length(b"abc\x00\x00") == 3
    assert real_length(b"abc") == 3
    assert real_length(b"\x00\x00") == 0
    assert real_length(b"") == 0


def test_real_length_of_zero_padded_truncation_matches_unpadded(tmp_path):
    plain = make_truncated_jpeg(tmp_path / "a.jpg").read_bytes()
    padded = make_truncated_jpeg(tmp_path / "b.jpg", pad=5000).read_bytes()
    assert real_length(padded) == real_length(plain)
```

Append to `tests/test_hash_stage.py`:

```python
from photodedup.hash_stage import hash_one
from photodedup.hashing import real_length


def make_truncated(path):
    intact = path.with_name("intact_" + path.name)
    Image.effect_noise((96, 64), 64).convert("RGB").save(intact, quality=95)
    data = intact.read_bytes()
    path.write_bytes(data[: data.rfind(b"\xff\xd9") - 200] + b"\x00" * 300)
    return path


def test_hash_one_reports_truncated_with_perceptual_hashes(tmp_path):
    path = make_truncated(tmp_path / "t.jpg")
    row = hash_one("u1", str(path), "image", "public.jpeg")
    assert row["status"] == "truncated"
    assert row.get("scan_hash") is None
    assert row["dhash"] and row["phash"] and row["pixel_hash"]
    assert (row["pixel_width"], row["pixel_height"]) == (96, 64)
    assert row["bytes_read"] == real_length(path.read_bytes())


def test_hash_one_still_errors_on_a_non_image(tmp_path):
    path = tmp_path / "fake.png"
    path.write_bytes(b'{"normalImage": "iVBOR..."}')
    row = hash_one("u1", str(path), "image", "public.png")
    assert row["status"] == "error"
    assert row["error"] == "no usable hash"


def test_run_hash_counts_truncated_and_does_not_rehash_it(conn, tmp_path):
    add_asset(conn, "u1", path=make_truncated(tmp_path / "t.jpg"))
    first = run_hash(conn, workers=1)
    assert first.truncated == 1
    assert first.errors == 0
    second = run_hash(conn, workers=1)
    assert second.total == 0
    assert second.already_done == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hashing_pixel.py tests/test_hash_stage.py -q`
Expected: ImportError on `real_length`.

- [ ] **Step 3: Implement**

In `src/photodedup/hashing.py`, change the Pillow import to `from PIL import Image, ImageFile, ImageOps` and replace `pixel_hashes` with:

```python
def real_length(data: bytes) -> int:
    """Length of *data* with trailing zero bytes stripped.

    A truncated file that was later zero-filled to its original size (the
    2003 FUJI files and both 2016 files in this library) carries its real
    content in a prefix; this is that prefix's length, and it is what the
    group stage compares against an intact partner.
    """
    return len(data.rstrip(b"\x00"))


def pixel_hashes(path: Path, *, lenient: bool = False) -> PixelHashes | None:
    """Decode *path* once and derive every pixel-based signal from it.

    Returns None when the file cannot be decoded, which the hash stage records
    as an error rather than letting it abort a 65,974-asset run.

    With *lenient* the decoder tolerates a missing tail (no EOI marker), which
    is how a truncated file still yields perceptual hashes. The flag is a
    Pillow process global; it is set and restored around the decode so a
    strict call in the same process stays strict.
    """
    previous = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = lenient
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
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = previous
```

In `src/photodedup/hash_stage.py`:

Change the hashing import to `from photodedup.hashing import jpeg_scan_hash, pixel_hashes, real_length, video_stream_hash`.

Change `HashResult` to:

```python
@dataclass(frozen=True)
class HashResult:
    total: int
    hashed: int
    already_done: int
    no_original: int
    errors: int
    truncated: int = 0
```

Replace the final `if row.get("scan_hash") is None and pixels is None:` block of `hash_one` with:

```python
    if row.get("scan_hash") is None and pixels is None:
        # Neither signal: either the file is not an image at all, or it is a
        # JPEG cut short before its EOI. Only the second decodes leniently.
        partial = pixel_hashes(target, lenient=True)
        if partial is None:
            return {"uuid": uuid, "status": "error", "error": "no usable hash"}
        return {
            "uuid": uuid,
            "status": "truncated",
            "pixel_hash": partial.pixel_hash,
            "dhash": partial.dhash,
            "phash": partial.phash,
            "pixel_width": partial.width,
            "pixel_height": partial.height,
            "bytes_read": real_length(data),
        }
    return row
```

In `run_hash`, change the `already` query to `WHERE status IN ('ok', 'no_original', 'truncated')`.

Change the counters: `hashed = no_original = errors = truncated = 0`, and the `record` closure to:

```python
    def record(row: dict) -> None:
        nonlocal hashed, no_original, errors, truncated
        _write(conn, row)
        if row["status"] == "ok":
            hashed += 1
        elif row["status"] == "truncated":
            truncated += 1
        elif row["status"] == "no_original":
            no_original += 1
        else:
            errors += 1
```

Change the final `set_stage` notes to `f"{hashed} hashed, {truncated} truncated, {no_original} without originals, {errors} errors"` and the return to `HashResult(total, hashed, already, no_original, errors, truncated)`. The early `return HashResult(0, 0, already, 0, 0)` is unchanged (its `truncated` defaults to 0).

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/hashing.py src/photodedup/hash_stage.py tests/test_hashing_pixel.py tests/test_hash_stage.py
git commit -m "feat: hash truncated images leniently and record them as such"
```

---

### Task 3: Byte-prefix exact rule in grouping

**Files:**
- Modify: `src/photodedup/grouping.py` (whole file replaced below)
- Modify: `src/photodedup/report.py` (`coverage` only)
- Test: `tests/test_grouping.py`, `tests/test_report_groups.py`

**Interfaces:**
- Consumes: `asset_hash.status = 'truncated'` rows with `bytes_read`, `pixel_width`, `pixel_height` from Task 2.
- Produces: `prefix_groups(conn) -> list[tuple[str, list[str]]]`; `dup_group` rows with `signal='prefix'`; `dhash` candidate groups that include truncated members. `GroupResult` unchanged in this task (Task 6 extends it).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_grouping.py`:

```python
from PIL import Image

from photodedup.grouping import prefix_groups


def write_noisy_jpeg(path, seed):
    Image.effect_noise((96, 64), 64 + seed).convert("RGB").save(path, quality=95)
    return path


def add_file(conn, uuid, path, *, status, w=96, h=64, scan=None, pixel=None,
             dhash=None, bytes_read=None, uti="public.jpeg", file_size=None):
    size = file_size if file_size is not None else path.stat().st_size
    conn.execute(
        """INSERT INTO asset (uuid, kind, has_raw, uti, path, file_size, survey_status)
           VALUES (?, 'image', 0, ?, ?, ?, 'ok')""",
        (uuid, uti, str(path), size),
    )
    conn.execute(
        """INSERT INTO asset_hash (uuid, status, scan_hash, pixel_hash, dhash,
                                   pixel_width, pixel_height, bytes_read)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (uuid, status, scan, pixel, dhash, w, h, bytes_read),
    )
    conn.commit()


def truncate(intact, out, *, cut=200, pad=0):
    data = intact.read_bytes()
    body = data[: data.rfind(b"\xff\xd9") - cut]
    out.write_bytes(body + b"\x00" * pad)
    return out, len(body)


def test_truncated_prefix_of_intact_forms_a_prefix_group(conn, tmp_path):
    intact = write_noisy_jpeg(tmp_path / "a.jpg", 1)
    cut, length = truncate(intact, tmp_path / "a_cut.jpg", pad=300)
    add_file(conn, "ok1", intact, status="ok", scan="s1", pixel="p1")
    add_file(conn, "tr1", cut, status="truncated", bytes_read=length)
    result = run_group(conn)
    rows = groups(conn, "prefix")
    assert len(rows) == 1
    assert rows[0]["tier"] == "exact" and rows[0]["size"] == 2
    assert rows[0]["digest"] == "s1"
    members = {
        r["uuid"] for r in conn.execute(
            "SELECT uuid FROM dup_member WHERE group_id = ?", (rows[0]["id"],)
        )
    }
    assert members == {"ok1", "tr1"}
    assert result.groups == 1


def test_prefix_digest_falls_back_to_pixel_hash(conn, tmp_path):
    intact = write_noisy_jpeg(tmp_path / "a.jpg", 1)
    cut, length = truncate(intact, tmp_path / "a_cut.jpg")
    add_file(conn, "ok1", intact, status="ok", pixel="p1")
    add_file(conn, "tr1", cut, status="truncated", bytes_read=length)
    run_group(conn)
    assert groups(conn, "prefix")[0]["digest"] == "p1"


def test_diverging_bytes_with_same_dimensions_never_group(conn, tmp_path):
    a = write_noisy_jpeg(tmp_path / "a.jpg", 1)
    b = write_noisy_jpeg(tmp_path / "b.jpg", 2)
    cut, length = truncate(a, tmp_path / "a_cut.jpg")
    add_file(conn, "okb", b, status="ok", scan="s2", pixel="p2")
    add_file(conn, "tr1", cut, status="truncated", bytes_read=length)
    run_group(conn)
    assert groups(conn, "prefix") == []


def test_same_bytes_different_dimensions_are_never_compared(conn, tmp_path):
    """A partner whose recorded dimensions differ is not a candidate, even if
    its bytes would match. Dimensions come from the decode, and two files that
    decode to different sizes are not the same image."""
    intact = write_noisy_jpeg(tmp_path / "a.jpg", 1)
    cut, length = truncate(intact, tmp_path / "a_cut.jpg")
    add_file(conn, "ok1", intact, status="ok", scan="s1", pixel="p1", w=10, h=10)
    add_file(conn, "tr1", cut, status="truncated", bytes_read=length)
    run_group(conn)
    assert groups(conn, "prefix") == []


def test_partner_smaller_than_prefix_is_never_compared(conn, tmp_path):
    intact = write_noisy_jpeg(tmp_path / "a.jpg", 1)
    cut, length = truncate(intact, tmp_path / "a_cut.jpg")
    # Lie about the partner's size so the SQL filter, not the byte compare,
    # is what excludes it.
    add_file(conn, "ok1", intact, status="ok", scan="s1", pixel="p1", file_size=length - 1)
    add_file(conn, "tr1", cut, status="truncated", bytes_read=length)
    run_group(conn)
    assert groups(conn, "prefix") == []


def test_two_intact_copies_join_one_prefix_group(conn, tmp_path):
    intact = write_noisy_jpeg(tmp_path / "a.jpg", 1)
    copy = tmp_path / "a_copy.jpg"
    copy.write_bytes(intact.read_bytes())
    cut, length = truncate(intact, tmp_path / "a_cut.jpg")
    add_file(conn, "ok1", intact, status="ok", scan="s1", pixel="p1")
    add_file(conn, "ok2", copy, status="ok", scan="s1", pixel="p1")
    add_file(conn, "tr1", cut, status="truncated", bytes_read=length)
    run_group(conn)
    rows = groups(conn, "prefix")
    assert len(rows) == 1 and rows[0]["size"] == 3


def test_missing_truncated_file_is_skipped(conn, tmp_path):
    intact = write_noisy_jpeg(tmp_path / "a.jpg", 1)
    add_file(conn, "ok1", intact, status="ok", scan="s1", pixel="p1")
    add_file(conn, "tr1", tmp_path / "gone.jpg", status="truncated",
             bytes_read=100, file_size=100)
    assert prefix_groups(conn) == []


def test_truncated_joins_dhash_candidate_groups(conn):
    add(conn, "u1", dhash="dddd")
    add(conn, "u2", dhash="dddd", status="truncated")
    run_group(conn)
    rows = groups(conn, "dhash")
    assert len(rows) == 1 and rows[0]["size"] == 2


def test_truncated_never_joins_pixel_exact_groups(conn):
    add(conn, "u1", pixel="ppp")
    add(conn, "u2", pixel="ppp", status="truncated")
    run_group(conn)
    assert groups(conn, "pixel") == []


def test_truncated_with_no_partner_forms_no_group(conn, tmp_path):
    intact = write_noisy_jpeg(tmp_path / "a.jpg", 1)
    cut, length = truncate(intact, tmp_path / "a_cut.jpg")
    add_file(conn, "tr1", cut, status="truncated", bytes_read=length)
    assert run_group(conn).groups == 0
```

Append to `tests/test_report_groups.py`:

```python
def test_coverage_counts_truncated_rows(conn):
    add(conn, "u1", scan="aaa")
    add(conn, "u2", status="truncated", dhash="d")
    counts = dict(coverage(conn))
    assert counts["hashed ok"] == 1
    assert counts["hashed leniently (truncated)"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_grouping.py tests/test_report_groups.py -q`
Expected: ImportError on `prefix_groups`.

- [ ] **Step 3: Implement**

Replace `src/photodedup/grouping.py` entirely with:

```python
"""Form duplicate groups from the content signals. Decides nothing."""

from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection

from photodedup.catalog import get_stage, set_stage

# (signal, hash column, tier). Order is the trust order and is also the order
# the review report presents them in. A None column marks a signal computed by
# its own code path rather than by grouping on a stored hash.
SIGNALS: tuple[tuple[str, str | None, str], ...] = (
    ("scan", "scan_hash", "exact"),
    ("pixel", "pixel_hash", "exact"),
    ("video", "video_hash", "exact"),
    ("dhash", "dhash", "candidate"),
    ("prefix", None, "exact"),
)

# Formats that can hold an animation. `pixel_hashes` decodes frame 0 only, so
# two animations sharing a first frame would otherwise look byte-for-byte
# identical and land in an `exact` group that is safe to auto-resolve. They are
# not the same file. These may still form `candidate` groups for human review.
MULTIFRAME_UTIS: tuple[str, ...] = ("com.compuserve.gif", "org.webmproject.webp")

_MULTIFRAME_SQL = ", ".join(f"'{u}'" for u in MULTIFRAME_UTIS)


class HashStageIncomplete(RuntimeError):
    """Grouping was asked to run before the hash stage finished."""


@dataclass(frozen=True)
class GroupResult:
    groups: int
    members: int
    removable: int


def _read_prefix(path: str, length: int) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read(length)
    except OSError:
        return None


def prefix_groups(conn: Connection) -> list[tuple[str, list[str]]]:
    """Pair each truncated file with intact files it is a byte prefix of.

    A truncated JPEG has no scan hash (no EOI) and a pixel hash of a partial
    image, so no stored signal can link it to its intact twin. Its real bytes
    can: if the first `bytes_read` bytes of an intact file with the same
    decoded dimensions are identical, the two are the same file, one of them
    cut short. Returns (digest, [truncated_uuid, partner_uuid, ...]) per match;
    the digest is the partner's scan hash (or pixel hash) so that two
    truncated copies of one intact file merge into one set later.
    """
    truncated = conn.execute(
        """
        SELECT h.uuid AS uuid, a.path AS path, h.bytes_read AS length,
               h.pixel_width AS pw, h.pixel_height AS ph,
               a.kind AS kind, a.has_raw AS has_raw
        FROM asset_hash h
        JOIN asset a ON a.uuid = h.uuid AND a.survey_status = 'ok'
        WHERE h.status = 'truncated' AND a.path IS NOT NULL
          AND h.bytes_read IS NOT NULL AND h.bytes_read > 0
          AND h.pixel_width IS NOT NULL AND h.pixel_height IS NOT NULL
        ORDER BY h.uuid
        """
    ).fetchall()

    out: list[tuple[str, list[str]]] = []
    for row in truncated:
        head = _read_prefix(row["path"], row["length"])
        if head is None or len(head) < row["length"]:
            # Vanished or shorter than the hash stage saw it. Nothing to compare.
            continue
        partners = conn.execute(
            f"""
            SELECT h.uuid AS uuid, a.path AS path,
                   h.scan_hash AS scan_hash, h.pixel_hash AS pixel_hash
            FROM asset_hash h
            JOIN asset a ON a.uuid = h.uuid AND a.survey_status = 'ok'
            WHERE h.status = 'ok'
              AND a.kind = :kind AND a.has_raw = :has_raw
              AND h.pixel_width = :w AND h.pixel_height = :h
              AND a.file_size >= :length AND a.path IS NOT NULL
              AND COALESCE(a.uti, '') NOT IN ({_MULTIFRAME_SQL})
            ORDER BY h.uuid
            """,
            {
                "kind": row["kind"], "has_raw": row["has_raw"],
                "w": row["pw"], "h": row["ph"], "length": row["length"],
            },
        ).fetchall()
        matches = [p for p in partners if _read_prefix(p["path"], row["length"]) == head]
        if not matches:
            continue
        digest = matches[0]["scan_hash"] or matches[0]["pixel_hash"]
        out.append((digest, [row["uuid"]] + [m["uuid"] for m in matches]))
    return out


def run_group(conn: Connection, *, force: bool = False) -> GroupResult:
    """Rebuild every duplicate group from scratch. Idempotent by construction."""
    if not force:
        stage = get_stage(conn, "hash")
        status = stage["status"] if stage is not None else "never run"
        if status != "complete":
            raise HashStageIncomplete(
                f"the hash stage is '{status}', not 'complete'; grouping a "
                "partially hashed catalog produces numbers that look final and "
                "are not. Finish `photodedup hash`, or pass --force if you "
                "deliberately want groups over a partial catalog."
            )

    set_stage(conn, "group", "running")
    conn.execute("DELETE FROM dup_member")
    conn.execute("DELETE FROM dup_group")

    groups = members = 0

    def insert(signal: str, tier: str, digest: str, uuids: list[str]) -> None:
        nonlocal groups, members
        cursor = conn.execute(
            "INSERT INTO dup_group (signal, tier, digest, size) VALUES (?, ?, ?, ?)",
            (signal, tier, digest, len(uuids)),
        )
        conn.executemany(
            "INSERT INTO dup_member (group_id, uuid) VALUES (?, ?)",
            [(cursor.lastrowid, uuid) for uuid in uuids],
        )
        groups += 1
        members += len(uuids)

    for signal, column, tier in SIGNALS:
        if column is None:
            continue
        # Partition by kind and has_raw inside the key itself, so a group can
        # never span the hard partitions no matter what the hashes say.
        multiframe = ""
        if tier == "exact":
            # COALESCE because `NULL NOT IN (...)` is NULL, which would drop
            # every asset whose uti the survey never recorded.
            multiframe = f"AND COALESCE(a.uti, '') NOT IN ({_MULTIFRAME_SQL})"
        # A truncated file has honest perceptual hashes of a partial image and
        # may look like its intact twin; that belongs in review, never in an
        # exact group.
        statuses = "('ok', 'truncated')" if tier == "candidate" else "('ok')"
        rows = conn.execute(
            f"""
            SELECT h.{column} AS digest, a.kind AS kind, a.has_raw AS has_raw,
                   GROUP_CONCAT(a.uuid) AS uuids, COUNT(*) AS n
            FROM asset_hash h
            JOIN asset a ON a.uuid = h.uuid AND a.survey_status = 'ok'
            WHERE h.status IN {statuses} AND h.{column} IS NOT NULL {multiframe}
            GROUP BY h.{column}, a.kind, a.has_raw
            HAVING n > 1
            """
        ).fetchall()
        for row in rows:
            insert(signal, tier, row["digest"], row["uuids"].split(","))

    for digest, uuids in prefix_groups(conn):
        insert("prefix", "exact", digest, uuids)

    conn.commit()
    removable = members - groups
    set_stage(
        conn, "group", "complete",
        notes=f"{groups} groups, {members} members, {removable} removable",
    )
    return GroupResult(groups=groups, members=members, removable=removable)
```

In `src/photodedup/report.py`, inside `coverage`, add after `hashed_ok`:

```python
    truncated = one(
        """
        SELECT COUNT(*) AS n FROM asset_hash h
        JOIN asset a ON a.uuid = h.uuid AND a.survey_status = 'ok'
        WHERE h.status = 'truncated'
        """
    )
```

and insert `("hashed leniently (truncated)", truncated),` into the returned list right after `("hashed ok", hashed_ok),`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/grouping.py src/photodedup/report.py tests/test_grouping.py tests/test_report_groups.py
git commit -m "feat: byte-prefix exact rule links truncated files to intact twins"
```

---

### Task 4: Thumbnail path in the survey

**Files:**
- Modify: `src/photodedup/library.py`
- Modify: `src/photodedup/survey.py`
- Test: `tests/test_library.py`, `tests/test_survey.py`

**Interfaces:**
- Consumes: `AssetRecord`, `_iter_records`, `_write_asset`.
- Produces: `AssetRecord.thumb_path: str | None`; `asset.thumb_path` written by the survey.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_library.py`:

```python
def test_asset_record_thumb_path_defaults_to_none():
    assert AssetRecord(uuid="u1", kind="image").thumb_path is None


def test_iter_records_takes_the_smallest_derivative():
    from photodedup.library import _iter_records

    class Photo:
        uuid = "u1"
        ismovie = False
        filename = original_filename = "a.jpg"
        uti = "public.jpeg"
        width = height = 10
        path = "/tmp/a.jpg"
        date_added = date = None
        live_photo = has_raw = hasadjustments = favorite = hidden = intrash = False
        title = description = None
        location = None
        keywords = albums = ()
        path_derivatives = ["/lib/big.jpeg", "/lib/small.jpeg"]

    class DB:
        def photos(self, **kwargs):
            return [Photo()]

    [record] = list(_iter_records(DB()))
    assert record.thumb_path == "/lib/small.jpeg"


def test_iter_records_tolerates_derivative_lookup_failure():
    from photodedup.library import _iter_records

    class Photo:
        uuid = "u1"
        ismovie = False
        filename = original_filename = "a.jpg"
        uti = "public.jpeg"
        width = height = 10
        path = "/tmp/a.jpg"
        date_added = date = None
        live_photo = has_raw = hasadjustments = favorite = hidden = intrash = False
        title = description = None
        location = None
        keywords = albums = ()

        @property
        def path_derivatives(self):
            raise RuntimeError("no derivatives directory")

    class DB:
        def photos(self, **kwargs):
            return [Photo()]

    [record] = list(_iter_records(DB()))
    assert record.thumb_path is None
```

Append to `tests/test_survey.py`:

```python
def test_thumb_path_is_stored(conn):
    rec = AssetRecord(
        uuid="u1", kind="image", filename="a.jpg", original_filename="a.jpg",
        path="/tmp/a.jpg", date_added=datetime(2020, 1, 1), thumb_path="/lib/t.jpg",
    )
    run_survey(conn, [rec], library_path=LIBRARY, read_exif=no_exif)
    row = conn.execute("SELECT thumb_path FROM asset WHERE uuid='u1'").fetchone()
    assert row["thumb_path"] == "/lib/t.jpg"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_library.py tests/test_survey.py -q`
Expected: `AttributeError: thumb_path` / TypeError on unexpected keyword.

- [ ] **Step 3: Implement**

In `src/photodedup/library.py`, add to `AssetRecord` after `import_uuid`:

```python
    thumb_path: str | None = None
```

Add above `_iter_records`:

```python
def _thumb_path(photo: object) -> str | None:
    """The smallest derivative Photos keeps for *photo*, or None.

    osxphotos orders `path_derivatives` largest first. The lookup walks the
    library's derivatives directory and can fail for shared or syndicated
    assets; a missing thumbnail only costs the review report a placeholder,
    so failure here must never fail the survey.
    """
    try:
        derivatives = getattr(photo, "path_derivatives", None) or []
    except Exception:
        return None
    return str(derivatives[-1]) if derivatives else None
```

In `_iter_records`, add `thumb_path=_thumb_path(photo),` after `import_uuid=...`.

In `src/photodedup/survey.py`, in `_write_asset`, add `thumb_path` to the INSERT column list right after `path`, add one `?` to the VALUES list, add `record.thumb_path,` to the parameter tuple right after `record.path,`, and extend the `ON CONFLICT` clause to `survey_status = 'ok', survey_error = NULL, exif_error = excluded.exif_error, thumb_path = excluded.thumb_path`.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/library.py src/photodedup/survey.py tests/test_library.py tests/test_survey.py
git commit -m "feat: record the Photos thumbnail path per asset"
```

---

### Task 5: Import stamps and pooled date choice

**Files:**
- Modify: `src/photodedup/dates.py`
- Modify: `src/photodedup/report.py` (`exif_vs_photos_db` and its SQL)
- Test: `tests/test_dates_arbitration.py`, `tests/test_report.py`

**Interfaces:**
- Consumes: `Candidate`, `Decision`, `rejection_reason`, `choose_date`, `SOURCE_RANK`.
- Produces: `IMPORT_STAMPS: tuple[tuple[datetime, datetime], ...]`; `rejection_reason(..., stamps=IMPORT_STAMPS)` returning `"import_stamp"`; `@dataclass Pool(candidates: tuple[Candidate, ...], date_added: datetime | None, file_mtime: datetime | None)`; `choose_date_pooled(pools: Sequence[Pool], *, floor=None, now=None, margin=DEFAULT_DISAGREEMENT_MARGIN, stamps=IMPORT_STAMPS) -> Decision`; `choose_date` unchanged in signature except for a new `stamps` keyword.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dates_arbitration.py` (extend its import from `photodedup.dates` with `IMPORT_STAMPS, Pool, choose_date_pooled`):

```python
STAMP_2020 = datetime(2020, 12, 18, 9, 10, 0)


def test_photos_db_inside_an_import_stamp_is_rejected():
    why = rejection_reason(
        Candidate("photos_db", STAMP_2020, None),
        date_added=datetime(2020, 12, 18, 18, 0, 0), file_mtime=MTIME, now=NOW,
    )
    assert why == "import_stamp"


def test_exif_inside_an_import_stamp_survives():
    """One asset in the 2023 batch carries a genuine EXIF date inside the
    interval. Stamps only ever reject what Photos recorded."""
    why = rejection_reason(
        Candidate("exif", datetime(2023, 1, 14, 11, 20, 0), None),
        date_added=datetime(2023, 1, 14, 12, 0, 0), file_mtime=MTIME, now=NOW,
    )
    assert why is None


def test_photos_db_one_second_outside_a_stamp_survives():
    start, end = IMPORT_STAMPS[0]
    added = datetime(2020, 12, 31, 0, 0, 0)
    for value in (start - timedelta(seconds=1), end + timedelta(seconds=1)):
        assert rejection_reason(
            Candidate("photos_db", value, None),
            date_added=added, file_mtime=MTIME, now=NOW,
        ) is None
    for value in (start, end):
        assert rejection_reason(
            Candidate("photos_db", value, None),
            date_added=added, file_mtime=MTIME, now=NOW,
        ) == "import_stamp"


def test_stamps_can_be_switched_off():
    why = rejection_reason(
        Candidate("photos_db", STAMP_2020, None),
        date_added=datetime(2020, 12, 18, 18, 0, 0), file_mtime=MTIME, now=NOW,
        stamps=(),
    )
    assert why is None


def test_choose_date_prefers_exif_over_a_stamped_photos_date():
    decision = choose_date(
        [Candidate("photos_db", STAMP_2020, None),
         Candidate("exif", datetime(2014, 5, 12, 17, 0, 0), None)],
        date_added=datetime(2020, 12, 18, 18, 0, 0), file_mtime=MTIME, now=NOW,
    )
    assert decision.value == datetime(2014, 5, 12, 17, 0, 0)
    assert decision.source == "exif"
    assert ("photos_db", "import_stamp") in decision.rejected
    assert decision.needs_review is False


def test_pooled_choice_rescues_a_date_across_members():
    """Cohort A member has only a stamped Photos date; its twin carries EXIF."""
    stamped = Pool(
        (Candidate("photos_db", STAMP_2020, None),),
        date_added=datetime(2020, 12, 18, 18, 0, 0), file_mtime=MTIME,
    )
    twin = Pool(
        (Candidate("exif", datetime(2014, 5, 12, 17, 0, 0), None),
         Candidate("photos_db", datetime(2014, 5, 12, 17, 0, 0), None)),
        date_added=datetime(2015, 1, 1), file_mtime=MTIME,
    )
    decision = choose_date_pooled([stamped, twin], now=NOW)
    assert decision.value == datetime(2014, 5, 12, 17, 0, 0)
    assert decision.source == "exif"
    assert decision.needs_review is False


def test_pooled_choice_rejects_against_each_members_own_date_added():
    """A value after member A's date_added but before member B's survives via B."""
    value = datetime(2019, 6, 1)
    a = Pool((Candidate("photos_db", value, None),), date_added=datetime(2018, 1, 1), file_mtime=MTIME)
    b = Pool((Candidate("photos_db", value, None),), date_added=datetime(2020, 1, 1), file_mtime=MTIME)
    decision = choose_date_pooled([a, b], now=NOW)
    assert decision.value == value
    assert ("photos_db", "at_or_after_date_added") in decision.rejected


def test_pooled_choice_flags_disagreement_across_members():
    a = Pool((Candidate("exif", datetime(2014, 1, 1), None),), date_added=datetime(2020, 1, 1), file_mtime=MTIME)
    b = Pool((Candidate("exif", datetime(2016, 1, 1), None),), date_added=datetime(2020, 1, 1), file_mtime=MTIME)
    decision = choose_date_pooled([a, b], now=NOW)
    assert decision.value == datetime(2014, 1, 1)
    assert decision.needs_review is True
    assert decision.review_reason == "candidates_disagree"


def test_pooled_choice_with_no_survivors_needs_review():
    a = Pool((Candidate("photos_db", STAMP_2020, None),), date_added=datetime(2020, 12, 18, 18, 0), file_mtime=MTIME)
    decision = choose_date_pooled([a], now=NOW)
    assert decision.value is None
    assert decision.review_reason == "no_plausible_candidate"


def test_choose_date_is_the_one_pool_case():
    candidates = [Candidate("exif", datetime(2014, 1, 1), None)]
    single = choose_date(candidates, date_added=ADDED, file_mtime=MTIME, now=NOW)
    pooled = choose_date_pooled([Pool(tuple(candidates), ADDED, MTIME)], now=NOW)
    assert single == pooled
```

In `tests/test_report.py`, the existing `test_exif_vs_photos_db_covers_every_combination` and `test_cohort_a_recovery_is_visible_in_the_report` stay as they are; they are the regression net for the rewrite. Add:

```python
def test_exif_vs_photos_db_treats_a_stamped_photos_date_as_rejected(conn):
    """The Python rewrite must apply the same rules as choose_date, including
    the import stamps the old SQL could not know about."""
    conn.execute(
        """INSERT INTO asset (uuid, survey_status, date_added, file_mtime)
           VALUES ('u1', 'ok', '2020-12-18T18:00:00', '2019-01-01T00:00:00')"""
    )
    conn.execute(
        """INSERT INTO date_candidate (uuid, source, value, raw, rejected_reason)
           VALUES ('u1', 'exif', '2014-05-12T17:00:00', 'x', NULL)"""
    )
    conn.execute(
        """INSERT INTO date_candidate (uuid, source, value, raw, rejected_reason)
           VALUES ('u1', 'photos_db', '2020-12-18T09:10:00', 'x', NULL)"""
    )
    conn.commit()
    counts = dict(exif_vs_photos_db(conn))
    assert counts["exif_only_photos_rejected"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dates_arbitration.py tests/test_report.py -q`
Expected: ImportError on `IMPORT_STAMPS`.

- [ ] **Step 3: Implement**

In `src/photodedup/dates.py`, change the `dataclasses`/`typing` imports to include `from collections.abc import Sequence`. After `DEFAULT_DISAGREEMENT_MARGIN` add:

```python
# Import-time stamps. Two bulk imports into this library overwrote capture
# dates with the import clock (STATE.md, "Phase 1 result"). Each interval is
# the earliest and latest photos_date in that batch, measured from the catalog
# on 2026-09-19. Only a `photos_db` candidate is rejected by these: a genuine
# EXIF date inside the interval survives, and one asset in the 2023 batch has
# exactly that.
#
# These are a domain decision, like SENTINEL_DATES, not something derived from
# cluster size: a real hour in this library holds 218 photos, as many as the
# 2023 stamp. The survey report's capture-hour clustering is how a human finds
# stamps on another library; this constant is where they are then recorded.
IMPORT_STAMPS: tuple[tuple[datetime, datetime], ...] = (
    (datetime(2020, 12, 18, 9, 3, 42), datetime(2020, 12, 18, 9, 26, 2)),
    (datetime(2023, 1, 14, 11, 6, 19), datetime(2023, 1, 14, 11, 49, 25)),
)
```

Replace `rejection_reason` and `choose_date` with:

```python
def rejection_reason(
    candidate: Candidate,
    *,
    date_added: datetime | None,
    file_mtime: datetime | None,
    floor: datetime | None = None,
    now: datetime | None = None,
    stamps: Sequence[tuple[datetime, datetime]] = IMPORT_STAMPS,
) -> str | None:
    """Return why *candidate* cannot be trusted, or None if it survives."""
    value = candidate.value
    if value is None:
        return "unparseable"
    for sentinel, window in SENTINEL_DATES:
        if abs(value - sentinel) < window:
            return "sentinel"
    if candidate.source == "photos_db" and any(
        start <= value <= end for start, end in stamps
    ):
        return "import_stamp"
    if (now or datetime.now()) < value:
        return "future"
    # A capture always predates its own import into the library.
    if date_added is not None and value >= date_added:
        return "at_or_after_date_added"
    # A Photos database date that is exactly the file mtime is the mtime echoed
    # back, not independent evidence. This is NOT true of other sources: a
    # camera writes the file at the capture instant, so EXIF:DateTimeOriginal
    # (or a filename date) equalling FileModifyDate is the normal signature of
    # an untouched original and is corroboration, not fabrication.
    if (
        candidate.source == "photos_db"
        and file_mtime is not None
        and value == file_mtime
    ):
        return "equals_file_mtime"
    if floor is not None and value < floor:
        return "below_floor"
    return None


@dataclass(frozen=True)
class Pool:
    """One asset's candidates with the context its rejection rules need.

    Rejection is per asset: `date_added` differs between the two members of a
    cohort A / cohort B pair, and that difference is the whole reason one of
    them can rescue the other's date.
    """

    candidates: tuple[Candidate, ...]
    date_added: datetime | None
    file_mtime: datetime | None


def choose_date_pooled(
    pools: Sequence[Pool],
    *,
    floor: datetime | None = None,
    now: datetime | None = None,
    margin: timedelta = DEFAULT_DISAGREEMENT_MARGIN,
    stamps: Sequence[tuple[datetime, datetime]] = IMPORT_STAMPS,
) -> Decision:
    """Pick one capture date across every pool: earliest survivor, source rank
    breaking ties. This is the single date authority; nothing else may
    re-express its rules."""
    survivors: list[Candidate] = []
    rejected: list[tuple[str, str]] = []
    for pool in pools:
        for candidate in pool.candidates:
            why = rejection_reason(
                candidate,
                date_added=pool.date_added,
                file_mtime=pool.file_mtime,
                floor=floor,
                now=now,
                stamps=stamps,
            )
            if why is None:
                survivors.append(candidate)
            else:
                rejected.append((candidate.source, why))

    if not survivors:
        return Decision(None, None, tuple(rejected), True, "no_plausible_candidate")

    winner = min(
        survivors,
        key=lambda c: (c.value, SOURCE_RANK.get(c.source, len(SOURCE_RANK))),
    )
    spread = max(c.value for c in survivors) - min(c.value for c in survivors)
    disagree = spread > margin
    return Decision(
        winner.value,
        winner.source,
        tuple(rejected),
        disagree,
        "candidates_disagree" if disagree else None,
    )


def choose_date(
    candidates: list[Candidate],
    *,
    date_added: datetime | None,
    file_mtime: datetime | None,
    floor: datetime | None = None,
    now: datetime | None = None,
    margin: timedelta = DEFAULT_DISAGREEMENT_MARGIN,
    stamps: Sequence[tuple[datetime, datetime]] = IMPORT_STAMPS,
) -> Decision:
    """Pick the winning capture date for a single asset."""
    return choose_date_pooled(
        [Pool(tuple(candidates), date_added, file_mtime)],
        floor=floor, now=now, margin=margin, stamps=stamps,
    )
```

In `src/photodedup/report.py`, delete `_EXIF_VS_PHOTOS_SQL` entirely and replace `exif_vs_photos_db` with:

```python
def exif_vs_photos_db(conn: Connection) -> list[tuple[str, int]]:
    """Cross-tabulate the EXIF date against the Photos database date per asset.

    Marginal counts cannot answer the question this project exists to answer.
    The cohort A recovery case is precisely the one where the Photos date is
    REJECTED and the EXIF date survives: measured over survivors alone it looks
    like a single-candidate asset in perfect agreement with itself. This pivot
    re-evaluates each candidate through `rejection_reason`, the one authority,
    so the stored `rejected_reason` column is never trusted over the rules.

    Returns one (bucket_key, count) pair for every combination, in a fixed
    order, including zero rows.
    """
    rows = conn.execute(
        """
        SELECT a.uuid AS uuid, a.date_added AS added, a.file_mtime AS mtime,
               c.source AS source, c.value AS value
        FROM asset a
        LEFT JOIN date_candidate c
            ON c.uuid = a.uuid AND c.source IN ('exif', 'photos_db')
        WHERE a.survey_status = 'ok'
        ORDER BY a.uuid
        """
    ).fetchall()

    per_asset: dict[str, dict] = {}
    for row in rows:
        entry = per_asset.setdefault(
            row["uuid"],
            {"added": _parse(row["added"]), "mtime": _parse(row["mtime"]), "exif": None, "photos_db": None},
        )
        if row["source"] is not None:
            entry[row["source"]] = _parse(row["value"])

    counts: dict[str, int] = {}
    for entry in per_asset.values():
        usable = {}
        for source in ("exif", "photos_db"):
            value = entry[source]
            if value is None:
                continue
            why = rejection_reason(
                Candidate(source, value, None),
                date_added=entry["added"], file_mtime=entry["mtime"],
            )
            if why is None:
                usable[source] = value
        if "exif" in usable and "photos_db" in usable:
            gap = abs((usable["exif"] - usable["photos_db"]).total_seconds())
            if gap == 0:
                bucket = "both_equal"
            elif gap <= _MARGIN_SECONDS:
                bucket = "both_close"
            else:
                bucket = "both_far"
        elif "exif" in usable:
            bucket = "exif_only_photos_rejected"
        elif "photos_db" in usable:
            bucket = "photos_only"
        else:
            bucket = "neither"
        counts[bucket] = counts.get(bucket, 0) + 1
    return [(key, counts.get(key, 0)) for key, _label in _EXIF_VS_PHOTOS_LABELS]


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)
```

Add `from datetime import datetime` and `from photodedup.dates import DEFAULT_DISAGREEMENT_MARGIN, Candidate, rejection_reason` to `report.py`'s imports.

Note the old SQL treated an asset whose `exif` value was present but whose stored `rejected_reason` was non-null as rejected; the rewrite recomputes instead. If `test_exif_vs_photos_db_covers_every_combination` inserted rows with a stored reason but a value the rules would accept, update that test's fixture to use values the rules reject (a sentinel date, or one after `date_added`), because the stored column is no longer authoritative. Keep the assertion counts identical.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass. `tests/test_survey.py::test_import_time_date_is_recorded_but_rejected` may now see `import_stamp` instead of `at_or_after_date_added` if its fixture date falls inside the 2020 interval; if so, assert the reason is one of the two, since either is a correct rejection.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/dates.py src/photodedup/report.py tests/test_dates_arbitration.py tests/test_report.py tests/test_survey.py
git commit -m "feat: import-stamp sentinel and pooled date choice as the single authority"
```

---

### Task 6: Sets

**Files:**
- Create: `src/photodedup/sets.py`
- Modify: `src/photodedup/grouping.py` (`GroupResult`, end of `run_group`)
- Modify: `src/photodedup/report.py` (`exact_components`)
- Test: `tests/test_sets.py`, `tests/test_report_groups.py`

**Interfaces:**
- Consumes: `dup_group`, `dup_member` from Task 3; `dup_set`, `dup_set_member` from Task 1.
- Produces: `run_sets(conn) -> SetResult` with fields `exact_sets, exact_assets, candidate_sets, candidate_assets`; `class SetsInconsistent(RuntimeError)`; `GroupResult.exact_sets: int`, `GroupResult.candidate_sets: int`; `exact_components` reading `dup_set`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sets.py`:

```python
import pytest

from photodedup.catalog import open_catalog, set_stage
from photodedup.grouping import run_group
from photodedup.sets import SetsInconsistent, run_sets


@pytest.fixture
def conn(tmp_path):
    conn = open_catalog(tmp_path / "catalog.sqlite")
    set_stage(conn, "hash", "complete")
    return conn


def add(conn, uuid, *, kind="image", scan=None, pixel=None, dhash=None, status="ok"):
    conn.execute(
        "INSERT INTO asset (uuid, kind, has_raw, uti, survey_status) VALUES (?, ?, 0, 'public.jpeg', 'ok')",
        (uuid, kind),
    )
    conn.execute(
        "INSERT INTO asset_hash (uuid, status, scan_hash, pixel_hash, dhash) VALUES (?, ?, ?, ?, ?)",
        (uuid, status, scan, pixel, dhash),
    )
    conn.commit()


def sets(conn, tier=None):
    sql = "SELECT * FROM dup_set"
    if tier:
        sql += f" WHERE tier = '{tier}'"
    return conn.execute(sql + " ORDER BY id").fetchall()


def members(conn, set_id):
    return {
        r["uuid"]: r["signals"]
        for r in conn.execute("SELECT uuid, signals FROM dup_set_member WHERE set_id = ?", (set_id,))
    }


def test_pair_found_by_two_signals_is_one_exact_set(conn):
    add(conn, "u1", scan="s", pixel="p")
    add(conn, "u2", scan="s", pixel="p")
    run_group(conn)
    result = run_sets(conn)
    assert (result.exact_sets, result.exact_assets) == (1, 2)
    [s] = sets(conn)
    assert s["tier"] == "exact" and s["kind"] == "image" and s["size"] == 2
    assert members(conn, s["id"]) == {"u1": "pixel,scan", "u2": "pixel,scan"}


def test_chained_groups_merge_into_one_set(conn):
    add(conn, "u1", scan="s")
    add(conn, "u2", scan="s", pixel="p")
    add(conn, "u3", pixel="p")
    run_group(conn)
    result = run_sets(conn)
    assert result.exact_sets == 1 and result.exact_assets == 3
    assert members(conn, sets(conn)[0]["id"]) == {"u1": "scan", "u2": "pixel,scan", "u3": "pixel"}


def test_dhash_only_pair_is_a_candidate_set(conn):
    add(conn, "u1", dhash="d")
    add(conn, "u2", dhash="d")
    run_group(conn)
    result = run_sets(conn)
    assert result.exact_sets == 0
    assert (result.candidate_sets, result.candidate_assets) == (1, 2)
    [s] = sets(conn)
    assert s["tier"] == "candidate"
    assert members(conn, s["id"]) == {"u1": "dhash", "u2": "dhash"}


def test_dhash_linking_two_exact_sets_keeps_them_separate_and_adds_a_candidate(conn):
    add(conn, "a1", scan="sa", dhash="d")
    add(conn, "a2", scan="sa")
    add(conn, "b1", scan="sb", dhash="d")
    add(conn, "b2", scan="sb")
    run_group(conn)
    result = run_sets(conn)
    assert result.exact_sets == 2 and result.exact_assets == 4
    assert result.candidate_sets == 1 and result.candidate_assets == 4
    [cand] = sets(conn, "candidate")
    assert members(conn, cand["id"]) == {
        "a1": "dhash", "a2": "via_exact", "b1": "dhash", "b2": "via_exact",
    }


def test_dhash_entirely_inside_one_exact_set_adds_nothing(conn):
    add(conn, "u1", scan="s", dhash="d")
    add(conn, "u2", scan="s", dhash="d")
    run_group(conn)
    result = run_sets(conn)
    assert result.exact_sets == 1
    assert result.candidate_sets == 0


def test_standalone_asset_joins_a_candidate_set_through_an_exact_member(conn):
    add(conn, "a1", scan="sa", dhash="d")
    add(conn, "a2", scan="sa")
    add(conn, "x", dhash="d")
    run_group(conn)
    result = run_sets(conn)
    assert result.exact_sets == 1
    assert result.candidate_sets == 1 and result.candidate_assets == 3


def test_rerun_is_idempotent(conn):
    add(conn, "u1", scan="s")
    add(conn, "u2", scan="s")
    run_group(conn)
    run_sets(conn)
    run_sets(conn)
    assert len(sets(conn)) == 1


def test_run_group_runs_sets(conn):
    add(conn, "u1", scan="s")
    add(conn, "u2", scan="s")
    result = run_group(conn)
    assert result.exact_sets == 1
    assert len(sets(conn)) == 1


def test_mixed_kinds_in_one_group_trip_the_wire(conn):
    """Grouping keys on kind so this cannot happen through run_group. If it
    ever does, sets must refuse rather than write a set that mixes a video
    with a still."""
    add(conn, "u1", kind="image")
    add(conn, "u2", kind="video")
    conn.execute("INSERT INTO dup_group (id, signal, tier, digest, size) VALUES (1, 'scan', 'exact', 'x', 2)")
    conn.execute("INSERT INTO dup_member VALUES (1, 'u1')")
    conn.execute("INSERT INTO dup_member VALUES (1, 'u2')")
    conn.commit()
    with pytest.raises(SetsInconsistent):
        run_sets(conn)
    assert sets(conn) == []
```

Append to `tests/test_report_groups.py`:

```python
def test_exact_components_reads_sets(conn):
    add(conn, "a1", scan="sa", dhash="d")
    add(conn, "a2", scan="sa")
    add(conn, "b1", scan="sb", dhash="d")
    add(conn, "b2", scan="sb")
    add(conn, "x", dhash="d")
    run_group(conn)
    assert exact_components(conn) == (2, 4, 2)
    assert exact_components(conn, all_tiers=True) == (1, 5, 4)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sets.py tests/test_report_groups.py -q`
Expected: ModuleNotFoundError on `photodedup.sets`.

- [ ] **Step 3: Implement**

Create `src/photodedup/sets.py`:

```python
"""Merge per-signal duplicate groups into sets. One exact set per asset."""

from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection


class SetsInconsistent(RuntimeError):
    """A set would have crossed a hard partition. Nothing was written."""


@dataclass(frozen=True)
class SetResult:
    exact_sets: int
    exact_assets: int
    candidate_sets: int
    candidate_assets: int


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic: the lexically smaller root wins, so re-runs
            # produce identical set membership order.
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra

    def components(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for x in sorted(self.parent):
            out.setdefault(self.find(x), []).append(x)
        return out


def run_sets(conn: Connection) -> SetResult:
    """Rebuild `dup_set` and `dup_set_member` from the groups. Idempotent.

    Exact sets are the connected components of the exact-signal groups. A
    candidate set is a connected component of the candidate-signal groups
    computed over exact sets rather than over assets, so it records "these
    exact sets (and these loose assets) look alike" without ever merging
    exact sets that no exact signal links.
    """
    rows = conn.execute(
        """
        SELECT m.group_id AS gid, g.signal AS signal, g.tier AS tier,
               m.uuid AS uuid, a.kind AS kind
        FROM dup_member m
        JOIN dup_group g ON g.id = m.group_id
        JOIN asset a ON a.uuid = m.uuid
        ORDER BY g.tier, m.group_id, m.uuid
        """
    ).fetchall()

    kind: dict[str, str] = {}
    exact_groups: dict[int, list[str]] = {}
    cand_groups: dict[int, list[str]] = {}
    exact_signals: dict[str, set[str]] = {}
    cand_signals: dict[str, set[str]] = {}
    for r in rows:
        kind[r["uuid"]] = r["kind"]
        if r["tier"] == "exact":
            exact_groups.setdefault(r["gid"], []).append(r["uuid"])
            exact_signals.setdefault(r["uuid"], set()).add(r["signal"])
        else:
            cand_groups.setdefault(r["gid"], []).append(r["uuid"])
            cand_signals.setdefault(r["uuid"], set()).add(r["signal"])

    exact = _UnionFind()
    for uuids in exact_groups.values():
        for u in uuids:
            exact.add(u)
        for other in uuids[1:]:
            exact.union(uuids[0], other)
    exact_components = exact.components()

    def node(uuid: str) -> str:
        return exact.find(uuid) if uuid in exact.parent else uuid

    cand = _UnionFind()
    for uuids in cand_groups.values():
        nodes = [node(u) for u in uuids]
        for n in nodes:
            cand.add(n)
        for other in nodes[1:]:
            cand.union(nodes[0], other)

    def check_one_kind(uuids: list[str]) -> str:
        kinds = {kind[u] for u in uuids}
        if len(kinds) != 1:
            raise SetsInconsistent(
                f"set {sorted(uuids)} mixes kinds {sorted(kinds)}; a group crossed "
                "a hard partition, which grouping is supposed to make impossible"
            )
        return kinds.pop()

    # Validate everything before writing anything.
    plan_exact = [(check_one_kind(us), us) for us in exact_components.values()]
    plan_cand = []
    for nodes in cand.components().values():
        if len(nodes) < 2:
            continue  # every member already shares one exact set: nothing new
        uuids: list[str] = []
        for n in nodes:
            uuids.extend(exact_components.get(n, [n]))
        uuids = sorted(set(uuids))
        plan_cand.append((check_one_kind(uuids), uuids))

    conn.execute("DELETE FROM dup_set_member")
    conn.execute("DELETE FROM dup_set")

    exact_assets = cand_assets = 0
    for k, uuids in plan_exact:
        cursor = conn.execute(
            "INSERT INTO dup_set (tier, kind, size) VALUES ('exact', ?, ?)", (k, len(uuids))
        )
        conn.executemany(
            "INSERT INTO dup_set_member (set_id, uuid, tier, signals) VALUES (?, ?, 'exact', ?)",
            [(cursor.lastrowid, u, ",".join(sorted(exact_signals[u]))) for u in uuids],
        )
        exact_assets += len(uuids)
    for k, uuids in plan_cand:
        cursor = conn.execute(
            "INSERT INTO dup_set (tier, kind, size) VALUES ('candidate', ?, ?)", (k, len(uuids))
        )
        conn.executemany(
            "INSERT INTO dup_set_member (set_id, uuid, tier, signals) VALUES (?, ?, 'candidate', ?)",
            [
                (cursor.lastrowid, u, ",".join(sorted(cand_signals[u])) if u in cand_signals else "via_exact")
                for u in uuids
            ],
        )
        cand_assets += len(uuids)
    conn.commit()
    return SetResult(len(plan_exact), exact_assets, len(plan_cand), cand_assets)
```

In `src/photodedup/grouping.py`, add `from photodedup.sets import run_sets` to the imports, change `GroupResult` to:

```python
@dataclass(frozen=True)
class GroupResult:
    groups: int
    members: int
    removable: int
    exact_sets: int = 0
    candidate_sets: int = 0
```

and replace the tail of `run_group` from `conn.commit()` onward with:

```python
    conn.commit()
    sets = run_sets(conn)
    removable = members - groups
    set_stage(
        conn, "group", "complete",
        notes=(
            f"{groups} groups, {members} members, {removable} removable; "
            f"{sets.exact_sets} exact sets over {sets.exact_assets} assets, "
            f"{sets.candidate_sets} candidate sets"
        ),
    )
    return GroupResult(
        groups=groups, members=members, removable=removable,
        exact_sets=sets.exact_sets, candidate_sets=sets.candidate_sets,
    )
```

In `src/photodedup/report.py`, replace `exact_components` with:

```python
def exact_components(conn: Connection, *, all_tiers: bool = False) -> tuple[int, int, int]:
    """(sets, assets, removable) over the exact sets, where removable = assets - sets.

    With *all_tiers* the candidate sets join in: a candidate set replaces the
    exact sets it touches, which answers "how many assets are implicated in
    total" rather than "how many are safe to resolve".
    """
    one = lambda sql: conn.execute(sql).fetchone()["n"]
    exact_sets = one("SELECT COUNT(*) AS n FROM dup_set WHERE tier = 'exact'")
    if not all_tiers:
        assets = one("SELECT COALESCE(SUM(size), 0) AS n FROM dup_set WHERE tier = 'exact'")
        return (exact_sets, assets, assets - exact_sets)
    touched = one(
        """
        SELECT COUNT(DISTINCT e.set_id) AS n
        FROM dup_set_member e
        JOIN dup_set_member c ON c.uuid = e.uuid AND c.tier = 'candidate'
        WHERE e.tier = 'exact'
        """
    )
    cand_sets = one("SELECT COUNT(*) AS n FROM dup_set WHERE tier = 'candidate'")
    assets = one("SELECT COUNT(DISTINCT uuid) AS n FROM dup_set_member")
    components = exact_sets - touched + cand_sets
    return (components, assets, assets - components)
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass. The `sets` module must not import `grouping` (it would be circular); it imports nothing from the package.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/sets.py src/photodedup/grouping.py src/photodedup/report.py tests/test_sets.py tests/test_report_groups.py
git commit -m "feat: merge per-signal groups into exact and candidate sets"
```

---

### Task 7: Arbitrate

**Files:**
- Create: `src/photodedup/arbitrate.py`
- Test: `tests/test_arbitrate.py`

**Interfaces:**
- Consumes: `dup_set`, `dup_set_member`, `decision`, `decision_loser`, `require_stage_complete`, `StageIncomplete` (Task 1); `Pool`, `Candidate`, `choose_date_pooled`, `DEFAULT_DISAGREEMENT_MARGIN` (Task 5); `MULTIFRAME_UTIS` (grouping).
- Produces: `KEEPER_RULES`; `@dataclass Member`; `rank_key(m: Member) -> tuple`; `choose_keeper(members: Sequence[Member]) -> tuple[Member, str, list[tuple[Member, str]]]`; `review_flags(tier, members, keeper, decision) -> list[str]`; `carries(keeper, loser) -> dict`; `run_arbitrate(conn, *, margin=DEFAULT_DISAGREEMENT_MARGIN, force=False, now=None) -> ArbitrateResult(sets, resolvable, review, dates_rescued)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_arbitrate.py`:

```python
import itertools
import json
from datetime import datetime

import pytest

from photodedup.arbitrate import (
    KEEPER_RULES,
    Member,
    carries,
    choose_keeper,
    run_arbitrate,
)
from photodedup.catalog import StageIncomplete, open_catalog, set_stage

NOW = datetime(2026, 9, 19, 12, 0, 0)


def member(uuid, **overrides):
    base = dict(
        uuid=uuid, hash_status="ok", file_size=1000, is_favorite=False,
        has_adjustments=False, title=None, description=None, albums=(), keywords=(),
        date_added="2020-01-01T00:00:00", photos_date="2014-05-12T17:00:00",
        file_mtime=None, uti="public.jpeg", original_filename="IMG_0001.jpg",
    )
    base.update(overrides)
    return Member(**base)


def test_rule_names_are_fixed():
    assert KEEPER_RULES == (
        "intact", "file_size", "favorite", "adjustments", "caption", "albums",
        "date_added", "uuid",
    )


def test_intact_beats_truncated_no_matter_what():
    """The truncated member is bigger, favourited, edited, captioned, in more
    albums and added earlier. It still loses."""
    trunc = member("a", hash_status="truncated", file_size=9999, is_favorite=True,
                   has_adjustments=True, title="t", albums=("x", "y"),
                   date_added="2000-01-01T00:00:00")
    intact = member("b")
    keeper, reason, losers = choose_keeper([trunc, intact])
    assert keeper.uuid == "b"
    assert reason == "intact"
    assert losers == [(trunc, "intact")]


def test_bigger_file_wins():
    keeper, reason, _ = choose_keeper([member("a", file_size=1), member("b", file_size=2)])
    assert keeper.uuid == "b" and reason == "file_size"


def test_favorite_breaks_a_size_tie():
    keeper, reason, _ = choose_keeper([member("a"), member("b", is_favorite=True)])
    assert keeper.uuid == "b" and reason == "favorite"


def test_adjustments_then_caption_then_albums():
    k, r, _ = choose_keeper([member("a"), member("b", has_adjustments=True)])
    assert (k.uuid, r) == ("b", "adjustments")
    k, r, _ = choose_keeper([member("a"), member("b", description="x")])
    assert (k.uuid, r) == ("b", "caption")
    k, r, _ = choose_keeper([member("a", albums=("one",)), member("b", albums=("one", "two"))])
    assert (k.uuid, r) == ("b", "albums")


def test_whitespace_caption_does_not_count():
    k, r, _ = choose_keeper([member("a", title="   "), member("b")])
    assert r == "uuid" and k.uuid == "a"


def test_earlier_date_added_wins():
    k, r, _ = choose_keeper([
        member("a", date_added="2021-01-01T00:00:00"),
        member("b", date_added="2020-01-01T00:00:00"),
    ])
    assert (k.uuid, r) == ("b", "date_added")


def test_missing_date_added_loses_to_any_date():
    k, r, _ = choose_keeper([member("a", date_added=None), member("b")])
    assert (k.uuid, r) == ("b", "date_added")


def test_lowest_uuid_is_the_final_tie_break():
    k, r, _ = choose_keeper([member("zzz"), member("aaa")])
    assert (k.uuid, r) == ("aaa", "uuid")


def test_each_loser_records_the_rule_that_beat_it():
    a = member("a", file_size=5)
    b = member("b", file_size=5, is_favorite=True)
    c = member("c", file_size=1)
    keeper, reason, losers = choose_keeper([a, b, c])
    assert keeper.uuid == "b" and reason == "favorite"
    assert dict((m.uuid, why) for m, why in losers) == {"a": "favorite", "c": "file_size"}


def test_input_order_never_changes_the_winner():
    ms = [member("a", file_size=3), member("b", file_size=3, has_adjustments=True),
          member("c", file_size=9, hash_status="truncated"), member("d", file_size=3)]
    winners = {choose_keeper(list(p))[0].uuid for p in itertools.permutations(ms)}
    assert winners == {"b"}


def test_keeper_is_never_smaller_than_an_intact_loser():
    ms = [member(str(i), file_size=s) for i, s in enumerate((5, 9, 2, 9, 7))]
    keeper, _, losers = choose_keeper(ms)
    assert all(keeper.file_size >= m.file_size for m, _ in losers)


def test_carries_reports_what_the_keeper_lacks():
    keeper = member("k", albums=("A",), keywords=("x",), title=None, description=None)
    loser = member("l", albums=("A", "B"), keywords=("x", "y"), is_favorite=True,
                   title="Beach", description="IMG_0001")
    got = carries(keeper, loser)
    assert got == {
        "albums": ["B"], "keywords": ["y"], "favorite": True, "title": "Beach",
    }


def test_carries_ignores_filename_echoes_and_shorter_captions():
    keeper = member("k", title="A long title")
    loser = member("l", title="Short", description="IMG_0001.jpg")
    assert carries(keeper, loser) == {}


def test_carries_is_empty_when_nothing_is_gained():
    assert carries(member("k", albums=("A",)), member("l", albums=("A",))) == {}


# --- end to end over a small catalog ---------------------------------------


@pytest.fixture
def conn(tmp_path):
    conn = open_catalog(tmp_path / "catalog.sqlite")
    set_stage(conn, "group", "complete")
    return conn


def add_asset(conn, uuid, *, size=1000, added="2020-01-01T00:00:00",
              photos_date="2014-05-12T17:00:00", uti="public.jpeg",
              hash_status="ok", favorite=0, albums=(), exif=None, title=None):
    conn.execute(
        """INSERT INTO asset (uuid, kind, uti, file_size, date_added, photos_date,
                              is_favorite, albums, keywords, title, survey_status,
                              original_filename)
           VALUES (?, 'image', ?, ?, ?, ?, ?, ?, '[]', ?, 'ok', 'IMG_0001.jpg')""",
        (uuid, uti, size, added, photos_date, favorite, json.dumps(list(albums)), title),
    )
    conn.execute("INSERT INTO asset_hash (uuid, status) VALUES (?, ?)", (uuid, hash_status))
    conn.execute(
        "INSERT INTO date_candidate (uuid, source, value) VALUES (?, 'photos_db', ?)",
        (uuid, photos_date),
    )
    if exif:
        conn.execute(
            "INSERT INTO date_candidate (uuid, source, value) VALUES (?, 'exif', ?)",
            (uuid, exif),
        )
    conn.commit()


def add_set(conn, set_id, uuids, *, tier="exact"):
    conn.execute(
        "INSERT INTO dup_set (id, tier, kind, size) VALUES (?, ?, 'image', ?)",
        (set_id, tier, len(uuids)),
    )
    for u in uuids:
        conn.execute(
            "INSERT INTO dup_set_member (set_id, uuid, tier, signals) VALUES (?, ?, ?, 'scan')",
            (set_id, u, tier),
        )
    conn.commit()


def decision(conn, set_id):
    return conn.execute("SELECT * FROM decision WHERE set_id = ?", (set_id,)).fetchone()


def test_refuses_before_group_is_complete(tmp_path):
    fresh = open_catalog(tmp_path / "fresh.sqlite")
    with pytest.raises(StageIncomplete):
        run_arbitrate(fresh)


def test_exact_set_without_flags_is_resolvable(conn):
    add_asset(conn, "a", size=10)
    add_asset(conn, "b", size=20)
    add_set(conn, 1, ["a", "b"])
    result = run_arbitrate(conn, now=NOW)
    assert (result.sets, result.resolvable, result.review) == (1, 1, 0)
    d = decision(conn, 1)
    assert d["keeper_uuid"] == "b" and d["keeper_reason"] == "file_size"
    assert d["resolvable"] == 1 and d["review_flags"] == ""
    loser = conn.execute("SELECT * FROM decision_loser WHERE set_id = 1").fetchone()
    assert loser["uuid"] == "a" and loser["lost_on"] == "file_size"
    assert json.loads(loser["carries"]) == {}


def test_date_is_rescued_from_the_twin(conn):
    """Cohort A keeper with a stamped Photos date; the loser carries EXIF."""
    add_asset(conn, "a", size=20, photos_date="2020-12-18T09:10:00", added="2020-12-18T18:00:00")
    add_asset(conn, "b", size=10, photos_date="2014-05-12T17:00:00", exif="2014-05-12T17:00:00")
    add_set(conn, 1, ["a", "b"])
    result = run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert d["keeper_uuid"] == "a"
    assert d["date_value"] == "2014-05-12T17:00:00" and d["date_source"] == "exif"
    assert d["date_changes"] == 1
    assert ["photos_db", "import_stamp"] in json.loads(d["date_rejected"])
    assert result.dates_rescued == 1
    assert d["resolvable"] == 1


def test_disagreeing_dates_flag_but_do_not_block(conn):
    add_asset(conn, "a", size=20, exif="2014-01-01T00:00:00")
    add_asset(conn, "b", size=10, exif="2016-01-01T00:00:00")
    add_set(conn, 1, ["a", "b"])
    run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert "dates_disagree" in d["review_flags"].split(",")
    assert d["resolvable"] == 1


def test_candidate_tier_is_never_resolvable(conn):
    add_asset(conn, "a")
    add_asset(conn, "b")
    add_set(conn, 1, ["a", "b"], tier="candidate")
    result = run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert d["resolvable"] == 0 and "candidate_tier" in d["review_flags"]
    assert result.review == 1


def test_png_multiframe_and_truncated_flags_block(conn):
    add_asset(conn, "a", uti="public.png")
    add_asset(conn, "b", uti="public.png")
    add_set(conn, 1, ["a", "b"])
    add_asset(conn, "c", uti="com.compuserve.gif")
    add_asset(conn, "d", uti="com.compuserve.gif")
    add_set(conn, 2, ["c", "d"])
    add_asset(conn, "e", size=5)
    add_asset(conn, "f", size=50, hash_status="truncated")
    add_set(conn, 3, ["e", "f"])
    run_arbitrate(conn, now=NOW)
    assert decision(conn, 1)["review_flags"] == "png"
    assert decision(conn, 2)["review_flags"] == "multiframe"
    d3 = decision(conn, 3)
    assert d3["review_flags"] == "truncated_member" and d3["keeper_uuid"] == "e"
    assert all(decision(conn, i)["resolvable"] == 0 for i in (1, 2, 3))


def test_size_spread_flags_but_does_not_block(conn):
    add_asset(conn, "a", size=3_000_000)
    add_asset(conn, "b", size=1_000_000)
    add_set(conn, 1, ["a", "b"])
    run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert d["review_flags"] == "size_spread" and d["resolvable"] == 1
    assert d["keeper_uuid"] == "a"


def test_no_plausible_date_blocks(conn):
    add_asset(conn, "a", photos_date="1970-01-01T00:00:00")
    add_asset(conn, "b", photos_date="1970-01-01T00:00:00")
    add_set(conn, 1, ["a", "b"])
    run_arbitrate(conn, now=NOW)
    d = decision(conn, 1)
    assert d["date_value"] is None
    assert "no_plausible_date" in d["review_flags"] and d["resolvable"] == 0


def test_loser_carries_albums_and_favorite(conn):
    add_asset(conn, "a", size=20, albums=("Trips",))
    add_asset(conn, "b", size=10, albums=("Trips", "Best"), favorite=1, title="Beach")
    add_set(conn, 1, ["a", "b"])
    run_arbitrate(conn, now=NOW)
    loser = conn.execute("SELECT carries FROM decision_loser WHERE set_id = 1").fetchone()
    assert json.loads(loser["carries"]) == {"albums": ["Best"], "favorite": True, "title": "Beach"}


def test_rerun_replaces_decisions(conn):
    add_asset(conn, "a", size=10)
    add_asset(conn, "b", size=20)
    add_set(conn, 1, ["a", "b"])
    run_arbitrate(conn, now=NOW)
    run_arbitrate(conn, now=NOW)
    assert conn.execute("SELECT COUNT(*) AS n FROM decision").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM decision_loser").fetchone()["n"] == 1


def test_empty_catalog_completes_with_zero(conn):
    result = run_arbitrate(conn, now=NOW)
    assert result.sets == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_arbitrate.py -q`
Expected: ModuleNotFoundError on `photodedup.arbitrate`.

- [ ] **Step 3: Implement**

Create `src/photodedup/arbitrate.py`:

```python
"""Decide, per duplicate set, the keeper and the capture date.

Pure catalog-in, catalog-out. Re-running with changed rules costs nothing,
which is the point: every rule here can be revised after the user samples
the review report, without touching a photo.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePath
from sqlite3 import Connection

from photodedup.catalog import require_stage_complete, set_stage
from photodedup.dates import (
    DEFAULT_DISAGREEMENT_MARGIN,
    Candidate,
    Decision,
    Pool,
    choose_date_pooled,
)
from photodedup.grouping import MULTIFRAME_UTIS

# Tie-break order, highest priority first. Each name is the value written to
# `decision.keeper_reason` and `decision_loser.lost_on`.
KEEPER_RULES: tuple[str, ...] = (
    "intact", "file_size", "favorite", "adjustments", "caption", "albums",
    "date_added", "uuid",
)

SIZE_SPREAD_BYTES = 1_000_000

# Flags that describe a rule doing its job rather than a doubt about the
# decision. They are surfaced for sampling and do not block resolvability.
_INFORMATIONAL_FLAGS = frozenset({"size_spread", "dates_disagree"})


@dataclass(frozen=True)
class Member:
    uuid: str
    hash_status: str
    file_size: int | None
    is_favorite: bool
    has_adjustments: bool
    title: str | None
    description: str | None
    albums: tuple[str, ...]
    keywords: tuple[str, ...]
    date_added: str | None
    photos_date: str | None
    file_mtime: str | None
    uti: str | None
    original_filename: str | None


@dataclass(frozen=True)
class ArbitrateResult:
    sets: int
    resolvable: int
    review: int
    dates_rescued: int


def _has_caption(m: Member) -> bool:
    return bool((m.title or "").strip() or (m.description or "").strip())


def _added_ts(value: str | None) -> float:
    return math.inf if value is None else datetime.fromisoformat(value).timestamp()


def rank_key(m: Member) -> tuple:
    """Sort key: ascending order puts the keeper first. One component per
    entry of KEEPER_RULES, in the same order."""
    return (
        1 if m.hash_status == "truncated" else 0,
        -(m.file_size or 0),
        0 if m.is_favorite else 1,
        0 if m.has_adjustments else 1,
        0 if _has_caption(m) else 1,
        -len(m.albums),
        _added_ts(m.date_added),
        m.uuid,
    )


def _separating_rule(a: tuple, b: tuple) -> str:
    for name, x, y in zip(KEEPER_RULES, a, b):
        if x != y:
            return name
    return KEEPER_RULES[-1]


def choose_keeper(
    members: Sequence[Member],
) -> tuple[Member, str, list[tuple[Member, str]]]:
    """Return (keeper, rule that decided it, [(loser, rule it lost on), ...])."""
    if not members:
        raise ValueError("a set has no members")
    ranked = sorted(members, key=rank_key)
    keeper = ranked[0]
    keeper_key = rank_key(keeper)
    losers = [(m, _separating_rule(keeper_key, rank_key(m))) for m in ranked[1:]]
    reason = losers[0][1] if losers else KEEPER_RULES[-1]
    return keeper, reason, losers


def _is_filename_echo(value: str, m: Member) -> bool:
    name = m.original_filename or ""
    stem = PurePath(name).stem
    return value.strip().lower() in {name.lower(), stem.lower()} if name else False


def _caption_gain(keeper: Member, loser: Member, field: str) -> str | None:
    theirs = (getattr(loser, field) or "").strip()
    if not theirs or _is_filename_echo(theirs, loser):
        return None
    ours = (getattr(keeper, field) or "").strip()
    if _is_filename_echo(ours, keeper):
        ours = ""
    return theirs if len(theirs) > len(ours) else None


def carries(keeper: Member, loser: Member) -> dict:
    """What *loser* has that *keeper* lacks. Phase 4 applies it; the report
    shows it so nothing is silently dropped."""
    out: dict = {}
    albums = sorted(set(loser.albums) - set(keeper.albums))
    if albums:
        out["albums"] = albums
    keywords = sorted(set(loser.keywords) - set(keeper.keywords))
    if keywords:
        out["keywords"] = keywords
    if loser.is_favorite and not keeper.is_favorite:
        out["favorite"] = True
    for field in ("title", "description"):
        gained = _caption_gain(keeper, loser, field)
        if gained is not None:
            out[field] = gained
    return out


def review_flags(
    tier: str, members: Sequence[Member], keeper: Member, decision: Decision
) -> list[str]:
    flags: list[str] = []
    if tier == "candidate":
        flags.append("candidate_tier")
    if decision.review_reason == "candidates_disagree":
        flags.append("dates_disagree")
    if decision.review_reason == "no_plausible_candidate":
        flags.append("no_plausible_date")
    if any(m.uti == "public.png" for m in members):
        flags.append("png")
    if any(m.uti in MULTIFRAME_UTIS for m in members):
        flags.append("multiframe")
    if any(m.hash_status == "truncated" for m in members):
        flags.append("truncated_member")
    intact_sizes = [m.file_size for m in members if m.hash_status != "truncated" and m.file_size is not None]
    if len(intact_sizes) > 1 and max(intact_sizes) - min(intact_sizes) >= SIZE_SPREAD_BYTES:
        flags.append("size_spread")
    # Impossible under rank_key. Computed from the data, not the rule, so a
    # bug in the rule shows up in the report instead of in a deletion.
    if keeper.hash_status != "truncated" and any(
        m.hash_status != "truncated" and (m.file_size or 0) > (keeper.file_size or 0)
        for m in members
    ):
        flags.append("keeper_smaller")
    return flags


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def load_members(conn: Connection, set_id: int) -> list[Member]:
    rows = conn.execute(
        """
        SELECT a.uuid, COALESCE(h.status, 'missing') AS hash_status, a.file_size,
               a.is_favorite, a.has_adjustments, a.title, a.description,
               a.albums, a.keywords, a.date_added, a.photos_date, a.file_mtime,
               a.uti, a.original_filename
        FROM dup_set_member m
        JOIN asset a ON a.uuid = m.uuid
        LEFT JOIN asset_hash h ON h.uuid = a.uuid
        WHERE m.set_id = ?
        ORDER BY a.uuid
        """,
        (set_id,),
    ).fetchall()
    return [
        Member(
            uuid=r["uuid"],
            hash_status=r["hash_status"],
            file_size=r["file_size"],
            is_favorite=bool(r["is_favorite"]),
            has_adjustments=bool(r["has_adjustments"]),
            title=r["title"],
            description=r["description"],
            albums=tuple(json.loads(r["albums"]) if r["albums"] else ()),
            keywords=tuple(json.loads(r["keywords"]) if r["keywords"] else ()),
            date_added=r["date_added"],
            photos_date=r["photos_date"],
            file_mtime=r["file_mtime"],
            uti=r["uti"],
            original_filename=r["original_filename"],
        )
        for r in rows
    ]


def _pools(conn: Connection, members: Sequence[Member]) -> list[Pool]:
    by_uuid: dict[str, list[Candidate]] = {m.uuid: [] for m in members}
    placeholders = ", ".join("?" * len(members))
    for r in conn.execute(
        f"SELECT uuid, source, value FROM date_candidate WHERE uuid IN ({placeholders})",
        [m.uuid for m in members],
    ):
        by_uuid[r["uuid"]].append(Candidate(r["source"], _parse(r["value"]), None))
    return [
        Pool(tuple(by_uuid[m.uuid]), _parse(m.date_added), _parse(m.file_mtime))
        for m in members
    ]


def run_arbitrate(
    conn: Connection,
    *,
    margin: timedelta = DEFAULT_DISAGREEMENT_MARGIN,
    force: bool = False,
    now: datetime | None = None,
) -> ArbitrateResult:
    """Rewrite every decision from the current sets and rules."""
    require_stage_complete(conn, "group", force=force)
    set_stage(conn, "arbitrate", "running")
    decided_at = datetime.now().replace(microsecond=0).isoformat()

    sets = conn.execute("SELECT id, tier FROM dup_set ORDER BY id").fetchall()
    resolvable_n = review_n = rescued_n = 0
    try:
        conn.execute("DELETE FROM decision_loser")
        conn.execute("DELETE FROM decision")
        for s in sets:
            members = load_members(conn, s["id"])
            keeper, reason, losers = choose_keeper(members)
            date = choose_date_pooled(_pools(conn, members), margin=margin, now=now)
            flags = review_flags(s["tier"], members, keeper, date)
            blocking = set(flags) - _INFORMATIONAL_FLAGS
            resolvable = s["tier"] == "exact" and not blocking
            keeper_date = _parse(keeper.photos_date)
            changes = (
                date.value is not None
                and (keeper_date is None or abs(date.value - keeper_date) > margin)
            )
            conn.execute(
                """
                INSERT INTO decision (set_id, keeper_uuid, keeper_reason, date_value,
                                      date_source, date_rejected, date_changes,
                                      resolvable, review_flags, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    s["id"], keeper.uuid, reason,
                    date.value.isoformat() if date.value else None,
                    date.source,
                    json.dumps([list(pair) for pair in date.rejected]),
                    int(changes), int(resolvable), ",".join(flags), decided_at,
                ),
            )
            conn.executemany(
                "INSERT INTO decision_loser (set_id, uuid, lost_on, carries) VALUES (?, ?, ?, ?)",
                [
                    (s["id"], m.uuid, why, json.dumps(carries(keeper, m), sort_keys=True))
                    for m, why in losers
                ],
            )
            resolvable_n += int(resolvable)
            review_n += int(not resolvable)
            rescued_n += int(changes)
    except Exception as exc:
        # A half-written decision table is worse than none; decisions are
        # cheap to recompute.
        conn.rollback()
        set_stage(conn, "arbitrate", "failed", notes=str(exc))
        raise

    conn.commit()
    set_stage(
        conn, "arbitrate", "complete",
        notes=(
            f"{len(sets)} sets: {resolvable_n} resolvable, {review_n} need review, "
            f"{rescued_n} dates rescued"
        ),
    )
    return ArbitrateResult(len(sets), resolvable_n, review_n, rescued_n)
```

Note on `set_stage` inside the try: `set_stage` commits. The `running` marker is written before the loop starts, so the rollback on failure only discards decisions written since, which is the intent. Do not move `set_stage(..., "running")` inside the try.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/arbitrate.py tests/test_arbitrate.py
git commit -m "feat: arbitrate keeper and capture date per duplicate set"
```

---

### Task 8: HTML review report

**Files:**
- Create: `src/photodedup/review.py`
- Test: `tests/test_review.py`

**Interfaces:**
- Consumes: `decision`, `decision_loser`, `dup_set`, `dup_set_member`, `asset.thumb_path`, `asset_hash.phash`, `require_stage_complete`.
- Produces: `write_review(conn, out_dir: Path, *, per_page: int = 250, sample: int = 300, force: bool = False) -> ReviewResult(sets, pages, thumbs, placeholders)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_review.py`:

```python
import json
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_review.py -q`
Expected: ModuleNotFoundError on `photodedup.review`.

- [ ] **Step 3: Implement**

Create `src/photodedup/review.py`:

```python
"""Static HTML review report over the arbitrate decisions.

Plain HTML and one stylesheet. No JavaScript, no network. Thumbnails are the
derivatives Photos already keeps, linked into the report directory so no
original is ever decoded here.
"""

from __future__ import annotations

import html
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection, Row

import imagehash

from photodedup.catalog import require_stage_complete, set_stage

CSS = """
body { font: 14px/1.4 -apple-system, Helvetica, Arial, sans-serif; margin: 1.5rem; color: #222; }
h1, h2 { font-weight: 600; }
table.totals td { padding: 0.15rem 1rem 0.15rem 0; }
.set { border: 1px solid #ddd; border-radius: 6px; padding: 0.75rem; margin: 1rem 0; }
.set h3 { margin: 0 0 0.5rem; font-size: 1rem; }
.flags { color: #a40; }
.resolvable { color: #080; }
.members { display: flex; flex-wrap: wrap; gap: 0.75rem; }
.member { width: 220px; border: 2px solid transparent; padding: 0.4rem; border-radius: 4px; }
.member.keeper { border-color: #080; background: #f2fbf2; }
.member img, .member .placeholder { width: 200px; height: 150px; object-fit: contain; background: #f4f4f4; display: block; }
.member .placeholder { color: #999; text-align: center; line-height: 150px; }
.member dl { margin: 0.4rem 0 0; font-size: 12px; }
.member dt { color: #777; float: left; clear: left; width: 62px; }
.member dd { margin: 0 0 0 66px; word-break: break-all; }
.date-agree { color: #080; } .date-conflict { color: #a40; font-weight: 600; } .date-missing { color: #999; }
nav a { margin-right: 0.75rem; }
"""


@dataclass(frozen=True)
class ReviewResult:
    sets: int
    pages: int
    thumbs: int
    placeholders: int


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _page(title: str, body: str, *, depth: int) -> str:
    css = "../" * depth + "review.css"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_e(title)}</title><link rel='stylesheet' href='{css}'></head>"
        f"<body><h1>{_e(title)}</h1>{body}</body></html>"
    )


def _link_thumb(src: str | None, dest_dir: Path, uuid: str) -> str | None:
    """Return the published thumbnail's file name, or None for a placeholder."""
    if not src or not Path(src).is_file():
        return None
    name = f"{uuid}{Path(src).suffix.lower() or '.jpg'}"
    target = dest_dir / name
    if target.exists() or target.is_symlink():
        return name
    try:
        os.symlink(src, target)
    except OSError:
        shutil.copyfile(src, target)
    return name


def _phash_distance(members: list[Row]) -> int | None:
    hashes = [m["phash"] for m in members if m["phash"]]
    if len(hashes) < 2:
        return None
    parsed = [imagehash.hex_to_hash(h) for h in hashes]
    return max(a - b for i, a in enumerate(parsed) for b in parsed[i + 1 :])


def _load(conn: Connection) -> list[dict]:
    """Every set with its decision, members and losers, ordered by set id."""
    sets = conn.execute(
        """
        SELECT s.id, s.tier, s.size, d.keeper_uuid, d.keeper_reason, d.date_value,
               d.date_source, d.date_rejected, d.date_changes, d.resolvable, d.review_flags
        FROM dup_set s JOIN decision d ON d.set_id = s.id
        ORDER BY s.id
        """
    ).fetchall()
    members = conn.execute(
        """
        SELECT m.set_id, a.uuid, a.original_filename, a.file_size, a.photos_date,
               a.date_added, a.thumb_path, a.uti, COALESCE(h.status, 'missing') AS hash_status,
               h.phash, l.lost_on, l.carries
        FROM dup_set_member m
        JOIN asset a ON a.uuid = m.uuid
        LEFT JOIN asset_hash h ON h.uuid = a.uuid
        LEFT JOIN decision_loser l ON l.set_id = m.set_id AND l.uuid = m.uuid
        ORDER BY m.set_id, a.uuid
        """
    ).fetchall()
    by_set: dict[int, list[Row]] = {}
    for m in members:
        by_set.setdefault(m["set_id"], []).append(m)
    out = []
    for s in sets:
        ms = by_set.get(s["id"], [])
        out.append({"set": s, "members": ms, "phash_distance": _phash_distance(ms)})
    return out


def _date_class(member: Row, s: Row) -> str:
    if not member["photos_date"]:
        return "date-missing"
    if not s["date_value"]:
        return "date-missing"
    return "date-agree" if member["photos_date"] == s["date_value"] else "date-conflict"


def _render_set(entry: dict, thumbs_dir: Path, *, depth: int, counters: dict) -> str:
    s = entry["set"]
    flags = s["review_flags"]
    status = (
        "<span class='resolvable'>resolvable</span>" if s["resolvable"]
        else "<span class='flags'>needs review</span>"
    )
    head = (
        f"<div class='set' id='set-{s['id']}'><h3>Set {s['id']} · {_e(s['tier'])} · "
        f"{s['size']} members · {status}"
        + (f" · <span class='flags'>flags: {_e(flags)}</span>" if flags else "")
        + "</h3>"
    )
    meta = (
        f"<p>keeper decided by <code>{_e(s['keeper_reason'])}</code>; capture date "
        f"<strong>{_e(s['date_value'] or 'none')}</strong> from <code>{_e(s['date_source'] or '-')}</code>"
        + (" (differs from keeper's Photos date: date rescued)" if s["date_changes"] else "")
        + (f"; phash distance {entry['phash_distance']}" if entry["phash_distance"] is not None else "")
        + "</p>"
    )
    cards = []
    for m in entry["members"]:
        is_keeper = m["uuid"] == s["keeper_uuid"]
        name = _link_thumb(m["thumb_path"], thumbs_dir, m["uuid"])
        if name is None:
            counters["placeholders"].add(m["uuid"])
            img = "<div class='placeholder'>no thumbnail</div>"
        else:
            counters["thumbs"].add(m["uuid"])
            img = f"<img loading='lazy' src='{'../' * depth}thumbs/{_e(name)}' alt=''>"
        carries = json.loads(m["carries"]) if m["carries"] else {}
        cards.append(
            f"<div class='member{' keeper' if is_keeper else ''}'>{img}<dl>"
            f"<dt>file</dt><dd>{_e(m['original_filename'])}</dd>"
            f"<dt>bytes</dt><dd>{_e(m['file_size'])}</dd>"
            f"<dt>photos</dt><dd class='{_date_class(m, s)}'>{_e(m['photos_date'] or 'missing')}</dd>"
            f"<dt>added</dt><dd>{_e(m['date_added'])}</dd>"
            f"<dt>hash</dt><dd>{_e(m['hash_status'])}</dd>"
            f"<dt>{'keeper' if is_keeper else 'lost on'}</dt>"
            f"<dd>{'KEEP' if is_keeper else _e(m['lost_on'])}</dd>"
            + (f"<dt>carries</dt><dd>{_e(json.dumps(carries))}</dd>" if carries else "")
            + "</dl></div>"
        )
    return head + meta + "<div class='members'>" + "".join(cards) + "</div></div>"


def _paginate(entries: list[dict], per_page: int) -> list[list[dict]]:
    return [entries[i : i + per_page] for i in range(0, len(entries), per_page)] or [[]]


def _nav(prefix: str, count: int, current: int) -> str:
    links = [
        f"<a href='{i:03d}.html'>{i}</a>" if i != current else f"<strong>{i}</strong>"
        for i in range(1, count + 1)
    ]
    return f"<nav><a href='../index.html'>index</a> {prefix}: {' '.join(links)}</nav>"


def write_review(
    conn: Connection,
    out_dir: Path,
    *,
    per_page: int = 250,
    sample: int = 300,
    force: bool = False,
) -> ReviewResult:
    require_stage_complete(conn, "arbitrate", force=force)
    set_stage(conn, "review", "running")

    out_dir = Path(out_dir)
    for sub in ("exact", "candidate", "samples", "thumbs"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)
    (out_dir / "review.css").write_text(CSS)
    thumbs_dir = out_dir / "thumbs"
    # Sets of uuids: a member rendered on a listing page and again on a sample
    # page is still one thumbnail on disk.
    counters: dict[str, set[str]] = {"thumbs": set(), "placeholders": set()}

    entries = _load(conn)
    exact = [e for e in entries if e["set"]["tier"] == "exact"]
    candidate = [e for e in entries if e["set"]["tier"] == "candidate"]
    pages = 0

    def write_listing(name: str, items: list[dict]) -> int:
        chunks = _paginate(items, per_page)
        for i, chunk in enumerate(chunks, start=1):
            body = _nav(name, len(chunks), i) + "".join(
                _render_set(e, thumbs_dir, depth=1, counters=counters) for e in chunk
            )
            (out_dir / name / f"{i:03d}.html").write_text(
                _page(f"{name} sets, page {i} of {len(chunks)}", body, depth=1)
            )
        return len(chunks)

    pages += write_listing("exact", exact)
    candidate_sorted = sorted(
        candidate,
        key=lambda e: (e["phash_distance"] if e["phash_distance"] is not None else 999, e["set"]["id"]),
    )
    pages += write_listing("candidate", candidate_sorted[:sample])

    def has_flag(e: dict, flag: str) -> bool:
        return flag in e["set"]["review_flags"].split(",")

    def spread(e: dict) -> int:
        sizes = [m["file_size"] for m in e["members"] if m["file_size"] is not None]
        return max(sizes) - min(sizes) if sizes else 0

    def keeper_gap(e: dict) -> float:
        """Seconds between the arbitrated date and the keeper's own Photos date."""
        s = e["set"]
        keeper = next((m for m in e["members"] if m["uuid"] == s["keeper_uuid"]), None)
        if keeper is None or not keeper["photos_date"] or not s["date_value"]:
            return float("inf")
        return abs(
            (datetime.fromisoformat(s["date_value"]) - datetime.fromisoformat(keeper["photos_date"])).total_seconds()
        )

    samples: list[tuple[str, str, list[dict]]] = [
        ("dates", "Dates rescued (largest keeper disagreement first)",
         sorted([e for e in exact if e["set"]["date_changes"]], key=lambda e: (-keeper_gap(e), e["set"]["id"]))[:sample]),
        ("largest", "Largest exact sets",
         sorted(exact, key=lambda e: (-e["set"]["size"], e["set"]["id"]))[:sample]),
        ("truncated", "Sets with a truncated member",
         [e for e in entries if has_flag(e, "truncated_member")][:sample]),
        ("size_spread", "Exact sets whose intact members differ by 1 MB or more",
         sorted([e for e in exact if has_flag(e, "size_spread")], key=lambda e: -spread(e))[:sample]),
        ("keeper_smaller", "Keeper smaller than an intact loser (must be empty)",
         [e for e in entries if has_flag(e, "keeper_smaller")]),
        ("candidate", "Candidate-only sets, closest phash first",
         candidate_sorted[:sample]),
    ]
    for key, title, items in samples:
        body = "<nav><a href='../index.html'>index</a></nav>" + (
            "".join(_render_set(e, thumbs_dir, depth=1, counters=counters) for e in items)
            or "<p>none</p>"
        )
        (out_dir / "samples" / f"{key}.html").write_text(_page(title, body, depth=1))
        pages += 1

    flag_counts: dict[str, int] = {}
    for e in entries:
        for f in e["set"]["review_flags"].split(","):
            if f:
                flag_counts[f] = flag_counts.get(f, 0) + 1
    resolvable = sum(1 for e in entries if e["set"]["resolvable"])
    rescued = sum(1 for e in entries if e["set"]["date_changes"])
    exact_assets = sum(e["set"]["size"] for e in exact)
    damaged = conn.execute(
        """
        SELECT COUNT(*) AS n FROM asset_hash h
        WHERE h.status = 'truncated'
          AND NOT EXISTS (SELECT 1 FROM dup_set_member m WHERE m.uuid = h.uuid AND m.tier = 'exact')
        """
    ).fetchone()["n"]

    keeper_smaller = flag_counts.get("keeper_smaller", 0)
    totals = [
        ("sets", f"{len(entries)} sets ({len(exact)} exact, {len(candidate)} candidate)"),
        ("exact assets", f"{exact_assets} assets in exact sets, {exact_assets - len(exact)} removable"),
        ("resolvable", f"{resolvable} resolvable"),
        ("dates rescued", f"{rescued} dates rescued"),
        ("keeper smaller than an intact loser", f"{keeper_smaller}, as required" if keeper_smaller == 0 else f"{keeper_smaller} — BUG, do not proceed"),
        ("unique damaged files", f"{damaged} truncated files with no intact twin"),
    ] + [(f"flag {k}", str(v)) for k, v in sorted(flag_counts.items())]
    rows = "".join(f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in totals)
    sample_links = "".join(
        f"<li><a href='samples/{_e(key)}.html'>{_e(title)}</a> ({len(items)})</li>"
        for key, title, items in samples
    )
    exact_pages = len(_paginate(exact, per_page))
    exact_links = " ".join(f"<a href='exact/{i:03d}.html'>{i}</a>" for i in range(1, exact_pages + 1))
    index = (
        f"<table class='totals'>{rows}</table>"
        f"<h2>Samples</h2><ul>{sample_links}</ul>"
        f"<h2>All exact sets</h2><p>{exact_links}</p>"
        f"<h2>Candidate sets (first {sample})</h2><p><a href='candidate/001.html'>open</a></p>"
    )
    (out_dir / "index.html").write_text(_page("Duplicate review", index, depth=0))
    pages += 1

    result = ReviewResult(
        len(entries), pages, len(counters["thumbs"]), len(counters["placeholders"])
    )
    set_stage(
        conn, "review", "complete",
        notes=f"{result.sets} sets on {result.pages} pages, {result.thumbs} thumbnails",
    )
    return result
```

`ReviewResult.thumbs` and `.placeholders` are distinct member counts, not
render counts; a set rendered on a listing page and a sample page contributes
its members once. `test_writes_index_pages_and_thumbs` expects 6 and 1.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/photodedup/review.py tests/test_review.py
git commit -m "feat: static HTML review report over arbitrate decisions"
```

---

### Task 9: CLI, docs, and handoff

**Files:**
- Modify: `src/photodedup/cli.py`
- Modify: `STATE.md`
- Test: `tests/test_cli.py` (create)

**Interfaces:**
- Consumes: `run_sets`, `run_arbitrate`, `write_review`, `StageIncomplete`, `GroupResult.exact_sets`, `HashResult.truncated`.
- Produces: `photodedup sets`, `photodedup arbitrate [--margin-hours] [--force]`, `photodedup review [--out] [--per-page] [--sample] [--force]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli.py`:

```python
import pytest
from click.testing import CliRunner

from photodedup.catalog import open_catalog, set_stage
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


def test_sets_runs_on_grouped_catalog(catalog):
    conn = open_catalog(catalog)
    set_stage(conn, "group", "complete")
    conn.close()
    result = run("sets", "--catalog", str(catalog))
    assert result.exit_code == 0, result.output
    assert "0 exact sets" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q`
Expected: `test_help_lists_phase3_commands` fails; the others fail with "No such command".

- [ ] **Step 3: Implement**

In `src/photodedup/cli.py`, add imports:

```python
from datetime import timedelta

from photodedup.arbitrate import run_arbitrate
from photodedup.catalog import StageIncomplete, open_catalog, require_stage_complete
from photodedup.review import write_review
from photodedup.sets import run_sets
```

(replace the existing `from photodedup.catalog import open_catalog` line.)

In the `hash` command's final `click.echo`, change the parenthetical to `f"({result.hashed} hashed, {result.truncated} truncated, {result.no_original} without originals, "`.

In the `group` command, replace the `click.echo` with:

```python
    click.echo(
        f"{result.groups} groups covering {result.members} assets; "
        f"{result.exact_sets} exact sets, {result.candidate_sets} candidate sets "
        f"(see `photodedup duplicates` for removable counts)"
    )
```

Append three commands:

```python
@main.command()
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
@click.option("--force", is_flag=True, help="Rebuild sets even though the group stage has not reported complete.")
def sets(catalog: Path, force: bool) -> None:
    """Rebuild duplicate sets from the groups. `group` does this itself; use
    this after a manual fix to the groups."""
    conn = open_catalog(catalog)
    try:
        require_stage_complete(conn, "group", force=force)
    except StageIncomplete as exc:
        raise click.ClickException(str(exc)) from exc
    result = run_sets(conn)
    click.echo(
        f"{result.exact_sets} exact sets over {result.exact_assets} assets; "
        f"{result.candidate_sets} candidate sets over {result.candidate_assets} assets"
    )


@main.command()
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
@click.option(
    "--margin-hours", type=float, default=26.0, show_default=True,
    help="Dates closer than this are one date rendered in two timezones, not a conflict.",
)
@click.option("--force", is_flag=True, help="Arbitrate even though the group stage has not reported complete.")
def arbitrate(catalog: Path, margin_hours: float, force: bool) -> None:
    """Decide the keeper and capture date for every duplicate set. Writes only
    to the catalog; re-run freely after changing rules."""
    conn = open_catalog(catalog)
    try:
        result = run_arbitrate(conn, margin=timedelta(hours=margin_hours), force=force)
    except StageIncomplete as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"{result.sets} sets: {result.resolvable} resolvable, {result.review} need review, "
        f"{result.dates_rescued} dates rescued"
    )


@main.command()
@click.option(
    "--catalog", type=click.Path(path_type=Path), default=DEFAULT_CATALOG, show_default=True
)
@click.option(
    "--out", type=click.Path(path_type=Path), default=Path("reports/review"), show_default=True
)
@click.option("--per-page", type=int, default=250, show_default=True)
@click.option("--sample", type=int, default=300, show_default=True, help="Sets per sampled section.")
@click.option("--force", is_flag=True, help="Write the report even though arbitrate has not reported complete.")
def review(catalog: Path, out: Path, per_page: int, sample: int, force: bool) -> None:
    """Write the static HTML review report. Open <out>/index.html in a browser."""
    conn = open_catalog(catalog)
    try:
        result = write_review(conn, out, per_page=per_page, sample=sample, force=force)
    except StageIncomplete as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"wrote {out / 'index.html'}: {result.sets} sets on {result.pages} pages, "
        f"{result.thumbs} thumbnails, {result.placeholders} placeholders"
    )
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Update STATE.md**

Edit `STATE.md`:

- Header: set "Current phase" to `Phase 3 (arbitrate, review report) — BUILD COMPLETE at HEAD; not yet run against the real catalog.` Update "Last updated".
- Replace "The one outstanding action" with:

```
## The one outstanding action

Rebuild the catalog at schema v3 and run the pipeline through `review`, in
your own terminal (the agent shell cannot start the process pool):

    cd /Users/robotdan/dev/tools
    rm -f catalog.sqlite catalog.sqlite-shm catalog.sqlite-wal
    uv run photodedup survey --missing-tolerance 0.05     # ~5 min
    uv run photodedup hash --workers 4                    # <15 min
    uv run photodedup group
    uv run photodedup duplicates
    uv run photodedup arbitrate
    uv run photodedup review
    open reports/review/index.html

Then record the numbers under "Phase 3 results" and compare them with the
"Expected results on this library" section of the Phase 3 spec. If they
disagree by more than the prefix rule explains, stop and find out why
before sampling.
```

- Under "Progress", add a "Phase 3 — BUILD COMPLETE" block listing the nine tasks with their commit SHAs (fill in from `git log`).
- Add an empty "## Phase 3 results" section with the line `_Empty on purpose. Fill in from the real run: arbitrate totals, review report location, and how the sampled decisions looked._`
- In "Modules", add `sets.py`, `arbitrate.py`, `review.py` with one line each, and note that `dates.py` now holds `IMPORT_STAMPS` and `choose_date_pooled`.
- In "Known Phase 2 obligation", replace the paragraph about `choose_date` being unused with: `Resolved in Phase 3: choose_date_pooled is the single authority; the report's SQL copy is gone.`
- Under "Running the tool", add `sets`, `arbitrate`, `review` lines.
- Add to the "Phase 2 opportunity" or a new short section: `A third capture-hour cluster, 218 assets at 2023-05-03 01:00 with no EXIF, all added within two minutes on 2025-03-12, is unexplained. It is NOT in IMPORT_STAMPS. Look at it before Phase 4 corrects any dates.`

- [ ] **Step 6: Commit**

```bash
git add src/photodedup/cli.py tests/test_cli.py STATE.md
git commit -m "feat: sets, arbitrate and review commands; phase 3 handoff in STATE.md"
```

---

## Self-review against the spec

- Task A (truncated files): Tasks 2 and 3. Lenient decode, `real_length`, `truncated` status, prefix rule, truncated in dhash candidate groups, JSON PNG stays an error. Covered.
- Task B (sets): Task 6. Exact union-find, candidate components over exact sets, `via_exact` marker, kind tripwire, `exact_components` rewrite. Covered.
- Task C (arbitrate): Task 7 with Task 5 supplying the date authority and stamps. Keeper rules, `lost_on`, flags with the two informational exceptions, `carries` with the filename-echo filter, guard on `group`. Covered.
- Task D (review): Task 8. Static folder, no JS, thumbnails via symlink with copy fallback, placeholder for missing, index totals, six sample pages, candidate ordering by phash distance. Covered. `thumb_path` supplied by Task 4.
- Task E (CLI): Task 9, plus STATE.md handoff.
- Schema: Task 1. `thumb_path`, statuses, `prefix` signal, four tables, partial unique index, stage guard.
- Error handling: vanished file skipped (Task 3), all-truncated set still decided and flagged (Task 7 flags), empty catalog completes (Task 7 test), per-set exception rolls back (Task 7).
- Expected results on the library are in the spec and in the STATE.md instructions; the numbers are checked by the user's run, not by tests.

Type consistency checked: `Member` fields match `load_members`; `Pool`/`choose_date_pooled` names match between Tasks 5 and 7; `StageIncomplete`/`require_stage_complete` match between Tasks 1, 7, 8, 9; `GroupResult.exact_sets` used in Task 9 is defined in Task 6; `HashResult.truncated` used in Task 9 is defined in Task 2.
