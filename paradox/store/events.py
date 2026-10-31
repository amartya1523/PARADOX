"""ChronoStore: a content-addressed, causally-hashed, bitemporal event store.

Three ideas carry the design.

1. **Causal content addressing.** An event's id is the hash of its content
   *and of the ids of its causes*. The log is therefore a Merkle DAG over
   causality: two timelines contain the same event iff they agree on its
   whole causal past. Diffing two branches is a set difference, and that
   difference *is* the causal footprint of whatever separates them.

2. **Branches as overlays.** A branch is ``(parent, fork_t, fork_tx)`` plus
   its own segment of events at or after ``fork_t``. Reading a branch merges
   the parent's prefix (as the parent was known at ``fork_tx``) with its own
   segment. Events are stored once in a shared object store, so a what-if
   timeline costs only the references it adds.

3. **Bitemporality.** Every event has a valid time ``t`` (when it happened
   in the simulated world) and a list of transaction-time intervals
   ``[assert_tx, retract_tx)`` (when the database believed it). Corrections
   never mutate: they retract and re-assert in one transaction, so
   ``known_at=`` can always answer "what did we believe then?".

Each branch keeps a hash chain over its commits. A branch's genesis commits
to the parent's chain *at fork time*, so a branch cryptographically pins
the history it shares with its parent.
"""

from __future__ import annotations

import bisect
import gzip
import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

import numpy as np

GENESIS = "0" * 32
T_RESOLUTION = 4  # decimals of a minute kept in valid time


def _plain(obj):
    """Coerce to a canonical JSON-able value; reject anything ambiguous."""
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        if not math.isfinite(f):
            return repr(f)
        return round(f, 6)
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    raise TypeError(f"cannot canonically encode {type(obj).__name__}: {obj!r}")


def canonical(obj) -> str:
    return json.dumps(_plain(obj), sort_keys=True, separators=(",", ":"))


def _h(*parts: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    for p in parts:
        b = p.encode()
        h.update(len(b).to_bytes(4, "little"))
        h.update(b)
    return h.hexdigest()


def round_t(t: float) -> float:
    return round(float(t), T_RESOLUTION)


class Event:
    """An immutable fact about the simulated world."""

    __slots__ = ("t", "kind", "entity", "attrs", "causes", "rule", "eid")

    def __init__(self, t: float, kind: str, entity: str, attrs: dict | None = None,
                 causes: Iterable[str] = (), rule: str = ""):
        object.__setattr__(self, "t", round_t(t))
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "entity", entity)
        object.__setattr__(self, "attrs", _plain(dict(attrs or {})))
        object.__setattr__(self, "causes", tuple(sorted(set(c for c in causes if c))))
        object.__setattr__(self, "rule", rule)
        object.__setattr__(self, "eid", _h(canonical(
            [self.t, kind, entity, self.attrs, list(self.causes), rule])))

    def __setattr__(self, *_):
        raise AttributeError("events are immutable")

    def same_content(self, other: "Event") -> bool:
        return (self.t, self.kind, self.entity, self.attrs, self.causes, self.rule) == \
               (other.t, other.kind, other.entity, other.attrs, other.causes, other.rule)

    def to_dict(self) -> dict:
        return {"eid": self.eid, "t": self.t, "kind": self.kind, "entity": self.entity,
                "attrs": self.attrs, "causes": list(self.causes), "rule": self.rule}

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        ev = cls(d["t"], d["kind"], d["entity"], d["attrs"], d["causes"], d["rule"])
        if ev.eid != d["eid"]:
            raise ValueError(f"content hash mismatch for event {d['eid']}")
        return ev

    def __repr__(self) -> str:
        return f"<{self.eid[:8]} t={self.t:.2f} {self.kind} {self.entity}>"


@dataclass
class Branch:
    name: str
    parent: "Branch | None"
    fork_t: float
    fork_tx: int
    fork_chain_len: int
    genesis: str
    meta: dict = field(default_factory=dict)
    created_tx: int = 0
    _keys: list = field(default_factory=list)                         # sorted (t, eid)
    _vis: dict = field(default_factory=lambda: defaultdict(list))     # eid -> [[assert, retract]]
    _log: list = field(default_factory=list)                          # [(tx, op, eid)]
    _chain: list = field(default_factory=list)
    _by_entity: dict = field(default_factory=lambda: defaultdict(list))

    @property
    def head(self) -> str:
        return self._chain[-1] if self._chain else self.genesis

    def head_at(self, tx: int) -> tuple[int, str]:
        """(chain length, head) as of transaction ``tx``."""
        n = bisect.bisect_right([e[0] for e in self._log], tx)
        return n, (self._chain[n - 1] if n else self.genesis)

    @property
    def own_count(self) -> int:
        return len(self._keys)

    def lineage(self) -> list[str]:
        out, b = [], self
        while b is not None:
            out.append(b.name)
            b = b.parent
        return out

    def visible(self, eid: str, known_at: int) -> bool:
        for a, r in self._vis.get(eid, ()):
            if a <= known_at and (r is None or r > known_at):
                return True
        return False

    def is_live(self, eid: str) -> bool:
        iv = self._vis.get(eid)
        return bool(iv) and iv[-1][1] is None


class ChronoStore:
    """The embedded causal-temporal event database."""

    def __init__(self):
        self.objects: dict[str, Event] = {}
        self.branches: dict[str, Branch] = {}
        self.tx = 0
        self.branches["main"] = Branch("main", None, -math.inf, 0, 0, GENESIS)

    # -- branches -----------------------------------------------------------
    def branch(self, name: str) -> Branch:
        try:
            return self.branches[name]
        except KeyError:
            raise KeyError(f"no such branch: {name!r}") from None

    def create_branch(self, name: str, parent: str = "main", fork_t: float = 0.0,
                      meta: dict | None = None) -> Branch:
        if name in self.branches:
            raise ValueError(f"branch {name!r} already exists")
        p = self.branch(parent)
        fork_t = round_t(fork_t)
        if fork_t < p.fork_t:
            raise ValueError("a branch cannot fork before its parent's fork point")
        n, phead = len(p._chain), p.head
        self.tx += 1
        genesis = _h("branch", parent, str(n), phead, repr(fork_t), name)
        b = Branch(name, p, fork_t, self.tx, n, genesis, meta=dict(meta or {}), created_tx=self.tx)
        self.branches[name] = b
        return b

    def drop_branch(self, name: str) -> None:
        if name == "main":
            raise ValueError("main is the record of reality and cannot be dropped")
        target = self.branch(name)
        if any(b.parent is target for b in self.branches.values()):
            raise ValueError("branch has children")
        self.branches.pop(name)

    # -- writes ---------------------------------------------------------------
    def _commit(self, b: Branch, tx: int, op: str, eid: str) -> None:
        b._log.append((tx, op, eid))
        b._chain.append(_h(b.head, str(tx), op, eid))

    def _intern(self, ev: Event) -> None:
        old = self.objects.get(ev.eid)
        if old is None:
            self.objects[ev.eid] = ev
        elif not old.same_content(ev):
            raise RuntimeError(f"event id collision on {ev.eid}")

    def _assert(self, b: Branch, ev: Event, tx: int) -> bool:
        if ev.t < b.fork_t:
            raise ValueError(f"event at t={ev.t} precedes fork point {b.fork_t} of {b.name!r}")
        if b.is_live(ev.eid):
            return False  # idempotent re-assertion
        self._intern(ev)
        if ev.eid not in b._vis:
            bisect.insort(b._keys, (ev.t, ev.eid))
            b._by_entity[ev.entity].append(ev.eid)
        b._vis[ev.eid].append([tx, None])
        self._commit(b, tx, "+", ev.eid)
        return True

    def _retract(self, b: Branch, eid: str, tx: int) -> None:
        if not b.is_live(eid):
            raise KeyError(f"{eid} is not live on {b.name!r}")
        b._vis[eid][-1][1] = tx
        self._commit(b, tx, "-", eid)

    def append(self, branch: str, events: Iterable[Event]) -> int:
        """Commit a batch of events to a branch as one transaction."""
        b = self.branch(branch)
        batch = sorted(events, key=lambda e: (e.t, e.eid))
        for ev in batch:  # validate before touching anything
            if ev.t < b.fork_t:
                raise ValueError(f"event at t={ev.t} precedes fork point {b.fork_t} of {branch!r}")
        if not batch:
            return self.tx
        self.tx += 1
        for ev in batch:
            self._assert(b, ev, self.tx)
        return self.tx

    def retract(self, branch: str, eid: str) -> int:
        b = self.branch(branch)
        self.tx += 1
        self._retract(b, eid, self.tx)
        return self.tx

    def correct(self, branch: str, eid: str, **changes) -> Event:
        """Bitemporal correction: retract ``eid`` and assert a revised copy, atomically."""
        b = self.branch(branch)
        old = self.objects[eid]
        fields = {"t": old.t, "kind": old.kind, "entity": old.entity,
                  "attrs": dict(old.attrs), "causes": old.causes, "rule": old.rule}
        attrs = changes.pop("attrs", None)
        fields.update(changes)
        if attrs:
            fields["attrs"].update(attrs)
        fields["attrs"]["corrects"] = eid
        new = Event(**fields)
        if new.t < b.fork_t:
            raise ValueError("a correction cannot move an event before the branch fork point")
        if not b.is_live(eid):
            raise KeyError(f"{eid} is not live on {branch!r}")
        self.tx += 1
        self._retract(b, eid, self.tx)
        self._assert(b, new, self.tx)
        return new

    # -- reads ----------------------------------------------------------------
    def _own_range(self, b: Branch, t0: float, t1: float, known_at: int) -> Iterator[str]:
        lo = bisect.bisect_left(b._keys, (t0, ""))
        for t, eid in b._keys[lo:]:
            if t > t1:
                break
            if b.visible(eid, known_at):
                yield eid

    def iter_ids(self, branch: str, t0: float = -math.inf, t1: float = math.inf,
                 known_at: int | None = None) -> Iterator[str]:
        """Event ids visible on ``branch`` in valid-time order."""
        b = self.branch(branch)
        known = self.tx if known_at is None else known_at
        chunks = []
        cur, upper, k = b, t1, known
        while cur is not None:
            lo = max(t0, cur.fork_t)
            if lo <= upper:
                chunks.append(list(self._own_range(cur, lo, upper, k)))
            # the parent is visible strictly before the fork, as known at fork time
            upper = min(upper, cur.fork_t - 10 ** -(T_RESOLUTION + 1))
            k = min(k, cur.fork_tx)
            cur = cur.parent
        for chunk in reversed(chunks):
            yield from chunk

    def events(self, branch: str, t0: float = -math.inf, t1: float = math.inf,
               kind: str | None = None, entity: str | None = None,
               known_at: int | None = None) -> list[Event]:
        if entity is not None:
            return self._entity_events(branch, entity, t0, t1, kind, known_at)
        out = []
        for eid in self.iter_ids(branch, t0, t1, known_at):
            ev = self.objects[eid]
            if kind is None or ev.kind.startswith(kind):
                out.append(ev)
        return out

    def _entity_events(self, branch, entity, t0, t1, kind, known_at):
        b = self.branch(branch)
        known = self.tx if known_at is None else known_at
        out, cur, upper, k = [], b, t1, known
        while cur is not None:
            lo = max(t0, cur.fork_t)
            for eid in cur._by_entity.get(entity, ()):
                ev = self.objects[eid]
                if lo <= ev.t <= upper and cur.visible(eid, k) and \
                        (kind is None or ev.kind.startswith(kind)):
                    out.append(ev)
            upper = min(upper, cur.fork_t - 10 ** -(T_RESOLUTION + 1))
            k = min(k, cur.fork_tx)
            cur = cur.parent
        return sorted(out, key=lambda e: (e.t, e.eid))

    def get(self, eid: str) -> Event:
        return self.objects[eid]

    def id_set(self, branch: str, known_at: int | None = None) -> set[str]:
        return set(self.iter_ids(branch, known_at=known_at))

    def head(self, branch: str) -> str:
        return self.branch(branch).head

    # -- integrity ------------------------------------------------------------
    def verify(self, branch: str) -> dict:
        """Recompute hash chains up the lineage and every object's content hash."""
        problems = []
        b = self.branch(branch)
        while b is not None:
            if b.parent is not None:
                p = b.parent
                phead = p._chain[b.fork_chain_len - 1] if b.fork_chain_len else p.genesis
                if b.genesis != _h("branch", p.name, str(b.fork_chain_len), phead,
                                   repr(b.fork_t), b.name):
                    problems.append(f"{b.name}: genesis does not commit to parent chain")
            prev = b.genesis
            if len(b._log) != len(b._chain):
                problems.append(f"{b.name}: log/chain length mismatch")
            for (tx, op, eid), expected in zip(b._log, b._chain):
                prev = _h(prev, str(tx), op, eid)
                if prev != expected:
                    problems.append(f"{b.name}: chain broken at tx {tx}")
                    break
                ev = self.objects.get(eid)
                if ev is None or Event(ev.t, ev.kind, ev.entity, ev.attrs, ev.causes,
                                       ev.rule).eid != eid:
                    problems.append(f"{b.name}: object {eid[:10]} fails its content hash")
                    break
            b = b.parent
        return {"ok": not problems, "problems": problems}

    # -- comparison -----------------------------------------------------------
    def diff(self, a: str, b: str, known_at: int | None = None) -> dict:
        """Symmetric difference of two timelines, both pinned to the same ``known_at``."""
        k = self.tx if known_at is None else known_at
        ia, ib = self.id_set(a, k), self.id_set(b, k)
        key = lambda e: (self.objects[e].t, e)
        return {"only_a": sorted(ia - ib, key=key), "only_b": sorted(ib - ia, key=key),
                "shared": len(ia & ib), "known_at": k}

    def lineage(self, eid: str, branch: str | None = None, depth: int = 12,
                known_at: int | None = None) -> dict:
        """Why-provenance: the causal ancestry DAG of an event (BFS, visibility-aware)."""
        visible = self.id_set(branch, known_at) if branch else None
        nodes, edges, level = {}, [], {eid: 0}
        frontier = [eid]
        while frontier:
            nxt = []
            for cur in frontier:
                if cur in nodes or cur not in self.objects:
                    continue
                if visible is not None and cur not in visible:
                    continue
                ev = self.objects[cur]
                nodes[cur] = ev
                if level[cur] >= depth:
                    continue
                for c in ev.causes:
                    edges.append((c, cur))
                    if c not in level:
                        level[c] = level[cur] + 1
                        nxt.append(c)
            frontier = nxt
        edges = [(a, b) for a, b in edges if a in nodes]
        return {"nodes": nodes, "edges": edges, "depth": level}

    def stats(self) -> dict:
        refs = sum(b.own_count for b in self.branches.values())
        return {"objects": len(self.objects), "references": refs, "tx": self.tx,
                "branches": {n: {"own": b.own_count, "parent": b.parent.name if b.parent else None,
                                 "fork_t": b.fork_t if math.isfinite(b.fork_t) else None,
                                 "head": b.head[:12]}
                             for n, b in self.branches.items()},
                "dedup_ratio": round(refs / max(1, len(self.objects)), 3)}

    # -- persistence ----------------------------------------------------------
    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        with gzip.open(path / "objects.jsonl.gz", "wt") as f:
            for ev in self.objects.values():
                f.write(canonical(ev.to_dict()) + "\n")
        order = []
        for name, b in self.branches.items():
            order.append({"name": name, "parent": b.parent.name if b.parent else None,
                          "fork_t": b.fork_t if math.isfinite(b.fork_t) else None,
                          "fork_tx": b.fork_tx, "fork_chain_len": b.fork_chain_len,
                          "genesis": b.genesis, "meta": b.meta, "created_tx": b.created_tx,
                          "log": b._log, "chain": b._chain})
        (path / "branches.json").write_text(json.dumps({"tx": self.tx, "branches": order}))

    @classmethod
    def load(cls, path: str | Path) -> "ChronoStore":
        path = Path(path)
        st = cls()
        with gzip.open(path / "objects.jsonl.gz", "rt") as f:
            for line in f:
                ev = Event.from_dict(json.loads(line))
                st.objects[ev.eid] = ev
        meta = json.loads((path / "branches.json").read_text())
        st.branches.clear()
        for d in meta["branches"]:
            parent = st.branches[d["parent"]] if d["parent"] else None
            fork_t = d["fork_t"] if d["fork_t"] is not None else -math.inf
            b = Branch(d["name"], parent, fork_t, d["fork_tx"], d["fork_chain_len"], d["genesis"],
                       d["meta"], d["created_tx"])
            for tx, op, eid in d["log"]:
                if op == "+":
                    if eid not in b._vis:
                        ev = st.objects[eid]
                        b._keys.append((ev.t, eid))
                        b._by_entity[ev.entity].append(eid)
                    b._vis[eid].append([tx, None])
                else:
                    b._vis[eid][-1][1] = tx
                b._log.append((tx, op, eid))
            b._keys.sort()
            b._chain = list(d["chain"])
            st.branches[b.name] = b
        st.tx = meta["tx"]
        return st
