"""Road network travel times under degraded signal states.

Emergency travel time on each arc follows a BPR link function,
t = t0 * (1 + alpha * (v/c)^beta). A dark signal turns its intersection
into an all-way stop. That cuts the capacity of every arc entering it to
``dark_cap`` of its signalised value and adds a stop delay.

Lights-and-sirens gives an advantage ``k_ev`` that fades to nothing as
v/c approaches 1, because a jammed road has nowhere to pull over.

The *road-state epoch* is the set of dark intersections plus a 15-minute
time-of-day bin. Every epoch's shortest-path tables are cached and shared
across branches and Monte Carlo replicates. Most counterfactuals change
only a few epochs, so most lookups never run Dijkstra.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from .generator import City


def _vc_profile(t: float) -> tuple[float, float]:
    """Background volume/capacity for (arterial, local) at minute-of-day t."""
    peak = np.exp(-((t - 1050.0) / 80.0) ** 2)      # 17:30 commute peak
    evening = np.exp(-((t - 1230.0) / 60.0) ** 2)   # 20:30 secondary bump
    return 0.42 + 0.33 * peak + 0.06 * evening, 0.28 + 0.15 * peak + 0.04 * evening


class RoadNet:
    TOD_BIN = 15.0

    def __init__(self, city: City, cache_size: int = 320):
        self.city = city
        self.n = len(city.node_xy)
        self.src = city.arc_src
        self.dst = city.arc_dst
        self.t0 = city.arc_len / (city.arc_speed * 1000.0 / 60.0)  # minutes
        self.art = city.arc_cls == 1
        self.station_nodes = [s["node"] for s in city.stations]
        self.hospital_nodes = [h["node"] for h in city.hospitals]
        self.fwd_sources = sorted(set(self.station_nodes) | set(self.hospital_nodes))
        self.fwd_row = {n: i for i, n in enumerate(self.fwd_sources)}
        self._cache: OrderedDict = OrderedDict()
        self.cache_size = cache_size
        self.hits = 0
        self.misses = 0

    def weights(self, dark_nodes: frozenset, t: float, p: dict) -> np.ndarray:
        vc_a, vc_l = _vc_profile(t)
        vc = np.where(self.art, vc_a, vc_l)
        dark = np.zeros(self.n, bool)
        if dark_nodes:
            dark[list(dark_nodes)] = True
        into_dark = dark[self.dst]
        vc_eff = np.where(into_dark, vc / p["dark_cap"], vc)
        x = np.minimum(vc_eff, p["vc_cap"])
        bpr = 1.0 + p["bpr_alpha"] * x ** p["bpr_beta"]
        k_ev = 1.0 + p["ev_gain"] * np.maximum(0.0, 1.0 - x)
        w = self.t0 * bpr / k_ev
        w = w + np.where(into_dark, p["dark_delay"] * (1.0 + x), 0.0)
        return w

    def epoch_key(self, dark_nodes: frozenset, t: float, p: dict) -> str:
        h = hashlib.blake2b(digest_size=12)
        h.update(np.array(sorted(dark_nodes), np.int32).tobytes())
        h.update(str(int(t // self.TOD_BIN)).encode())
        h.update(repr(tuple(p[k] for k in ("dark_cap", "vc_cap", "bpr_alpha", "bpr_beta",
                                             "ev_gain", "dark_delay"))).encode())
        return h.hexdigest()

    def tables(self, dark_nodes: frozenset, t: float, p: dict) -> dict:
        key = self.epoch_key(dark_nodes, t, p)
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            self.hits += 1
            return hit
        self.misses += 1
        tb = (int(t // self.TOD_BIN) + 0.5) * self.TOD_BIN
        w = self.weights(dark_nodes, tb, p)
        m = csr_matrix((w, (self.src, self.dst)), shape=(self.n, self.n))
        fd, fp = dijkstra(m, directed=True, indices=self.fwd_sources, return_predecessors=True)
        rd = dijkstra(m.T.tocsr(), directed=True, indices=self.hospital_nodes)
        out = {"fwd": fd.astype(np.float32), "pred": fp.astype(np.int32),
               "rev_h": rd.astype(np.float32), "key": key}
        self._cache[key] = out
        if len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return out

    def path(self, tab: dict, src_node: int, dst_node: int) -> list[int]:
        pred = tab["pred"][self.fwd_row[src_node]]
        out, cur = [dst_node], dst_node
        while cur != src_node and cur >= 0:
            cur = int(pred[cur])
            if cur < 0:
                break
            out.append(cur)
        return out[::-1]

    def cache_stats(self) -> dict:
        total = self.hits + self.misses
        return {"epochs_cached": len(self._cache), "hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else None}
