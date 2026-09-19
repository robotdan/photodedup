"""Markdown reporting over the catalog."""

from __future__ import annotations

from sqlite3 import Connection

from photodedup.catalog import get_run
from photodedup.dates import DEFAULT_DISAGREEMENT_MARGIN

# Seconds of difference below which two dates for the same asset are treated as
# a timezone rendering of the same instant rather than a real conflict.
_MARGIN_SECONDS = int(DEFAULT_DISAGREEMENT_MARGIN.total_seconds())


def _cluster(
    conn: Connection, column: str, *, window_minutes: int, min_size: int
) -> list[tuple[str, int]]:
    seconds = window_minutes * 60
    rows = conn.execute(
        f"""
        SELECT
            datetime((strftime('%s', {column}) / :sec) * :sec, 'unixepoch') AS bucket,
            COUNT(*) AS n
        FROM asset
        WHERE {column} IS NOT NULL AND survey_status = 'ok'
        GROUP BY bucket
        HAVING n >= :min_size
        ORDER BY n DESC, bucket ASC
        """,
        {"sec": seconds, "min_size": min_size},
    ).fetchall()
    return [(row["bucket"].replace(" ", "T"), row["n"]) for row in rows]


def date_added_clusters(
    conn: Connection, *, window_minutes: int = 60, min_size: int = 50
) -> list[tuple[str, int]]:
    """Bucket date_added into windows and return the crowded ones, largest first.

    A bulk import shows up here as hundreds or thousands of assets sharing one
    window, which is the signature that identifies the bad-metadata cohort.
    """
    return _cluster(conn, "date_added", window_minutes=window_minutes, min_size=min_size)


def photos_date_clusters(
    conn: Connection, *, window_minutes: int = 60, min_size: int = 50
) -> list[tuple[str, int]]:
    """The same hourly clustering, but over the CAPTURE date.

    Clustering date_added marks every bulk import, good and bad alike.
    Clustering photos_date isolates the assets whose capture dates collapsed
    onto import time, because real captures do not pile hundreds deep into a
    single hour while import batches do.
    """
    return _cluster(
        conn, "photos_date", window_minutes=window_minutes, min_size=min_size
    )


_EXIF_VS_PHOTOS_LABELS: tuple[tuple[str, str], ...] = (
    ("both_equal", "EXIF and Photos dates both usable and identical"),
    (
        "both_close",
        "both usable, differ by 26 hours or less (timezone-sized)",
    ),
    (
        "both_far",
        "both usable, differ by more than 26 hours (real conflict)",
    ),
    (
        "exif_only_photos_rejected",
        "EXIF usable, Photos date rejected (recoverable from EXIF)",
    ),
    (
        "photos_only",
        "Photos date usable, EXIF absent or unparseable",
    ),
    ("neither", "neither source usable"),
)

_EXIF_VS_PHOTOS_SQL = """
WITH pivot AS (
    SELECT
        a.uuid AS uuid,
        MAX(CASE WHEN c.source = 'exif' THEN c.value END) AS e_val,
        MAX(CASE WHEN c.source = 'exif' THEN c.rejected_reason END) AS e_rej,
        MAX(CASE WHEN c.source = 'photos_db' THEN c.value END) AS p_val,
        MAX(CASE WHEN c.source = 'photos_db' THEN c.rejected_reason END) AS p_rej
    FROM asset a
    LEFT JOIN date_candidate c
        ON c.uuid = a.uuid AND c.source IN ('exif', 'photos_db')
    WHERE a.survey_status = 'ok'
    GROUP BY a.uuid
),
classified AS (
    SELECT CASE
        WHEN e_val IS NOT NULL AND e_rej IS NULL
             AND p_val IS NOT NULL AND p_rej IS NULL THEN
            CASE
                WHEN e_val = p_val THEN 'both_equal'
                WHEN ABS(strftime('%s', e_val) - strftime('%s', p_val))
                     <= :margin THEN 'both_close'
                ELSE 'both_far'
            END
        WHEN e_val IS NOT NULL AND e_rej IS NULL THEN 'exif_only_photos_rejected'
        WHEN p_val IS NOT NULL AND p_rej IS NULL THEN 'photos_only'
        ELSE 'neither'
    END AS bucket
    FROM pivot
)
SELECT bucket, COUNT(*) AS n FROM classified GROUP BY bucket
"""


def exif_vs_photos_db(conn: Connection) -> list[tuple[str, int]]:
    """Cross-tabulate the EXIF date against the Photos database date per asset.

    Marginal counts cannot answer the question this project exists to answer.
    The cohort A recovery case is precisely the one where the Photos date is
    REJECTED and the EXIF date survives: measured over survivors alone it looks
    like a single-candidate asset in perfect agreement with itself. This pivot
    uses the raw parsed value and its rejection state, so that case gets its
    own row.

    Returns one (bucket_key, count) pair for every combination, in a fixed
    order, including zero rows.
    """
    counts = {
        row["bucket"]: row["n"]
        for row in conn.execute(_EXIF_VS_PHOTOS_SQL, {"margin": _MARGIN_SECONDS})
    }
    return [(key, counts.get(key, 0)) for key, _label in _EXIF_VS_PHOTOS_LABELS]


def sentinel_breakdown(conn: Connection) -> list[tuple[str, str, int]]:
    """Which sentinel values actually fired, by source, largest bucket first.

    Aggregate sentinel counts hide the only ambiguous sentinel: 2001-01-01 is
    Apple's reference epoch but is also a date a photo can really be taken on.
    This is what makes that bucket visible for individual inspection.

    Grouped by DAY rather than by exact timestamp. A camera whose clock reset
    keeps counting from the epoch, so a reset run yields one distinct value per
    second; grouping by value rendered such a run as hundreds of single-asset
    rows, burying the fact that they are one event.
    """
    rows = conn.execute(
        """
        SELECT c.source AS source, substr(c.value, 1, 10) AS day, COUNT(*) AS n
        FROM date_candidate c
        JOIN asset a ON a.uuid = c.uuid AND a.survey_status = 'ok'
        WHERE c.rejected_reason = 'sentinel'
        GROUP BY c.source, day
        ORDER BY n DESC, c.source ASC, day ASC
        """
    ).fetchall()
    return [(row["source"], row["day"], row["n"]) for row in rows]


def timezone_artifacts(conn: Connection) -> int:
    """Assets whose photos_date sits after date_added by less than 24 hours.

    A capture cannot really postdate its own import, but 74% of this library's
    assets carry a photos_date timezone offset that differs from date_added's.
    A sub-day overshoot is that rendering artifact, not a corrupt date.
    """
    return conn.execute(
        """
        SELECT COUNT(*) AS n FROM asset
        WHERE survey_status = 'ok'
          AND photos_date IS NOT NULL
          AND date_added IS NOT NULL
          AND photos_date > date_added
          AND strftime('%s', photos_date) - strftime('%s', date_added) < 86400
        """
    ).fetchone()["n"]


def _table(header: tuple[str, ...], rows: list[tuple]) -> str:
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out) + "\n"


def survey_report(conn: Connection) -> str:
    run = get_run(conn)
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM asset WHERE survey_status = 'ok'"
    ).fetchone()["n"]
    error_count = conn.execute(
        "SELECT COUNT(*) AS n FROM asset WHERE survey_status = 'error'"
    ).fetchone()["n"]

    by_kind = conn.execute(
        """
        SELECT kind, COUNT(*) AS n FROM asset WHERE survey_status = 'ok'
        GROUP BY kind ORDER BY n DESC
        """
    ).fetchall()
    by_uti = conn.execute(
        """
        SELECT uti, COUNT(*) AS n FROM asset WHERE survey_status = 'ok'
        GROUP BY uti ORDER BY n DESC LIMIT 15
        """
    ).fetchall()
    flags = conn.execute(
        """
        SELECT
            SUM(is_live) AS live,
            SUM(has_raw) AS raw,
            SUM(has_adjustments) AS edited,
            SUM(is_favorite) AS favorite,
            SUM(CASE WHEN path IS NULL THEN 1 ELSE 0 END) AS no_original,
            SUM(CASE WHEN exif_error IS NOT NULL THEN 1 ELSE 0 END) AS damaged
        FROM asset
        WHERE survey_status = 'ok'
        """
    ).fetchone()
    rejections = conn.execute(
        """
        SELECT c.source AS source,
               COALESCE(c.rejected_reason, 'accepted') AS reason,
               COUNT(*) AS n
        FROM date_candidate c
        JOIN asset a ON a.uuid = c.uuid AND a.survey_status = 'ok'
        GROUP BY c.source, reason
        ORDER BY c.source, n DESC
        """
    ).fetchall()
    disagreements = conn.execute(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT c.uuid
            FROM date_candidate c
            JOIN asset a ON a.uuid = c.uuid AND a.survey_status = 'ok'
            WHERE c.rejected_reason IS NULL AND c.value IS NOT NULL
            GROUP BY c.uuid
            HAVING MAX(c.value) > MIN(c.value)
        )
        """
    ).fetchone()["n"]
    no_candidate = conn.execute(
        """
        SELECT COUNT(*) AS n FROM asset a
        WHERE a.survey_status = 'ok' AND NOT EXISTS (
            SELECT 1 FROM date_candidate c
            WHERE c.uuid = a.uuid AND c.rejected_reason IS NULL AND c.value IS NOT NULL
        )
        """
    ).fetchone()["n"]

    labels = dict(_EXIF_VS_PHOTOS_LABELS)
    cross = [(labels[key], n) for key, n in exif_vs_photos_db(conn)]
    sentinels = sentinel_breakdown(conn)
    tz_artifacts = timezone_artifacts(conn)
    clusters = date_added_clusters(conn)
    capture_clusters = photos_date_clusters(conn)

    parts = [
        "# Survey Report\n",
        f"- Library: `{run['library_path'] if run else 'unknown'}`",
        f"- Cutoff (max date_added at survey time): `{run['cutoff_date_added'] if run else 'unset'}`",
        f"- Assets surveyed OK: **{total}**",
        f"- Assets that failed to survey (excluded from every count below): **{error_count}**\n",
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
                ("survey errors", error_count),
            ],
        ),
        "\n## Date candidates by source and outcome\n",
        _table(
            ("source", "outcome", "count"),
            [(r["source"], r["reason"], r["n"]) for r in rejections],
        ),
        "\n## EXIF date vs Photos database date\n",
        "This is the table that sizes the recovery opportunity. The row for a",
        "rejected Photos date alongside a usable EXIF date counts the assets",
        "whose true capture date is still recoverable from the file itself.\n",
        _table(("case", "assets"), cross),
        "\n## Sentinel rejections by day\n",
        "1970, 1980, 1904 and 1899 are software defaults and are never real",
        "here. 2001-01-01 is Apple's reference epoch but is also a plausible",
        "capture date; inspect that bucket individually before trusting it.\n",
        _table(("source", "day", "assets"), sentinels),
        "\n## Date agreement\n",
        _table(
            ("measure", "count"),
            [
                ("assets whose surviving candidates disagree", disagreements),
                ("assets with no surviving candidate", no_candidate),
                (
                    "photos_date after date_added by under 24h (timezone artifact)",
                    tz_artifacts,
                ),
            ],
        ),
        "\n## Import spikes (date_added clustered hourly, 50+ assets)\n",
        "Large clusters are bulk imports. The cohort whose dates collapsed to",
        "import time will appear here.\n",
        _table(("hour", "assets"), clusters[:40]),
        "\n## Capture-date spikes (photos_date clustered hourly, 50+ assets)\n",
        "The section above marks every bulk import, the good ones included.",
        "This one clusters the CAPTURE date instead, which real photography",
        "never piles hundreds deep into a single hour. A large cluster here is",
        "the cohort whose capture dates were overwritten with import time.\n",
        _table(("hour", "assets"), capture_clusters[:40]),
    ]
    return "\n".join(parts)


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


def exact_components(conn: Connection) -> tuple[int, int, int]:
    """Merge overlapping exact-tier groups into connected components.

    A pair found by two signals sits in two groups; summing per-signal
    counts would count it twice. Union-find over memberships gives the true
    (components, assets, removable) where removable = assets - components.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    rows = conn.execute(
        """
        SELECT m.group_id AS gid, m.uuid AS uuid
        FROM dup_member m JOIN dup_group g ON g.id = m.group_id
        WHERE g.tier = 'exact' ORDER BY m.group_id
        """
    ).fetchall()
    by_group: dict[int, list[str]] = {}
    for r in rows:
        parent.setdefault(r["uuid"], r["uuid"])
        by_group.setdefault(r["gid"], []).append(r["uuid"])
    for members in by_group.values():
        for other in members[1:]:
            union(members[0], other)
    roots = {find(u) for u in parent}
    assets = len(parent)
    return (len(roots), assets, assets - len(roots))


def duplicate_report(conn: Connection) -> str:
    totals = group_totals(conn)
    exact_removable = sum(t[4] for t in totals if t[1] == "exact")
    all_removable = sum(t[4] for t in totals)
    components, comp_assets, comp_removable = exact_components(conn)

    parts = [
        "# Duplicate Report\n",
        "Groups are formed per signal, so one set of assets can appear as both",
        "a `scan` group and a `pixel` group. Those are two views of one fact.",
        "Only `exact` tiers are ever safe to resolve automatically.\n",
        f"- Removable on exact signals, de-duplicated across signals: "
        f"**{comp_removable}** ({comp_assets} assets in {components} sets)",
        f"- Removable on exact signals, summed per signal (over-counts pairs "
        f"found by two signals): **{exact_removable}**",
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
