"""TemporalGraph: the dependency graph *as it existed* at any instant.

Edges carry a validity interval ``[valid_from, valid_to)`` so topology can
change over time (a tie switch closes and a feeder is suddenly fed from a
different substation; a dispatch centre fails over). Node state is a delta
log ``(t, node, state)`` derived from the event store.

``state_as_of(t)`` uses the snapshot-plus-replay scheme from the historical
graph literature (DeltaGraph, Khurana & Deshpande 2013): a full checkpoint
every K deltas, so a reconstruction loads the nearest checkpoint at or
before ``t`` and replays at most K deltas. K defaults to sqrt(N), which
balances checkpoint storage against replay cost.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict, deque

import networkx as nx
import numpy as np

# Rinaldi, Peerenboom & Kelly (2001) dependency classes
DEPENDENCY_CLASS = {
    "POWERS": "physical",
    "BACKS_UP": "physical",
    "SUPPLIES_WATER": "physical",
    "TIES": "physical",
    "CONTROLS": "cyber",
    "RELAYS_CALLS": "cyber",
    "DISPATCHES": "logical",
    "ROUTES_THROUGH": "geographic",
    "SERVES": "logical",
}


class TemporalGraph:
    def __init__(self):
        self.node_attrs: dict[str, dict] = {}
        self._src: list[str] = []
        self._dst: list[str] = []
        self._rel: list[str] = []
        self._meta: list[dict] = []
        self._vf = np.zeros(0)
        self._vt = np.zeros(0)
        self._pending_vf: list[float] = []
        self._pending_vt: list[float] = []
        self._deltas: list[tuple[float, int, str, str, str]] = []  # (t, seq, node, state, eid)
        self._delta_t: list[float] = []
        self._checkpoints: list[tuple[int, dict]] = []  # (delta index, state dict)
        self._checkpoint_idx: list[int] = []
        self.checkpoint_every = 0
        self._dirty = True
        self._out = defaultdict(list)
        self._in = defaultdict(list)

    # -- building -------------------------------------------------------------
    def add_node(self, node: str, **attrs) -> None:
        self.node_attrs[node] = attrs

    def add_edge(self, src: str, dst: str, rel: str, valid_from: float = -math.inf,
                 valid_to: float = math.inf, **meta) -> None:
        i = len(self._src)
        self._src.append(src)
        self._dst.append(dst)
        self._rel.append(rel)
        self._meta.append(meta)
        self._pending_vf.append(valid_from)
        self._pending_vt.append(valid_to)
        self._out[src].append(i)
        self._in[dst].append(i)

    def add_delta(self, t: float, node: str, state: str, eid: str = "", seq: int | None = None) -> None:
        """Record a state change. ``seq`` orders same-instant deltas causally."""
        self._deltas.append((t, len(self._deltas) if seq is None else seq, node, state, eid))
        self._dirty = True

    def _flush(self) -> None:
        if self._pending_vf:
            self._vf = np.concatenate([self._vf, np.array(self._pending_vf, float)])
            self._vt = np.concatenate([self._vt, np.array(self._pending_vt, float)])
            self._pending_vf.clear()
            self._pending_vt.clear()

    def build_index(self, checkpoint_every: int | None = None) -> None:
        self._flush()
        self._deltas.sort(key=lambda d: (d[0], d[1]))
        self._delta_t = [d[0] for d in self._deltas]
        n = len(self._deltas)
        k = checkpoint_every or max(16, int(math.sqrt(max(n, 1))))
        self.checkpoint_every = k
        self._checkpoints = []
        state: dict[str, str] = {}
        for i, (_, _, node, s, _) in enumerate(self._deltas):
            if i % k == 0:
                self._checkpoints.append((i, dict(state)))
            state[node] = s
        self._checkpoint_idx = [c[0] for c in self._checkpoints]
        self._dirty = False

    # -- as-of queries --------------------------------------------------------
    def state_as_of(self, t: float, use_checkpoints: bool = True) -> dict[str, str]:
        """Node states in effect at time ``t`` (deltas with time <= t applied)."""
        if self._dirty:
            self.build_index(self.checkpoint_every or None)
        end = bisect.bisect_right(self._delta_t, t)
        if use_checkpoints and self._checkpoints:
            ci = bisect.bisect_right(self._checkpoint_idx, end) - 1
            start, base = self._checkpoints[max(ci, 0)]
            state = dict(base)
        else:
            start, state = 0, {}
        for i in range(start, end):
            _, _, node, s, _ = self._deltas[i]
            state[node] = s
        return state

    def state_of(self, node: str, t: float) -> str | None:
        """State of one node; ``None`` if the graph has never heard of it."""
        if self._dirty:
            self.build_index(self.checkpoint_every or None)
        state = None if node not in self.node_attrs else "ok"
        end = bisect.bisect_right(self._delta_t, t)
        for i in range(end - 1, -1, -1):
            if self._deltas[i][2] == node:
                return self._deltas[i][3]
        return state

    def edge_mask(self, t: float) -> np.ndarray:
        self._flush()
        return (self._vf <= t) & (t < self._vt)

    def edges_as_of(self, t: float, rel: str | None = None) -> list[tuple[str, str, str, dict]]:
        mask = self.edge_mask(t)
        return [(self._src[i], self._dst[i], self._rel[i], self._meta[i])
                for i in np.flatnonzero(mask) if rel is None or self._rel[i] == rel]

    def as_of(self, t: float) -> nx.MultiDiGraph:
        g = nx.MultiDiGraph(as_of=t)
        state = self.state_as_of(t)
        for n, a in self.node_attrs.items():
            g.add_node(n, **a, state=state.get(n, "ok"))
        for s, d, r, m in self.edges_as_of(t):
            g.add_edge(s, d, rel=r, cls=DEPENDENCY_CLASS.get(r, "logical"), **m)
        return g

    def _walk(self, start: str, t: float, depth: int, forward: bool, rels=None) -> list[dict]:
        mask = self.edge_mask(t)
        adj = self._out if forward else self._in
        seen, out = {start}, []
        q = deque([(start, 0)])
        while q:
            node, d = q.popleft()
            if d >= depth:
                continue
            for i in adj.get(node, ()):
                if not mask[i] or (rels and self._rel[i] not in rels):
                    continue
                nxt = self._dst[i] if forward else self._src[i]
                if nxt in seen:
                    continue
                seen.add(nxt)
                out.append({"node": nxt, "via": node, "rel": self._rel[i], "depth": d + 1,
                            "class": DEPENDENCY_CLASS.get(self._rel[i], "logical")})
                q.append((nxt, d + 1))
        return out

    def dependents(self, node: str, t: float, depth: int = 4, rels=None) -> list[dict]:
        """Everything that (transitively) depends on ``node`` at time ``t``."""
        return self._walk(node, t, depth, True, rels)

    def dependencies(self, node: str, t: float, depth: int = 4, rels=None) -> list[dict]:
        return self._walk(node, t, depth, False, rels)

    def stats(self) -> dict:
        self._flush()
        return {"nodes": len(self.node_attrs), "edges": len(self._src),
                "time_varying_edges": int(np.sum(np.isfinite(self._vf) | np.isfinite(self._vt))),
                "deltas": len(self._deltas), "checkpoints": len(self._checkpoints),
                "checkpoint_every": self.checkpoint_every}
