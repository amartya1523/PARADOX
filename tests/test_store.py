import math

import numpy as np
import pytest

from paradox.store import ChronoStore, Event, Hypertable, TemporalGraph


def chain(n, t0=0.0, entity="x"):
    evs, prev = [], None
    for i in range(n):
        ev = Event(t0 + i, "tick", entity, {"i": i}, [prev] if prev else [], "test")
        evs.append(ev)
        prev = ev.eid
    return evs


def test_causal_ids_propagate_divergence():
    a = chain(5)
    root = Event(0, "tick", "x", {"i": 99}, [], "test")
    b = [root] + [Event(e.t, e.kind, e.entity, e.attrs, [root.eid] if i == 0 else [], e.rule)
                  for i, e in enumerate(a[1:])]
    assert a[1].eid != b[1].eid  # different cause => different identity
    assert Event(1, "tick", "x", {"i": 1}, [a[0].eid], "test").eid == a[1].eid


def test_numpy_values_hash_like_python_values():
    assert Event(1, "k", "e", {"v": np.float64(0.5), "n": np.int64(3)}).eid == \
        Event(1, "k", "e", {"v": 0.5, "n": 3}).eid
    assert Event(1, "k", "e", {"b": np.bool_(False)}).eid == Event(1, "k", "e", {"b": False}).eid
    with pytest.raises(TypeError):
        Event(1, "k", "e", {"v": {1, 2}})


def test_branch_overlay_and_isolation():
    st = ChronoStore()
    st.append("main", chain(10))
    head_before = st.head("main")
    st.create_branch("alt", "main", fork_t=5)
    alt_events = [Event(t, "tick", "x", {"alt": True}, [], "test") for t in (5, 6, 7)]
    st.append("alt", alt_events)
    ids = [st.get(e).t for e in st.iter_ids("alt")]
    assert ids == [0, 1, 2, 3, 4, 5, 6, 7]
    assert st.head("main") == head_before
    d = st.diff("main", "alt")
    assert d["shared"] == 5 and len(d["only_a"]) == 5 and len(d["only_b"]) == 3
    assert st.verify("alt")["ok"]


def test_branch_cannot_write_before_fork():
    st = ChronoStore()
    st.create_branch("alt", fork_t=10)
    with pytest.raises(ValueError):
        st.append("alt", [Event(3, "k", "e")])


def test_bitemporal_correction_is_atomic_and_as_of_works():
    st = ChronoStore()
    ev = Event(10, "fault", "substation:S3", {"cause": "transformer"})
    tx1 = st.append("main", [ev])
    new = st.correct("main", ev.eid, t=8)
    tx2 = st.tx
    assert [e.t for e in st.events("main", known_at=tx1)] == [10]
    assert [e.t for e in st.events("main", known_at=tx2)] == [8]
    assert st.events("main")[0].attrs["corrects"] == ev.eid
    assert new.eid != ev.eid


def test_reassert_after_retract_keeps_history():
    st = ChronoStore()
    ev = Event(1, "k", "e")
    tx1 = st.append("main", [ev])
    tx2 = st.retract("main", ev.eid)
    tx3 = st.append("main", [ev])
    assert len(st.events("main", known_at=tx1)) == 1
    assert len(st.events("main", known_at=tx2)) == 0
    assert len(st.events("main", known_at=tx3)) == 1
    with pytest.raises(KeyError):
        st.retract("main", "nope")


def test_branch_does_not_see_parent_corrections_after_fork():
    st = ChronoStore()
    ev = Event(1, "k", "e")
    st.append("main", [ev])
    st.create_branch("alt", fork_t=5)
    st.correct("main", ev.eid, t=2)
    assert [e.t for e in st.events("alt")] == [1]
    assert [e.t for e in st.events("main")] == [2]


def test_tamper_detection():
    st = ChronoStore()
    st.append("main", chain(4))
    assert st.verify("main")["ok"]
    b = st.branch("main")
    tx, op, eid = b._log[1]
    b._log[1] = (tx, op, b._log[2][2])
    assert not st.verify("main")["ok"]


def test_lineage_bfs_finds_shallow_path():
    st = ChronoStore()
    r = Event(0, "root", "r")
    a = Event(1, "a", "a", causes=[r.eid])
    b = Event(2, "b", "b", causes=[a.eid])
    c = Event(3, "c", "c", causes=[b.eid, r.eid])
    st.append("main", [r, a, b, c])
    lin = st.lineage(c.eid, "main", depth=1)
    assert r.eid in lin["nodes"] and a.eid not in lin["nodes"]


def test_save_load_roundtrip(tmp_path):
    st = ChronoStore()
    st.append("main", chain(6))
    st.create_branch("alt", fork_t=3)
    st.append("alt", [Event(4, "k", "e", {"v": 1.5})])
    st.save(tmp_path / "db")
    st2 = ChronoStore.load(tmp_path / "db")
    assert st2.head("alt") == st.head("alt")
    assert st2.verify("alt")["ok"]
    assert [e.eid for e in st2.events("alt")] == [e.eid for e in st.events("alt")]


def test_temporal_graph_as_of_matches_naive():
    g = TemporalGraph()
    rng = np.random.default_rng(0)
    for i in range(50):
        g.add_node(f"n{i}")
    for k in range(2000):
        g.add_delta(float(rng.uniform(0, 100)), f"n{rng.integers(50)}", str(rng.integers(3)), seq=k)
    g.add_edge("n1", "n2", "POWERS")
    g.add_edge("n2", "n3", "POWERS", valid_from=40, valid_to=60)
    g.build_index()
    for t in (0, 13.3, 50, 99.9):
        assert g.state_as_of(t) == g.state_as_of(t, use_checkpoints=False)
    assert [d["node"] for d in g.dependents("n1", 50)] == ["n2", "n3"]
    assert [d["node"] for d in g.dependents("n1", 70)] == ["n2"]
    assert g.state_of("missing", 10) is None


def test_hypertable_query_and_rollup():
    ht = Hypertable(chunk_minutes=60)
    e = ht.entity_id("feeder:F1")
    m = ht.metric_id("load_mw")
    t = np.arange(0, 600, 1.0)
    ht.insert(t, e, m, np.ones_like(t) * 2.0)
    ht.insert(np.array([0.1]), e, m, np.array([4.0]))
    tq, vq = ht.query("feeder:F1", "load_mw", 0.1, 0.1)
    assert len(tq) == 1 and vq[0] == 4.0  # exact-boundary hit (float64 time)
    tq, _ = ht.query("feeder:F1", "load_mw", 100, 200)
    assert len(tq) == 101
    assert ht.chunks_scanned("feeder:F1", 100, 200) == 3  # chunks 1..3 of 10
    r = ht.rollup(60, "load_mw")
    assert math.isclose(r["mean"][0], (60 * 2 + 4) / 61)
    assert ht.rollup(60, "missing")["count"].size == 0
