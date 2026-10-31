"""PLAN: the smallest affordable set of changes that meets a target.

The plan is found by simulation optimisation, with each evaluation being a
paired ensemble:

* Every candidate is scored on the same *training* evenings: common random
  numbers, the scenario's faults fixed, the luck fresh. Each run is
  incremental from that evening's baseline world.
* **CELF lazy greedy** runs twice, once by gain per cost and once by raw
  gain, and the cheaper plan that meets the target wins. The (1-1/e)
  guarantee holds only for submodular objectives. Redundant fixes are
  complements (supermodular), so the bound is not claimed.
* The plan is **pruned to an irreducible set**: any action whose removal
  still meets the target is dropped.
* The result is reported on **held-out** evenings that the optimiser never
  saw (optimiser's curse, Smith & Winkler 2006). That report gives the mean
  effect with CI, P(meet target) and a cost-impact Pareto curve.
"""

from __future__ import annotations

import heapq

import numpy as np

from ..sim.interventions import Scenario
from ..sim.noise import NoiseTape, replicate_seed
from ..uncertainty.ensemble import bootstrap_ci
from .actions import cone_actions


class Evaluator:
    def __init__(self, px, metric: str, seeds: list[int], scenario: Scenario):
        self.px, self.metric, self.seeds, self.sc = px, metric, seeds, scenario
        self.base = {s: px.sim.run(noise=NoiseTape(s), scenario=scenario) for s in seeds}
        self.cache = {}
        self.runs = len(seeds)

    def values(self, actions: tuple) -> np.ndarray:
        key = frozenset(a.key for a in actions)
        if key not in self.cache:
            ivs = tuple(iv for a in actions for iv in a.interventions)
            if not ivs:
                vals = [self.base[s].kpis[self.metric] for s in self.seeds]
            else:
                vals = [self.px.sim.run(ivs, noise=NoiseTape(s), scenario=self.sc, base=self.base[s]).kpis[self.metric]
                        for s in self.seeds]
                self.runs += len(self.seeds)
            self.cache[key] = np.array(vals)
        return self.cache[key]

    def mean(self, actions) -> float:
        return float(self.values(tuple(actions)).mean())


def celf(ev: Evaluator, cands, target: float, budget: float, by_ratio: bool):
    chosen: list = []
    cur = ev.mean(())
    spent = 0.0
    traj = [(0.0, cur, [])]
    heap = []
    for i, a in enumerate(cands):
        gain = cur - ev.mean((a,))
        score = gain / a.cost if by_ratio else gain
        heapq.heappush(heap, (-score, i, 0))
    rnd = 0
    while heap and cur > target:
        neg, i, when = heapq.heappop(heap)
        a = cands[i]
        if spent + a.cost > budget:
            continue
        if when != rnd:
            gain = cur - ev.mean(tuple(chosen) + (a,))
            score = gain / a.cost if by_ratio else gain
            heapq.heappush(heap, (-score, i, rnd))
            continue
        if -neg <= 0:
            break
        chosen.append(a)
        spent += a.cost
        cur = ev.mean(tuple(chosen))
        rnd += 1
        traj.append((spent, cur, [x.key for x in chosen]))
    return chosen, traj


def prune(ev: Evaluator, chosen: list, target: float) -> list:
    keep = list(chosen)
    for a in sorted(chosen, key=lambda x: -x.cost):
        trial = [x for x in keep if x is not a]
        if trial and ev.mean(tuple(trial)) <= target:
            keep = trial
    return keep


def plan(px, metric: str, target: float, budget: float = 60.0, n_train: int = 8, n_test: int = 16,
         seed: int = 0, kinds: tuple | None = None) -> dict:
    sc = Scenario(px.scenario.name + " (generic evenings)", px.scenario.faults)
    train = [replicate_seed(px.world_seed + 1000 + seed, r) for r in range(n_train)]
    test = [replicate_seed(px.world_seed + 5000 + seed, r) for r in range(n_test)]
    cands = [a for a in cone_actions(px) if kinds is None or a.kind in kinds]
    ev = Evaluator(px, metric, train, sc)
    results = []
    for by_ratio in (True, False):
        chosen, traj = celf(ev, cands, target, budget, by_ratio)
        results.append((chosen, traj, by_ratio))
    def rank(r):
        chosen, traj, _ = r
        met = ev.mean(tuple(chosen)) <= target
        return (not met, sum(a.cost for a in chosen), ev.mean(tuple(chosen)))
    chosen, traj, by_ratio = min(results, key=rank)
    final = prune(ev, chosen, target)
    tev = Evaluator(px, metric, test, sc)
    base_t = tev.values(())
    plan_t = tev.values(tuple(final))
    d = plan_t - base_t
    pareto = []
    for spent, _, keys in traj:
        acts = tuple(a for a in cands if a.key in keys)
        v = tev.values(acts)
        pareto.append({"cost": spent, "test_mean": float(v.mean()), "actions": keys})
    return {
        "metric": metric, "target": target, "budget": budget, "kinds": list(kinds) if kinds else "all",
        "candidates": [a.to_dict() for a in cands],
        "search": "CELF lazy greedy (" + ("gain/cost" if by_ratio else "gain") + "), then prune to irreducible",
        "plan": [a.to_dict() for a in final], "plan_cost": sum(a.cost for a in final),
        "train": {"n": n_train, "baseline": float(ev.mean(())), "with_plan": float(ev.mean(tuple(final)))},
        "test": {"n": n_test, "baseline": float(base_t.mean()), "with_plan": float(plan_t.mean()),
                 "delta_mean": float(d.mean()), "delta_ci95": list(bootstrap_ci(d)),
                 "p_meet_target": float(np.mean(plan_t <= target)),
                 "p_meet_target_baseline": float(np.mean(base_t <= target))},
        # optimiser's curse: how much the in-sample improvement overstates the held-out one
        "optimism_gap": float((ev.mean(()) - ev.mean(tuple(final))) - (base_t.mean() - plan_t.mean())),
        "pareto": pareto, "simulations": ev.runs + tev.runs,
        "met_on_train": ev.mean(tuple(final)) <= target,
        "watermark": "synthetic city · relative costs · illustrative, not decision-grade",
    }
