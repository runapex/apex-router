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


def _token_count(*values) -> int:
    """First positive integer telemetry count; zero/malformed values fall through."""
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return 0


def _request_context_tokens(row: dict) -> int:
    """Prompt context on either provider wire without double-counting cached tokens.

    OpenAI reports ``input_tokens`` as the TOTAL prompt and cached tokens as a subset.
    Anthropic reports fresh input, cache reads, and cache writes as disjoint pools. This
    is the same wire invariant used by proxy_engine.readout.doctor._fresh_input.
    """
    usage = row.get("usage")
    if not isinstance(usage, dict):
        shadow = row.get("shadow")
        usage = shadow.get("usage") if isinstance(shadow, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    total_or_fresh = _token_count(row.get("tokens_in"), usage.get("input_tokens"))
    endpoint = (row.get("endpoint_id") or "").lower()
    # Codex telemetry is the OpenAI-compatible wire. Older rows can lack endpoint_id,
    # so client=codex preserves inclusive-input semantics rather than double-counting cache.
    if endpoint == "openai" or (not endpoint and row.get("client") == "codex"):
        return total_or_fresh
    cache_read = _token_count(
        row.get("cache_read_tokens"), usage.get("cache_read_tokens"),
        usage.get("cache_read_input_tokens"),
    )
    cache_write = _token_count(
        row.get("cache_write_tokens"), usage.get("cache_creation_tokens"),
        usage.get("cache_creation_input_tokens"),
    )
    return total_or_fresh + cache_read + cache_write


def _codex_context_watch(now: float, *, days: float = 14,
                         telemetry: Path | None = None) -> str:
    """Per-request context distribution for codex-venue traffic vs the venue policy:
    the % fitting under the downshift ceiling (k2.7-code-eligible and cheaper) and the
    % approaching k3's 1M ceiling. This is the codex venue's cost lever — context size,
    not cache hygiene (kimi cache writes are free; reads ~10%)."""
    import json as _json
    from . import model_registry
    tel = telemetry or (Path.home() / ".apex" / "telemetry.jsonl")
    vpol = model_registry.venue("codex") or {}
    ceiling = vpol.get("downshift_ctx_ceiling", 250_000)
    hard = vpol.get("ceiling_ctx", 1_000_000)
    ctx: list[int] = []
    try:
        lo = now - days * 86400
        with tel.open("r", errors="replace") as f:
            for line in f:
                try:
                    r = _json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                if r.get("ev") == "hb" or r.get("client") != "codex":
                    continue
                ts = r.get("ts")
                if not isinstance(ts, (int, float)) or ts < lo:
                    continue
                total = _request_context_tokens(r)
                if total:
                    ctx.append(total)
    except OSError:
        return "  (telemetry unavailable)"
    if not ctx:
        return "  (no codex-venue traffic in window)"
    ctx.sort()
    n = len(ctx)
    fits = sum(1 for c in ctx if c <= ceiling)
    near_hard = sum(1 for c in ctx if c > hard * 0.9)
    p50, p95 = ctx[n // 2], ctx[min(n - 1, int(n * 0.95))]
    return (f"```\nrequests={n}  ctx p50={p50:,} p95={p95:,} max={ctx[-1]:,}\n"
            f"fits downshift (<={ceiling:,} → {vpol.get('downshift_model', '?')}): "
            f"{fits}/{n} ({fits / n:.0%})  — the cheaper tier only serves these\n"
            f"near 1M ceiling: {near_hard}/{n}\n"
            f"lever: hand off / compact codex sessions under {ceiling:,} ctx to unlock "
            f"{vpol.get('downshift_model', 'the downshift model')}\n```")


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

    parts.append("### codex venue context watch (DECISION-kimi-codex-routing)")
    parts.append(_codex_context_watch(now))

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
