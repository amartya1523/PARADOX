"""Actual causation over counterfactual worlds.

*Candidates* are the variables a WHY question can blame:
* exogenous root events, such as a substation fault or a generator's
  failure to start;
* modifiable conditions on the causal path, such as signals without UPS
  or the unit held by another incident.

Each candidate has a *neutraliser*, the do-operator that sets it to its
benign value.

* **But-for**: neutralise one candidate and see whether the outcome flips.
* **Minimal flipping sets** (|S| <= 3): the Chockler–Halpern
  responsibility of a candidate is 1/|S| for the smallest minimal set that
  contains it. Contingencies are restricted to neutralising other
  candidates; mediators are not frozen (no HP "W" set). This is a
  documented simplification of the modified HP definition.
* **Probability of necessity**, PN = P(outcome would not have occurred
  without c | it did occur, evidence), estimated over posterior
  replicates. It is computed under the shared-noise coupling and again
  with all per-incident service noise re-drawn. If the two disagree, the
  verdict is flagged as *coupling-sensitive*.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from ..sim.interventions import Intervention
from ..sim.noise import Posterior

SERVICE_NOISE = frozenset({"callproc", "turnout", "scene", "offload", "choice", "telecom", "transport"})


@dataclass(frozen=True)
class Candidate:
    key: str
    label: str
    kind: str                 # root | condition
    eid: str
    neutraliser: tuple        # interventions

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "kind": self.kind, "eid": self.eid,
                "neutraliser": [iv.text() for iv in self.neutraliser]}


def root_candidates(world) -> list[Candidate]:
    out = []
    for e in world.events:
        if e.kind == "fault.substation":
            out.append(Candidate(e.entity, f"{e.entity} fault at {_clock(e.t)}", "root", e.eid,
                                 (Intervention("never_fails", e.entity),)))
        elif e.kind == "gen.start_failed":
            owner = e.entity.split(":")[1]
            target = f"dispatch:{owner}" if e.entity.startswith("dispatch_gen") else f"hospital:{owner}"
            out.append(Candidate(e.entity, f"{e.entity} failed to start at {_clock(e.t)}", "root", e.eid,
                                 (Intervention("gen_starts", target, 0.17),)))
    return out


def incident_conditions(px, world, inc) -> list[Candidate]:
    out = []
    if inc.route_dark:
        sigs = tuple(Intervention("ups", f"signal:{s}", 8) for s in sorted(set(inc.route_dark)))
        out.append(Candidate("route_ups", f"{len(set(inc.route_dark))} dark signal(s) on the route had no UPS",
                             "condition", inc.events[2].eid if len(inc.events) > 2 else "", sigs))
    if inc.held_by:
        nearest = next(u for u, st in _units(px) if u == _nearest_unit(px, world, inc))
        st = _station_of(px, nearest)
        out.append(Candidate("fleet", f"nearest unit {nearest} was held by {inc.held_by}", "condition",
                             inc.events[1].eid, (Intervention("add_units", f"station:{st}", 1),)))
    if inc.components.get("telecom", 0) > 0:
        tower = px.city.towers[px.city.block_tower[inc.block]]["id"]
        out.append(Candidate("telecom", f"tower {tower} was down when the call came in", "condition",
                             inc.events[0].eid, (Intervention("battery", f"tower:{tower}", 24),)))
    return out


def _units(px):
    for s in px.city.stations:
        for k in range(s["units"]):
            yield f"A{s['id'][1:]}-{k + 1}", s


def _station_of(px, unit: str) -> str:
    return "E" + unit[1:].split("-")[0]


def _nearest_unit(px, world, inc) -> str:
    for e in inc.events:
        if e.kind == "ems.dispatched" and e.attrs.get("held_by"):
            # the held unit is the one whose dispatch event is cited as a cause
            for c in e.causes:
                for j in world.ems.incidents:
                    if j.events and len(j.events) > 1 and j.events[1].eid == c:
                        return j.unit
    return inc.unit


def _clock(t: float) -> str:
    t = int(round(t))
    return f"{t // 60:02d}:{t % 60:02d}"


class OutcomeProbe:
    """Evaluates an outcome predicate across counterfactual worlds, with caching."""

    def __init__(self, px, predicate, base_world=None):
        self.px = px
        self.pred = predicate
        self.base = base_world or px.actual
        self.cache = {}
        self.runs = 0

    def world(self, ivs, noise=None):
        key = (frozenset(ivs), id(noise) if noise is not None else None)
        if key not in self.cache:
            self.runs += 1
            ivs_all = tuple(self.base.interventions) + tuple(ivs)
            if noise is None:
                self.cache[key] = self.px.sim.run(ivs_all, noise=self.base.noise.fork_same(), base=self.base)
            else:
                self.cache[key] = self.px.sim.run(ivs_all, noise=noise, base=self.base if not getattr(noise, "fresh_mechanisms", None) else None)
        return self.cache[key]

    def holds(self, ivs=(), noise=None) -> bool:
        return self.pred(self.world(ivs, noise))


def minimal_flipping_sets(probe: OutcomeProbe, cands: list[Candidate], max_size: int = 3) -> list[tuple]:
    found = []
    for k in range(1, min(max_size, len(cands)) + 1):
        for combo in itertools.combinations(cands, k):
            keys = {c.key for c in combo}
            if any(set(f) <= keys for f in found):
                continue
            ivs = tuple(iv for c in combo for iv in c.neutraliser)
            if not probe.holds(ivs):
                found.append(tuple(sorted(keys)))
    return found


def responsibility(cands: list[Candidate], sets: list[tuple]) -> dict:
    out = {}
    for c in cands:
        sizes = [len(s) for s in sets if c.key in s]
        out[c.key] = round(1.0 / min(sizes), 3) if sizes else 0.0
    return out


def probability_of_necessity(probe: OutcomeProbe, cand: Candidate, reps: int = 16,
                             fresh=frozenset()) -> float:
    """P(outcome absent under do(neutralise c) | outcome present), over posterior replicates."""
    flips = 0
    total = 0
    for r in range(reps):
        noise = Posterior(probe.base.noise, r, fresh_mechanisms=fresh)
        if fresh and not probe.holds((), noise):
            continue  # under a re-drawn coupling the factual outcome itself may not recur
        total += 1
        flips += not probe.holds(cand.neutraliser, noise)
    return round(flips / total, 3) if total else float("nan")
