"""Command line: ``paradox serve | query | demo | bench | export``."""

from __future__ import annotations

import argparse
import json
import sys
import time


def _px(args):
    from .engine import Paradox
    t = time.perf_counter()
    px = Paradox(city_seed=args.city_seed, world_seed=args.world_seed, telemetry=not getattr(args, "no_telemetry", False))
    print(f"[paradox] city + record of reality ready in {time.perf_counter() - t:.2f}s", file=sys.stderr)
    return px


def cmd_serve(args):
    import uvicorn
    from .api.app import create_app
    app = create_app(_px(args))
    print(f"[paradox] open http://{args.host}:{args.port}", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_query(args):
    from .pql.executor import Executor
    ex = Executor(_px(args))
    queries = args.q or [line.strip() for line in sys.stdin if line.strip() and not line.startswith("--")]
    for q in queries:
        r = ex.run(q)
        print(json.dumps(r, indent=2, default=str) if args.json else _pretty(r))


def _pretty(r: dict) -> str:
    res = r["result"]
    head = f"\n» {r['query']}   [{r['elapsed_ms']} ms · receipt {r['receipt']['id']}]"
    if "narrative" in res:
        return head + "\n" + res["narrative"]
    if res.get("kind") == "whatif":
        lines = [head, f"  branch {res['branch_created']} forked at {res['branch']['fork']}"]
        for m, st in res["ensemble"]["metrics"].items():
            d = st["delta"]
            lines.append(f"  {m:28s} actual {res['point'][m]['actual']:9.2f} → alternate {res['point'][m]['alternate']:9.2f}"
                         f"   Δ {d['mean']:+.2f} [{d['ci95'][0]:+.2f}, {d['ci95'][1]:+.2f}]  P(Δ<0)={d['p_negative']:.2f}")
        return "\n".join(lines)
    return head + "\n" + json.dumps(res, indent=2, default=str)[:4000]


def cmd_demo(args):
    from .pql.executor import Executor
    ex = Executor(_px(args))
    script = [
        "SHOW CITY",
        "AS OF 18:40 SHOW DEPENDENTS OF substation:S3 DEPTH 2",
        "WHY hospital:H04",
        "WHAT IF substation:S3 NEVER FAILS COMPARE ems.p90_response, ems.p1_late RUNS 16 COUPLING",
        "WHAT IF hospital:H04 GENERATOR STARTS AFTER 0 COMPARE hospital.unpowered_min, ems.p90_response RUNS 16",
        "ATTRIBUTE ems.p1_late",
        "AUDIT",
    ]
    for q in script:
        print(_pretty(ex.run(q)))


def cmd_bench(args):
    from benchmarks.run import main as bench_main  # type: ignore
    bench_main()


def cmd_export(args):
    from .export import export_sql, export_cypher
    px = _px(args)
    if args.sql:
        export_sql(px, args.sql)
        print(f"wrote {args.sql}")
    if args.cypher:
        export_cypher(px, args.cypher)
        print(f"wrote {args.cypher}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="paradox", description="PARADOX - the what-if database of reality")
    ap.add_argument("--city-seed", type=int, default=7)
    ap.add_argument("--world-seed", type=int, default=1815)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="start the API + investigation UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8642)
    s.set_defaults(fn=cmd_serve)
    q = sub.add_parser("query", help="run PQL queries (args or stdin)")
    q.add_argument("q", nargs="*")
    q.add_argument("--json", action="store_true")
    q.add_argument("--no-telemetry", action="store_true")
    q.set_defaults(fn=cmd_query)
    d = sub.add_parser("demo", help="scripted tour")
    d.add_argument("--no-telemetry", action="store_true")
    d.set_defaults(fn=cmd_demo)
    b = sub.add_parser("bench", help="run benchmarks")
    b.set_defaults(fn=cmd_bench)
    e = sub.add_parser("export", help="export to PostGIS/Timescale SQL and Neo4j Cypher")
    e.add_argument("--sql")
    e.add_argument("--cypher")
    e.add_argument("--no-telemetry", action="store_true", default=True)
    e.set_defaults(fn=cmd_export)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
