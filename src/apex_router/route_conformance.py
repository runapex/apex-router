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
                    matched=None, *, log_path=None, ts=None, note="") -> bool:
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
        record = {"ts": ts, "surface": surface, "task_type": task_type,
                  "requested_tier": requested_tier, "resolved_model": resolved_model,
                  "matched": matched, "note": note if isinstance(note, str) else ""}
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
