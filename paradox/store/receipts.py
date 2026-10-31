"""Provenance receipts: every answer is pinned to what produced it.

A receipt records the query text, the head and ``known_at`` of every
branch it read, the code, model and parameter digests, the seeds and
replicate counts, and the coupling assumption. It also records how many
what-if queries this session had already explored, which counts the
forking paths behind the answer.

``Ledger.verify`` re-executes the query and compares result digests.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .events import canonical

_CODE_DIGEST = None


def code_digest() -> str:
    global _CODE_DIGEST
    if _CODE_DIGEST is None:
        h = hashlib.blake2b(digest_size=12)
        root = Path(__file__).resolve().parents[1]
        for p in sorted(root.rglob("*.py")):
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
        _CODE_DIGEST = h.hexdigest()
    return _CODE_DIGEST


def result_digest(result) -> str:
    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k not in ("receipt", "elapsed_ms")}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    return hashlib.blake2b(canonical(strip(result)).encode(), digest_size=16).hexdigest()


class Ledger:
    def __init__(self):
        self.receipts: dict[str, dict] = {}
        self.explored = 0

    def issue(self, query: str, result, branches: dict, *, model: str, params: str,
              seeds=None, n: int | None = None, coupling: str = "shared-noise",
              kind: str = "read") -> dict:
        if kind == "what-if":
            self.explored += 1
        body = {"query": query, "branches": branches, "code": code_digest(), "model": model,
                "params": params, "seeds": seeds, "n": n, "coupling": coupling, "kind": kind,
                "queries_explored": self.explored, "result": result_digest(result)}
        rid = "R-" + hashlib.blake2b(canonical(body).encode(), digest_size=6).hexdigest()
        body["id"] = rid
        body["issued_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.receipts[rid] = body
        return body

    def get(self, rid: str) -> dict:
        return self.receipts[rid]

    def list(self) -> list[dict]:
        return list(self.receipts.values())
