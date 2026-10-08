"""``apex-router worldmodel <cmd>`` — P6 world model commands.

One dispatcher: E1 ``build``/``stats``, E2 ``baseline``/``chains``/``progress``, E3 ``train``/
``probe``, E4 ``evaluate`` — all registered in ``COMMANDS``. Heavy imports (numpy, mlx) happen inside each command.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _fmt(x, nd: int = 4) -> str:
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return str(x)


def _print_probes(probes: dict) -> None:
    print("  (phase and fail_bucket are model inputs: those probes are near-tautological and only "
          "check that z keeps the state)")
    for target, by_input in probes.items():
        for inp, r in by_input.items():
            if r.get("skipped"):
                print(f"  probe {target:<12} on {inp:<9} skipped (n_train={r['n_train']}, n_val={r['n_val']})")
                continue
            extra = (f" brier {_fmt(r['brier'])} (prior {_fmt(r['prior_brier'])})" if "brier" in r else "")
            print(f"  probe {target:<12} on {inp:<9} n={r['n_val']:<6} ce {_fmt(r['ce'])} "
                  f"(prior {_fmt(r['prior_ce'])}) acc {_fmt(r['acc'], 3)}{extra}")


def _print_collapse(c: dict) -> None:
    print(f"  collapse: effective rank {_fmt(c['effective_rank'], 1)} (min {c['erank_min']}), "
          f"SIGReg {_fmt(c['sigreg'])} (max {c['sigreg_max']}), mean cosine {_fmt(c['mean_cosine'], 3)}, "
          f"n={c['n']} -> {'within bounds' if c['within_bounds'] else 'OUTSIDE BOUNDS'}")


def cmd_train(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="apex-router worldmodel train",
                                 description="Train the P6 JEPA world model (needs the [worldmodel] extra).")
    ap.add_argument("--synthetic", type=int, metavar="N",
                    help="train on N synthetic tasks instead of <data home>/steps.jsonl")
    ap.add_argument("--config", help="JSON object or path to a JSON file with TrainConfig fields")
    ap.add_argument("--epochs", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--run-id")
    ap.add_argument("--device", choices=("gpu", "cpu"),
                    help="cpu = bit-exact replay for a fixed seed; gpu (default) is faster but "
                         "not bit-exact across runs")
    ap.add_argument("--sweep-w-reg", action="store_true",
                    help="train w_reg in {1, 3, 10}; pick min val CE among runs within the "
                         "collapse bounds (recorded as w_reg_sweep in the winner's summary.json)")
    ap.add_argument("--view", choices=("streams", "task"), default="streams",
                    help="sequence unit: one per (task, agent) stream (default; contract §1 counts "
                         "i/dt/phase per stream) or the raw per-task order (escape hatch)")
    ap.add_argument("--features", choices=("a1", "a2f"),
                    help="inputs: a1 (attempt 1, default) or a2f (A2-F: + cross-step task state)")
    ap.add_argument("--regime", action="store_true",
                    help="A2-D: + the day-regime feature (errors in the session's last 15 min, "
                         "session-day error rate so far)")
    ap.add_argument("--sweep-every-epoch", action="store_true",
                    help="A2-W: with --sweep-w-reg, reject a w_reg whose ANY epoch is outside "
                         "the collapse bounds (how G1 C4 reads a run)")
    ap.add_argument("--w-reg-grid", default=None, metavar="LIST",
                    help="with --sweep-w-reg: comma-separated w_reg values (default 1,3,10)")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    a = ap.parse_args(argv)
    if (a.sweep_every_epoch or a.w_reg_grid) and not a.sweep_w_reg:
        print("--sweep-every-epoch / --w-reg-grid need --sweep-w-reg", file=sys.stderr)
        return 2
    from .jepa import mlx_available
    if not mlx_available():
        print("mlx is not installed: `pip install 'apex-router[worldmodel]'` (Apple Silicon only)",
              file=sys.stderr)
        return 2
    from .train import TrainConfig, load_view, synthetic_dataset, train
    cfgd: dict = {}
    if a.config:
        p = Path(a.config)
        cfgd = json.loads(p.read_text() if p.exists() else a.config)
    if a.epochs is not None:
        cfgd["epochs"] = a.epochs
    if a.seed is not None:
        cfgd["seed"] = a.seed
    if a.device:
        cfgd["device"] = a.device
    if a.features:
        cfgd["features"] = a.features
    if a.regime:
        cfgd["regime"] = True
    cfg = TrainConfig.from_dict(cfgd)
    ds = synthetic_dataset(a.synthetic, cfg.seed) if a.synthetic else load_view(a.view)
    st = ds.stats()
    print(f"data: {st['source']} — {st['tasks']} tasks, {st['steps']} steps "
          f"(train {st['tasks_train']}, val {st['tasks_val']}, test {st['tasks_test']} held out)")
    if a.sweep_w_reg:
        from .train import W_REG_GRID, parse_grid, sweep_w_reg
        try:
            grid = parse_grid(a.w_reg_grid) if a.w_reg_grid else W_REG_GRID
        except ValueError as e:
            print(f"--w-reg-grid: {e}", file=sys.stderr)
            return 2
        sw = sweep_w_reg(cfg, ds, values=grid, run_id=a.run_id,
                         log=(lambda m: None) if a.json else print,
                         every_epoch=a.sweep_every_epoch)
        if a.json:
            print(json.dumps(sw, indent=1, default=float))
            return 0
        for r in sw["candidates"]:
            print(f"  w_reg {r['w_reg']:g}: val CE {_fmt(r['val_next_ce'])} (epoch {r['best_epoch']}), "
                  f"erank {_fmt(r['effective_rank'], 1)}, SIGReg {_fmt(r['sigreg'])}, "
                  f"{'within' if r['within_bounds'] else 'OUTSIDE'} bounds"
                  + (f" (epochs outside: {r['epochs_outside']})" if r.get("epochs_outside") else "")
                  + ("  <- chosen" if r["chosen"] else ""))
        print(f"chosen w_reg: {sw['chosen_w_reg']}  (grid {sw['grid']}; {sw['rule']})")
        if sw.get("note"):
            print(sw["note"])
        return 0 if sw["chosen_w_reg"] is not None else 1
    s = train(cfg, ds, run_id=a.run_id, log=(lambda m: None) if a.json else print)
    if a.json:
        print(json.dumps(s, indent=1, default=float))
        return 0
    b = s["best"]
    t = s["throughput"]
    print(f"run {s['run_id']}: {s['params']:,} params, {s['n_features']} features "
          f"(set {s['features']}{', + regime' if s['regime'] else ''}), "
          f"windows train {s['windows']['train']} / val {s['windows']['val']}")
    print(f"  throughput {t['windows_per_s']:.0f} windows/s ({t['steps_per_s']:.0f} steps/s), "
          f"{t['train_seconds']:.1f} s training")
    print(f"  best epoch {s['best_epoch']}: val next-action CE {_fmt(b['val_next_ce'])} nats/step "
          f"(unigram {_fmt(b['val_unigram_ce'])}), acc {_fmt(b['val_next_acc'], 3)}, "
          f"value BCE {_fmt(b['val_value_bce'])}, task Brier {_fmt(b['val_brier_task'])} "
          f"(base rate {_fmt(b['val_brier_task_baserate'])}, n={b['val_tasks_labelled']}), "
          f"latent pred {_fmt(b['val_pred'])}")
    print(f"  CE on steps just after a former hard window cut: {_fmt(b['val_next_ce_cut'])} "
          f"(n={b['val_steps_cut']})")
    _print_collapse(s["final_val"]["collapse"])
    if s["collapse_outside_bounds_epochs"]:
        print(f"  epochs outside collapse bounds: {s['collapse_outside_bounds_epochs']}")
    _print_probes(s["probes"])
    return 0


def cmd_probe(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="apex-router worldmodel probe",
                                 description="Collapse diagnostics + linear probes for a saved run.")
    ap.add_argument("run_id")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    from .jepa import mlx_available
    if not mlx_available():
        print("mlx is not installed: `pip install 'apex-router[worldmodel]'`", file=sys.stderr)
        return 2
    from .train import probe_run
    r = probe_run(a.run_id)
    if a.json:
        print(json.dumps(r, indent=1, default=float))
        return 0
    v = r["val"]
    print(f"run {r['run']}: val next-action CE {_fmt(v['val_next_ce'])} (unigram "
          f"{_fmt(v['val_unigram_ce'])}), task Brier {_fmt(v['val_brier_task'])} (n={v['val_tasks_labelled']})")
    _print_collapse(v["collapse"])
    _print_probes(r["probes"])
    return 0


def cmd_evaluate(argv: list[str]) -> int:
    """E4: the G1 scorecard (evaluate.py; numpy is imported only when the command runs)."""
    from .evaluate import cmd_evaluate as _run
    return _run(argv)


COMMANDS = {"train": cmd_train, "probe": cmd_probe, "evaluate": cmd_evaluate}
# E2: baselines, absorbing chains, Zeno progress (readout imports numpy/scipy inside each command).
from .readout import COMMANDS as _E2_COMMANDS  # noqa: E402

COMMANDS.update(_E2_COMMANDS)


# ---- E1: the step dataset ---------------------------------------------------------------------

def _summary(m: dict) -> str:
    c = m.get("counts", {})
    lines = [f"steps {c.get('steps')} · tasks {c.get('tasks')} · sessions {c.get('sessions')} · "
             f"subagents {c.get('subagents')} (unlinked {c.get('subagents_unlinked')}, "
             f"orphan {c.get('subagents_orphan')}, by time {c.get('subagents_by_time')})"]
    for src, v in (m.get("per_source") or {}).items():
        lines.append(f"  {src:7} " + " · ".join(f"{k} {n}" for k, n in v.items()))
    cls = m.get("classes") or {}
    tot = max(sum(cls.values()), 1)
    lines.append("classes: " + ", ".join(f"{k} {n} ({100 * n / tot:.1f}%)"
                                         for k, n in sorted(cls.items(), key=lambda x: -x[1]) if n))
    lines.append("phases: " + ", ".join(f"{k} {n}" for k, n in (m.get("phases") or {}).items()))
    t = m.get("tests") or {}
    cov = t.get("coverage")
    lines.append(f"tests: {t.get('test_steps')} test steps, counts parsed on {t.get('parsed')} "
                 f"({'n/a' if cov is None else f'{100 * cov:.1f}%'})")
    lines.append("splits: " + " · ".join(
        f"{k} {v.get('sessions')}s/{v.get('tasks')}t/{v.get('steps')}st"
        for k, v in (m.get("splits") or {}).items()))
    sb = m.get("split_bounds") or {}
    lines.append(f"split frozen {sb.get('frozen_at')}: val from {sb.get('val_from_iso')}, "
                 f"test from {sb.get('test_from_iso')}")
    o = m.get("outcomes") or {}
    lines.append(f"outcomes: gold {o.get('gold')} · weak {o.get('weak')} · none {o.get('none')}")
    tt = ", ".join(f"{k} {v}" for k, v in (m.get("task_types") or {}).items())
    wf = ", ".join(f"{k} {v}" for k, v in (m.get("workflows") or {}).items())
    lines.append(f"task types: {tt} · workflows: {wf}")
    ms = m.get("model_source") or {}
    lines.append("model from: " + " · ".join(f"{k} {v}" for k, v in ms.items()))
    tr = m.get("time_range") or {}
    lines.append(f"time: {tr.get('first')} .. {tr.get('last')} · git {str(m.get('git_sha'))[:12]}"
                 f" · built {m.get('built_at')}")
    re_ = m.get("read_errors") or {}
    lines.append(f"read errors: {re_.get('malformed_lines')} malformed lines, "
                 f"{re_.get('unreadable_files')} unreadable files")
    return "\n".join(lines)



def _e1_parser(cmd: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=f"apex-router worldmodel {cmd}")
    ap.add_argument("--home", default=None,
                    help="apex-router home (default $APEX_ROUTER_HOME, else ~/.apex-router); "
                         "data goes to <home>/worldmodel, outcomes come from <home>/labels")
    ap.add_argument("--json", action="store_true", help="print the manifest as JSON")
    if cmd == "build":
        ap.add_argument("--resplit", action="store_true",
                        help="recompute the frozen train/val/test boundaries (sessions may change "
                             "split; a test set already scored stops being held out)")
        ap.add_argument("--no-embed", action="store_true",
                        help="skip the task-type classifier's local embedding (task_type stays null)")
    return ap


def cmd_build(argv: list[str]) -> int:
    """Rebuild steps.jsonl / tasks.jsonl / manifest.json from the pi + Claude Code transcripts."""
    a = _e1_parser("build").parse_args(argv)
    from . import steps as S
    m = S.build(home=a.home, embed_fn=None if a.no_embed else "auto", resplit=a.resplit)
    print(json.dumps(m, indent=2) if a.json else _summary(m))
    return 0


def cmd_stats(argv: list[str]) -> int:
    """Print the manifest of the last build."""
    a = _e1_parser("stats").parse_args(argv)
    from . import steps as S
    m = S.read_manifest(a.home)
    if m is None:
        print("no manifest yet — run `apex-router worldmodel build`", file=sys.stderr)
        return 1
    print(json.dumps(m, indent=2) if a.json else _summary(m))
    return 0


def cmd_snapshot(argv: list[str]) -> int:
    """Mirror the pi / Claude Code / Codex transcripts into <home>/transcripts so retention
    pruning stops shrinking the dataset (never deletes, never truncates)."""
    ap = argparse.ArgumentParser(prog="apex-router worldmodel snapshot",
                                 description=cmd_snapshot.__doc__)
    ap.add_argument("--home", default=None,
                    help="apex-router home (default $APEX_ROUTER_HOME, else ~/.apex-router); "
                         "the mirror is <home>/transcripts")
    ap.add_argument("--dry-run", action="store_true", help="count what would be copied; write nothing")
    ap.add_argument("--json", action="store_true", help="print the counts as JSON")
    a = ap.parse_args(argv)
    from .. import transcript_mirror as TM
    r = TM.snapshot(home=a.home, dry_run=a.dry_run)
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        print(f"transcript snapshot{' (dry run)' if a.dry_run else ''} -> {r['mirror']}: "
              f"new {r['new']}, updated {r['updated']}, unchanged {r['unchanged']}, "
              f"bytes {r['bytes']}" + (f", kept (mirror larger) {r['kept_mirror_larger']}"
                                       if r["kept_mirror_larger"] else "")
              + (f", skipped >512MB {r['skipped_large']}" if r["skipped_large"] else "")
              + (f", errors {r['errors']}" if r["errors"] else ""))
        for src, v in r["per_source"].items():
            print(f"  {src:7} files {v['files']} · new {v['new']} · updated {v['updated']}")
        for n in r["notes"]:
            print(f"  note: {n}")
    return 0


COMMANDS.update({"build": cmd_build, "stats": cmd_stats, "snapshot": cmd_snapshot})


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in COMMANDS:
        order = ["build", "stats", "snapshot", "baseline", "chains", "progress", "train", "probe", "evaluate"]
        names = [c for c in order if c in COMMANDS] + sorted(set(COMMANDS) - set(order))
        print("usage: apex-router worldmodel {" + ",".join(names) + "} ...")
        return 0 if (not argv or argv[0] in ("-h", "--help")) else 2
    return COMMANDS[argv[0]](argv[1:])
