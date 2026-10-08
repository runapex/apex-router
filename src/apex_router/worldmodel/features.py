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

Attempt-2 ablations (``docs/research/2026-10-08-p6-attempt-2-predeclaration.md``), appended
after the attempt-1 block and selected by config so attempt 1 stays reproducible:

- ``features="a2f"`` (A2-F) adds the cross-step state ``baselines.prefix_features`` carries, per
  sequence (stream) and causal (steps <= t): f_ref-normalised test progress 1 − failing/f_ref
  (f_ref = the first non-zero failing count, carried forward) and whether it is defined, the
  cumulative error share, tests-ran-so-far (a parsed test result seen), spawned-so-far (any
  subagent so far, 0/1, as the baseline) and log1p(position). ``A2F_NAMES``.
- ``regime=True`` (A2-D) adds the day regime read off the ``_regime`` annotation
  (``annotate_regime``): log1p(errors in the session over the last 15 min), the session-day error
  rate so far. ``REGIME_NAMES``. The same two numbers feed the logistic baseline.

The prediction target of position t is the action class of step t + 1 (``-1`` on the last step
of a task: there is no successor, and the position is masked out of the next-action loss). The
first action of a task is therefore never predicted from the model; E4 scores it with the
START prior (train first actions, ``start_prior``) for every model alike (``train.WorldModel.action_logprobs``).
"""
from __future__ import annotations

import math
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

# A2-F: cross-step task state (attempt 2). A2-D: day regime (attempt 2).
A2F_NAMES: tuple[str, ...] = ("a2f.progress", "a2f.progress_defined", "a2f.err_share",
                              "a2f.tests_ran", "a2f.spawned", "a2f.log1p_pos")
REGIME_NAMES: tuple[str, ...] = ("regime.err15m_log1p", "regime.day_err_rate")
FEATURE_SETS = ("a1", "a2f")
REGIME_KEY = "_regime"           # step annotation written by ``annotate_regime``
REGIME_WINDOW_S = 900.0          # "the session's last 15 min"


def check_feature_set(features: str) -> str:
    if features not in FEATURE_SETS:
        raise ValueError(f"features must be one of {FEATURE_SETS}, not {features!r}")
    return features


def feature_names(features: str = "a1", regime: bool = False) -> tuple[str, ...]:
    check_feature_set(features)
    return (FEATURE_NAMES + (A2F_NAMES if features == "a2f" else ())
            + (REGIME_NAMES if regime else ()))


def n_feat(features: str = "a1", regime: bool = False) -> int:
    return len(feature_names(features, regime))

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

def task_features(steps: list[dict], features: str = "a1", regime: bool = False) -> np.ndarray:
    """Feature matrix ``(n, n_feat(features, regime))`` float32 for one task's steps (already in
    step order). ``features="a1"`` with ``regime=False`` is attempt 1's ``(n, N_FEAT)``.

    The task-so-far counts and the step index come from the position in ``steps`` (not the
    record's ``i``), so the same function serves raw and macro-merged sequences.
    """
    n = len(steps)
    X = np.zeros((n, n_feat(features, regime)), dtype=np.float32)
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
    o = N_FEAT
    if features == "a2f":
        X[:, o:o + len(A2F_NAMES)] = a2f_features(steps)
        o += len(A2F_NAMES)
    if regime:
        X[:, o:o + len(REGIME_NAMES)] = regime_features(steps)
    return X


def a2f_features(steps: list[dict]) -> np.ndarray:
    """A2-F block ``(n, 6)``: row t is the state after steps 0..t of this sequence (inclusive, as
    every other column of the step vector), the same quantities ``baselines.prefix_features``
    computes (its row t + 1), by the same rules:

    progress = 1 − failed/f_ref once f_ref (the first non-zero ``failed`` of a parsed test result)
    exists, carried forward between test runs; progress_defined = 1 from then on; err_share =
    errors so far / (t + 1); tests_ran = 1 once a parsed test result (``ran`` and an int
    ``failed``) was seen; spawned = 1 once any step so far spawned a subagent; log1p(t), t = the
    0-based position in the sequence. Causal: row t reads steps <= t only."""
    n = len(steps)
    out = np.zeros((n, len(A2F_NAMES)), dtype=np.float32)
    errs = spawned = ran = 0
    f_ref = None
    prog, has_prog = 0.0, 0
    for t, rec in enumerate(steps):
        errs += 1 if _int(rec.get("err")) else 0
        spawned = max(spawned, 1 if _int(rec.get("spawn")) else 0)
        tr = rec.get("tests") if isinstance(rec.get("tests"), dict) else {}
        f = tr.get("failed")
        if _int(tr.get("ran")) and isinstance(f, int) and not isinstance(f, bool):
            ran = 1
            if f_ref is None and f > 0:
                f_ref = f
            if f_ref:
                prog, has_prog = 1.0 - f / f_ref, 1
        out[t] = (prog, has_prog, errs / (t + 1), ran, spawned, math.log1p(t))
    return out


def regime_features(steps: list[dict]) -> np.ndarray:
    """A2-D block ``(n, 2)`` from each step's ``_regime`` annotation (``annotate_regime``):
    log1p(session errors in the last 15 min), session-day error rate so far. A step without the
    annotation reads 0, 0 (fail-open; ``train.WorldModel`` refuses unannotated input instead)."""
    out = np.zeros((len(steps), len(REGIME_NAMES)), dtype=np.float32)
    for t, rec in enumerate(steps):
        out[t] = regime_values(rec)
    return out


def regime_values(rec) -> tuple[float, float]:
    """(log1p(errors in the last 15 min), session-day error rate) of one annotated step; (0, 0)
    when the annotation is missing or malformed. Shared by the JEPA and the logistic baseline."""
    r = rec.get(REGIME_KEY) if isinstance(rec, dict) else None
    if isinstance(r, (list, tuple)) and len(r) == 2:
        try:
            return math.log1p(max(float(r[0]), 0.0)), float(r[1])
        except (TypeError, ValueError):
            pass
    return 0.0, 0.0


def has_regime(rec) -> bool:
    return isinstance(rec, dict) and REGIME_KEY in rec


# ---- A2-D: the day regime (causal, telemetry-free) -------------------------------------------------

def _num_ts(rec) -> float | None:
    v = rec.get("ts") if isinstance(rec, dict) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return float(v)


def annotate_regime(seqs: dict[str, list[dict]], sid_of: dict[str, str] | None = None,
                    window_s: float = REGIME_WINDOW_S) -> int:
    """Write ``rec["_regime"] = [errors_15m, day_rate]`` on every step of ``seqs`` (sequence id ->
    steps in sequence order: the stream view's streams, or whole tasks in the task view).

    Telemetry-free: only the steps' own ``err`` flags and ``ts``. For step p of sequence k at
    time t, in session S (``sid_of[k]``, else the steps' ``sid``, else k itself):

    - errors_15m = errors of k's steps at positions <= p (itself included) with ts in
      [t − 15 min, t], plus errors of the OTHER sequences of S (sibling streams, the session's
      other tasks) with ts in [t − 15 min, t) — strictly earlier, so a sibling step stamped at
      the same instant is never read;
    - day_rate = errors / steps over the same two sets restricted to t's UTC day (00:00 UTC to
      t): the session's error rate so far today; >= 1 step (itself) by construction.

    Causal: appending steps (later in time, or later in a sequence) never changes an earlier
    value. A step without a numeric ``ts`` gets [0, 0] and is not counted for others.
    Idempotent; returns the number of steps annotated."""
    import bisect
    by_sess: dict[str, list[str]] = {}
    for k, st in seqs.items():
        sid = (sid_of or {}).get(k)
        if sid is None:
            sid = next((r.get("sid") for r in (st or [])
                        if isinstance(r, dict) and r.get("sid") is not None), None)
        by_sess.setdefault(str(sid if sid is not None else k), []).append(k)

    def window(tl, cum, lo, hi):
        """(errors, steps) among the sorted times ``tl`` in [lo, hi)."""
        a, b = bisect.bisect_left(tl, lo), bisect.bisect_left(tl, hi)
        return float(cum[b] - cum[a]), b - a

    def sorted_cum(rows):
        rows = sorted(rows)
        return [x[0] for x in rows], np.concatenate([[0.0], np.cumsum([x[1] for x in rows])])

    n_done = 0
    for keys in by_sess.values():
        own_rows = {k: [(_num_ts(r), 1 if _int(r.get("err")) else 0) for r in (seqs[k] or [])
                        if isinstance(r, dict) and _num_ts(r) is not None] for k in keys}
        T, ce = sorted_cum([x for k in keys for x in own_rows[k]])
        for k in keys:
            ot, oc = sorted_cum(own_rows[k])
            mono = all(a[0] <= b[0] for a, b in zip(own_rows[k], own_rows[k][1:]))
            seen_t: list[float] = []                    # own steps at positions <= p
            seen_e: list[int] = []
            seen_c: list[float] = [0.0]
            for r in seqs[k] or []:
                if not isinstance(r, dict):
                    continue
                n_done += 1
                t = _num_ts(r)
                if t is None:
                    r[REGIME_KEY] = [0.0, 0.0]
                    continue
                ee = 1 if _int(r.get("err")) else 0
                seen_t.append(t)
                seen_e.append(ee)
                seen_c.append(seen_c[-1] + ee)
                day0 = math.floor(t / 86400.0) * 86400.0
                vals = []
                for lo in (t - window_s, day0):
                    e_all, n_all = window(T, ce, lo, t)
                    e_own, n_own = window(ot, oc, lo, t)
                    e, n = e_all - e_own, n_all - n_own           # siblings, strictly earlier
                    if mono:                                     # own times sorted: bisect
                        a = bisect.bisect_left(seen_t, lo)
                        e += seen_c[-1] - seen_c[a]
                        n += len(seen_t) - a
                    else:
                        for tt, x in zip(seen_t, seen_e):
                            if lo <= tt <= t:
                                e += x
                                n += 1
                    vals.append((e, n))
                (e15, _), (ed, nd) = vals
                r[REGIME_KEY] = [float(e15), float(ed / nd) if nd else 0.0]
    return n_done


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
        # A2-D: a macro-step carries the regime as of its run's last step
        **({REGIME_KEY: group[-1][REGIME_KEY]} if REGIME_KEY in group[-1] else {}),
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
                  stride: int | None = None, features: str = "a1",
                  regime: bool = False) -> Windows:
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
    F = n_feat(features, regime) + extra_dim
    rows = []
    for ti, tid in enumerate(ids):
        steps = sorted((s for s in (tasks[tid] or []) if isinstance(s, dict)),
                       key=lambda s: _int(s.get("i")))
        if not steps:
            continue
        if macro:
            steps = macro_steps(steps)
        n = len(steps)
        X = task_features(steps, features, regime)
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
