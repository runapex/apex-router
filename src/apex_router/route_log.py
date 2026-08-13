"""Phase-1 escalation outcome log — write-only, fail-safe.

Records, after the fact, whether a cheap-started subtask succeeded (`ok`) or escalated
to the frontier tier (`escalated`). This is the measurement half of the Switchyard-style
escalation on-ramp: it lets us measure the per-task-type escalation rate ("when we start
`explore` cheap, how often does it bounce to opus?") from `passed`/`escalated` alone, with
no proxy telemetry. It is NOT a router and NOT the (deferred, circular-in-Phase-1)
`consumer.resolve` consultation — it only records.

Load-bearing property: FAIL-SAFE. A logging failure (unwritable dir, disk full, bad
args) must NEVER raise into or block a dispatch — `log_outcome` returns False instead.
The log path resolves arg > env (`APEX_ROUTER_LOG`) > home (`~/.apex-router/route_log.jsonl`).
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

_VALID_OUTCOMES = ("ok", "escalated")


def default_log_path() -> Path:
    """Resolve the log path from env, else the home default (matching watch.py's
    `~/.apex-router` state convention)."""
    env = os.environ.get("APEX_ROUTER_LOG")
    if env:
        return Path(env)
    return Path.home() / ".apex-router" / "route_log.jsonl"


def read_rates(*, log_path=None) -> dict:
    """Aggregate the log into per-task-type escalation rates — the Phase-1 payoff.

    Returns `{task_type: {"n": int, "escalated": int, "rate": float}}`. Fail-safe like
    the writer: a missing/unreadable log yields `{}`, and an individual malformed line
    (e.g. a partial trailing record from a disk-full append) is skipped, not fatal.
    """
    rates: dict = {}
    try:
        p = Path(log_path) if log_path is not None else default_log_path()
        if not p.is_file():
            return {}
        text = p.read_text()
    except Exception:
        return {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            tt = rec["task_type"]
            escalated = bool(rec["escalated"])
        except Exception:
            continue  # skip a corrupt/partial line, keep aggregating the rest
        cell = rates.setdefault(tt, {"n": 0, "escalated": 0, "rate": 0.0})
        cell["n"] += 1
        cell["escalated"] += 1 if escalated else 0
        cell["rate"] = cell["escalated"] / cell["n"]
    return rates


def log_outcome(task_type, model, outcome, *, log_path=None, ts=None, note="") -> bool:
    """Append one outcome record to the log. Returns True on success, False on ANY
    failure (never raises). `outcome` is "ok" (cheap succeeded) or "escalated"
    (re-dispatched heavy); any other value is rejected and nothing is written."""
    try:
        # Type-strict outcome check: `in` alone is spoofable by an __eq__-overloaded
        # object (Codex code-xval #5) — require an actual str, then membership.
        if not isinstance(outcome, str) or outcome not in _VALID_OUTCOMES:
            return False
        escalated = outcome == "escalated"
        record = {
            "ts": ts,
            "task_type": task_type,
            "model": model,
            "passed": not escalated,
            "escalated": escalated,
            "note": note,
        }
        # Serialize BEFORE opening the file so a non-serializable field (e.g. a NaN/Inf
        # ts, which is not valid JSON) fails here and writes nothing, rather than
        # leaving a partial/unparseable line (Codex code-xval #6). allow_nan=False makes
        # non-finite numbers raise instead of emitting bare NaN/Infinity tokens.
        line = json.dumps(record, allow_nan=False) + "\n"
        p = Path(log_path) if log_path is not None else default_log_path()
        # Never OPEN a non-regular-file target: appending to a FIFO/device blocks until
        # a reader appears, which would stall a dispatch — the one thing worse than
        # raising (Codex code-xval #2). Refuse anything that exists and isn't a plain
        # file; a not-yet-existing path is fine (we create it).
        if p.exists() and not stat.S_ISREG(p.stat().st_mode):
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(line)
        return True
    except Exception:
        # Fail-safe: a logging failure must never block or break a dispatch.
        return False
