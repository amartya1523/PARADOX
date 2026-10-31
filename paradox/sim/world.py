"""The world function: run(city, scenario, interventions, noise, params).

``Simulator.run(..., base=world)`` recomputes incrementally from a baseline:

* in the infrastructure SCM, only the nodes the interventions touch, plus
  their descendants whose inputs actually changed;
* in the EMS block, only from tau*, the first instant any of its inputs
  diverge.

The result is event-for-event identical to a full run, and the tests check
that.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

import numpy as np

from ..city.roads import RoadNet
from .ems import EMSModel, EMSResult, first_divergence
from .infra import InfraModel, InfraResult
from .interventions import Intervention, Scenario, compile_interventions, heat_dome
from .noise import NoiseTape, Posterior
from .params import defaults


def noise_key(noise: NoiseTape) -> tuple:
    if isinstance(noise, Posterior):
        return ("posterior", noise.seed, noise.rep, tuple(sorted(noise.fresh_mechanisms)), id(noise._points))
    return ("tape", noise.seed)


def compatible(base_noise: NoiseTape, noise: NoiseTape) -> bool:
    """Can a world computed under ``base_noise`` be reused under ``noise``?

    Same key, or ``noise`` is a shared-coupling posterior *of* the baseline's
    tape: the posterior reproduces every value the baseline observed, so any
    computation the baseline did is exactly what the posterior would do.
    """
    if noise_key(base_noise) == noise_key(noise):
        return True
    return (isinstance(noise, Posterior) and not isinstance(base_noise, Posterior)
            and not noise.fresh_mechanisms and noise._points is base_noise.points
            and noise.seed == base_noise.seed)


@dataclass
class World:
    scenario: Scenario
    interventions: tuple
    params: dict
    noise: NoiseTape
    infra: InfraResult
    ems: EMSResult
    kpis: dict
    stats: dict = field(default_factory=dict)
    _events: list | None = None

    @property
    def events(self) -> list:
        if self._events is None:
            evs = {e.eid: e for e in self.infra.events + self.ems.events}
            self._events = sorted(evs.values(), key=lambda e: (e.t, e.eid))
        return self._events

    def head(self) -> str:
        h = hashlib.blake2b(digest_size=16)
        for e in self.events:
            h.update(e.eid.encode())
        return h.hexdigest()

    def incident(self, iid: str):
        return self.ems.by_id().get(iid)

    def incidents(self, reported_only: bool = True) -> list:
        t0 = self.scenario.t_report
        return [i for i in self.ems.incidents if not reported_only or i.t_call >= t0]


class Simulator:
    def __init__(self, city, scenario: Scenario | None = None):
        self.city = city
        self.scenario = scenario or heat_dome(city)
        self.roads = RoadNet(city)
        self._feeder_bldgs = np.bincount(city.block_feeder[city.bldg_block], minlength=len(city.feeders))

    def run(self, interventions=(), noise: NoiseTape | int = 0, params: dict | None = None,
            base: World | None = None, scenario: Scenario | None = None) -> World:
        sc = scenario or self.scenario
        if isinstance(noise, int):
            noise = NoiseTape(noise)
        p = defaults()
        p.update(params or {})
        ivs = tuple(interventions)
        eff = compile_interventions(sc, ivs, p, self.city)
        self.apply_observations(sc, noise, p)

        touched: set = set()
        if base is not None:
            if base.scenario != sc or base.params != p or not compatible(base.noise, noise):
                base = None
            else:
                for iv in set(ivs) ^ set(base.interventions):
                    touched |= iv.touches(self.city)
        infra = InfraModel(self.city, sc, eff, noise).run(base.infra if base else None, touched)
        ems_model = EMSModel(self.city, self.roads, sc, eff, noise, infra)
        tau = None
        if base is not None and "ems" not in touched and "*" not in touched:
            old = EMSModel(self.city, self.roads, sc, eff, noise, base.infra).input_timelines()
            tau = first_divergence(old, ems_model.input_timelines())
            if tau is None:
                tau = math.inf
        ems = ems_model.run(base.ems if base else None, tau)
        world = World(sc, ivs, p, noise, infra, ems, {})
        world.kpis = self.kpis(world)
        world.stats = {"infra_recomputed": len(infra.recomputed), "infra_reused": len(infra.reused),
                       "ems_kept": ems.kept, "ems_total": len(ems.incidents), "tau": tau,
                       "incremental": base is not None}
        return world

    @staticmethod
    def apply_observations(sc: Scenario, noise: NoiseTape, p: dict) -> None:
        from ..store.events import Event
        for ob in sc.observations:
            fault = next((f for f in sc.faults if f.entity == ob.root_entity), None)
            if fault is None:
                continue
            root = Event(fault.t, "fault.substation", fault.entity, {"state": "down"}, (),
                         "exogenous:scenario").eid
            thr = p["p_fts"] if ob.mechanism == "gen_start" else 0.5
            lo, hi = (0.0, thr) if ob.failed else (thr, 1.0)
            noise.constrain(lo, hi, ob.entity, ob.mechanism, root)

    # -- KPIs ----------------------------------------------------------------------
    def kpis(self, w: World) -> dict:
        sc, city, inf = w.scenario, self.city, w.infra
        t0, t1 = sc.t_report, sc.t_end
        inc = w.incidents()
        resp = np.array([i.response for i in inc]) if inc else np.zeros(1)
        base = np.array([i.response for i in inc if i.origin == "base"]) if inc else np.zeros(1)
        p1 = [i for i in inc if i.priority == 1]
        k = {
            "ems.p90_response": float(np.percentile(resp, 90)),
            "ems.median_response": float(np.median(resp)),
            "ems.mean_response": float(resp.mean()),
            "ems.max_response": float(resp.max()),
            "ems.p90_matched": float(np.percentile(base, 90)) if len(base) else 0.0,
            "ems.mean_matched": float(base.mean()) if len(base) else 0.0,
            "ems.p1_late": float(sum(1 for i in p1 if i.response > w.params["target_response_min"])),
            "ems.p1_late_pct": 100.0 * sum(1 for i in p1 if i.response > w.params["target_response_min"]) / max(1, len(p1)),
            "ems.queue_wait_mean": float(np.mean([i.queue_wait for i in inc])) if inc else 0.0,
            "ems.n_incidents": float(len(inc)),
            "ems.n_induced": float(sum(1 for i in inc if i.origin != "base")),
        }
        feeder_h = 0.0
        cust_h = 0.0
        for fi, f in enumerate(city.feeders):
            hrs = inf.feeder(f["id"]).total(t0, t1) / 60.0
            feeder_h += hrs
            cust_h += hrs * self._feeder_bldgs[fi]
        k["power.customer_hours"] = cust_h
        k["power.feeder_hours"] = feeder_h
        k["hospital.unpowered_min"] = sum(inf.value(f"hospital:{h['id']}")["unpowered"].total(t0, t1)
                                          for h in city.hospitals)
        k["hospital.diversion_min"] = sum(inf.value(f"hospital:{h['id']}")["diversion"].total(t0, t1)
                                          for h in city.hospitals)
        k["signals.dark_hours"] = sum(inf.value(f"signal:{s}").total(t0, t1) for s in city.signals) / 60.0
        k["telecom.tower_hours"] = sum(inf.value(f"tower:{t['id']}").total(t0, t1) for t in city.towers) / 60.0
        k["water.zone_hours"] = sum(inf.value(f"zone:{z['id']}").total(t0, t1) for z in city.zones) / 60.0
        k["dispatch.down_min"] = sum(inf.value(f"dispatch:{d['id']}").total(t0, t1) for d in city.dispatch)
        return {key: round(float(v), 4) for key, v in k.items()}


KPI_INFO = {
    "ems.p90_response": ("min", "90th percentile EMS response time, all incidents"),
    "ems.median_response": ("min", "median EMS response time"),
    "ems.mean_response": ("min", "mean EMS response time"),
    "ems.max_response": ("min", "worst EMS response time"),
    "ems.p90_matched": ("min", "p90 response over the matched (outage-independent) population"),
    "ems.mean_matched": ("min", "mean response over the matched population"),
    "ems.p1_late": ("count", "priority-1 incidents over the 8-min target"),
    "ems.p1_late_pct": ("%", "share of priority-1 incidents over target"),
    "ems.queue_wait_mean": ("min", "mean wait for a free ambulance"),
    "ems.n_incidents": ("count", "incidents in the reporting window"),
    "ems.n_induced": ("count", "outage-induced incidents (surge + crashes)"),
    "power.customer_hours": ("bldg-h", "building-hours without power"),
    "power.feeder_hours": ("h", "feeder-hours de-energised"),
    "hospital.unpowered_min": ("min", "hospital-minutes with no power at all"),
    "hospital.diversion_min": ("min", "hospital-minutes on ambulance diversion"),
    "signals.dark_hours": ("h", "signal-hours dark"),
    "telecom.tower_hours": ("h", "tower-hours down"),
    "water.zone_hours": ("h", "zone-hours depressurised"),
    "dispatch.down_min": ("min", "dispatch-centre minutes down"),
}
