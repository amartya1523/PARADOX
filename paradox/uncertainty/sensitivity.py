"""Global sensitivity analysis: which assumptions does an answer hinge on?

* **Morris elementary effects.** r one-at-a-time trajectories over the
  unit hypercube of every [A]/[D] parameter, ranked by mu* (importance) and
  sigma (non-linearity or interaction).
* **Sobol indices** for the top-k Morris parameters, using the Saltelli
  A/B/AB design with the Saltelli-2010 first-order estimator and the
  Jansen total-effect estimator, plus bootstrap intervals.

The quantity analysed is a KPI of the record's posterior world, or, when
interventions are given, the paired effect Delta. The noise is held fixed
(posterior replicate 0) inside every evaluation, so the indices measure
parameter effects and not Monte Carlo noise.
"""

from __future__ import annotations

import numpy as np

from ..sim.noise import Posterior
from ..sim.params import PARAMS, uncertain


def _scale(names, u):
    return {k: PARAMS[k].lo + (PARAMS[k].hi - PARAMS[k].lo) * float(x) for k, x in zip(names, u)}


def _evaluator(px, metric, interventions):
    ivs = tuple(interventions or ())

    def f(params: dict) -> float:
        nz = Posterior(px.actual.noise, 0)
        fw = px.sim.run((), noise=nz, params=params)
        if not ivs:
            return fw.kpis[metric]
        cw = px.sim.run(ivs, noise=Posterior(px.actual.noise, 0), params=params, base=fw)
        return cw.kpis[metric] - fw.kpis[metric]
    return f


def morris(px, metric: str, interventions=None, r: int = 5, levels: int = 4, seed: int = 0,
           names: list[str] | None = None) -> dict:
    names = names or uncertain()
    k = len(names)
    f = _evaluator(px, metric, interventions)
    rng = np.random.default_rng(seed)
    delta = levels / (2 * (levels - 1))
    grid = np.arange(levels // 2) / (levels - 1)
    effects = {n: [] for n in names}
    evals = 0
    for _ in range(r):
        x = rng.choice(grid, size=k)
        y0 = f(_scale(names, x))
        evals += 1
        for j in rng.permutation(k):
            x2 = x.copy()
            x2[j] = x[j] + delta if x[j] + delta <= 1 else x[j] - delta
            y1 = f(_scale(names, x2))
            evals += 1
            effects[names[j]].append((y1 - y0) / (x2[j] - x[j]))
            x, y0 = x2, y1
    rows = []
    for n in names:
        e = np.array(effects[n])
        rows.append({"param": n, "mu_star": float(np.mean(np.abs(e))), "mu": float(np.mean(e)),
                     "sigma": float(np.std(e, ddof=1)) if len(e) > 1 else 0.0, "tag": PARAMS[n].tag,
                     "range": [PARAMS[n].lo, PARAMS[n].hi], "note": PARAMS[n].note})
    rows.sort(key=lambda r_: -r_["mu_star"])
    return {"metric": metric, "interventions": [iv.text() for iv in interventions or ()], "method": "morris",
            "r": r, "evaluations": evals, "ranking": rows}


def sobol(px, metric: str, names: list[str], interventions=None, N: int = 32, seed: int = 0) -> dict:
    k = len(names)
    f = _evaluator(px, metric, interventions)
    rng = np.random.default_rng(seed)
    A = rng.random((N, k))
    B = rng.random((N, k))
    fA = np.array([f(_scale(names, a)) for a in A])
    fB = np.array([f(_scale(names, b)) for b in B])
    fAB = np.zeros((k, N))
    for i in range(k):
        ABi = A.copy()
        ABi[:, i] = B[:, i]
        fAB[i] = [f(_scale(names, x)) for x in ABi]
    var = np.var(np.concatenate([fA, fB]), ddof=1)

    def indices(idx):
        va = np.var(np.concatenate([fA[idx], fB[idx]]), ddof=1) or 1e-12
        S = [float(np.mean(fB[idx] * (fAB[i][idx] - fA[idx])) / va) for i in range(k)]
        ST = [float(0.5 * np.mean((fA[idx] - fAB[i][idx]) ** 2) / va) for i in range(k)]
        return S, ST

    S, ST = indices(np.arange(N))
    boots = [indices(rng.integers(0, N, N)) for _ in range(200)]
    rows = []
    for i, n in enumerate(names):
        bs = np.array([b[0][i] for b in boots])
        bt = np.array([b[1][i] for b in boots])
        rows.append({"param": n, "S1": S[i], "S1_ci": [float(np.quantile(bs, .025)), float(np.quantile(bs, .975))],
                     "ST": ST[i], "ST_ci": [float(np.quantile(bt, .025)), float(np.quantile(bt, .975))]})
    return {"metric": metric, "method": "sobol", "N": N, "evaluations": int(N * (k + 2)),
            "variance": float(var), "indices": sorted(rows, key=lambda r_: -r_["ST"])}


def sensitivity(px, metric: str, interventions=None, method: str = "morris", runs: int = 5,
                top: int = 4, seed: int = 0) -> dict:
    m = morris(px, metric, interventions, r=runs, seed=seed)
    if method == "sobol":
        names = [row["param"] for row in m["ranking"][:top]]
        s = sobol(px, metric, names, interventions, N=max(16, runs * 6), seed=seed)
        m["sobol"] = s
    return m
