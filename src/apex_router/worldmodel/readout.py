"""`apex-router worldmodel baseline | chains | progress` — the E2 readouts (P6 baselines + Markov/Zeno).

Reads `~/.apex-router/worldmodel/{steps,tasks}.jsonl` (APEX_ROUTER_HOME honoured; `--steps` /
`--tasks` override) read-only, or generates `--synthetic N` sessions in memory (fixtures.py; known
dynamics, labels marked gold — a check of the estimators, not a measurement of this machine).
Every number carries its n and a 95% session-bootstrap CI (Wilson for rates); outcome numbers say
gold / provisional / inconclusive.

Entry points: `cmd_baseline(argv)`, `cmd_chains(argv)`, `cmd_progress(argv)` and `COMMANDS` (name →
fn), for the single `worldmodel` dispatcher (worldmodel/cli.py) to merge into its own table. This
module owns no top-level dispatch. numpy/scipy are imported only inside a command, so a missing
one prints a one-line message (rc 1) instead of a traceback.
"""
from __future__ import annotations

import argparse
import json
import math
import sys

ORDERS = (1, 2, 3)


def _f(x, d=3):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else (
        "∞" if isinstance(x, float) and math.isinf(x) else f"{x:.{d}f}")


def _ci(ci, d=3):
    lo, hi = ci if ci else (None, None)
    return "—" if lo is None else f"[{_f(lo, d)}, {_f(hi, d)}]"


def _pct(x, d=0):
    return "—" if x is None else f"{100 * x:.{d}f}%"


def _pci(ci, d=0):
    lo, hi = ci if ci else (None, None)
    return "—" if lo is None else f"[{_pct(lo, d)}, {_pct(hi, d)}]"


def _load(args):
    from . import protocol as P
    if args.synthetic:
        from .fixtures import dataset
        ds, _ = dataset(args.synthetic, seed=args.seed)
        return ds, f"synthetic: {args.synthetic} sessions, seed {args.seed} (known dynamics, labels gold)"
    ds = P.load(args.steps, args.tasks)
    return ds, f"{args.steps or P.data_dir() / 'steps.jsonl'} (+ tasks.jsonl)"


def _source(ds, desc) -> dict:
    from . import protocol as P
    sp = {}
    for s in P.SPLITS:
        ids = ds.ids(s)
        sp[s] = {"tasks": len(ids), "sessions": len({ds.sid(t) for t in ids}),
                 "steps": sum(len(ds.steps[t]) for t in ids),
                 "gold": sum(1 for t in ids if P.label(ds.tasks[t]) is not None
                             and ds.tasks[t].get("outcome_src") == "gold"),
                 "weak": sum(1 for t in ids if P.label(ds.tasks[t]) is not None
                             and ds.tasks[t].get("outcome_src") != "gold")}
    return {"desc": desc, "synthetic": desc.startswith("synthetic:"), "splits": sp,
            "dropped_steps": ds.dropped}


def _head(title, src) -> list:
    L = [title, f"source: {src['desc']}"]
    L.append("split (by session start): " + " · ".join(
        f"{s} {v['tasks']} tasks / {v['sessions']} sessions / {v['steps']} steps "
        f"(labels {v['gold']} gold, {v['weak']} weak)" for s, v in src["splits"].items())
        + (f"; {src['dropped_steps']} steps dropped (no split)" if src["dropped_steps"] else ""))
    return L


# ---- baseline ---------------------------------------------------------------------------------

def baseline_report(ds, desc, seed: int = 0) -> dict:
    from . import protocol as P
    from . import baselines as B
    tr, va = ds.ids("train"), ds.ids("val")
    models = [B.Unigram()] + [B.Markov(order=k) for k in ORDERS] + [B.Logistic()]
    for m in models:
        m.fit(ds, tr, va)
    rows = []
    for m in models:
        ce_v, _ = P.next_action_scores(m, m.name, ds, "val", seed)
        ce_t, ppl_t = P.next_action_scores(m, m.name, ds, "test", seed)
        br_v, _ = P.outcome_scores(m, m.name, ds, "val", seed=seed)
        br, ec = P.outcome_scores(m, m.name, ds, "test", seed=seed)
        br_end, _ = P.outcome_scores(m, m.name, ds, "test", seed=seed, prefix=None)
        br_gold, _ = P.outcome_scores(m, m.name, ds, "test", gold_only=True, seed=seed)
        rows.append({"model": m.name, "ce_val": ce_v.to_dict(), "ce_test": ce_t.to_dict(),
                     "ppl_test": ppl_t.to_dict(), "brier_val": br_v.to_dict(),
                     "brier": br.to_dict(), "ece": ec.to_dict(),
                     "brier_end": br_end.to_dict(), "brier_gold": br_gold.to_dict()})
    # best next-action baseline by VAL CE (test is not used to choose)
    scored = [(r["ce_val"]["value"], i) for i, r in enumerate(rows) if r["ce_val"]["value"] is not None]
    best_i = min(scored)[1] if scored else 0
    best = models[best_i]
    paired = [P.paired_ce(best, m, f"{best.name} − {m.name}", ds, "test", seed).to_dict()
              for i, m in enumerate(models) if i != best_i]
    # outcome bar chosen on VAL Brier at the protocol prefix; test is scored once
    b_scored = [(r["brier_val"]["value"], i) for i, r in enumerate(rows)
                if r["brier_val"]["value"] is not None]
    ob_i = min(b_scored)[1] if b_scored else 0
    mk = [(rows[i]["ce_val"]["value"], i) for i, m in enumerate(models)
          if isinstance(m, B.Markov) and rows[i]["ce_val"]["value"] is not None]
    show = [models[0]] + ([models[min(mk)[1]]] if mk else []) + [best]
    days = {}
    for m in show:
        days.setdefault(m.name, P.per_day(m, m.name, ds, "test"))
    ob = P.paired_brier(models[ob_i], models[0], f"{models[ob_i].name} − {models[0].name}", ds,
                        "test", gold_only=True, seed=seed).to_dict()
    return {"source": _source(ds, desc), "rows": rows, "best_next_action": best.name,
            "best_outcome": rows[ob_i]["model"], "paired_test": paired, "paired_brier_gold": ob,
            "bic": B.bic_order(ds, tr, ORDERS), "per_day_test": days,
            "outcome_prefix": P.OUTCOME_PREFIX}


def render_baseline(rep) -> str:
    L = _head("World-model baselines (P6 E2) — next action and outcome on held-out sessions",
              rep["source"])
    L += ["", "1. next action — cross-entropy (nats/step), 95% session-bootstrap CI"]
    L.append(f"  {'model':<26} {'val CE':<22} {'test CE':<22} {'test ppl':<20} n steps / sessions")
    for r in rep["rows"]:
        v, t, p = r["ce_val"], r["ce_test"], r["ppl_test"]
        L.append(f"  {r['model']:<26} {_f(v['value']):>5} {_ci(v['ci']):<16} "
                 f"{_f(t['value']):>5} {_ci(t['ci']):<16} {_f(p['value'], 2):>5} {_ci(p['ci'], 2):<14} "
                 f"{t['n']} / {t['n_sessions']}")
    L.append(f"  best on val: {rep['best_next_action']} — G1 criterion 1 needs a test CE ≥ 5% below it")
    for d in rep["paired_test"]:
        rel = d["extra"].get("rel")
        L.append(f"    {d['model']:<44} {_f(d['value']):>6} {_ci(d['ci']):<18} "
                 f"rel {_pct(rel, 1)} {_pci(d['extra'].get('rel_ci'), 1)}")
    L += ["", f"2. outcome — P(success) after the first {rep['outcome_prefix']} steps "
              "(never the last), test tasks"]
    L.append(f"  {'model':<26} {'val Brier':<10} {'test Brier':<22} {'ECE (10 bins)':<22} "
             f"{'n tasks / sessions':<19} labels")
    for r in rep["rows"]:
        b, e, bv = r["brier"], r["ece"], r["brier_val"]
        L.append(f"  {r['model']:<26} {_f(bv['value']):<10} {_f(b['value']):>5} {_ci(b['ci']):<16} "
                 f"{_f(e['value']):>5} {_ci(e['ci']):<16} "
                 f"{str(b['n']) + ' / ' + str(b['n_sessions']):<19} {b['quality']}")
    L.append("  (Markov orders share one outcome model: P(SUCCESS) of the order-1 absorbing chain from "
             "the last action seen)")
    L.append(f"  best on val: {rep['best_outcome']} — G1 criterion 2 (gold only) needs a test Brier "
             "below it with the paired CI excluding 0")
    pb = rep["paired_brier_gold"]
    L.append(f"    {pb['model']:<44} {_f(pb['value']):>6} {_ci(pb['ci']):<18} n {pb['n']} "
             f"(paired Brier, {pb['quality']})")
    L.append("  end-of-task readout (ceiling, the last steps all but state the label): " + ", ".join(
        f"{r['model']} {_f(r['brier_end']['value'])}" for r in rep["rows"]))
    g = rep["rows"][0]["brier_gold"]
    if g["quality"] == "inconclusive":
        L.append("  gold-only: no gold labels on test — criteria 2 and 5 are INCONCLUSIVE")
    b = rep["bic"]
    L += ["", f"3. Markov order test (BIC, ML counts on train, n = {b['n']} steps)"]
    for o in b["orders"]:
        L.append(f"  order {o['order']}: log-lik {o['loglik']:,.0f}  params {o['params']:,}  "
                 f"contexts {o['contexts']}  BIC {o['bic']:,.0f}")
    if b["best"] is not None:
        L.append(f"  verdict: order {b['best']} (ΔBIC to the next best {b['delta_bic']:,.0f})")
    L += ["", "4. per day (test, UTC) — CE nats/step; a day-level regime shows as days that move "
              "together across models"]
    names = list(rep["per_day_test"])
    days = sorted({d["day"] for v in rep["per_day_test"].values() for d in v}, key=str)
    for day in days:
        cells = []
        for n in names:
            row = next((d for d in rep["per_day_test"][n] if d["day"] == day), None)
            cells.append(f"{n} {_f(row['ce']) if row else '—'}")
        nn = next((d for d in rep["per_day_test"][names[0]] if d["day"] == day), {})
        L.append(f"  {day}  n={nn.get('n', 0):<5} sessions={nn.get('n_sessions', 0):<3} " + "  ".join(cells))
    L.append("  caveat: one split, one seed; held-out sessions are later in time than training, so "
             "drift between them is part of the score")
    return "\n".join(L)


# ---- chains -----------------------------------------------------------------------------------

def chains_report(ds, desc, lam: float = 0.0) -> dict:
    from . import chains as C
    tr = ds.ids("train")
    by = C.by_key(ds, tr)
    vals = C.workflow_values(ds, tr, lam=lam)
    real = C.realized(ds, ds.ids("test"))
    rk = C.ranking_accuracy(vals, real)
    unl = sum(1 for t in tr if C.absorbing_state(ds.tasks[t], ds.steps[t]) is None)
    return {"source": _source(ds, desc), "table": C.table(by), "lambda": lam,
            "excluded_unknown": unl,
            "workflows": [{"key": list(k), **v, "test": real.get(k)} for k, v in vals.items()],
            "ranking": {"accuracy": rk["accuracy"], "n_pairs": rk["n_pairs"],
                        "pairs": [{**p, "a": list(p["a"]), "b": list(p["b"])} for p in rk["pairs"]]}}


def render_chains(rep) -> str:
    L = _head("Absorbing chain over action classes (P6 E2) — fitted on train", rep["source"])
    L.append(f"  absorbing: success→SUCCESS, fail→FAIL, partial→ESCALATE if handed off else "
             f"ABANDON; {rep['excluded_unknown']} unlabeled train tasks left out")
    L += ["", "1. per task type (rows shrunk to the pooled chain, β = 8)"]
    L.append(f"  {'type':<10} {'tasks':>5} {'transitions':>11}  {'E[steps] ± sd':<16} "
             f"{'P(success)':>10} {'P(fail)':>8} {'P(esc)':>7} {'P(aband)':>8}  ρ(Q)")
    for r in rep["table"]:
        if r["singular"]:
            L.append(f"  {str(r['key']):<10} {r['n_tasks']:>5} {r['n_transitions']:>11}  "
                     f"singular (ρ(Q) = {_f(r['rho'])}) — expected steps diverge")
            continue
        pa = r["p_absorb"]
        L.append(f"  {str(r['key']):<10} {r['n_tasks']:>5} {r['n_transitions']:>11}  "
                 f"{_f(r['expected_steps'], 1):>6} ± {_f(r['sd_steps'], 1):<7} "
                 f"{_pct(pa['SUCCESS'], 1):>10} {_pct(pa['FAIL'], 1):>8} {_pct(pa['ESCALATE'], 1):>7} "
                 f"{_pct(pa['ABANDON'], 1):>8}  {_f(r['rho'], 4)}")
    L.append("  E[steps] = π0·N·1 from START; ρ(Q) → 1 is the closed-form 'steps to absorption → ∞'")
    L.append("  misspecification check — model E[steps] ± sd vs observed task length mean ± sd: "
             + "; ".join(f"{r['key']} {_f(r['expected_steps'], 1)} ± {_f(r['sd_steps'], 1)} vs "
                         f"{_f(r['observed_mean'], 1)} ± {_f(r['observed_sd'], 1)}"
                         for r in rep["table"] if not r["singular"]))
    L.append("  (a first-order chain has geometric-like length tails; an observed sd far from the "
             "model's says length depends on more than the current action)")
    pooled = rep["table"][0] if rep["table"] else None
    if pooled and (pooled["n_mapped_other"] or pooled["n_dropped"]):
        L.append(f"  {pooled['n_mapped_other']} steps outside the state set counted as `other`, "
                 f"{pooled['n_dropped']} dropped")
    L += ["", f"2. workflow value = P(success) − λ·E[steps], λ = {rep['lambda']:g} "
              "(train) vs realized success (test)"]
    for w in rep["workflows"]:
        t = w["test"]
        real = "—" if not t else f"{t[0]}/{t[1]} = {_pct(t[0] / t[1], 0)}"
        L.append(f"  {w['key'][0]:<10} {w['key'][1]:<4} value {_f(w['value'])}  "
                 f"P(success) {_pct(w['p_success'], 1):>6}  E[steps] {_f(w['expected_steps'], 1):>6}  "
                 f"n {w['n']:<4} test {real}")
    rk = rep["ranking"]
    L.append(f"  ranking accuracy (within type, pairs with ≥ 5 test tasks each): "
             f"{_pct(rk['accuracy'], 0)} over {rk['n_pairs']} pairs — the bar for G1 criterion 3")
    L.append("  caveat: the workflow is the task's recorded template, else W2 if it spawned a "
             "subagent, else W0; the user chose it, so this is association, not effect")
    return "\n".join(L)


# ---- progress ---------------------------------------------------------------------------------

def progress_report(ds, desc, seed: int = 0, task=None) -> dict:
    from . import chains as C
    from . import progress as G
    tr, va, te = ds.ids("train"), ds.ids("val"), ds.ids("test")
    chain = C.fit_tasks(ds, tr)
    out = {"source": _source(ds, desc)}
    if task is not None:
        st = ds.steps.get(task)
        if st is None:
            return {**out, "error": f"no task {task!r}"}
        det = G.detect(st, seed=seed, task_id=task)
        rho = C.window_rho(chain, st)
        return {**out, "task": task, "outcome": ds.tasks[task].get("outcome"),
                "failing": G.failing_counts(st), "kind": det["kind"], "windows": det["windows"],
                "rho": [None if math.isnan(x) else float(x) for x in rho], "n_steps": len(st)}
    scores = G.detector_scores(ds, tr + va + te, chain, seed=seed)
    c5 = G.criterion5(ds, scores, tr + va, te)
    kinds, status = {}, {}
    for t in te:
        det = G.detect(ds.steps[t], seed=seed, task_id=t)
        kinds[det["kind"] or "none"] = kinds.get(det["kind"] or "none", 0) + 1
        for w in det["windows"]:
            status[w["status"]] = status.get(w["status"], 0) + 1
    return {**out, "criterion5": c5, "signal_kinds_test": kinds, "window_status_test": status}


def render_progress(rep) -> str:
    L = _head("Zeno progress detector (P6 E2) — converging short of the goal?", rep["source"])
    if rep.get("error"):
        return "\n".join(L + [f"  {rep['error']}"])
    if "task" in rep:
        L += ["", f"task {rep['task']} — {rep['n_steps']} steps, outcome {rep['outcome']}, "
                  f"progress signal {rep['kind'] or 'none'}"]
        L.append("  failing tests per run: " + (", ".join(
            f"{f}@{i}" for i, f in rep["failing"]) or "—"))
        for w in rep["windows"]:
            L.append(f"  step {w['step']:>3}  v {_f(w['v'])}  r {_f(w['r'])}  v∞ {_f(w['v_inf'])} "
                     f"{_ci(w['ci']):<18} {w['status']:<14}"
                     + ("  CONVERGING SHORT" if w["converging_short"] else "")
                     + ("  STALLED (stuck detector)" if w["stalled"] else ""))
        if not rep["windows"]:
            L.append("  fewer than 4 progress observations — no window to test")
        rho = [x for x in rep["rho"] if x is not None]
        if rho:
            L.append(f"  window ρ(Q) (last 20 transitions): first {_f(rho[0], 4)}  max "
                     f"{_f(max(rho), 4)}  last {_f(rho[-1], 4)}")
        return "\n".join(L)
    c5 = rep["criterion5"]
    th = c5["thresholds"]
    if rep["source"].get("synthetic"):
        L.append("  SYNTHETIC: planted dynamics — the generator plants the geometric-to-residual "
                 "shape the detector looks for; this checks the code, not the method")
    L += ["", "1. detectors on test tasks"]
    L.append("  progress signal: " + ", ".join(f"{k} {v}" for k, v in sorted(rep["signal_kinds_test"].items()))
             + " tasks;  window status: " + (", ".join(
                 f"{k} {v}" for k, v in sorted(rep["window_status_test"].items())) or "—"))
    L.append(f"  thresholds tuned on train+val successes (≤ {_pct(c5['fpr_target'])} of them flagged): "
             f"zeno CI-upper < {_f(1.0 - th['zeno'])}, stalled v < {_f(1.0 - th['stalled'])}, "
             f"ρ(Q) > {_f(th.get('rho'), 4)}, "
             f"steps > {_f(th['steps'], 0)}, wall > {_f(th['wall'], 0)} s")
    L += ["", f"2. G1 criterion 5 — tasks ending fail/escalate/abandon flagged before the "
              f"step/wall cutoff (labels: {c5['quality']}; rule {c5['rule']})"]
    bl = c5["baseline"]
    L.append(f"  baseline (earlier of step and wall cutoff): recall {_pct(bl['recall']['rate'])} "
             f"{_pci(bl['recall']['ci'])} of {c5['n_bad']}, FPR {_pct(bl['fpr']['rate'])} "
             f"{_pci(bl['fpr']['ci'])} of {c5['n_good']} successes")
    for name, d in c5["detectors"].items():
        ws = d["win_share"]
        L.append(f"  {name:<13} wins {d['wins']:>2} losses {d['losses']:>2}  win share "
                 f"{_pct(ws['rate'])} {_pci(ws['ci'])}  median saved {_f(d['median_saved'], 1)}  "
                 f"recall {_pct(d['recall']['rate'])}  FPR {d['fpr']['k']}/{d['fpr']['n']} "
                 f"{_pci(d['fpr']['ci'])}  → {d['verdict']}")
    L.append("  win = flagged before the cutoff and ≥ 2 steps before the end; loss = cutoff first. "
             "PASS = FPR Wilson upper ≤ 10%, win-share Wilson lower > 50%, median steps saved > 0 "
             "(pending owner sign-off)")
    L.append("  stalled = the stuck detector (flat window), reported apart from Zeno; combined rows "
             "(a|b) use each part's θ, not re-tuned — their tuned FPR can reach ~20%; the "
             "test-FPR gate still applies")
    L.append("  caveat: a flag needs ≥ 4 test runs (or error events) in the task — tasks without "
             "them can only be caught by the cutoff")
    return "\n".join(L)


# ---- CLI --------------------------------------------------------------------------------------

_HELP = {"baseline": "next-action CE / outcome Brier per baseline + BIC order test",
         "chains": "absorbing chain per task type + workflow ranking",
         "progress": "Zeno progress detector + G1 criterion 5"}
_OPTIONAL = {"numpy", "scipy"}


def _parser(name: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"apex-router worldmodel {name}", description=_HELP[name])
    p.add_argument("--synthetic", type=int, metavar="N", help="N synthetic sessions instead of data")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--steps", help="steps.jsonl (default ~/.apex-router/worldmodel/)")
    p.add_argument("--tasks", help="tasks.jsonl (default ~/.apex-router/worldmodel/)")
    p.add_argument("--json", action="store_true")
    if name == "chains":
        p.add_argument("--lam", type=float, default=0.0, help="λ in value = P(success) − λ·E[steps]")
    if name == "progress":
        p.add_argument("--task", help="trace one task id")
    return p


def _run(name: str, argv) -> int:
    args = _parser(name).parse_args(list(argv or []))
    try:
        ds, desc = _load(args)
        if not ds.ids("train") or not ds.ids("test"):
            print(f"apex-router worldmodel: no train/test tasks in {desc} — build the step dataset "
                  "first (E1) or pass --synthetic N", file=sys.stderr)
            return 1
        if name == "baseline":
            rep, render = baseline_report(ds, desc, args.seed), render_baseline
        elif name == "chains":
            rep, render = chains_report(ds, desc, args.lam), render_chains
        else:
            rep, render = progress_report(ds, desc, args.seed, args.task), render_progress
    except ModuleNotFoundError as e:
        if (e.name or "").split(".")[0] not in _OPTIONAL:
            raise
        print(f"apex-router worldmodel {name}: needs numpy and scipy (missing: {e.name}); "
              "install the world-model extra", file=sys.stderr)
        return 1
    print(json.dumps(_clean(rep), indent=2, default=_json_default) if args.json else render(rep))
    return 0


def cmd_baseline(argv=None) -> int:
    return _run("baseline", argv)


def cmd_chains(argv=None) -> int:
    return _run("chains", argv)


def cmd_progress(argv=None) -> int:
    return _run("progress", argv)


COMMANDS = {"baseline": cmd_baseline, "chains": cmd_chains, "progress": cmd_progress}


def main(argv=None) -> int:
    """Standalone use (`python -m apex_router.worldmodel.readout baseline …`)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        print("usage: readout {" + "|".join(COMMANDS) + "} [--synthetic N] [--json] …",
              file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](argv[1:])


def _clean(o):
    """Strict JSON: non-finite floats become the strings 'inf' / '-inf' / 'nan'."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, float) and not math.isfinite(o):
        return "nan" if math.isnan(o) else ("inf" if o > 0 else "-inf")
    if hasattr(o, "tolist"):
        return _clean(o.tolist())
    return o


def _json_default(o):
    try:
        import numpy as np
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:
        pass
    return str(o)


if __name__ == "__main__":
    raise SystemExit(main())
