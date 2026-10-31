"""Interval timelines: the values of the structural causal model.

A ``Timeline`` is a normalised, sorted set of disjoint "bad" intervals
``[a, b)``. Each interval also carries three ids:

* ``on`` is the event that started it;
* ``off`` is the event that ended it, or "" if it is still open at the
  horizon;
* ``root`` is the exogenous root fault the episode descends from.

Structural equations are written in this algebra, which keeps the
provenance attached to every endpoint as it passes through union,
intersection and onset delays.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Iterable

Q = 2  # decimals of a minute (0.6 s) - all decisions see quantised time


def q(t: float) -> float:
    return round(float(t), Q)


@dataclass(frozen=True)
class Iv:
    a: float
    b: float
    on: str = ""
    off: str = ""
    root: str = ""


class Timeline:
    __slots__ = ("iv",)

    def __init__(self, intervals: Iterable[Iv] = ()):
        self.iv: tuple[Iv, ...] = self._normalise(intervals)

    @staticmethod
    def _normalise(intervals) -> tuple[Iv, ...]:
        items = sorted((Iv(q(i.a), q(i.b), i.on, i.off, i.root) for i in intervals if q(i.b) > q(i.a)),
                       key=lambda i: (i.a, i.b, i.on))
        out: list[Iv] = []
        for i in items:
            if out and i.a <= out[-1].b:
                last = out[-1]
                if i.b > last.b:
                    out[-1] = Iv(last.a, i.b, last.on, i.off, last.root)
            else:
                out.append(i)
        return tuple(out)

    # -- queries ------------------------------------------------------------
    def __bool__(self) -> bool:
        return bool(self.iv)

    def __len__(self) -> int:
        return len(self.iv)

    def __iter__(self):
        return iter(self.iv)

    def __eq__(self, other) -> bool:
        return isinstance(other, Timeline) and self.iv == other.iv

    def at(self, t: float) -> Iv | None:
        for i in self.iv:
            if i.a <= t < i.b:
                return i
            if i.a > t:
                break
        return None

    def total(self, t0: float = float("-inf"), t1: float = float("inf")) -> float:
        return sum(max(0.0, min(i.b, t1) - max(i.a, t0)) for i in self.iv)

    def first_difference(self, other: "Timeline") -> float | None:
        """Earliest time at which the two timelines' membership differs."""
        pts = sorted({p for i in self.iv + other.iv for p in (i.a, i.b)})
        for p in pts:
            if (self.at(p) is None) != (other.at(p) is None):
                return p
            # same membership but a different provenance also counts as divergence
            x, y = self.at(p), other.at(p)
            if x is not None and y is not None and (x.on, x.root) != (y.on, y.root):
                return p
        return None

    def digest(self) -> str:
        h = hashlib.blake2b(digest_size=12)
        for i in self.iv:
            h.update(f"{i.a:.2f}|{i.b:.2f}|{i.on}|{i.off}|{i.root};".encode())
        return h.hexdigest()

    # -- algebra ------------------------------------------------------------
    def union(self, other: "Timeline") -> "Timeline":
        return Timeline(self.iv + other.iv)

    def intersect(self, other: "Timeline") -> "Timeline":
        """Intersection; the later start supplies ``on``, the earlier end ``off``.

        ``root`` is taken from ``self``: by convention the left operand is the
        primary failure and the right one the enabling condition.
        """
        out, j = [], 0
        for x in self.iv:
            for y in other.iv:
                a, b = max(x.a, y.a), min(x.b, y.b)
                if a < b:
                    on = x.on if x.a >= y.a else y.on
                    off = x.off if x.b <= y.b else y.off
                    out.append(Iv(a, b, on, off, x.root))
        return Timeline(out)

    def minus(self, other: "Timeline") -> "Timeline":
        """Remove ``other`` from ``self``; cut points take ``other``'s tags."""
        out = []
        for x in self.iv:
            pieces = [x]
            for y in other.iv:
                nxt = []
                for p in pieces:
                    if y.b <= p.a or y.a >= p.b:
                        nxt.append(p)
                        continue
                    if y.a > p.a:
                        nxt.append(Iv(p.a, y.a, p.on, y.on, p.root))
                    if y.b < p.b:
                        nxt.append(Iv(y.b, p.b, y.off, p.off, p.root))
                pieces = nxt
            out.extend(pieces)
        return Timeline(out)

    def delay_onset(self, delay: Callable[[Iv], float], on_tag: Callable[[Iv], str] | None = None) -> "Timeline":
        """A buffer: each interval only starts biting ``delay(iv)`` minutes in."""
        out = []
        for i in self.iv:
            d = delay(i)
            if i.a + d < i.b:
                out.append(Iv(i.a + d, i.b, on_tag(i) if on_tag else i.on, i.off, i.root))
        return Timeline(out)

    def clip(self, t0: float, t1: float) -> "Timeline":
        return Timeline(Iv(max(i.a, t0), min(i.b, t1), i.on, i.off, i.root) for i in self.iv)

    def to_list(self) -> list[dict]:
        return [{"a": i.a, "b": i.b, "on": i.on, "off": i.off, "root": i.root} for i in self.iv]


EMPTY = Timeline()
