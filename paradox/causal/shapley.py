"""Shapley attribution of a continuous impact to root events.

v(S) = KPI(world in which only the roots in S occur) - KPI(world with none).
Each coalition is a counterfactual world, built incrementally from the
record. Attribution is exact up to 7 players and uses permutation sampling
beyond that. Root events that depend on each other (a generator only
fails to start because the substation failed) share an interaction term,
and Shapley splits it evenly. The report says so, rather than presenting
the split as a law of nature.
"""

from __future__ import annotations

import itertools
import math

import numpy as np


def shapley(value, players: list, exact_max: int = 7, samples: int = 200, seed: int = 0) -> dict:
    n = len(players)
    cache = {}

    def v(coalition: frozenset) -> float:
        if coalition not in cache:
            cache[coalition] = value(coalition)
        return cache[coalition]

    phi = {p: 0.0 for p in players}
    if n <= exact_max:
        for p in players:
            others = [q for q in players if q != p]
            for k in range(n):
                w = math.factorial(k) * math.factorial(n - k - 1) / math.factorial(n)
                for S in itertools.combinations(others, k):
                    S = frozenset(S)
                    phi[p] += w * (v(S | {p}) - v(S))
    else:
        rng = np.random.default_rng(seed)
        for _ in range(samples):
            perm = list(rng.permutation(players))
            S = frozenset()
            for p in perm:
                phi[p] += (v(S | {p}) - v(S)) / samples
                S = S | {p}
    total = v(frozenset(players)) - v(frozenset())
    return {"phi": phi, "total": total, "evaluations": len(cache),
            "efficiency_gap": abs(sum(phi.values()) - total)}


def attribute(px, metric: str, branch: str = "main") -> dict:
    """ATTRIBUTE <metric>: split the excess over the root-free world among root events."""
    from .actual import root_candidates

    world = px.world(branch)
    roots = root_candidates(world)
    by_key = {c.key: c for c in roots}
    worlds = {}

    def value(coalition: frozenset) -> float:
        off = tuple(iv for k, c in by_key.items() if k not in coalition for iv in c.neutraliser)
        w = px.sim.run(tuple(world.interventions) + off, noise=world.noise.fork_same(), base=world)
        worlds[coalition] = w
        return w.kpis[metric]

    res = shapley(value, list(by_key))
    base = res["total"]
    rows = [{"key": k, "label": by_key[k].label, "phi": round(v, 3),
             "share": round(v / base, 3) if base else None} for k, v in
            sorted(res["phi"].items(), key=lambda kv: -abs(kv[1]))]
    return {"metric": metric, "branch": branch, "actual": world.kpis[metric],
            "root_free": round(worlds[frozenset()].kpis[metric], 3), "excess": round(base, 3),
            "attribution": rows, "evaluations": res["evaluations"],
            "efficiency_gap": round(res["efficiency_gap"], 6),
            "note": "Exact Shapley over root events; interactions between dependent roots are split evenly."}
