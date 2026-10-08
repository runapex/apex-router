"""Absorbing Markov chain over action classes (DESIGN-worldmodel-P6.md §4, PLAN-workflow-graph G1).

Transient states = the action classes seen in training; absorbing states SUCCESS / FAIL /
ESCALATE / ABANDON. One task is one walk: START → a_0 → a_1 → … → a_{L−1} → absorbing state.

    P = [Q R; 0 I],  N = (I − Q)⁻¹,  t = N·1 (expected steps),  B = N·R (absorption odds),
    Var(steps) = (2N − I)·t − t∘t,  ρ(Q) = spectral radius (t diverges as ρ(Q) → 1).

From START the first state is drawn from π0, so E[steps] = π0·t and P(SUCCESS) = π0·B[:, SUCCESS].

**Outcome → absorbing state** (a choice — the label set has no "escalate"/"abandon"):
  success → SUCCESS;  fail → FAIL;
  partial → ESCALATE when the task handed off (any step spawned a subagent, or its last action
            is `delegate` / `ask`), else ABANDON (stopped part-way with nothing handed on);
  unknown → excluded. An unlabeled task has no absorbing transition; counting its transient
            transitions anyway would leave rows with less exit mass than the labeled walks show
            and inflate expected steps, so the whole task is left out.

Smoothing: α = 0.5 per transient cell, and α·4 per row on the absorbing cells split by the observed
outcome mix — every row keeps some exit mass, so I − Q is invertible whenever there is any data; the guard still checks ρ(Q) < 1 and reports
`singular` instead of a number. Per task type (or workflow), rows are shrunk to the pooled chain:
(c_type + β·P_pooled)/(n_type + β), β = 8, on the pooled state set.

The 2-state burst chain over call outcomes stays in `markov.py` (re-exported here as `burst`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .. import markov as burst  # noqa: F401 — the call-outcome chain; imported, never copied
from .protocol import ACTIONS, label, task_type

ABSORBING = ("SUCCESS", "FAIL", "ESCALATE", "ABANDON")
_A = len(ABSORBING)


def absorbing_state(task: dict, steps) -> str | None:
    o = task.get("outcome")
    if o == "success":
        return "SUCCESS"
    if o == "fail":
        return "FAIL"
    if o == "partial":
        handed = any(s.get("spawn") for s in steps) or (
            bool(steps) and steps[-1].get("act") in ("delegate", "ask"))
        return "ESCALATE" if handed else "ABANDON"
    return None


@dataclass
class Chain:
    states: tuple                    # transient action classes, ACTIONS order
    P: np.ndarray                    # (T, T + 4) rows: transient then absorbing
    pi0: np.ndarray                  # (T,) first-action distribution
    n_tasks: int = 0
    n_transitions: int = 0
    counts: np.ndarray | None = None
    lengths: tuple = ()              # observed walk lengths (for the misspecification check)
    n_mapped_other: int = 0          # steps whose class was outside `states` → counted as other
    n_dropped: int = 0               # … and with no `other` state to count them in
    _an: dict | None = field(default=None, repr=False)

    @property
    def Q(self):
        return self.P[:, :len(self.states)]

    @property
    def R(self):
        return self.P[:, len(self.states):]

    def index(self, act):
        try:
            return self.states.index(act)
        except ValueError:
            return None


def fit(walks, alpha: float = 0.5, prior: Chain | None = None, beta: float = 8.0) -> Chain | None:
    """`walks`: [(action names, absorbing state)]. With `prior`, shrink to it on its state set.

    A class outside §2 counts as `other` (as in protocol.act_id); with a prior, a class missing
    from the prior's state set also counts as `other` when that state exists — never silently
    dropped. Both are counted (`n_mapped_other`; `n_dropped` when there is no `other` state)."""
    walks = [(list(acts), z) for acts, z in walks if acts and z in ABSORBING]
    if prior is not None:
        states = prior.states
    else:
        seen = {a if a in ACTIONS else "other" for acts, _ in walks for a in acts}
        states = tuple(a for a in ACTIONS if a in seen)
    if not states:
        return None
    T = len(states)
    pos = {a: i for i, a in enumerate(states)}
    other = pos.get("other")
    mapped = dropped = 0
    pos_all = {}
    for acts, _ in walks:
        for a in acts:
            a2 = a if a in ACTIONS else "other"
            if a2 in pos:
                pos_all[a] = pos[a2]
                mapped += a2 != a
            elif other is not None:
                pos_all[a] = other
                mapped += 1
            else:
                pos_all[a] = None
                dropped += 1
    C = np.zeros((T, T + _A))
    c0 = np.zeros(T)
    n_tr = 0
    for acts, z in walks:
        idx = [pos_all.get(a) for a in acts]
        if idx[0] is not None:
            c0[idx[0]] += 1
        for x, y in zip(idx, idx[1:]):
            if x is not None and y is not None:
                C[x, y] += 1
                n_tr += 1
        if idx[-1] is not None:
            C[idx[-1], T + ABSORBING.index(z)] += 1
            n_tr += 1
    if prior is None:
        # α per transient cell; the absorbing cells share α·4 by the observed outcome mix (+α/4
        # each so an outcome never seen keeps a little), not α each: rare exits (ABANDON) are a
        # few walks, and a flat α per row and absorbing cell would hand them more P(absorb) than
        # the data shows (1.8% vs 0 observed on the 200-session synthetic set).
        mix = np.bincount([ABSORBING.index(z) for _, z in walks], minlength=_A) + 0.25
        prior_abs = alpha * _A * mix / mix.sum()
        pseudo = np.concatenate([np.full(T, alpha), prior_abs])
        P = (C + pseudo) / (C.sum(axis=1, keepdims=True) + pseudo.sum())
        pi0 = (c0 + alpha) / (c0.sum() + alpha * T)
    else:
        P = (C + beta * prior.P) / (C.sum(axis=1, keepdims=True) + beta)
        pi0 = (c0 + beta * prior.pi0) / (c0.sum() + beta)
    return Chain(states, P, pi0, len(walks), n_tr, C, tuple(len(a) for a, _ in walks),
                 mapped, dropped)


def walks_of(ds, ids) -> list:
    out = []
    for tid in ids:
        st = ds.steps[tid]
        z = absorbing_state(ds.tasks[tid], st)
        if z is not None and st:
            out.append(([s.get("act") for s in st], z))
    return out


def fit_tasks(ds, ids, alpha: float = 0.5, prior: Chain | None = None, beta: float = 8.0):
    return fit(walks_of(ds, ids), alpha=alpha, prior=prior, beta=beta)


def spectral_radius(Q) -> float:
    Q = np.asarray(Q, dtype=float)
    return float(np.max(np.abs(np.linalg.eigvals(Q)))) if Q.size else 0.0


def analyse(chain: Chain) -> dict:
    """Fundamental-matrix readout. `singular` (ρ(Q) ≥ 1 or I − Q not invertible) → no numbers."""
    if chain._an is not None:
        return chain._an
    Q, R = chain.Q, chain.R
    T = Q.shape[0]
    rho = spectral_radius(Q)
    out = {"states": list(chain.states), "rho": rho, "n_tasks": chain.n_tasks,
           "n_transitions": chain.n_transitions, "singular": False, "N": None, "t": None,
           "var": None, "B": None, "expected_steps": None, "var_steps": None,
           "p_absorb": None}
    try:
        if rho >= 1.0 - 1e-9:
            raise np.linalg.LinAlgError("rho(Q) >= 1")
        N = np.linalg.inv(np.eye(T) - Q)
        if not np.all(np.isfinite(N)) or np.linalg.cond(np.eye(T) - Q) > 1e12:
            raise np.linalg.LinAlgError("ill-conditioned")
    except np.linalg.LinAlgError:
        out["singular"] = True
        out["expected_steps"] = float("inf")
        chain._an = out
        return out
    t = N @ np.ones(T)
    var = (2 * N - np.eye(T)) @ t - t * t
    B = N @ R
    e = float(chain.pi0 @ t)
    out.update(N=N, t=t, var=var, B=B, expected_steps=e,
               var_steps=float(chain.pi0 @ (var + t * t) - e * e),
               p_absorb={z: float(chain.pi0 @ B[:, k]) for k, z in enumerate(ABSORBING)})
    chain._an = out
    return out


def p_success_from(chain: Chain | None, act) -> float | None:
    """P(absorb in SUCCESS | current state = act) — B[act, SUCCESS]. None if unknown/singular."""
    if chain is None:
        return None
    i = chain.index(act)
    an = analyse(chain)
    if i is None or an["B"] is None:
        return None
    return float(an["B"][i, 0])


def by_key(ds, ids, key=None, alpha: float = 0.5, beta: float = 8.0) -> dict:
    """{"pooled": Chain, key value: Chain shrunk to pooled} — keys with no labeled task are absent.
    `key(task, steps)` defaults to the task type (tasks without one are pooled only)."""
    key = key or (lambda t, s: task_type(t))
    pooled = fit_tasks(ds, ids, alpha=alpha)
    out = {"pooled": pooled}
    if pooled is None:
        return out
    groups = {}
    for tid in ids:
        k = key(ds.tasks[tid], ds.steps[tid])
        if k is not None:
            groups.setdefault(k, []).append(tid)
    for k, g in sorted(groups.items(), key=lambda kv: str(kv[0])):
        ch = fit_tasks(ds, g, prior=pooled, beta=beta)
        if ch is not None and ch.n_tasks:
            out[k] = ch
    return out


def table(chains: dict) -> list:
    """Rows for rendering: key, tasks, expected steps (sd), P(absorb) per state, ρ(Q)."""
    rows = []
    for k, ch in chains.items():
        if ch is None:
            continue
        an = analyse(ch)
        L = np.asarray(ch.lengths, dtype=float)
        rows.append({"key": k, "n_tasks": ch.n_tasks, "n_transitions": ch.n_transitions,
                     "observed_mean": float(L.mean()) if len(L) else None,
                     "observed_sd": float(L.std(ddof=1)) if len(L) > 1 else None,
                     "n_mapped_other": ch.n_mapped_other, "n_dropped": ch.n_dropped,
                     "expected_steps": an["expected_steps"],
                     "sd_steps": (None if an["var_steps"] is None
                                  else float(np.sqrt(max(an["var_steps"], 0.0)))),
                     "p_absorb": an["p_absorb"], "rho": an["rho"], "singular": an["singular"]})
    return rows


# ---- sliding-window ρ(Q) ------------------------------------------------------------------------

def window_rho(chain: Chain | None, steps, w: int = 20, beta: float = 5.0) -> np.ndarray:
    """ρ(Q) per step of a running task: rows of the states visited in the last `w` transitions are
    re-estimated as (window counts + β·pooled row)/(n + β); the window has no exits yet, so a loop
    (search ⇄ read, fix one test / break another) pushes its rows' exit mass towards 0 and ρ → 1.
    Unvisited rows keep the pooled estimate. NaN where the chain is missing."""
    L = len(steps)
    if chain is None:
        return np.full(L, np.nan)
    T = len(chain.states)
    idx = [chain.index(s.get("act")) for s in steps]
    out = np.empty(L)
    for i in range(L):
        lo = max(0, i - w)
        C = np.zeros((T, T))
        for x, y in zip(idx[lo:i + 1], idx[lo + 1:i + 1]):
            if x is not None and y is not None:
                C[x, y] += 1
        n = C.sum(axis=1)
        Q = chain.Q.copy()
        vis = n > 0
        if vis.any():
            Q[vis] = (C[vis] + beta * chain.Q[vis]) / (n[vis, None] + beta)
        out[i] = spectral_radius(Q)
    return out


# ---- workflow ranking (G1 criterion 3) ----------------------------------------------------------

def workflow_of(task: dict, steps) -> str:
    """The task's workflow template if recorded, else W2 (delegated) when a subagent was spawned,
    else W0 (solo). Coarse until E1/P1 log the template."""
    wf = task.get("workflow")
    if isinstance(wf, str) and wf:
        return wf
    return "W2" if any(s.get("spawn") for s in steps) else "W0"


def _group_key(ds, tid):
    return (task_type(ds.tasks[tid]) or "all", workflow_of(ds.tasks[tid], ds.steps[tid]))


def workflow_values(ds, ids, lam: float = 0.0, beta: float = 8.0, min_tasks: int = 5) -> dict:
    """value = P(SUCCESS) − λ·E[steps] per (task type, workflow) from START, chains shrunk to the
    pooled chain. Groups with < `min_tasks` labeled train tasks are left out."""
    pooled = fit_tasks(ds, ids)
    if pooled is None:
        return {}
    groups = {}
    for tid in ids:
        if label(ds.tasks[tid]) is not None:
            groups.setdefault(_group_key(ds, tid), []).append(tid)
    out = {}
    for g, tids in sorted(groups.items()):
        if len(tids) < min_tasks:
            continue
        an = analyse(fit_tasks(ds, tids, prior=pooled, beta=beta))
        if an["singular"]:
            continue
        ps = an["p_absorb"]["SUCCESS"]
        out[g] = {"value": ps - lam * an["expected_steps"], "p_success": ps,
                  "expected_steps": an["expected_steps"], "n": len(tids)}
    return out


def realized(ds, ids) -> dict:
    """(successes, labeled tasks) per (task type, workflow)."""
    out = {}
    for tid in ids:
        y = label(ds.tasks[tid])
        if y is None:
            continue
        k, n = out.get(_group_key(ds, tid), (0, 0))
        out[_group_key(ds, tid)] = (k + y, n + 1)
    return out


def ranking_accuracy(values: dict, real: dict, min_n: int = 5) -> dict:
    """Pairwise concordance of a model's workflow ranking with the realized success rates, within
    each task type: over pairs of workflows both with ≥ `min_n` held-out tasks and different
    realized rates, the share the model orders the same way (a tie in value counts ½). Any model
    that gives a value per (type, workflow) — the chain here, the JEPA value head in E3 — is
    scored the same way, which is what G1 criterion 3 compares."""
    keys = [k for k in values if k in real and real[k][1] >= min_n]
    pairs, score = [], 0.0
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if a[0] != b[0]:
                continue
            ra, rb = real[a][0] / real[a][1], real[b][0] / real[b][1]
            if ra == rb:
                continue
            va, vb = values[a]["value"], values[b]["value"]
            s = 0.5 if va == vb else 1.0 if (va > vb) == (ra > rb) else 0.0
            score += s
            pairs.append({"a": a, "b": b, "realized": (ra, rb), "value": (va, vb), "score": s})
    return {"accuracy": score / len(pairs) if pairs else None, "n_pairs": len(pairs),
            "pairs": pairs}
