"""Scenarios and do-operators.

An intervention replaces a structural equation, or the value of an
exogenous variable, and changes nothing else. ``touches()`` names the SCM
nodes whose equations it edits directly. Those nodes are recomputed even
when their inputs are unchanged. Every other node is reused if its parents'
outputs hash equal to the baseline (early cutoff).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Fault:
    entity: str          # e.g. "substation:S3"
    t: float             # minute of day
    repair_min: float | None = None   # None => drawn from addressed noise


@dataclass(frozen=True)
class Observation:
    """A fact in the incident record that constrains exogenous noise.

    ``("generator:H01", "gen_start", "substation:S3")`` with ``failed=True``
    says: in the outage episode rooted at S3's fault, H01's generator failed
    to start. The engine turns this into the constraint u < p_fts, so
    counterfactual replicates sample from the truncated posterior.
    """
    entity: str
    mechanism: str
    root_entity: str
    failed: bool = True


@dataclass(frozen=True)
class Scenario:
    name: str
    faults: tuple[Fault, ...]
    observations: tuple[Observation, ...] = ()
    t_start: float = 840.0      # 14:00, EMS warm-up so units are busy at 16:00
    t_report: float = 960.0     # 16:00, KPIs count incidents from here
    t_end: float = 1440.0       # 24:00

    def describe(self) -> str:
        parts = [f"{f.entity} fails at {hhmm(f.t)}" for f in self.faults]
        parts += [f"{o.entity} {o.mechanism} {'failed' if o.failed else 'succeeded'}" for o in self.observations]
        return "; ".join(parts)


HEAT_DOME = Scenario("Heat Dome, 18:15", (Fault("substation:S3", 18 * 60 + 15),))


def heat_dome(city) -> Scenario:
    """The reference scenario: S3 transformer fault at 18:15 during a heat dome.

    The incident record also says the backup generator of the first single-fed
    hospital on S3 failed to start. That is an *observation*, a constraint on
    the noise, not a choice of lucky seed.
    """
    s3 = {f["id"] for f in city.feeders if f["sub"] == "S3"}
    hosp = [h for h in city.hospitals if len(h["feeds"]) == 1 and h["feeds"][0] in s3]
    obs = (Observation(f"generator:{hosp[0]['id']}", "gen_start", "substation:S3"),) if hosp else ()
    return Scenario("Heat Dome, 18:15", HEAT_DOME.faults, obs)


def hhmm(t: float) -> str:
    t = int(round(t))
    return f"{(t // 60) % 24:02d}:{t % 60:02d}"


@dataclass(frozen=True)
class Intervention:
    op: str
    target: str = "*"
    value: float | None = None
    extra: float | None = None
    cost: float = 0.0
    label: str = field(default="", compare=False)

    def text(self) -> str:
        t, v, x = self.target, self.value, self.extra
        return {
            "never_fails": f"{t} NEVER FAILS",
            "fails_at": f"{t} FAILS AT {hhmm(v or 0)}" + (f" FOR {x:g}" if x else ""),
            "repairs_in": f"{t} REPAIRS IN {v:g}",
            "gen_starts": f"{t} GENERATOR STARTS" + (f" AFTER {v:g}" if v else ""),
            "fuel_backup": f"{t} GETS FUEL BACKUP",
            "maintenance": f"{t} GETS MAINTENANCE",
            "flisr": f"{t} GETS FLISR",
            "ups": f"{t} GETS UPS {v:g}",
            "battery": f"{t} GETS BATTERY {v:g}",
            "add_units": f"ADD {int(v or 0)} AMBULANCES AT {t}",
            "set": f"SET {t} = {v:g}",
        }.get(self.op, f"{self.op} {t}")

    def touches(self, city) -> set[str]:
        t = self.target
        if self.op in ("never_fails", "fails_at", "repairs_in"):
            return {"sub:" + t.split(":")[1]}
        if self.op == "flisr":
            return {"grid"}
        if self.op in ("gen_starts", "fuel_backup", "maintenance"):
            if t == "*":
                return {f"hospital:{h['id']}" for h in city.hospitals}
            kind, ident = t.split(":")
            return {f"{'dispatch' if kind == 'dispatch' else 'hospital'}:{ident}"}
        if self.op == "ups":
            if t == "*":
                return {f"signal:{s}" for s in city.signals}
            return {t}
        if self.op == "battery":
            if t == "*":
                return {f"tower:{k['id']}" for k in city.towers}
            return {t}
        if self.op == "add_units":
            return {"ems"}
        if self.op == "set":
            return {"*"}
        return set()


@dataclass
class Effective:
    """Interventions compiled into lookups the mechanisms consult."""
    faults: list
    repair: dict
    gen_forced: dict           # entity id (hospital:H01 / dispatch:D1) -> start delay
    fuel_backup: set
    maintained: set
    flisr: set
    ups: dict
    battery: dict
    extra_units: dict
    params: dict


def compile_interventions(scenario: Scenario, ivs, params: dict, city) -> Effective:
    faults = {f.entity: f for f in scenario.faults}
    eff = Effective([], {}, {}, set(), set(), set(), {}, {}, {}, dict(params))
    for iv in ivs:
        if iv.op == "never_fails":
            faults.pop(iv.target, None)
        elif iv.op == "fails_at":
            faults[iv.target] = Fault(iv.target, float(iv.value), iv.extra)
        elif iv.op == "repairs_in":
            eff.repair[iv.target] = float(iv.value)
        elif iv.op == "gen_starts":
            eff.gen_forced[iv.target] = float(iv.value or 0.17)
        elif iv.op == "fuel_backup":
            eff.fuel_backup |= _expand(iv.target, "hospital", [h["id"] for h in city.hospitals])
        elif iv.op == "maintenance":
            eff.maintained |= _expand(iv.target, "hospital", [h["id"] for h in city.hospitals])
        elif iv.op == "flisr":
            eff.flisr |= _expand(iv.target, "feeder", [f["id"] for f in city.feeders])
        elif iv.op == "ups":
            for s in _expand(iv.target, "signal", list(city.signals)):
                eff.ups[s] = float(iv.value)
        elif iv.op == "battery":
            for k in _expand(iv.target, "tower", [t["id"] for t in city.towers]):
                eff.battery[k] = float(iv.value)
        elif iv.op == "add_units":
            sid = iv.target.split(":")[1]
            eff.extra_units[sid] = eff.extra_units.get(sid, 0) + int(iv.value)
        elif iv.op == "set":
            if iv.target not in eff.params:
                raise KeyError(f"unknown parameter {iv.target!r}")
            eff.params[iv.target] = float(iv.value)
        else:
            raise ValueError(f"unknown intervention {iv.op!r}")
    eff.faults = sorted(faults.values(), key=lambda f: (f.t, f.entity))
    return eff


def _expand(target: str, kind: str, all_ids) -> set[str]:
    if target in ("*", f"{kind}:*"):
        return set(all_ids)
    k, ident = target.split(":", 1)
    if k != kind:
        raise ValueError(f"expected a {kind}, got {target!r}")
    return {ident}
