"""Execute PQL against a Paradox engine.

Every result carries a header (branch, as-of, known-at), a receipt, and
for what-if answers a credibility scorecard in the spirit of
NASA-STD-7009B / ASME V&V40. The scorecard's *input pedigree* row is
permanently capped at 1 because the city is synthetic, and the card says
so instead of overclaiming.
"""

from __future__ import annotations

import math
import time

from ..causal.evidence import correlate
from ..causal.shapley import attribute
from ..causal.why import why_entity, why_incident, why_not
from ..planner.celf import plan
from ..sim.interventions import hhmm
from ..sim.params import table as params_table
from ..sim.world import KPI_INFO
from ..store.receipts import result_digest
from ..uncertainty.ensemble import compare
from ..uncertainty.sensitivity import sensitivity
from ..uncertainty.validate import audit, validate
from .parser import PQLError, parse

DEFAULT_METRICS = ["ems.p90_response", "ems.p1_late", "hospital.unpowered_min", "power.customer_hours"]


class Executor:
    def __init__(self, px):
        self.px = px
        self.last_validation = None

    def run(self, text: str) -> dict:
        t0 = time.perf_counter()
        q = parse(text)
        branch = q["branch"] or "main"
        results = []
        for st in q["pipeline"]:
            res = self.dispatch(st, branch, q)
            results.append(res)
            if res.get("branch_created"):
                branch = res["branch_created"]
        out = results[-1] if len(results) == 1 else {"kind": "pipeline", "steps": results}
        out.setdefault("kind", q["pipeline"][-1]["verb"])
        header = {"branch": branch, "as_of": hhmm(q["as_of"]) if q["as_of"] is not None else None,
                  "known_at": q["known_at"], "tx": self.px.store.tx}
        kind = "what-if" if any(s["verb"] == "whatif" for s in q["pipeline"]) else "read"
        branches = {"main", branch}
        rec = self.px.receipt(text, out, branches=sorted(branches), kind=kind,
                              seeds=[self.px.world_seed], n=out.get("n"),
                              coupling="shared-noise (posterior)" if kind == "what-if" else "n/a")
        return {"query": text, "header": header, "result": out, "receipt": rec,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000)}

    # -- dispatch -----------------------------------------------------------------
    def dispatch(self, st: dict, branch: str, q: dict) -> dict:
        v = st["verb"]
        px = self.px
        if v == "show":
            return self.show(st, branch, q)
        if v == "why":
            ref = st["ref"]
            kind, ident = ref.split(":", 1)
            if st["not"]:
                return {"kind": "why_not", **why_not(px, ref, branch)}
            if kind == "incident":
                return {"kind": "why_incident", **why_incident(px, ident, branch)}
            if kind == "kpi":
                return {"kind": "attribute", **attribute(px, ident, branch)}
            self._check_entity(ref)
            return {"kind": "why_entity", **why_entity(px, ref, branch)}
        if v == "whatif":
            for iv in st["ivs"]:
                if iv.target not in ("*",) and iv.op != "set":
                    self._check_entity(iv.target)
            info = px.create_branch(st["ivs"], st["name"])
            metrics = st["metrics"] or DEFAULT_METRICS
            for m in metrics:
                if m not in KPI_INFO:
                    raise PQLError(f"unknown metric {m!r}; try SHOW KPIS")
            ens = compare(px, st["ivs"], metrics, n=st["runs"], mode=st["mode"],
                          tier=st["tier"] if st["mode"] == "counterfactual" else "mc", coupling=st["coupling"])
            point = {m: {"actual": px.actual.kpis[m], "alternate": info["kpis"][m]} for m in metrics}
            checks = self.live_checks(st["ivs"], info)
            return {"kind": "whatif", "branch_created": info["name"], "branch": info, "point": point,
                    "ensemble": ens, "n": st["runs"], "scorecard": self.scorecard(ens, checks),
                    "disclaimer": "Model-relative counterfactual; the noise coupling is assumed, not identified; "
                                  "intervals cover Monte Carlo and parameter uncertainty, not structural error "
                                  "(see VALIDATE)."}
        if v == "attribute":
            return {"kind": "attribute", **attribute(px, st["metric"], branch)}
        if v == "plan":
            return {"kind": "plan", **plan(px, st["metric"], st["target"], st["budget"], n_train=st["runs"],
                                          n_test=2 * st["runs"], kinds=tuple(st["kinds"]) if st["kinds"] else None)}
        if v == "sensitivity":
            return {"kind": "sensitivity", **sensitivity(px, st["metric"], method=st["method"], runs=st["runs"])}
        if v == "correlate":
            self._check_entity(st["a"])
            self._check_entity(st["b"])
            return {"kind": "correlate", **correlate(px, st["a"], st["b"], runs=st["runs"])}
        if v == "audit":
            return {"kind": "audit", **audit(px)}
        if v == "validate":
            res = validate(px, m=st["runs"], k=5)
            self.last_validation = res
            return {"kind": "validate", **res}
        if v == "diff":
            a, b = st["a"], st["b"]
            d = px.store.diff(b, a)
            ev = px.store.objects
            return {"kind": "diff", "a": b, "b": a, "shared": d["shared"],
                    "only_a": [ev[e].to_dict() for e in d["only_a"][:40]], "only_b": [ev[e].to_dict() for e in d["only_b"][:40]],
                    "counts": {"only_" + b: len(d["only_a"]), "only_" + a: len(d["only_b"])},
                    "first_divergence": hhmm(min([ev[e].t for e in d["only_a"] + d["only_b"]], default=0))}
        if v == "verify":
            return self.verify(st["target"])
        if v == "trace":
            return self.trace(st, branch)
        if v == "correct":
            return self.correct(st)
        raise PQLError(f"unsupported verb {v}")

    def _check_entity(self, ref: str) -> None:
        if ref.startswith(("incident:", "kpi:")):
            return
        kind, ident = ref.split(":", 1)
        if ident == "*":
            return
        if ref not in self.px.city.registry:
            raise PQLError(f"unknown entity {ref!r}")

    # -- SHOW ------------------------------------------------------------------------
    def show(self, st: dict, branch: str, q: dict) -> dict:
        px, what = self.px, st["what"]
        t = q["as_of"] if q["as_of"] is not None else px.scenario.t_end - 1
        if what in ("dependents", "dependencies"):
            self._check_entity(st["ref"])
            g = px.tgraph(branch, q["known_at"])
            fn = g.dependents if what == "dependents" else g.dependencies
            rows = fn(st["ref"], t, depth=st["depth"])
            state = g.state_as_of(t)
            for r in rows:
                r["state"] = state.get(r["node"], "ok")
            return {"kind": "graph", "root": st["ref"], "direction": what, "as_of": hhmm(t), "rows": rows,
                    "affected": sum(1 for r in rows if r["state"] not in ("ok", "closed", "open", "running")),
                    "graph_stats": g.stats()}
        if what == "state":
            self._check_entity(st["ref"])
            g = px.tgraph(branch, q["known_at"])
            s = g.state_of(st["ref"], t)
            evs = px.store.events(branch, entity=st["ref"], t1=t, known_at=q["known_at"])
            return {"kind": "state", "entity": st["ref"], "as_of": hhmm(t), "state": s,
                    "history": [e.to_dict() for e in evs]}
        if what == "events":
            t0 = st["from"] if st["from"] is not None else -math.inf
            t1 = st["to"] if st["to"] is not None else (q["as_of"] if q["as_of"] is not None else math.inf)
            evs = px.store.events(branch, t0=t0, t1=t1, kind=st["kind"], entity=st["ref"], known_at=q["known_at"])
            return {"kind": "events", "count": len(evs), "events": [e.to_dict() for e in evs[: st["limit"]]]}
        if what == "kpis":
            w = px.world(branch)
            return {"kind": "kpis", "branch": branch,
                    "kpis": [{"name": k, "value": v, "unit": KPI_INFO[k][0], "label": KPI_INFO[k][1]}
                             for k, v in w.kpis.items()]}
        if what == "branches":
            return {"kind": "branches", "branches": px.branches(), "store": px.store.stats()}
        if what == "params":
            return {"kind": "params", "params": params_table()}
        if what == "city":
            return {"kind": "city", "summary": px.city.summary(), "scenario": px.scenario.describe(),
                    "telemetry": px.telemetry.stats() if px.telemetry else None}
        if what == "incidents":
            w = px.world(branch)
            inc = sorted(w.incidents(), key=lambda i: -i.response)
            if st["late"]:
                inc = [i for i in inc if i.late]
            return {"kind": "incidents", "count": len(inc), "incidents": [i.summary() for i in inc[: st["limit"]]]}
        if what == "receipts":
            return {"kind": "receipts", "receipts": px.ledger.list()[-30:]}
        if what == "telemetry":
            if px.telemetry is None:
                raise PQLError("telemetry layer disabled")
            ht = px.telemetry
            metric = st["metric"] or {"feeder": "load_mw", "signal": "signal_ok", "hospital": "power_source"}.get(
                st["ref"].split(":")[0], "meter_kw")
            ts, vs = ht.query(st["ref"], metric, -math.inf, math.inf)
            step = max(1, len(ts) // 240)
            return {"kind": "telemetry", "entity": st["ref"], "metric": metric, "rows": int(len(ts)),
                    "chunks_scanned": ht.chunks_scanned(st["ref"], -math.inf, math.inf),
                    "series": [[float(a), float(b)] for a, b in zip(ts[::step], vs[::step])], "store": ht.stats()}
        raise PQLError(f"SHOW {what} not supported")

    # -- other verbs --------------------------------------------------------------------
    def trace(self, st: dict, branch: str) -> dict:
        evs = self.px.store.events(branch, entity=st["ref"],
                                   t0=st["from"] if st["from"] is not None else -math.inf,
                                   t1=st["to"] if st["to"] is not None else math.inf)
        if st["ref"].startswith("incident:"):
            evs = self.px.store.events(branch, entity=st["ref"].split(":", 1)[1])
        return {"kind": "trace", "entity": st["ref"], "events": [e.to_dict() for e in evs]}

    def verify(self, target: str) -> dict:
        px = self.px
        if target in px.store.branches:
            v = px.store.verify(target)
            return {"kind": "verify", "target": target, "type": "branch", **v,
                    "head": px.store.head(target), "events_checked": len(px.store.id_set(target))}
        if target in px.ledger.receipts:
            r = px.ledger.get(target)
            again = self.run(r["query"]) if r["kind"] == "read" else None
            ok = again is not None and result_digest(again["result"]) == r["result"]
            return {"kind": "verify", "target": target, "type": "receipt", "ok": ok,
                    "note": "re-executed and compared result digests" if again is not None else
                            "what-if receipts are verified by re-running the query in a fresh session"}
        raise PQLError(f"nothing to verify called {target!r}")

    def correct(self, st: dict) -> dict:
        px = self.px
        evs = px.store.events("main", entity=st["ref"], kind="fault.")
        if not evs:
            raise PQLError(f"no fault event for {st['ref']} on main")
        before = px.store.tx
        new = px.store.correct("main", evs[0].eid, t=st["t"])
        return {"kind": "correct", "retracted": evs[0].to_dict(), "asserted": new.to_dict(),
                "known_before": before, "known_after": px.store.tx,
                "note": f"Bitemporal correction: query with KNOWN AT {before} to see the old belief. "
                        "The simulated world is not recomputed; the record of what was believed changes."}

    def live_checks(self, ivs, info) -> dict:
        """Cheap per-query verification: replay equality and abduction consistency."""
        px = self.px
        full = px.sim.run(ivs, noise=px.actual.noise.fork_same())
        from ..sim.noise import Posterior
        factual = px.sim.run((), noise=Posterior(px.actual.noise, 0))
        return {"incremental_equals_full": full.head() == px.world(info["name"]).head(),
                "posterior_reproduces_record": factual.head() == px.actual.head()}

    def scorecard(self, ens: dict, checks: dict) -> list[dict]:
        val = self.last_validation
        cov = None
        if val:
            cov = min(v["coverage_90"] for v in val["by_metric"].values())
        rows = [
            {"factor": "Counterfactual-engine verification",
             "level": 3 if all(checks.values()) else 0,
             "evidence": ("live: incremental == full replay, posterior reproduces the record; "
                          "suite: 20+ axiom/property tests") if all(checks.values()) else f"FAILED live checks {checks}"},
            {"factor": "Validation against oracle reality", "level": (2 if cov and cov >= 0.85 else 1) if val else 1,
             "evidence": f"worst 90%-interval coverage {cov:.0%}" if val else "run VALIDATE to score"},
            {"factor": "Input pedigree", "level": 1, "evidence": "SYNTHETIC city; parameters from literature (capped at 1)"},
            {"factor": "Results uncertainty", "level": 2,
             "evidence": f"n={ens['n']} paired replicates, tier={ens['tier']}, mode={ens['mode']}"},
            {"factor": "Coupling sensitivity", "level": 2 if "coupling" in ens and not ens["coupling"]["sensitive"] else 1,
             "evidence": ("coupling-sensitive" if ens.get("coupling", {}).get("sensitive") else
                          "not checked (add COUPLING)" if "coupling" not in ens else "stable under re-drawn service noise")},
            {"factor": "Use history", "level": 0, "evidence": "no real-world decisions informed"},
        ]
        verdict = "DOES NOT MEET" if rows[0]["level"] == 0 else "EXPLORATORY (synthetic, not decision-grade)"
        rows.append({"factor": "Verdict", "level": None, "evidence": verdict})
        return rows
