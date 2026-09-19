"""Form duplicate sets from the content signals. Decides nothing."""

from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection

from photodedup.catalog import get_stage, set_stage

# (signal, hash column, tier). Order is the trust order and is also the order
# the review report presents them in.
SIGNALS: tuple[tuple[str, str, str], ...] = (
    ("scan", "scan_hash", "exact"),
    ("pixel", "pixel_hash", "exact"),
    ("video", "video_hash", "exact"),
    ("dhash", "dhash", "candidate"),
)

# Formats that can hold an animation. `pixel_hashes` decodes frame 0 only, so
# two animations sharing a first frame would otherwise look byte-for-byte
# identical and land in an `exact` group that is safe to auto-resolve. They are
# not the same file. These may still form `candidate` groups for human review.
MULTIFRAME_UTIS: tuple[str, ...] = ("com.compuserve.gif", "org.webmproject.webp")


class HashStageIncomplete(RuntimeError):
    """Grouping was asked to run before the hash stage finished."""


@dataclass(frozen=True)
class GroupResult:
    groups: int
    members: int
    removable: int


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

    for signal, column, tier in SIGNALS:
        # Partition by kind and has_raw inside the key itself, so a group can
        # never span the hard partitions no matter what the hashes say.
        multiframe = ""
        if tier == "exact":
            placeholders = ", ".join(f"'{u}'" for u in MULTIFRAME_UTIS)
            # COALESCE because `NULL NOT IN (...)` is NULL, which would drop
            # every asset whose uti the survey never recorded.
            multiframe = f"AND COALESCE(a.uti, '') NOT IN ({placeholders})"
        rows = conn.execute(
            f"""
            SELECT h.{column} AS digest, a.kind AS kind, a.has_raw AS has_raw,
                   GROUP_CONCAT(a.uuid) AS uuids, COUNT(*) AS n
            FROM asset_hash h
            JOIN asset a ON a.uuid = h.uuid AND a.survey_status = 'ok'
            WHERE h.status = 'ok' AND h.{column} IS NOT NULL {multiframe}
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
