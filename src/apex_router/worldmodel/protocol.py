"""P6 evaluation protocol (DESIGN-worldmodel-P6.md §3) — shared by every track.

What it fixes, so E2's baselines, E3's JEPA and E4's scorecard score the same thing:

- **Split** by session start time, exactly as `tasks.jsonl` gives it (`split ∈ train|val|test`).
  A step whose task has no `tasks.jsonl` row inherits its session's split; with no session row it
  is dropped (counted in `Dataset.dropped`). Tuning reads train/val only; test is scored once.
- **Next-action cross-entropy** (nats/step) and perplexity = exp(CE). Every step of a task is
  scored, the first one against the START context: −log P(act_i | steps_<i). Probabilities are
  clipped at 1e-12, so one confident miss is a large penalty, not −inf. Vocabulary: the 13 classes
  of §2 (an unknown class counts as `other`).
- **Outcome Brier** on tasks: P(success) vs 1[outcome == success]; `partial` and `fail` are 0,
  `unknown` is left out. The probe reads the task's first `prefix` steps, never its last step
  (min(k, L − 1); default k = OUTCOME_PREFIX = 8): read at the end of the task, the last test run
  all but states the label (a leak, not a forecast). `prefix=None` gives that end-of-task readout,
  reported only as a ceiling. **ECE**: 10 equal-width bins, Σ_b (n_b/N)·|mean p_b − mean y_b|.
- **Session-cluster bootstrap**: resample sessions with replacement (1,000 draws, seeded), keep
  each session's steps/tasks together, 95% percentile CI. Paired differences resample the same
  sessions for both models.
- **Label quality** on every outcome number: `gold` (every scored label is gold), `provisional`
  (any weak label among them), `inconclusive` (no labels to score). Label-free metrics (CE) say
  `label-free`. G1 criteria 2 and 5 are scored on gold only (`gold_only=True`).

Every `Score` carries its n (steps or tasks), the number of sessions, and the CI. Pure numpy.
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

ACTIONS = ("search", "read", "edit", "write", "run", "test", "build", "vcs", "remote", "plan",
           "delegate", "ask", "other")
A_INDEX = {a: i for i, a in enumerate(ACTIONS)}
V = len(ACTIONS)
PHASES = ("explore", "edit", "verify", "deliver", "other")
SPLITS = ("train", "val", "test")
EPS = 1e-12
N_BOOT = 1000
OUTCOME_PREFIX = 8


def act_id(step: dict) -> int:
    return A_INDEX.get(step.get("act"), A_INDEX["other"])


def label(task: dict):
    """1 = success, 0 = partial/fail, None = unknown or missing."""
    o = task.get("outcome")
    return 1 if o == "success" else 0 if o in ("partial", "fail") else None


def task_type(task: dict):
    """Task type if the task row carries one (`task_type` or `type`); None → pooled models only."""
    t = task.get("task_type") or task.get("type")
    return t if isinstance(t, str) and t else None


def day_of(ts) -> str | None:
    """UTC calendar day of an epoch timestamp."""
    if not isinstance(ts, (int, float)):
        return None
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


# ---- data --------------------------------------------------------------------------------------

def read_jsonl(path):
    """Dict rows of a JSONL file; malformed lines and a missing file are skipped (fail open)."""
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        pass
    return out


def data_dir() -> Path:
    return Path(os.environ.get("APEX_ROUTER_HOME", Path.home() / ".apex-router")) / "worldmodel"


@dataclass
class Dataset:
    tasks: dict                                  # task id -> task row (always has 'split')
    steps: dict                                  # task id -> [step rows] sorted by i
    dropped: int = 0                             # steps without a usable split

    def ids(self, split=None, labeled: bool = False, gold_only: bool = False) -> list:
        """Task ids in time order (t0, then id), optionally one split / labeled / gold only."""
        out = []
        for tid, t in self.tasks.items():
            if split is not None and t.get("split") != split:
                continue
            if labeled and label(t) is None:
                continue
            if gold_only and t.get("outcome_src") != "gold":
                continue
            out.append(tid)
        return sorted(out, key=lambda k: (self.tasks[k].get("t0") or 0.0, k))

    def sid(self, tid) -> str:
        return str(self.tasks[tid].get("sid"))


def build(step_rows, task_rows) -> Dataset:
    """Group steps by task (sorted by i) and attach each task's split (§1)."""
    tasks = {}
    for t in task_rows:
        tid = t.get("task")
        if tid is None or t.get("split") not in SPLITS:
            continue
        tasks[str(tid)] = dict(t, task=str(tid))
    sess_split = {}
    for t in tasks.values():
        sess_split.setdefault(str(t.get("sid")), t["split"])
    by = defaultdict(list)
    dropped = 0
    for s in step_rows:
        tid = s.get("task")
        if tid is None or not isinstance(s.get("i"), int):
            dropped += 1
            continue
        tid = str(tid)
        if tid not in tasks:
            sp = sess_split.get(str(s.get("sid")))
            if sp is None:
                dropped += 1
                continue
            tasks[tid] = {"task": tid, "sid": s.get("sid"), "split": sp, "outcome": "unknown",
                          "outcome_src": "none", "t0": s.get("ts"), "inferred": True}
        by[tid].append(s)
    steps = {tid: sorted(v, key=lambda s: s["i"]) for tid, v in by.items()}
    tasks = {tid: t for tid, t in tasks.items() if steps.get(tid)}
    for tid, t in tasks.items():
        if t.get("t0") is None:
            t["t0"] = steps[tid][0].get("ts")
    return Dataset(tasks=tasks, steps=steps, dropped=dropped)


def load(steps_path=None, tasks_path=None) -> Dataset:
    d = data_dir()
    return build(read_jsonl(steps_path or d / "steps.jsonl"),
                 read_jsonl(tasks_path or d / "tasks.jsonl"))


# ---- scores + bootstrap ------------------------------------------------------------------------

@dataclass
class Score:
    metric: str
    model: str
    split: str
    value: float | None
    ci: tuple = (None, None)
    n: int = 0                       # steps (CE) or tasks (Brier / ECE)
    n_sessions: int = 0
    quality: str = "label-free"      # label-free | gold | provisional | inconclusive
    note: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def quality_of(srcs) -> str:
    srcs = list(srcs)
    if not srcs:
        return "inconclusive"
    return "gold" if all(s == "gold" for s in srcs) else "provisional"


def _multiplicities(n_clusters: int, n_boot: int, seed: int) -> np.ndarray:
    """(n_boot, n_clusters) resample counts: each draw picks n_clusters sessions with replacement."""
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n_clusters, size=(n_boot, n_clusters))
    m = np.zeros((n_boot, n_clusters))
    np.add.at(m, (np.repeat(np.arange(n_boot), n_clusters), idx.ravel()), 1.0)
    return m


def _cluster_index(clusters):
    keys = sorted(set(clusters), key=str)
    pos = {k: i for i, k in enumerate(keys)}
    return np.array([pos[c] for c in clusters], dtype=int), len(keys)


def cluster_bootstrap(fn, clusters, n_boot: int = N_BOOT, seed: int = 0, alpha: float = 0.05):
    """95% percentile CI of a weighted statistic under a session-cluster bootstrap.

    `fn(w)` gets one weight per item (the number of times its session was drawn) and returns the
    statistic or None. Returns (point, lo, hi); (point, None, None) with fewer than 2 sessions.
    Deterministic for a given seed.
    """
    ci_idx, s = _cluster_index(list(clusters))
    point = fn(np.ones(len(ci_idx)))
    if s < 2 or point is None:
        return point, None, None
    m = _multiplicities(s, n_boot, seed)
    vals = []
    for row in m:
        v = fn(row[ci_idx])
        if v is not None and np.isfinite(v):
            vals.append(v)
    if not vals:
        return point, None, None
    lo, hi = np.quantile(vals, [alpha / 2, 1 - alpha / 2])
    return point, float(lo), float(hi)


def weighted_mean_ci(x, clusters, n_boot: int = N_BOOT, seed: int = 0):
    """Cluster-bootstrap CI of a mean — vectorised (one matrix product, not a Python loop)."""
    x = np.asarray(x, dtype=float)
    if not len(x):
        return None, None, None
    ci_idx, s = _cluster_index(list(clusters))
    point = float(x.mean())
    if s < 2:
        return point, None, None
    sums = np.bincount(ci_idx, weights=x, minlength=s)
    cnts = np.bincount(ci_idx, minlength=s).astype(float)
    m = _multiplicities(s, n_boot, seed)
    den = m @ cnts
    means = (m @ sums)[den > 0] / den[den > 0]
    lo, hi = np.quantile(means, [0.025, 0.975])
    return point, float(lo), float(hi)


# ---- next action -------------------------------------------------------------------------------

def task_logprobs(model, steps, task) -> np.ndarray:
    """log P(act_i | steps_<i) for every step. Models may provide a faster `task_logprobs`."""
    if hasattr(model, "task_logprobs"):
        return np.asarray(model.task_logprobs(steps, task), dtype=float)
    out = np.empty(len(steps))
    for i, s in enumerate(steps):
        p = model.predict_proba(steps[:i], task)
        out[i] = math.log(max(float(p[act_id(s)]), EPS))
    return out


def step_losses(model, ds: Dataset, ids) -> tuple:
    """(per-step NLL array, session per step, day per step) over the given tasks."""
    losses, sids, days = [], [], []
    for tid in ids:
        st = ds.steps[tid]
        lp = task_logprobs(model, st, ds.tasks[tid])
        losses.extend((-np.maximum(lp, math.log(EPS))).tolist())
        sids.extend([ds.sid(tid)] * len(st))
        days.extend(day_of(s.get("ts")) for s in st)
    return np.array(losses), sids, days


def next_action_scores(model, name: str, ds: Dataset, split: str, seed: int = 0) -> list:
    """[CE score, perplexity score] on one split."""
    ids = ds.ids(split)
    loss, sids, _ = step_losses(model, ds, ids)
    if not len(loss):
        return [Score("ce", name, split, None), Score("perplexity", name, split, None)]
    pt, lo, hi = weighted_mean_ci(loss, sids, seed=seed)
    ns = len(set(sids))
    ce = Score("ce", name, split, pt, (lo, hi), len(loss), ns, note="nats/step")
    ppl = Score("perplexity", name, split, math.exp(pt),
                (None if lo is None else math.exp(lo), None if hi is None else math.exp(hi)),
                len(loss), ns)
    return [ce, ppl]


def paired_ce(model_a, model_b, name: str, ds: Dataset, split: str, seed: int = 0) -> Score:
    """CE(a) − CE(b) on the same steps, session-cluster bootstrap. extra.rel = 1 − CE(a)/CE(b)
    (the relative reduction G1 criterion 1 asks ≥ 5% of) with its own CI."""
    ids = ds.ids(split)
    la, sids, _ = step_losses(model_a, ds, ids)
    lb, _, _ = step_losses(model_b, ds, ids)
    if not len(la):
        return Score("ce_diff", name, split, None)
    pt, lo, hi = weighted_mean_ci(la - lb, sids, seed=seed)

    def rel(w):
        b = float(w @ lb)
        return None if b <= 0 else 1.0 - float(w @ la) / b

    rp, rlo, rhi = cluster_bootstrap(rel, sids, seed=seed)
    return Score("ce_diff", name, split, pt, (lo, hi), len(la), len(set(sids)),
                 note="a − b, nats/step", extra={"rel": rp, "rel_ci": (rlo, rhi)})


def per_day(model, name: str, ds: Dataset, split: str) -> list:
    """Per-UTC-day CE (no CI: a day is a handful of sessions). Rows sorted by day."""
    loss, sids, days = step_losses(model, ds, ds.ids(split))
    by = defaultdict(list)
    for l_, s, d in zip(loss, sids, days):
        by[d].append((l_, s))
    return [{"day": d, "model": name, "n": len(v), "n_sessions": len({s for _, s in v}),
             "ce": float(np.mean([l_ for l_, _ in v]))} for d, v in sorted(by.items(), key=lambda kv: str(kv[0]))]


# ---- outcome -----------------------------------------------------------------------------------

def ece(p, y, w=None, bins: int = 10):
    """Expected calibration error, equal-width bins; `w` optional item weights."""
    p, y = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    w = np.ones(len(p)) if w is None else np.asarray(w, dtype=float)
    tot = w.sum()
    if not len(p) or tot <= 0:
        return None
    b = np.minimum((p * bins).astype(int), bins - 1)
    out = 0.0
    for k in range(bins):
        m = b == k
        wk = w[m].sum()
        if wk > 0:
            out += wk / tot * abs(float(w[m] @ p[m]) / wk - float(w[m] @ y[m]) / wk)
    return out


def prefix_of(steps, prefix):
    """The steps an outcome probe may read: all (prefix None) or the first min(k, L − 1)."""
    return list(steps) if prefix is None else list(steps[:max(0, min(prefix, len(steps) - 1))])


def outcome_preds(model, ds: Dataset, ids, prefix=OUTCOME_PREFIX) -> tuple:
    """(p, y, sids, srcs) over labeled tasks among `ids`; P(success) from the task's prefix."""
    p, y, sids, srcs = [], [], [], []
    for tid in ids:
        t = ds.tasks[tid]
        lab = label(t)
        if lab is None:
            continue
        p.append(float(np.clip(model.outcome_proba(prefix_of(ds.steps[tid], prefix), t), 0.0, 1.0)))
        y.append(lab)
        sids.append(ds.sid(tid))
        srcs.append(t.get("outcome_src"))
    return np.array(p), np.array(y, dtype=float), sids, srcs


def outcome_scores(model, name: str, ds: Dataset, split: str, gold_only: bool = False,
                   seed: int = 0, prefix=OUTCOME_PREFIX) -> list:
    """[Brier, ECE] with cluster CIs and the label-quality flag."""
    ids = ds.ids(split, labeled=True, gold_only=gold_only)
    p, y, sids, srcs = outcome_preds(model, ds, ids, prefix)
    q = quality_of(srcs)
    if not len(p):
        return [Score("brier", name, split, None, quality="inconclusive", note="no labels"),
                Score("ece", name, split, None, quality="inconclusive", note="no labels")]
    ns = len(set(sids))
    bp, blo, bhi = weighted_mean_ci((p - y) ** 2, sids, seed=seed)
    ep, elo, ehi = cluster_bootstrap(lambda w: ece(p, y, w), sids, seed=seed)
    at = "end of task" if prefix is None else f"first {prefix} steps"
    return [Score("brier", name, split, bp, (blo, bhi), len(p), ns, quality=q, note=at),
            Score("ece", name, split, ep, (elo, ehi), len(p), ns, quality=q,
                  note=f"10 bins, {at}")]


def paired_brier(model_a, model_b, name: str, ds: Dataset, split: str, gold_only: bool = True,
                 seed: int = 0, prefix=OUTCOME_PREFIX) -> Score:
    """Brier(a) − Brier(b) on the same tasks (G1 criterion 2: CI must exclude 0, on gold)."""
    ids = ds.ids(split, labeled=True, gold_only=gold_only)
    pa, y, sids, srcs = outcome_preds(model_a, ds, ids, prefix)
    pb, _, _, _ = outcome_preds(model_b, ds, ids, prefix)
    if not len(pa):
        return Score("brier_diff", name, split, None, quality="inconclusive", note="no labels")
    pt, lo, hi = weighted_mean_ci((pa - y) ** 2 - (pb - y) ** 2, sids, seed=seed)
    return Score("brier_diff", name, split, pt, (lo, hi), len(pa), len(set(sids)),
                 quality=quality_of(srcs), note="a − b")
