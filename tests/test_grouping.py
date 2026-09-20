import pytest

from photodedup.catalog import get_stage, open_catalog, set_stage
from photodedup.grouping import HashStageIncomplete, run_group


@pytest.fixture
def conn(tmp_path):
    """A catalog whose hash stage has reported complete.

    `run_group` refuses to run otherwise, so every grouping test needs the
    precondition satisfied before it can measure anything else.
    """
    conn = open_catalog(tmp_path / "catalog.sqlite")
    set_stage(conn, "hash", "complete")
    return conn


def add(conn, uuid, *, kind="image", has_raw=0, scan=None, pixel=None,
        video=None, dhash=None, status="ok", uti="public.jpeg"):
    conn.execute(
        """INSERT INTO asset (uuid, kind, has_raw, uti, survey_status)
           VALUES (?, ?, ?, ?, 'ok')""",
        (uuid, kind, has_raw, uti),
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


GIF = "com.compuserve.gif"


def test_animated_gifs_never_form_an_exact_group(conn):
    """Frame 0 is not the file. Two animations can share a first frame."""
    add(conn, "u1", pixel="ppp", uti=GIF)
    add(conn, "u2", pixel="ppp", uti=GIF)
    assert run_group(conn).groups == 0


def test_animated_gifs_still_form_a_candidate_group(conn):
    add(conn, "u1", dhash="dddd", uti=GIF)
    add(conn, "u2", dhash="dddd", uti=GIF)
    run_group(conn)
    rows = groups(conn)
    assert [(r["signal"], r["tier"]) for r in rows] == [("dhash", "candidate")]


def test_webp_is_excluded_from_exact_groups_too(conn):
    add(conn, "u1", pixel="ppp", uti="org.webmproject.webp")
    add(conn, "u2", pixel="ppp", uti="org.webmproject.webp")
    assert run_group(conn).groups == 0


def test_jpegs_sharing_a_pixel_hash_still_group_exactly(conn):
    add(conn, "u1", pixel="ppp")
    add(conn, "u2", pixel="ppp")
    result = run_group(conn)
    assert result.groups == 1
    assert groups(conn)[0]["tier"] == "exact"


def test_assets_with_no_uti_still_group(conn):
    """A missing uti must not be read as 'might be an animation'."""
    add(conn, "u1", pixel="ppp", uti=None)
    add(conn, "u2", pixel="ppp", uti=None)
    assert run_group(conn).groups == 1


def test_refuses_when_the_hash_stage_is_still_running(conn):
    set_stage(conn, "hash", "running")
    add(conn, "u1", scan="aaa")
    add(conn, "u2", scan="aaa")
    with pytest.raises(HashStageIncomplete) as exc:
        run_group(conn)
    assert "running" in str(exc.value)
    assert "--force" in str(exc.value)
    assert conn.execute("SELECT COUNT(*) AS n FROM dup_group").fetchone()["n"] == 0


def test_refuses_when_the_hash_stage_never_ran(tmp_path):
    fresh = open_catalog(tmp_path / "fresh.sqlite")
    with pytest.raises(HashStageIncomplete):
        run_group(fresh)


def test_force_groups_a_partially_hashed_catalog(conn):
    set_stage(conn, "hash", "running")
    add(conn, "u1", scan="aaa")
    add(conn, "u2", scan="aaa")
    result = run_group(conn, force=True)
    assert result.groups == 1
    assert get_stage(conn, "group")["status"] == "complete"


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


def test_partner_diverging_within_the_probe_is_rejected_without_a_full_read(
    conn, tmp_path, monkeypatch
):
    """On the real catalog a truncated file draws thousands of same-dimension
    partner candidates; reading each one's full prefix to reject it is the
    32 GB problem. A 64 KB head comparison must reject a diverging partner
    without ever requesting its full-length prefix, and a real match must
    still be found."""
    import photodedup.grouping as grouping_module

    length = grouping_module.PREFIX_PROBE_BYTES + 5000
    head_bytes = bytes(i % 256 for i in range(length))

    truncated_path = tmp_path / "tr.bin"
    truncated_path.write_bytes(head_bytes)

    matching_path = tmp_path / "match.bin"
    matching_path.write_bytes(head_bytes + b"trailing-bytes-not-in-the-prefix")

    diverging = bytearray(head_bytes)
    diverging[100] = (diverging[100] + 1) % 256  # diverges well inside 64 KB
    diverging_path = tmp_path / "diverge.bin"
    diverging_path.write_bytes(bytes(diverging))

    add_file(conn, "tr1", truncated_path, status="truncated", bytes_read=length)
    add_file(conn, "ok_match", matching_path, status="ok", scan="s1", pixel="p1")
    add_file(conn, "ok_diverge", diverging_path, status="ok", scan="s2", pixel="p2")

    requests: dict[str, list[int]] = {}
    real_read = grouping_module._read_prefix

    def spy(path, n):
        requests.setdefault(path, []).append(n)
        return real_read(path, n)

    monkeypatch.setattr(grouping_module, "_read_prefix", spy)

    pairs = prefix_groups(conn)

    assert requests[str(diverging_path)] == [grouping_module.PREFIX_PROBE_BYTES]
    assert requests[str(matching_path)] == [grouping_module.PREFIX_PROBE_BYTES, length]
    assert len(pairs) == 1
    digest, uuids = pairs[0]
    assert set(uuids) == {"tr1", "ok_match"}
