"""``apex-router worldmodel <cmd>`` (DESIGN-worldmodel-P6.md §0). E1: ``build`` and ``stats``."""
from __future__ import annotations

import argparse
import json
import sys


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="apex-router worldmodel",
                                 description="P6 world model: step dataset (E1)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="rebuild steps.jsonl / tasks.jsonl / manifest.json from the "
                                     "pi + Claude Code transcripts (read-only over them)")
    s = sub.add_parser("stats", help="print the manifest of the last build")
    for p in (b, s):
        p.add_argument("--home", default=None,
                       help="apex-router home (default $APEX_ROUTER_HOME, else ~/.apex-router); "
                            "data goes to <home>/worldmodel, outcomes come from <home>/labels")
        p.add_argument("--json", action="store_true", help="print the manifest as JSON")
    b.add_argument("--resplit", action="store_true",
                   help="recompute the frozen train/val/test boundaries (sessions may change "
                        "split; a test set already scored stops being held out)")
    b.add_argument("--no-embed", action="store_true",
                   help="skip the task-type classifier's local embedding (task_type stays null)")
    a = ap.parse_args(argv)
    from . import steps as S
    if a.cmd == "build":
        m = S.build(home=a.home, embed_fn=None if a.no_embed else "auto", resplit=a.resplit)
    else:
        m = S.read_manifest(a.home)
        if m is None:
            print("no manifest yet — run `apex-router worldmodel build`", file=sys.stderr)
            return 1
    print(json.dumps(m, indent=2) if a.json else _summary(m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
