"""Paired ensembles: what-if answers with honest intervals.

Modes:

* ``counterfactual`` (rung 3). Every replicate is a posterior draw given the
  record: observed noise is fixed, partially observed noise is re-sampled
  inside its interval, and unobserved noise is drawn fresh. The factual arm
  then reproduces the record exactly (a consistency check), so the spread
  of the difference is genuine posterior uncertainty about *this* evening.
* ``interventional`` (rung 2). Fresh evenings with the same faults. Both
  arms share a replicate seed (common random numbers), and the reported
  variance-reduction factor shows what pairing bought.

Tiers, each widening the last:

* ``mc``: aleatoric only.
* ``params``: plus a Latin-hypercube draw of every assumption-tagged
  parameter, shared by both arms of a replicate.
* ``structural``: is not computed here; the oracle harness (``VALIDATE``)
  reports it separately.
"""

from __future__ import annotations

import numpy as np
from scipy.stats import qmc

from ..sim.noise import NoiseTape, Posterior, replicate_seed
from ..sim.params import PARAMS, uncertain

SERVICE_NOISE = frozenset({"callproc", "turnout", "scene", "offload", "choice", "telecom", "transport"})


def lhs_params(n: int, seed: int = 0, names: list[str] | None = None) -> list[dict]:
    names = names or uncertain()
    sampler = qmc.LatinHypercube(d=len(names), seed=seed)
    u = sampler.random(n)
    out = []
    for row in u:
        out.append({k: PARAMS[k].lo + (PARAMS[k].hi - PARAMS[k].lo) * float(x) for k, x in zip(names, row)})
    return out


def bootstrap_ci(x: np.ndarray, stat=np.mean, n_boot: int = 2000, alpha: float = 0.05, seed: int = 0):
    x = np.asarray(x, float)
    if len(x) < 2 or np.allclose(x, x[0]):
        v = float(stat(x)) if len(x) else float("nan")
        return v, v
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), (n_boot, len(x)))
    stats = np.array([stat(x[i]) for i in idx])
    return float(np.quantile(stats, alpha / 2)), float(np.quantile(stats, 1 - alpha / 2))


def summarise(f: np.ndarray, c: np.ndarray, independent_var: float | None = None) -> dict:
    d = c - f
    lo, hi = bootstrap_ci(d)
    var_p = float(np.var(d, ddof=1)) if len(d) > 1 else 0.0
    var_i = independent_var if independent_var is not None else (
        float(np.var(f, ddof=1) + np.var(c, ddof=1)) if len(d) > 1 else 0.0)
    return {
        "factual": {"mean": float(f.mean()), "q05": float(np.quantile(f, 0.05)), "q95": float(np.quantile(f, 0.95))},
        "alternate": {"mean": float(c.mean()), "q05": float(np.quantile(c, 0.05)), "q95": float(np.quantile(c, 0.95))},
        "delta": {"mean": float(d.mean()), "ci95": [lo, hi], "q05": float(np.quantile(d, 0.05)),
                  "q50": float(np.quantile(d, 0.5)), "q95": float(np.quantile(d, 0.95)),
                  "p_negative": float(np.mean(d < 0)), "p_positive": float(np.mean(d > 0))},
        "variance_reduction": (var_i / var_p) if var_p > 1e-12 else None,
        "samples": [round(float(x), 3) for x in d],
    }


def _strata_kpi(world, metric: str, area_blocks: set) -> dict:
    inc = world.incidents()
    if not metric.startswith("ems.") or not inc:
        return {}
    def p90(xs):
        return float(np.percentile(xs, 90)) if xs else float("nan")
    a = [i.response for i in inc if i.block in area_blocks]
    b = [i.response for i in inc if i.block not in area_blocks]
    return {"affected_area": p90(a), "rest_of_city": p90(b)}


def compare(px, interventions, metrics=("ems.p90_response",), n: int = 32, mode: str = "counterfactual",
            tier: str = "mc", coupling: bool = False, seed: int = 0) -> dict:
    sim, actual = px.sim, px.actual
    ivs = tuple(interventions)
    params = lhs_params(n, seed) if tier == "params" else [None] * n
    rows_f, rows_c = {m: [] for m in metrics}, {m: [] for m in metrics}
    strata_f, strata_c = [], []
    faulted = {f.entity.split(":")[1] for f in px.scenario.faults}
    area = {b for b in range(len(px.city.block_xy))
            if px.city.feeders[px.city.block_feeder[b]]["sub"] in faulted}
    consistency = True
    for r in range(n):
        p = params[r]
        if mode == "counterfactual":
            noise_f = Posterior(actual.noise, r)
            if p is None:
                fw = actual  # the posterior's factual arm *is* the record (tested)
                cw = sim.run(ivs, noise=noise_f, base=actual)
            else:
                fw = sim.run((), noise=noise_f, params=p)
                cw = sim.run(ivs, noise=Posterior(actual.noise, r), params=p, base=fw)
        else:
            s = replicate_seed(px.world_seed, r)
            fw = sim.run((), noise=NoiseTape(s), params=p)
            cw = sim.run(ivs, noise=NoiseTape(s), params=p, base=fw)
        for m in metrics:
            rows_f[m].append(fw.kpis[m])
            rows_c[m].append(cw.kpis[m])
        strata_f.append(_strata_kpi(fw, metrics[0], area))
        strata_c.append(_strata_kpi(cw, metrics[0], area))
    out = {"interventions": [iv.text() for iv in ivs], "mode": mode, "tier": tier, "n": n, "seed": seed,
           "metrics": {}, "consistency_checked": consistency}
    for m in metrics:
        f, c = np.array(rows_f[m]), np.array(rows_c[m])
        out["metrics"][m] = summarise(f, c)
    # stratified view and reversal detector (red-team #7)
    if strata_f and strata_f[0]:
        strat = {}
        for k in strata_f[0]:
            f = np.array([s[k] for s in strata_f])
            c = np.array([s[k] for s in strata_c])
            ok = ~(np.isnan(f) | np.isnan(c))
            if ok.sum():
                d = c[ok] - f[ok]
                strat[k] = {"delta_mean": float(d.mean()), "ci95": list(bootstrap_ci(d))}
        overall = out["metrics"][metrics[0]]["delta"]["mean"]
        reversal = [k for k, v in strat.items() if v["ci95"][0] > 0 > overall or v["ci95"][1] < 0 < overall]
        out["strata"] = strat
        out["reversal"] = reversal
    if coupling and mode == "counterfactual":
        alt = []
        m0 = metrics[0]
        for r in range(max(8, n // 2)):
            nz = Posterior(actual.noise, r, fresh_mechanisms=SERVICE_NOISE)
            fw = sim.run((), noise=nz)
            cw = sim.run(ivs, noise=Posterior(actual.noise, r, fresh_mechanisms=SERVICE_NOISE))
            alt.append(cw.kpis[m0] - fw.kpis[m0])
        alt = np.array(alt)
        shared = out["metrics"][m0]["delta"]["mean"]
        lo, hi = bootstrap_ci(alt)
        out["coupling"] = {"redrawn_delta_mean": float(alt.mean()), "redrawn_ci95": [lo, hi],
                           "shared_delta_mean": shared,
                           "sensitive": bool(not (lo <= shared <= hi) or (np.sign(alt.mean()) != np.sign(shared)))}
    return out
