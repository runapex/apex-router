"""Dispatch tier-conformance log — write-only, fail-safe.

Records, per dispatch, whether the model that actually ran matches the tier it was routed to.
Companion to route_log (which measures ESCALATION); this measures CONFORMANCE. Same load-bearing
property: a logging failure NEVER raises into or blocks a dispatch — log_conformance returns False.

Honesty invariant: where the resolved model is unobservable (Claude Code Agent subagents), the row
carries resolved_model=None / matched=None (intent only). read_conformance keeps those out of the
drift-rate denominator so an un-observable dispatch can never fake a conformance number.
The log path resolves arg > env (APEX_CONFORMANCE_LOG) > home (~/.apex-router/conformance.jsonl).
"""
from __future__ import annotations

import json
import math
import os
import stat
import time
from pathlib import Path

_SURFACES = ("pi", "resolve", "agent")


def default_conformance_path() -> Path:
    env = os.environ.get("APEX_CONFORMANCE_LOG")
    if env:
        return Path(env)
    return Path.home() / ".apex-router" / "conformance.jsonl"


def log_conformance(surface, task_type, requested_tier, resolved_model=None,
                    matched=None, *, log_path=None, ts=None, note="",
                    context_size=None, session_id=None) -> bool:
    """Append one conformance row. Returns True on success, False on ANY failure (never raises)."""
    try:
        if ts is None:
            ts = time.time()
        # strict types — a non-str key would alias in read_conformance; a non-finite ts isn't JSON.
        if not (isinstance(surface, str) and surface in _SURFACES):
            return False
        if not (isinstance(task_type, str) and task_type):
            return False
        if not (isinstance(requested_tier, str) and requested_tier):
            return False
        if resolved_model is not None and not isinstance(resolved_model, str):
            return False
        if matched is not None and not isinstance(matched, bool):
            return False
        if not isinstance(ts, (int, float)) or not math.isfinite(ts):
            return False
        if context_size is not None:
            if isinstance(context_size, bool) or not isinstance(context_size, int) or context_size < 0:
                return False
        if session_id is not None and not isinstance(session_id, str):
            return False
        record = {"ts": ts, "surface": surface, "task_type": task_type,
                  "requested_tier": requested_tier, "resolved_model": resolved_model,
                  "matched": matched, "note": note if isinstance(note, str) else ""}
        if context_size is not None:
            record["context_size"] = context_size
        if session_id is not None:
            record["session_id"] = session_id
        line = json.dumps(record, allow_nan=False) + "\n"
        p = Path(log_path) if log_path is not None else default_conformance_path()
        if p.exists() and not stat.S_ISREG(p.stat().st_mode):
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(line)
        return True
    except Exception:
        return False


def expected_models(tier, *, registry=None) -> set:
    """Allowed model id(s) for a frontier tier name, from model_registry. Unknown tier → empty set
    (the emitter then logs matched=None rather than a false mismatch).

    When no registry is supplied, load the ACTIVE registry (DEFAULTS + the user's models.json
    overlay) — the same registry resolve_text routes with. Using the hardcoded DEFAULTS instead
    would falsely flag an overlay-overridden tier id as drift (P1-a)."""
    try:
        from . import model_registry
        reg = model_registry.load() if registry is None else registry
        m = model_registry.tier_model(tier, registry=reg)
        return {m} if isinstance(m, str) and m else set()
    except Exception:
        return set()


def log_agent_dispatch(task_type, requested_tier, *, log_path=None, note="",
                       context_size=None, session_id=None) -> bool:
    """Log a Claude Code Agent dispatch. The harness does NOT expose the subagent's resolved model,
    so this is INTENT ONLY (resolved_model=None, matched=None) — read_conformance keeps it out of the
    drift denominator. Honest by construction: we never claim a conformance verdict we can't observe."""
    return log_conformance("agent", task_type, requested_tier, log_path=log_path, note=note,
                           context_size=context_size, session_id=session_id)


def log_resolve_conformance(task_type, requested_tier, resolved_model, *,
                            log_path=None, note="", context_size=None,
                            session_id=None) -> bool:
    """Log a resolve()-surface conformance row. matched = resolved_model ∈ expected_models(tier);
    an unknown tier (empty expected set) logs matched=None (no false mismatch). Fail-safe."""
    try:
        exp = expected_models(requested_tier)
        matched = (resolved_model in exp) if exp else None
        return log_conformance("resolve", task_type, requested_tier,
                               resolved_model=resolved_model, matched=matched,
                               log_path=log_path, note=note,
                               context_size=context_size, session_id=session_id)
    except Exception:
        return False


def _accumulate(agg: dict, line: str) -> None:
    try:
        rec = json.loads(line)
    except Exception:
        return
    # A valid non-object JSON (null / [] / "str") parses fine but has no .get — guard the type
    # so read_conformance can never raise on it (fail-safe invariant, P2-c).
    if not isinstance(rec, dict):
        return
    surface = rec.get("surface")
    task_type = rec.get("task_type")
    if not (isinstance(surface, str) and isinstance(task_type, str)):
        return
    matched = rec.get("matched")
    key = f"{surface}\t{task_type}"
    cell = agg.setdefault(key, {"n": 0, "observed": 0, "mismatches": 0, "drift_rate": 0.0})
    cell["n"] += 1
    if isinstance(matched, bool):          # only observed rows enter the denominator
        cell["observed"] += 1
        if not matched:
            cell["mismatches"] += 1
        cell["drift_rate"] = cell["mismatches"] / cell["observed"]


def read_conformance(*, log_path=None) -> dict:
    """Aggregate to {'surface\\ttask_type': {n, observed, mismatches, drift_rate}}. Fail-safe:
    a missing/unreadable log yields {}; a malformed line is skipped, never fatal."""
    agg: dict = {}
    p = Path(log_path) if log_path is not None else default_conformance_path()
    try:
        text = p.read_text()
    except Exception:
        return agg
    for line in text.splitlines():
        if line.strip():
            _accumulate(agg, line)
    return agg


def main(argv=None) -> int:
    """Read-only conformance readout — fail-safe, never raises. --json dumps the aggregate;
    the human table labels any row with observed==0 (agent-surface / intent-only) 'unobservable'
    so an un-observable dispatch can never display a fake drift number."""
    import argparse
    ap = argparse.ArgumentParser(prog="route-check",
                                 description="per-(surface,task_type) tier-conformance drift rate")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--record")
    a = ap.parse_args(argv)
    if a.record:
        # Hidden write-path: a caller (e.g. the pi extension) hands us one JSON row to
        # append. Fail-open — malformed JSON or a bad dict is a no-op, never a raise, and
        # log_conformance's own type checks reject a malformed dict.
        try:
            d = json.loads(a.record)
            if isinstance(d, dict):
                log_conformance(d.get("surface"), d.get("task_type"), d.get("requested_tier"),
                                resolved_model=d.get("resolved_model"), matched=d.get("matched"),
                                note=d.get("note", ""), context_size=d.get("context_size"),
                                session_id=d.get("session_id"))
        except Exception:
            pass
        return 0
    try:
        agg = read_conformance()
    except Exception:
        agg = {}
    if a.json:
        print(json.dumps(agg, indent=2, sort_keys=True))
        return 0
    if not agg:
        print("route-check: no conformance data yet")
        return 0
    print(f"{'surface':8} {'task_type':20} {'n':>4} {'obs':>4} {'drift':>6}")
    for key in sorted(agg):
        surface, task_type = key.split("\t", 1)
        c = agg[key]
        if c["observed"] == 0:
            print(f"{surface:8} {task_type:20} {c['n']:>4} {'-':>4} {'unobservable':>6}")
        else:
            print(f"{surface:8} {task_type:20} {c['n']:>4} {c['observed']:>4} {c['drift_rate']:>6.2f}")
    return 0
