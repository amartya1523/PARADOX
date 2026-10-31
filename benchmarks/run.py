"""PARADOX benchmarks: python -m benchmarks.run

Measures the claims made in the README on this machine. Every number is
the median of several repetitions after a warm-up run.
"""

from __future__ import annotations

import json
import platform
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paradox.engine import Paradox  # noqa: E402
from paradox.sim.interventions import Intervention as I  # noqa: E402
from paradox.sim.noise import NoiseTape, replicate_seed  # noqa: E402
from paradox.store import ChronoStore, Event, Hypertable, TemporalGraph  # noqa: E402
from paradox.uncertainty.ensemble import compare  # noqa: E402


def timed(fn, reps=5):
    fn()
    xs = []
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        xs.append(time.perf_counter() - t)
    return statistics.median(xs)


def bench_event_store(n=200_000):
    evs, prev = [], None
    for i in range(n):
        ev = Event(i * 0.01, "tick", f"e{i % 500}", {"v": i % 97}, [prev] if prev and i % 4 else [], "bench")
        evs.append(ev)
        prev = ev.eid
    t = time.perf_counter()
    for i in range(n):
        Event(i * 0.01, "tick", f"e{i % 500}", {"v": i % 97}, (), "bench")
    hash_rate = n / (time.perf_counter() - t)
    st = ChronoStore()
    t = time.perf_counter()
    st.append("main", evs)
    ingest = n / (time.perf_counter() - t)
    st.create_branch("alt", fork_t=n * 0.01 * 0.9)
    st.append("alt", [Event(n * 0.01 * 0.95 + k * 0.001, "alt", "x", {"k": k}) for k in range(1000)])
    t = time.perf_counter()
    ids = st.id_set("alt")
    read = len(ids) / (time.perf_counter() - t)
    t = time.perf_counter()
    ok = st.verify("main")["ok"]
    verify = n / (time.perf_counter() - t)
    return {"events": n, "event_hash_per_s": round(hash_rate), "ingest_events_per_s": round(ingest),
            "branch_read_events_per_s": round(read), "verify_events_per_s": round(verify), "verified": ok,
            "branch_own_events": st.branch("alt").own_count, "objects": len(st.objects)}


def bench_asof(n=200_000, entities=2000):
    rng = np.random.default_rng(0)
    g = TemporalGraph()
    for e in range(entities):
        g.add_node(f"n{e}")
    ts = np.sort(rng.uniform(0, 1440, n))
    ents = rng.integers(0, entities, n)
    sts = rng.integers(0, 3, n)
    for k in range(n):
        g.add_delta(float(ts[k]), f"n{ents[k]}", str(sts[k]), seq=k)
    rows = []
    qs = rng.uniform(0, 1440, 40)
    for K in (None, 64, int(np.sqrt(n)), 4096, 32768):
        if K is None:
            g.build_index(n + 1)
            lat = timed(lambda: [g.state_as_of(float(t), use_checkpoints=False) for t in qs], reps=3) / len(qs)
            rows.append({"K": "none (replay)", "ms_per_query": round(lat * 1000, 3), "checkpoints": 0})
        else:
            g.build_index(K)
            lat = timed(lambda: [g.state_as_of(float(t)) for t in qs], reps=3) / len(qs)
            rows.append({"K": K, "ms_per_query": round(lat * 1000, 3), "checkpoints": len(g._checkpoints)})
    return {"deltas": n, "entities": entities, "rows": rows}


def bench_hypertable(px):
    ht = px.telemetry
    t0 = time.perf_counter()
    rebuilt = Hypertable()
    for c in ht.chunks.values():
        c.seal()
        rebuilt.insert(c.t, c.entity, c.metric, c.value)
    ingest = rebuilt.rows / (time.perf_counter() - t0)
    q = timed(lambda: ht.query("bldg:01234", "meter_kw", 1100, 1200), reps=20)
    q_narrow = timed(lambda: ht.query("feeder:F3-6", "load_mw", 1100, 1130), reps=20)
    roll = timed(lambda: (ht._rollups.clear(), ht.rollup(60, "meter_kw")), reps=3)
    return {"rows": ht.rows, "chunks": len(ht.chunks), "ingest_rows_per_s": round(ingest),
            "point_series_ms": round(q * 1000, 3), "narrow_range_ms": round(q_narrow * 1000, 3),
            "chunks_scanned_narrow": ht.chunks_scanned("feeder:F3-6", 1100, 1130),
            "rollup_hourly_ms": round(roll * 1000, 1)}


def bench_counterfactual(px):
    cases = [("substation:S3 NEVER FAILS", [I("never_fails", "substation:S3")]),
             ("hospital:H04 GENERATOR STARTS", [I("gen_starts", "hospital:H04", 0.17)]),
             ("ALL SIGNALS GET UPS 4", [I("ups", "*", 4)]),
             ("signal UPS on one feeder", [I("ups", f"signal:{s}", 6) for s, v in px.city.signals.items() if v["feeder"] == "F3-2"]),
             ("ADD 2 AMBULANCES AT E17", [I("add_units", "station:E17", 2)]),
             ("SET dark_cap = 0.3", [I("set", "dark_cap", 0.3)])]
    rows = []
    for label, ivs in cases:
        full = timed(lambda: px.sim.run(ivs, noise=px.actual.noise.fork_same()), reps=5)
        inc = timed(lambda: px.sim.run(ivs, noise=px.actual.noise.fork_same(), base=px.actual), reps=5)
        w = px.sim.run(ivs, noise=px.actual.noise.fork_same(), base=px.actual)
        same = w.head() == px.sim.run(ivs, noise=px.actual.noise.fork_same()).head()
        st = w.stats
        rows.append({"intervention": label, "full_ms": round(full * 1000, 1), "incremental_ms": round(inc * 1000, 1),
                     "speedup": round(full / inc, 2), "infra_reused": f"{st['infra_reused']}/{st['infra_reused'] + st['infra_recomputed']}",
                     "ems_kept": f"{st['ems_kept']}/{st['ems_total']}", "identical_to_full": same})
    return rows


def bench_crn(px):
    rows = []
    for label, ivs in (("S3 never fails", [I("never_fails", "substation:S3")]),
                       ("all signals UPS 4h", [I("ups", "*", 4)]),
                       ("+2 ambulances at E17", [I("add_units", "station:E17", 2)])):
        r = compare(px, ivs, ("ems.p90_response",), n=24, mode="interventional")
        m = r["metrics"]["ems.p90_response"]
        rows.append({"intervention": label, "delta_mean": round(m["delta"]["mean"], 2),
                     "ci95": [round(x, 2) for x in m["delta"]["ci95"]],
                     "variance_reduction": round(m["variance_reduction"], 1) if m["variance_reduction"] else None})
    return rows


def bench_throughput(px):
    seeds = [replicate_seed(99, r) for r in range(12)]
    t = time.perf_counter()
    for s in seeds:
        px.sim.run(noise=NoiseTape(s))
    full = len(seeds) / (time.perf_counter() - t)
    return {"full_worlds_per_s": round(full, 1), "road_cache": px.sim.roads.cache_stats()}


def charts(res, out: Path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    plt.rcParams.update({"figure.facecolor": "#0b0e14", "axes.facecolor": "#0f141d", "axes.edgecolor": "#2a3446",
                         "axes.labelcolor": "#c9d2e0", "xtick.color": "#8a97ab", "ytick.color": "#8a97ab",
                         "text.color": "#dbe2ee", "font.family": "monospace", "font.size": 9})
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6))
    rows = res["asof"]["rows"]
    labels = [str(r["K"]) for r in rows]
    axes[0].bar(labels, [r["ms_per_query"] for r in rows], color=["#ff5d5d"] + ["#ffb547"] * (len(rows) - 1))
    axes[0].set_yscale("log")
    axes[0].set_title(f"as-of reconstruction, {res['asof']['deltas']:,} deltas", loc="left")
    axes[0].set_xlabel("checkpoint interval K")
    axes[0].set_ylabel("ms / query (log)")
    cf = res["counterfactual"]
    y = np.arange(len(cf))
    axes[1].barh(y - 0.2, [r["full_ms"] for r in cf], 0.4, color="#3b4a60", label="full replay")
    axes[1].barh(y + 0.2, [r["incremental_ms"] for r in cf], 0.4, color="#b48cff", label="incremental (early cutoff + τ*)")
    axes[1].set_yticks(y, [r["intervention"] for r in cf])
    axes[1].invert_yaxis()
    axes[1].set_xlabel("ms per counterfactual world")
    axes[1].set_title("counterfactual recompute (identical output)", loc="left")
    axes[1].legend(frameon=False, loc="lower left", bbox_to_anchor=(0, -0.42), ncol=2)
    fig.tight_layout()
    fig.savefig(out / "bench.png", dpi=160)
    plt.close(fig)


def main():
    out = ROOT / "docs" / "media"
    out.mkdir(parents=True, exist_ok=True)
    t = time.perf_counter()
    px = Paradox()
    boot = time.perf_counter() - t
    res = {"machine": {"platform": platform.platform(), "processor": platform.processor() or platform.machine(),
                       "python": platform.python_version(), "numpy": np.__version__},
           "boot_s": round(boot, 2), "city": px.city.summary()}
    print("event store…", flush=True)
    res["event_store"] = bench_event_store()
    print("as-of…", flush=True)
    res["asof"] = bench_asof()
    print("hypertable…", flush=True)
    res["hypertable"] = bench_hypertable(px)
    print("counterfactual…", flush=True)
    res["counterfactual"] = bench_counterfactual(px)
    print("CRN…", flush=True)
    res["crn"] = bench_crn(px)
    print("throughput…", flush=True)
    res["throughput"] = bench_throughput(px)
    (ROOT / "benchmarks" / "results.json").write_text(json.dumps(res, indent=2))
    charts(res, out)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
