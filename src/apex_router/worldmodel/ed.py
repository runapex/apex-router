"""Experiment E-D readout — token-matched W0 vs W2, randomised per turn, intention-to-treat.

Pre-declared in docs/research/2026-10-12-rsi-next-steps.md §3: per task type, keep W2 only where
A_e > 1 at matched tokens. Here A_e = (success rate / mean tokens) of W2 over the same of W0
(Kim et al., arXiv 2512.08296: E_MAS / E_SAS), with a percentile bootstrap over SESSIONS (tasks
cluster by session). The success-rate difference W2 − W0 is reported per group (Newcombe interval,
`core.verdict`); the decision follows the A_e rule, not the success contrast. Units are tasks from
``tasks.jsonl`` with an ``ed_arm`` (the context block reached the prompt and a fan-out tool was
offered) and a known outcome; the arm is the assignment, not what ran (``workflow`` is what ran;
``compliance`` is their agreement).

Caveats stated on the card: tokens = input + output + cache read + cache write at equal weight (a
volume measure, not a cost measure: cache reads dominate it); no multiplicity correction across
task types (each rule is a 95% interval on its own). Stdlib only.
"""
from __future__ import annotations

import json
import math
import random
from collections import defaultdict

from apex_router.core import verdict as V

ARMS = ("W0", "W2")
MIN_N = 30
MIN_SESSIONS = 10
N_BOOT = 2000
ADOPT = "adopt W2"
STAY = "stay on W0"


def _units(tasks) -> list:
    out = []
    for t in tasks:
        if t.get("ed_arm") not in ARMS or t.get("outcome") not in ("success", "partial", "fail"):
            continue
        tok = t.get("ed_tok")
        out.append({"type": t.get("task_type") or "null", "arm": t["ed_arm"], "sid": t.get("sid"),
                    "ok": t["outcome"] == "success", "workflow": t.get("workflow"),
                    "tok": float(tok) if isinstance(tok, (int, float)) and not isinstance(tok, bool)
                    and tok > 0 else None,
                    "steps": (t.get("steps") or 0) + (t.get("sub_steps") or 0)})
    return out


def _arm_summary(us: list) -> dict:
    toks = [u["tok"] for u in us if u["tok"] is not None]
    return {"n": len(us), "success": sum(u["ok"] for u in us),
            "rate": round(sum(u["ok"] for u in us) / len(us), 4) if us else None,
            "mean_tok": round(sum(toks) / len(toks), 1) if toks else None,
            "mean_steps": round(sum(u["steps"] for u in us) / len(us), 2) if us else None,
            "compliance": round(sum(u["workflow"] == u["arm"] for u in us) / len(us), 4) if us else None}


def _efficiency(us: list):
    """success rate per mean token over the tokened units; None when there are none."""
    toks = [u for u in us if u["tok"] is not None]
    if not toks:
        return None
    return (sum(u["ok"] for u in toks) / len(toks)) / (sum(u["tok"] for u in toks) / len(toks))


def _ratio(e2, e0):
    if e2 is None or e0 is None:
        return None
    if e0 == 0:
        return math.inf if e2 > 0 else None
    return e2 / e0


def a_e(units: list, *, n_boot: int = N_BOOT, seed: int = 1):
    """A_e = efficiency(W2) / efficiency(W0) and a percentile interval from a session-level
    bootstrap (sessions resampled with replacement; each draw keeps the arms its sessions carry).
    A draw where W0 has no success is +inf, kept in the tail (dropping it would bias toward W0).
    Returns (estimate, (lo, hi)); the estimate is inf when W0 never succeeds and W2 does, None
    when either arm has no tokened unit."""
    tokened = [u for u in units if u["tok"] is not None]
    w2 = [u for u in tokened if u["arm"] == "W2"]
    w0 = [u for u in tokened if u["arm"] == "W0"]
    est = _ratio(_efficiency(w2), _efficiency(w0))
    if est is None:
        return None, (None, None)
    by_sid: dict = defaultdict(list)
    for u in tokened:
        by_sid[u["sid"]].append(u)
    sids = sorted(by_sid, key=str)
    rng = random.Random(seed)
    draws = []
    for _ in range(n_boot):
        pick = [by_sid[sids[rng.randrange(len(sids))]] for _ in sids]
        us = [u for grp in pick for u in grp]
        r = _ratio(_efficiency([u for u in us if u["arm"] == "W2"]),
                   _efficiency([u for u in us if u["arm"] == "W0"]))
        if r is not None:
            draws.append(r)
    if len(draws) < 10:
        return est, (None, None)
    draws.sort()
    return est, (draws[int(0.025 * (len(draws) - 1))], draws[int(0.975 * (len(draws) - 1))])


def _num(x):
    """JSON-safe: inf -> None (the card flags it), floats rounded."""
    return None if x is None or (isinstance(x, float) and math.isinf(x)) else round(x, 3)


def _rule(ae, lo, hi, enough: bool) -> str:
    if ae is None or lo is None or not enough:
        return V.INCONCLUSIVE
    if lo > 1:
        return "keep W2"
    if hi < 1:
        return "keep W0"
    return V.NO_DIFF


def readout(tasks, *, min_n: int = MIN_N, min_sessions: int = MIN_SESSIONS,
            n_boot: int = N_BOOT) -> dict:
    """The card: overall and per task type. Per group: both arms' summaries, the success contrast,
    A_e with its interval and the pre-declared rule (keep W2 only when A_e's interval lies above 1,
    both arms reach ``min_n`` tasks and the group spans ``min_sessions`` sessions — the bootstrap
    resamples sessions, so two sessions are two clusters however many tasks they hold). The decision follows the rule: adopt W2 on the types where it
    holds; stay on W0 (kill) when at least one type reached a verdict and none holds; INCONCLUSIVE
    while no type has (enough tasks per arm and tokens) for a verdict."""
    units = _units(tasks)
    by_type: dict = defaultdict(list)
    for u in units:
        by_type[u["type"]].append(u)
    groups = [("all", units)] + sorted(by_type.items())
    cells, contrasts = {}, []
    for name, us in groups:
        w0 = [u for u in us if u["arm"] == "W0"]
        w2 = [u for u in us if u["arm"] == "W2"]
        c = V.proportion_contrast(f"success:{name}", "W2", "W0", sum(u["ok"] for u in w2), len(w2),
                                  sum(u["ok"] for u in w0), len(w0), metric="success rate",
                                  min_n=min_n)
        ae, (lo, hi) = a_e(us, n_boot=n_boot)
        enough = len(w0) >= min_n and len(w2) >= min_n and \
            len({u["sid"] for u in us}) >= min_sessions
        cells[name] = {"W0": _arm_summary(w0), "W2": _arm_summary(w2),
                       "success_contrast": c.status, "enough": enough,
                       "a_e": _num(ae), "a_e_ci": (_num(lo), _num(hi)),
                       "a_e_unbounded": bool(ae is not None and (math.isinf(ae)
                                                                 or (hi is not None and math.isinf(hi)))),
                       "rule": _rule(ae, lo, hi, enough)}
        contrasts.append(c)
    types = {k: v for k, v in cells.items() if k != "all"}
    keep = sorted(k for k, v in types.items() if v["rule"] == "keep W2")
    judged = sorted(k for k, v in types.items() if v["rule"] != V.INCONCLUSIVE)
    kill = bool(judged) and not keep
    if keep:
        decision = f"{ADOPT} on {', '.join(keep)}; W0 elsewhere"
    elif kill:
        decision = V.decide("W0", kills={"no_type_with_A_e_above_1": True})
    else:
        decision = V.INCONCLUSIVE
    card = V.Verdict(
        question="E-D: does W2 (delegate) beat W0 (single session) at matched tokens, per task type?",
        incumbent="W0", decision=decision,
        arms={a: cells["all"][a] for a in ARMS}, contrasts=[V.asdict(c) for c in contrasts],
        kills={"no_type_with_A_e_above_1": kill},
        diagnostics={"units": len(units), "sessions": len({u["sid"] for u in units}),
                     "types": types, "judged": judged, "keep_w2_on": keep, "min_n": min_n,
                     "min_sessions": min_sessions},
        meta={"experiment": "E-D", "analysis": "intention-to-treat; bootstrap over sessions",
              "tokens": "input + output + cache read + cache write, equal weight (volume, not cost)",
              "multiplicity": "none across task types (95% per type)",
              "pre_declared": "docs/research/2026-10-12-rsi-next-steps.md §3"})
    return V.asdict(card)


def render(card: dict) -> str:
    d = card["diagnostics"]
    L = [f"E-D (W0 vs W2, randomised per turn, ITT) — units {d['units']} in {d['sessions']} sessions · "
         f"min n/arm {d['min_n']} · min sessions {d['min_sessions']}", f"  decision: {card['decision']}"]
    rows = [("all", {"W0": card["arms"]["W0"], "W2": card["arms"]["W2"], "rule": None, "a_e": None,
                     "a_e_ci": (None, None), "a_e_unbounded": False,
                     "success_contrast": card["contrasts"][0]["status"]})] + sorted(d["types"].items())
    for name, c in rows:
        w0, w2 = c["W0"], c["W2"]
        ae = ""
        if c["a_e"] is not None or c["a_e_unbounded"]:
            lo = c["a_e_ci"][0]
            hi = "∞" if c["a_e_unbounded"] and c["a_e_ci"][1] is None else c["a_e_ci"][1]
            ae = f" · A_e {'∞' if c['a_e'] is None else c['a_e']}" + \
                ("" if lo is None else f" [{lo}, {hi}]")
        rule = "" if c["rule"] is None else f" → {c['rule']}"
        L.append(f"  {name:10} W0 n {w0['n']} ok {w0['rate']} tok {w0['mean_tok']} · "
                 f"W2 n {w2['n']} ok {w2['rate']} tok {w2['mean_tok']} · "
                 f"success W2−W0: {c['success_contrast']}{ae}{rule}")
    if d["keep_w2_on"]:
        L.append(f"  keep W2 on: {', '.join(d['keep_w2_on'])}")
    L.append("  compliance (ran what was assigned): " + ", ".join(
        f"{a} {card['arms'][a]['compliance']}" for a in ARMS))
    L.append(f"  tokens: {card['meta']['tokens']}; multiplicity: {card['meta']['multiplicity']}")
    return "\n".join(L)


def to_json(card: dict) -> str:
    return json.dumps(card, indent=2)
