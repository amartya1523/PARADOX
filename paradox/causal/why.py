"""WHY queries: explain an outcome with evidence, not correlation.

``WHY incident:<id>`` returns five things:
1. a response-time decomposition, set against the same incident in the
   *nominal* world (every root fault neutralised);
2. the provenance DAG of the arrival event, each edge labelled with its
   dependency class and evidence basis;
3. candidate causes (root events plus conditions on the path), with but-for
   effects, minimal flipping sets and Chockler–Halpern responsibility;
4. the probability of necessity for the leading causes under two noise
   couplings, with a coupling-sensitivity flag;
5. a plain-language narrative.

Blame attaches to components and conditions, never to people (red-team #1).

``WHY <entity>`` (a hospital, signal, feeder ...) walks the lineage of the
entity's first bad-state event.
"""

from __future__ import annotations

from ..store.tgraph import DEPENDENCY_CLASS
from .actual import (SERVICE_NOISE, OutcomeProbe, incident_conditions, minimal_flipping_sets,
                     probability_of_necessity, responsibility, root_candidates)

BAD_KINDS = ("fault.", "feeder.down", "signal.dark", "tower.battery_depleted", "zone.depressurized",
             "hospital.unpowered", "hospital.diversion_on", "hospital.water_lost", "gen.start_failed",
             "gen.fuel_exhausted", "dispatch.down", "power.lost", "tie.blocked")

RULE_CLASS = {
    "radial_supply": "physical", "transfer": "physical", "battery": "physical", "ups": "physical",
    "no_backup": "physical", "storage_tank": "physical", "utility_feed": "physical",
    "backup_power": "physical", "day_tank": "physical", "ats_start": "physical",
    "manual_restart": "physical", "water_pressure": "physical", "ups_exhausted": "physical",
    "dispatch": "logical", "travel": "geographic", "transport": "geographic", "unit_cycle": "logical",
    "outage_surge": "logical", "dark_signal_crash": "geographic", "diversion": "logical",
    "transfer_arbiter": "physical", "repair": "physical", "return_to_normal": "physical",
}


def _cls(rule: str) -> str:
    parts = rule.split(":")
    if parts[0] in ("exogenous", "noise", "intervention"):
        return parts[0]
    return RULE_CLASS.get(parts[1] if len(parts) > 1 else parts[0], "logical")


def _clock(t: float) -> str:
    t = int(round(t))
    return f"{t // 60:02d}:{t % 60:02d}"


def provenance(px, branch: str, eid: str, depth: int = 10) -> dict:
    lin = px.store.lineage(eid, branch, depth=depth)
    nodes = [{"eid": e.eid, "t": e.t, "clock": _clock(e.t), "kind": e.kind, "entity": e.entity,
              "rule": e.rule, "class": _cls(e.rule), "depth": lin["depth"].get(e.eid, 0),
              "attrs": {k: v for k, v in e.attrs.items() if k != "topo"}}
             for e in sorted(lin["nodes"].values(), key=lambda e: e.t)]
    edges = [{"from": a, "to": b} for a, b in lin["edges"]]
    return {"nodes": nodes, "edges": edges}


def why_incident(px, iid: str, branch: str = "main", pn_reps: int = 12) -> dict:
    world = px.world(branch)
    inc = world.incident(iid)
    if inc is None:
        raise KeyError(f"no incident {iid} on {branch}")
    target = world.params["target_response_min"]
    late = inc.response > target

    def outcome(w):
        j = w.incident(iid)
        return j is not None and (j.response > target if late else j.response > inc.response - 1.0)

    probe = OutcomeProbe(px, outcome, base_world=world)
    roots = root_candidates(world)
    conds = incident_conditions(px, world, inc)
    cands = roots + conds
    nominal_ivs = tuple(iv for c in roots for iv in c.neutraliser)
    nominal = probe.world(nominal_ivs)
    twin = nominal.incident(iid)

    but_for = []
    for c in cands:
        w = probe.world(c.neutraliser)
        j = w.incident(iid)
        but_for.append({**c.to_dict(), "response_without": round(j.response, 2) if j else None,
                        "delta": round(inc.response - j.response, 2) if j else None,
                        "incident_exists": j is not None, "flips": not outcome(w)})
    sets = minimal_flipping_sets(probe, cands, 3)
    resp = responsibility(cands, sets)
    for b in but_for:
        b["responsibility"] = resp[b["key"]]
    ranked = sorted(but_for, key=lambda b: (-b["responsibility"], -(b["delta"] or 0)))
    pn = []
    for b in ranked[:2]:
        c = next(x for x in cands if x.key == b["key"])
        shared = probability_of_necessity(probe, c, pn_reps)
        alt = probability_of_necessity(probe, c, pn_reps, fresh=SERVICE_NOISE)
        pn.append({"key": c.key, "label": c.label, "pn_shared": shared, "pn_redrawn": alt,
                   "coupling_sensitive": (abs(shared - alt) > 0.2) if alt == alt else True})

    decomposition = {}
    for k, v in inc.components.items():
        w = twin.components.get(k, 0.0) if twin else 0.0
        decomposition[k] = {"actual": round(v, 2), "nominal": round(w, 2), "delta": round(v - w, 2)}
    arrived = next(e for e in inc.events if e.kind == "ems.arrived")
    prov = provenance(px, branch, arrived.eid)
    result = {
        "target": iid, "branch": branch, "priority": inc.priority, "origin": inc.origin,
        "call": _clock(inc.t_call), "response": round(inc.response, 2), "target_min": target,
        "late": late, "unit": inc.unit, "hospital": inc.hospital, "held_by": inc.held_by,
        "route_dark": sorted(set(inc.route_dark)),
        "nominal_response": round(twin.response, 2) if twin else None,
        "exists_in_nominal": twin is not None,
        "decomposition": decomposition, "candidates": ranked, "minimal_sets": [list(s) for s in sets],
        "probability_of_necessity": pn, "provenance": prov, "counterfactual_runs": probe.runs,
        "caveats": ["Model-relative counterfactual; the noise coupling is assumed, not identified.",
                    "Responsibility uses contingencies over the candidate set only (no frozen mediators).",
                    "Blame attaches to components and conditions, not to people."],
    }
    result["narrative"] = narrative(result, inc, twin)
    return result


def narrative(r: dict, inc, twin) -> str:
    parts = [f"Incident {r['target']} (P{r['priority']}, {r['origin']}, called {r['call']}) was reached in "
             f"{r['response']:.1f} min against an {r['target_min']:.0f}-min target."]
    if twin is None:
        parts.append("In the nominal world, with no root faults, this call never happens. It is itself a "
                     "product of the outage.")
    else:
        parts.append(f"In the nominal world the same patient is reached in {twin.response:.1f} min.")
    big = sorted(((k, v["delta"]) for k, v in r["decomposition"].items() if abs(v["delta"]) >= 0.3),
                 key=lambda kv: -abs(kv[1]))
    if big:
        parts.append("The gap is made of " + ", ".join(f"{k} {d:+.1f} min" for k, d in big) + ".")
    if r["held_by"]:
        parts.append(f"The nearest ambulance was still committed to {r['held_by']}, so the call queued "
                     "behind it (resource coupling).")
    if r["route_dark"]:
        parts.append(f"The route crossed {len(r['route_dark'])} dark intersection(s): "
                     + ", ".join(r["route_dark"][:4]) + ("…" if len(r["route_dark"]) > 4 else "") + ".")
    top = [c for c in r["candidates"] if c["responsibility"] > 0]
    if top:
        parts.append("Responsible causes: " + "; ".join(
            f"{c['label']} (responsibility {c['responsibility']:.2f})" for c in top[:3]) + ".")
    for p in r["probability_of_necessity"]:
        flag = " This verdict is coupling-sensitive." if p["coupling_sensitive"] else ""
        parts.append(f"Probability that removing \"{p['label']}\" would have changed the outcome: "
                     f"{p['pn_shared']:.2f} (shared noise) / {p['pn_redrawn']:.2f} (re-drawn service noise).{flag}")
    return " ".join(parts)


def why_entity(px, entity: str, branch: str = "main") -> dict:
    events = px.store.events(branch, entity=entity)
    bad = [e for e in events if e.kind.startswith(BAD_KINDS)]
    if not bad:
        good = [e for e in events if e.kind in ("gen.started", "feeder.restored", "tie.closed")]
        return {"target": entity, "branch": branch, "bad": False,
                "narrative": f"{entity} never entered a bad state on {branch}."
                             + (f" Protective events: {', '.join(sorted({e.kind for e in good}))}." if good else ""),
                "events": [e.to_dict() for e in events[:40]]}
    first = bad[0]
    prov = provenance(px, branch, first.eid)
    chain = sorted(prov["nodes"], key=lambda n: n["t"])
    text = f"{entity}: {first.kind} at {_clock(first.t)}. Causal chain: " + " → ".join(
        f"{n['kind']} {n['entity']} ({n['clock']}, {n['class']})" for n in chain)
    return {"target": entity, "branch": branch, "bad": True, "first": first.to_dict(), "provenance": prov,
            "narrative": text, "events": [e.to_dict() for e in events[:60]],
            "classes": sorted({DEPENDENCY_CLASS.get(n.get("rel", ""), n["class"]) for n in chain})}


def why_not(px, entity: str, branch: str = "main") -> dict:
    """Why did something NOT go wrong? List the protective mechanisms that fired."""
    events = px.store.events(branch, entity=entity)
    protective = [e for e in events if e.kind in ("gen.started", "gen.restarted", "feeder.restored", "tie.closed")]
    if entity.startswith("signal:"):
        s = px.city.signals[entity.split(":")[1]]
        if s["ups_h"] > 0:
            protective.append(None)
    lines = []
    for e in protective:
        if e is None:
            lines.append(f"{entity} has a UPS battery ({px.city.signals[entity.split(':')[1]]['ups_h']:.1f} h).")
        else:
            lines.append(f"{e.kind} at {_clock(e.t)} via {e.rule}")
    return {"target": entity, "branch": branch, "protective": lines,
            "narrative": (f"{entity} was protected by: " + "; ".join(lines)) if lines
            else f"{entity} was simply outside the reach of every fault on {branch}."}
