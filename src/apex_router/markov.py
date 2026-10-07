"""Two-state Markov chain over per-call outcomes — a bursty-failure signal beside zeno's p^n.

zeno's horizon assumes calls fail independently: P(n clean calls) = p^n. On this machine's proxy
telemetry they do not — a failed call is far more likely right after another failure (measured:
fail→fail pairs are seconds apart, the client's immediate retry failing too; slower drift over
hours is NOT captured). This module fits the simplest model that has memory, a chain over {ok, fail}:

    P(ok | ok) = a,   P(fail | fail) = b,   P(first call ok) = q
    P(n-call session clean) = q * a^(n-1)

and reports it as an INDEPENDENT estimate next to p^n, not a replacement. It still treats every
session as the same chain: between-session and over-time heterogeneity (one session on a bad
upstream, another on a good one) is not modeled, so long sessions can still be under-predicted.

Pure: no I/O. Inputs are per-session 0/1 sequences in time order (1 = failed call), and for the
held-out split, sessions in first-appearance order.
"""
from __future__ import annotations

import math

_EPS = 1e-12


def fit_chain(seqs, prior: float = 0.5) -> dict:
    """Transition counts and probabilities with an additive `prior` per cell.

    A row with no observed transitions (no failure followed by another call) has probability None,
    not the prior alone — a number made only of prior is not an estimate. Same for the initial
    state with no sessions.
    """
    oo = of = fo = ff = 0
    first_ok = first_fail = 0
    for s in seqs:
        if not s:
            continue
        if s[0]:
            first_fail += 1
        else:
            first_ok += 1
        for x, y in zip(s, s[1:]):
            if x:
                ff += 1 if y else 0
                fo += 0 if y else 1
            else:
                of += 1 if y else 0
                oo += 0 if y else 1

    def _p(hit, miss):
        return (hit + prior) / (hit + miss + 2 * prior) if hit + miss else None

    return {"counts": {"ok_ok": oo, "ok_fail": of, "fail_ok": fo, "fail_fail": ff,
                       "first_ok": first_ok, "first_fail": first_fail},
            "sessions": first_ok + first_fail, "pairs": oo + of + fo + ff, "prior": prior,
            "p_ok_ok": _p(oo, of), "p_fail_fail": _p(ff, fo),
            "p_first_ok": _p(first_ok, first_fail)}


def autocorr(seqs) -> tuple:
    """(r1, pairs): Pearson correlation of consecutive within-session outcomes. r1 is None with
    fewer than 2 pairs or when either side never varies (all ok, or all fail)."""
    xs, ys = [], []
    for s in seqs:
        for x, y in zip(s, s[1:]):
            xs.append(x)
            ys.append(y)
    n = len(xs)
    if n < 2:
        return None, n
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None, n
    return sxy / math.sqrt(sxx * syy), n


def p_clean(chain: dict, n: int):
    """P(an n-call session has no failed call) under the chain: q * a^(n-1). None if unfitted.
    A single-call session needs only q, so it is defined even when a is not."""
    q, a = chain.get("p_first_ok"), chain.get("p_ok_ok")
    if q is None or n < 1:
        return None
    if n == 1:
        return q
    return None if a is None else q * a ** (n - 1)


def mean_fail_run(p_fail_fail):
    """Expected length of a failure burst, 1/(1 - P(fail|fail)). None if unfitted or b == 1."""
    if p_fail_fail is None or p_fail_fail >= 1.0:
        return None
    return 1.0 / (1.0 - p_fail_fail)


def stationary_fail(p_ok_ok, p_fail_fail):
    """Long-run share of failed calls: (1-a) / ((1-a) + (1-b)). None if either is unfitted."""
    if p_ok_ok is None or p_fail_fail is None:
        return None
    den = (1.0 - p_ok_ok) + (1.0 - p_fail_fail)
    return (1.0 - p_ok_ok) / den if den > 0 else None


def iid_p(seqs, prior: float = 0.5):
    """Pooled per-call success with the same additive prior as the chain. None with no calls."""
    calls = sum(len(s) for s in seqs)
    if not calls:
        return None
    fails = sum(sum(s) for s in seqs)
    return (calls - fails + prior) / (calls + 2 * prior)


def clean_loglik(seqs, predict) -> float | None:
    """Sum over sessions of log P(observed clean / not clean); `predict(n)` -> P(clean). Probabilities
    are clipped to [1e-12, 1-1e-12] so one confident miss is a large penalty, not -inf."""
    total, any_ = 0.0, False
    for s in seqs:
        if not s:
            continue
        pc = predict(len(s))
        if pc is None:
            return None
        pc = min(max(pc, _EPS), 1.0 - _EPS)
        total += math.log(pc if not any(s) else 1.0 - pc)
        any_ = True
    return total if any_ else None


def clean_brier(seqs, predict) -> float | None:
    """Mean squared error of P(clean) against the 0/1 clean outcome, per session."""
    errs = []
    for s in seqs:
        if not s:
            continue
        pc = predict(len(s))
        if pc is None:
            return None
        errs.append((pc - (0.0 if any(s) else 1.0)) ** 2)
    return sum(errs) / len(errs) if errs else None


def holdout(seqs, train: float = 0.7, prior: float = 0.5) -> dict:
    """Fit on the first `train` share of sessions (in the order given — first appearance), score
    P(clean) on the rest: iid p^n vs the chain. Deterministic; no shuffling. None fields when either
    side is empty or a model cannot be fitted from the training sessions."""
    seqs = [list(s) for s in seqs if s]
    k = int(round(train * len(seqs)))
    tr, te = seqs[:k], seqs[k:]
    out = {"n_train": len(tr), "n_test": len(te), "iid_loglik": None, "markov_loglik": None,
           "iid_brier": None, "markov_brier": None}
    if not tr or not te:
        return out
    p = iid_p(tr, prior)
    ch = fit_chain(tr, prior)

    def iid(n):
        return None if p is None else p ** n

    def mk(n):
        return p_clean(ch, n)

    out.update(iid_loglik=clean_loglik(te, iid), markov_loglik=clean_loglik(te, mk),
               iid_brier=clean_brier(te, iid), markov_brier=clean_brier(te, mk))
    return out
