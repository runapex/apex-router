"""P6 E4 — the G1 scorecard (DESIGN-worldmodel-P6.md §6; RESEARCH-FIT-BACKLOG.md P6, gate G1).

`apex-router worldmodel evaluate [--run ID ... | --train [--seeds K] [--sweep]] [--synthetic N]`

Scores the JEPA (E3) against E2's baselines on the TEST split under one protocol (protocol.py)
and prints G1's five criteria, each PASS / FAIL / INCONCLUSIVE with its numbers, n and CI, then
the overall verdict. G1 passes only if all five PASS.

**Sequences = streams.** A step's `i` counts within its (task, agent) stream (contract §1), so a
task's steps sorted by `i` interleave the main thread with every subagent. The scorecard scores
each stream as its own sequence (`stream_view`): the main stream keeps the task id, a subagent
stream is `<task>@<agent>` with the same session (so the session-cluster bootstrap and the split
are unchanged). Outcome criteria (2, 3, 5) read the MAIN stream of each task — the label is the
task's; subagent streams carry no outcome (they train next-action only). `--view task` gives
the raw interleaved order instead (E2/E3's readouts) for comparison.

**Same rules for every model.**
- Step 0 of every sequence is scored from ONE start prior: add-½ over the train sequences'
  first actions (`features.start_prior`), for the JEPA and every baseline alike (`StartPrior`).
- Hyper-parameters and the "best baseline" are chosen on val; test is scored in exactly one
  pass per model (`_TestPass`) and every test number (CE, paired differences, per-day, Brier,
  ECE, ranking, detector flags, probes, test-z collapse) is computed from that one pass.
- Outcome probes read the first min(OUTCOME_PREFIX, L − 1) steps (never the last step).

**Criteria (the rule each line states):**
1. rel = 1 − CE(JEPA)/CE(best-on-val baseline) on test steps; PASS iff rel ≥ 5% AND the
   session-bootstrap 95% CI lower bound of rel > 0.
2. paired Brier(JEPA value head) − Brier(best-on-val outcome baseline) on GOLD test tasks;
   PASS iff the difference < 0 and its CI upper bound < 0; no gold → INCONCLUSIVE (a weak-label
   line is printed as provisional, never used).
3. workflow-ranking accuracy (chains.ranking_accuracy) of JEPA values vs the absorbing chain's;
   values are train-derived for both (chain: P(SUCCESS) per (type, workflow) on train; JEPA:
   mean value-head P(success) at the outcome prefix over the same train tasks); PASS iff
   JEPA ≥ chain; no scorable pairs → INCONCLUSIVE.
4. collapse: every evaluation checkpoint (each epoch's val diagnostics in metrics.jsonl and the
   test-z diagnostics of the scored checkpoint) within the run's preset bounds.
5. progress.criterion5 with the JEPA Zeno detector (v_t = value-head P(success), threshold
   SUCCESS_V) as the detector, E2's proxy detectors alongside; gold only, else INCONCLUSIVE.
   The criterion-5 rule is E2's, signed off by the owner 2026-10-07.

With several seeds a criterion PASSes only if it PASSes on every seed (any FAIL → FAIL); the
CPU replay run is reported but not part of the verdict.

**Guards.** A run trained on another sequence view than `--view` is refused before any test
pass (`summary.view`, else the data-source suffix `…:streams` / `…:task`). A run that saw no
labelled train task has an untrained value head (a random projection): criteria 2, 3 and 5
are INCONCLUSIVE for it ("value head untrained (0 labelled train tasks)") whatever the test
labels, and no outcome number is printed for it, not even a provisional one.

**Ledger.** Every scoring of the real test split appends one line per run to
`<data home>/eval/ledger.jsonl` (0600: ts, manifest + steps/tasks sha256, run id, view, git
sha); the card prints how often the split has been scored for this manifest. The first write
backfills from the saved scorecards (`--backfill-ledger` does only that).
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import time
from pathlib import Path

import numpy as np

from . import protocol as P

REL_MIN = 0.05
DAY_MIN_N = 30              # per-day rows with fewer test steps are flagged "too few"
SUCCESS_V = 0.5             # Zeno threshold on the value head: converging short of P(success) 0.5
VERDICTS = ("PASS", "FAIL", "INCONCLUSIVE")
RULES = {
    1: "PASS iff rel = 1 − CE(JEPA)/CE(best-on-val baseline) ≥ 5% and its 95% CI lower bound > 0",
    2: "gold only: PASS iff Brier(JEPA) − Brier(best-on-val outcome baseline) < 0 with 95% CI upper < 0",
    3: "PASS iff JEPA workflow-ranking accuracy ≥ the absorbing chain's (pairs with ≥ 5 test tasks each)",
    4: "PASS iff every evaluation checkpoint (each epoch's val z + the scored checkpoint's test z) is "
       "within the preset bounds (effective rank ≥ min, SIGReg ≤ max)",
    5: "gold only: progress.criterion5 for the JEPA Zeno detector (FPR Wilson-upper ≤ 10%, win share "
       "Wilson-lower > 0.5, median steps saved > 0) — rule owner-signed 2026-10-07",
}
CRITERIA = {1: "next-state CE ≥ 5% below the best baseline", 2: "outcome Brier below baseline (gold)",
            3: "workflow ranking ≥ Markov chain", 4: "no collapse at any checkpoint",
            5: "Zeno detector beats cutoffs (gold)"}


# ---- data views ----------------------------------------------------------------------------------

def stream_view(ds: P.Dataset) -> P.Dataset:
    """One sequence per (task, agent) stream. Main stream keeps the task id and the label; a
    subagent stream `<task>@<agent>` keeps session/split/time and carries no outcome."""
    tasks, steps = {}, {}
    for tid, st in ds.steps.items():
        row = ds.tasks[tid]
        by = {}
        for s in st:
            by.setdefault(s.get("agent"), []).append(s)
        for agent, ss in by.items():
            sid = tid if agent is None else f"{tid}@{agent}"
            ss = sorted(ss, key=lambda s: s["i"])
            r = dict(row, task=sid, stream_of=tid, agent=agent, t0=ss[0].get("ts", row.get("t0")))
            if agent is not None:
                r.update(outcome="unknown", outcome_src="none")
            tasks[sid] = r
            steps[sid] = ss
    return P.Dataset(tasks=tasks, steps=steps, dropped=ds.dropped)


def main_ids(ds: P.Dataset, split=None, labeled: bool = False, gold_only: bool = False) -> list:
    return [t for t in ds.ids(split, labeled=labeled, gold_only=gold_only)
            if ds.tasks[t].get("agent") is None]


def to_train_dataset(ds: P.Dataset, source: str):
    """The same sequences as E3's train.Dataset (one 'task' per sequence)."""
    from . import train as T
    return T.Dataset(tasks={t: list(ds.steps[t]) for t in ds.tasks},
                     meta={t: dict(r) for t, r in ds.tasks.items()}, source=source)


def start_prior(ds: P.Dataset, ids) -> np.ndarray:
    from . import features as F
    return F.start_prior({t: ds.steps[t] for t in ids})


# ---- models under one convention ------------------------------------------------------------------

class StartPrior:
    """A protocol model whose step 0 is scored from the shared start prior (everything else is the
    wrapped model's)."""

    def __init__(self, base, sp):
        self.base, self.sp, self.name = base, np.asarray(sp, dtype=float), base.name

    def task_logprobs(self, steps, task=None):
        lp = np.array(P.task_logprobs(self.base, steps, task), dtype=float)
        if len(lp):
            lp[0] = math.log(max(float(self.sp[P.act_id(steps[0])]), P.EPS))
        return lp

    def predict_proba(self, history, task=None):
        return self.sp if not history else self.base.predict_proba(history, task)

    def outcome_proba(self, steps, task=None):
        return self.base.outcome_proba(steps, task)


class Jepa:
    """E3's WorldModel as a protocol model. `prepare(ds, ids)` runs ONE `WorldModel.score` pass
    over the given sequences; afterwards logprobs / values are looked up, never recomputed."""

    def __init__(self, wm, sp, base_rate: float, name: str = "JEPA"):
        self.wm, self.sp, self.base_rate, self.name = wm, np.asarray(sp, dtype=float), base_rate, name
        self.cache: dict = {}

    def prepare(self, ds: P.Dataset, ids) -> None:
        res = self.wm.score({t: ds.steps[t] for t in ids})
        for t in ids:
            r = res.get(t)
            if r is None or len(r["p_success"]) != len(ds.steps[t]):
                raise RuntimeError(f"JEPA scored {0 if r is None else len(r['p_success'])} of "
                                   f"{len(ds.steps[t])} steps of {t}")
            self.cache[t] = r

    def task_logprobs(self, steps, task):
        r = self.cache[task["task"]]
        acts = [P.act_id(s) for s in steps]
        lp = np.empty(len(acts))
        if acts:
            lp[0] = math.log(max(float(self.sp[acts[0]]), P.EPS))
            pr = r["next_action_probs"]
            lp[1:] = np.log(np.clip(pr[np.arange(len(acts) - 1), acts[1:]], P.EPS, 1.0))
        return lp

    def outcome_proba(self, steps, task):
        k = len(steps)
        return self.base_rate if k == 0 else float(self.cache[task["task"]]["p_success"][k - 1])

    def value_series(self, tid) -> np.ndarray:
        return np.asarray(self.cache[tid]["p_success"], dtype=float)


class _TestPass:
    """Everything a model says about the test split, computed once."""

    def __init__(self, model, ds: P.Dataset, ids, out_ids, prefix=P.OUTCOME_PREFIX):
        self.name = model.name
        loss, sids, days, main = [], [], [], []
        for t in ids:
            st = ds.steps[t]
            lp = model.task_logprobs(st, ds.tasks[t])
            loss.extend((-np.maximum(lp, math.log(P.EPS))).tolist())
            sids.extend([ds.sid(t)] * len(st))
            days.extend(P.day_of(s.get("ts")) for s in st)
            main.extend([ds.tasks[t].get("agent") is None] * len(st))
        self.loss, self.sids, self.days = np.array(loss), sids, days
        self.main = np.array(main, dtype=bool)
        self.p = {t: float(np.clip(model.outcome_proba(P.prefix_of(ds.steps[t], prefix),
                                                       ds.tasks[t]), 0.0, 1.0))
                  for t in out_ids}


# ---- statistics from one pass ---------------------------------------------------------------------

def _ce(tp: _TestPass, seed: int, mask=None) -> dict:
    loss = tp.loss if mask is None else tp.loss[mask]
    sids = tp.sids if mask is None else [s for s, m in zip(tp.sids, mask) if m]
    if not len(loss):
        return {"value": None, "ci": (None, None), "n": 0, "n_sessions": 0}
    pt, lo, hi = P.weighted_mean_ci(loss, sids, seed=seed)
    return {"value": pt, "ci": (lo, hi), "n": int(len(loss)), "n_sessions": len(set(sids))}


def paired(a: _TestPass, b: _TestPass, seed: int, mask=None) -> dict:
    """CE(a) − CE(b) and rel = 1 − CE(a)/CE(b), session-cluster bootstrap on the same steps."""
    la, lb = (a.loss, b.loss) if mask is None else (a.loss[mask], b.loss[mask])
    sids = a.sids if mask is None else [s for s, m in zip(a.sids, mask) if m]
    if not len(la):
        return {"diff": None, "diff_ci": (None, None), "rel": None, "rel_ci": (None, None), "n": 0,
                "n_sessions": 0}
    pt, lo, hi = P.weighted_mean_ci(la - lb, sids, seed=seed)

    def rel(w):
        den = float(w @ lb)
        return None if den <= 0 else 1.0 - float(w @ la) / den

    rp, rlo, rhi = P.cluster_bootstrap(rel, sids, seed=seed)
    return {"diff": pt, "diff_ci": (lo, hi), "rel": rp, "rel_ci": (rlo, rhi), "n": int(len(la)),
            "n_sessions": len(set(sids))}


def per_day(tps: list) -> list:
    rows = {}
    for tp in tps:
        by = {}
        for l_, d, s in zip(tp.loss, tp.days, tp.sids):
            by.setdefault(d, []).append((l_, s))
        for d, v in by.items():
            r = rows.setdefault(d, {"day": d, "n": len(v), "n_sessions": len({s for _, s in v}),
                                    "too_few": len(v) < DAY_MIN_N})
            r[tp.name] = float(np.mean([x for x, _ in v]))
    return [rows[d] for d in sorted(rows, key=str)]


def outcome_stats(tp_a: _TestPass, tp_b: _TestPass, ds: P.Dataset, ids, seed: int) -> dict:
    """Brier/ECE of a and b and the paired Brier difference a − b over `ids` (labeled)."""
    ids = [t for t in ids if P.label(ds.tasks[t]) is not None]
    if not ids:
        return {"n": 0, "n_sessions": 0, "quality": "inconclusive"}
    y = np.array([P.label(ds.tasks[t]) for t in ids], dtype=float)
    pa = np.array([tp_a.p[t] for t in ids])
    pb = np.array([tp_b.p[t] for t in ids])
    sids = [ds.sid(t) for t in ids]
    out = {"n": len(ids), "n_sessions": len(set(sids)),
           "quality": P.quality_of(ds.tasks[t].get("outcome_src") for t in ids)}
    for k, p in (("a", pa), ("b", pb)):
        bp, blo, bhi = P.weighted_mean_ci((p - y) ** 2, sids, seed=seed)
        ep, elo, ehi = P.cluster_bootstrap(lambda w, p=p: P.ece(p, y, w), sids, seed=seed)
        out[k] = {"model": (tp_a if k == "a" else tp_b).name, "brier": bp, "brier_ci": (blo, bhi),
                  "ece": ep, "ece_ci": (elo, ehi)}
    d, lo, hi = P.weighted_mean_ci((pa - y) ** 2 - (pb - y) ** 2, sids, seed=seed)
    out["diff"], out["diff_ci"] = d, (lo, hi)
    return out


# ---- the five rules (pure; unit-tested on hand-built inputs) --------------------------------------

def rule1(rel, rel_lo) -> str:
    if rel is None or rel_lo is None:
        return "INCONCLUSIVE"
    return "PASS" if (rel >= REL_MIN and rel_lo > 0) else "FAIL"


def rule2(gold: dict) -> str:
    if not gold or not gold.get("n") or gold.get("quality") != "gold":
        return "INCONCLUSIVE"
    lo, hi = gold.get("diff_ci") or (None, None)
    if gold.get("diff") is None or hi is None:
        return "INCONCLUSIVE"
    return "PASS" if (gold["diff"] < 0 and hi < 0) else "FAIL"


def rule3(jepa_acc, chain_acc, n_pairs) -> str:
    if not n_pairs or jepa_acc is None or chain_acc is None:
        return "INCONCLUSIVE"
    return "PASS" if jepa_acc >= chain_acc else "FAIL"


def rule4(checkpoints) -> str:
    cps = list(checkpoints)
    if not cps:
        return "INCONCLUSIVE"
    return "PASS" if all(bool(c.get("within_bounds")) for c in cps) else "FAIL"


def rule5(det: dict | None, quality: str) -> str:
    if quality != "gold" or not det:
        return "INCONCLUSIVE"
    v = det.get("verdict")
    return v if v in VERDICTS else "INCONCLUSIVE"


def combine(verdicts) -> str:
    """Across seeds: any FAIL → FAIL; all PASS → PASS; else INCONCLUSIVE."""
    v = list(verdicts)
    if not v:
        return "INCONCLUSIVE"
    if "FAIL" in v:
        return "FAIL"
    return "PASS" if all(x == "PASS" for x in v) else "INCONCLUSIVE"


def overall(verdicts: dict) -> str:
    """G1 passes only if all five PASS; any FAIL → FAIL; otherwise INCONCLUSIVE."""
    vs = [verdicts[k] for k in sorted(verdicts)]
    if vs and all(v == "PASS" for v in vs):
        return "PASS"
    return "FAIL" if "FAIL" in vs else "INCONCLUSIVE"


# ---- JEPA-derived signals -------------------------------------------------------------------------

def value_zeno_scores(v, threshold: float = SUCCESS_V, w: int = 4, seed: int = 0,
                      task_id: str = "") -> np.ndarray:
    """Per-step Zeno score on the value-head series: threshold − CI upper of v_∞ at a converging /
    oscillating window whose CI upper is below the threshold (converging short); −∞ elsewhere, so
    the tuned θ is never looser than 'CI upper < threshold' (the proxy's floor-0 rule)."""
    from . import progress as G
    v = np.asarray(v, dtype=float)
    out = np.full(len(v), -np.inf)
    for t in range(w - 1, len(v)):
        z = G.zeno(v[t - w + 1:t + 1], threshold, seed=G._seed(seed, task_id, t))
        hi = z["ci"][1]
        if z["status"] in G.FLAGGING and hi is not None and math.isfinite(hi) and hi < threshold:
            out[t] = threshold - hi
    return out


def jepa_workflow_values(jepa: Jepa, ds: P.Dataset, chain_vals: dict, train_ids,
                         prefix=P.OUTCOME_PREFIX) -> dict:
    """Mean value-head P(success) at the outcome prefix over the labeled train tasks of each
    (type, workflow) group the chain also values — train-derived, like the chain's."""
    from . import chains as C
    groups = {}
    for t in train_ids:
        if P.label(ds.tasks[t]) is None:
            continue
        g = C._group_key(ds, t)
        if g in chain_vals:
            groups.setdefault(g, []).append(
                jepa.outcome_proba(P.prefix_of(ds.steps[t], prefix), ds.tasks[t]))
    return {g: {"value": float(np.mean(v)), "n": len(v)} for g, v in groups.items()}


def probe_arrays(jepa: Jepa, ds: P.Dataset, ids, prefix=P.OUTCOME_PREFIX) -> dict:
    """train.linear_probes input from cached scores: per-step z / raw features / targets, and per
    labeled main-stream task z at the outcome prefix (not the last step — that would leak)."""
    from . import features as F
    z, x, y, ph, fb = [], [], [], [], []
    zl, xl, ol = [], [], []
    for t in ids:
        st = ds.steps[t]
        r = jepa.cache[t]
        X = F.task_features(st)
        z.append(r["z"])
        x.append(X)
        y.append(F.next_action_targets(st))
        ph.append([F.phase_index(s) for s in st])
        fb.append([F.fail_bucket(s) for s in st])
        lab = P.label(ds.tasks[t])
        k = len(P.prefix_of(st, prefix))
        if lab is not None and ds.tasks[t].get("agent") is None and k > 0:
            zl.append(r["z"][k - 1])
            xl.append(X[k - 1])
            ol.append(lab)
    d = jepa.wm.cfg.d
    cat = (lambda a, shape: np.concatenate(a) if a else np.zeros(shape))
    return {"z": cat(z, (0, d)), "x": cat(x, (0, F.N_FEAT)),
            "y_next": cat(y, (0,)).astype(np.int64),
            "phase": np.concatenate([np.asarray(p, np.int64) for p in ph]) if ph else np.zeros(0, np.int64),
            "fail_b": np.concatenate([np.asarray(p, np.int64) for p in fb]) if fb else np.zeros(0, np.int64),
            "z_prefix": np.array(zl) if zl else np.zeros((0, d)),
            "x_prefix": np.array(xl) if xl else np.zeros((0, F.N_FEAT)),
            "outcome_prefix": np.array(ol, dtype=float)}


def collapse_checkpoints(run_dir: Path, test_z, cfg, seed: int) -> list:
    """Every evaluation checkpoint: one row per epoch (val, from metrics.jsonl) + test z."""
    from . import jepa as J
    from . import train as T
    rows = []
    for r in T.read_jsonl(Path(run_dir) / "metrics.jsonl"):
        c = r.get("collapse") or {}
        rows.append({"checkpoint": f"epoch {r.get('epoch')} (val)",
                     "effective_rank": c.get("effective_rank"), "sigreg": c.get("sigreg"),
                     "within_bounds": bool(c.get("within_bounds"))})
    d = J.collapse_diagnostics(np.asarray(test_z), erank_min=cfg.erank_min,
                               sigreg_max=cfg.sigreg_max, seed=seed)
    rows.append({"checkpoint": "scored checkpoint (test z)", "effective_rank": d["effective_rank"],
                 "sigreg": d["sigreg"], "within_bounds": bool(d["within_bounds"]), "n": d["n"]})
    return rows


# ---- evaluation of one run ------------------------------------------------------------------------

def fit_baselines(ds: P.Dataset, sp, seed: int = 0) -> dict:
    """Fit E2's baselines on train (val picks hyper-parameters), wrap them with the shared start
    prior, and pick the best next-action and outcome baselines ON VAL."""
    from . import baselines as B
    tr, va = ds.ids("train"), ds.ids("val")
    models = [StartPrior(m.fit(ds, tr, va), sp) for m in B.all_baselines((1, 2))]
    val_ce = {}
    for m in models:
        n = tot = 0.0
        for t in va:
            lp = m.task_logprobs(ds.steps[t], ds.tasks[t])
            tot -= float(np.sum(np.maximum(lp, math.log(P.EPS))))
            n += len(lp)
        val_ce[m.name] = tot / n if n else None
    ok = {k: v for k, v in val_ce.items() if v is not None}
    best = min(ok, key=ok.get) if ok else models[0].name
    vlab = main_ids(ds, "val", labeled=True)
    val_brier = {}
    for m in models:
        if vlab:
            val_brier[m.name] = float(np.mean([
                (float(m.outcome_proba(P.prefix_of(ds.steps[t], P.OUTCOME_PREFIX), ds.tasks[t]))
                 - P.label(ds.tasks[t])) ** 2 for t in vlab]))
    best_o = min(val_brier, key=val_brier.get) if val_brier else models[0].name
    return {"models": models, "val_ce": val_ce, "best": best, "val_brier": val_brier,
            "best_outcome": best_o, "n_val_labeled": len(vlab),
            "outcome_note": "" if val_brier else "no labeled val tasks: the prior (train base rate) by rule"}


UNTRAINED = "value head untrained (0 labelled train tasks)"


class ViewMismatch(ValueError):
    pass


def check_view(summary: dict, view: str | None) -> str | None:
    """The run's training view; raises ViewMismatch when it differs from the requested one."""
    from . import train as T
    rv = summary.get("view") or T.run_view((summary.get("data") or {}).get("source"))
    if view is not None and rv is not None and rv != view:
        raise ViewMismatch(f"run was trained on the {rv!r} view but --view is {view!r}: CE over "
                           f"different sequences is not comparable — pass --view {rv} or retrain")
    return rv


def _run_summary(run) -> dict:
    """summary.json of a run id or directory, without loading weights ({} if absent)."""
    from . import train as T
    p = Path(run)
    if not p.is_dir():
        p = T.data_home() / "runs" / str(run)
    try:
        return json.loads((p / "summary.json").read_text())
    except (OSError, ValueError):
        return {}


def value_head_trained(summary: dict, ds: P.Dataset) -> bool:
    """False when the run saw no labelled train task (its value head is a random projection).
    Read from the run's summary; a run without that record falls back to the dataset."""
    n = (summary.get("data") or {}).get("labelled_train")
    if n is None:
        n = len(main_ids(ds, "train", labeled=True))
    return n > 0


def evaluate_run(run, ds: P.Dataset, base: dict, sp, seed: int = 0, chain=None,
                 crit5: bool = True, view: str | None = None, log=print) -> dict:
    """Score one trained run on test (one pass per model) and apply the five rules."""
    from . import chains as C
    from . import progress as G
    from . import train as T
    t_start = time.perf_counter()
    wm = T.WorldModel.load(run)
    if wm.cfg.macro:
        raise ValueError("macro-step runs are offline-only and not per-step comparable; "
                         "evaluate a run trained with macro=false")
    if wm.cfg.embed:
        raise ValueError("embedding runs key embeddings by task id; not supported by the stream view")
    summ = wm.summary or {}
    run_view = check_view(summ, view)
    trained_v = value_head_trained(summ, ds)
    tr, va, te = ds.ids("train"), ds.ids("val"), ds.ids("test")
    m_tr, m_va, m_te = main_ids(ds, "train"), main_ids(ds, "val"), main_ids(ds, "test")
    jepa = Jepa(wm, sp, base_rate=_base_rate(ds, m_tr))
    jepa.prepare(ds, tr + va)                      # train/val: values, probes, criterion-5 tuning
    # val CE scored like the baselines' (step 0 from the start prior included)
    n_v = tot_v = 0.0
    for t in va:
        lp = jepa.task_logprobs(ds.steps[t], ds.tasks[t])
        tot_v -= float(np.sum(np.maximum(lp, math.log(P.EPS))))
        n_v += len(lp)
    val_ce = tot_v / n_v if n_v else None
    jepa.prepare(ds, te)                           # THE test pass
    models = {m.name: m for m in base["models"]}
    tps = {"JEPA": _TestPass(jepa, ds, te, m_te)}
    for name, m in models.items():
        tps[name] = _TestPass(m, ds, te, m_te)
    best, best_o = base["best"], base["best_outcome"]

    # 1. next action
    ce = {k: _ce(tp, seed) for k, tp in tps.items()}
    pairs = {k: paired(tps["JEPA"], tp, seed) for k, tp in tps.items() if k != "JEPA"}
    pm = tps["JEPA"].main
    c1 = pairs[best]
    c1_main = paired(tps["JEPA"], tps[best], seed, mask=pm) if pm.any() and not pm.all() else None
    v1 = rule1(c1["rel"], c1["rel_ci"][0])

    # 2. outcome (gold only for the verdict). An untrained value head is not a forecast: no
    # number is printed for it (not even the provisional one), whatever labels test has.
    reasons = {}
    n_gold_te = len(main_ids(ds, "test", gold_only=True))
    if trained_v:
        gold = outcome_stats(tps["JEPA"], tps[best_o], ds, main_ids(ds, "test", gold_only=True), seed)
        allq = outcome_stats(tps["JEPA"], tps[best_o], ds, m_te, seed)
        v2 = rule2(gold)
    else:
        gold = {"n": 0, "n_gold_test": n_gold_te, "quality": "inconclusive", "skipped": UNTRAINED}
        allq = {"n": 0, "quality": "inconclusive", "skipped": UNTRAINED}
        v2 = "INCONCLUSIVE"
        reasons[2] = UNTRAINED

    # 3. workflow ranking
    chain_vals = C.workflow_values(ds, m_tr)
    real = C.realized(ds, m_te)
    rk_chain = C.ranking_accuracy(chain_vals, real)
    if trained_v:
        jv = jepa_workflow_values(jepa, ds, chain_vals, m_tr)
        rk_jepa = C.ranking_accuracy(jv, real)
        v3 = rule3(rk_jepa["accuracy"], rk_chain["accuracy"], rk_chain["n_pairs"])
    else:
        jv, rk_jepa = {}, {"accuracy": None, "n_pairs": 0}
        v3 = "INCONCLUSIVE"
        reasons[3] = UNTRAINED
    q3 = P.quality_of(ds.tasks[t].get("outcome_src") for t in m_te if P.label(ds.tasks[t]) is not None)

    # 4. collapse
    test_z = np.concatenate([jepa.cache[t]["z"] for t in te]) if te else np.zeros((0, wm.cfg.d))
    cps = collapse_checkpoints(wm.run_dir, test_z, wm.cfg, seed)
    v4 = rule4(cps)

    # 5. Zeno detector on the value head
    c5, v5 = None, "INCONCLUSIVE"
    labeled_test = [t for t in m_te if P.label(ds.tasks[t]) is not None]
    if not trained_v:
        reasons[5] = UNTRAINED
    if crit5 and labeled_test:
        chain = chain or C.fit_tasks(ds, m_tr)
        tune = m_tr + m_va
        scores = G.detector_scores(ds, tune + m_te, chain, seed=seed)
        dets = ("zeno", "stalled", "rho")
        if trained_v:
            scores["jepa"] = {t: value_zeno_scores(jepa.value_series(t), seed=seed, task_id=t)
                              for t in tune + m_te}
            dets = ("jepa",) + dets
        c5 = G.criterion5(ds, scores, tune, m_te, detectors=dets)
        v5 = rule5(c5["detectors"].get("jepa"), c5["quality"]) if trained_v else "INCONCLUSIVE"

    # probes on test z (train fit, test score), per day
    probes = T.linear_probes(probe_arrays(jepa, ds, tr), probe_arrays(jepa, ds, te),
                             wm.cfg.d_control, seed=seed)
    days = per_day([tps["JEPA"], tps[best]] + [tp for k, tp in tps.items() if k not in ("JEPA", best)])
    run_sp = np.asarray(summ.get("start_prior") or sp)
    return {
        "run_id": wm.run_dir.name, "seed": wm.cfg.seed, "device": wm.cfg.device,
        "w_reg": wm.cfg.w_reg, "best_epoch": summ.get("best_epoch"), "view": run_view,
        "value_head_trained": trained_v, "reasons": reasons,
        "val_ce": val_ce,                                  # incl. step 0, as the baselines
        "val_next_ce_excl_step0": (summ.get("best") or {}).get("val_next_ce"),   # E3's readout
        "start_prior_matches_run": bool(len(run_sp) == len(sp) and np.allclose(run_sp, sp, atol=1e-9)),
        "ce": ce, "paired": pairs, "c1": c1, "c1_main_streams": c1_main,
        "outcome_gold": gold, "outcome_all": allq,
        "ranking": {"jepa": _rk(rk_jepa), "chain": _rk(rk_chain), "quality": q3,
                    "groups": [{"key": list(k), "chain": v["value"], "jepa": jv.get(k, {}).get("value"),
                                "n_train": v["n"], "test": real.get(k)} for k, v in chain_vals.items()]},
        "collapse": cps, "criterion5": c5, "probes": probes, "per_day": days,
        "verdicts": {1: v1, 2: v2, 3: v3, 4: v4, 5: v5},
        "seconds": time.perf_counter() - t_start,
    }


def _rk(r):
    return {"accuracy": r["accuracy"], "n_pairs": r["n_pairs"]}


def _base_rate(ds, ids) -> float:
    ys = [P.label(ds.tasks[t]) for t in ids]
    ys = [y for y in ys if y is not None]
    return (sum(ys) + 0.5) / (len(ys) + 1.0)


# ---- training orchestration -----------------------------------------------------------------------

def train_runs(ds: P.Dataset, source: str, seeds: int = 2, sweep: bool = False, cpu: bool = True,
               base_seed: int = 0, overrides: dict | None = None, tag: str = "steps",
               log=print) -> dict:
    """Train k GPU seeds (seed 0 = the sweep winner when --sweep) + one CPU replay run."""
    from dataclasses import asdict
    from . import train as T
    tds = to_train_dataset(ds, source)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    prefix = f"{stamp}-e4-{tag}"
    cfg = T.TrainConfig.from_dict({**asdict(T.TrainConfig(seed=base_seed)), **(overrides or {})})
    out = {"runs": [], "cpu_run": None, "sweep": None, "timings": {}}
    reuse = None
    if sweep:
        t0 = time.perf_counter()
        sw = T.sweep_w_reg(cfg, tds, run_id=f"{prefix}-sweep-s{base_seed}", log=log)
        out["timings"]["sweep_s"] = time.perf_counter() - t0
        out["sweep"] = sw
        if sw["chosen_w_reg"] is not None:
            cfg = T.TrainConfig.from_dict({**asdict(cfg), "w_reg": sw["chosen_w_reg"]})
            reuse = next(r["run_id"] for r in sw["candidates"] if r["chosen"])
        else:
            log("sweep: no w_reg within the collapse bounds — training with the default w_reg")
    for k in range(seeds):
        s = base_seed + k
        if k == 0 and reuse:
            out["runs"].append(reuse)
            continue
        t0 = time.perf_counter()
        c = T.TrainConfig.from_dict({**asdict(cfg), "seed": s, "device": "gpu"})
        rid = f"{prefix}-s{s}-gpu"
        T.train(c, tds, run_id=rid, log=log)
        out["timings"][rid] = time.perf_counter() - t0
        out["runs"].append(rid)
    if cpu:
        t0 = time.perf_counter()
        c = T.TrainConfig.from_dict({**asdict(cfg), "seed": base_seed, "device": "cpu"})
        rid = f"{prefix}-s{base_seed}-cpu"
        T.train(c, tds, run_id=rid, log=log)
        out["timings"][rid] = time.perf_counter() - t0
        out["cpu_run"] = rid
    out["config"] = asdict(cfg)
    return out


# ---- provenance + persistence ---------------------------------------------------------------------

def git_sha(here: Path | None = None) -> dict:
    """HEAD of the repo holding this package, and whether anything under the repo's top-level
    `src/` differs from it (`:/src` is relative to the repo root, not to `here`)."""
    here = Path(here or Path(__file__).resolve().parent)
    try:
        sha = subprocess.run(["git", "-C", str(here), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=5).stdout.strip() or None
        if sha is None:
            return {"sha": None, "dirty_src": None}
        dirty = subprocess.run(["git", "-C", str(here), "status", "--porcelain", "--", ":/src"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return {"sha": sha, "dirty_src": bool(dirty)}
    except (OSError, subprocess.SubprocessError):
        return {"sha": None, "dirty_src": None}


def _sha256_file(p: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def manifest_info(home: Path | None = None) -> dict:
    """Manifest hash + its split bounds and counts, and the hashes of the data files it built."""
    home = home or P.data_dir()
    p = home / "manifest.json"
    data = {"steps_sha256": _sha256_file(home / "steps.jsonl"),
            "tasks_sha256": _sha256_file(home / "tasks.jsonl")}
    try:
        raw = p.read_bytes()
    except OSError:
        return {"path": str(p), "sha256": None, **data}
    try:
        m = json.loads(raw)
    except ValueError:
        m = {}
    return {"path": str(p), "sha256": hashlib.sha256(raw).hexdigest(), **data,
            "built_at": m.get("built_at"), "git_sha": m.get("git_sha"),
            "counts": m.get("counts"), "splits": m.get("splits"),
            "split_bounds": m.get("split_bounds")}


# ---- test-scoring ledger (append-only) ------------------------------------------------------------

def ledger_path(home: Path | None = None) -> Path:
    return (home or P.data_dir()) / "eval" / "ledger.jsonl"


def _ledger_rows(home: Path | None = None) -> list:
    return P.read_jsonl(ledger_path(home))


def _append_ledger(rows: list, home: Path | None = None) -> None:
    from . import train as T
    p = ledger_path(home)
    T._mkdir_private(p.parent.parent)
    T._mkdir_private(p.parent)
    for r in rows:
        T._append_private(p, json.dumps(r, sort_keys=True))


def backfill_ledger(home: Path | None = None) -> int:
    """First use: one ledger line per test scoring recorded in the saved scorecards (real data
    only; data-file hashes were not recorded then, so they are null). No-op once a ledger exists."""
    if _ledger_rows(home):
        return 0
    d = ledger_path(home).parent
    rows = []
    for f in sorted(d.glob("*.json")):
        try:
            c = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if c.get("kind") != "worldmodel-g1-scorecard" or str(c.get("source", "")).startswith("synthetic"):
            continue
        m = c.get("manifest") or {}
        runs = list(c.get("runs") or []) + ([c["cpu_replay_run"]] if c.get("cpu_replay_run") else [])
        for r in runs:
            rows.append({"ts": c.get("created"), "invocation": c.get("created"),
                         "manifest_sha256": m.get("sha256"), "steps_sha256": m.get("steps_sha256"),
                         "tasks_sha256": m.get("tasks_sha256"), "run_id": r,
                         "view": c.get("view"), "git_sha": (c.get("git") or {}).get("sha"),
                         "card": f.name, "backfilled": True})
    if rows:
        _append_ledger(rows, home)
    return len(rows)


def record_test_scoring(card: dict, home: Path | None = None) -> dict:
    """Append one ledger line per run whose test split was scored by this invocation, and count
    the scorings recorded for this manifest (this one included)."""
    backfill_ledger(home)
    m = card.get("manifest") or {}
    runs = list(card.get("runs") or []) + ([card["cpu_replay_run"]] if card.get("cpu_replay_run") else [])
    _append_ledger([{"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "invocation": card.get("created"), "manifest_sha256": m.get("sha256"),
                     "steps_sha256": m.get("steps_sha256"), "tasks_sha256": m.get("tasks_sha256"),
                     "run_id": r, "view": card.get("view"),
                     "git_sha": (card.get("git") or {}).get("sha")} for r in runs], home)
    same = [r for r in _ledger_rows(home) if r.get("manifest_sha256") == m.get("sha256")]
    return {"path": str(ledger_path(home)), "manifest_sha256": m.get("sha256"),
            "scorings": len(same), "invocations": len({r.get("invocation") for r in same})}


def save(card: dict, home: Path | None = None) -> Path:
    from . import train as T
    from .readout import _clean, _json_default
    d = (home or P.data_dir()) / "eval"
    T._mkdir_private(d.parent)
    T._mkdir_private(d)
    run = (card.get("runs") or ["norun"])[0]
    path = d / f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{run}.json"
    T._write_private(path, json.dumps(_clean(card), indent=1, default=_json_default, sort_keys=True))
    return path


# ---- the scorecard --------------------------------------------------------------------------------

def scorecard(ds: P.Dataset, desc: str, runs: list, cpu_run=None, seed: int = 0, training=None,
              view: str = "streams", log=print) -> dict:
    sp = start_prior(ds, ds.ids("train"))
    t0 = time.perf_counter()
    base = fit_baselines(ds, sp, seed)
    t_base = time.perf_counter() - t0
    for r in list(runs) + ([cpu_run] if cpu_run else []):      # refuse before ANY test pass
        check_view(_run_summary(r), view)
    per = [evaluate_run(r, ds, base, sp, seed, view=view, log=log) for r in runs]
    replay = (evaluate_run(cpu_run, ds, base, sp, seed, crit5=False, view=view, log=log)
              if cpu_run else None)
    verdicts = {k: combine(p["verdicts"][k] for p in per) for k in range(1, 6)}
    labs = {s: {"gold": len(main_ids(ds, s, gold_only=True)),
                "labeled": len(main_ids(ds, s, labeled=True)), "tasks": len(main_ids(ds, s))}
            for s in P.SPLITS}
    blockers = []
    untrained = any(not p["value_head_trained"] for p in per)
    for k in (2, 3, 5):
        if verdicts[k] == "INCONCLUSIVE" and untrained:
            blockers.append(f"C{k}: {UNTRAINED} — label train tasks, then rebuild and retrain")
    for k in (2, 5):
        if verdicts[k] == "INCONCLUSIVE" and labs["test"]["gold"] == 0:
            blockers.append(f"C{k}: {labs['test']['gold']} gold-labeled test tasks "
                            f"(gold labels needed: `apex-router labels review`, then rebuild)")
    if verdicts[3] == "INCONCLUSIVE" and not any(p["ranking"]["chain"]["n_pairs"] for p in per):
        blockers.append("C3: no workflow pair with ≥ 5 labeled test tasks each and different "
                        "realized success")
    for k in (1, 4):
        if verdicts[k] == "INCONCLUSIVE":
            blockers.append(f"C{k}: no scorable checkpoint/steps")
    rels = [p["c1"]["rel"] for p in per if p["c1"]["rel"] is not None]
    synthetic = desc.startswith("synthetic")
    card = {
        "kind": "worldmodel-g1-scorecard", "schema": 1,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": desc, "view": view, "git": git_sha(),
        "manifest": None if synthetic else manifest_info(),
        "runs": [p["run_id"] for p in per], "seeds": [p["seed"] for p in per],
        "cpu_replay_run": replay["run_id"] if replay else None, "training": training,
        "splits": {s: {"sequences": len(ds.ids(s)), "tasks": labs[s]["tasks"],
                       "steps": sum(len(ds.steps[t]) for t in ds.ids(s)),
                       "sessions": len({ds.sid(t) for t in ds.ids(s)}),
                       "labeled": labs[s]["labeled"], "gold": labs[s]["gold"]} for s in P.SPLITS},
        "start_prior": sp.tolist(),
        "baselines": {"val_ce": base["val_ce"], "best_next_action": base["best"],
                      "val_brier": base["val_brier"], "best_outcome": base["best_outcome"],
                      "outcome_note": base["outcome_note"], "seconds": t_base},
        "per_run": per, "cpu_replay": replay,
        "c1_rel_seeds": {"values": rels, "mean": float(np.mean(rels)) if rels else None,
                         "spread": float(np.max(rels) - np.min(rels)) if len(rels) > 1 else None},
        "verdicts": verdicts, "g1": overall(verdicts), "blockers": blockers, "rules": RULES,
        "seed_rule": "a criterion PASSes only if it PASSes on every seed; any FAIL → FAIL",
        "expected": "first G1 attempt on today's data is expected to FAIL on at least one "
                    "criterion (RESEARCH-FIT-BACKLOG P6) — this card says which and why",
    }
    # every scoring of the real test split is recorded (synthetic data has no test to protect)
    card["ledger"] = None if synthetic else record_test_scoring(card)
    return card


def _f(x, d=3):
    if x is None:
        return "—"
    if isinstance(x, float) and math.isnan(x):
        return "nan"
    if isinstance(x, float) and math.isinf(x):
        return "∞"
    return f"{x:.{d}f}"


def _ci(ci, d=3):
    lo, hi = ci if ci else (None, None)
    return "[—]" if lo is None else f"[{_f(lo, d)}, {_f(hi, d)}]"


def _pct(x, d=1):
    return "—" if x is None else f"{100 * x:.{d}f}%"


def _pci(ci, d=1):
    lo, hi = ci if ci else (None, None)
    return "[—]" if lo is None else f"[{_pct(lo, d)}, {_pct(hi, d)}]"


def render(card: dict) -> str:
    per = card["per_run"]
    p0 = per[0]
    sp = card["splits"]
    L = ["G1 scorecard (P6 E4) — JEPA world model vs E2 baselines, test split scored once",
         f"source: {card['source']}  (view: {card['view']}; git {str((card['git'] or {}).get('sha'))[:10]})",
         "split: " + " · ".join(f"{s} {v['tasks']} tasks / {v['sequences']} sequences / {v['steps']} "
                                f"steps / {v['sessions']} sessions (labels {v['labeled']}, gold "
                                f"{v['gold']})" for s, v in sp.items()),
         "runs: " + ", ".join(f"{p['run_id']} (seed {p['seed']}, {p['device']}, w_reg "
                              f"{p['w_reg']:g}, best epoch {p['best_epoch']})" for p in per)]
    if card.get("cpu_replay_run"):
        L.append(f"cpu replay run (exact replay, not in the verdict): {card['cpu_replay_run']}")
    b = card["baselines"]
    L += ["", "next-action CE on test (nats/step, 95% session-cluster CI); step 0 of every sequence "
              "from one train start prior",
          "  (val CE: every val step incl. step 0, the same way for every model)",
          f"  {'model':<26} {'val CE':>7}   {'test CE':<24} n steps / sessions"]
    for name, c in p0["ce"].items():
        vce = p0["val_ce"] if name == "JEPA" else b["val_ce"].get(name)
        tag = "  <- best on val" if name == b["best_next_action"] else ""
        L.append(f"  {name:<26} {_f(vce):>7}   {_f(c['value'])} {_ci(c['ci']):<17} "
                 f"{c['n']} / {c['n_sessions']}{tag}")
    L.append("  JEPA − baseline (paired): " + "; ".join(
        f"{k} {_f(v['diff'])} {_ci(v['diff_ci'])} rel {_pct(v['rel'])}" for k, v in p0["paired"].items()))
    L += ["", "criteria (one line each; verdict across seeds: any FAIL → FAIL, PASS only if every seed passes)"]
    v = card["verdicts"]
    c1 = p0["c1"]
    seeds = card["c1_rel_seeds"]
    sl = ""
    if len(per) > 1:
        sl = ("; seeds " + ", ".join(f"s{p['seed']} {_pct(p['c1']['rel'])} {_pci(p['c1']['rel_ci'])} "
                                     f"{p['verdicts'][1]}" for p in per)
              + f" — mean {_pct(seeds['mean'])} ± {_pct((seeds['spread'] or 0) / 2)} (half-range)")
    L.append(f"C1 {v[1]:<12} next-state CE: JEPA {_f(p0['ce']['JEPA']['value'])} vs "
             f"{b['best_next_action']} {_f(p0['ce'][b['best_next_action']]['value'])}; rel "
             f"{_pct(c1['rel'])} CI {_pci(c1['rel_ci'])}, diff {_f(c1['diff'])} {_ci(c1['diff_ci'])}; "
             f"n {c1['n']} steps / {c1['n_sessions']} sessions{sl}")
    L.append(f"   rule: {card['rules'][1]}")
    if p0.get("c1_main_streams"):
        m = p0["c1_main_streams"]
        L.append(f"   (info) main streams only: rel {_pct(m['rel'])} {_pci(m['rel_ci'])}, n {m['n']} steps")
    g, a = p0["outcome_gold"], p0["outcome_all"]
    if g.get("skipped"):
        L.append(f"C2 {v[2]:<12} outcome Brier: not scored — {g['skipped']} "
                 f"({g.get('n_gold_test', 0)} gold-labeled test tasks)")
    elif g.get("n"):
        L.append(f"C2 {v[2]:<12} outcome Brier (gold, first {P.OUTCOME_PREFIX} steps): JEPA "
                 f"{_f(g['a']['brier'])} vs {g['b']['model']} {_f(g['b']['brier'])}; diff {_f(g['diff'])} "
                 f"{_ci(g['diff_ci'])}; ECE {_f(g['a']['ece'])} vs {_f(g['b']['ece'])}; n {g['n']} tasks / "
                 f"{g['n_sessions']} sessions")
    else:
        L.append(f"C2 {v[2]:<12} outcome Brier: 0 gold-labeled test tasks (n 0) — no gold, no verdict")
    if a.get("n") and a.get("quality") != "gold":
        L.append(f"   provisional (weak labels, NOT used): JEPA {_f(a['a']['brier'])} vs {a['b']['model']} "
                 f"{_f(a['b']['brier'])}; diff {_f(a['diff'])} {_ci(a['diff_ci'])}; n {a['n']}")
    L.append(f"   rule: {card['rules'][2]}; outcome baseline chosen on val: {b['best_outcome']}"
             + (f" ({b['outcome_note']})" if b["outcome_note"] else ""))
    rk = p0["ranking"]
    jr = (f"not scored — {p0['reasons'][3]}" if 3 in p0["reasons"]
          else _pct(rk['jepa']['accuracy'], 0))
    L.append(f"C3 {v[3]:<12} workflow ranking: JEPA {jr} vs chain "
             f"{_pct(rk['chain']['accuracy'], 0)} over {rk['chain']['n_pairs']} pairs "
             f"(labels: {rk['quality']}); groups valued on train: {len(rk['groups'])}")
    L.append(f"   rule: {card['rules'][3]}")
    for p in per:
        bad = [c for c in p["collapse"] if not c["within_bounds"]]
        last = p["collapse"][-1]
        L.append(f"C4 {p['verdicts'][4] if len(per) > 1 else v[4]:<12} collapse ({p['run_id']}): "
                 f"{len(p['collapse']) - len(bad)}/{len(p['collapse'])} checkpoints within bounds; test z "
                 f"erank {_f(last['effective_rank'], 1)}, SIGReg {_f(last['sigreg'])}"
                 + (f"; outside: " + ", ".join(f"{c['checkpoint']} (erank {_f(c['effective_rank'], 1)}, "
                                                f"SIGReg {_f(c['sigreg'])})" for c in bad[:6])
                    + (" …" if len(bad) > 6 else "") if bad else ""))
    if len(per) > 1:
        L.append(f"C4 {v[4]:<12} (across seeds)")
    L.append(f"   rule: {card['rules'][4]}")
    alone = ", ".join(f"{p['run_id']} {'within' if p['collapse'][-1]['within_bounds'] else 'OUTSIDE'}"
                      for p in per)
    L.append(f"   (info) the scored checkpoint alone (its test z): {alone}; note the w_reg sweep "
             "checks the bounds at the best epoch only, the rule above checks every epoch")
    c5 = p0["criterion5"]
    if 5 in p0["reasons"]:
        L.append(f"C5 {v[5]:<12} Zeno on value head: not scored — {p0['reasons'][5]}")
        if c5:
            dz = c5["detectors"].get("zeno", {})
            L.append(f"   proxy Zeno (tests/errors) for reference ({c5['quality']}): {dz.get('verdict')}, "
                     f"wins {dz.get('wins')} / losses {dz.get('losses')}, FPR {_pct(dz.get('fpr', {}).get('rate'))}")
    elif c5:
        dj = c5["detectors"].get("jepa", {})
        L.append(f"C5 {v[5]:<12} Zeno on value head ({c5['quality']}): wins {dj.get('wins')} / losses "
                 f"{dj.get('losses')}, win share {_pct(dj.get('win_share', {}).get('rate'))} "
                 f"{_pci(dj.get('win_share', {}).get('ci'))}, FPR {_pct(dj.get('fpr', {}).get('rate'))} "
                 f"{_pci(dj.get('fpr', {}).get('ci'))}, median saved {_f(dj.get('median_saved'), 1)}; "
                 f"n bad {c5['n_bad']} / good {c5['n_good']}")
        dz = c5["detectors"].get("zeno", {})
        L.append(f"   proxy Zeno (tests/errors) for reference: {dz.get('verdict')}, wins {dz.get('wins')} / "
                 f"losses {dz.get('losses')}, FPR {_pct(dz.get('fpr', {}).get('rate'))}")
    else:
        L.append(f"C5 {v[5]:<12} Zeno detector: 0 labeled test tasks (gold {sp['test']['gold']}) — not scored")
    L.append(f"   rule: {card['rules'][5]}")
    L += ["", f"G1: {card['g1']}  ({', '.join(f'C{k} {x}' for k, x in v.items())})"]
    for bl in card["blockers"]:
        L.append(f"  blocked: {bl}")
    L.append(f"  {card['expected']}")
    led = card.get("ledger")
    if led:
        L.append(f"  test split scored {led['scorings']} times for this manifest (ledger; "
                 f"{led['invocations']} invocations): {led['path']}")
    # probes
    L += ["", "linear probes on test z (fit on train z, scored on test; raw = same probe on the step features)",
          "  (phase and fail_bucket are model inputs: those probes are near-tautological and only "
          "check that z keeps the state; outcome rows read the outcome prefix, never the last step)"]
    for target, by in p0["probes"].items():
        for inp, r in by.items():
            if r.get("skipped"):
                L.append(f"  {target:<12} {inp:<9} skipped (n_train {r['n_train']}, n_test {r['n_val']})")
                continue
            ex = f" Brier {_f(r['brier'])} (prior {_f(r['prior_brier'])})" if "brier" in r else ""
            L.append(f"  {target:<12} {inp:<9} n {r['n_val']:<6} CE {_f(r['ce'])} (prior {_f(r['prior_ce'])}) "
                     f"acc {_f(r['acc'])}{ex}")
    # per day
    names = ["JEPA", b["best_next_action"]] + [k for k in p0["ce"] if k not in ("JEPA", b["best_next_action"])]
    L += ["", f"per day (test, UTC; CE nats/step, no CI — a day is a handful of sessions; "
              f"n < {DAY_MIN_N} steps flagged too few)"]
    for r in p0["per_day"]:
        L.append(f"  {r['day']}  n {r['n']:<5} sessions {r['n_sessions']:<3} "
                 + "  ".join(f"{n} {_f(r.get(n))}" for n in names)
                 + ("  [too few]" if r["n"] < DAY_MIN_N else ""))
    if card.get("cpu_replay"):
        c = card["cpu_replay"]
        L += ["", f"cpu replay {c['run_id']}: test CE {_f(c['ce']['JEPA']['value'])}, C1 rel "
                  f"{_pct(c['c1']['rel'])} {_pci(c['c1']['rel_ci'])}, verdicts "
                  + ", ".join(f"C{k} {x}" for k, x in c["verdicts"].items()
                              if not (str(k) == "5" and not c.get("criterion5")))
                  + " (C5 not re-scored; not in the verdict)"]
    tr = card.get("training") or {}
    if tr.get("sweep"):
        L += ["", "w_reg sweep (val only): " + "; ".join(
            f"{r['w_reg']:g}: val CE {_f(r['val_next_ce'])}, erank {_f(r['effective_rank'], 1)}, SIGReg "
            f"{_f(r['sigreg'])}{' within' if r['within_bounds'] else ' OUTSIDE'}{' <- chosen' if r['chosen'] else ''}"
            for r in tr["sweep"]["candidates"])]
    return "\n".join(L)


# ---- CLI ------------------------------------------------------------------------------------------

def cmd_evaluate(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="apex-router worldmodel evaluate",
                                 description="G1 scorecard: JEPA vs baselines on the test split (scored once).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run", action="append", help="evaluate a saved run (repeat for several seeds)")
    g.add_argument("--train", action="store_true", help="train on train/val first (default config)")
    ap.add_argument("--seeds", type=int, default=2, help="GPU seeds to train with --train (default 2)")
    ap.add_argument("--sweep", action="store_true", help="run train.sweep_w_reg first; seed 0 = the winner")
    ap.add_argument("--no-cpu", action="store_true", help="skip the --device cpu replay run")
    ap.add_argument("--epochs", type=int, help="override TrainConfig.epochs (default: early stop)")
    ap.add_argument("--seed", type=int, default=0, help="first training seed and the bootstrap seed")
    ap.add_argument("--synthetic", type=int, metavar="N", help="N synthetic sessions (fixtures.py)")
    ap.add_argument("--view", choices=("streams", "task"), default="streams",
                    help="sequence unit: one per (task, agent) stream (default) or the raw task order")
    ap.add_argument("--no-save", action="store_true", help="do not write the scorecard JSON")
    ap.add_argument("--json", action="store_true")
    g.add_argument("--backfill-ledger", action="store_true",
                   help="only seed eval/ledger.jsonl from the saved scorecards (scores nothing)")
    a = ap.parse_args(list(argv or []))
    if a.backfill_ledger:
        n = backfill_ledger()
        print(f"ledger: {n} line(s) backfilled into {ledger_path()}" if n else
              f"ledger: nothing to backfill ({ledger_path()} exists or no real-data scorecards)")
        return 0
    from .jepa import mlx_available
    if not mlx_available():
        print("mlx is not installed: `pip install 'apex-router[worldmodel]'` (Apple Silicon only)",
              file=sys.stderr)
        return 2
    if a.synthetic:
        from .fixtures import dataset
        raw, _ = dataset(a.synthetic, seed=0)
        desc, tag = f"synthetic: {a.synthetic} sessions, fixtures seed 0 (known dynamics, labels gold)", "synthetic"
    else:
        raw = P.load()
        desc, tag = f"{P.data_dir() / 'steps.jsonl'} (+ tasks.jsonl, manifest.json)", "steps"
    ds = stream_view(raw) if a.view == "streams" else raw
    if not ds.ids("train") or not ds.ids("test"):
        print("apex-router worldmodel evaluate: no train/test sequences — build the dataset first "
              "or pass --synthetic N", file=sys.stderr)
        return 1
    quiet = (lambda m: None) if a.json else (lambda m: print(m, file=sys.stderr))
    training, cpu_run = None, None
    t0 = time.perf_counter()
    if a.train:
        ov = {"epochs": a.epochs} if a.epochs else {}
        src = (f"{P.data_dir() / 'steps.jsonl'}:{a.view}" if tag == "steps"
               else f"synthetic-fixtures:{a.synthetic}")
        training = train_runs(ds, src,
                              seeds=max(1, a.seeds), sweep=a.sweep, cpu=not a.no_cpu,
                              base_seed=a.seed, overrides=ov, tag=tag, log=quiet)
        runs, cpu_run = training["runs"], training["cpu_run"]
    else:
        runs = a.run
    t_train = time.perf_counter() - t0
    try:
        card = scorecard(ds, desc, runs, cpu_run=cpu_run, seed=a.seed, training=training,
                         view=a.view, log=quiet)
    except ViewMismatch as e:
        print(f"apex-router worldmodel evaluate: {e}", file=sys.stderr)
        return 2
    card["timings"] = {"train_s": t_train, "evaluate_s": time.perf_counter() - t0 - t_train}
    if not a.no_save:
        card["path"] = str(save(card))
    if a.json:
        from .readout import _clean, _json_default
        print(json.dumps(_clean(card), indent=1, default=_json_default, sort_keys=True))
    else:
        print(render(card))
        print(f"\ntimings: train {card['timings']['train_s']:.1f} s, evaluate "
              f"{card['timings']['evaluate_s']:.1f} s" + (f"\nscorecard JSON: {card['path']}" if card.get("path") else ""))
    return 0


COMMANDS = {"evaluate": cmd_evaluate}
