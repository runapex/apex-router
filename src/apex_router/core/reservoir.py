"""Stratified reservoir — keep rare cases without distorting the overall picture (stdlib only).

Two parts per stratum, kept apart on purpose:

- **Counters** (exact, never sampled): ``N_h``, Welford moments of each numeric field, value counts
  of each enum field. Totals and population means come from here, so a sample never distorts them.
- **Reservoir** (bounded): a uniform sample of up to ``cap`` rows (Vitter's Algorithm R). Row-level
  detail — distributions, percentiles, rare-case examples — comes from here.

Allocation (a floor for rare strata, the rest proportional or Neyman) is applied at ``snapshot``
time by uniform subsampling of each reservoir. A uniform subsample of a simple random sample is a
simple random sample, so every kept row of stratum h has the exact design weight ``N_h / n_h``.
(Changing a stratum's capacity while streaming does NOT keep this property: rows seen before a
capacity rise had a smaller chance than rows seen after it. That is why the reservoir keeps a
fixed ``cap`` and allocation happens only at read time.)

Rules for a caller:
1. Never compute an overall number from kept rows without the weights (use ``estimate``).
2. Stratify on fields known when the row is written; a row must never move to another stratum.
3. Rows are flat: numbers, bools and short enum strings only (no text, no paths).
"""
from __future__ import annotations

import math
import random
import re
from collections.abc import Callable, Iterable
from typing import Any

from .stats import wilson_ci

OTHER = "_other"                        # stratum for new keys once ``max_strata`` is reached
_KEY_RE = re.compile(r"\A[A-Za-z0-9 _.:+#@(),|=-]{1,128}\Z")
_ENUM_MAX = 64                          # distinct values counted per enum field and stratum
_STR_MAX = 64
_INT_MAX = 2 ** 53                      # allocate() shares are floats: exact up to here
# Enum values: short labels only. No "/" or "\" (no path or URL), no "..", no whitespace.
_ENUM_RE = re.compile(r"\A[A-Za-z0-9_.:+#@,=-]{0,64}\Z")


def _stream(seed: Any, purpose: str) -> random.Random:
    """An RNG for one purpose, independent of the reservoir's own stream for the same seed.
    Reusing ``Random(seed)`` for a second draw correlates it with the first (a subsample then
    favours the rows that the reservoir admitted last)."""
    return random.Random(f"apex-reservoir:{purpose}:{seed!r}")


def key(*parts: Any) -> str:
    """Stratum key from parts, e.g. ``key(task_type, tier, outcome)`` -> ``"explore|haiku|ok"``."""
    k = "|".join("" if p is None else str(p) for p in parts)
    if not _KEY_RE.match(k) or k.startswith(("/", "~")) or k == OTHER:
        raise ValueError(f"bad stratum key {k!r}")
    return k


def _check_row(row: dict) -> dict:
    if not isinstance(row, dict):
        raise TypeError("row must be a dict")
    for f, v in row.items():
        if not isinstance(f, str) or not _KEY_RE.match(f):
            raise ValueError(f"bad field name {f!r}")
        if v is None or isinstance(v, (bool, int)):
            continue
        if isinstance(v, float):
            if not math.isfinite(v):
                raise ValueError(f"field {f}: non-finite float")
            continue
        if isinstance(v, str):
            if len(v) > _STR_MAX or not _ENUM_RE.match(v) or ".." in v:
                raise ValueError(f"field {f}: string is not a short enum value")
            continue
        raise TypeError(f"field {f}: {type(v).__name__} is not a number, bool or enum string")
    return dict(row)


# --- exact counters ---------------------------------------------------------------------------

def _moments_push(m: dict | None, x: float) -> dict:
    if m is None:
        return {"n": 1, "mean": float(x), "m2": 0.0, "min": float(x), "max": float(x)}
    n = m["n"] + 1
    d = x - m["mean"]
    mean = m["mean"] + d / n
    return {"n": n, "mean": mean, "m2": m["m2"] + d * (x - mean),
            "min": min(m["min"], x), "max": max(m["max"], x)}


def _moments_merge(a: dict | None, b: dict | None) -> dict | None:
    """Chan et al. parallel combine of two Welford states."""
    if not a or not a["n"]:
        return dict(b) if b else None
    if not b or not b["n"]:
        return dict(a)
    n = a["n"] + b["n"]
    d = b["mean"] - a["mean"]
    return {"n": n, "mean": a["mean"] + d * b["n"] / n,
            "m2": a["m2"] + b["m2"] + d * d * a["n"] * b["n"] / n,
            "min": min(a["min"], b["min"]), "max": max(a["max"], b["max"])}


def _new_stratum() -> dict:
    return {"n": 0, "num": {}, "enum": {}, "rows": []}


def _count(st: dict, row: dict) -> None:
    st["n"] += 1
    for f, v in row.items():
        if v is None:
            continue
        if isinstance(v, (bool, str)):
            c = st["enum"].setdefault(f, {})
            s = str(v).lower() if isinstance(v, bool) else v
            if s in c or len(c) < _ENUM_MAX:
                c[s] = c.get(s, 0) + 1
            else:
                c[OTHER] = c.get(OTHER, 0) + 1
        else:
            st["num"][f] = _moments_push(st["num"].get(f), float(v))


# --- the reservoir ----------------------------------------------------------------------------

class StratifiedReservoir:
    """Exact counters plus a bounded uniform sample per stratum. Deterministic for a given seed."""

    def __init__(self, cap: int = 200, max_strata: int = 256, seed: int = 7):
        if cap < 1 or max_strata < 1:
            raise ValueError("cap and max_strata must be >= 1")
        self.cap = int(cap)
        self.max_strata = int(max_strata)
        self.seed = seed
        self.rng = random.Random(seed)
        self.strata: dict[str, dict] = {}

    # -- write --
    def add(self, stratum: str, row: dict) -> str:
        """Count ``row`` exactly and offer it to the stratum's reservoir. Returns the stratum used
        (``OTHER`` once ``max_strata`` distinct keys exist)."""
        if stratum != OTHER:
            key(stratum)                                    # validates
        row = _check_row(row)
        if stratum not in self.strata and len(self.strata) >= self.max_strata:
            stratum = OTHER
        st = self.strata.setdefault(stratum, _new_stratum())
        _count(st, row)
        if len(st["rows"]) < self.cap:                      # Algorithm R
            st["rows"].append(row)
        else:
            j = self.rng.randrange(st["n"])
            if j < self.cap:
                st["rows"][j] = row
        return stratum

    # -- read --
    def population(self) -> dict:
        """stratum -> N_h (exact)."""
        return {h: st["n"] for h, st in self.strata.items()}

    def counters(self, stratum: str) -> dict:
        """Exact counters of one stratum: ``{"n", "num": {field: moments}, "enum": {field: counts}}``."""
        st = self.strata[stratum]
        return {"n": st["n"], "num": {f: dict(m) for f, m in st["num"].items()},
                "enum": {f: dict(c) for f, c in st["enum"].items()}}

    def total_mean(self, field: str) -> float | None:
        """Population mean of a numeric field from the exact counters (no sampling)."""
        n = s = 0.0
        for st in self.strata.values():
            m = st["num"].get(field)
            if m:
                n += m["n"]
                s += m["n"] * m["mean"]
        return s / n if n else None

    def snapshot(self, budget: int | None = None, floor: int = 0, method: str = "proportional",
                 sd: dict | None = None, seed: int | None = None) -> dict:
        """Kept rows under an allocation, each with its design weight.

        ``budget`` None keeps every reservoir row. Else ``allocate`` (with the reservoir sizes as
        the upper bound) gives n_h, and each reservoir is subsampled uniformly to n_h. The live
        reservoir is not changed. -> ``{"N": {h: N_h}, "n": {h: n_h}, "rows": [(h, weight, row)]}``.
        """
        pop = self.population()
        have = {h: len(st["rows"]) for h, st in self.strata.items()}
        if budget is None:
            take = have
        else:
            take = allocate(pop, budget, floor=floor, method=method, sd=sd, upper=have)
        rng = _stream(self.seed if seed is None else seed, "snapshot")
        rows = []
        for h in sorted(self.strata):
            k = take.get(h, 0)
            if k <= 0:
                continue
            kept = self.strata[h]["rows"]
            picked = kept if k >= len(kept) else rng.sample(kept, k)
            w = pop[h] / len(picked)
            rows.extend((h, w, dict(r)) for r in picked)
        return {"N": pop, "n": {h: k for h, k in take.items() if k > 0}, "rows": rows}

    # -- merge (mesh: one reservoir per node) --
    def merge(self, other: StratifiedReservoir) -> StratifiedReservoir:
        """A new reservoir equal in distribution to one fed both streams.

        Counters add exactly. Per stratum, k = min(cap, N1 + N2) rows are kept: x of them from
        this side, x ~ Hypergeometric(N1 + N2, N1, k), drawn uniformly from each reservoir. Uses
        the smaller ``cap`` and ``max_strata``; strata beyond ``max_strata`` fold into ``OTHER``.
        """
        out = StratifiedReservoir(min(self.cap, other.cap), min(self.max_strata, other.max_strata),
                                  self.seed)
        rng = _stream(self.seed, "merge")
        keys = sorted(set(self.strata) | set(other.strata), key=lambda h: (h == OTHER, h))
        for h in keys:
            a = self.strata.get(h) or _new_stratum()
            b = other.strata.get(h) or _new_stratum()
            dest = h if (h in out.strata or len(out.strata) < out.max_strata) else OTHER
            cur = out.strata.get(dest)
            for part in (a, b):
                if cur is None:
                    cur = _new_stratum()
                cur = _merge_stratum(cur, part, out.cap, rng)
            out.strata[dest] = cur
        return out

    # -- persistence --
    def to_dict(self) -> dict:
        state = self.rng.getstate()
        return {"v": 1, "cap": self.cap, "max_strata": self.max_strata, "seed": self.seed,
                "rng": [state[0], list(state[1]), state[2]], "strata": self.strata}

    @classmethod
    def from_dict(cls, d: dict) -> StratifiedReservoir:
        if d.get("v") != 1:
            raise ValueError("unknown reservoir format")
        r = cls(d["cap"], d["max_strata"], d["seed"])
        v, s, g = d["rng"]
        r.rng.setstate((v, tuple(s), g))
        for h, st in d["strata"].items():
            if h != OTHER:
                key(h)
            r.strata[h] = {"n": int(st["n"]), "num": dict(st["num"]), "enum": dict(st["enum"]),
                           "rows": [_check_row(x) for x in st["rows"]][: r.cap]}
        return r


def _merge_stratum(a: dict, b: dict, cap: int, rng: random.Random) -> dict:
    n1, n2 = a["n"], b["n"]
    out = _new_stratum()
    out["n"] = n1 + n2
    for f in set(a["num"]) | set(b["num"]):
        out["num"][f] = _moments_merge(a["num"].get(f), b["num"].get(f))
    for f in set(a["enum"]) | set(b["enum"]):
        c = dict(a["enum"].get(f, {}))
        for v, k in b["enum"].get(f, {}).items():
            if v in c or len(c) < _ENUM_MAX:
                c[v] = c.get(v, 0) + k
            else:
                c[OTHER] = c.get(OTHER, 0) + k
        out["enum"][f] = c
    k = min(cap, n1 + n2)
    x, left, good = 0, n1 + n2, n1                      # sequential hypergeometric draw
    for _ in range(k):
        if rng.random() * left < good:
            x += 1
            good -= 1
        left -= 1
    ra, rb = a["rows"], b["rows"]
    x = min(x, len(ra))                                 # equal caps make this a no-op
    y = min(k - x, len(rb))
    out["rows"] = [dict(r) for r in rng.sample(ra, x)] + [dict(r) for r in rng.sample(rb, y)]
    return out


# --- allocation and design --------------------------------------------------------------------

def allocate(pop: dict, budget: int, floor: int = 0, method: str = "proportional",
             sd: dict | None = None, upper: dict | None = None) -> dict:
    """Integer sample sizes n_h with sum <= budget and n_h <= min(N_h, upper_h).

    1. Every stratum gets min(floor, bound_h). If the floors alone exceed ``budget``, ValueError.
    2. The rest goes by score — N_h (``proportional``) or N_h * S_h (``neyman``, S_h from ``sd``,
       0.5 when missing or zero) — capped at the bound; a capped stratum's share is given again.
    3. Integers by largest remainder.
    """
    if method not in ("proportional", "neyman"):
        raise ValueError("method must be proportional or neyman")
    for name, v in (("budget", budget), ("floor", floor)):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f"{name} must be an int >= 0")
    if budget > _INT_MAX or any(not isinstance(n, int) or n > _INT_MAX for n in pop.values()):
        raise ValueError("budget and N_h must be ints <= 2**53 (exact in float arithmetic)")
    for u in (upper or {}).values():
        if isinstance(u, bool) or not isinstance(u, int) or u < 0:
            raise ValueError("upper bounds must be ints >= 0")
    bound = {h: min(n, (upper or {}).get(h, n)) for h, n in pop.items() if n > 0}
    # S_h per stratum in the caller's units (0.5 where missing or invalid), then one common scale
    # to <= 1 so N_h * S_h cannot overflow. Neyman shares are invariant to a common scale; strata
    # not in ``pop`` do not set the scale.
    s_raw = {}
    for h in bound:
        s = (sd or {}).get(h)
        s_raw[h] = float(s) if isinstance(s, (int, float)) and math.isfinite(s) and s > 0 else 0.5
    s_max = max(s_raw.values(), default=1.0)
    take = {h: min(floor, b) for h, b in bound.items()}
    if sum(take.values()) > budget:
        raise ValueError(f"floor {floor} x {len(take)} strata needs {sum(take.values())} rows "
                         f"> budget {budget}")

    prop = method == "proportional"

    def score(h):
        return float(pop[h]) if prop else pop[h] * (s_raw[h] / s_max)

    share = {h: float(t) for h, t in take.items()}
    free = {h for h in bound if share[h] < bound[h]}
    rest = budget - sum(take.values())
    while rest > 1e-9 and free:
        tot = sum(score(h) for h in free)
        if tot <= 0:                              # Neyman scores underflowed: share by N_h
            prop = True
            continue
        capped = set()
        for h in free:
            add = rest * score(h) / tot
            if share[h] + add >= bound[h]:
                capped.add(h)
        if not capped:
            for h in free:
                share[h] += rest * score(h) / tot
            rest = 0.0
            break
        for h in capped:
            rest -= bound[h] - share[h]
            share[h] = float(bound[h])
        free -= capped
    out = {h: math.floor(v + 1e-9) for h, v in share.items()}
    left = min(budget, sum(bound.values())) - sum(out.values())
    for h in sorted(share, key=lambda h: (-(share[h] - out[h]), h)):
        if left <= 0:
            break
        if out[h] < bound[h]:
            out[h] += 1
            left -= 1
    return out


def design_effect(pop: dict, take: dict) -> float:
    """Kish design effect of weights N_h/n_h: deff = n * sum_h(W_h^2 / n_h), W_h = N_h / N.
    1.0 for proportional allocation; > 1 when small strata are over-sampled."""
    strata = [h for h in pop if take.get(h, 0) > 0]
    n = sum(take[h] for h in strata)
    big_n = sum(pop[h] for h in strata)
    if not n or not big_n:
        return float("nan")
    return n * sum((pop[h] / big_n) ** 2 / take[h] for h in strata)


def n_eff(weights: Iterable[float]) -> float:
    """Kish effective sample size (sum w)^2 / sum w^2."""
    w = list(weights)
    s2 = sum(x * x for x in w)
    return sum(w) ** 2 / s2 if s2 else 0.0


def rows_needed(p: float, target_low: float, deff: float = 1.0, z: float = 1.96,
                limit: float = 1e7) -> float:
    """Raw rows so that the Wilson low bound at observed rate ``p`` reaches ``target_low``:
    the smallest n_eff with wilson_low(p, n_eff) >= target_low, times ``deff``. inf if p <= target."""
    if not (0 <= p <= 1) or not (0 <= target_low < 1) or deff <= 0:
        raise ValueError("need 0 <= p <= 1, 0 <= target_low < 1, deff > 0")
    if p <= target_low:
        return math.inf

    def low(n):
        return wilson_ci(p * n, n, z)[0]

    lo, hi = 1e-9, 1.0
    while low(hi) < target_low:
        hi *= 2
        if hi > limit:
            return math.inf
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if low(mid) < target_low else (lo, mid)
    return hi * deff


# --- estimators over a snapshot ---------------------------------------------------------------

def estimate(snap: dict, fn: Callable[[dict], float | bool | None]) -> dict:
    """Stratified estimate of the population mean of ``fn(row)`` (a bool gives a proportion).

    Mean = sum_h W_h * ybar_h over strata with kept rows; SE from sum_h W_h^2 (1 - n_h/N_h) s_h^2/n_h.
    Rows where ``fn`` returns None are left out (missing at random inside their stratum).
    ``covered`` = the population share of strata that gave a value: below 1.0 the mean describes
    only the covered strata. ``se`` is None when a stratum gives 1 row of N_h > 1 (its variance is
    unknown). -> {mean, se, n, n_eff, covered}.
    """
    by: dict = {}
    for h, _, row in snap["rows"]:
        y = fn(row)
        if y is not None:
            by.setdefault(h, []).append(float(y))
    pop = snap["N"]
    big_n = sum(pop.values())
    cov_n = sum(pop[h] for h in by)
    if not by or not cov_n:
        return {"mean": None, "se": None, "n": 0, "n_eff": 0.0, "covered": 0.0}
    mean = var = 0.0
    weights = []
    se_known = True                               # False: a stratum has 1 row of N_h > 1
    for h, ys in by.items():
        wh = pop[h] / cov_n
        nh = len(ys)
        m = sum(ys) / nh
        if nh > 1:
            s2 = sum((y - m) ** 2 for y in ys) / (nh - 1)
        else:
            s2 = 0.0
            se_known = se_known and pop[h] <= 1       # one row cannot show its own variance
        mean += wh * m
        var += wh * wh * (1 - nh / pop[h]) * s2 / nh
        weights.extend([pop[h] / nh] * nh)
    return {"mean": mean, "se": math.sqrt(max(var, 0.0)) if se_known else None, "n": len(weights),
            "n_eff": n_eff(weights), "covered": cov_n / big_n}
