"""Synthetic telemetry for the time-series layer.

The world's timelines are turned into sensor streams, about 2.3M rows for
the reference evening:

* a 15-minute smart-meter reading for each of the 50,000 buildings;
* per-minute feeder loading;
* per-minute signal and hospital status.

Everything is vectorised. The streams are derived *from* the causal
record, so the time-series layer agrees with the event layer by
construction.
"""

from __future__ import annotations

import numpy as np

from ..store.timeseries import Hypertable


def _down_matrix(timelines, t: np.ndarray) -> np.ndarray:
    m = np.zeros((len(timelines), len(t)), bool)
    for i, tl in enumerate(timelines):
        for iv in tl:
            m[i] |= (t >= iv.a) & (t < iv.b)
    return m


def build_telemetry(city, world, seed: int = 0) -> Hypertable:
    rng = np.random.default_rng(seed)
    sc = world.scenario
    ht = Hypertable(chunk_minutes=60)
    inf = world.infra

    # per-minute feeder load (MW), zero when de-energised
    tm = np.arange(sc.t_report, sc.t_end, 1.0)
    fdown = _down_matrix([inf.feeder(f["id"]) for f in city.feeders], tm)
    diurnal = 0.85 + 0.25 * np.exp(-((tm - 1110) / 120) ** 2)
    m_load = ht.metric_id("load_mw")
    for i, f in enumerate(city.feeders):
        e = ht.entity_id(f"feeder:{f['id']}")
        v = f["load_mw"] * world.params["load_factor"] * diurnal * (1 + 0.02 * rng.standard_normal(len(tm)))
        ht.insert(tm, e, m_load, np.where(fdown[i], 0.0, v))

    # 15-minute building meters (kW)
    t15 = np.arange(sc.t_report, sc.t_end, 15.0)
    fdown15 = _down_matrix([inf.feeder(f["id"]) for f in city.feeders], t15)
    bf = city.block_feeder[city.bldg_block]
    d15 = 0.85 + 0.25 * np.exp(-((t15 - 1110) / 120) ** 2)
    kw = city.bldg_kw[:, None] * d15[None, :] * rng.lognormal(0, 0.08, (len(city.bldg_kw), len(t15)))
    kw = np.where(fdown15[bf], 0.0, kw)
    nb = len(city.bldg_kw)
    ids = np.array([ht.entity_id(f"bldg:{i:05d}") for i in range(nb)], np.int32)
    m_kw = ht.metric_id("meter_kw")
    ht.insert(np.tile(t15, nb), np.repeat(ids, len(t15)), m_kw, kw.ravel())

    # per-minute signal status (1 = operating, 0 = dark)
    m_sig = ht.metric_id("signal_ok")
    sids = list(city.signals)
    sdown = _down_matrix([inf.value(f"signal:{s}") for s in sids], tm)
    for i, s in enumerate(sids):
        ht.insert(tm, ht.entity_id(f"signal:{s}"), m_sig, (~sdown[i]).astype(np.float32))

    # per-minute hospital power source: 2 utility, 1 generator, 0 none
    m_pw = ht.metric_id("power_source")
    for h in city.hospitals:
        v = inf.value(f"hospital:{h['id']}")
        lost = _down_matrix([v["lost"]], tm)[0]
        unp = _down_matrix([v["unpowered"]], tm)[0]
        ht.insert(tm, ht.entity_id(f"hospital:{h['id']}"), m_pw, np.where(unp, 0, np.where(lost, 1, 2)))
    return ht
