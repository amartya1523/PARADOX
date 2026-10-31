"""Hypertable: a time-chunked columnar store for telemetry.

Modelled on the TimescaleDB hypertable idea:

* rows are partitioned into fixed-width time chunks (``chunk = floor(t / w)``),
  so a range query touches only the chunk ids in ``[floor(t0/w), floor(t1/w)]``;
* each chunk stores its columns as contiguous numpy arrays sorted by
  ``(entity, metric, t)``, so a point series is two ``searchsorted`` calls;
* each chunk keeps a zone map (entity bounds) for an extra pruning step;
* rollups ("continuous aggregates") keep ``sum/count/min/max`` per bucket and
  are invalidated per chunk, not globally.

Strings are dictionary-encoded. A row costs 18 bytes:
``float64 t | int32 entity | int16 metric | float32 value``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Chunk:
    start: float
    t: np.ndarray
    entity: np.ndarray
    metric: np.ndarray
    value: np.ndarray
    key: np.ndarray = field(default=None)  # entity * 2**16 + metric, sorted
    pending: list = field(default_factory=list)

    def seal(self) -> None:
        if self.pending:
            parts = [(self.t, self.entity, self.metric, self.value)] + self.pending
            self.t, self.entity, self.metric, self.value = (
                np.concatenate([p[i] for p in parts]) for i in range(4))
            self.pending = []
        order = np.lexsort((self.t, self.metric, self.entity))
        self.t, self.entity, self.metric, self.value = (
            self.t[order], self.entity[order], self.metric[order], self.value[order])
        self.key = self.entity.astype(np.int64) * 65536 + self.metric.astype(np.int64)

    @property
    def rows(self) -> int:
        return len(self.t) + sum(len(p[0]) for p in self.pending)


class Hypertable:
    def __init__(self, chunk_minutes: float = 60.0):
        self.chunk_minutes = chunk_minutes
        self.chunks: dict[int, Chunk] = {}
        self._zone: dict[int, tuple[int, int]] = {}
        self.entities: dict[str, int] = {}
        self.entity_names: list[str] = []
        self.metrics: dict[str, int] = {}
        self.metric_names: list[str] = []
        self._rollups: dict[tuple, dict[int, tuple]] = {}

    def entity_id(self, name: str) -> int:
        if name not in self.entities:
            self.entities[name] = len(self.entity_names)
            self.entity_names.append(name)
        return self.entities[name]

    def metric_id(self, name: str) -> int:
        if name not in self.metrics:
            self.metrics[name] = len(self.metric_names)
            self.metric_names.append(name)
        return self.metrics[name]

    def insert(self, t, entity, metric, value) -> int:
        """Vectorised batch insert. ``entity``/``metric`` are encoded ids."""
        t = np.asarray(t, np.float64)
        entity = np.broadcast_to(np.asarray(entity, np.int32), t.shape).copy()
        metric = np.broadcast_to(np.asarray(metric, np.int16), t.shape).copy()
        value = np.asarray(value, np.float32)
        if len(t) == 0:
            return 0
        cid = np.floor(t / self.chunk_minutes).astype(np.int64)
        order = np.argsort(cid, kind="stable")
        cid, t, entity, metric, value = cid[order], t[order], entity[order], metric[order], value[order]
        bounds = np.flatnonzero(np.diff(cid)) + 1
        for lo, hi in zip(np.r_[0, bounds], np.r_[bounds, len(cid)]):
            c = int(cid[lo])
            part = (t[lo:hi], entity[lo:hi], metric[lo:hi], value[lo:hi])
            if c in self.chunks:
                self.chunks[c].pending.append(part)
            else:
                self.chunks[c] = Chunk(c * self.chunk_minutes, *part)
                self.chunks[c].pending = []
            ch = self.chunks[c]
            emin, emax = int(part[1].min()), int(part[1].max())
            if c in self._zone:
                emin, emax = min(emin, self._zone[c][0]), max(emax, self._zone[c][1])
            self._zone[c] = (emin, emax)
            ch.key = None
            for r in self._rollups.values():
                r.pop(c, None)
        return len(t)

    def _sealed(self, c: int) -> Chunk:
        ch = self.chunks[c]
        if ch.key is None or ch.pending:
            ch.seal()
        return ch

    def _chunk_ids(self, t0: float, t1: float, e: int | None = None) -> list[int]:
        c0 = math.floor(t0 / self.chunk_minutes) if math.isfinite(t0) else min(self.chunks, default=0)
        c1 = math.floor(t1 / self.chunk_minutes) if math.isfinite(t1) else max(self.chunks, default=0)
        out = []
        for c in range(c0, c1 + 1):
            if c not in self.chunks:
                continue
            if e is not None:
                lo, hi = self._zone[c]
                if e < lo or e > hi:
                    continue  # zone-map pruning
            out.append(c)
        return out

    def query(self, entity: str, metric: str, t0: float = -math.inf,
              t1: float = math.inf) -> tuple[np.ndarray, np.ndarray]:
        e = self.entities.get(entity)
        m = self.metrics.get(metric)
        if e is None or m is None:
            return np.zeros(0), np.zeros(0, np.float32)
        k = e * 65536 + m
        ts, vs = [], []
        for c in self._chunk_ids(t0, t1, e):
            ch = self._sealed(c)
            lo, hi = np.searchsorted(ch.key, k, "left"), np.searchsorted(ch.key, k, "right")
            if lo == hi:
                continue
            tt = ch.t[lo:hi]
            a, b = np.searchsorted(tt, t0, "left"), np.searchsorted(tt, t1, "right")
            ts.append(tt[a:b])
            vs.append(ch.value[lo:hi][a:b])
        if not ts:
            return np.zeros(0), np.zeros(0, np.float32)
        return np.concatenate(ts), np.concatenate(vs)

    def chunks_scanned(self, entity: str, t0: float, t1: float) -> int:
        return len(self._chunk_ids(t0, t1, self.entities.get(entity, -1)))

    def rollup(self, bucket_minutes: float, metric: str) -> dict:
        """Continuous aggregate: mean/min/max/count per (bucket, entity) for a metric."""
        if metric not in self.metrics:
            return {"bucket": np.zeros(0), "entity": np.zeros(0, int), "mean": np.zeros(0),
                    "min": np.zeros(0), "max": np.zeros(0), "count": np.zeros(0, int)}
        m = self.metrics[metric]
        cache = self._rollups.setdefault((bucket_minutes, metric), {})
        ne = len(self.entity_names) + 1
        for c in self.chunks:
            if c in cache:
                continue
            ch = self._sealed(c)
            sel = ch.metric == m
            k = np.floor(ch.t[sel] / bucket_minutes).astype(np.int64) * ne + ch.entity[sel]
            v = ch.value[sel].astype(np.float64)
            uk, inv = np.unique(k, return_inverse=True)
            mn = np.full(len(uk), np.inf)
            mx = np.full(len(uk), -np.inf)
            np.minimum.at(mn, inv, v)
            np.maximum.at(mx, inv, v)
            cache[c] = (uk, np.bincount(inv, weights=v, minlength=len(uk)),
                        np.bincount(inv, minlength=len(uk)), mn, mx)
        parts = [cache[c] for c in sorted(cache) if len(cache[c][0])]
        if not parts:
            return self.rollup(bucket_minutes, "__none__")
        k = np.concatenate([p[0] for p in parts])
        uk, inv = np.unique(k, return_inverse=True)
        s = np.bincount(inv, weights=np.concatenate([p[1] for p in parts]))
        n = np.bincount(inv, weights=np.concatenate([p[2] for p in parts]))
        mn = np.full(len(uk), np.inf)
        mx = np.full(len(uk), -np.inf)
        np.minimum.at(mn, inv, np.concatenate([p[3] for p in parts]))
        np.maximum.at(mx, inv, np.concatenate([p[4] for p in parts]))
        return {"bucket": (uk // ne) * bucket_minutes, "entity": uk % ne, "mean": s / n,
                "min": mn, "max": mx, "count": n.astype(int)}

    @property
    def rows(self) -> int:
        return sum(c.rows for c in self.chunks.values())

    def stats(self) -> dict:
        rows = self.rows
        return {"rows": rows, "chunks": len(self.chunks), "entities": len(self.entity_names),
                "metrics": list(self.metric_names), "bytes": rows * 18,
                "chunk_minutes": self.chunk_minutes}
