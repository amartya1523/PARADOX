"""The EMS layer: one coupled block, simulated in time order.

Incidents compete for a shared fleet. Two incidents interact whenever one
is still holding the unit the other would have got. So early cutoff per
incident would be unsound, and the whole layer is one block. For a
counterfactual it is resumed rather than rerun:

* ``tau*`` is the earliest time at which any input the layer reads differs
  from the baseline (signals, towers, dispatch, hospitals, feeders);
* every incident whose *whole* lifecycle, call to unit back at station,
  ended before ``tau*`` used only identical inputs, so it is kept as is;
* the fleet state is rebuilt from the kept assignments and the rest is
  simulated again.

The test suite checks this against a full replay.

Demand comes from an exogenous **candidate tape**. Each demand cell runs a
dominating Poisson process, and every candidate gets a stable id
``I-<cell>-<k>``. Thinning decides two things for each candidate:

* whether it is a *base* incident, which happens regardless of the outage
  and forms the matched population used by fixed-population KPIs;
* whether it is *induced* by the outage, in which case it carries the
  ``feeder.down`` event that caused it.

Crashes at dark signals come from their own per-signal tapes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..store.events import Event
from .params import SPREAD
from .timeline import EMPTY, q

PRIORITY_P = [0.29, 0.25, 0.46]   # P1 / P2 / P3 (ESO shares mapped)


@dataclass
class Incident:
    iid: str
    t_call: float
    block: int
    cell: int
    origin: str                   # base | induced | crash
    cause: str = ""
    priority: int = 3
    t_answer: float = 0.0
    t_disp: float = 0.0
    unit: str = ""
    queue_wait: float = 0.0
    t_depart: float = 0.0
    t_arrive: float = 0.0
    response: float = 0.0
    components: dict = field(default_factory=dict)
    transported: bool = False
    hospital: str = ""
    nearest_open: str = ""
    t_free: float = 0.0
    route_dark: list = field(default_factory=list)
    events: list = field(default_factory=list)
    rec: dict = field(default_factory=dict)
    held_by: str = ""

    @property
    def late(self) -> bool:
        return self.response > 8.0

    def summary(self) -> dict:
        return {"iid": self.iid, "t_call": self.t_call, "origin": self.origin, "priority": self.priority,
                "response": round(self.response, 2), "unit": self.unit, "hospital": self.hospital,
                "components": {k: round(v, 2) for k, v in self.components.items()},
                "queue_wait": round(self.queue_wait, 2), "route_dark": len(self.route_dark),
                "block": self.block, "held_by": self.held_by}


@dataclass
class EMSResult:
    incidents: list
    order: list                    # processing order (iids)
    kept: int = 0
    resumed_at: float | None = None

    @property
    def events(self) -> list:
        return [e for i in self.incidents for e in i.events]

    def by_id(self) -> dict:
        return {i.iid: i for i in self.incidents}


class EMSModel:
    def __init__(self, city, roads, scenario, eff, noise, infra):
        self.city, self.roads, self.sc, self.eff, self.noise, self.infra = city, roads, scenario, eff, noise, infra
        self.p = eff.params
        self.road_p = {k: self.p[k] for k in ("dark_cap", "vc_cap", "bpr_alpha", "bpr_beta", "ev_gain", "dark_delay", "spillback")}
        self.cells = city.cells
        self.signal_nodes = {sid: s["node"] for sid, s in city.signals.items()}
        self.stations = city.stations
        self.units = []
        for s in city.stations:
            for k in range(s["units"] + eff.extra_units.get(s["id"], 0)):
                self.units.append((f"A{s['id'][1:]}-{k + 1}", s))
        # per-cell population shares by feeder for the outage surge
        self.cell_feeders = []
        for c in self.cells:
            pops = {}
            for b in c["blocks"]:
                fid = city.feeders[city.block_feeder[b]]["id"]
                pops[fid] = pops.get(fid, 0.0) + float(city.block_pop[b])
            total = sum(pops.values()) or 1.0
            self.cell_feeders.append({f: v / total for f, v in pops.items()})

    # -- input views ------------------------------------------------------------
    def input_timelines(self) -> dict:
        """Everything this block reads, as timelines (used to compute tau*)."""
        inf = self.infra
        out = {}
        for sid in self.city.signals:
            out[f"signal:{sid}"] = inf.value(f"signal:{sid}")
        for k in self.city.towers:
            out[f"tower:{k['id']}"] = inf.value(f"tower:{k['id']}")
        for h in self.city.hospitals:
            v = inf.value(f"hospital:{h['id']}")
            out[f"hospital:{h['id']}:div"] = v["diversion"]
            out[f"hospital:{h['id']}:unp"] = v["unpowered"]
        for d in self.city.dispatch:
            out[f"dispatch:{d['id']}"] = inf.value(f"dispatch:{d['id']}")
        for f in self.city.feeders:
            out[f"feeder:{f['id']}"] = inf.feeder(f["id"])
        return out

    def dark_nodes(self, t: float) -> frozenset:
        tb = math.floor(t / 5.0) * 5.0  # signal state sampled at the start of a 5-min bin
        out = []
        for sid, node in self.signal_nodes.items():
            if self.infra.value(f"signal:{sid}").at(tb) is not None:
                out.append(node)
        return frozenset(out)

    def tod(self, t: float) -> float:
        return self.p["heat_mult"] * (self.p["evening_mult"] if 17 * 60 <= t < 21 * 60 else 1.0)

    # -- demand -------------------------------------------------------------------
    def candidates(self) -> list[Incident]:
        p, nz, city = self.p, self.noise, self.city
        per_res_min = p["calls_per_1000_day"] / 1000.0 / 1440.0
        surge_max = 1.0 + p["outage_surge"] + p["resp_surge"]
        out = []
        for ci, cell in enumerate(self.cells):
            if not cell["blocks"]:
                continue
            base_max = cell["pop"] * per_res_min * p["heat_mult"] * max(p["evening_mult"], 1.0)
            lam_max = base_max * surge_max * 1.05
            if lam_max <= 0:
                continue
            t, k = self.sc.t_start, 0
            pops = [float(city.block_pop[b]) + 1e-6 for b in cell["blocks"]]
            while True:
                t = t + nz.exponential(1.0 / lam_max, f"C{ci:02d}", "gap", k)
                if t >= self.sc.t_end:
                    break
                iid = f"I-{ci:02d}-{k:03d}"
                base = cell["pop"] * per_res_min * self.tod(t)
                f_out, f_long, cause_feeders = self._outage_share(ci, t)
                if nz.below(base / lam_max, f"C{ci:02d}", "acc", k):
                    b = cell["blocks"][nz.choice_index(pops, f"C{ci:02d}", "block", k)]
                    out.append(Incident(iid, q(t), int(b), ci, "base"))
                else:
                    surge = base * (p["outage_surge"] * f_out + p["resp_surge"] * f_long)
                    if surge > 0 and nz.below(surge / (lam_max - base), f"C{ci:02d}", "acc2", k):
                        fid, ev = cause_feeders[nz.choice_index([w for _, w, _ in cause_feeders],
                                                                f"C{ci:02d}", "cause", k)][::2]
                        fblocks = [b for b in cell["blocks"]
                                   if city.feeders[city.block_feeder[b]]["id"] == fid]
                        b = fblocks[nz.choice_index([float(city.block_pop[x]) + 1e-6 for x in fblocks],
                                                    f"C{ci:02d}", "block", k)]
                        out.append(Incident(iid, q(t), int(b), ci, "induced", cause=ev))
                k += 1
        # crashes at dark intersections
        rate = p["crash_rate_dark_per_h"] / 60.0
        if rate > 0:
            for sid, s in self.city.signals.items():
                tl = self.infra.value(f"signal:{sid}")
                if not tl:
                    continue
                t, k = self.sc.t_start, 0
                while True:
                    t = t + nz.exponential(1.0 / rate, f"signal:{sid}", "crash_gap", k)
                    if t >= self.sc.t_end:
                        break
                    iv = tl.at(t)
                    if iv is not None:
                        out.append(Incident(f"C-{sid}-{k}", q(t), int(s["block"]), int(city.block_cell[s["block"]]),
                                            "crash", cause=iv.on, priority=2))
                    k += 1
        return out

    def _outage_share(self, ci: int, t: float):
        f_out = f_long = 0.0
        causes = []
        onset = self.p["resp_onset_min"]
        for fid, share in self.cell_feeders[ci].items():
            iv = self.infra.feeder(fid).at(t)
            if iv is None:
                continue
            f_out += share
            w = self.p["outage_surge"] * share
            if t - iv.a >= onset:
                f_long += share
                w += self.p["resp_surge"] * share
            causes.append((fid, w, iv.on))
        return f_out, f_long, causes

    # -- dispatch -------------------------------------------------------------------
    def _answer(self, inc: Incident) -> list[str]:
        """Call processing, telecom and dispatch-centre delays. Returns cause ids."""
        nz, p, city = self.noise, self.p, self.city
        causes = []
        call = nz.lognormal(p["callproc_median_min"], SPREAD["callproc"], inc.iid, "callproc")
        tower = city.towers[city.block_tower[inc.block]]
        tiv = self.infra.value(f"tower:{tower['id']}").at(inc.t_call)
        tele = 0.0
        if tiv is not None:
            tele = nz.lognormal(p["telecom_penalty_median_min"], SPREAD["telecom"], inc.iid, "telecom")
            causes.append(tiv.on)
        t = inc.t_call + call + tele
        d1 = self.infra.value("dispatch:D1").at(t)
        disp = 0.0
        if d1 is not None:
            causes.append(d1.on)
            ready = d1.a + p["failover_min"]
            disp = (ready - t) if t < ready else p["backup_dispatch_penalty_min"]
            if self.infra.value("dispatch:D2").at(t) is not None:
                disp += 6.0
        inc.components = {"call": call, "telecom": tele, "dispatch": disp}
        inc.t_answer = q(t)
        inc.t_disp = q(t + disp)
        if inc.priority == 3 and inc.origin != "crash":
            inc.priority = 1 + nz.choice_index(PRIORITY_P, inc.iid, "priority")
        return causes

    def _serve(self, inc: Incident, free_at: dict, last_clear: dict, last_disp: dict, answer_causes: list):
        nz, p, roads = self.noise, self.p, self.roads
        scene_node = int(self.city.block_node[inc.block])
        tab = roads.tables(self.dark_nodes(inc.t_disp), inc.t_disp, self.road_p)
        best, best_eta, nearest, nearest_tt = None, math.inf, None, math.inf
        for uid, st in self.units:
            tt = float(tab["fwd"][roads.fwd_row[st["node"]], scene_node])
            if tt < nearest_tt:
                nearest, nearest_tt = uid, tt
            eta = max(free_at.get(uid, -math.inf), inc.t_disp) + tt + 0.05 * nz.gumbel(inc.iid, uid, "choice")
            if eta < best_eta:
                best, best_eta = (uid, st), eta
        uid, st = best
        causes = [inc.events[0].eid] + answer_causes
        inc.unit = uid
        ready = max(free_at.get(uid, -math.inf), inc.t_disp)
        inc.queue_wait = max(0.0, ready - inc.t_disp)
        if inc.queue_wait > 0 and uid in last_clear:
            causes.append(last_clear[uid])
        if nearest != uid and free_at.get(nearest, -math.inf) > inc.t_disp and nearest in last_disp:
            inc.held_by = last_disp[nearest][1]
            causes.append(last_disp[nearest][0])
        disp = Event(inc.t_disp, "ems.dispatched", inc.iid,
                     {"unit": uid, "queue_wait": round(inc.queue_wait, 2), "held_by": inc.held_by,
                      "topo": {"op": "open", "src": f"unit:{uid}", "dst": f"incident:{inc.iid}", "rel": "SERVES"}},
                     causes, "mechanism:dispatch" + (":resource_contention" if inc.held_by else ""))
        inc.events.append(disp)
        turnout = nz.lognormal(p["turnout_median_min"], SPREAD["turnout"], inc.iid, "turnout")
        if self.infra.value(f"station:{st['id']}").at(ready) is not None:
            turnout += 0.5
        inc.t_depart = q(ready + turnout)
        dark = self.dark_nodes(inc.t_depart)
        tab = roads.tables(dark, inc.t_depart, self.road_p)
        travel = float(tab["fwd"][roads.fwd_row[st["node"]], scene_node])
        inc.t_arrive = q(inc.t_depart + travel)
        inc.response = inc.t_arrive - inc.t_call
        inc.components.update({"queue": inc.queue_wait, "turnout": turnout, "travel": travel})
        route_causes = []
        if dark:
            for n in roads.path(tab, st["node"], scene_node):
                sid = self.city.signal_of_node.get(n)
                if sid and n in dark:
                    iv = self.infra.value(f"signal:{sid}").at(math.floor(inc.t_depart / 5.0) * 5.0)
                    if iv is not None:
                        inc.route_dark.append(sid)
                        route_causes.append(iv.on)
        arr = Event(inc.t_arrive, "ems.arrived", inc.iid,
                    {"unit": uid, "response": round(inc.response, 2), "late": inc.late, "priority": inc.priority,
                     "travel": round(travel, 2), "dark_on_route": inc.route_dark[:12]},
                    [disp.eid] + route_causes[:12], "mechanism:travel")
        inc.events.append(arr)
        scene = nz.lognormal(p["scene_median_min"], SPREAD["scene"], inc.iid, "scene")
        p_tr = p["p_transport_hi"] if inc.priority <= 2 else p["p_transport_lo"]
        t_leave = q(inc.t_arrive + scene)
        if nz.below(p_tr, inc.iid, "transport"):
            inc.transported = True
            tab = roads.tables(self.dark_nodes(t_leave), t_leave, self.road_p)
            best_h, best_t, nearest_h, nearest_t = None, math.inf, None, math.inf
            div_causes = []
            for hi, h in enumerate(self.city.hospitals):
                tt = float(tab["rev_h"][hi, scene_node])
                if tt < nearest_t:
                    nearest_h, nearest_t = h, tt
                div = self.infra.value(f"hospital:{h['id']}")["diversion"].at(t_leave)
                if div is not None:
                    if tt < best_t:
                        div_causes.append((tt, div.on))
                    continue
                if tt < best_t:
                    best_h, best_t = h, tt
            if best_h is None:
                best_h, best_t = nearest_h, nearest_t
            inc.hospital = best_h["id"]
            inc.nearest_open = nearest_h["id"]
            t_hosp = q(t_leave + best_t)
            offload = nz.lognormal(p["offload_median_min"], SPREAD["offload"], inc.iid, "offload")
            unp = self.infra.value(f"hospital:{best_h['id']}")["unpowered"].at(t_hosp)
            causes_d = [arr.eid] + [c for tt, c in div_causes if tt < best_t]
            if unp is not None:
                offload += p["unpowered_offload_extra_min"]
                causes_d.append(unp.on)
            dl = Event(t_hosp, "ems.delivered", inc.iid,
                       {"hospital": best_h["id"], "transport_min": round(best_t, 2),
                        "diverted_from": nearest_h["id"] if nearest_h is not best_h else ""},
                       causes_d, "mechanism:transport")
            inc.events.append(dl)
            t_clear = q(t_hosp + offload)
            tab_c = roads.tables(self.dark_nodes(t_clear), t_clear, self.road_p)
            back = float(tab_c["fwd"][roads.fwd_row[best_h["node"]], st["node"]])
            last = dl.eid
        else:
            t_clear = t_leave
            back = travel
            last = arr.eid
        clear = Event(t_clear, "ems.cleared", inc.iid, {"unit": uid}, [last], "mechanism:unit_cycle")
        inc.events.append(clear)
        inc.t_free = q(t_clear + back)
        free_at[uid] = inc.t_free
        last_clear[uid] = clear.eid
        last_disp[uid] = (disp.eid, inc.iid)

    def run(self, base: EMSResult | None = None, tau: float | None = None) -> EMSResult:
        cands = self.candidates()
        kept: dict[str, Incident] = {}
        if base is not None and tau is not None:
            base_index = base.by_id()
            for iid in base.order:
                inc = base_index[iid]
                if inc.t_free < tau and inc.t_call < tau:
                    kept[iid] = inc
                else:
                    break
        todo = []
        for inc in cands:
            if inc.iid in kept:
                continue
            self.noise.begin_scope()
            call_ev = Event(inc.t_call, "incident.call", inc.iid,
                            {"cell": inc.cell, "block": inc.block, "origin": inc.origin},
                            [inc.cause] if inc.cause else [],
                            {"base": "exogenous:demand", "induced": "mechanism:outage_surge",
                             "crash": "mechanism:dark_signal_crash"}[inc.origin])
            inc.events = [call_ev]
            causes = self._answer(inc)
            inc.rec = self.noise.end_scope()
            todo.append((inc.t_disp, inc.iid, inc, causes))
        free_at, last_clear, last_disp = {}, {}, {}
        order = []
        for iid, inc in kept.items():
            self.noise.absorb(inc.rec)
            free_at[inc.unit] = inc.t_free
            last_clear[inc.unit] = inc.events[-1].eid
            last_disp[inc.unit] = (inc.events[1].eid, inc.iid)
            order.append(iid)
        todo.sort(key=lambda x: (x[0], x[1]))
        for _, iid, inc, causes in todo:
            self.noise.begin_scope()
            self.noise.absorb(inc.rec)
            # call event records the priority after it is drawn
            inc.events[0] = Event(inc.t_call, "incident.call", inc.iid,
                                  {"cell": inc.cell, "block": inc.block, "origin": inc.origin,
                                   "priority": inc.priority},
                                  [inc.cause] if inc.cause else [], inc.events[0].rule)
            self._serve(inc, free_at, last_clear, last_disp, causes)
            inc.rec = self.noise.end_scope()
            order.append(iid)
        incidents = list(kept.values()) + [x[2] for x in todo]
        return EMSResult(incidents, order, kept=len(kept), resumed_at=tau)


def first_divergence(a: dict, b: dict) -> float | None:
    """tau*: earliest time any shared input timeline differs between two worlds."""
    best = None
    for k in set(a) | set(b):
        ta, tb = a.get(k, EMPTY), b.get(k, EMPTY)
        if ta.digest() == tb.digest():
            continue
        d = ta.first_difference(tb)
        if d is not None and (best is None or d < best):
            best = d
    return best
