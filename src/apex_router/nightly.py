"""Nightly maintenance — the measured-adaptivity loop, run on a schedule.

One fail-open pass over everything that adapts to measured evidence:

  1. ROUTE ADVISE     — per-task-type cheap-vs-heavy verdicts from the escalation log
                        (Wilson + BH gate; recommends, never mutates).
  2. HANDOFF THRESHOLD— recompute the adaptive cache-handoff threshold from telemetry
                        (scripts/handoff_threshold.py) so the Stop nudge tracks the real
                        per-session read distribution instead of a static cap.
  3. MEMORY INDEX     — refresh the L2 memory-retrieval index (scripts/memory_search.py
                        ingest) for every registered memory dir, so archived memories stay
                        queryable instead of prefix-resident.
  4. JUDGE PROBE      — if a judge endpoint is configured, drift-check the pinned chain
                        judge against its frozen probe set (scripts/judge_probe.py).

Every step is independently fail-open: a step's failure degrades its own section of the
digest, never the run. Pure orchestration — the measurement logic lives in the tools.

CLI: `apex-router nightly` (also invoked by `watch --run-daily`).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"


def _run_script(name: str, *args: str, timeout: int = 600) -> tuple[int, str]:
    """Run a scripts/ tool with the current interpreter; capture output. Never raises."""
    script = _SCRIPTS / name
    if not script.is_file():
        return 2, f"({name} not found at {script})"
    try:
        p = subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        out = (p.stdout + p.stderr).strip()
        return p.returncode, out
    except (OSError, subprocess.SubprocessError) as e:
        return 2, f"({name} failed to run: {type(e).__name__})"


def _advise_section() -> str:
    try:
        from . import route_advise
        verdicts = route_advise.advise()
    except Exception as e:  # noqa: BLE001
        return f"  (route-advise unavailable: {type(e).__name__})"
    if not verdicts:
        return "  (no escalation outcomes logged yet)"
    lines = []
    for tt in sorted(verdicts, key=str):
        v = verdicts[tt]
        lines.append(f"  {str(tt)[:24]:<24} {v['verdict']:<24} n={v['n']:<4} "
                     f"rate={v['rate']:.2f} CI=[{v['ci_low']:.2f},{v['ci_high']:.2f}]")
    return "\n".join(lines)


def _memory_dirs(env=None) -> list[Path]:
    """Registered memory dirs: APEX_MEMORY_DIRS (colon-sep) > memory_dirs.txt > auto-discover
    ~/.claude/projects/*/memory. Only existing dirs are returned."""
    e = os.environ if env is None else env
    dirs: list[Path] = []
    if e.get("APEX_MEMORY_DIRS"):
        dirs = [Path(p) for p in e["APEX_MEMORY_DIRS"].split(":") if p.strip()]
    else:
        lst = Path.home() / ".apex-router" / "memory_dirs.txt"
        try:
            if lst.is_file():
                dirs = [Path(line.strip()) for line in lst.read_text().splitlines()
                        if line.strip() and not line.startswith("#")]
        except OSError:
            dirs = []
        if not dirs:
            projects = Path.home() / ".claude" / "projects"
            try:
                dirs = [p for p in sorted(projects.glob("*/memory")) if p.is_dir()]
            except OSError:
                dirs = []
    return [d for d in dirs if d.is_dir()]


def run(*, now: float | None = None) -> str:
    """Run all nightly steps; return the Markdown digest section. Never raises."""
    now = time.time() if now is None else now
    parts = ["\n## nightly adaptivity\n"]

    parts.append("### route-advise (cheap-start verdicts)\n```")
    parts.append(_advise_section())
    parts.append("```")

    parts.append("### handoff threshold")
    rc, out = _run_script("handoff_threshold.py")
    parts.append("```\n" + (out or f"(exit {rc})") + "\n```")

    parts.append("### memory index (L2 retrieval)")
    dirs = _memory_dirs()
    if not dirs:
        parts.append("  (no memory dirs registered or discovered)")
    for d in dirs:
        rc, out = _run_script("memory_search.py", "ingest", "--dir", str(d))
        first = out.splitlines()[0] if out else f"(exit {rc})"
        counts = " ".join(line for line in out.splitlines()
                          if line.startswith(("added:", "skipped:", "removed:")))
        parts.append(f"  {d.parent.name}/{d.name}: {counts or first}")

    parts.append("### judge drift probe")
    if not (os.environ.get("CODEQA_JUDGE_BASE") or os.environ.get("CHAIN_JUDGE_URL")):
        parts.append("  (skipped: no judge endpoint configured)")
    else:
        rc, out = _run_script("judge_probe.py")
        parts.append("```\n" + (out or f"(exit {rc})") + "\n```")

    return "\n".join(parts) + "\n"


def main() -> int:
    digest = run()
    out = Path.home() / ".apex-router" / "offload_daily.md"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8") as f:
            f.write(digest)
    except OSError as e:
        print(f"(nightly digest write failed: {type(e).__name__})", file=sys.stderr)
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
