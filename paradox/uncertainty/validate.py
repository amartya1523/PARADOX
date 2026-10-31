"""The oracle harness: how wrong is the twin, measured against a hidden reality.

A counterfactual can never be checked against the real world, because the
alternate world never happened. A synthetic city allows something no real
twin can: build a *reality* that differs from the twin's model, then grade
the twin against that reality's true counterfactuals.

The hidden reality differs in four ways:

1. **Bellevue-style common-mode dependency.** One hospital's fuel-transfer
   pump is wired to the *normal* utility bus, not the emergency bus. When
   utility power is lost the generator starts, but its day tank cannot be
   refilled, so the generator dies an hour or two later (A2, Sandy 2012).
2. Generators fail to start more often than the twin assumes (0.10 vs 0.06).
3. Dark intersections keep less capacity (0.33 vs 0.40), with more spillback.
4. Crashes at dark signals are more frequent.

``validate`` asks for the effect of several interventions in many
scenarios. It compares the twin's interval for the effect (parameter tier)
with the true effect, and reports the error and the empirical coverage.

``audit`` reproduces what a careful analyst would do with observed
reality:
1. screen for systematic residuals (twin prediction vs observation, with a
   sign test across scenarios);
2. localise the missing mechanism from the observed causal record;
3. confirm it with a knockout in reality.

Each finding is graded with the evidence scale in docs/ANALYSIS.md section 4.
"""

from __future__ import annotations

import copy
from math import comb

import numpy as np

from ..sim.interventions import Fault, Intervention, Scenario, hhmm
from ..sim.noise import NoiseTape
from ..sim.world import Simulator
from .ensemble import lhs_params

TRUE_PARAMS = {"p_fts": 0.10, "dark_cap": 0.33, "spillback": 0.5, "crash_rate_dark_per_h": 0.09}


def hidden_hospital(city) -> str:
    s3 = [f["id"] for f in city.feeders if f["sub"] == "S3"]
    for h in city.hospitals:
        if len(h["feeds"]) == 1 and h["feeds"][0] in s3:
            return h["id"]
    return city.hospitals[0]["id"]


def oracle_city(city):
    real = copy.deepcopy(city)
    hid = hidden_hospital(city)
    h = next(x for x in real.hospitals if x["id"] == hid)
    h["fuelpump"] = h["feeds"][0]          # normal bus: dies with the utility feed
    real.registry[f"fuelpump:{hid}"]["supply"] = h["feeds"][0]
    return real, {"hospital": hid, "fuel_feeder": h["feeds"][0],
                  "description": "fuel transfer pump on the normal bus instead of the emergency bus"}


class Oracle:
    def __init__(self, px):
        self.px = px
        self.city, self.hidden = oracle_city(px.city)
        self.real = Simulator(self.city, px.scenario)
        self.twin = px.sim

    def scenarios(self, m: int, seed: int = 0) -> list[tuple[Scenario, int]]:
        rng = np.random.default_rng(seed)
        subs = [s["id"] for s in self.px.city.substations]
        out = []
        for i in range(m):
            sid = subs[i % len(subs)] if i < len(subs) else subs[int(rng.integers(len(subs)))]
            t = float(rng.integers(17 * 60, 20 * 60))
            out.append((Scenario(f"oracle-{i}: {sid} at {hhmm(t)}", (Fault(f"substation:{sid}", t),)),
                        int(rng.integers(1, 10 ** 6))))
        return out


def validate(px, m: int = 6, k: int = 6, metric: str = "ems.p90_response", seed: int = 0) -> dict:
    oc = Oracle(px)
    rows = []
    params = lhs_params(k, seed + 1)
    for sc, s in oc.scenarios(m, seed):
        sub = sc.faults[0].entity
        ivs_list = [(Intervention("never_fails", sub),), (Intervention("flisr", "*"),),
                    (Intervention("add_units", "station:E01", 2),)]
        R = oc.real.run(noise=NoiseTape(s), params=TRUE_PARAMS, scenario=sc)
        for metric_ in (metric, "hospital.unpowered_min"):
            for ivs in ivs_list:
                Rc = oc.real.run(ivs, noise=NoiseTape(s), params=TRUE_PARAMS, scenario=sc, base=R)
                truth = Rc.kpis[metric_] - R.kpis[metric_]
                deltas = []
                for p in params:
                    T = oc.twin.run(noise=NoiseTape(s), params=p, scenario=sc)
                    Tc = oc.twin.run(ivs, noise=NoiseTape(s), params=p, scenario=sc, base=T)
                    deltas.append(Tc.kpis[metric_] - T.kpis[metric_])
                T0 = oc.twin.run(noise=NoiseTape(s), scenario=sc)
                T0c = oc.twin.run(ivs, noise=NoiseTape(s), scenario=sc, base=T0)
                point = T0c.kpis[metric_] - T0.kpis[metric_]
                lo, hi = float(np.quantile(deltas, 0.05)), float(np.quantile(deltas, 0.95))
                rows.append({"scenario": sc.name, "metric": metric_, "intervention": ivs[0].text(),
                             "true_delta": round(truth, 3), "twin_point": round(point, 3),
                             "twin_interval": [round(lo, 3), round(hi, 3)],
                             "covered": lo - 1e-9 <= truth <= hi + 1e-9,
                             "abs_error": round(abs(point - truth), 3)})
    out = {"hidden_structure": {**oc.hidden, "true_params": TRUE_PARAMS}, "rows": rows, "by_metric": {}}
    for metric_ in sorted({r["metric"] for r in rows}):
        rs = [r for r in rows if r["metric"] == metric_]
        out["by_metric"][metric_] = {
            "cases": len(rs), "coverage_90": round(float(np.mean([r["covered"] for r in rs])), 3),
            "mean_abs_error": round(float(np.mean([r["abs_error"] for r in rs])), 3),
            "worst": max(rs, key=lambda r: r["abs_error"])}
    out["verdict"] = _verdict(out["by_metric"])
    return out


def _verdict(by_metric: dict) -> str:
    worst = min(v["coverage_90"] for v in by_metric.values())
    if worst >= 0.85:
        return "intervals approximately calibrated against the oracle"
    return (f"intervals UNDER-COVER (worst {worst:.0%} vs nominal 90%): structural error the "
            "parameter tier cannot see. Treat as exploratory, not decision-grade.")


def _sign_test(pos: int, n: int) -> float:
    """One-sided binomial sign test p-value for >= pos successes out of n."""
    return sum(comb(n, i) for i in range(pos, n + 1)) / 2 ** n if n else 1.0


def stress_scenarios(px, per_entity: int = 4, seed: int = 0) -> list[tuple[Scenario, int]]:
    """For every hospital, ``per_entity`` evenings in which its own substation fails.

    Every entity is stress-tested equally, so the screen is not tuned to the
    planted fault.
    """
    rng = np.random.default_rng(seed)
    out = []
    for h in px.city.hospitals:
        sub = next(f["sub"] for f in px.city.feeders if f["id"] == h["feeds"][0])
        for j in range(per_entity):
            t = float(rng.integers(17 * 60, 20 * 60))
            out.append((Scenario(f"stress {h['id']}#{j}: {sub} at {hhmm(t)}", (Fault(f"substation:{sub}", t),)),
                        int(rng.integers(1, 10 ** 6))))
    return out


def audit(px, seed: int = 0, per_entity: int = 4) -> dict:
    oc = Oracle(px)
    findings = []
    resid = {h["id"]: [] for h in px.city.hospitals}
    unexplained = {}
    scen = stress_scenarios(px, per_entity, seed)
    for sc, s in scen:
        R = oc.real.run(noise=NoiseTape(s), params=TRUE_PARAMS, scenario=sc)
        T = oc.twin.run(noise=NoiseTape(s), params=TRUE_PARAMS, scenario=sc)
        for h in px.city.hospitals:
            r = R.infra.value(f"hospital:{h['id']}")["unpowered"].total()
            t = T.infra.value(f"hospital:{h['id']}")["unpowered"].total()
            if r or t:
                resid[h["id"]].append((r - t, sc.name))
        twin_kinds = {(e.kind, e.entity) for e in T.events}
        for e in R.events:
            if (e.kind, e.entity) not in twin_kinds and e.kind.startswith("gen."):
                unexplained.setdefault((e.kind, e.entity), []).append((sc.name, e, R))
    for hid, rs in resid.items():
        pos = sum(1 for d, _ in rs if d > 1.0)
        if pos == 0:
            continue
        p = _sign_test(pos, len(rs))
        findings.append({"entity": f"hospital:{hid}", "episodes": len(rs), "excess_episodes": pos,
                         "mean_excess_min": round(float(np.mean([d for d, _ in rs])), 1), "sign_test_p": round(p, 4),
                         "grade": "X (model contradicted)" if p < 0.05 or pos == len(rs) else "C (weak signal)"})
    localised = []
    for (kind, ent), occ in unexplained.items():
        sc_name, ev, R = occ[0]
        cause_entities = [R.events[[x.eid for x in R.events].index(c)].entity for c in ev.causes
                          if c in {x.eid for x in R.events}]
        hidden_feeders = [c for c in cause_entities if c.startswith("feeder:")]
        item = {"event": kind, "entity": ent, "first_seen": sc_name, "observed_causes": cause_entities,
                "suspected_dependency": (f"{hidden_feeders[0]} → fuelpump:{ent.split(':')[1]} → {ent}"
                                         if hidden_feeders else None)}
        if hidden_feeders:
            hid = ent.split(":")[1]
            sc, s = next((x, sd) for x, sd in scen if x.name == sc_name)
            R0 = oc.real.run(noise=NoiseTape(s), params=TRUE_PARAMS, scenario=sc)
            Rk = oc.real.run((Intervention("fuel_backup", f"hospital:{hid}"),), noise=NoiseTape(s),
                             params=TRUE_PARAMS, scenario=sc, base=R0)
            before = R0.infra.value(f"hospital:{hid}")["unpowered"].total()
            after = Rk.infra.value(f"hospital:{hid}")["unpowered"].total()
            item["knockout"] = {"intervention": f"hospital:{hid} GETS FUEL BACKUP (in reality)",
                                "unpowered_before": round(before, 1), "unpowered_after": round(after, 1)}
            item["grade"] = "D (hidden dependency: discovered, knockout-confirmed, missing from model)" \
                if after < before else "C (suspected, knockout inconclusive)"
        localised.append(item)
    return {"hidden_truth_for_scoring": oc.hidden, "scenarios": len(scen), "residual_findings": findings,
            "localised_mechanisms": localised,
            "method": "residual sign test → provenance localisation → knockout in reality",
            "note": "In a real deployment step 3 would be a field inspection; here the oracle plays reality."}
