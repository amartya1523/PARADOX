"""End-to-end tests for the engine, PQL, causal and planning layers."""

import pytest
from fastapi.testclient import TestClient

from paradox.api.app import create_app
from paradox.causal.evidence import correlate
from paradox.causal.shapley import attribute, shapley
from paradox.causal.why import why_incident
from paradox.engine import Paradox
from paradox.pql.executor import Executor
from paradox.pql.parser import PQLError, parse
from paradox.sim.interventions import Intervention as I
from paradox.uncertainty.ensemble import compare
from paradox.uncertainty.validate import audit


@pytest.fixture(scope="module")
def px():
    return Paradox(telemetry=True)


@pytest.fixture(scope="module")
def ex(px):
    return Executor(px)


def test_record_is_materialised_and_verifiable(px):
    assert px.store.verify("main")["ok"]
    assert len(px.store.id_set("main")) == len(px.actual.events)
    assert px.telemetry.rows > 1_500_000


def test_branch_shares_prefix_and_stores_only_its_footprint(px):
    info = px.create_branch([I("never_fails", "substation:S3")])
    assert info["fork"] == "18:15"
    b = px.store.branch(info["name"])
    assert all(px.store.get(e).t >= b.fork_t for _, e in b._keys)
    assert info["diff"]["shared"] > 0 and px.store.verify(info["name"])["ok"]
    assert px.create_branch([I("never_fails", "substation:S3")])["name"] == info["name"]  # idempotent


def test_as_of_graph_reconstruction(px):
    g = px.tgraph("main")
    before = g.state_as_of(18 * 60)
    during = g.state_as_of(18 * 60 + 30)
    assert before.get("feeder:F3-6", "ok") == "ok" and during["feeder:F3-6"] == "down"
    assert g.state_as_of(18 * 60 + 30) == g.state_as_of(18 * 60 + 30, use_checkpoints=False)


def test_pql_grammar():
    q = parse("AS OF 18:40 KNOWN AT 3 IN main SHOW DEPENDENTS OF substation:S3 DEPTH 2")
    assert q["as_of"] == 1120 and q["known_at"] == 3 and q["pipeline"][0]["depth"] == 2
    q = parse("WHAT IF substation:S3 NEVER FAILS AND ALL SIGNALS GET UPS 4 AS x |> WHY incident:I-00-001")
    assert len(q["pipeline"]) == 2 and len(q["pipeline"][0]["ivs"]) == 2
    with pytest.raises(PQLError):
        parse("WHAT IF substation:S3 EXPLODES")


def test_pql_end_to_end(ex):
    r = ex.run("AS OF 18:40 SHOW DEPENDENTS OF substation:S3 DEPTH 2")
    assert r["result"]["affected"] > 5 and r["receipt"]["id"].startswith("R-")
    r = ex.run("WHAT IF hospital:H04 GENERATOR STARTS AFTER 0 COMPARE hospital.unpowered_min RUNS 6 TIER MC")
    m = r["result"]["ensemble"]["metrics"]["hospital.unpowered_min"]
    assert m["delta"]["mean"] < 0
    card = {row["factor"]: row for row in r["result"]["scorecard"]}
    assert card["Counterfactual-engine verification"]["level"] == 3
    assert card["Input pedigree"]["level"] == 1


def test_bitemporal_correction_via_pql(ex):
    before = ex.run("SHOW EVENTS OF substation:S3")["result"]["events"][0]["t"]
    r = ex.run("CORRECT substation:S3 FAULT TIME TO 18:12")["result"]
    old = ex.run(f"AS OF 23:00 KNOWN AT {r['known_before']} SHOW EVENTS OF substation:S3")["result"]["events"]
    new = ex.run("SHOW EVENTS OF substation:S3")["result"]["events"]
    assert old[0]["t"] == before == 1095 and new[0]["t"] == 1092


def test_why_incident_finds_root_cause(px):
    late = sorted((i for i in px.actual.incidents() if i.late and i.t_call > 1095), key=lambda i: -i.response)
    r = why_incident(px, late[0].iid, pn_reps=4)
    assert any(c["key"] == "substation:S3" and c["responsibility"] > 0 for c in r["candidates"])
    assert r["provenance"]["nodes"] and "narrative" in r


def test_shapley_efficiency_and_attribution(px):
    res = shapley(lambda s: 3.0 * ("a" in s) + 2.0 * ("b" in s) + 1.0 * ("a" in s and "b" in s), ["a", "b"])
    assert abs(res["phi"]["a"] - 3.5) < 1e-9 and res["efficiency_gap"] < 1e-9
    a = attribute(px, "hospital.unpowered_min")
    assert abs(sum(x["phi"] for x in a["attribution"]) - a["excess"]) < 1e-6


def test_crn_pairing_reduces_variance(px):
    r = compare(px, [I("never_fails", "substation:S3")], ("ems.p90_response",), n=12, mode="interventional")
    assert r["metrics"]["ems.p90_response"]["variance_reduction"] > 1.5


def test_correlate_detects_confounding(px):
    sig = next(s for s, v in px.city.signals.items() if v["feeder"] == "F3-2" and v["ups_h"] == 0)
    r = correlate(px, "hospital:H04", f"signal:{sig}", runs=30)
    assert "confounder" in r["verdict"] and "substation:S3" in r["common_ancestors"]


def test_audit_discovers_the_hidden_dependency(px):
    a = audit(px, per_entity=2)
    hid = a["hidden_truth_for_scoring"]["hospital"]
    assert any(f["entity"] == f"hospital:{hid}" for f in a["residual_findings"])
    loc = [m for m in a["localised_mechanisms"] if m["entity"] == f"generator:{hid}"]
    assert loc and loc[0]["grade"].startswith("D") and loc[0]["knockout"]["unpowered_after"] < loc[0]["knockout"]["unpowered_before"]


def test_api_smoke(px):
    client = TestClient(create_app(px))
    assert client.get("/api/meta").json()["city"]["buildings"] == 50_000
    assert len(client.get("/api/geometry").json()["segments"]) == px.city.n_segments
    tl = client.get("/api/timeline?branch=main").json()
    assert tl["incidents"] and "F3-6" in tl["feeders"]
    r = client.post("/api/query", json={"q": "SHOW KPIS"})
    assert r.status_code == 200
    assert client.post("/api/query", json={"q": "SHOW NONSENSE"}).status_code == 400
