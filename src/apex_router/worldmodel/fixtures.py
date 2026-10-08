"""Deterministic synthetic step/task data in the §1 contract schema — for tests and `--synthetic`.

Not a model of this machine's sessions: a generator with KNOWN dynamics, so every E2 estimator
can be checked against the answer it should recover.

**Circularity, stated:** the `short` kind plants exactly the geometric-to-residual shape the Zeno
detector looks for, and `stuck` the flat shape the stalled detector looks for. A detector score on
this data checks the code (does it find what was planted, at what FPR), not the method (does that
shape occur in, and predict, real failed tasks). `worldmodel progress --synthetic` says so.

- **Action dependence**: order 1 (P(b | a)) or order 2 (P(c | a, b)); each row puts most mass on
  one fixed successor and spreads the rest (Dirichlet), plus a pull towards `test` so tasks run
  tests often enough to measure progress. Per task type the rows are perturbed (hierarchy to find).
  The dynamics are fixed by (order, n_actions), not by `seed`: every seed draws from the same
  process, so a model fitted on one split is the right model for the next.
- **Bursty errors** on non-test steps: a 2-state chain, P(err | ok) = 0.04 × the day's regime
  multiplier, P(err | err) = 0.45 (a day-level regime, the thing `markov.py` does not model).
- **Task kinds** drive the failing-test count at each `test` step and the outcome:
  `converge` — failing ← floor(r·failing), r ~ U(0.3, 0.6), reaches 0 → success;
  `short`    — failing = R + round((f0 − R)·r^k), a geometric decay to a residual R ≥ 1 → the task
               runs on and ends `fail` (60%) or `partial` (40%);
  `stuck`    — failing flat ± 1 → `fail`;
  `notest`   — no tests at all; success iff the last step is clean and < 20% of steps errored.
  A `test` step errors iff it reports failures. `partial` tasks spawn a subagent half the time.
- **Workflows** W0 (solo) / W2 (delegate first): the kind mix depends on (task type, workflow), so
  a workflow ranking has a true answer (`KIND_MIX`: W2 helps `fix`, hurts `feature`).
- **Split** by session start: first 70% of sessions train, next 10% val, last 20% test.
"""
from __future__ import annotations

from collections import namedtuple

import numpy as np

from .protocol import ACTIONS

Synthetic = namedtuple("Synthetic", "steps tasks truth")

_PRIORITY = ("read", "edit", "test", "search", "run", "vcs", "write", "build", "plan",
             "delegate", "ask", "remote", "other")
_TOOL = {"search": "Grep", "read": "Read", "edit": "Edit", "write": "Write", "run": "bash",
         "test": "bash", "build": "bash", "vcs": "bash", "remote": "bash", "plan": "TodoWrite",
         "delegate": "Agent", "ask": "AskUserQuestion", "other": "other"}
TYPES = ("fix", "feature", "chore")
# P(kind) per (task type, workflow): converge / short / stuck / notest
KIND_MIX = {("fix", "W0"): (0.50, 0.20, 0.10, 0.20), ("fix", "W2"): (0.70, 0.10, 0.05, 0.15),
             ("feature", "W0"): (0.55, 0.15, 0.10, 0.20), ("feature", "W2"): (0.35, 0.25, 0.20, 0.20),
             ("chore", "W0"): (0.40, 0.10, 0.10, 0.40), ("chore", "W2"): (0.40, 0.10, 0.10, 0.40)}
KINDS = ("converge", "short", "stuck", "notest")


def vocab(n_actions: int) -> list:
    if not 3 <= n_actions <= len(ACTIONS):
        raise ValueError(f"n_actions must be in [3, {len(ACTIONS)}]")
    return list(_PRIORITY[:n_actions])


def _dynamics(order: int, n_actions: int, test_pull: float):
    """Transition tensors fixed by (order, n_actions). Index K = START."""
    if order not in (1, 2):
        raise ValueError("order must be 1 or 2")
    K = n_actions
    rng = np.random.default_rng(1000 + 100 * order + K)
    test = vocab(K).index("test")
    shape = (K + 1,) * order
    peak, conc = (0.55, 0.6) if order == 1 else (0.70, 0.5)
    succ = rng.integers(0, K, size=shape)
    base = rng.dirichlet(np.full(K, conc), size=shape)
    T = (1 - peak) * base
    for idx in np.ndindex(shape):
        T[idx + (succ[idx],)] += peak
    T = (1 - test_pull) * T
    T[..., test] += test_pull
    per_type = {}
    for j, ty in enumerate(TYPES):
        r2 = np.random.default_rng(5000 + 100 * order + 10 * K + j)
        per_type[ty] = 0.8 * T + 0.2 * r2.dirichlet(np.full(K, 0.5), size=shape)
    return per_type


def _phases(acts) -> list:
    """§2 phases from the action sequence of one task."""
    out, edited, n = [], False, len(acts)
    for i, a in enumerate(acts):
        if a in ("edit", "write"):
            out.append("edit")
            edited = True
        elif a in ("search", "read") and not edited:
            out.append("explore")
        elif a in ("test", "build", "run") and edited:
            out.append("verify")
        elif a in ("vcs", "ask") and i >= n - 2:
            out.append("deliver")
        else:
            out.append("other")
    return out


def synthetic(n_sessions: int = 200, seed: int = 0, order: int = 1, n_actions: int = 13,
              days: int = 14, weak_share: float = 0.0, unknown_share: float = 0.05,
              t_start: float = 1_790_000_000.0, test_pull: float = 0.15,
              typed: bool = True) -> Synthetic:
    """Generate `n_sessions` sessions. Returns (steps, tasks, truth): lists of contract rows plus
    the hidden truth per task id (kind, workflow, type, r, residual, true outcome)."""
    rng = np.random.default_rng(seed)
    voc = vocab(n_actions)
    K = len(voc)
    T = _dynamics(order, K, test_pull)
    test_i = voc.index("test")
    alt_test = voc.index("run") if "run" in voc else voc.index("read")
    day_mult = np.exp(rng.normal(0.0, 0.6, size=days))
    starts = np.sort(rng.uniform(0, days * 86400.0, size=n_sessions)) + t_start
    n_tr, n_va = int(round(0.7 * n_sessions)), int(round(0.1 * n_sessions))
    steps, tasks, truth = [], [], {}
    for si, t0s in enumerate(starts):
        sid = f"s{seed}-{si:04d}"
        split = "train" if si < n_tr else "val" if si < n_tr + n_va else "test"
        ts = float(t0s)
        mult = day_mult[min(int((t0s - t_start) // 86400), days - 1)]
        for ti in range(int(rng.integers(1, 5))):
            tid = f"{sid}:{ti}"
            ty = TYPES[int(rng.integers(0, len(TYPES)))]
            wf = "W2" if ("delegate" in voc and rng.random() < 0.3) else "W0"
            kind = KINDS[int(rng.choice(4, p=KIND_MIX[(ty, wf)]))]
            r = float(rng.uniform(0.3, 0.6))
            f0 = int(rng.integers(4, 41))
            resid = int(rng.integers(1, 5)) if kind == "short" else 0
            cap = {"converge": 120, "short": int(rng.integers(35, 91)),
                   "stuck": int(rng.integers(30, 81)), "notest": int(rng.integers(5, 41))}[kind]
            Tt = T[ty]
            hist = [K] * order
            acts, errs, tests_l, failing, k_run = [], [], [], f0, 0
            done_at = None
            for i in range(cap):
                if wf == "W2" and i == 1:
                    a = voc.index("delegate")
                else:
                    a = int(rng.choice(K, p=Tt[tuple(hist[-order:])]))
                if kind == "notest" and a == test_i:
                    a = alt_test
                tst = {"ran": 0, "failed": None, "passed": None}
                if a == test_i:
                    if kind == "converge":
                        failing = f0 if k_run == 0 else int(np.floor(failing * r))
                    elif kind == "short":
                        failing = resid + int(round((f0 - resid) * r ** k_run))
                    else:
                        failing = max(1, f0 + int(rng.integers(-1, 2))) if k_run else f0
                    k_run += 1
                    tst = {"ran": 1, "failed": failing, "passed": int(rng.integers(20, 200))}
                    e = 1 if failing > 0 else 0
                else:
                    p_err = 0.45 if (errs and errs[-1]) else min(0.04 * mult, 0.5)
                    e = int(rng.random() < p_err)
                acts.append(a)
                errs.append(e)
                tests_l.append(tst)
                hist.append(a)
                if kind == "converge" and tst["ran"] and failing == 0:
                    done_at = i
                    break
            if done_at is not None:                       # deliver after the green run
                for _ in range(int(rng.integers(1, 3))):
                    acts.append(voc.index("vcs") if "vcs" in voc else voc.index("edit"))
                    errs.append(0)
                    tests_l.append({"ran": 0, "failed": None, "passed": None})
            L = len(acts)
            if kind == "converge":
                outcome = "success" if done_at is not None else "partial"
            elif kind == "notest":
                outcome = "success" if (errs[-1] == 0 and sum(errs) < 0.2 * L) else "fail"
            elif kind == "short":
                outcome = "partial" if rng.random() < 0.4 else "fail"
            else:
                outcome = "fail"
            spawn_tail = outcome == "partial" and "delegate" in voc and rng.random() < 0.5
            if spawn_tail:
                acts[-1] = voc.index("delegate")
            names = [voc[a] for a in acts]
            ph = _phases(names)
            t_task0 = ts
            for i, a in enumerate(names):
                dt = None if i == 0 else float(rng.exponential(6.0) * (3.0 if errs[i - 1] else 1.0))
                ts += dt or 0.0
                steps.append({"sid": sid, "agent": None, "src": "claude", "task": tid, "i": i,
                              "ts": ts, "act": a, "tool": _TOOL[a], "err": errs[i],
                              "tests": tests_l[i],
                              "out_b": int(min(3, rng.poisson(1.2 if a == "test" else 0.6))),
                              "in_b": int(min(3, rng.poisson(0.4))), "dt": dt, "phase": ph[i],
                              "model": None, "spawn": 1 if a == "delegate" else 0})
            u = rng.random()
            if u < unknown_share:
                outcome_l, src = "unknown", "none"
            else:
                outcome_l, src = outcome, ("weak" if rng.random() < weak_share else "gold")
            row = {"task": tid, "sid": sid, "src": "claude", "t0": t_task0, "t1": ts, "steps": L,
                   "outcome": outcome_l, "outcome_src": src, "split": split, "workflow": wf}
            if typed:
                row["task_type"] = ty
            tasks.append(row)
            truth[tid] = {"kind": kind, "workflow": wf, "type": ty, "r": r, "residual": resid,
                          "outcome": outcome, "f0": f0}
            ts += float(rng.uniform(60, 600))
    return Synthetic(steps, tasks, {"tasks": truth, "vocab": voc, "order": order})


def dataset(n_sessions: int = 200, seed: int = 0, **kw):
    """The synthetic data as a protocol.Dataset (plus the truth dict)."""
    from .protocol import build
    syn = synthetic(n_sessions, seed, **kw)
    return build(syn.steps, syn.tasks), syn.truth
