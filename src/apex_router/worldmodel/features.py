"""P6 world model (E3): contract step records -> fixed-width feature vectors and sequence windows.

Pure numpy. Consumes the step schema of ``docs/DESIGN-worldmodel-P6.md`` §1 (one record per tool
call: ``task, i, act, phase, in_b, out_b, dt, err, tests{ran, failed, passed}, spawn``). Every
reader is fail-open: a missing or malformed field becomes its neutral value, never an exception.
No text is read or produced — only classes, buckets, flags and counts.

Layout of one step vector (``FEATURE_NAMES`` lists every column):

=====================  =====  ==============================================================
block                  width  meaning
=====================  =====  ==============================================================
act                    13     one-hot over ``ACTIONS``
phase                  5      one-hot over ``PHASES``
in_b / out_b           4 + 4  one-hot size buckets (0:<1k 1:<10k 2:<100k 3:>=100k chars)
dt                     6      one-hot: unknown, <2 s, <10 s, <60 s, <300 s, >=300 s
err                    1      tool returned an error
tests                  3      ran, any failing, normalised failing share
spawn                  1      step spawned a subagent
so_far                 13     task-so-far class counts (inclusive of this step) / (i + 1)
step index             7      one-hot bucket: 0, 1, 2-3, 4-7, 8-15, 16-31, >=32
run length             4      one-hot bucket of steps merged into this one: 1, 2, 3-4, >=5
=====================  =====  ==============================================================

The prediction target of position t is the action class of step t + 1 (``-1`` on the last step
of a task: there is no successor, and the position is masked out of the next-action loss). The
first action of a task is therefore never predicted from the model; E4 scores it with the
START prior (train first actions, ``start_prior``) for every model alike (``train.WorldModel.action_logprobs``).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

ACTIONS = ("search", "read", "edit", "write", "run", "test", "build", "vcs", "remote", "plan",
           "delegate", "ask", "other")
PHASES = ("explore", "edit", "verify", "deliver", "other")
ACT_INDEX = {a: i for i, a in enumerate(ACTIONS)}
PHASE_INDEX = {p: i for i, p in enumerate(PHASES)}
N_ACT, N_PHASE = len(ACTIONS), len(PHASES)
N_SIZE_B, N_DT_B, N_STEP_B, N_RUN_B = 4, 6, 7, 4
# Failing-tests bucket (a probe target): 0 no tests ran, 1 ran + 0 failing, 2 one failing,
# 3 two to five failing, 4 more than five failing.
N_FAIL_B = 5

FEATURE_NAMES: tuple[str, ...] = (
    tuple(f"act={a}" for a in ACTIONS)
    + tuple(f"phase={p}" for p in PHASES)
    + tuple(f"in_b={b}" for b in range(N_SIZE_B))
    + tuple(f"out_b={b}" for b in range(N_SIZE_B))
    + ("dt=unknown", "dt<2s", "dt<10s", "dt<60s", "dt<300s", "dt>=300s")
    + ("err", "tests.ran", "tests.any_failing", "tests.failing_share", "spawn")
    + tuple(f"so_far={a}" for a in ACTIONS)
    + ("i=0", "i=1", "i<4", "i<8", "i<16", "i<32", "i>=32")
    + ("run=1", "run=2", "run<5", "run>=5")
)
N_FEAT = len(FEATURE_NAMES)

_OFF_ACT = 0
_OFF_PHASE = _OFF_ACT + N_ACT
_OFF_IN = _OFF_PHASE + N_PHASE
_OFF_OUT = _OFF_IN + N_SIZE_B
_OFF_DT = _OFF_OUT + N_SIZE_B
_OFF_FLAGS = _OFF_DT + N_DT_B          # err, ran, any_failing, failing_share, spawn
_OFF_SOFAR = _OFF_FLAGS + 5
_OFF_STEP = _OFF_SOFAR + N_ACT
_OFF_RUN = _OFF_STEP + N_STEP_B
assert _OFF_RUN + N_RUN_B == N_FEAT


# ---- fail-open field readers -------------------------------------------------------------------

def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def act_index(rec: dict) -> int:
    return ACT_INDEX.get(rec.get("act") if isinstance(rec, dict) else None, ACT_INDEX["other"])


def phase_index(rec: dict) -> int:
    return PHASE_INDEX.get(rec.get("phase") if isinstance(rec, dict) else None, PHASE_INDEX["other"])


def _bucket(v, n: int) -> int:
    return min(max(_int(v), 0), n - 1)


def dt_bucket(dt) -> int:
    try:
        x = float(dt)
    except (TypeError, ValueError):
        return 0
    if x != x or x < 0:          # NaN or negative clock skew -> unknown
        return 0
    for b, edge in enumerate((2.0, 10.0, 60.0, 300.0), start=1):
        if x < edge:
            return b
    return 5


def step_bucket(i: int) -> int:
    i = max(_int(i), 0)
    if i <= 1:
        return i
    for b, edge in enumerate((4, 8, 16, 32), start=2):
        if i < edge:
            return b
    return 6


def run_bucket(n: int) -> int:
    n = max(_int(n, 1), 1)
    return 0 if n == 1 else 1 if n == 2 else 2 if n < 5 else 3


def _tests(rec: dict) -> tuple[int, int | None, int | None]:
    t = rec.get("tests") if isinstance(rec.get("tests"), dict) else {}
    ran = 1 if _int(t.get("ran")) else 0
    failed = t.get("failed")
    passed = t.get("passed")
    failed = _int(failed) if failed is not None else None
    passed = _int(passed) if passed is not None else None
    return ran, failed, passed


def fail_bucket(rec: dict) -> int:
    ran, failed, _ = _tests(rec)
    if not ran:
        return 0
    f = failed or 0
    return 1 if f <= 0 else 2 if f == 1 else 3 if f <= 5 else 4


def _failing_share(failed: int | None, passed: int | None) -> float:
    if failed is None:
        return 0.0
    if passed is not None and failed + passed > 0:
        return max(0.0, min(1.0, failed / (failed + passed)))
    return 1.0 if failed > 0 else 0.0


def causal_phases(acts: list[str]) -> list[str]:
    """Contract §2 phase, CAUSAL (steps <= t only; appending steps never changes earlier ones):
    ``edit`` this step is edit/write; ``explore`` search/read before the first edit so far;
    ``verify`` test/build/run after an edit so far; ``deliver`` this step is vcs/ask; else
    ``other``. A reference for the synthetic generator — E1's ``actions.py`` owns the real one."""
    out, edited = [], False
    for a in acts:
        if a in ("edit", "write"):
            out.append("edit")
            edited = True
        elif a in ("search", "read") and not edited:
            out.append("explore")
        elif a in ("test", "build", "run") and edited:
            out.append("verify")
        elif a in ("vcs", "ask"):
            out.append("deliver")
        else:
            out.append("other")
    return out


# ---- per-step features ---------------------------------------------------------------------------

def task_features(steps: list[dict]) -> np.ndarray:
    """Feature matrix ``(n, N_FEAT)`` float32 for one task's steps (already in step order).

    The task-so-far counts and the step index come from the position in ``steps`` (not the
    record's ``i``), so the same function serves raw and macro-merged sequences.
    """
    n = len(steps)
    X = np.zeros((n, N_FEAT), dtype=np.float32)
    counts = np.zeros(N_ACT, dtype=np.float32)
    for t, rec in enumerate(steps):
        a = act_index(rec)
        X[t, _OFF_ACT + a] = 1.0
        X[t, _OFF_PHASE + phase_index(rec)] = 1.0
        X[t, _OFF_IN + _bucket(rec.get("in_b"), N_SIZE_B)] = 1.0
        X[t, _OFF_OUT + _bucket(rec.get("out_b"), N_SIZE_B)] = 1.0
        X[t, _OFF_DT + dt_bucket(rec.get("dt"))] = 1.0
        ran, failed, passed = _tests(rec)
        X[t, _OFF_FLAGS + 0] = 1.0 if _int(rec.get("err")) else 0.0
        X[t, _OFF_FLAGS + 1] = float(ran)
        X[t, _OFF_FLAGS + 2] = 1.0 if ran and (failed or 0) > 0 else 0.0
        X[t, _OFF_FLAGS + 3] = _failing_share(failed, passed) if ran else 0.0
        X[t, _OFF_FLAGS + 4] = 1.0 if _int(rec.get("spawn")) else 0.0
        counts[a] += 1.0
        X[t, _OFF_SOFAR:_OFF_SOFAR + N_ACT] = counts / (t + 1)
        X[t, _OFF_STEP + step_bucket(t)] = 1.0
        X[t, _OFF_RUN + run_bucket(rec.get("_run", 1))] = 1.0
    return X


def next_action_targets(steps: list[dict]) -> np.ndarray:
    """``(n,)`` int: the action index of step t + 1, ``-1`` on the last step."""
    acts = np.array([act_index(r) for r in steps], dtype=np.int64)
    y = np.full(len(steps), -1, dtype=np.int64)
    if len(steps) > 1:
        y[:-1] = acts[1:]
    return y


# ---- macro-steps ---------------------------------------------------------------------------------

def _merge(group: list[dict]) -> dict:
    """One macro-step from a run of consecutive steps of a single class."""
    by_act: dict[int, int] = {}
    for r in group:
        by_act[act_index(r)] = by_act.get(act_index(r), 0) + _int(r.get("_run", 1), 1)
    act = ACTIONS[max(by_act, key=lambda k: (by_act[k], -k))]
    phases = [phase_index(r) for r in group]
    phase = PHASES[max(set(phases), key=lambda p: (phases.count(p), -p))]
    last_tests = next((r.get("tests") for r in reversed(group)
                       if isinstance(r.get("tests"), dict) and _int(r["tests"].get("ran"))), None)
    dts = [r.get("dt") for r in group]
    dt0 = dts[0]
    try:
        dt = (float(dt0) if dt0 is not None else None)
        if dt is not None:
            dt += sum(float(d) for d in dts[1:] if d is not None)
    except (TypeError, ValueError):
        dt = None
    return {
        "task": group[0].get("task"), "sid": group[0].get("sid"), "i": group[0].get("i"),
        "act": act, "phase": phase,
        "in_b": max(_bucket(r.get("in_b"), N_SIZE_B) for r in group),
        "out_b": max(_bucket(r.get("out_b"), N_SIZE_B) for r in group),
        "dt": dt,
        "err": max(1 if _int(r.get("err")) else 0 for r in group),
        "tests": last_tests if last_tests is not None else {"ran": 0, "failed": None, "passed": None},
        "spawn": max(1 if _int(r.get("spawn")) else 0 for r in group),
        "_run": sum(_int(r.get("_run", 1), 1) for r in group),
    }


def macro_steps(steps: list[dict]) -> list[dict]:
    """Run-length merge of consecutive same-class steps into macro-steps (no global cap).

    A run of k steps of one action class becomes one macro-step (``_run = k``; err/spawn = any,
    buckets = max, dt = sum, tests = the run's last parsed test result, phase = majority).

    Causality: appending steps can only extend or follow the *last* run, so every macro-step but
    the last is final — but a macro-step is only finalised when its run ends, i.e. one step of
    look-ahead. Macro mode is therefore for OFFLINE evaluation only (whole tasks), never for an
    online per-step readout. (An earlier global cap that merged the minimum adjacent pair was
    removed: it let later steps rewrite macro-step 0.)
    """
    if not steps:
        return []
    runs: list[list[dict]] = [[steps[0]]]
    for r in steps[1:]:
        if act_index(r) == act_index(runs[-1][-1]):
            runs[-1].append(r)
        else:
            runs.append([r])
    return [_merge(g) for g in runs]


# ---- windows -------------------------------------------------------------------------------------

@dataclass
class Windows:
    """Fixed-length windows over tasks (right-padded; ``mask`` marks real positions).

    ``X (W, L, F)`` features; ``act (W, L)`` action at each position; ``y_next (W, L)`` next-action
    target (-1 = none); ``phase``/``fail_b (W, L)`` probe targets; ``outcome (W,)`` 1 success /
    0 not, with ``has_label (W,)``; ``task_idx (W,)`` index into ``task_ids``; ``start (W,)`` the
    position in the task's (possibly macro-merged) sequence of window slot 0.

    Windows overlap (stride ``L // 2``). Training uses every real position (``mask``); scoring
    uses ``own``: each step is owned by exactly one window, the one where it has at least
    ``L // 2`` steps of context (the task's first ``L // 2`` steps are owned by the first window).
    """
    X: np.ndarray
    mask: np.ndarray
    act: np.ndarray
    y_next: np.ndarray
    phase: np.ndarray
    fail_b: np.ndarray
    outcome: np.ndarray
    has_label: np.ndarray
    task_idx: np.ndarray
    start: np.ndarray
    task_ids: list[str]
    own: np.ndarray | None = None

    def __len__(self) -> int:
        return int(self.X.shape[0])

    def subset(self, idx) -> "Windows":
        idx = np.asarray(idx)
        return Windows(self.X[idx], self.mask[idx], self.act[idx], self.y_next[idx],
                       self.phase[idx], self.fail_b[idx], self.outcome[idx], self.has_label[idx],
                       self.task_idx[idx], self.start[idx], self.task_ids,
                       None if self.own is None else self.own[idx])


def outcome_target(outcome) -> tuple[float, bool]:
    """``success`` -> (1, labelled); ``partial``/``fail`` -> (0, labelled); else unlabelled."""
    if outcome == "success":
        return 1.0, True
    if outcome in ("partial", "fail"):
        return 0.0, True
    return 0.0, False


def window_starts(n: int, L: int, stride: int) -> list[int]:
    """Start positions of the windows over a sequence of ``n`` steps: 0, stride, 2·stride, … as
    long as the window adds steps the previous one did not cover."""
    starts = [0]
    while starts[-1] + L < n:
        starts.append(starts[-1] + stride)
    return starts


def owner_start(p: int, L: int, stride: int) -> int:
    """Start of the window that owns step ``p`` for scoring: the latest start with
    ``p - start >= L - stride`` (context), or 0 for the first ``L - stride`` steps."""
    return max(0, (p - (L - stride)) // stride * stride)


def build_windows(tasks: dict[str, list[dict]], outcomes: dict[str, str] | None = None,
                  L: int = 32, macro: bool = False, extra: dict[str, np.ndarray] | None = None,
                  stride: int | None = None) -> Windows:
    """Cut every task into overlapping windows of ``L`` positions (default stride ``L // 2``).

    ``tasks`` maps task id -> its step records (sorted here by ``i``; non-dict records are
    skipped). A step at position p >= L/2 is scored from a window where it has >= L/2 steps of
    in-window context (``Windows.own``), so no prediction sits right after a hard cut; longer
    range survives through the task-so-far counts and the step-index bucket. ``stride = L``
    reproduces non-overlapping cuts. ``extra`` optionally maps task id -> a per-task vector (e.g.
    the frozen request embedding) appended to every position of that task.
    """
    outcomes = outcomes or {}
    stride = stride or max(1, L // 2)
    if L % stride:
        raise ValueError("L must be a multiple of stride")
    ids = sorted(tasks)
    extra_dim = 0
    if extra:
        extra_dim = len(next(iter(extra.values())))
    F = N_FEAT + extra_dim
    rows = []
    for ti, tid in enumerate(ids):
        steps = sorted((s for s in (tasks[tid] or []) if isinstance(s, dict)),
                       key=lambda s: _int(s.get("i")))
        if not steps:
            continue
        if macro:
            steps = macro_steps(steps)
        n = len(steps)
        X = task_features(steps)
        if extra_dim:
            ev = np.asarray(extra.get(tid, np.zeros(extra_dim)), dtype=np.float32).reshape(1, -1)
            X = np.concatenate([X, np.repeat(ev, n, axis=0)], axis=1)
        acts = np.array([act_index(s) for s in steps], dtype=np.int64)
        y = next_action_targets(steps)
        ph = np.array([phase_index(s) for s in steps], dtype=np.int64)
        fb = np.array([fail_bucket(s) for s in steps], dtype=np.int64)
        owner = np.array([owner_start(p, L, stride) for p in range(n)])
        v, lab = outcome_target(outcomes.get(tid))
        for s0 in window_starts(n, L, stride):
            sl = slice(s0, s0 + L)
            rows.append((ti, s0, X[sl], acts[sl], y[sl], ph[sl], fb[sl], owner[sl] == s0, v, lab))
    W = len(rows)
    out = Windows(
        X=np.zeros((W, L, F), np.float32), mask=np.zeros((W, L), np.float32),
        act=np.zeros((W, L), np.int64), y_next=np.full((W, L), -1, np.int64),
        phase=np.zeros((W, L), np.int64), fail_b=np.zeros((W, L), np.int64),
        outcome=np.zeros(W, np.float32), has_label=np.zeros(W, np.float32),
        task_idx=np.zeros(W, np.int64), start=np.zeros(W, np.int64), task_ids=ids,
        own=np.zeros((W, L), np.float32))
    for w, (ti, s0, X, a, y, ph, fb, own, v, lab) in enumerate(rows):
        n = len(a)
        out.X[w, :n] = X
        out.mask[w, :n] = 1.0
        out.own[w, :n] = own
        out.act[w, :n] = a
        out.y_next[w, :n] = y
        out.phase[w, :n] = ph
        out.fail_b[w, :n] = fb
        out.outcome[w] = v
        out.has_label[w] = 1.0 if lab else 0.0
        out.task_idx[w] = ti
        out.start[w] = s0
    return out


def start_prior(tasks: dict[str, list[dict]], alpha: float = 0.5, macro: bool = False) -> np.ndarray:
    """Add-alpha distribution of each task's FIRST action (step 0 is scored from this, for every
    model alike — E2's Markov chains score step 0 from their START context)."""
    c = np.zeros(N_ACT)
    for steps in tasks.values():
        st = sorted((s for s in (steps or []) if isinstance(s, dict)), key=lambda s: _int(s.get("i")))
        if st:
            c[act_index(st[0])] += 1
    return (c + alpha) / (c.sum() + alpha * N_ACT)


def group_steps(records) -> dict[str, list[dict]]:
    """Group step records by ``task`` (records without a task id are dropped)."""
    out: dict[str, list[dict]] = {}
    for r in records:
        if isinstance(r, dict) and isinstance(r.get("task"), str):
            out.setdefault(r["task"], []).append(r)
    return out


def unigram_ce(y_train: np.ndarray, y_eval: np.ndarray, alpha: float = 0.5) -> float:
    """Cross-entropy (nats/step) of the add-alpha unigram prior fit on ``y_train`` over ``y_eval``
    (entries < 0 ignored) — the floor a next-action model must beat."""
    yt = y_train[y_train >= 0]
    ye = y_eval[y_eval >= 0]
    if ye.size == 0:
        return float("nan")
    p = (np.bincount(yt, minlength=N_ACT) + alpha) / (yt.size + alpha * N_ACT)
    return float(-np.log(p[ye]).mean())
