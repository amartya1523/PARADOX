"""Paradox: the database facade.

It holds:
* the synthetic city;
* the record of reality (the ``main`` branch);
* every what-if branch, stored as an overlay in the ChronoStore;
* telemetry in the hypertable;
* a receipt ledger.

The analysis engines (WHY, ATTRIBUTE, COMPARE, PLAN, ...) live in their own
packages and take an engine.
"""

from __future__ import annotations

import math
import time

from . import MODEL_VERSION
from .city import generate_city
from .sim.interventions import Intervention, hhmm
from .sim.noise import NoiseTape
from .sim.params import digest as params_digest
from .sim.telemetry import build_telemetry
from .sim.world import KPI_INFO, Simulator, World
from .store import ChronoStore
from .store.derive import derive_tgraph
from .store.receipts import Ledger

# The reference evening's luck. Committed before looking at any outcome
# (18:15, the fault time). Red-team item 10: the seed is never chosen for drama.
WORLD_SEED = 1815


class Paradox:
    def __init__(self, city_seed: int = 7, world_seed: int = WORLD_SEED, telemetry: bool = True):
        t0 = time.perf_counter()
        self.city = generate_city(city_seed)
        self.sim = Simulator(self.city)
        self.world_seed = world_seed
        self.store = ChronoStore()
        self.actual = self.sim.run(noise=NoiseTape(world_seed))
        self.store.append("main", self.actual.events)
        self.store.branch("main").meta.update({"interventions": [], "world_head": self.actual.head(),
                                               "scenario": self.sim.scenario.describe()})
        self.worlds: dict[str, World] = {"main": self.actual}
        self.ledger = Ledger()
        self._tg_cache: dict = {}
        self.telemetry = build_telemetry(self.city, self.actual, seed=world_seed) if telemetry else None
        self.boot_ms = round((time.perf_counter() - t0) * 1000)

    # -- branches -------------------------------------------------------------------
    @property
    def scenario(self):
        return self.sim.scenario

    def factual_noise(self) -> NoiseTape:
        return self.actual.noise

    def counterfactual(self, interventions, noise=None) -> World:
        """One rung-3 world: the record's luck, the intervention's change."""
        return self.sim.run(interventions, noise=noise or self.actual.noise.fork_same(), base=self.actual)

    def branch_name(self, interventions) -> str:
        base = "-".join(iv.text().lower().replace(":", "").replace(" ", "_") for iv in interventions)[:48]
        name, k = base or "alt", 2
        while name in self.worlds:
            name, k = f"{base}-{k}", k + 1
        return name

    def create_branch(self, interventions, name: str | None = None) -> dict:
        ivs = tuple(interventions)
        for n, w in self.worlds.items():
            if n != "main" and set(w.interventions) == set(ivs):
                return self.branch_info(n)
        world = self.counterfactual(ivs)
        name = name or self.branch_name(ivs)
        a, b = {e.eid for e in self.actual.events}, {e.eid for e in world.events}
        diff = a ^ b
        evmap = {e.eid: e for e in self.actual.events + world.events}
        fork_t = min((evmap[e].t for e in diff), default=self.scenario.t_end)
        prefix_ok = {e.eid for e in world.events if e.t < fork_t} == {e.eid for e in self.actual.events if e.t < fork_t}
        if not prefix_ok:
            raise RuntimeError("counterfactual prefix diverges before its fork point")
        self.store.create_branch(name, "main", fork_t=fork_t,
                                 meta={"interventions": [iv.text() for iv in ivs], "world_head": world.head()})
        self.store.append(name, [e for e in world.events if e.t >= fork_t])
        self.worlds[name] = world
        return self.branch_info(name)

    def branch_info(self, name: str) -> dict:
        w = self.worlds[name]
        b = self.store.branch(name)
        info = {"name": name, "interventions": [iv.text() for iv in w.interventions],
                "fork_t": None if not math.isfinite(b.fork_t) else b.fork_t,
                "fork": hhmm(b.fork_t) if math.isfinite(b.fork_t) else None,
                "head": b.head, "own_events": b.own_count, "kpis": w.kpis, "stats": w.stats}
        if name != "main":
            d = self.store.diff("main", name)
            info["diff"] = {"only_actual": len(d["only_a"]), "only_alternate": len(d["only_b"]),
                            "shared": d["shared"]}
        return info

    def branches(self) -> list[dict]:
        return [self.branch_info(n) for n in self.worlds]

    def world(self, name: str = "main") -> World:
        if name not in self.worlds:
            raise KeyError(f"no such branch: {name!r}")
        return self.worlds[name]

    # -- views ------------------------------------------------------------------------
    def tgraph(self, branch: str = "main", known_at: int | None = None):
        b = self.store.branch(branch)
        key = (branch, b.head, known_at)
        if key not in self._tg_cache:
            self._tg_cache[key] = derive_tgraph(self.store, branch, self.city, known_at)
        return self._tg_cache[key]

    def state_at(self, branch: str, t: float) -> dict:
        """Map-ready state of the city at minute ``t`` on a branch."""
        w = self.world(branch)
        inf = w.infra
        feeders_down = [f["id"] for f in self.city.feeders if inf.feeder(f["id"]).at(t) is not None]
        signals_dark = [s for s in self.city.signals if inf.value(f"signal:{s}").at(t) is not None]
        hospitals = {}
        for h in self.city.hospitals:
            v = inf.value(f"hospital:{h['id']}")
            st = "ok"
            if v["unpowered"].at(t) is not None:
                st = "unpowered"
            elif v["lost"].at(t) is not None:
                st = "generator"
            hospitals[h["id"]] = {"state": st, "diverting": v["diversion"].at(t) is not None}
        units = []
        for inc in w.ems.incidents:
            if inc.t_disp <= t < inc.t_free:
                units.append({"unit": inc.unit, "incident": inc.iid, "phase": self._phase(inc, t)})
        return {"t": t, "clock": hhmm(t), "feeders_down": feeders_down, "signals_dark": signals_dark,
                "hospitals": hospitals,
                "towers_down": [k["id"] for k in self.city.towers if inf.value(f"tower:{k['id']}").at(t) is not None],
                "zones_dry": [z["id"] for z in self.city.zones if inf.value(f"zone:{z['id']}").at(t) is not None],
                "dispatch_down": [d["id"] for d in self.city.dispatch if inf.value(f"dispatch:{d['id']}").at(t) is not None],
                "substations_down": [s["id"] for s in self.city.substations if inf.value(f"sub:{s['id']}").at(t) is not None],
                "active_units": units}

    @staticmethod
    def _phase(inc, t) -> str:
        if t < inc.t_depart:
            return "turnout"
        if t < inc.t_arrive:
            return "to_scene"
        return "on_scene_or_transport"

    def receipt(self, query: str, result, branches=("main",), **kw) -> dict:
        heads = {b: {"head": self.store.head(b), "known_at": self.store.tx} for b in branches if b in self.store.branches}
        return self.ledger.issue(query, result, heads, model=MODEL_VERSION,
                                 params=params_digest(self.actual.params), **kw)

    def kpi_info(self) -> dict:
        return {k: {"unit": u, "label": l} for k, (u, l) in KPI_INFO.items()}

    def parse_intervention(self, text: str) -> Intervention:
        from .pql.parser import parse_intervention
        return parse_intervention(text)
