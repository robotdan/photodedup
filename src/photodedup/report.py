"""Markdown reporting over the catalog."""

from __future__ import annotations

from sqlite3 import Connection

from photodedup.catalog import get_run


def date_added_clusters(
    conn: Connection, *, window_minutes: int = 60, min_size: int = 50
) -> list[tuple[str, int]]:
    """Bucket date_added into windows and return the crowded ones, largest first.

    A bulk import shows up here as hundreds or thousands of assets sharing one
    window, which is the signature that identifies the bad-metadata cohort.
    """
    seconds = window_minutes * 60
    rows = conn.execute(
        """
        SELECT
            datetime((strftime('%s', date_added) / :sec) * :sec, 'unixepoch') AS bucket,
            COUNT(*) AS n
        FROM asset
        WHERE date_added IS NOT NULL
        GROUP BY bucket
        HAVING n >= :min_size
        ORDER BY n DESC, bucket ASC
        """,
        {"sec": seconds, "min_size": min_size},
    ).fetchall()
    return [(row["bucket"].replace(" ", "T"), row["n"]) for row in rows]


def _table(header: tuple[str, ...], rows: list[tuple]) -> str:
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out) + "\n"


def survey_report(conn: Connection) -> str:
    run = get_run(conn)
    total = conn.execute("SELECT COUNT(*) AS n FROM asset").fetchone()["n"]

    by_kind = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM asset GROUP BY kind ORDER BY n DESC"
    ).fetchall()
    by_uti = conn.execute(
        "SELECT uti, COUNT(*) AS n FROM asset GROUP BY uti ORDER BY n DESC LIMIT 15"
    ).fetchall()
    flags = conn.execute(
        """
        SELECT
            SUM(is_live) AS live,
            SUM(has_raw) AS raw,
            SUM(has_adjustments) AS edited,
            SUM(is_favorite) AS favorite,
            SUM(CASE WHEN path IS NULL THEN 1 ELSE 0 END) AS no_original,
            SUM(CASE WHEN exif_error IS NOT NULL THEN 1 ELSE 0 END) AS damaged,
            SUM(CASE WHEN survey_status = 'error' THEN 1 ELSE 0 END) AS errors
        FROM asset
        """
    ).fetchone()
    rejections = conn.execute(
        """
        SELECT source, COALESCE(rejected_reason, 'accepted') AS reason, COUNT(*) AS n
        FROM date_candidate
        GROUP BY source, reason
        ORDER BY source, n DESC
        """
    ).fetchall()
    disagreements = conn.execute(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT uuid
            FROM date_candidate
            WHERE rejected_reason IS NULL AND value IS NOT NULL
            GROUP BY uuid
            HAVING MAX(value) > MIN(value)
        )
        """
    ).fetchone()["n"]
    no_candidate = conn.execute(
        """
        SELECT COUNT(*) AS n FROM asset a
        WHERE NOT EXISTS (
            SELECT 1 FROM date_candidate c
            WHERE c.uuid = a.uuid AND c.rejected_reason IS NULL AND c.value IS NOT NULL
        )
        """
    ).fetchone()["n"]

    clusters = date_added_clusters(conn)

    parts = [
        "# Survey Report\n",
        f"- Library: `{run['library_path'] if run else 'unknown'}`",
        f"- Cutoff (max date_added at survey time): `{run['cutoff_date_added'] if run else 'unset'}`",
        f"- Assets surveyed: **{total}**\n",
        "## Asset kinds\n",
        _table(("kind", "count"), [(r["kind"], r["n"]) for r in by_kind]),
        "\n## File types\n",
        _table(("uti", "count"), [(r["uti"], r["n"]) for r in by_uti]),
        "\n## Flags\n",
        _table(
            ("flag", "count"),
            [
                ("live photos", flags["live"] or 0),
                ("has raw", flags["raw"] or 0),
                ("edited", flags["edited"] or 0),
                ("favorite", flags["favorite"] or 0),
                ("no local original", flags["no_original"] or 0),
                ("damaged (exiftool error)", flags["damaged"] or 0),
                ("survey errors", flags["errors"] or 0),
            ],
        ),
        "\n## Date candidates by source and outcome\n",
        _table(
            ("source", "outcome", "count"),
            [(r["source"], r["reason"], r["n"]) for r in rejections],
        ),
        "\n## Date agreement\n",
        _table(
            ("measure", "count"),
            [
                ("assets whose surviving candidates disagree", disagreements),
                ("assets with no surviving candidate", no_candidate),
            ],
        ),
        "\n## Import spikes (date_added clustered hourly, 50+ assets)\n",
        "Large clusters are bulk imports. The cohort whose dates collapsed to",
        "import time will appear here.\n",
        _table(("hour", "assets"), clusters[:40]),
    ]
    return "\n".join(parts)
