"""PQL, the PARADOX Query Language.

Time is a prefix clause and the world is a clause; verbs compose with
``|>``. The design follows SQL:2011's ``AS OF``, XTDB's two clocks and
HypeR's what-if clauses (docs/ANALYSIS.md section 9).

    query        := [AS OF time [KNOWN AT int]] [IN ident] stmt { "|>" stmt }
    stmt         := SHOW show | WHY [NOT] ref | WHAT IF iv {AND iv} [AS ident] [opts]
                  | ATTRIBUTE metric | PLAN REDUCE metric BELOW num [BUDGET num] [USING kind] [RUNS int]
                  | SENSITIVITY metric [METHOD (MORRIS|SOBOL)] [RUNS int]
                  | CORRELATE ref WITH ref [RUNS int] | AUDIT | VALIDATE [RUNS int]
                  | DIFF ident [WITH ident] | VERIFY (ident | receipt) | TRACE ref [FROM time TO time]
                  | CORRECT ref FAULT TIME TO time
    show         := (DEPENDENTS|DEPENDENCIES) OF ref [DEPTH int] | STATE OF ref | EVENTS [OF ref]
                    [FROM time TO time] [KIND ident] [LIMIT int] | KPIS | BRANCHES | PARAMS | CITY
                  | INCIDENTS [LATE] [LIMIT int] | RECEIPTS | TELEMETRY OF ref [METRIC ident]
    opts         := [COMPARE metric {, metric}] [RUNS int] [MODE (COUNTERFACTUAL|INTERVENTIONAL)]
                    [TIER (MC|PARAMS)] [COUPLING]
    iv           := ref NEVER FAILS | ref FAILS AT time [FOR num] | ref REPAIRS IN num
                  | ref [GENERATOR] STARTS [AFTER num] | ref GETS (FLISR | UPS num | BATTERY num
                  | MAINTENANCE | FUEL BACKUP) | ADD int AMBULANCES AT ref | SET ident = num
                  | ALL (SIGNALS GET UPS num | FEEDERS GET FLISR | TOWERS GET BATTERY num
                  | HOSPITALS GET MAINTENANCE)
    ref          := ident ":" ident        time := HH:MM
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..sim.interventions import Intervention

TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<pipe>\|>)
  | (?P<time>\d{1,2}:\d{2}(?![\w:]))
  | (?P<ref>[A-Za-z_][\w\-\.]*:[\w\-\.\*]+)
  | (?P<num>-?\d+(?:\.\d+)?)
  | (?P<ident>[A-Za-z_\*][\w\.\-]*)
  | (?P<sym>[,=()])
""", re.X)


class PQLError(ValueError):
    pass


@dataclass
class Tok:
    kind: str
    text: str
    pos: int

    @property
    def up(self) -> str:
        return self.text.upper()


def lex(text: str) -> list[Tok]:
    out, i = [], 0
    while i < len(text):
        m = TOKEN.match(text, i)
        if not m:
            raise PQLError(f"unexpected character {text[i]!r} at {i}")
        kind = m.lastgroup
        if kind != "ws":
            out.append(Tok(kind, m.group(), i))
        i = m.end()
    return out


def to_minutes(s: str) -> float:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


class Parser:
    def __init__(self, text: str):
        self.text = text
        self.toks = lex(text)
        self.i = 0

    # -- helpers ----------------------------------------------------------------
    def peek(self, k: int = 0) -> Tok | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def at(self, *words: str) -> bool:
        for k, w in enumerate(words):
            t = self.peek(k)
            if t is None or t.up != w:
                return False
        return True

    def accept(self, *words: str) -> bool:
        if self.at(*words):
            self.i += len(words)
            return True
        return False

    def expect(self, *words: str) -> None:
        if not self.accept(*words):
            got = self.peek()
            raise PQLError(f"expected {' '.join(words)} but found {got.text if got else 'end of query'!r}")

    def take(self, kind: str) -> str:
        t = self.peek()
        if t is None or t.kind != kind:
            raise PQLError(f"expected a {kind} but found {t.text if t else 'end of query'!r}")
        self.i += 1
        return t.text

    def number(self) -> float:
        return float(self.take("num"))

    def time(self) -> float:
        return to_minutes(self.take("time"))

    def ident(self) -> str:
        t = self.peek()
        if t is None or t.kind not in ("ident", "ref"):
            raise PQLError(f"expected a name but found {t.text if t else 'end of query'!r}")
        self.i += 1
        return t.text

    # -- grammar ------------------------------------------------------------------
    def query(self) -> dict:
        q = {"as_of": None, "known_at": None, "branch": None, "pipeline": []}
        if self.accept("AS", "OF"):
            q["as_of"] = self.time()
            if self.accept("KNOWN", "AT"):
                q["known_at"] = int(self.number())
        if self.accept("IN"):
            q["branch"] = self.ident()
        q["pipeline"].append(self.stmt())
        while self.peek() and self.peek().kind == "pipe":
            self.i += 1
            q["pipeline"].append(self.stmt())
        if self.peek() is not None:
            raise PQLError(f"unexpected {self.peek().text!r} at position {self.peek().pos}")
        return q

    def stmt(self) -> dict:
        if self.accept("SHOW"):
            return self.show()
        if self.accept("WHY"):
            neg = self.accept("NOT")
            return {"verb": "why", "not": neg, "ref": self.take("ref")}
        if self.accept("WHAT", "IF"):
            return self.whatif()
        if self.accept("ATTRIBUTE"):
            return {"verb": "attribute", "metric": self.ident()}
        if self.accept("PLAN"):
            self.expect("REDUCE")
            st = {"verb": "plan", "metric": self.ident()}
            self.expect("BELOW")
            st["target"] = self.number()
            st["budget"] = self.number() if self.accept("BUDGET") else 60.0
            st["kinds"] = None
            if self.accept("USING"):
                st["kinds"] = [self.ident().lower()]
            st["runs"] = int(self.number()) if self.accept("RUNS") else 8
            return st
        if self.accept("SENSITIVITY"):
            st = {"verb": "sensitivity", "metric": self.ident(), "method": "morris", "runs": 4}
            while True:
                if self.accept("METHOD"):
                    st["method"] = self.ident().lower()
                elif self.accept("RUNS"):
                    st["runs"] = int(self.number())
                else:
                    break
            return st
        if self.accept("CORRELATE"):
            a = self.take("ref")
            self.expect("WITH")
            b = self.take("ref")
            return {"verb": "correlate", "a": a, "b": b, "runs": int(self.number()) if self.accept("RUNS") else 40}
        if self.accept("AUDIT"):
            return {"verb": "audit"}
        if self.accept("VALIDATE"):
            return {"verb": "validate", "runs": int(self.number()) if self.accept("RUNS") else 4}
        if self.accept("DIFF"):
            a = self.ident()
            b = self.ident() if self.accept("WITH") else "main"
            return {"verb": "diff", "a": a, "b": b}
        if self.accept("VERIFY"):
            return {"verb": "verify", "target": self.ident()}
        if self.accept("TRACE"):
            st = {"verb": "trace", "ref": self.take("ref"), "from": None, "to": None}
            if self.accept("FROM"):
                st["from"] = self.time()
                self.expect("TO")
                st["to"] = self.time()
            return st
        if self.accept("CORRECT"):
            ref = self.take("ref")
            self.expect("FAULT", "TIME", "TO")
            return {"verb": "correct", "ref": ref, "t": self.time()}
        t = self.peek()
        raise PQLError(f"unknown statement starting at {t.text if t else 'end'!r}")

    def show(self) -> dict:
        if self.accept("DEPENDENTS", "OF") or self.accept("DEPENDENCIES", "OF"):
            what = "dependents" if self.toks[self.i - 2].up == "DEPENDENTS" else "dependencies"
            st = {"verb": "show", "what": what, "ref": self.take("ref"), "depth": 4}
            if self.accept("DEPTH"):
                st["depth"] = int(self.number())
            return st
        if self.accept("STATE", "OF"):
            return {"verb": "show", "what": "state", "ref": self.take("ref")}
        if self.accept("EVENTS"):
            st = {"verb": "show", "what": "events", "ref": None, "from": None, "to": None, "kind": None, "limit": 50}
            while True:
                if self.accept("OF"):
                    st["ref"] = self.take("ref")
                elif self.accept("FROM"):
                    st["from"] = self.time()
                    self.expect("TO")
                    st["to"] = self.time()
                elif self.accept("KIND"):
                    st["kind"] = self.ident()
                elif self.accept("LIMIT"):
                    st["limit"] = int(self.number())
                else:
                    break
            return st
        if self.accept("INCIDENTS"):
            st = {"verb": "show", "what": "incidents", "late": self.accept("LATE"), "limit": 25}
            if self.accept("LIMIT"):
                st["limit"] = int(self.number())
            return st
        if self.accept("TELEMETRY", "OF"):
            st = {"verb": "show", "what": "telemetry", "ref": self.take("ref"), "metric": None}
            if self.accept("METRIC"):
                st["metric"] = self.ident()
            return st
        for w in ("KPIS", "BRANCHES", "PARAMS", "CITY", "RECEIPTS"):
            if self.accept(w):
                return {"verb": "show", "what": w.lower()}
        raise PQLError("SHOW what? (DEPENDENTS OF, STATE OF, EVENTS, KPIS, BRANCHES, INCIDENTS, ...)")

    def whatif(self) -> dict:
        ivs = [self.intervention()]
        while self.accept("AND"):
            ivs.append(self.intervention())
        st = {"verb": "whatif", "ivs": ivs, "name": None, "metrics": None, "runs": 24,
              "mode": "counterfactual", "tier": "params", "coupling": False}
        while True:
            if self.accept("AS"):
                st["name"] = self.ident()
            elif self.accept("COMPARE"):
                st["metrics"] = [self.ident()]
                while self.peek() and self.peek().text == ",":
                    self.i += 1
                    st["metrics"].append(self.ident())
            elif self.accept("RUNS"):
                st["runs"] = int(self.number())
            elif self.accept("MODE"):
                st["mode"] = self.ident().lower()
            elif self.accept("TIER"):
                st["tier"] = self.ident().lower()
            elif self.accept("COUPLING"):
                st["coupling"] = True
            else:
                break
        return st

    def intervention(self) -> Intervention:
        if self.accept("ALL"):
            if self.accept("SIGNALS", "GET", "UPS"):
                return Intervention("ups", "*", self.number())
            if self.accept("FEEDERS", "GET", "FLISR"):
                return Intervention("flisr", "*")
            if self.accept("TOWERS", "GET", "BATTERY"):
                return Intervention("battery", "*", self.number())
            if self.accept("HOSPITALS", "GET", "MAINTENANCE"):
                return Intervention("maintenance", "*")
            raise PQLError("ALL SIGNALS GET UPS n | ALL FEEDERS GET FLISR | ALL TOWERS GET BATTERY n | "
                           "ALL HOSPITALS GET MAINTENANCE")
        if self.accept("ADD"):
            n = self.number()
            self.expect("AMBULANCES", "AT")
            return Intervention("add_units", self.take("ref"), n)
        if self.accept("SET"):
            name = self.ident()
            self.expect("=")
            return Intervention("set", name, self.number())
        ref = self.take("ref")
        kind, ident = ref.split(":", 1)
        if self.accept("NEVER", "FAILS"):
            return Intervention("never_fails", ref)
        if self.accept("FAILS", "AT"):
            t = self.time()
            dur = self.number() if self.accept("FOR") else None
            return Intervention("fails_at", ref, t, dur)
        if self.accept("REPAIRS", "IN"):
            return Intervention("repairs_in", ref, self.number())
        if self.accept("GENERATOR", "STARTS") or self.accept("STARTS"):
            after = self.number() if self.accept("AFTER") else None
            target = ref if kind in ("hospital", "dispatch") else f"hospital:{ident}"
            return Intervention("gen_starts", target, after if after else 0.17)
        if self.accept("GETS"):
            if self.accept("FLISR"):
                return Intervention("flisr", ref)
            if self.accept("UPS"):
                return Intervention("ups", ref, self.number())
            if self.accept("BATTERY"):
                return Intervention("battery", ref, self.number())
            if self.accept("MAINTENANCE"):
                return Intervention("maintenance", ref if kind == "hospital" else f"hospital:{ident}")
            if self.accept("FUEL", "BACKUP"):
                return Intervention("fuel_backup", ref if kind == "hospital" else f"hospital:{ident}")
            raise PQLError("GETS FLISR | UPS n | BATTERY n | MAINTENANCE | FUEL BACKUP")
        raise PQLError(f"don't know what to do with {ref} (NEVER FAILS, FAILS AT, GETS ..., STARTS ...)")


def parse(text: str) -> dict:
    return Parser(text.strip().rstrip(";")).query()


def parse_intervention(text: str) -> Intervention:
    p = Parser(text)
    iv = p.intervention()
    if p.peek() is not None:
        raise PQLError(f"unexpected {p.peek().text!r}")
    return iv
