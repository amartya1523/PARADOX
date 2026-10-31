"""Map the embedded engine onto the production polyglot stack.

* ``export_sql``: PostgreSQL + PostGIS (infrastructure geometry), TimescaleDB
  (telemetry hypertable) and an append-only events table that keeps the
  causal hash, the cause ids, the branch overlay and the transaction time.
* ``export_cypher``: Neo4j, with typed dependency edges carrying their
  Rinaldi class and their validity intervals.

Exporting does not change the semantics. It shows that the contracts in
docs/ARCHITECTURE.md section 5 (identity, overlay, bitemporality) survive the
move to the production stack.
"""

from __future__ import annotations

import json
import math

from .store.derive import static_graph
from .store.tgraph import DEPENDENCY_CLASS

DDL = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE entity (
  id        text PRIMARY KEY,           -- e.g. 'substation:S3'
  kind      text NOT NULL,
  attrs     jsonb NOT NULL DEFAULT '{}',
  geom      geometry(Point, 3857)
);

CREATE TABLE road_segment (
  a int, b int, arterial boolean, geom geometry(LineString, 3857)
);

CREATE TABLE dependency (                -- the Neo4j graph, relationally
  src text REFERENCES entity(id), dst text REFERENCES entity(id),
  rel text, rinaldi_class text,
  valid tstzrange NOT NULL DEFAULT tstzrange('-infinity', 'infinity')
);

CREATE TABLE branch (
  name text PRIMARY KEY, parent text REFERENCES branch(name),
  fork_t double precision, fork_tx bigint, genesis char(32), head char(32), meta jsonb
);

CREATE TABLE event (                     -- content + cause addressed; never updated
  eid char(32) PRIMARY KEY, t double precision NOT NULL, kind text NOT NULL,
  entity text NOT NULL, attrs jsonb NOT NULL, causes char(32)[] NOT NULL, rule text NOT NULL
);

CREATE TABLE event_visibility (          -- bitemporal: per branch, assert/retract transactions
  branch text REFERENCES branch(name), eid char(32) REFERENCES event(eid),
  assert_tx bigint NOT NULL, retract_tx bigint
);
CREATE INDEX ON event (t);
CREATE INDEX ON event (entity, t);
CREATE INDEX ON event USING gin (causes);

CREATE TABLE telemetry (ts double precision, entity text, metric text, value real);
SELECT create_hypertable('telemetry', by_range('ts', 60));   -- 60-minute chunks

-- a branch read: the parent's prefix as known at fork time, plus the branch's own segment
CREATE OR REPLACE FUNCTION branch_events(b text) RETURNS SETOF event LANGUAGE sql AS $$
  WITH RECURSIVE lineage(name, parent, fork_t, fork_tx, upper_t, known) AS (
    SELECT name, parent, fork_t, fork_tx, 'infinity'::float8, 9223372036854775807 FROM branch WHERE name = b
    UNION ALL
    SELECT p.name, p.parent, p.fork_t, p.fork_tx, l.fork_t, LEAST(l.known, l.fork_tx)
    FROM branch p JOIN lineage l ON p.name = l.parent)
  SELECT e.* FROM lineage l
  JOIN event_visibility v ON v.branch = l.name
  JOIN event e ON e.eid = v.eid
  WHERE e.t < l.upper_t AND v.assert_tx <= l.known AND (v.retract_tx IS NULL OR v.retract_tx > l.known)
  ORDER BY e.t, e.eid;
$$;
"""


def _q(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def export_sql(px, path: str) -> None:
    c = px.city
    out = [DDL]
    for eid, ent in sorted(c.registry.items()):
        attrs = {k: v for k, v in ent.items() if k not in ("blocks",) and not isinstance(v, (list, dict))}
        xy = None
        if "xy" in ent:
            xy = ent["xy"]
        elif "node" in ent:
            xy = c.node_xy[ent["node"]].tolist()
        elif "block" in ent:
            xy = c.block_xy[ent["block"]].tolist()
        geom = f"ST_SetSRID(ST_MakePoint({xy[0]:.1f},{xy[1]:.1f}),3857)" if xy else "NULL"
        out.append(f"INSERT INTO entity VALUES ({_q(eid)},{_q(ent['type'])},{_q(json.dumps(attrs, default=str))},{geom});")
    seen = set()
    for a, b, k in zip(c.arc_src.tolist(), c.arc_dst.tolist(), c.arc_cls.tolist()):
        key = (min(a, b), max(a, b))
        if key in seen:
            continue
        seen.add(key)
        (x1, y1), (x2, y2) = c.node_xy[key[0]], c.node_xy[key[1]]
        out.append(f"INSERT INTO road_segment VALUES ({key[0]},{key[1]},{str(bool(k)).lower()},"
                   f"ST_SetSRID(ST_MakeLine(ST_MakePoint({x1:.1f},{y1:.1f}),ST_MakePoint({x2:.1f},{y2:.1f})),3857));")
    g = static_graph(c)
    for s, d, r, _ in g.edges_as_of(0.0):
        out.append(f"INSERT INTO dependency (src,dst,rel,rinaldi_class) VALUES ({_q(s)},{_q(d)},{_q(r)},{_q(DEPENDENCY_CLASS.get(r, 'logical'))});")
    st = px.store
    for name, b in st.branches.items():
        fork = "NULL" if not math.isfinite(b.fork_t) else f"{b.fork_t}"
        out.append(f"INSERT INTO branch VALUES ({_q(name)},{_q(b.parent.name) if b.parent else 'NULL'},{fork},{b.fork_tx},"
                   f"{_q(b.genesis)},{_q(b.head)},{_q(json.dumps(b.meta, default=str))});")
    for ev in st.objects.values():
        causes = "ARRAY[" + ",".join(_q(x) for x in ev.causes) + "]::char(32)[]" if ev.causes else "'{}'"
        out.append(f"INSERT INTO event VALUES ({_q(ev.eid)},{ev.t},{_q(ev.kind)},{_q(ev.entity)},"
                   f"{_q(json.dumps(ev.attrs))},{causes},{_q(ev.rule)});")
    for name, b in st.branches.items():
        for eid, ivs in b._vis.items():
            for a, r in ivs:
                out.append(f"INSERT INTO event_visibility VALUES ({_q(name)},{_q(eid)},{a},{'NULL' if r is None else r});")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def export_cypher(px, path: str) -> None:
    c = px.city
    out = ["// PARADOX dependency graph for Neo4j"]
    for eid, ent in sorted(c.registry.items()):
        if ent["type"] in ("cell",):
            continue
        label = ent["type"].capitalize()
        out.append(f"CREATE (:{label} {{id: {json.dumps(eid)}}});")
    out.append("CREATE INDEX entity_id IF NOT EXISTS FOR (n:Substation) ON (n.id);")
    g = static_graph(c)
    for s, d, r, _ in g.edges_as_of(0.0):
        out.append(f"MATCH (a {{id: {json.dumps(s)}}}), (b {{id: {json.dumps(d)}}}) "
                   f"CREATE (a)-[:{r} {{class: {json.dumps(DEPENDENCY_CLASS.get(r, 'logical'))}, valid_from: -1e9, valid_to: 1e9}}]->(b);")
    tg = px.tgraph("main")
    for i, (s, d, r, m) in enumerate(zip(tg._src, tg._dst, tg._rel, tg._meta)):
        vf, vt = float(tg._vf[i]), float(tg._vt[i])
        if math.isfinite(vf) or math.isfinite(vt):
            out.append(f"MATCH (a {{id: {json.dumps(s)}}}), (b {{id: {json.dumps(d)}}}) "
                       f"CREATE (a)-[:{r} {{class: 'physical', valid_from: {vf}, valid_to: {vt if math.isfinite(vt) else 1e9}, branch: 'main'}}]->(b);")
    out.append("// as-of query: MATCH p=(s {id:'substation:S3'})-[*1..3]->(x) "
               "WHERE all(r IN relationships(p) WHERE r.valid_from <= 1120 < r.valid_to) RETURN p;")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
