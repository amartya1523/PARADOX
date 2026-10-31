"""CORRELATE: tell a modelled dependency from a confounded association.

An ensemble of evenings with randomised root faults acts as a set of
natural experiments. Each substation fails independently with probability
q at a random time, and the luck is fresh. Over it, PARADOX:

1. measures the association between two entities' bad states (phi
   coefficient, lift);
2. checks the dependency DAG for a directed path in either direction;
3. if there is none, finds the common ancestors and a minimal d-separator Z;
4. re-measures the association *within strata* of the root faults among
   the common ancestors, a Mantel–Haenszel-style weighted phi.

If the association vanishes given Z, it is reported as *explained by
confounder Z*. If it survives with no modelled path, it is reported as a
hidden dependency or an unmodelled confounder, and the knockout route of
AUDIT applies.
"""

from __future__ import annotations

import networkx as nx
import numpy as np

from ..sim.interventions import Fault, Scenario
from ..sim.noise import NoiseTape, replicate_seed
from ..store.derive import static_graph


def dependency_dag(city) -> nx.DiGraph:
    tg = static_graph(city)
    g = nx.DiGraph()
    g.add_nodes_from(tg.node_attrs)
    for s, d, r, _ in tg.edges_as_of(0.0):
        if r == "TIES":
            continue
        if s.startswith("generator:") and d.startswith("fuelpump:"):
            continue  # emergency-bus loop is internal to the backup system; keep the DAG acyclic
        g.add_edge(s, d, rel=r)
    return g


def _bad(world, entity: str) -> bool:
    kind, ident = entity.split(":", 1)
    inf = world.infra
    if kind == "hospital":
        v = inf.value(entity)
        return bool(v["lost"]) or bool(v["diversion"])  # lost utility power, or diverting
    if kind == "feeder":
        return bool(inf.feeder(ident))
    if kind == "substation":
        return bool(inf.value(f"sub:{ident}"))
    if kind in ("signal", "tower", "zone", "dispatch"):
        return bool(inf.value(entity))
    raise ValueError(f"cannot correlate entity kind {kind!r}")


def _phi(x: np.ndarray, y: np.ndarray) -> float:
    if x.std() == 0 or y.std() == 0:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def correlate(px, a: str, b: str, runs: int = 60, q: float = 0.2, seed: int = 0) -> dict:
    city = px.city
    rng = np.random.default_rng(seed)
    subs = [s["id"] for s in city.substations]
    X, Y, R = [], [], []
    for r in range(runs):
        fails = [s for s in subs if rng.random() < q]
        faults = tuple(Fault(f"substation:{s}", float(rng.integers(17 * 60, 21 * 60))) for s in fails)
        sc = Scenario(f"corr-{r}", faults)
        w = px.sim.run(noise=NoiseTape(replicate_seed(seed + 77, r)), scenario=sc)
        X.append(_bad(w, a))
        Y.append(_bad(w, b))
        R.append(tuple(s in fails for s in subs))
    X, Y, R = np.array(X, float), np.array(Y, float), np.array(R)
    phi = _phi(X, Y)
    lift = float(np.mean(X * Y) / (np.mean(X) * np.mean(Y))) if X.mean() and Y.mean() else None
    g = dependency_dag(city)
    out = {"a": a, "b": b, "runs": runs, "fault_probability": q, "phi": round(phi, 3),
           "lift": round(lift, 3) if lift else None, "p_a": float(X.mean()), "p_b": float(Y.mean())}
    if a not in g or b not in g:
        out["verdict"] = "entity not in the dependency graph"
        return out
    if nx.has_path(g, a, b) or nx.has_path(g, b, a):
        src, dst = (a, b) if nx.has_path(g, a, b) else (b, a)
        path = nx.shortest_path(g, src, dst)
        out.update({"modelled_path": path, "relation": "direct" if len(path) == 2 else "mediated",
                    "verdict": f"modelled dependency ({'direct' if len(path) == 2 else 'mediated'}): "
                               + " → ".join(path)})
        return out
    common = nx.ancestors(g, a) & nx.ancestors(g, b)
    roots = sorted(c for c in common if c.startswith("substation:"))
    try:
        z = sorted(nx.find_minimal_d_separator(g, {a}, {b}) or [])
    except Exception:
        z = []
    out["common_ancestors"] = roots
    out["minimal_d_separator"] = z[:12]
    # stratify on the joint state of the common-ancestor substations
    idx = [subs.index(r.split(":")[1]) for r in roots]
    strata_phi, weights = [], []
    if idx:
        keys = [tuple(row[idx]) for row in R]
        for key in set(keys):
            m = np.array([k == key for k in keys])
            if m.sum() >= 5:
                strata_phi.append(_phi(X[m], Y[m]))
                weights.append(m.sum())
    within = float(np.average(strata_phi, weights=weights)) if weights else None
    out["phi_within_strata"] = round(within, 3) if within is not None else None
    if abs(phi) < 0.1:
        out["verdict"] = "no meaningful association and no modelled path"
    elif roots and within is not None and abs(within) < max(0.1, abs(phi) / 3):
        out["verdict"] = (f"association explained by confounder(s) {', '.join(roots)}: no modelled path between "
                          f"{a} and {b}; phi {phi:.2f} → {within:.2f} within strata")
    else:
        out["verdict"] = ("association survives stratification with no modelled path: hidden dependency or "
                          "unmodelled confounder. Run AUDIT (knockout) to discriminate.")
    return out
