"""Addressed exogenous noise, with abduction.

A counterfactual is only meaningful if the alternate timeline experiences
the *same luck* as the actual one wherever the intervention does not reach.
A sequential RNG cannot promise that: once the alternate branch takes a
different path, every later draw shifts and the two worlds differ by noise
rather than by cause.

So every random number in PARADOX is *addressed*: a pure function of
``(seed, address)``, where the address names what the draw is *for*, e.g.
``("hospital:H04", "gen_start", <root fault id>)`` or
``("I-12-007", "scene")``. Addresses are typed tuples of str/int, hashed
with a length-prefixed encoding so ``("a/b", "c")`` and ``("a", "b/c")``
can never collide.

The tape remembers what each draw revealed about the world:

* a continuous draw (inverse-CDF transform) reveals the uniform exactly,
  so it is recorded as a **point**;
* a threshold draw (``below(p)``) only reveals which side of ``p`` it fell
  on, so it is recorded as an **interval** ``(0, p)`` or ``[p, 1)``.

``Posterior`` turns a factual tape into noise for replicate ``r``: points
stay fixed, intervals are re-sampled *inside* their interval (the
truncated posterior), and addresses the factual world never touched are
drawn fresh. This is Pearl's abduction step, done exactly.
"""

from __future__ import annotations

import hashlib
import math
import struct
from statistics import NormalDist

_NORM = NormalDist()
_SCALE = 1.0 / float(1 << 53)

Address = tuple


def _encode(parts: Address) -> bytes:
    out = bytearray()
    for p in parts:
        if isinstance(p, bool) or not isinstance(p, (str, int)):
            raise TypeError(f"noise address parts must be str or int, got {type(p).__name__}: {p!r}")
        if isinstance(p, int):
            b = str(p).encode()
            out += b"i" + struct.pack("<H", len(b)) + b
        else:
            b = p.encode()
            out += b"s" + struct.pack("<H", len(b)) + b
    return bytes(out)


def address_hash(seed: int, parts: Address, salt: bytes = b"") -> int:
    """64-bit hash of a noise address. Stable across processes and platforms."""
    h = hashlib.blake2b(digest_size=8, key=salt[:64] if salt else b"")
    h.update(struct.pack("<q", int(seed)))
    h.update(_encode(parts))
    return int.from_bytes(h.digest(), "little")


def _to_unit(x: int) -> float:
    return ((x >> 11) + 0.5) * _SCALE


class NoiseTape:
    """Counter-based noise with a tape of what each draw revealed."""

    def __init__(self, seed: int, record: bool = True):
        self.seed = int(seed)
        self.record = record
        self.points: dict[Address, float] = {}
        self.intervals: dict[Address, tuple[float, float]] = {}
        self.forced: dict[Address, float] = {}
        self.constraints: dict[Address, tuple[float, float]] = {}
        self._scopes: list[dict] = []

    def constrain(self, lo: float, hi: float, *addr) -> None:
        """Observed evidence: the draw at ``addr`` is known to lie in [lo, hi).

        The raw uniform is mapped into the interval, so the world is consistent
        with the evidence while keeping the in-interval value uncertain.
        """
        self.constraints[addr] = (lo, hi)

    # -- scopes: lets a reused SCM node hand its observations to a new tape ----
    def begin_scope(self) -> None:
        self._scopes.append({})

    def end_scope(self) -> dict:
        rec = self._scopes.pop()
        if self._scopes:
            self._scopes[-1].update(rec)
        return rec

    def absorb(self, rec: dict) -> None:
        """Replay the observations of a reused computation into this tape."""
        if not self.record:
            return
        for addr, obs in rec.items():
            if obs[0] == "p":
                self.points[addr] = obs[1]
                self.intervals.pop(addr, None)
            elif addr not in self.points:
                self.intervals[addr] = (obs[1], obs[2])
        if self._scopes:
            self._scopes[-1].update(rec)

    # the only source of raw uniforms; subclasses change this
    def _base(self, addr: Address) -> float:
        return _to_unit(address_hash(self.seed, addr))

    def _value(self, addr: Address) -> float:
        if addr in self.forced:
            return self.forced[addr]
        v = self._base(addr)
        c = self.constraints.get(addr)
        if c is not None and not (c[0] <= v < c[1]):
            v = c[0] + (c[1] - c[0]) * _to_unit(address_hash(self.seed, addr, b"constraint"))
        return v

    # -- primitive draws ------------------------------------------------------
    def u(self, *addr) -> float:
        v = self._value(addr)
        if self.record:
            self.points[addr] = v
            self.intervals.pop(addr, None)
        if self._scopes:
            self._scopes[-1][addr] = ("p", v)
        return v

    def below(self, p: float, *addr) -> bool:
        """Threshold draw: True with probability p. Records only the side."""
        v = self._value(addr)
        hit = v < p
        if self.record and addr not in self.points:
            lo, hi = (0.0, p) if hit else (p, 1.0)
            if addr in self.intervals:  # the same draw compared twice: intersect
                plo, phi = self.intervals[addr]
                lo, hi = max(lo, plo), min(hi, phi)
            self.intervals[addr] = (lo, hi)
        if self._scopes and addr not in self.points:
            self._scopes[-1][addr] = ("i",) + self.intervals.get(addr, (0.0, p) if hit else (p, 1.0))
        return hit

    # -- transforms -----------------------------------------------------------
    def bernoulli(self, p: float, *addr) -> bool:
        return self.below(p, *addr)

    def normal(self, mu: float, sigma: float, *addr) -> float:
        return mu + sigma * _NORM.inv_cdf(self.u(*addr))

    def lognormal(self, median: float, sigma: float, *addr) -> float:
        """Lognormal parametrised by its median (easier to source than a mean)."""
        return median * math.exp(sigma * _NORM.inv_cdf(self.u(*addr)))

    def uniform(self, lo: float, hi: float, *addr) -> float:
        return lo + (hi - lo) * self.u(*addr)

    def exponential(self, mean: float, *addr) -> float:
        return -mean * math.log(1.0 - self.u(*addr))

    def gumbel(self, *addr) -> float:
        """Per-option Gumbel noise: counterfactually stable argmax (Oberst & Sontag)."""
        return -math.log(-math.log(self.u(*addr)))

    def choice_index(self, weights, *addr) -> int:
        """Inverse-CDF categorical choice from a single uniform."""
        v = self.u(*addr)
        total = float(sum(weights))
        acc = 0.0
        for i, w in enumerate(weights):
            acc += w / total
            if v < acc:
                return i
        return len(weights) - 1

    # -- interventions on noise -------------------------------------------------
    def force(self, value: float, *addr) -> None:
        if not 0.0 < value < 1.0:
            raise ValueError("forced uniforms must lie strictly inside (0, 1)")
        self.forced[addr] = value

    def observations(self) -> dict:
        return {"points": dict(self.points), "intervals": dict(self.intervals)}

    def digest(self) -> str:
        h = hashlib.blake2b(digest_size=16)
        for addr in sorted(self.points, key=_encode):
            h.update(_encode(addr) + struct.pack("<d", self.points[addr]))
        for addr in sorted(self.intervals, key=_encode):
            lo, hi = self.intervals[addr]
            h.update(_encode(addr) + struct.pack("<dd", lo, hi))
        return h.hexdigest()


class Posterior(NoiseTape):
    """Noise for replicate ``rep`` conditioned on a factual tape (abduction).

    ``fresh_mechanisms`` implements the alternative *coupling* used for
    coupling-sensitivity analysis: draws whose mechanism name (address
    element 1) is listed are re-drawn independently instead of being shared
    with the factual world.
    """

    def __init__(self, factual: NoiseTape, rep: int, fresh_mechanisms: frozenset = frozenset(),
                 record: bool = True):
        super().__init__(factual.seed, record=record)
        self.rep = int(rep)
        self._points = factual.points
        self._intervals = factual.intervals
        self.forced = dict(factual.forced)
        self.constraints = dict(factual.constraints)
        self.fresh_mechanisms = frozenset(fresh_mechanisms)
        self._salt = struct.pack("<q", self.rep) + b"posterior"

    def _fresh(self, addr: Address) -> float:
        return _to_unit(address_hash(self.seed, addr, self._salt))

    def _base(self, addr: Address) -> float:
        if len(addr) > 1 and addr[1] in self.fresh_mechanisms:
            return self._fresh(addr)
        if addr in self._points:
            return self._points[addr]
        if addr in self._intervals:
            lo, hi = self._intervals[addr]
            return lo + (hi - lo) * self._fresh(addr)
        return self._fresh(addr)


def replicate_seed(base_seed: int, rep: int) -> int:
    """Seed for an independent replicate. Derived from the index, never the worker."""
    return address_hash(base_seed, ("replicate", rep)) & ((1 << 62) - 1)
