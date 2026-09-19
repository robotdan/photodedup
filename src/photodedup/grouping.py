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
