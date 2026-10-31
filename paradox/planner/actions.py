"""Candidate interventions, drawn from the causal cone of the harm.

Costs are relative cost units (1 CU ~ $10k of capital), taken from the
resilience catalogue in docs/ANALYSIS.md section 8 (DOE FLISR report, FDOT
signal UPS tests, NJ HMGP generator costs, NREL generator reliability). The
search only offers actions that touch an entity which actually failed, or
which sits next to the failure, in the record. That *causal pruning* keeps
the search small without ever offering a fix for something that never
broke.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..sim.interventions import Intervention


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    interventions: tuple
    cost: float
    kind: str        # capital | programme | operational
    basis: str

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "cost": self.cost, "kind": self.kind,
                "basis": self.basis, "pql": [iv.text() for iv in self.interventions]}


def cone_actions(px, world=None) -> list[Action]:
    w = world or px.actual
    city = px.city
    acts: list[Action] = []
    failed_feeders = sorted({e.entity.split(":")[1] for e in w.events if e.kind == "feeder.down"})
    for fid in failed_feeders:
        f = next(x for x in city.feeders if x["id"] == fid)
        if f["ties"]:
            acts.append(Action(f"flisr:{fid}", f"FLISR self-healing on feeder {fid}",
                               (Intervention("flisr", f"feeder:{fid}"),), 20.0, "capital",
                               "DOE 2014: restoration <5 min; 15-30 CU per feeder"))
    dark = sorted({e.entity.split(":")[1] for e in w.events if e.kind == "signal.dark"})
    by_feeder: dict[str, list[str]] = {}
    for sid in dark:
        by_feeder.setdefault(city.signals[sid]["feeder"], []).append(sid)
    for fid, sigs in sorted(by_feeder.items()):
        acts.append(Action(f"ups:{fid}", f"Battery UPS on the {len(sigs)} signal(s) of feeder {fid}",
                           tuple(Intervention("ups", f"signal:{s}", 6) for s in sigs), 1.0 * len(sigs),
                           "capital", "FDOT/Toronto: ~1 CU per intersection, 6-8 h"))
    for e in w.events:
        if e.kind == "power.lost" and e.entity.startswith("hospital:"):
            hid = e.entity.split(":")[1]
            acts.append(Action(f"maint:{hid}", f"Generator test & maintenance programme at {hid}",
                               (Intervention("maintenance", f"hospital:{hid}"),), 1.0, "programme",
                               "NREL/ASHE: FTS 5% → ~1%; 0.5-1 CU per site-year"))
            acts.append(Action(f"n1:{hid}", f"N+1 redundant generator at {hid}",
                               (Intervention("gen_starts", f"hospital:{hid}", 0.17),), 45.0, "capital",
                               "Marqusee 2020: parallel redundancy; 30-60 CU"))
        if e.kind == "power.lost" and e.entity.startswith("dispatch:"):
            did = e.entity.split(":")[1]
            acts.append(Action(f"disp:{did}", f"Redundant backup power at dispatch {did}",
                               (Intervention("gen_starts", f"dispatch:{did}", 0.17),), 15.0, "capital",
                               "assumption: 10-20 CU"))
    for e in w.events:
        if e.kind == "tower.battery_depleted":
            kid = e.entity.split(":")[1]
            acts.append(Action(f"bat:{kid}", f"Extend battery at tower {kid} to 24 h",
                               (Intervention("battery", f"tower:{kid}", 24),), 2.5, "capital",
                               "industry: 2-3 CU per site"))
    # surge staffing at the stations nearest the faulted area
    faulted = {f.entity.split(":")[1] for f in px.scenario.faults}
    blocks = [b for b in range(len(city.block_xy)) if city.feeders[city.block_feeder[b]]["sub"] in faulted]
    if blocks:
        c = city.block_xy[blocks].mean(0)
        st = sorted(city.stations, key=lambda s: float(np.linalg.norm(city.block_xy[s["block"]] - c)))[:3]
        for s in st:
            acts.append(Action(f"surge:{s['id']}", f"Surge staffing: +2 reserve ambulances at {s['id']}",
                               (Intervention("add_units", f"station:{s['id']}", 2),), 3.0, "operational",
                               "assumption: overtime crews for one evening"))
    for f in px.scenario.faults:
        acts.append(Action(f"mobile:{f.entity}", f"Pre-staged mobile substation for {f.entity} (repair 90 min)",
                           (Intervention("repairs_in", f.entity, 90),), 280.0, "capital",
                           "Con Ed / utility cost: 250-300 CU"))
    seen, out = set(), []
    for a in acts:
        if a.key not in seen:
            seen.add(a.key)
            out.append(a)
    return out
