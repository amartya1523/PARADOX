"""Procedural synthetic city.

The generator builds a city at the scale of the brief:
* about 2,000 road segments on a perturbed lattice, with arterials and a
  river crossed by three bridges;
* 50,000 buildings;
* about 500 electrical components: 10 substations, 60 feeders, normally-open
  tie switches and about 370 distribution transformers;
* about 100 critical facilities: 12 hospitals, 18 EMS stations, 2 dispatch
  centres, 30 telecom towers and 20 water pumps feeding pressure zones.

None of this is a real place. Placement uses population-weighted k-means,
and service areas use Voronoi assignment. That is the usual practice for
synthetic test cities (Micropolis/Mesopolis, TAMU synthetic grids).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx
import numpy as np

from .. import MODEL_VERSION


@dataclass
class City:
    seed: int
    G: int
    spacing: float
    node_xy: np.ndarray
    arc_src: np.ndarray
    arc_dst: np.ndarray
    arc_len: np.ndarray
    arc_speed: np.ndarray
    arc_cap: np.ndarray
    arc_cls: np.ndarray
    n_segments: int
    bridges: list
    signals: dict
    signal_of_node: dict
    block_xy: np.ndarray
    block_node: np.ndarray
    block_cell: np.ndarray
    block_feeder: np.ndarray
    block_dtx: np.ndarray
    block_zone: np.ndarray
    block_tower: np.ndarray
    block_pop: np.ndarray
    block_kw: np.ndarray
    bldg_block: np.ndarray
    bldg_kind: np.ndarray
    bldg_res: np.ndarray
    bldg_kw: np.ndarray
    substations: list
    feeders: list
    ties: list
    dtx: list
    hospitals: list
    stations: list
    dispatch: list
    towers: list
    pumps: list
    zones: list
    cells: list
    registry: dict = field(default_factory=dict)
    model_version: str = MODEL_VERSION

    # -- lookups ------------------------------------------------------------
    def entity(self, eid: str) -> dict:
        return self.registry[eid]

    def feeder_index(self, fid: str) -> int:
        return self._feeder_idx[fid]

    def summary(self) -> dict:
        return {
            "seed": self.seed, "intersections": int(len(self.node_xy)),
            "road_segments": int(self.n_segments), "signals": len(self.signals),
            "blocks": int(len(self.block_xy)), "buildings": int(len(self.bldg_block)),
            "residents": int(self.bldg_res.sum()),
            "electrical_components": len(self.substations) * 3 + len(self.feeders) + len(self.ties) + len(self.dtx),
            "substations": len(self.substations), "feeders": len(self.feeders),
            "tie_switches": len(self.ties), "distribution_transformers": len(self.dtx),
            "hospitals": len(self.hospitals), "ems_stations": len(self.stations),
            "ambulances": sum(s["units"] for s in self.stations),
            "dispatch_centres": len(self.dispatch), "telecom_towers": len(self.towers),
            "water_pumps": len(self.pumps),
            "critical_facilities": len(self.hospitals) + len(self.stations) + len(self.dispatch)
            + len(self.towers) + len(self.pumps),
            "load_mw": round(float(self.block_kw.sum()) / 1000, 1),
        }


def _kmeans(xy: np.ndarray, w: np.ndarray, k: int, rng, iters: int = 30) -> tuple[np.ndarray, np.ndarray]:
    p = w / w.sum()
    cent = xy[rng.choice(len(xy), size=k, replace=False, p=p)].copy()
    for _ in range(iters):
        d = ((xy[:, None, :] - cent[None, :, :]) ** 2).sum(-1)
        lab = d.argmin(1)
        for j in range(k):
            m = lab == j
            if m.any():
                cent[j] = (xy[m] * w[m, None]).sum(0) / w[m].sum()
    d = ((xy[:, None, :] - cent[None, :, :]) ** 2).sum(-1)
    return cent, d.argmin(1)


def generate_city(seed: int = 7, G: int = 32, spacing: float = 220.0, n_buildings: int = 50_000,
                  n_substations: int = 10, feeders_per_sub: int = 6, n_hospitals: int = 12,
                  n_stations: int = 18, n_ambulances: int = 26, n_towers: int = 30,
                  n_pumps: int = 20) -> City:
    rng = np.random.default_rng(seed)

    # -- road lattice -------------------------------------------------------
    rr, cc = np.meshgrid(np.arange(G), np.arange(G), indexing="ij")
    node_xy = np.stack([cc.ravel() * spacing, rr.ravel() * spacing], 1).astype(float)
    node_xy += rng.uniform(-0.12, 0.12, node_xy.shape) * spacing
    nid = lambda r, c: r * G + c
    river_row = G // 2 - 1
    bridge_cols = [G // 6, G // 2, (5 * G) // 6]
    und = []
    for r in range(G):
        for c in range(G):
            if c + 1 < G:
                und.append((nid(r, c), nid(r, c + 1), r % 4 == 0))
            if r + 1 < G:
                if r == river_row and c not in bridge_cols:
                    continue
                und.append((nid(r, c), nid(r + 1, c), c % 4 == 0 or (r == river_row)))
    g = nx.Graph()
    g.add_nodes_from(range(G * G))
    for a, b, art in und:
        g.add_edge(a, b, art=art)
    # thin out a few local streets, keeping the network connected
    locals_ = [(a, b) for a, b, art in und if not art]
    for i in rng.permutation(len(locals_))[: int(0.05 * len(locals_))]:
        a, b = locals_[i]
        g.remove_edge(a, b)
        if not nx.has_path(g, a, b):
            g.add_edge(a, b, art=False)
    segs = [(a, b, d["art"]) for a, b, d in g.edges(data=True)]
    src, dst, cls = [], [], []
    for a, b, art in segs:
        src += [a, b]
        dst += [b, a]
        cls += [int(art), int(art)]
    arc_src, arc_dst, arc_cls = np.array(src, np.int32), np.array(dst, np.int32), np.array(cls, np.int8)
    arc_len = np.linalg.norm(node_xy[arc_src] - node_xy[arc_dst], axis=1)
    arc_speed = np.where(arc_cls == 1, 50.0, 30.0)
    arc_cap = np.where(arc_cls == 1, 1800.0, 900.0)
    bridges = [(nid(river_row, c), nid(river_row + 1, c)) for c in bridge_cols]

    # -- blocks and buildings ----------------------------------------------
    B = (G - 1) * (G - 1)
    br, bc = np.divmod(np.arange(B), G - 1)
    corners = np.stack([nid(br, bc), nid(br, bc + 1), nid(br + 1, bc), nid(br + 1, bc + 1)], 1)
    block_xy = node_xy[corners].mean(1)
    block_node = corners[np.arange(B), rng.integers(0, 4, B)].astype(np.int32)
    centre = node_xy.mean(0)
    dist_c = np.linalg.norm(block_xy - centre, axis=1)
    river_y = node_xy[nid(river_row, 0), 1] + spacing / 2
    on_river = np.abs(block_xy[:, 1] - river_y) < spacing * 0.6
    weight = np.exp(-dist_c / 2400.0) + 0.25
    weight[on_river] *= 0.15
    bldg_block = rng.choice(B, size=n_buildings, p=weight / weight.sum()).astype(np.int32)
    kind = rng.choice(4, size=n_buildings, p=[0.80, 0.15, 0.04, 0.01]).astype(np.int8)
    res = np.where(kind == 0, np.round(rng.lognormal(np.log(12), 0.6, n_buildings)), 0).astype(np.int32)
    kw = np.select([kind == 0, kind == 1, kind == 2], [res * 0.6, rng.uniform(20, 70, n_buildings),
                                                       rng.uniform(80, 200, n_buildings)], 150.0)
    block_pop = np.bincount(bldg_block, weights=res, minlength=B)
    block_kw = np.bincount(bldg_block, weights=kw, minlength=B)
    block_cell = ((br // 4) * 8 + (bc // 4)).astype(np.int32)

    # -- power: substations, feeders, transformers, ties --------------------
    sub_xy, block_sub = _kmeans(block_xy, block_kw + 1, n_substations, rng)
    # the substation feeding the most people becomes S3: the scenario's fault
    pop_by_sub = np.bincount(block_sub, weights=block_pop, minlength=n_substations)
    order = list(np.argsort(-pop_by_sub))
    critical = order.pop(0)
    rest = [i for i in range(n_substations) if i != critical]
    ids = rest[:2] + [critical] + rest[2:]
    remap = {old: new for new, old in enumerate(ids)}
    block_sub = np.array([remap[s] for s in block_sub])
    sub_xy = sub_xy[ids]

    substations, feeders, dtx = [], [], []
    block_feeder = np.zeros(B, np.int32)
    block_dtx = np.zeros(B, np.int32)
    for s in range(n_substations):
        sid = f"S{s + 1}"
        blocks = np.flatnonzero(block_sub == s)
        ang = np.arctan2(*(block_xy[blocks] - sub_xy[s]).T[::-1])
        blocks = blocks[np.argsort(ang)]
        cum = np.cumsum(block_kw[blocks])
        cut = np.searchsorted(cum, np.linspace(0, cum[-1], feeders_per_sub + 1)[1:-1])
        fids = []
        for j, grp in enumerate(np.split(blocks, cut)):
            fi = len(feeders)
            fid = f"F{s + 1}-{j + 1}"
            block_feeder[grp] = fi
            d = np.linalg.norm(block_xy[grp] - sub_xy[s], axis=1)
            grp = grp[np.argsort(d)]
            for chunk in np.array_split(grp, max(1, int(round(len(grp) / 2.6)))):
                block_dtx[chunk] = len(dtx)
                dtx.append({"id": f"X{len(dtx):04d}", "feeder": fid, "blocks": chunk.tolist()})
            feeders.append({"id": fid, "sub": sid, "load_mw": float(block_kw[grp].sum() / 1000),
                            "blocks": grp.tolist(), "ties": []})
            fids.append(fid)
        load = float(block_kw[blocks].sum() / 1000)
        substations.append({"id": sid, "xy": sub_xy[s].tolist(), "load_mw": load,
                            "cap_mw": load * float(rng.uniform(1.35, 1.6)), "feeders": fids,
                            "transformers": 2})

    adj_pairs: dict[tuple[int, int], list[int]] = {}
    grid_b = np.arange(B).reshape(G - 1, G - 1)
    for a, b in list(zip(grid_b[:, :-1].ravel(), grid_b[:, 1:].ravel())) + \
            list(zip(grid_b[:-1, :].ravel(), grid_b[1:, :].ravel())):
        fa, fb = block_feeder[a], block_feeder[b]
        if feeders[fa]["sub"] != feeders[fb]["sub"]:
            adj_pairs.setdefault((min(fa, fb), max(fa, fb)), []).append(int(a))
    ties = []
    tie_count = np.zeros(len(feeders), int)
    for (fa, fb), blks in sorted(adj_pairs.items(), key=lambda kv: -len(kv[1])):
        if tie_count[fa] >= 2 or tie_count[fb] >= 2:
            continue
        tid = f"T{len(ties) + 1:02d}"
        ties.append({"id": tid, "a": feeders[fa]["id"], "b": feeders[fb]["id"],
                     "block": blks[len(blks) // 2]})
        feeders[fa]["ties"].append(tid)
        feeders[fb]["ties"].append(tid)
        tie_count[fa] += 1
        tie_count[fb] += 1

    # -- telecom, water ----------------------------------------------------
    tower_xy, block_tower = _kmeans(block_xy, np.ones(B), n_towers, rng)
    towers = []
    for k in range(n_towers):
        nb = int(np.argmin(np.linalg.norm(block_xy - tower_xy[k], axis=1)))
        towers.append({"id": f"K{k + 1:02d}", "xy": tower_xy[k].tolist(), "block": nb,
                       "feeder": feeders[block_feeder[nb]]["id"],
                       "battery_h": float(rng.uniform(2.0, 6.0))})
    for t in ties:
        t["tower"] = towers[block_tower[t["block"]]]["id"]
    zone_xy, block_zone = _kmeans(block_xy, block_pop + 1, n_pumps, rng)
    pumps, zones = [], []
    pumped_only = set(rng.choice(n_pumps, size=max(1, int(0.3 * n_pumps)), replace=False).tolist())
    for z in range(n_pumps):
        nb = int(np.argmin(np.linalg.norm(block_xy - zone_xy[z], axis=1)))
        buffer_h = 0.25 if z in pumped_only else float(rng.uniform(3.0, 14.0))
        pumps.append({"id": f"P{z + 1:02d}", "block": nb, "feeder": feeders[block_feeder[nb]]["id"],
                      "zone": f"Z{z + 1:02d}"})
        zones.append({"id": f"Z{z + 1:02d}", "pump": f"P{z + 1:02d}", "buffer_h": buffer_h,
                      "blocks": np.flatnonzero(block_zone == z).tolist()})

    # -- facilities ---------------------------------------------------------
    def place(k, w):
        cent, _ = _kmeans(block_xy, w, k, rng)
        used, out = set(), []
        for c in cent:
            d = np.linalg.norm(block_xy - c, axis=1)
            for b in np.argsort(d):
                if int(b) not in used and not on_river[b]:
                    used.add(int(b))
                    out.append(int(b))
                    break
        return out

    hospitals = []
    for i, b in enumerate(place(n_hospitals, block_pop + 1)):
        f = block_feeder[b]
        feeds = [feeders[f]["id"]]
        if i % 4 == 1 and feeders[f]["ties"]:  # a few hospitals have a second utility feed
            t = next(t for t in ties if t["id"] == feeders[f]["ties"][0])
            feeds.append(t["b"] if t["a"] == feeders[f]["id"] else t["a"])
        hospitals.append({"id": f"H{i + 1:02d}", "block": b, "node": int(block_node[b]),
                          "feeds": feeds, "p_fts": 0.06, "daytank_h": float(rng.uniform(1.25, 2.5)),
                          "fuelpump": "emergency_bus", "zone": zones[block_zone[b]]["id"],
                          "beds": int(rng.integers(20, 60))})
    stations = []
    st_blocks = place(n_stations, block_pop + 1)
    extra = set(rng.choice(n_stations, size=n_ambulances - n_stations, replace=False).tolist())
    for i, b in enumerate(st_blocks):
        stations.append({"id": f"E{i + 1:02d}", "block": b, "node": int(block_node[b]),
                         "feeder": feeders[block_feeder[b]]["id"], "units": 2 if i in extra else 1})
    centre_b = int(np.argmin(dist_c + on_river * 1e9))
    far_b = int(np.argmax(np.where(block_sub != block_sub[centre_b], -dist_c * 0 + dist_c, -1)))
    dispatch = [
        {"id": "D1", "role": "primary", "block": centre_b, "feeder": feeders[block_feeder[centre_b]]["id"],
         "ups_min": 15.0, "p_fts": 0.06},
        {"id": "D2", "role": "backup", "block": far_b, "feeder": feeders[block_feeder[far_b]]["id"],
         "ups_min": 15.0, "p_fts": 0.06},
    ]

    # -- signals -------------------------------------------------------------
    signals, signal_of_node = {}, {}
    for r in range(G):
        for c in range(G):
            if (r % 4 == 0 and c % 2 == 0) or (c % 4 == 0 and r % 2 == 0):
                n = nid(r, c)
                if g.degree(n) < 3:
                    continue
                d = np.linalg.norm(block_xy - node_xy[n], axis=1)
                b = int(np.argmin(d))
                sid = f"I-{r:02d}-{c:02d}"
                signals[sid] = {"id": sid, "node": n, "block": b,
                                "feeder": feeders[block_feeder[b]]["id"],
                                "ups_h": float(rng.uniform(4.0, 8.0)) if rng.random() < 0.15 else 0.0}
                signal_of_node[n] = sid

    cells = []
    for c in range(64):
        blocks = np.flatnonzero(block_cell == c)
        cells.append({"id": f"C{c:02d}", "blocks": blocks.tolist(), "pop": float(block_pop[blocks].sum())})

    city = City(seed, G, spacing, node_xy, arc_src, arc_dst, arc_len, arc_speed, arc_cap, arc_cls,
                len(segs), bridges, signals, signal_of_node, block_xy, block_node, block_cell,
                block_feeder, block_dtx, block_zone.astype(np.int32), block_tower.astype(np.int32),
                block_pop, block_kw, bldg_block, kind, res, kw, substations, feeders, ties, dtx,
                hospitals, stations, dispatch, towers, pumps, zones, cells)
    city._feeder_idx = {f["id"]: i for i, f in enumerate(feeders)}
    reg = city.registry
    for kind_name, items in (("substation", substations), ("feeder", feeders), ("tie", ties),
                             ("dtx", dtx), ("hospital", hospitals), ("station", stations),
                             ("dispatch", dispatch), ("tower", towers), ("pump", pumps),
                             ("zone", zones), ("cell", cells)):
        for it in items:
            reg[f"{kind_name}:{it['id']}"] = {"type": kind_name, **it}
    for sid, s in signals.items():
        reg[f"signal:{sid}"] = {"type": "signal", **s}
    for h in hospitals:
        reg[f"generator:{h['id']}"] = {"type": "generator", "id": h["id"], "hospital": h["id"]}
        reg[f"fuelpump:{h['id']}"] = {"type": "fuelpump", "id": h["id"], "hospital": h["id"]}
    return city
