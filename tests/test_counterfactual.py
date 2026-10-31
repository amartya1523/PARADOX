"""Verification of the counterfactual engine (docs/ANALYSIS.md section 5)."""

import itertools
import math

import numpy as np
import pytest

from paradox.city import generate_city
from paradox.sim.interventions import Fault, Intervention as I, Scenario
from paradox.sim.noise import NoiseTape, Posterior
from paradox.sim.world import Simulator


@pytest.fixture(scope="module")
def sim():
    return Simulator(generate_city(seed=7))


INTERVENTIONS = [
    I("never_fails", "substation:S3"),
    I("fails_at", "substation:S5", 19 * 60, 120),
    I("repairs_in", "substation:S3", 90),
    I("gen_starts", "hospital:H04", 0.17),
    I("maintenance", "hospital:H04"),
    I("flisr", "*"),
    I("flisr", "feeder:F3-1"),
    I("ups", "*", 4),
    I("battery", "*", 12),
    I("add_units", "station:E05", 2),
    I("set", "dark_cap", 0.6),
    I("gen_starts", "dispatch:D1", 0.17),
]


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_incremental_equals_full_replay(sim, seed):
    """The single most important property: early cutoff + tau* resume is exact."""
    base = sim.run(noise=seed)
    combos = [(iv,) for iv in INTERVENTIONS] + list(itertools.combinations(INTERVENTIONS[:6], 2))
    for ivs in combos:
        inc = sim.run(ivs, noise=seed, base=base)
        full = sim.run(ivs, noise=seed)
        assert inc.head() == full.head(), ivs
        assert inc.kpis == full.kpis, ivs


def test_incremental_from_record_under_posterior_noise(sim):
    """Posterior replicates may reuse the factual world; the result must not change."""
    base = sim.run(noise=9)
    for rep in range(3):
        for ivs in ([I("never_fails", "substation:S3")], [I("maintenance", "hospital:H04")],
                    [I("ups", "*", 4), I("flisr", "*")]):
            inc = sim.run(ivs, noise=Posterior(base.noise, rep), base=base)
            full = sim.run(ivs, noise=Posterior(base.noise, rep))
            assert inc.stats["incremental"] and inc.head() == full.head()


def test_counterfactual_runs_do_not_touch_the_factual_record(sim):
    w = sim.run(noise=12)
    before = (dict(w.noise.points), dict(w.noise.intervals))
    sim.run([I("never_fails", "substation:S3")], noise=w.noise.fork_same(), base=w)
    sim.run([I("maintenance", "hospital:H04")], noise=Posterior(w.noise, 0), base=w)
    assert (w.noise.points, w.noise.intervals) == before


def test_incremental_actually_reuses_work(sim):
    base = sim.run(noise=0)
    cf = sim.run([I("ups", "signal:I-00-00" if "I-00-00" in sim.city.signals else
                    "signal:" + next(iter(sim.city.signals)), 6)], noise=0, base=base)
    assert cf.stats["infra_reused"] > 250
    gen = sim.run([I("gen_starts", "hospital:H04")], noise=0, base=base)
    assert gen.stats["ems_kept"] > 0 and gen.stats["infra_recomputed"] <= 3


def test_replay_determinism_and_order_independence(sim):
    a = sim.run([I("flisr", "*"), I("ups", "*", 4)], noise=3)
    b = sim.run([I("ups", "*", 4), I("flisr", "*")], noise=3)  # commutativity
    c = Simulator(generate_city(seed=7)).run([I("flisr", "*"), I("ups", "*", 4)], noise=3)
    assert a.head() == b.head() == c.head()


def test_posterior_factual_arm_reproduces_the_record(sim):
    """Consistency axiom: abduction then no intervention returns the observed world."""
    w = sim.run(noise=11)
    for rep in range(3):
        again = sim.run(noise=Posterior(w.noise, rep))
        assert again.head() == w.head()


def test_observation_is_respected_in_every_replicate(sim):
    w = sim.run(noise=5)
    hid = sim.scenario.observations[0].entity.split(":")[1]
    for rep in range(5):
        r = sim.run(noise=Posterior(w.noise, rep))
        assert any(e.kind == "gen.start_failed" and e.entity == f"generator:{hid}" for e in r.events)


def test_parametric_counterfactual_uses_truncated_posterior(sim):
    """Observed failure-to-start at p=0.06; with maintenance p=0.01 it should still
    fail in about 1/6 of posterior replicates (probability-of-necessity style)."""
    w = sim.run(noise=5)
    hid = sim.scenario.observations[0].entity.split(":")[1]
    fails = 0
    n = 120
    for rep in range(n):
        r = sim.run([I("maintenance", f"hospital:{hid}")], noise=Posterior(w.noise, rep))
        fails += any(e.kind == "gen.start_failed" and e.entity == f"generator:{hid}" for e in r.events)
    assert 0.06 < fails / n < 0.32


def test_cone_locality(sim):
    """Entities outside the intervention's reach keep identical histories."""
    base = sim.run(noise=0)
    cf = sim.run([I("ups", "*", 4)], noise=0, base=base)
    for key in base.infra.order:
        if key.startswith(("sub:", "grid", "tower:", "zone:")):
            assert base.infra.nodes[key].digest == cf.infra.nodes[key].digest, key
    # incidents that finished before the fault are untouched
    before = [i for i in base.ems.incidents if i.t_free < 18 * 60 + 15]
    cfi = cf.ems.by_id()
    assert before and all(cfi[i.iid].events[-1].eid == i.events[-1].eid for i in before)


def test_noise_addresses_are_stable_under_path_changes(sim):
    """Removing the fault must not shift the luck of incidents it never touched."""
    base = sim.run(noise=2)
    cf = sim.run([I("never_fails", "substation:S3")], noise=2)
    shared = [a for a in base.noise.points if a in cf.noise.points and a[1] == "scene"]
    assert shared and all(base.noise.points[a] == cf.noise.points[a] for a in shared)


def test_matched_population_is_identical_across_worlds(sim):
    base = sim.run(noise=4)
    cf = sim.run([I("never_fails", "substation:S3")], noise=4)
    b = {i.iid for i in base.incidents() if i.origin == "base"}
    c = {i.iid for i in cf.incidents() if i.origin == "base"}
    assert b == c


def test_monotonicity_more_failure_never_helps_infrastructure(sim):
    for seed in range(3):
        a = sim.run(noise=seed)
        b = sim.run([I("fails_at", "substation:S7", 20 * 60, 90)], noise=seed)
        for k in ("power.customer_hours", "signals.dark_hours", "hospital.unpowered_min"):
            assert b.kpis[k] >= a.kpis[k] - 1e-9, k


def test_extreme_conditions(sim):
    calm = sim.run([I("never_fails", "substation:S3")], noise=0)
    assert calm.kpis["power.customer_hours"] == 0
    assert calm.kpis["hospital.unpowered_min"] == 0 and calm.kpis["ems.n_induced"] == 0
    fleet = sim.run([I("never_fails", "substation:S3")] +
                    [I("add_units", f"station:{s['id']}", 6) for s in sim.city.stations], noise=0)
    assert fleet.kpis["ems.queue_wait_mean"] == 0


def test_conservation_of_ambulances(sim):
    w = sim.run(noise=2)
    by_unit = {}
    for inc in sorted(w.ems.incidents, key=lambda i: i.t_disp):
        prev = by_unit.get(inc.unit)
        if prev is not None:
            # a unit never serves two incidents at once
            assert inc.t_disp + inc.queue_wait >= prev.t_free - 1e-6
        by_unit[inc.unit] = inc
    assert all(i.t_arrive >= i.t_disp >= i.t_call for i in w.ems.incidents)


def _erlang_c(c, a):
    s = sum(a ** k / math.factorial(k) for k in range(c))
    top = a ** c / math.factorial(c) * c / (c - a)
    return top / (s + top)


def test_ems_queue_reproduces_the_erlang_c_knee(sim):
    """Analytic sanity check (Sargent operational validity).

    With outage and traffic effects off, sweep offered load. P(wait) must rise
    monotonically and sharply past rho ~ 0.7 and stay within an envelope of the
    M/M/c (Erlang C) prediction. It is not an identity: dispatch is
    earliest-arrival over space, so a call sometimes waits for a busy nearby unit
    rather than take a free distant one, which inflates P(wait) at low load.
    """
    calm = Scenario("calm", (), (), t_start=0.0, t_report=180.0, t_end=1440.0)
    c = len(sim.city.stations)
    rows = []
    for rate in (0.4, 0.7, 0.85):
        params = {"evening_mult": 1.0, "heat_mult": 1.0, "calls_per_1000_day": rate}
        loads, waits, wq = [], [], []
        for seed in range(2):
            w = sim.run(noise=seed, params=params, scenario=calm)
            inc = [i for i in w.ems.incidents if i.t_call >= 180]
            busy = np.mean([i.t_free - i.t_disp - i.queue_wait for i in inc])
            loads.append(len(inc) / (1440 - 180) * busy)
            waits.append(np.mean([i.queue_wait > 0.01 for i in inc]))
            wq.append(np.mean([i.queue_wait for i in inc]))
        a = float(np.mean(loads))
        rows.append((a / c, float(np.mean(waits)), _erlang_c(c, a) if a < c else 1.0, float(np.mean(wq))))
    p_wait = [r[1] for r in rows]
    assert p_wait == sorted(p_wait)
    assert rows[-1][3] > 5 * rows[0][3]
    for rho, sim_w, erl, _ in rows:
        assert abs(sim_w - erl) < 0.2, (rho, sim_w, erl)
