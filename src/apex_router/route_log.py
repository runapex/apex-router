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
import math
import os
import stat
import time
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
        # Stream line-by-line (a huge log must not double-allocate) with tolerant
        # decoding (one bad byte must not discard the whole log — Codex readout #5/#6).
        with p.open("r", errors="replace") as f:
            for line in f:
                _accumulate(rates, line)
    except Exception:
        return {}
    return rates


def _accumulate(rates: dict, line: str) -> None:
    """Fold one raw log line into `rates`. A malformed line (bad JSON, wrong shape,
    non-str task_type, non-bool escalated) is SKIPPED, never fatal — the reader trusts
    field TYPES, not just presence, so a hand-edited/garbled record can't crash the
    aggregation or alias distinct keys (Codex readout #1/#3/#4)."""
    line = line.strip()
    if not line:
        return
    try:
        rec = json.loads(line)
    except Exception:
        return
    if not isinstance(rec, dict):
        return
    tt = rec.get("task_type")
    escalated = rec.get("escalated")
    # Strict types: task_type must be a str (so it's a safe, non-aliasing dict key) and
    # escalated must be a real bool (so bool("false")/1/0 can't inflate the rate).
    if not isinstance(tt, str) or not isinstance(escalated, bool):
        return
    ts = rec.get("ts")
    bad_ts = (isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts))
    cell = rates.setdefault(tt, {"n": 0, "escalated": 0, "rate": 0.0, "null_ts": 0})
    cell["n"] += 1
    cell["escalated"] += 1 if escalated else 0
    if bad_ts:
        cell["null_ts"] += 1
    cell["rate"] = cell["escalated"] / cell["n"]


def log_outcome(task_type, model, outcome, *, log_path=None, ts=None, note="",
                context_size=None, session_id=None) -> bool:
    """Append one outcome record to the log. Returns True on success, False on ANY
    failure (never raises). `outcome` is "ok" (cheap succeeded) or "escalated"
    (re-dispatched heavy); any other value is rejected and nothing is written.
    `ts` defaults to now: a row without a timestamp can't be era-sliced (cache_report's
    era gate, route_advise confounder #3), so callers must not have to remember it."""
    try:
        if ts is None:
            ts = time.time()
        # Type-strict outcome check: `in` alone is spoofable by an __eq__-overloaded
        # object (Codex code-xval #5) — require an actual str, then membership.
        if not isinstance(outcome, str) or outcome not in _VALID_OUTCOMES:
            return False
        escalated = outcome == "escalated"
        if context_size is not None:
            if isinstance(context_size, bool) or not isinstance(context_size, int) or context_size < 0:
                return False
        if session_id is not None and not isinstance(session_id, str):
            return False
        record = {
            "ts": ts,
            "task_type": task_type,
            "model": model,
            "passed": not escalated,
            "escalated": escalated,
            "note": note,
        }
        if context_size is not None:
            record["context_size"] = context_size
        if session_id is not None:
            record["session_id"] = session_id
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
