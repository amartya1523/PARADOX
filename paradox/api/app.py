"""REST API and static UI server.

Endpoints:

    GET  /api/meta                       city, scenario, KPI catalogue, branches
    GET  /api/geometry                   everything the map needs, once
    GET  /api/timeline?branch=           compact interval timelines + incidents for client-side scrubbing
    GET  /api/state?branch=&t=           city state at one instant (server-side as-of)
    GET  /api/graph?branch=&t=&root=     as-of dependency neighbourhood
    POST /api/query  {"q": "..."}        run PQL
    GET  /api/branches
"""

from __future__ import annotations

import math
import threading
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..engine import Paradox
from ..pql.executor import Executor
from ..pql.parser import PQLError
from ..sim.interventions import hhmm
from ..sim.params import table as params_table
from ..store.events import _plain

WEB = Path(__file__).resolve().parents[1] / "web"


class Query(BaseModel):
    q: str


def _json(obj):
    def clean(x):
        if isinstance(x, dict):
            return {str(k): clean(v) for k, v in x.items()}
        if isinstance(x, (list, tuple, set)):
            return [clean(v) for v in x]
        if isinstance(x, (np.floating, float)):
            f = float(x)
            return None if not math.isfinite(f) else f
        if isinstance(x, np.integer):
            return int(x)
        if isinstance(x, np.bool_):
            return bool(x)
        if hasattr(x, "text") and hasattr(x, "op"):
            return x.text()
        return x
    return JSONResponse(clean(obj))


def create_app(px: Paradox | None = None) -> FastAPI:
    app = FastAPI(title="PARADOX", version="0.1.0")
    state = {"px": px}
    lock = threading.Lock()

    def engine() -> Paradox:
        with lock:
            if state["px"] is None:
                state["px"] = Paradox()
                state["ex"] = Executor(state["px"])
            elif "ex" not in state:
                state["ex"] = Executor(state["px"])
        return state["px"]

    @app.get("/api/meta")
    def meta():
        p = engine()
        return _json({"city": p.city.summary(), "scenario": {"name": p.scenario.name,
                      "description": p.scenario.describe(), "t_start": p.scenario.t_report,
                      "t_end": p.scenario.t_end, "faults": [{"entity": f.entity, "t": f.t} for f in p.scenario.faults]},
                      "world_seed": p.world_seed, "kpis": p.kpi_info(), "params": params_table(),
                      "branches": p.branches(), "boot_ms": p.boot_ms,
                      "store": p.store.stats(), "telemetry": p.telemetry.stats() if p.telemetry else None})

    @app.get("/api/geometry")
    def geometry():
        p = engine()
        c = p.city
        xy = c.node_xy
        lo, hi = xy.min(0), xy.max(0)
        seen, segs = set(), []
        for a, b, k in zip(c.arc_src.tolist(), c.arc_dst.tolist(), c.arc_cls.tolist()):
            key = (min(a, b), max(a, b))
            if key not in seen:
                seen.add(key)
                segs.append([key[0], key[1], k])
        return _json({
            "bounds": [lo.tolist(), hi.tolist()], "spacing": c.spacing, "G": c.G,
            "nodes": np.round(xy, 1).tolist(), "segments": segs,
            "blocks": [[round(float(x), 1), round(float(y), 1), int(f), int(pp)] for (x, y), f, pp in
                       zip(c.block_xy, c.block_feeder, c.block_pop)],
            "feeders": [{"id": f["id"], "sub": f["sub"]} for f in c.feeders],
            "substations": [{"id": s["id"], "xy": s["xy"]} for s in c.substations],
            "hospitals": [{"id": h["id"], "node": h["node"], "feeds": h["feeds"]} for h in c.hospitals],
            "stations": [{"id": s["id"], "node": s["node"], "units": s["units"]} for s in c.stations],
            "signals": [{"id": sid, "node": s["node"], "ups": s["ups_h"] > 0} for sid, s in c.signals.items()],
            "towers": [{"id": t["id"], "xy": t["xy"]} for t in c.towers],
            "dispatch": [{"id": d["id"], "block": d["block"]} for d in c.dispatch],
            "bridges": c.bridges,
        })

    @app.get("/api/timeline")
    def timeline(branch: str = "main"):
        p = engine()
        try:
            w = p.world(branch)
        except KeyError as e:
            raise HTTPException(404, str(e))
        inf = w.infra
        iv = lambda tl: [[i.a, i.b] for i in tl]
        c = p.city
        incidents = []
        for i in w.ems.incidents:
            h = next((x for x in c.hospitals if x["id"] == i.hospital), None)
            st = next(x for x in c.stations if x["id"] == "E" + i.unit[1:].split("-")[0])
            incidents.append({"iid": i.iid, "t_call": i.t_call, "t_disp": i.t_disp, "t_depart": i.t_depart,
                              "t_arrive": i.t_arrive, "t_free": i.t_free, "node": int(c.block_node[i.block]),
                              "station": st["node"], "hospital": h["node"] if h else None,
                              "t_clear": next((e.t for e in i.events if e.kind == "ems.cleared"), i.t_free),
                              "t_hosp": next((e.t for e in i.events if e.kind == "ems.delivered"), None),
                              "t_leave": next((e.t - e.attrs["transport_min"] for e in i.events
                                               if e.kind == "ems.delivered"), None),
                              "response": round(i.response, 2), "late": i.late, "origin": i.origin,
                              "priority": i.priority, "unit": i.unit})
        ticks = [{"t": e.t, "kind": e.kind, "entity": e.entity} for e in w.events
                 if not e.kind.startswith(("ems.", "incident."))]
        return _json({
            "branch": branch, "kpis": w.kpis,
            "feeders": {f["id"]: iv(inf.feeder(f["id"])) for f in c.feeders if inf.feeder(f["id"])},
            "signals": {s: iv(inf.value(f"signal:{s}")) for s in c.signals if inf.value(f"signal:{s}")},
            "hospitals": {h["id"]: {k: iv(v) for k, v in inf.value(f"hospital:{h['id']}").items()} for h in c.hospitals},
            "towers": {t["id"]: iv(inf.value(f"tower:{t['id']}")) for t in c.towers if inf.value(f"tower:{t['id']}")},
            "zones": {z["id"]: iv(inf.value(f"zone:{z['id']}")) for z in c.zones if inf.value(f"zone:{z['id']}")},
            "dispatch": {d["id"]: iv(inf.value(f"dispatch:{d['id']}")) for d in c.dispatch},
            "substations": {s["id"]: iv(inf.value(f"sub:{s['id']}")) for s in c.substations if inf.value(f"sub:{s['id']}")},
            "incidents": incidents, "ticks": ticks})

    @app.get("/api/state")
    def state_at(branch: str = "main", t: float = 1140):
        return _json(engine().state_at(branch, t))

    @app.get("/api/graph")
    def graph(branch: str = "main", t: float = 1140, root: str = "substation:S3", depth: int = 3):
        p = engine()
        g = p.tgraph(branch)
        st = g.state_as_of(t)
        rows = g.dependents(root, t, depth=depth)
        return _json({"root": root, "t": t, "clock": hhmm(t), "rows": [{**r, "state": st.get(r["node"], "ok")} for r in rows],
                      "root_state": st.get(root, "ok")})

    @app.get("/api/branches")
    def branches():
        return _json(engine().branches())

    @app.post("/api/query")
    def query(body: Query):
        engine()
        try:
            with lock:
                res = state["ex"].run(body.q)
        except PQLError as e:
            raise HTTPException(400, f"PQL: {e}")
        except KeyError as e:
            raise HTTPException(404, str(e))
        return _json(res)

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app
