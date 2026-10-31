"""Materialised views over a branch: the temporal dependency graph.

Three sources make up the graph:
* static edges from the city registry;
* dynamic edges from ``topo`` open/close attributes on events, e.g. a tie
  switch closing, or an ambulance assigned to an incident;
* node-state deltas from ``state`` attributes.

Distribution transformers inherit their feeder's state through a derived
delta that carries the feeder event's id.
"""

from __future__ import annotations

from .events import ChronoStore
from .tgraph import TemporalGraph


def static_graph(city) -> TemporalGraph:
    g = TemporalGraph()
    add = g.add_node
    for s in city.substations:
        add(f"substation:{s['id']}", type="substation", cap_mw=round(s["cap_mw"], 1))
        for fid in s["feeders"]:
            g.add_edge(f"substation:{s['id']}", f"feeder:{fid}", "POWERS")
    for f in city.feeders:
        add(f"feeder:{f['id']}", type="feeder", load_mw=round(f["load_mw"], 2))
    for t in city.ties:
        add(f"tie:{t['id']}", type="tie")
        g.add_edge(f"feeder:{t['a']}", f"tie:{t['id']}", "TIES")
        g.add_edge(f"feeder:{t['b']}", f"tie:{t['id']}", "TIES")
        g.add_edge(f"tower:{t['tower']}", f"tie:{t['id']}", "CONTROLS")
    for x in city.dtx:
        add(f"dtx:{x['id']}", type="dtx", blocks=len(x["blocks"]))
        g.add_edge(f"feeder:{x['feeder']}", f"dtx:{x['id']}", "POWERS")
    for sid, s in city.signals.items():
        add(f"signal:{sid}", type="signal", ups_h=round(s["ups_h"], 1))
        g.add_edge(f"feeder:{s['feeder']}", f"signal:{sid}", "POWERS")
    for k in city.towers:
        add(f"tower:{k['id']}", type="tower", battery_h=round(k["battery_h"], 1))
        g.add_edge(f"feeder:{k['feeder']}", f"tower:{k['id']}", "POWERS")
    for p in city.pumps:
        add(f"pump:{p['id']}", type="pump")
        g.add_edge(f"feeder:{p['feeder']}", f"pump:{p['id']}", "POWERS")
    for z in city.zones:
        add(f"zone:{z['id']}", type="zone", buffer_h=round(z["buffer_h"], 1))
        g.add_edge(f"pump:{z['pump']}", f"zone:{z['id']}", "SUPPLIES_WATER")
    for h in city.hospitals:
        hid = f"hospital:{h['id']}"
        add(hid, type="hospital", beds=h["beds"])
        add(f"generator:{h['id']}", type="generator")
        add(f"fuelpump:{h['id']}", type="fuelpump", supply=h["fuelpump"])
        for f in h["feeds"]:
            g.add_edge(f"feeder:{f}", hid, "POWERS")
        g.add_edge(f"generator:{h['id']}", hid, "BACKS_UP")
        g.add_edge(f"fuelpump:{h['id']}", f"generator:{h['id']}", "SUPPLIES_FUEL")
        if h["fuelpump"] == "emergency_bus":
            g.add_edge(f"generator:{h['id']}", f"fuelpump:{h['id']}", "POWERS")
        else:
            g.add_edge(f"feeder:{h['fuelpump']}", f"fuelpump:{h['id']}", "POWERS")
        g.add_edge(f"zone:{h['zone']}", hid, "SUPPLIES_WATER")
    for d in city.dispatch:
        did = f"dispatch:{d['id']}"
        add(did, type="dispatch", role=d["role"])
        g.add_edge(f"feeder:{d['feeder']}", did, "POWERS")
        for s in city.stations:
            g.add_edge(did, f"station:{s['id']}", "DISPATCHES")
    for s in city.stations:
        add(f"station:{s['id']}", type="station", units=s["units"])
        g.add_edge(f"feeder:{s['feeder']}", f"station:{s['id']}", "POWERS")
    for k in city.towers:
        for d in city.dispatch:
            g.add_edge(f"tower:{k['id']}", f"dispatch:{d['id']}", "RELAYS_CALLS")
    return g


def derive_tgraph(store: ChronoStore, branch: str, city, known_at: int | None = None,
                  checkpoint_every: int | None = None) -> TemporalGraph:
    g = static_graph(city)
    feeder_dtx: dict[str, list[str]] = {}
    for x in city.dtx:
        feeder_dtx.setdefault(x["feeder"], []).append(f"dtx:{x['id']}")
    open_edges = {}
    seq = 0
    for ev in store.events(branch, known_at=known_at):
        st = ev.attrs.get("state")
        if st is not None:
            g.add_delta(ev.t, ev.entity, st, ev.eid, seq=seq)
            seq += 1
            if ev.entity.startswith("feeder:"):
                for d in feeder_dtx.get(ev.entity.split(":")[1], ()):
                    g.add_delta(ev.t, d, st, ev.eid, seq=seq)
                    seq += 1
        topo = ev.attrs.get("topo")
        if topo:
            key = (topo["src"], topo["dst"], topo["rel"])
            if topo["op"] == "open":
                open_edges[key] = ev.t
            elif key in open_edges:
                g.add_edge(*key, valid_from=open_edges.pop(key), valid_to=ev.t, by=ev.eid)
    for (src, dst, rel), t0 in open_edges.items():
        if rel != "SERVES":  # ambulance assignments are too numerous for the graph view
            g.add_edge(src, dst, rel, valid_from=t0, by="open")
    g.build_index(checkpoint_every)
    return g
