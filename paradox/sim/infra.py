"""The infrastructure layer: a structural causal model over timelines.

Each node computes its timeline from its parents' timelines and its own
addressed noise, and emits the events that explain it.

The executor runs nodes in topological order with **early cutoff**. A node
is reused from a baseline run when two things hold: no intervention touches
it, and its parents' output digests equal the baseline's. Every output
digest covers interval endpoints *and* their cause tags, so a reused node
is exactly what a fresh computation would have produced. The test suite
checks this against full replays.

Episode noise is keyed by the **root fault** that caused the episode, e.g.
``("tower:K07", "battery", <fault eid>)``. It is never keyed by an
occurrence count, which would shift when an intervention removes an
earlier episode.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

from ..store.events import Event
from .interventions import Effective, Scenario
from .params import SPREAD
from .timeline import EMPTY, Iv, Timeline, q


def _digest(value) -> str:
    if isinstance(value, Timeline):
        return value.digest()
    h = hashlib.blake2b(digest_size=12)
    for k in sorted(value):
        h.update(k.encode() + b"=" + _digest(value[k]).encode() + b";")
    return h.hexdigest()


@dataclass
class NodeOut:
    value: object
    events: list
    digest: str
    rec: dict
    parent_digests: tuple


@dataclass
class InfraResult:
    nodes: dict
    order: list
    recomputed: list = field(default_factory=list)
    reused: list = field(default_factory=list)

    def value(self, key: str):
        return self.nodes[key].value

    @property
    def events(self) -> list:
        return [e for k in self.order for e in self.nodes[k].events]

    def feeder(self, fid: str) -> Timeline:
        return self.nodes["grid"].value.get(fid, EMPTY)


class InfraModel:
    def __init__(self, city, scenario: Scenario, eff: Effective, noise):
        self.city = city
        self.sc = scenario
        self.eff = eff
        self.p = eff.params
        self.noise = noise
        self.T1 = scenario.t_end
        self.feeders = {f["id"]: f for f in city.feeders}
        self.subs = {s["id"]: s for s in city.substations}
        self.ties = {t["id"]: t for t in city.ties}
        self.towers = {t["id"]: t for t in city.towers}
        self.zones = {z["id"]: z for z in city.zones}
        self.pumps = {p["zone"]: p for p in city.pumps}

    # -- graph ----------------------------------------------------------------
    def plan(self) -> tuple[list, dict]:
        c = self.city
        order, parents = [], {}
        for s in c.substations:
            order.append(f"sub:{s['id']}")
            parents[order[-1]] = ()
        order.append("grid")
        parents["grid"] = tuple(f"sub:{s['id']}" for s in c.substations)
        for k in c.towers:
            order.append(f"tower:{k['id']}")
            parents[order[-1]] = ("grid",)
        for sid in c.signals:
            order.append(f"signal:{sid}")
            parents[order[-1]] = ("grid",)
        for z in c.zones:
            order.append(f"zone:{z['id']}")
            parents[order[-1]] = ("grid",)
        for h in c.hospitals:
            order.append(f"hospital:{h['id']}")
            parents[order[-1]] = ("grid", f"zone:{h['zone']}")
        for d in c.dispatch:
            order.append(f"dispatch:{d['id']}")
            parents[order[-1]] = ("grid",)
        for e in c.stations:
            order.append(f"station:{e['id']}")
            parents[order[-1]] = ("grid",)
        return order, parents

    def run(self, base: InfraResult | None = None, touched: set | None = None) -> InfraResult:
        order, parents = self.plan()
        touched = touched or set()
        everything = "*" in touched or base is None
        nodes: dict[str, NodeOut] = {}
        res = InfraResult(nodes, order)
        for key in order:
            pd = tuple(nodes[k].digest for k in parents[key])
            prev = None if everything else base.nodes.get(key)
            if prev is not None and key not in touched and prev.parent_digests == pd:
                self.noise.absorb(prev.rec)
                nodes[key] = prev
                res.reused.append(key)
                continue
            self.noise.begin_scope()
            value, events = self.mechanism(key, nodes)
            rec = self.noise.end_scope()
            nodes[key] = NodeOut(value, events, _digest(value), rec, pd)
            res.recomputed.append(key)
        return res

    def mechanism(self, key: str, nodes: dict):
        kind, ident = key.split(":", 1) if ":" in key else (key, "")
        return getattr(self, f"_{kind}")(ident, nodes)

    # -- mechanisms -----------------------------------------------------------
    def _sub(self, sid: str, nodes):
        ent = f"substation:{sid}"
        ivs, events = [], []
        for f in self.eff.faults:
            if f.entity != ent:
                continue
            repair = self.eff.repair.get(ent)
            if repair is None:
                repair = f.repair_min if f.repair_min is not None else self.noise.lognormal(
                    self.p["repair_median_min"], SPREAD["repair"], ent, "repair", int(round(f.t)))
            fault = Event(f.t, "fault.substation", ent, {"state": "down"}, (), "exogenous:scenario")
            events.append(fault)
            t_back = q(f.t + repair)
            off = ""
            if t_back < self.T1:
                up = Event(t_back, "substation.restored", ent, {"state": "ok", "repair_min": round(repair, 2)},
                           [fault.eid], "mechanism:repair")
                events.append(up)
                off = up.eid
            ivs.append(Iv(f.t, min(t_back, self.T1 + 1), fault.eid, off, fault.eid))
        return Timeline(ivs), events

    def _scada_up(self, tie: dict, t: float, nodes) -> bool:
        """SCADA reach at a tie: is its telecom tower still on battery or grid?

        Evaluated against the pre-restoration substation outage (a one-step lag
        that keeps the SCM acyclic; documented as an approximation).
        """
        tower = self.towers[tie["tower"]]
        sub = self.feeders[tower["feeder"]]["sub"]
        iv = nodes[f"sub:{sub}"].value.at(t)
        if iv is None:
            return True
        return (t - iv.a) < self.eff.battery.get(tower["id"], tower["battery_h"]) * 60

    def _grid(self, _ident, nodes):
        out: dict[str, Timeline] = {}
        events: list[Event] = []
        sub_tl = {sid: nodes[f"sub:{sid}"].value for sid in self.subs}
        requests = []
        downs = {}
        for sid, tl in sub_tl.items():
            for iv in tl:
                for fid in self.subs[sid]["feeders"]:
                    ev = Event(iv.a, "feeder.down", f"feeder:{fid}", {"state": "down", "sub": sid},
                               [iv.on], "mechanism:radial_supply")
                    events.append(ev)
                    downs[(fid, iv.a)] = (ev, iv)
                    f = self.feeders[fid]
                    if not f["ties"]:
                        continue
                    if fid in self.eff.flisr:
                        mode, med = "flisr", self.p["transfer_flisr_min"]
                    elif any(self._scada_up(self.ties[t], iv.a, nodes) for t in f["ties"]):
                        mode, med = "remote", self.p["transfer_remote_min"]
                    else:
                        mode, med = "manual", self.p["transfer_manual_min"]
                    delay = self.noise.lognormal(med, SPREAD["transfer"], f"feeder:{fid}", "transfer", iv.root)
                    requests.append((q(iv.a + delay), fid, iv.a, mode, delay))
        # transfer arbiter: neighbour substations hand out headroom first-come first-served
        lf = self.p["load_factor"]
        headroom = {sid: s["cap_mw"] - s["load_mw"] * lf for sid, s in self.subs.items()}
        active: list[tuple[float, str, float]] = []
        transfers = {}
        for t_ready, fid, a, mode, delay in sorted(requests):
            ev_down, iv = downs[(fid, a)]
            if t_ready >= iv.b:
                continue
            for tl_end, sid, load in list(active):
                if tl_end <= t_ready:
                    headroom[sid] += load
                    active.remove((tl_end, sid, load))
            load = self.feeders[fid]["load_mw"] * lf
            options = []
            for tid in self.feeders[fid]["ties"]:
                tie = self.ties[tid]
                other = tie["b"] if tie["a"] == fid else tie["a"]
                osub = self.feeders[other]["sub"]
                if sub_tl[osub].at(t_ready) is None:
                    options.append((-headroom[osub], tid, other, osub))
            chosen = None
            for _, tid, other, osub in sorted(options):
                if headroom[osub] >= load:
                    chosen = (tid, other, osub)
                    break
            if chosen is None:
                reason = "no neighbour headroom" if options else "neighbour substations down"
                events.append(Event(t_ready, "tie.blocked", f"feeder:{fid}",
                                    {"reason": reason, "mode": mode}, [ev_down.eid], "mechanism:transfer_arbiter"))
                continue
            tid, other, osub = chosen
            nxt = [j.a for j in sub_tl[osub] if j.a > t_ready]
            until = min([iv.b] + nxt)
            closed = Event(t_ready, "tie.closed", f"tie:{tid}",
                           {"state": "closed", "from": other, "to": fid, "mode": mode,
                            "delay_min": round(delay, 2),
                            "topo": {"op": "open", "src": f"feeder:{other}", "dst": f"feeder:{fid}",
                                     "rel": "POWERS"}},
                           [ev_down.eid], f"mechanism:transfer:{mode}")
            back = Event(t_ready, "feeder.restored", f"feeder:{fid}", {"state": "ok", "via": tid},
                         [closed.eid], "mechanism:transfer")
            events += [closed, back]
            headroom[osub] -= load
            active.append((until, osub, load))
            transfers[(fid, a)] = (t_ready, until, closed, back, osub)
        for sid, tl in sub_tl.items():
            for iv in tl:
                for fid in self.subs[sid]["feeders"]:
                    ev_down, _ = downs[(fid, iv.a)]
                    tr = transfers.get((fid, iv.a))
                    ivs = out.setdefault(fid, [])
                    if tr is None:
                        off = ""
                        if iv.off:
                            up = Event(iv.b, "feeder.restored", f"feeder:{fid}", {"state": "ok", "via": "substation"},
                                       [iv.off], "mechanism:radial_supply")
                            events.append(up)
                            off = up.eid
                        ivs.append(Iv(iv.a, iv.b, ev_down.eid, off, iv.root))
                        continue
                    t_ready, until, closed, back, osub = tr
                    ivs.append(Iv(iv.a, t_ready, ev_down.eid, back.eid, iv.root))
                    tie_id = closed.entity
                    if until >= iv.b:
                        if iv.off:
                            events.append(Event(iv.b, "tie.opened", tie_id,
                                                {"state": "open", "topo": {"op": "close", "src": f"feeder:{closed.attrs['from']}",
                                                                           "dst": f"feeder:{fid}", "rel": "POWERS"}},
                                                [iv.off], "mechanism:return_to_normal"))
                    else:
                        cause = next(j.on for j in sub_tl[osub] if j.a == until)
                        trip = Event(until, "feeder.down", f"feeder:{fid}", {"state": "down", "sub": osub, "via": tie_id},
                                     [cause, closed.eid], "mechanism:transfer_lost")
                        events.append(trip)
                        off = ""
                        if iv.off:
                            up = Event(iv.b, "feeder.restored", f"feeder:{fid}", {"state": "ok", "via": "substation"},
                                       [iv.off], "mechanism:radial_supply")
                            events.append(up)
                            off = up.eid
                        ivs.append(Iv(until, iv.b, trip.eid, off, iv.root))
        return {fid: Timeline(v) for fid, v in out.items()}, events

    def _buffered(self, ent: str, mech: str, feeder_tl: Timeline, minutes_nominal: float,
                  spread: float, kind_down: str, kind_up: str, rule: str, attr: str):
        """Generic 'buffer then fail' mechanism (batteries, UPS, storage tanks)."""
        ivs, events = [], []
        for iv in feeder_tl:
            if minutes_nominal <= 0:
                life = 0.0
            else:
                life = self.noise.lognormal(minutes_nominal, spread, ent, mech, iv.root)
            t_fail = q(iv.a + life)
            if t_fail >= iv.b:
                continue
            down = Event(t_fail, kind_down, ent, {"state": "down", attr: round(life, 2)}, [iv.on], rule)
            events.append(down)
            off = ""
            if iv.off and iv.b <= self.T1:
                up = Event(iv.b, kind_up, ent, {"state": "ok"}, [iv.off, down.eid], "mechanism:power_restored")
                events.append(up)
                off = up.eid
            ivs.append(Iv(t_fail, iv.b, down.eid, off, iv.root))
        return Timeline(ivs), events

    def _tower(self, kid: str, nodes):
        t = self.towers[kid]
        hours = self.eff.battery.get(kid, t["battery_h"])
        return self._buffered(f"tower:{kid}", "battery", self._feeder(nodes, t["feeder"]), hours * 60,
                              SPREAD["battery"], "tower.battery_depleted", "tower.restored",
                              "mechanism:battery", "battery_min")

    def _signal(self, sid: str, nodes):
        s = self.city.signals[sid]
        hours = self.eff.ups.get(sid, s["ups_h"])
        return self._buffered(f"signal:{sid}", "ups", self._feeder(nodes, s["feeder"]), hours * 60,
                              SPREAD["ups"], "signal.dark", "signal.restored",
                              "mechanism:ups" if hours else "mechanism:no_backup", "ups_min")

    def _zone(self, zid: str, nodes):
        z = self.zones[zid]
        pump = self.pumps[zid]
        return self._buffered(f"zone:{zid}", "buffer", self._feeder(nodes, pump["feeder"]), z["buffer_h"] * 60,
                              SPREAD["buffer"], "zone.depressurized", "zone.repressurized",
                              "mechanism:storage_tank", "buffer_min")

    @staticmethod
    def _feeder(nodes, fid: str) -> Timeline:
        return nodes["grid"].value.get(fid, EMPTY)

    def _backup(self, ent: str, gen_ent: str, lost: Timeline, p_fts: float, forced: float | None,
                fuel_feeder_tl: Timeline | None, daytank_h: float | None):
        """Generator start/restart/fuel logic shared by hospitals and dispatch centres.

        Returns (unavailable timeline, events, per-episode info).
        """
        ivs, events = [], []
        for iv in lost:
            lost_ev = Event(iv.a, "power.lost", ent, {"state": "on_battery"}, [iv.on], "mechanism:utility_feed")
            events.append(lost_ev)
            if forced is not None:
                start = q(iv.a + forced)
                g = Event(start, "gen.started", gen_ent, {"state": "running", "forced": True},
                          [lost_ev.eid], "intervention:do")
                events.append(g)
                gap_on = lost_ev.eid
            elif not self.noise.below(p_fts, gen_ent, "gen_start", iv.root):
                start = q(iv.a + self.p["gen_transfer_min"])
                g = Event(start, "gen.started", gen_ent, {"state": "running"}, [lost_ev.eid], "mechanism:ats_start")
                events.append(g)
                gap_on = lost_ev.eid
            else:
                fail = Event(iv.a, "gen.start_failed", gen_ent, {"state": "failed", "p_fts": p_fts},
                             [lost_ev.eid], "noise:gen_start")
                events.append(fail)
                gap_on = fail.eid
                delay = self.noise.lognormal(self.p["gen_restart_median_min"], SPREAD["restart"], gen_ent,
                                             "restart_delay", iv.root)
                if not self.noise.below(1 - self.p["p_restart"], gen_ent, "gen_restart", iv.root):
                    start = q(iv.a + delay)
                    g = Event(start, "gen.restarted", gen_ent, {"state": "running", "delay_min": round(delay, 2)},
                              [fail.eid], "mechanism:manual_restart")
                    events.append(g)
                else:
                    start = math.inf
                    g = None
                    if iv.a + delay < iv.b:
                        events.append(Event(iv.a + delay, "gen.restart_failed", gen_ent, {"state": "failed"},
                                            [fail.eid], "noise:gen_restart"))
            if start - iv.a > 1.0:
                ivs.append(Iv(iv.a, min(start, iv.b), gap_on, g.eid if g and start < iv.b else iv.off, iv.root))
            if g is None or start >= iv.b or fuel_feeder_tl is None:
                continue
            pump_iv = fuel_feeder_tl.at(start)
            if pump_iv is None:
                continue
            exhaust = q(start + daytank_h * 60)
            stop_at = min(iv.b, pump_iv.b)
            if exhaust < stop_at:
                x = Event(exhaust, "gen.fuel_exhausted", gen_ent, {"state": "stopped", "daytank_h": round(daytank_h, 2)},
                          [g.eid, pump_iv.on], "mechanism:day_tank")
                events.append(x)
                off = iv.off if stop_at == iv.b else pump_iv.off
                ivs.append(Iv(exhaust, stop_at, x.eid, off, iv.root))
        return Timeline(ivs), events

    def _hospital(self, hid: str, nodes):
        h = next(x for x in self.city.hospitals if x["id"] == hid)
        ent = f"hospital:{hid}"
        lost = self._feeder(nodes, h["feeds"][0])
        for f in h["feeds"][1:]:
            lost = lost.intersect(self._feeder(nodes, f))
        p_fts = self.p["p_fts_maintained"] if hid in self.eff.maintained else self.p["p_fts"]
        fuel_tl = None
        if h["fuelpump"] != "emergency_bus" and hid not in self.eff.fuel_backup:
            fuel_tl = self._feeder(nodes, h["fuelpump"])
        unpowered, events = self._backup(ent, f"generator:{hid}", lost, p_fts, self.eff.gen_forced.get(ent),
                                         fuel_tl, h["daytank_h"])
        div = []
        for iv in unpowered:
            on = Event(iv.a, "hospital.unpowered", ent, {"state": "unpowered"}, [iv.on], "mechanism:backup_power")
            events.append(on)
            off = ""
            if iv.off and iv.b <= self.T1:
                pw = Event(iv.b, "hospital.powered", ent, {"state": "ok"}, [iv.off, on.eid], "mechanism:backup_power")
                events.append(pw)
                off = pw.eid
            div.append(Iv(iv.a, iv.b, on.eid, off, iv.root))
        water = nodes[f"zone:{h['zone']}"].value
        delay = self.p["water_diversion_delay_min"]
        for iv in water:
            if iv.a + delay < iv.b:
                w = Event(iv.a + delay, "hospital.water_lost", ent, {"state": "diverting", "zone": h["zone"]},
                          [iv.on], "mechanism:water_pressure")
                events.append(w)
                div.append(Iv(iv.a + delay, iv.b, w.eid, iv.off, iv.root))
        diversion = Timeline(div)
        for iv in diversion:
            events.append(Event(iv.a, "hospital.diversion_on", ent, {"diverting": True}, [iv.on], "policy:diversion"))
        return {"lost": lost, "unpowered": unpowered, "diversion": diversion}, events

    def _dispatch(self, did: str, nodes):
        d = next(x for x in self.city.dispatch if x["id"] == did)
        ent = f"dispatch:{did}"
        lost = self._feeder(nodes, d["feeder"])
        unavail, events = self._backup(ent, f"dispatch_gen:{did}", lost, d["p_fts"],
                                       self.eff.gen_forced.get(ent), None, None)
        ups = d["ups_min"]
        down = unavail.delay_onset(lambda iv: ups)
        out = []
        for iv in down:
            ev = Event(iv.a, "dispatch.down", ent, {"state": "down"}, [iv.on], "mechanism:ups_exhausted")
            events.append(ev)
            out.append(Iv(iv.a, iv.b, ev.eid, iv.off, iv.root))
        return Timeline(out), events

    def _station(self, eid_: str, nodes):
        s = next(x for x in self.city.stations if x["id"] == eid_)
        tl = self._feeder(nodes, s["feeder"])
        return tl, []
