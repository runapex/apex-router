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

**Attempt-2 ablations** (`docs/research/2026-10-08-p6-attempt-2-predeclaration.md`), each a
flag, all off by default so attempt 1 replays: `--features a2f` (A2-F, JEPA inputs gain the
cross-step task state), `--err-states` (A2-E, the absorbing chain — C3 values, the Markov outcome
readout, the ρ detector — gets `<class>!err` states; the card reports both chains' E[steps] ± sd
vs observed on train), `--regime` (A2-D, the day regime for the logistic baseline AND the JEPA),
`--sweep-every-epoch` (A2-W, the w_reg sweep rejects a candidate with any epoch outside the
bounds). The card's `ablations` records which are on. A run whose input flags (features, regime)
differ from the requested ones is refused before any test pass, like the view guard.

**C4 collapse recipe** (`docs/research/2026-10-08-p6-c4-warmup-recipe.md`). `--train` takes
`--config JSON|PATH` (TrainConfig fields, merged before the sweep and seeds) and the explicit
recipe flags `--z-norm`, `--w-reg-schedule`, `--w-reg-start`, `--w-reg-anneal-epochs`,
`--lr-warmup-epochs`, `--rank-floor-init`; precedence is explicit flag > `--config` > default
(`eval_config`). With `--run` the same flags declare the recipe the run must have been trained
with: a run whose `summary.recipe` (else config; older runs = the defaults) differs in any
active field is refused before any test pass (`RecipeMismatch`, a `ViewMismatch`). The card's
`recipe` block and every ledger line record the full declared recipe; the C4 lines state which
z SIGReg was measured on, and with a z_norm the post-map z (C4's reading) is printed beside the
pre-map raw encoder output (best-epoch val, scored test), informational only.

**Declared n-floors** (attempt-2 pre-declaration). C2 is INCONCLUSIVE when the test split has
< 10 gold tasks; C5 when it has < 10 gold or < 3 bad gold tasks — whatever the numbers
(`floor_reason`: "below declared floor: …"). The card prints each floor under its rule.

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


GOLD_TEST_FLOOR = 10        # attempt-2 pre-declaration: < 10 gold test tasks -> C2/C5 INCONCLUSIVE
BAD_GOLD_FLOOR = 3          # C5 additionally needs >= 3 bad (failed) gold test tasks
FLOORS = {2: f"INCONCLUSIVE by rule if < {GOLD_TEST_FLOOR} gold test tasks",
          5: f"INCONCLUSIVE by rule if < {GOLD_TEST_FLOOR} gold test tasks or < {BAD_GOLD_FLOOR} "
             "bad gold test tasks"}
FLOOR_SOURCE = "declared floor (docs/research/2026-10-08-p6-attempt-2-predeclaration.md)"


def floor_reason(n_gold, n_bad=None) -> str | None:
    """Why a gold-only criterion is below its declared floor (None = at or above it). ``n_bad``
    is checked only when given (criterion 5)."""
    if n_gold is not None and n_gold < GOLD_TEST_FLOOR:
        return f"below declared floor: n gold test tasks < {GOLD_TEST_FLOOR} (n = {n_gold})"
    if n_bad is not None and n_bad < BAD_GOLD_FLOOR:
        return f"below declared floor: n bad gold test tasks < {BAD_GOLD_FLOOR} (n = {n_bad})"
    return None


def rule2(gold: dict) -> str:
    """Gold only; INCONCLUSIVE below the declared floor (``gold["n"]`` < 10) whatever the numbers."""
    if not gold or not gold.get("n") or gold.get("quality") != "gold":
        return "INCONCLUSIVE"
    if floor_reason(gold["n"]):
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


def rule5(det: dict | None, quality: str, n_gold: int | None = None, n_bad: int | None = None) -> str:
    """Gold only; INCONCLUSIVE below the declared floor (< 10 gold or < 3 bad gold test tasks)
    whatever the detector's numbers. The counts are checked when given (evaluate_run always
    passes them)."""
    if quality != "gold" or not det:
        return "INCONCLUSIVE"
    if floor_reason(n_gold, n_bad):
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
    labeled main-stream task z at the outcome prefix (not the last step — that would leak). The
    raw reference uses the run's own input columns (features / regime from its config)."""
    from . import features as F
    z, x, y, ph, fb = [], [], [], [], []
    zl, xl, ol = [], [], []
    feats, reg = _cfg_inputs(jepa.wm.cfg)
    nf = F.n_feat(feats, reg)
    for t in ids:
        st = ds.steps[t]
        r = jepa.cache[t]
        X = F.task_features(st, feats, reg)
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
    return {"z": cat(z, (0, d)), "x": cat(x, (0, nf)),
            "y_next": cat(y, (0,)).astype(np.int64),
            "phase": np.concatenate([np.asarray(p, np.int64) for p in ph]) if ph else np.zeros(0, np.int64),
            "fail_b": np.concatenate([np.asarray(p, np.int64) for p in fb]) if fb else np.zeros(0, np.int64),
            "z_prefix": np.array(zl) if zl else np.zeros((0, d)),
            "x_prefix": np.array(xl) if xl else np.zeros((0, nf)),
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

def fit_baselines(ds: P.Dataset, sp, seed: int = 0, regime: bool = False,
                  err_states: bool = False) -> dict:
    """Fit E2's baselines on train (val picks hyper-parameters), wrap them with the shared start
    prior, and pick the best next-action and outcome baselines ON VAL. ``regime`` (A2-D) and
    ``err_states`` (A2-E) as in ``baselines.all_baselines``."""
    from . import baselines as B
    tr, va = ds.ids("train"), ds.ids("val")
    models = [StartPrior(m.fit(ds, tr, va), sp)
              for m in B.all_baselines((1, 2), regime=regime, err_states=err_states)]
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


class FeatureMismatch(ViewMismatch):
    """A run trained on other inputs (A2-F features / A2-D regime) than the scoring asks for."""


def run_inputs(summary: dict) -> tuple[str, bool]:
    """(features, regime) a run was trained with: summary, else its config; attempt-1 runs (no
    record) are ("a1", False)."""
    cfg = summary.get("config") or {}
    feats = summary.get("features") or cfg.get("features") or "a1"
    reg = summary.get("regime")
    if reg is None:
        reg = cfg.get("regime", False)
    return str(feats), bool(reg)


def _cfg_inputs(cfg) -> tuple[str, bool]:
    """(features, regime) of a loaded run's config (attempt-1 configs: "a1", False)."""
    return str(getattr(cfg, "features", "a1") or "a1"), bool(getattr(cfg, "regime", False))


def check_inputs(summary: dict, features: str = "a1", regime: bool = False) -> tuple[str, bool]:
    """Raise FeatureMismatch when the run's input flags differ from the requested ones."""
    rf, rr = run_inputs(summary)
    if rf != features or rr != bool(regime):
        raise FeatureMismatch(
            f"run was trained with features={rf!r}, regime={rr} but this scoring asks for "
            f"features={features!r}, regime={bool(regime)}: pass --features {rf}"
            f"{' --regime' if rr else ''} (or retrain) — scores over different inputs are not "
            "the registered comparison")
    return rf, rr


class RecipeMismatch(ViewMismatch):
    """A run trained under another C4 collapse recipe (z_norm, w_reg schedule, init, LR warm-up)
    than the scoring declares."""


def _recipe_value(k: str, v):
    """Canonical form of one recipe field (True == "orthogonal"; numbers as float/int)."""
    if k == "rank_floor_init":
        return "orthogonal" if v is True else (v or False)
    if k == "w_reg_start":
        return float(v)
    if k == "w_reg_anneal_epochs":
        return int(v)
    if k == "lr_warmup_epochs":
        return None if v is None else float(v)
    return v


def recipe_request(src=None) -> dict:
    """The full recipe block (``train.RECIPE_KEYS`` + ``default``) of a TrainConfig, a config dict
    or a summary's ``recipe`` block; missing keys are the TrainConfig defaults (attempts 1-2)."""
    from . import train as T
    if src is None:
        src = {}
    get = (src.get if isinstance(src, dict) else lambda k, d=None: getattr(src, k, d))
    out = {k: _recipe_value(k, get(k, getattr(T.TrainConfig, k))) for k in T.RECIPE_KEYS}
    out["default"] = all(out[k] == _recipe_value(k, getattr(T.TrainConfig, k)) for k in T.RECIPE_KEYS)
    return out


def _recipe_effective(r: dict) -> dict:
    """The fields that change training: the anneal knobs are inert under ``const`` and
    ``z_norm_stats`` only matters for whiten/center, so those are compared only when active."""
    from . import train as T
    e = {k: r[k] for k in T.RECIPE_KEYS}
    if e["w_reg_schedule"] != "anneal":
        e.pop("w_reg_start"), e.pop("w_reg_anneal_epochs")
    if e["z_norm"] not in ("whiten", "center"):
        e.pop("z_norm_stats")
    return e


def run_recipe(summary: dict) -> dict:
    """The recipe a run was trained with: summary ``recipe``, else its config; runs from before
    the recipe options (no record) trained the defaults."""
    from . import train as T
    rec = summary.get("recipe")
    if not isinstance(rec, dict) or not any(k in rec for k in T.RECIPE_KEYS):
        rec = summary.get("config") or {}
    return recipe_request(rec)


RECIPE_FLAGS = {"z_norm": "--z-norm", "z_norm_stats": "--config z_norm_stats",
                "w_reg_schedule": "--w-reg-schedule", "w_reg_start": "--w-reg-start",
                "w_reg_anneal_epochs": "--w-reg-anneal-epochs",
                "rank_floor_init": "--rank-floor-init", "lr_warmup_epochs": "--lr-warmup-epochs"}


def check_recipe_match(summary: dict, recipe: dict | None = None) -> dict:
    """Raise RecipeMismatch when the run's collapse recipe differs from the declared one (None =
    the defaults), so a run cannot be scored under a recipe it was not trained with."""
    from . import train as T
    want = _recipe_effective(recipe_request(recipe or {}))
    have_full = run_recipe(summary)
    have = _recipe_effective(have_full)
    diff = [k for k in T.RECIPE_KEYS if want.get(k, "<inert>") != have.get(k, "<inert>")]
    if diff:
        raise RecipeMismatch(
            "run was trained with collapse recipe " + ", ".join(f"{k}={_rv(have.get(k, '<inert>'))}"
                                                                for k in diff)
            + " but this scoring declares " + ", ".join(f"{k}={_rv(want.get(k, '<inert>'))}"
                                                        for k in diff)
            + ": pass " + " ".join(f"{RECIPE_FLAGS[k]} {_rv(have[k])}" for k in diff if k in have)
            + " (or retrain) — a run is scored only under the recipe it was trained with")
    return have_full


def _rv(v) -> str:
    """A recipe value as a CLI token: None -> none, False -> off, 30.0 -> 30."""
    if v is None:
        return "none"
    if v is False:
        return "off"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


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
                 crit5: bool = True, view: str | None = None, log=print,
                 features: str = "a1", regime: bool = False, err_states: bool = False,
                 recipe: dict | None = None) -> dict:
    """Score one trained run on test (one pass per model) and apply the five rules. ``recipe``
    is the declared collapse recipe (None = the defaults); a run trained otherwise is refused."""
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
    check_inputs(summ, features, regime)
    run_rec = check_recipe_match(summ, recipe)
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
    reasons, floors = {}, {}            # floors: a gold criterion below its declared floor
    n_gold_te = len(main_ids(ds, "test", gold_only=True))
    if trained_v:
        gold = outcome_stats(tps["JEPA"], tps[best_o], ds, main_ids(ds, "test", gold_only=True), seed)
        allq = outcome_stats(tps["JEPA"], tps[best_o], ds, m_te, seed)
        v2 = rule2(gold)
        if gold.get("quality") == "gold" and gold.get("n") and floor_reason(gold["n"]):
            floors[2] = floor_reason(gold["n"])
    else:
        gold = {"n": 0, "n_gold_test": n_gold_te, "quality": "inconclusive", "skipped": UNTRAINED}
        allq = {"n": 0, "quality": "inconclusive", "skipped": UNTRAINED}
        v2 = "INCONCLUSIVE"
        reasons[2] = UNTRAINED

    # 3. workflow ranking
    chain_vals = C.workflow_values(ds, m_tr, err_states=err_states)
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
    rec_out = {**run_rec, **{k: v for k, v in (summ.get("recipe") or {}).items()
                             if k in ("lr_warmup_steps", "w_reg_per_epoch", "pre_norm_collapse")}}
    if run_rec["z_norm"] is not None and te and hasattr(wm, "model"):
        # informational: the encoder output before z_norm on the same test sequences (same
        # weights; not part of any rule — C4 reads the normalised z the model actually uses)
        rec_out["pre_norm_collapse_test"] = T._pre_norm_collapse(
            wm.model, wm._windows({t: ds.steps[t] for t in te}), wm.cfg)

    # 5. Zeno detector on the value head
    c5, v5 = None, "INCONCLUSIVE"
    labeled_test = [t for t in m_te if P.label(ds.tasks[t]) is not None]
    if not trained_v:
        reasons[5] = UNTRAINED
    if crit5 and labeled_test:
        chain = chain or C.fit_tasks(ds, m_tr, err_states=err_states)
        tune = m_tr + m_va
        scores = G.detector_scores(ds, tune + m_te, chain, seed=seed)
        dets = ("zeno", "stalled", "rho")
        if trained_v:
            scores["jepa"] = {t: value_zeno_scores(jepa.value_series(t), seed=seed, task_id=t)
                              for t in tune + m_te}
            dets = ("jepa",) + dets
        c5 = G.criterion5(ds, scores, tune, m_te, detectors=dets)
        n_bad_gold = c5["n_bad"] if c5["quality"] == "gold" else 0
        v5 = (rule5(c5["detectors"].get("jepa"), c5["quality"], n_gold=n_gold_te, n_bad=n_bad_gold)
              if trained_v else "INCONCLUSIVE")
        if trained_v and c5["quality"] == "gold" and floor_reason(n_gold_te, n_bad_gold):
            floors[5] = floor_reason(n_gold_te, n_bad_gold)

    # probes on test z (train fit, test score), per day
    probes = T.linear_probes(probe_arrays(jepa, ds, tr), probe_arrays(jepa, ds, te),
                             wm.cfg.d_control, seed=seed)
    days = per_day([tps["JEPA"], tps[best]] + [tp for k, tp in tps.items() if k not in ("JEPA", best)])
    run_sp = np.asarray(summ.get("start_prior") or sp)
    return {
        "run_id": wm.run_dir.name, "seed": wm.cfg.seed, "device": wm.cfg.device,
        "w_reg": wm.cfg.w_reg, "best_epoch": summ.get("best_epoch"), "view": run_view,
        "features": _cfg_inputs(wm.cfg)[0], "regime": _cfg_inputs(wm.cfg)[1],
        "value_head_trained": trained_v, "reasons": reasons, "floors": floors,
        "val_ce": val_ce,                                  # incl. step 0, as the baselines
        "val_next_ce_excl_step0": (summ.get("best") or {}).get("val_next_ce"),   # E3's readout
        "start_prior_matches_run": bool(len(run_sp) == len(sp) and np.allclose(run_sp, sp, atol=1e-9)),
        "ce": ce, "paired": pairs, "c1": c1, "c1_main_streams": c1_main,
        "outcome_gold": gold, "outcome_all": allq,
        "ranking": {"jepa": _rk(rk_jepa), "chain": _rk(rk_chain), "quality": q3,
                    "groups": [{"key": list(k), "chain": v["value"], "jepa": jv.get(k, {}).get("value"),
                                "n_train": v["n"], "test": real.get(k)} for k, v in chain_vals.items()]},
        "collapse": cps, "recipe": rec_out, "criterion5": c5, "probes": probes, "per_day": days,
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
               log=print, sweep_every_epoch: bool = False, w_reg_grid=None) -> dict:
    """Train k GPU seeds (seed 0 = the sweep winner when --sweep) + one CPU replay run.
    ``overrides`` (TrainConfig fields, e.g. ``eval_config``'s) may set the A2-F/A2-D inputs
    (``features``, ``regime``) and the C4 collapse recipe; ``sweep_every_epoch``
    is A2-W; ``w_reg_grid`` the sweep grid (default ``train.W_REG_GRID``). With no qualifying
    w_reg the seeds train the default w_reg and carry the sweep table (its ``note`` says C4 fails
    by construction)."""
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
        sw = T.sweep_w_reg(cfg, tds, values=tuple(w_reg_grid or T.W_REG_GRID),
                           run_id=f"{prefix}-sweep-s{base_seed}", log=log,
                           every_epoch=sweep_every_epoch)
        out["timings"]["sweep_s"] = time.perf_counter() - t0
        out["sweep"] = sw
        if sw["chosen_w_reg"] is not None:
            cfg = T.TrainConfig.from_dict({**asdict(cfg), "w_reg": sw["chosen_w_reg"]})
            reuse = next(r["run_id"] for r in sw["candidates"] if r["chosen"])
        else:
            log(f"sweep: {sw.get('note') or 'no w_reg within the collapse bounds'} — training "
                f"with the default w_reg {cfg.w_reg:g}")
    for k in range(seeds):
        s = base_seed + k
        if k == 0 and reuse:
            out["runs"].append(reuse)
            continue
        t0 = time.perf_counter()
        c = T.TrainConfig.from_dict({**asdict(cfg), "seed": s, "device": "gpu"})
        rid = f"{prefix}-s{s}-gpu"
        T.train(c, tds, run_id=rid, log=log)
        if sweep:
            T.record_sweep(rid, out["sweep"])
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
                     "run_id": r, "view": card.get("view"), "ablations": card.get("ablations"),
                     "recipe": card.get("recipe"),
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

def ablations_of(features: str = "a1", regime: bool = False, err_states: bool = False,
                 sweep_every_epoch: bool | None = None) -> dict:
    """Which attempt-2 ablations a scoring has on (A2-W: None when no sweep ran)."""
    return {"A2-F": features == "a2f", "A2-E": bool(err_states), "A2-D": bool(regime),
            "A2-W": sweep_every_epoch, "features": features}


def annotate_regime(ds: P.Dataset) -> int:
    """A2-D: the causal day-regime annotation on every step, all sequences of a session together."""
    from . import features as F
    return F.annotate_regime(ds.steps, {t: ds.sid(t) for t in ds.tasks})


def scorecard(ds: P.Dataset, desc: str, runs: list, cpu_run=None, seed: int = 0, training=None,
              view: str = "streams", log=print, features: str = "a1", regime: bool = False,
              err_states: bool = False, recipe: dict | None = None) -> dict:
    """``recipe``: the declared C4 collapse recipe (a TrainConfig / config dict / recipe block;
    None = the defaults of attempts 1-2). Every run must have been trained with it."""
    from . import features as F
    F.check_feature_set(features)
    recipe = recipe_request(recipe)
    for r in list(runs) + ([cpu_run] if cpu_run else []):      # refuse before ANY test pass
        check_view(_run_summary(r), view)
        check_inputs(_run_summary(r), features, regime)
        check_recipe_match(_run_summary(r), recipe)
    if regime:
        annotate_regime(ds)
    sp = start_prior(ds, ds.ids("train"))
    t0 = time.perf_counter()
    base = fit_baselines(ds, sp, seed, regime=regime, err_states=err_states)
    t_base = time.perf_counter() - t0
    kw = dict(view=view, log=log, features=features, regime=regime, err_states=err_states,
              recipe=recipe)
    per = [evaluate_run(r, ds, base, sp, seed, **kw) for r in runs]
    replay = (evaluate_run(cpu_run, ds, base, sp, seed, crit5=False, **kw)
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
    for k in (2, 5):
        fl = next((p["floors"][k] for p in per if k in p.get("floors", {})), None)
        if verdicts[k] == "INCONCLUSIVE" and fl and labs["test"]["gold"] > 0:
            blockers.append(f"C{k}: {fl} — {FLOOR_SOURCE}")
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
        "ablations": ablations_of(features, regime, err_states,
                                  (training or {}).get("sweep_every_epoch")),
        "recipe": recipe,
        "chain_steps": None,
        "w_reg_grid": ((training or {}).get("sweep") or {}).get("grid"),
        "c4_by_construction": (((training or {}).get("sweep") or {}).get("note")
                               if ((training or {}).get("sweep") or {}).get("every_epoch") else None),
        "verdicts": verdicts, "g1": overall(verdicts), "blockers": blockers, "rules": RULES,
        "floors": {"2": FLOORS[2], "5": FLOORS[5], "gold_test_min": GOLD_TEST_FLOOR,
                   "bad_gold_test_min": BAD_GOLD_FLOOR, "source": FLOOR_SOURCE},
        "seed_rule": "a criterion PASSes only if it PASSes on every seed; any FAIL → FAIL",
        "expected": "first G1 attempt on today's data is expected to FAIL on at least one "
                    "criterion (RESEARCH-FIT-BACKLOG P6) — this card says which and why",
    }
    if err_states:   # A2-E / H2: both chains' E[steps] ± sd vs observed, train main streams only
        from . import chains as C
        card["chain_steps"] = C.expected_steps_report(ds, main_ids(ds, "train"))
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


Z_NORM_READING = {
    None: "raw z (no z_norm)",
    "center": "centred z (a fixed affine map estimated on train: mean and one global scale, "
              "applied before every head)",
    "whiten": "centred and whitened z (a fixed ZCA map estimated on train, applied before every head)",
    "layernorm": "layer-normalised z (per row, no train statistics)",
}


def schedule_text(r: dict, w=None) -> str:
    """The SIGReg weight schedule of a recipe block (``w`` = the final w_reg, if known)."""
    fin = "final w_reg" if w is None else f"{float(w):g}"
    if r.get("w_reg_schedule") == "anneal":
        return (f"anneal {float(r['w_reg_start']):g} → {fin} over {int(r['w_reg_anneal_epochs'])} "
                f"epochs (geometric), then constant")
    return f"constant {fin}"


def recipe_text(r: dict | None) -> str:
    """One line for the card's recipe section."""
    r = recipe_request(r)
    if r["default"]:
        return ("default (attempts 1-2): z_norm none, SIGReg weight constant, rank_floor_init off, "
                "LR warm-up min(warmup, 10% of updates)")
    zn = r["z_norm"] or "none"
    if r["z_norm"] in ("whiten", "center"):
        zn += f" (training stats {r['z_norm_stats']}, train-set statistics before every checkpoint)"
    lw = ("min(warmup, 10% of updates)" if r["lr_warmup_epochs"] is None
          else f"{r['lr_warmup_epochs']:g} epochs")
    return (f"z_norm {zn}; SIGReg weight {schedule_text(r)}; rank_floor_init "
            f"{_rv(r['rank_floor_init'])}; LR warm-up {lw}")


def _map_pair(p: dict, z_norm) -> str:
    """Post-map (z_norm'd, C4's reading) beside pre-map (raw encoder) collapse for one run."""
    cps = p.get("collapse") or []
    val = next((c for c in cps if c.get("checkpoint") == f"epoch {p.get('best_epoch')} (val)"), {})
    test = cps[-1] if cps else {}
    pr = p.get("recipe") or {}
    pv, pt = pr.get("pre_norm_collapse") or {}, pr.get("pre_norm_collapse_test") or {}

    def ev(d):
        return f"erank {_f(d.get('effective_rank'), 1)} SIGReg {_f(d.get('sigreg'))}"
    return (f"post-map ({z_norm}) val {ev(val)} | test {ev(test)}; "
            f"pre-map (raw encoder) val {ev(pv)} | test {ev(pt)}")


def _floor_lines(card: dict, per: list, k: int) -> list:
    """The declared n-floor of a gold criterion, and which runs it made INCONCLUSIVE."""
    fl = (card.get("floors") or {}).get(str(k)) or FLOORS[k]
    out = [f"   floor: {fl} ({FLOOR_SOURCE})"]
    for p in per:
        r = (p.get("floors") or {}).get(k) or (p.get("floors") or {}).get(str(k))
        if r:
            out.append(f"   INCONCLUSIVE ({p['run_id']}): {r} — the numbers above are not a verdict")
    return out


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
    ab = card.get("ablations") or {}
    on = [k for k in ("A2-F", "A2-E", "A2-D", "A2-W") if ab.get(k)]
    L.append("attempt-2 ablations: " + (", ".join(on) if on else "none (attempt-1 configuration)")
             + f"  (features {ab.get('features', 'a1')}, sweep rule "
             + ("every epoch" if ab.get("A2-W") else "n/a (no sweep)" if ab.get("A2-W") is None
                else "best epoch") + ")")
    rec = recipe_request(card.get("recipe"))
    L.append("collapse recipe: " + recipe_text(rec))
    for p in per:
        pr = p.get("recipe") or {}
        if pr.get("w_reg_per_epoch") is not None or pr.get("lr_warmup_steps") is not None:
            L.append(f"  {p['run_id']}: w_reg per epoch ["
                     + ", ".join(f"{w:.3g}" for w in pr.get("w_reg_per_epoch") or [])
                     + f"], LR warm-up {pr.get('lr_warmup_steps')} steps")
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
    L += _floor_lines(card, per, 2)
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
    if card.get("c4_by_construction"):
        L.append(f"   {card['c4_by_construction']}; the seeds trained the default w_reg")
    L.append(f"   rule: {card['rules'][4]}")
    alone = ", ".join(f"{p['run_id']} {'within' if p['collapse'][-1]['within_bounds'] else 'OUTSIDE'}"
                      for p in per)
    L.append(f"   (info) the scored checkpoint alone (its test z): {alone}; "
             + ("the w_reg sweep checked every epoch (A2-W), as the rule above does"
                if ab.get("A2-W") else "note the w_reg sweep checks the bounds at the best epoch "
                                       "only, the rule above checks every epoch"))
    L.append(f"   (info) z_norm {rec['z_norm'] or 'none'}: SIGReg measured on "
             f"{Z_NORM_READING.get(rec['z_norm'], str(rec['z_norm']))}; effective rank is "
             "invariant to a fixed centring and scale")
    if rec["z_norm"] is not None:
        L.append(f"   (info, not in the rule) post-map z (what C4 reads) vs pre-map raw encoder "
                 f"output, best-epoch val | scored test:")
        for p in per:
            L.append(f"     {p['run_id']}: {_map_pair(p, rec['z_norm'])}")
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
    L += _floor_lines(card, per, 5)
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
    cs = card.get("chain_steps")
    if cs:
        L += ["", "absorbing chain E[steps] ± sd vs observed (train main streams, labeled walks; A2-E)"]
        for name, r in cs.items():
            if not r:
                L.append(f"  {name:<12} no chain")
                continue
            L.append(f"  {name:<12} {r['states']} states: E[steps] {_f(r['expected_steps'], 1)} ± "
                     f"{_f(r['sd_steps'], 1)} vs observed {_f(r['observed_mean'], 1)} ± "
                     f"{_f(r['observed_sd'], 1)} (sd ratio {_f(r['sd_ratio'], 2)}), n {r['n_tasks']}"
                     + (" [singular]" if r["singular"] else ""))
    tr = card.get("training") or {}
    if tr.get("sweep"):
        ann = rec["w_reg_schedule"] == "anneal"
        L += ["", f"w_reg sweep (val only; grid {tr['sweep'].get('grid')}, schedule "
                  f"{schedule_text(rec)}, rule: {tr['sweep'].get('rule')}): " + "; ".join(
            f"{r['w_reg']:g}"
            + (f" ({float(rec['w_reg_start']):g}→{r['w_reg']:g}/{int(rec['w_reg_anneal_epochs'])}ep)"
               if ann else "")
            + f": val CE {_f(r['val_next_ce'])}, erank {_f(r['effective_rank'], 1)}, SIGReg "
            f"{_f(r['sigreg'])}{' within' if r['within_bounds'] else ' OUTSIDE'}{' <- chosen' if r['chosen'] else ''}"
            for r in tr["sweep"]["candidates"])]
    return "\n".join(L)


# ---- CLI ------------------------------------------------------------------------------------------

EVAL_CONFIG_RESERVED = ("seed", "device")


def eval_config(a) -> dict:
    """TrainConfig overrides for ``evaluate`` (the trained config with --train; the declared
    inputs + recipe a --run must match otherwise). Precedence: explicit flag > --config >
    TrainConfig default. Validated through ``TrainConfig.from_dict`` (ValueError on a bad value).
    ``seed`` / ``device`` are refused in --config: --seed/--seeds and the CPU replay set them, so
    a config seed would silently relabel runs."""
    from . import train as T
    cfgd: dict = {}
    if getattr(a, "config", None):
        p = Path(a.config)
        try:
            cfgd = json.loads(p.read_text() if p.exists() else a.config)
        except (OSError, ValueError) as e:
            raise ValueError(f"--config: not a JSON object or readable JSON file ({e})") from None
        if not isinstance(cfgd, dict):
            raise ValueError("--config: must be a JSON object")
        bad = [k for k in EVAL_CONFIG_RESERVED if k in cfgd]
        if bad:
            raise ValueError(f"--config: {bad} are set by --seed/--seeds and the CPU replay, "
                             "not by the config")
    flags = {"epochs": a.epochs or None, "features": a.features,
             "regime": True if a.regime else None,
             "z_norm": None if a.z_norm is None else (None if a.z_norm == "none" else a.z_norm),
             "w_reg_schedule": a.w_reg_schedule, "w_reg_start": a.w_reg_start,
             "w_reg_anneal_epochs": a.w_reg_anneal_epochs,
             "rank_floor_init": (None if a.rank_floor_init is None else
                                 False if a.rank_floor_init == "off" else a.rank_floor_init)}
    for k, v in flags.items():
        if v is not None:
            cfgd[k] = v
    if a.z_norm == "none":                       # explicit "none" overrides a config's z_norm
        cfgd["z_norm"] = None
    if a.lr_warmup_epochs is not None:
        s = str(a.lr_warmup_epochs).strip().lower()
        try:
            cfgd["lr_warmup_epochs"] = None if s == "none" else float(s)
        except ValueError:
            raise ValueError(f"--lr-warmup-epochs: a number or none, not {a.lr_warmup_epochs!r}") from None
    T.TrainConfig.from_dict(cfgd)                # validate keys and recipe values now
    return cfgd


def evaluate_parser():
    import argparse
    ap = argparse.ArgumentParser(prog="apex-router worldmodel evaluate",
                                 description="G1 scorecard: JEPA vs baselines on the test split (scored once).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run", action="append", help="evaluate a saved run (repeat for several seeds)")
    g.add_argument("--train", action="store_true",
                   help="train on train/val first (default config; --config / recipe flags override)")
    ap.add_argument("--seeds", type=int, default=2, help="GPU seeds to train with --train (default 2)")
    ap.add_argument("--sweep", action="store_true", help="run train.sweep_w_reg first; seed 0 = the winner")
    ap.add_argument("--no-cpu", action="store_true", help="skip the --device cpu replay run")
    ap.add_argument("--epochs", type=int, help="override TrainConfig.epochs (default: early stop)")
    ap.add_argument("--seed", type=int, default=0, help="first training seed and the bootstrap seed")
    ap.add_argument("--synthetic", type=int, metavar="N", help="N synthetic sessions (fixtures.py)")
    ap.add_argument("--view", choices=("streams", "task"), default="streams",
                    help="sequence unit: one per (task, agent) stream (default) or the raw task order")
    ap.add_argument("--features", choices=("a1", "a2f"), default=None,
                    help="JEPA inputs: a1 (attempt 1, default) or a2f (A2-F: + cross-step state); "
                         "a --run trained on other inputs is refused")
    ap.add_argument("--regime", action="store_true", default=None,
                    help="A2-D: day-regime feature for the logistic baseline and the JEPA")
    ap.add_argument("--config", metavar="JSON|PATH",
                    help="TrainConfig fields (JSON object or a path to one), merged before the "
                         "sweep and seeds like `train --config`; explicit flags win over it. "
                         "seed/device are set by --seed/--seeds and the CPU replay, not here")
    rg = ap.add_argument_group("C4 collapse recipe (docs/research/2026-10-08-p6-c4-warmup-recipe.md; "
                               "a --run trained under another recipe is refused)")
    rg.add_argument("--z-norm", choices=("none", "layernorm", "whiten", "center"),
                    help="latent normalisation (default none)")
    rg.add_argument("--w-reg-schedule", choices=("const", "anneal"),
                    help="SIGReg weight schedule (default const)")
    rg.add_argument("--w-reg-start", type=float, metavar="W",
                    help="anneal: SIGReg weight at epoch 0 (default 30)")
    rg.add_argument("--w-reg-anneal-epochs", type=int, metavar="K",
                    help="anneal: epochs to reach the final w_reg (default 4)")
    rg.add_argument("--lr-warmup-epochs", metavar="E|none",
                    help="LR warm-up length in epochs (default none = min(warmup, 10%% of updates))")
    rg.add_argument("--rank-floor-init", choices=("off", "orthogonal", "centered"),
                    help="z projection init (default off)")
    ap.add_argument("--err-states", action="store_true",
                    help="A2-E: error-conditioned <class>!err states in the absorbing chain")
    ap.add_argument("--sweep-every-epoch", action="store_true",
                    help="A2-W: with --sweep, reject a w_reg whose ANY epoch is outside the bounds")
    ap.add_argument("--w-reg-grid", default=None, metavar="LIST",
                    help="with --sweep: comma-separated w_reg values (default 1,3,10 = attempt 1)")
    ap.add_argument("--no-save", action="store_true", help="do not write the scorecard JSON")
    ap.add_argument("--json", action="store_true")
    g.add_argument("--backfill-ledger", action="store_true",
                   help="only seed eval/ledger.jsonl from the saved scorecards (scores nothing)")
    return ap


def cmd_evaluate(argv=None) -> int:
    import sys
    a = evaluate_parser().parse_args(list(argv or []))
    if (a.sweep_every_epoch or a.w_reg_grid) and not (a.train and a.sweep):
        print("apex-router worldmodel evaluate: --sweep-every-epoch / --w-reg-grid need "
              "--train --sweep", file=sys.stderr)
        return 2
    grid = None
    if a.w_reg_grid:
        from .train import parse_grid
        try:
            grid = parse_grid(a.w_reg_grid)
        except ValueError as e:
            print(f"apex-router worldmodel evaluate: --w-reg-grid: {e}", file=sys.stderr)
            return 2
    try:
        cfgd = eval_config(a)
    except ValueError as e:
        print(f"apex-router worldmodel evaluate: {e}", file=sys.stderr)
        return 2
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
    features, regime = cfgd.get("features", "a1"), bool(cfgd.get("regime", False))
    if a.train:
        ov = dict(cfgd)
        src = (f"{P.data_dir() / 'steps.jsonl'}:{a.view}" if tag == "steps"
               else f"synthetic-fixtures:{a.synthetic}")
        training = train_runs(ds, src,
                              seeds=max(1, a.seeds), sweep=a.sweep, cpu=not a.no_cpu,
                              base_seed=a.seed, overrides=ov, tag=tag, log=quiet,
                              sweep_every_epoch=a.sweep_every_epoch, w_reg_grid=grid)
        training["sweep_every_epoch"] = bool(a.sweep_every_epoch) if a.sweep else None
        runs, cpu_run = training["runs"], training["cpu_run"]
    else:
        runs = a.run
    t_train = time.perf_counter() - t0
    try:
        card = scorecard(ds, desc, runs, cpu_run=cpu_run, seed=a.seed, training=training,
                         view=a.view, log=quiet, features=features, regime=regime,
                         err_states=a.err_states, recipe=cfgd)
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
