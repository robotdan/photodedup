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
