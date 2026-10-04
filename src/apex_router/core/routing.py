"""pce-core routing — the evidence cell state machine (spec §5) plus the existing promotion gate
and break-even, re-exported (reuse, not rewrite). ε-greedy exploration is deliberately absent:
it would act without labels (advise-only until cells are READY).
plugin/hooks/core/routing.ts mirrors cell_new/cell_observe."""
from __future__ import annotations

import math

from apex_router.core.drift import cusum_lower
from apex_router.core.stats import wilson_ci
from apex_router.gate import run_gate  # noqa: F401  (re-exported)
from apex_router.route_advise import _break_even as break_even  # noqa: F401  (re-exported)

MIN_N = 30      # sample floor (same as route-advise)
TARGET = 0.9    # promotion target — a prior, revisit after 30 days of labels (§15)
WINDOW = 10     # labels per evaluation window
ENTER = 2       # READY → DRIFTING after 2 consecutive windows below target
EXIT = 3        # DRIFTING → READY after 3 consecutive windows at/above target
REBASE = 3      # DRIFTING + 3 more windows below → stable new regime: start over
P0_CAP = 0.98   # keeps the CUSUM's standard error away from 0 when the baseline is 100%


def cell_new() -> dict:
    return {"n": 0, "pass": 0, "state": "COLD", "win_n": 0, "win_pass": 0,
            "below": 0, "above": 0, "cusum": 0.0, "p0": None}


def cell_observe(cell: dict, passed: bool, min_n: int = MIN_N, target: float = TARGET,
                 window: int = WINDOW) -> dict:
    x = 1 if passed else 0
    c = dict(cell)
    c["n"] += 1
    c["pass"] += x
    c["win_n"] += 1
    c["win_pass"] += x
    alarm = False
    if c["win_n"] >= window:
        rate = c["win_pass"] / c["win_n"]
        below = rate < target
        if c["state"] in ("READY", "DRIFTING") and c["p0"] is not None:
            p0 = min(c["p0"], P0_CAP)
            se = math.sqrt(p0 * (1 - p0) / c["win_n"])
            c["cusum"], alarm = cusum_lower(c["cusum"], (rate - p0) / se)
        if c["state"] == "READY":
            c["below"] = c["below"] + 1 if below else 0
            if c["below"] >= ENTER or alarm:
                c["state"], c["below"], c["above"] = "DRIFTING", 0, 0
        elif c["state"] == "DRIFTING":
            if below:
                c["below"] += 1
                c["above"] = 0
            else:
                c["above"] += 1
                c["below"] = 0
            if c["above"] >= EXIT:
                c["state"], c["above"], c["cusum"] = "READY", 0, 0.0
            elif c["below"] >= REBASE:
                return cell_new()
        c["win_n"] = 0
        c["win_pass"] = 0
    if c["state"] in ("COLD", "WARMING"):
        if c["n"] >= min_n and wilson_ci(c["pass"], c["n"])[0] >= target:
            c["state"], c["p0"], c["cusum"], c["below"], c["above"] = "READY", c["pass"] / c["n"], 0.0, 0, 0
        else:
            c["state"] = "COLD" if c["n"] == 0 else "WARMING"
    return c
