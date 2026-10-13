"""Task-outcome predictor ensemble prototype (P6 side study).

Predicts the binary "bad" outcome (gold ``fail`` or ``partial`` vs ``success``; ``unknown``
dropped) of a gold-labelled task from its step stream (main + subagent streams), optionally
stacked with the label judge / rule votes. Everything is evaluated with nested, stratified,
repeated k-fold cross-validation over the gold-labelled tasks only. It does NOT use the P6
split/protocol/ledger machinery and by default EXCLUDES tasks whose frozen P6 split is ``test``
so the held-out set stays untouched (``--include-p6-test`` overrides).

Models: A class prior; B length-only logistic; C judge-only rule; D L2 logistic on structure
features; E D + order-2 Markov surprise (fit on the other folds only, cross-fitted for the
training tasks); F E + judge/rule features (the stacked ensemble). G (gradient boosting) needs
scikit-learn and is skipped when it is not importable. Structure and surprise features are
prefix-computable and are evaluated at ``--prefixes``: values <= 1 are fractions of the task's
own length (they leak total length through ``n_steps``), values > 1 are absolute first-k-step
prefixes (the online quantity); the judge/rule features of F are whole-task signals at every
prefix. First run 2026-10-12 and the pre-declared verdict (stack not adopted over the judge;
8-step structure AUROC 0.68): ``docs/research/2026-10-12-rsi-next-steps.md`` §4.

Run: ``PYTHONPATH=src .venv/bin/python -m apex_router.worldmodel.outcome [--out card.json]``.
Read-only on its inputs; writes only ``--out``.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ACTS = ("search", "read", "edit", "write", "run", "test", "build", "vcs", "remote", "plan",
        "delegate", "ask", "other")
PHASES = ("explore", "edit", "verify", "deliver", "other")
EDIT_ACTS = {"edit", "write"}
RESPONSE_ACTS = {"read", "search", "test"}
BAD = {"fail", "partial"}
PREFIXES = (0.25, 0.5, 1.0)
LAMS = (0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0)
TOP_TYPES = 8


@dataclass
class Task:
    key: str
    y: int
    steps: list
    meta: dict
    judge: dict | None = None
    votes: dict = field(default_factory=dict)
    outcome: str = ""


# ---- loading ----------------------------------------------------------------------------------

def _jsonl(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def order_steps(steps: list) -> list:
    """Merge main + subagent streams in time order (main first on ties, then stream index)."""
    def k(s):
        ts = s.get("ts")
        return (ts if isinstance(ts, (int, float)) else math.inf,
                s.get("agent") is not None, str(s.get("agent")), s.get("i", 0))
    return sorted(steps, key=k)


def load(steps_path, tasks_path, gold_path, label_tasks_path, include_p6_test=False):
    """Gold-labelled tasks with their steps. Gold row in force per id is the LAST one."""
    gold = {}
    for r in _jsonl(gold_path):
        if isinstance(r, dict) and "id" in r:
            gold[r["id"]] = r
    ltasks = {r["id"]: r for r in _jsonl(label_tasks_path) if isinstance(r, dict) and "id" in r}
    wtasks = {r["task"]: r for r in _jsonl(tasks_path) if isinstance(r, dict) and "task" in r}
    info = Counter(gold_ids=len(gold))
    want = {}
    for gid, g in gold.items():
        lt = ltasks.get(gid)
        if lt is None:
            info["no_label_task"] += 1
            continue
        key = f"{lt['session']}:{lt['index']}"
        if key not in wtasks:
            info["no_wm_task"] += 1
            continue
        if g.get("outcome") not in ("success", "fail", "partial"):
            info["unknown"] += 1
            continue
        if wtasks[key].get("split") == "test" and not include_p6_test:
            info["excluded_p6_test"] += 1
            continue
        want[key] = (g, lt)
    by = defaultdict(list)
    for s in _jsonl(steps_path):
        if isinstance(s, dict) and s.get("task") in want:
            by[s["task"]].append(s)
    tasks = []
    for key, (g, lt) in sorted(want.items()):
        if not by.get(key):
            info["no_steps"] += 1
            continue
        tasks.append(Task(key=key, y=int(g["outcome"] in BAD), steps=order_steps(by[key]),
                          meta=wtasks[key], judge=lt.get("judge"), votes=lt.get("votes") or {},
                          outcome=g["outcome"]))
    oc = Counter(t.outcome for t in tasks)
    info.update(n=len(tasks), n_bad=sum(t.y for t in tasks),
                n_success=oc["success"], n_fail=oc["fail"], n_partial=oc["partial"])
    return tasks, dict(info)


def top_task_types(tasks_path, k=TOP_TYPES) -> list:
    c = Counter(r.get("task_type") for r in _jsonl(tasks_path) if isinstance(r, dict))
    return [t for t, _ in c.most_common() if t][:k]


# ---- features -----------------------------------------------------------------------------------

def prefix(steps: list, frac: float) -> list:
    """Prefix of a step list. ``frac`` <= 1 is a fraction of the task's own length (leaks the
    full length through n_steps); ``frac`` > 1 is an absolute step count (first k steps, the
    online quantity: a task shorter than k is scored on all its steps)."""
    if frac > 1.0:
        return list(steps[:int(frac)])
    if frac >= 1.0:
        return list(steps)
    return list(steps[:max(1, math.ceil(frac * len(steps)))])


def structure_features(steps: list, meta: dict, top_types: list) -> dict:
    """Prefix-computable structure features of a merged (time-ordered) step list."""
    n = len(steps)
    acts = [s.get("act") for s in steps]
    errs = [int(bool(s.get("err"))) for s in steps]
    f = {"n_steps": math.log1p(n),
         "n_sub_steps": math.log1p(sum(1 for s in steps if s.get("agent") is not None))}
    ac = Counter(acts)
    for a in ACTS:
        f[f"act_{a}"] = ac[a] / n if n else 0.0
    pc = Counter(s.get("phase") for s in steps)
    for p in PHASES:
        f[f"phase_{p}"] = pc[p] / n if n else 0.0
    first = next((j for j, a in enumerate(acts) if a in EDIT_ACTS), None)
    f["first_edit"] = first / n if first is not None else 1.0
    f["has_edit"] = float(first is not None)
    f["early_patch"] = sum(1 for a in acts[:10] if a in EDIT_ACTS) / 10.0
    n_err = sum(errs)
    f["n_err"] = math.log1p(n_err)
    f["err_share"] = n_err / n if n else 0.0
    # error response: an err step followed within 3 steps OF THE SAME STREAM by read/search/test
    streams = defaultdict(list)
    for s in steps:
        streams[s.get("agent")].append(s)
    answered = 0
    for st in streams.values():
        for j, s in enumerate(st):
            if s.get("err") and any(x.get("act") in RESPONSE_ACTS for x in st[j + 1:j + 4]):
                answered += 1
    f["err_response"] = answered / n_err if n_err else 0.0
    ran = [s for s in steps if (s.get("tests") or {}).get("ran")]
    f["tests_ran"] = math.log1p(len(ran))
    last_f = next((s["tests"]["failed"] for s in reversed(ran)
                   if s["tests"].get("failed") is not None), None)
    last_p = next((s["tests"]["passed"] for s in reversed(ran)
                   if s["tests"].get("passed") is not None), None)
    f["tests_failed_last"] = math.log1p(last_f) if last_f else 0.0
    f["tests_passed_last"] = math.log1p(last_p) if last_p else 0.0
    f["tail_errors"] = float(sum(errs[-3:]))
    f["n_spawns"] = math.log1p(sum(int(s.get("spawn") or 0) for s in steps))
    tt = meta.get("task_type")
    for t in top_types:
        f[f"tt_{t}"] = float(tt == t)
    f["w2"] = float(meta.get("workflow") == "W2")
    f["src_pi"] = float(meta.get("src") == "pi")
    return f


def judge_features(t: Task) -> dict:
    j = t.judge or {}
    out = j.get("outcome")
    return {"judge_bad": float(out in BAD), "judge_unknown": float(out not in BAD | {"success"}),
            "judge_conf": float(j.get("confidence") or 0.0),
            "rule_fail_votes": float(sum(1 for v in t.votes.values() if v == "fail"))}


class Markov:
    """Order-2 next-action model with a symmetric Dirichlet(alpha) prior per context."""

    def __init__(self, alpha: float = 0.5):
        self.alpha = alpha
        self.counts: dict = defaultdict(Counter)
        self.tot: Counter = Counter()

    def fit(self, seqs):
        for seq in seqs:
            ctx = ("<s>", "<s>")
            for a in seq:
                self.counts[ctx][a] += 1
                self.tot[ctx] += 1
                ctx = (ctx[1], a)
        return self

    def surprisal(self, seq) -> list:
        out, ctx, k = [], ("<s>", "<s>"), len(ACTS)
        for a in seq:
            p = (self.counts[ctx][a] + self.alpha) / (self.tot[ctx] + k * self.alpha) \
                if ctx in self.counts else 1.0 / k
            out.append(-math.log(p))
            ctx = (ctx[1], a)
        return out


def _streams(t: Task):
    st = defaultdict(list)
    for j, s in enumerate(t.steps):
        st[s.get("agent")].append(j)
    return st


def task_surprisal(m: Markov, t: Task) -> np.ndarray:
    """Per-step surprisal in merged order, each step conditioned on its own stream's history."""
    out = np.zeros(len(t.steps))
    for idx in _streams(t).values():
        out[idx] = m.surprisal([t.steps[j].get("act") for j in idx])
    return out


def fit_markov(tasks: list) -> Markov:
    seqs = []
    for t in tasks:
        for idx in _streams(t).values():
            seqs.append([t.steps[j].get("act") for j in idx])
    return Markov(0.5).fit(seqs)


def surprise_features(s: np.ndarray, frac: float) -> dict:
    s = s[:len(prefix(list(range(len(s))), frac))]
    mx = float(s.max()) if len(s) else 0.0
    return {"sur_mean": float(s.mean()) if len(s) else 0.0, "sur_max": mx,
            "sur_minp": math.exp(-mx), "sur_last5": float(s[-5:].mean()) if len(s) else 0.0}


# ---- models -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Spec:
    kind: str                 # prior | judge | logit
    groups: tuple = ()        # length | structure | surprise | judge


MODELS = {"A": Spec("prior"), "B": Spec("logit", ("length",)), "C": Spec("judge"),
          "D": Spec("logit", ("structure",)), "E": Spec("logit", ("structure", "surprise")),
          "F": Spec("logit", ("structure", "surprise", "judge"))}
DESC = {"A": "class prior", "B": "length-only logistic", "C": "judge-only rule",
        "D": "L2 logit: structure", "E": "L2 logit: structure+surprise",
        "F": "L2 logit: structure+surprise+judge/rules (stack)"}


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))


def fit_logreg(X: np.ndarray, y: np.ndarray, lam: float, iters: int = 50):
    """L2 logistic regression (mean log-loss + lam/2 |w|^2, intercept unpenalised), Newton."""
    n, d = X.shape
    Z = np.hstack([X, np.ones((n, 1))])
    w = np.zeros(d + 1)
    reg = np.full(d + 1, lam)
    reg[-1] = 1e-8
    for _ in range(iters):
        p = _sigmoid(Z @ w)
        g = Z.T @ (p - y) / n + reg * w
        H = (Z * (p * (1 - p))[:, None]).T @ Z / n + np.diag(reg)
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w[:-1], w[-1]


def _std(Xtr):
    mu = Xtr.mean(0)
    sd = Xtr.std(0)
    sd[sd < 1e-12] = 1.0
    return mu, sd


def _logloss(y, p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def stratified_folds(y: np.ndarray, k: int, rng) -> list:
    folds = [[] for _ in range(k)]
    for c in np.unique(y):
        idx = np.flatnonzero(y == c)
        rng.shuffle(idx)
        off = int(rng.integers(k))
        for j, i in enumerate(idx):
            folds[(j + off) % k].append(int(i))
    return [np.array(sorted(f), dtype=int) for f in folds]


def _cols(spec: Spec, names_by_group: dict) -> list:
    return [c for g in spec.groups for c in names_by_group[g]]


def nested_cv(tasks: list, spec: Spec, frac: float, repeats: int = 3, k: int = 5, seed: int = 0,
              inner_k: int = 5, lams=LAMS, top_types=(), on_markov_fit=None, on_fit=None,
              log: dict | None = None, cache: dict | None = None) -> np.ndarray:
    """Out-of-fold P(bad) per repeat, shape (repeats, n). Hyperparameter (lam) chosen by inner
    CV log-loss on the outer-training tasks only. Surprise features: held-out tasks are scored
    by a Markov fit on the outer-training tasks; outer-training tasks are cross-fitted (each
    scored by a Markov fit without its own inner chunk), so no task is ever scored by a model
    that saw its own steps."""
    y = np.array([t.y for t in tasks])
    n = len(tasks)
    keys = [t.key for t in tasks]
    cache = {} if cache is None else cache
    base = cache.setdefault(("base", frac, tuple(top_types)), [
        {**structure_features(prefix(t.steps, frac), t.meta, list(top_types)),
         **judge_features(t)} for t in tasks])
    sname = list(next(iter(base)).keys()) if base else []
    names_by_group = {"length": ["n_steps"],
                      "structure": [c for c in sname if c not in judge_features(tasks[0])],
                      "judge": list(judge_features(tasks[0])),
                      "surprise": ["sur_mean", "sur_max", "sur_minp", "sur_last5"]}
    cols = _cols(spec, names_by_group)
    out = np.zeros((repeats, n))
    for r in range(repeats):
        rng = np.random.default_rng(seed + 1000 * r)
        folds = stratified_folds(y, k, rng)
        for fi, te in enumerate(folds):
            tr = np.setdiff1d(np.arange(n), te)
            if spec.kind == "prior":
                if on_fit:
                    on_fit([keys[i] for i in tr], [keys[i] for i in te])
                out[r, te] = y[tr].mean()
                continue
            if spec.kind == "judge":
                out[r, te] = [base[i]["judge_bad"] for i in te]
                continue
            sur = None
            if "surprise" in spec.groups:
                ck = ("sur", seed, r, fi, k, inner_k)
                if ck not in cache or on_markov_fit:
                    cache[ck] = _fold_surprisal(tasks, tr, te, rng_seed=seed + 7919 * (r + 1) + fi,
                                                inner_k=inner_k, on_markov_fit=on_markov_fit)
                sur = cache[ck]

            def row(i):
                f = base[i]
                if sur is not None:
                    f = {**f, **surprise_features(sur[i], frac)}
                return [f[c] for c in cols]

            X = np.array([row(i) for i in range(n)], dtype=float)
            # inner CV for lam (inner splits of the outer-training tasks only)
            irng = np.random.default_rng(seed + 31 * r + fi + 1)
            ifolds = stratified_folds(y[tr], min(inner_k, max(2, int(y[tr].sum()))), irng)
            best, best_loss = lams[0], math.inf
            for lam in lams:
                losses = []
                for ite in ifolds:
                    itr = np.setdiff1d(np.arange(len(tr)), ite)
                    a, b = tr[itr], tr[ite]
                    if on_fit:
                        on_fit([keys[i] for i in a], [keys[i] for i in b])
                    mu, sd = _std(X[a])
                    w, c0 = fit_logreg((X[a] - mu) / sd, y[a], lam)
                    losses.append(_logloss(y[b], _sigmoid(((X[b] - mu) / sd) @ w + c0)))
                if np.mean(losses) < best_loss:
                    best, best_loss = lam, float(np.mean(losses))
            if on_fit:
                on_fit([keys[i] for i in tr], [keys[i] for i in te])
            mu, sd = _std(X[tr])
            w, c0 = fit_logreg((X[tr] - mu) / sd, y[tr], best)
            out[r, te] = _sigmoid(((X[te] - mu) / sd) @ w + c0)
            if log is not None:
                log.setdefault("lams", []).append(best)
                log.setdefault("coefs", []).append(w.tolist())
                log["names"] = cols
    return out


def _fold_surprisal(tasks, tr, te, rng_seed, inner_k, on_markov_fit):
    n = len(tasks)
    sur = [None] * n
    tr_tasks = [tasks[i] for i in tr]
    if on_markov_fit:
        on_markov_fit([t.key for t in tr_tasks], [tasks[i].key for i in te])
    m = fit_markov(tr_tasks)
    for i in te:
        sur[i] = task_surprisal(m, tasks[i])
    rng = np.random.default_rng(rng_seed)
    perm = rng.permutation(tr)
    for chunk in np.array_split(perm, inner_k):
        rest = np.setdiff1d(tr, chunk)
        if on_markov_fit:
            on_markov_fit([tasks[i].key for i in rest],
                          [tasks[i].key for i in chunk] + [tasks[i].key for i in te])
        mi = fit_markov([tasks[i] for i in rest])
        for i in chunk:
            sur[i] = task_surprisal(mi, tasks[i])
    return sur


# ---- metrics ------------------------------------------------------------------------------------

def _ranks(x):
    order = np.argsort(x, kind="mergesort")
    xs = x[order]
    r = np.empty(len(x))
    i = 0
    while i < len(x):
        j = i
        while j + 1 < len(x) and xs[j + 1] == xs[i]:
            j += 1
        r[order[i:j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    return r


def auroc(y, p) -> float:
    y = np.asarray(y)
    p = np.asarray(p, dtype=float)
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    r = _ranks(p)
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def brier(y, p) -> float:
    return float(np.mean((np.asarray(p, float) - np.asarray(y)) ** 2))


def ece(y, p, bins: int = 10):
    y = np.asarray(y)
    p = np.asarray(p, float)
    b = np.minimum((p * bins).astype(int), bins - 1)
    tot, rows = 0.0, []
    for k in range(bins):
        m = b == k
        if m.any():
            gap = abs(y[m].mean() - p[m].mean())
            tot += m.mean() * gap
            rows.append({"bin": k, "n": int(m.sum()), "mean_p": round(float(p[m].mean()), 4),
                         "frac_bad": round(float(y[m].mean()), 4)})
    return float(tot), rows


def _ci(vals):
    v = np.asarray([x for x in vals if not math.isnan(x)])
    return [round(float(np.percentile(v, 2.5)), 4), round(float(np.percentile(v, 97.5)), 4)]


# ---- driver -------------------------------------------------------------------------------------

def run(tasks, info, *, top_types, repeats=3, k=5, seed=0, boot=1000, command="",
        prefixes=PREFIXES):
    t0 = time.time()
    y = np.array([t.y for t in tasks])
    n = len(y)
    cache: dict = {}
    preds, per_rep, logs = {}, {}, {}
    plan = [("A", 1.0), ("C", 1.0)] + [(m, p) for m in "BDEF" for p in prefixes]
    for m, p in plan:
        log: dict = {}
        P = nested_cv(tasks, MODELS[m], p, repeats=repeats, k=k, seed=seed,
                      top_types=top_types, log=log, cache=cache)
        preds[(m, p)] = P.mean(0)
        per_rep[(m, p)] = [auroc(y, P[r]) for r in range(repeats)]
        logs[(m, p)] = log
    brng = np.random.default_rng(seed)
    draws = [brng.integers(0, n, n) for _ in range(boot)]
    draws = [d for d in draws if 0 < y[d].sum() < n]
    bA = {key: [auroc(y[d], v[d]) for d in draws] for key, v in preds.items()}
    bB = {key: [brier(y[d], v[d]) for d in draws] for key, v in preds.items()}
    results = []
    for (m, p), v in preds.items():
        results.append({"model": m, "desc": DESC[m], "prefix": p, "n": n,
                        "auroc": round(auroc(y, v), 4), "auroc_ci": _ci(bA[(m, p)]),
                        "auroc_repeat_mean": round(float(np.mean(per_rep[(m, p)])), 4),
                        "brier": round(brier(y, v), 4), "brier_ci": _ci(bB[(m, p)]),
                        "lam_chosen": dict(Counter(logs[(m, p)].get("lams", [])))})
    paired = []
    for m in "BDEF":
        for p in prefixes:
            refs = [("C", 1.0)] + ([("B", p)] if m != "B" else [])
            for ref in refs:
                d = np.array(bA[(m, p)]) - np.array(bA[ref])
                point = auroc(y, preds[(m, p)]) - auroc(y, preds[ref])
                paired.append({"model": m, "prefix": p, "vs": ref[0], "vs_prefix": ref[1],
                               "d_auroc": round(point, 4), "ci": _ci(d),
                               "p_le_0": round(float(np.mean(d <= 0)), 4)})
    e, rows = ece(y, preds[("F", 1.0)], 10)
    flog = logs[("F", 1.0)]
    W = np.array(flog["coefs"])
    mean_w = W.mean(0)
    sign_agree = (np.sign(W) == np.sign(mean_w)).mean(0)
    order = np.argsort(-np.abs(mean_w))
    top = [{"feature": flog["names"][j], "coef_std": round(float(mean_w[j]), 4),
            "sign_agreement": round(float(sign_agree[j]), 3)} for j in order[:8]]
    meta = {**info, "n": n, "n_bad": int(y.sum()), "n_success": int(n - y.sum()),
            "target": "bad = gold outcome in {fail, partial}; success = 0; unknown dropped",
            "cv": {"outer_folds": k, "inner_folds": 5, "repeats": repeats, "seed": seed,
                   "stratified": True, "lam_grid": list(LAMS), "inner_select": "log-loss"},
            "bootstrap": {"draws": len(draws), "requested": boot, "unit": "task",
                          "point": "AUROC/Brier of repeat-averaged out-of-fold predictions"},
            "markov": {"order": 2, "alpha": 0.5, "fit_on": "outer-train gold tasks only; "
                       "outer-train tasks cross-fitted (5 chunks)"},
            "top_task_types": list(top_types),
            "sklearn": False, "G_gbm": "skipped: scikit-learn not importable in .venv",
            "prefixes": list(prefixes),
            "caveats": [
                "prefix values <= 1 are fractions of each task's own length, so n_steps at such "
                "a prefix encodes the full length (optimistic for online use); values > 1 are "
                "absolute first-k-step prefixes (the online quantity)",
                "F uses whole-task judge/rule signals at every prefix",
                "C is a hard 0/1 rule: its AUROC is balanced accuracy, its Brier an error rate",
                "labels.jsonl posterior not used: gold-labelled rows carry from=gold, p=1.0"],
            "elapsed_s": round(time.time() - t0, 1), "command": command}
    return {"meta": meta, "results": results, "paired": paired,
            "calibration": {"model": "F", "prefix": 1.0, "ece10": round(e, 4), "bins": rows},
            "top_features": top}


def render(card) -> str:
    m = card["meta"]
    lines = [f"task-outcome predictor  n={m['n']}  bad={m['n_bad']} "
             f"(fail={m.get('n_fail')}, partial={m.get('n_partial')})  success={m['n_success']}  "
             f"excluded: P6-test={m.get('excluded_p6_test', 0)} unknown={m.get('unknown', 0)} "
             f"no-label-task={m.get('no_label_task', 0)}",
             f"nested CV {m['cv']['outer_folds']}x{m['cv']['repeats']} (inner "
             f"{m['cv']['inner_folds']}), bootstrap {m['bootstrap']['draws']} task draws",
             "", f"{'model':<6}{'prefix':>7}  {'AUROC':>6}  {'95% CI':<16}{'rep-mean':>9}  "
             f"{'Brier':>6}  {'95% CI':<16}desc"]
    for r in card["results"]:
        lines.append(f"{r['model']:<6}{r['prefix']:>7}  {r['auroc']:>6.3f}  "
                     f"[{r['auroc_ci'][0]:.3f},{r['auroc_ci'][1]:.3f}]  {r['auroc_repeat_mean']:>8.3f}"
                     f"  {r['brier']:>6.3f}  [{r['brier_ci'][0]:.3f},{r['brier_ci'][1]:.3f}]  "
                     f"{r['desc']}")
    lines += ["", "paired dAUROC (model - ref), bootstrap 95% CI:"]
    for p in card["paired"]:
        lines.append(f"  {p['model']}@{p['prefix']:<5} vs {p['vs']}@{p['vs_prefix']:<5} "
                     f"{p['d_auroc']:+.3f}  [{p['ci'][0]:+.3f},{p['ci'][1]:+.3f}]  "
                     f"P(d<=0)={p['p_le_0']:.3f}")
    c = card["calibration"]
    lines += ["", f"ECE(10 bins) F@1.0 = {c['ece10']:.3f}", "",
              "top F@1.0 features (mean standardised coef over outer fits, sign agreement):"]
    for f in card["top_features"]:
        lines.append(f"  {f['feature']:<20}{f['coef_std']:+.3f}  {f['sign_agreement']:.2f}")
    lines += ["", "G (HistGradientBoosting): " + m["G_gbm"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    home = Path(os.path.expanduser("~")) / ".apex-router"
    ap = argparse.ArgumentParser(prog="python -m apex_router.worldmodel.outcome",
                                 description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--steps", default=str(home / "worldmodel" / "steps.jsonl"))
    ap.add_argument("--tasks", default=str(home / "worldmodel" / "tasks.jsonl"))
    ap.add_argument("--gold", default=str(home / "labels" / "gold.jsonl"))
    ap.add_argument("--label-tasks", default=str(home / "labels" / "tasks.jsonl"))
    ap.add_argument("--out", default="/tmp/outcome-card.json")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--boot", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--prefixes", default=",".join(str(p) for p in PREFIXES),
                    help="comma list; <=1 fraction of task length, >1 absolute first-k steps")
    ap.add_argument("--include-p6-test", action="store_true",
                    help="also use gold tasks in the frozen P6 test split (default: excluded)")
    a = ap.parse_args(argv)
    tasks, info = load(a.steps, a.tasks, a.gold, a.label_tasks, a.include_p6_test)
    if len(tasks) < 2 * a.folds or not 0 < sum(t.y for t in tasks) < len(tasks):
        print(f"not enough labelled tasks: {info}", file=sys.stderr)
        return 2
    cmd = "PYTHONPATH=src .venv/bin/python -m apex_router.worldmodel.outcome " + " ".join(
        argv if argv is not None else sys.argv[1:])
    card = run(tasks, info, top_types=top_task_types(a.tasks), repeats=a.repeats, k=a.folds,
               seed=a.seed, boot=a.boot, command=cmd.strip(),
               prefixes=tuple(float(x) for x in a.prefixes.split(",") if x.strip()))
    Path(a.out).write_text(json.dumps(card, indent=1))
    print(render(card))
    print(f"\ncard -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
