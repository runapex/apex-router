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

Claude Code dispatch rows (surface="claude-code", written by hooks/agent-route-log.sh via
`python -m apex_router.route_log --hook`) carry `label_pending: true`: the PostToolUse hook
fires when the Agent tool returns (for async agents: at LAUNCH), so escalation is not
knowable at write time. `route-join` infers it offline (later same-description dispatch at
a strictly higher tier) and writes the resolved labels to the labeled table
(`labeled_table.jsonl` beside the log). `read_rates` therefore SKIPS pending rows in the raw
log and folds in the resolved claude-code rows from the labeled table instead.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import sys
import time
from pathlib import Path

_VALID_OUTCOMES = ("ok", "escalated")
# Outcomes for label-pending dispatch rows (escalation inferred offline by route-join).
# "async" = the Agent tool returned at launch (background agent); the real outcome is only
# observable later, via the telemetry join.
DISPATCH_OUTCOMES = ("ok", "error", "empty", "async")
CLAUDE_CODE_SURFACE = "claude-code"
# Frontier tier order for escalation inference: a strictly higher rank is an escalation.
TIER_RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3}
# Cheap-start tiers: only these claude-code rows enter read_rates (route-advise's question is
# "when we START cheap, how often does it bounce?"; an opus/fable start is not a cheap start).
CHEAP_START_TIERS = ("haiku", "sonnet")
_DESC_MAX = 200
_OPTIONAL_STR_FIELDS = ("surface", "agent_id", "tool_use_id", "start_tier", "description",
                        "resolved_model", "parent_agent_id")


def default_log_path() -> Path:
    """Resolve the log path from env, else the home default (matching watch.py's
    `~/.apex-router` state convention)."""
    env = os.environ.get("APEX_ROUTER_LOG")
    if env:
        return Path(env)
    return Path.home() / ".apex-router" / "route_log.jsonl"


def default_labeled_path(log_path=None) -> Path:
    """The route-join labeled table: env `APEX_LABELED_TABLE`, else `labeled_table.jsonl`
    beside the (resolved) route log — so a temp log path keeps the table hermetic too."""
    env = os.environ.get("APEX_LABELED_TABLE")
    if env:
        return Path(env)
    base = Path(log_path) if log_path is not None else default_log_path()
    return base.parent / "labeled_table.jsonl"


def tier_of(name) -> str | None:
    """Map a tier name or a model id ('claude-sonnet-5', 'opus') to a frontier tier, else None."""
    if not isinstance(name, str):
        return None
    low = name.lower()
    for t in TIER_RANK:
        if t in low:
            return t
    return None


def read_rates(*, log_path=None, labeled_path=None) -> dict:
    """Aggregate the log into per-task-type escalation rates — the Phase-1 payoff.

    Returns `{task_type: {"n": int, "escalated": int, "rate": float}}`. Fail-safe like
    the writer: a missing/unreadable log yields `{}`, and an individual malformed line
    (e.g. a partial trailing record from a disk-full append) is skipped, not fatal.

    Label-pending rows (claude-code dispatches) are skipped in the raw log; their resolved
    labels are folded in from the route-join labeled table (`labeled_path`, default
    `default_labeled_path(log_path)`), cheap-start tiers only.
    """
    rates: dict = {}
    try:
        p = Path(log_path) if log_path is not None else default_log_path()
        if p.is_file():
            # Stream line-by-line (a huge log must not double-allocate) with tolerant
            # decoding (one bad byte must not discard the whole log — Codex readout #5/#6).
            with p.open("r", errors="replace") as f:
                for line in f:
                    _accumulate(rates, line)
    except Exception:
        return {}
    try:
        lp = Path(labeled_path) if labeled_path is not None else default_labeled_path(log_path)
        if lp.is_file():
            with lp.open("r", errors="replace") as f:
                for line in f:
                    _accumulate(rates, line, labeled=True)
    except Exception:
        pass  # the labeled table is an add-on; a bad one must not discard the raw rates
    return rates


def _accumulate(rates: dict, line: str, *, labeled: bool = False) -> None:
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
    if labeled:
        # From the labeled table, take ONLY resolved claude-code cheap-start rows — every
        # other surface is already counted from the raw log (no double counting). A cheap
        # start is an EXPLICIT haiku/sonnet model arg: an `inherit` row's tier is the parent
        # session's model, not a decision to start cheap. Unlabeled rows (error/empty outcome,
        # async with no telemetry) carry no usable "did it bounce?" label.
        if rec.get("surface") != CLAUDE_CODE_SURFACE:
            return
        if rec.get("requested_tier") not in CHEAP_START_TIERS:
            return
        if rec.get("label_status") == "unlabeled":
            return
    elif rec.get("label_pending") is True:
        return  # escalation not yet inferred — counting it would fake an "ok" label
    tt = rec.get("task_type")
    escalated = rec.get("escalated")
    # Strict types: task_type must be a str (so it's a safe, non-aliasing dict key) and
    # escalated must be a real bool (so bool("false")/1/0 can't inflate the rate).
    if not isinstance(tt, str) or not isinstance(escalated, bool):
        return
    ts = rec.get("ts")
    if isinstance(ts, bool):
        bad_ts = True
    elif isinstance(ts, int):
        bad_ts = False
    elif isinstance(ts, float):
        bad_ts = not math.isfinite(ts)
    else:
        bad_ts = True
    cell = rates.setdefault(tt, {"n": 0, "escalated": 0, "rate": 0.0, "null_ts": 0})
    cell["n"] += 1
    cell["escalated"] += 1 if escalated else 0
    if bad_ts:
        cell["null_ts"] += 1
    cell["rate"] = cell["escalated"] / cell["n"]


def log_outcome(task_type, model, outcome, *, log_path=None, ts=None, note="",
                context_size=None, session_id=None, label_pending=False, surface=None,
                agent_id=None, tool_use_id=None, start_tier=None, description=None,
                resolved_model=None, parent_agent_id=None) -> bool:
    """Append one outcome record to the log. Returns True on success, False on ANY
    failure (never raises). `outcome` is "ok" (cheap succeeded) or "escalated"
    (re-dispatched heavy); any other value is rejected and nothing is written.
    `ts` defaults to now: a row without a timestamp can't be era-sliced (cache_report's
    era gate, route_advise confounder #3), so callers must not have to remember it.

    `label_pending=True` writes a dispatch row whose escalation is inferred offline by
    route-join: `outcome` must then be one of DISPATCH_OUTCOMES (ok|error|empty|async),
    `escalated` is written False, `passed` null, and `label_pending: true` keeps the row out
    of read_rates until route-join resolves it. The optional dispatch fields (surface,
    agent_id, tool_use_id, start_tier, description, resolved_model, parent_agent_id) must be
    str or None; they are written only when given."""
    try:
        if ts is None:
            ts = time.time()
        # Type-strict outcome check: `in` alone is spoofable by an __eq__-overloaded
        # object (Codex code-xval #5) — require an actual str, then membership.
        if not isinstance(label_pending, bool):
            return False
        allowed = DISPATCH_OUTCOMES if label_pending else _VALID_OUTCOMES
        if not isinstance(outcome, str) or outcome not in allowed:
            return False
        escalated = (outcome == "escalated") if not label_pending else False
        if context_size is not None:
            if isinstance(context_size, bool) or not isinstance(context_size, int) or context_size < 0:
                return False
        if session_id is not None and not isinstance(session_id, str):
            return False
        extras = {"surface": surface, "agent_id": agent_id, "tool_use_id": tool_use_id,
                  "start_tier": start_tier, "description": description,
                  "resolved_model": resolved_model, "parent_agent_id": parent_agent_id}
        for v in extras.values():
            if v is not None and not isinstance(v, str):
                return False
        if label_pending and not (isinstance(task_type, str) and isinstance(model, str)):
            return False  # route-join's strict parser would drop it anyway
        record = {
            "ts": ts,
            "task_type": task_type,
            "model": model,
            "passed": None if label_pending else not escalated,
            "escalated": escalated,
            "note": note,
        }
        if context_size is not None:
            record["context_size"] = context_size
        if session_id is not None:
            record["session_id"] = session_id
        if label_pending:
            record["label_pending"] = True
            record["outcome"] = outcome
        for k in _OPTIONAL_STR_FIELDS:
            v = extras[k]
            if v is not None:
                record[k] = v[:_DESC_MAX] if k == "description" else v
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


# --------------------------------------------------------------------------- #
# Claude Code PostToolUse(Agent) hook entry — `python -m apex_router.route_log --hook`.
# hooks/agent-route-log.sh pipes the hook JSON on stdin straight here: all parsing happens
# in Python (no shell-built JSON). Fail-safe: never raises, never prints, always exit 0.

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# Text fallback for a plain-string tool_response only, anchored to a line start: the trailer
# line Claude Code appends ("agentId: <id>"). Never run over a structured response's nested
# free text, where a subagent may quote another agent's id.
_AGENT_ID_TEXT_RE = re.compile(r"^agentId:\s*([A-Za-z0-9_-]{4,128})", re.MULTILINE)
_FALLBACK_RULES = (
    (re.compile(r"\b(review|audit|verif)"), "review"),
    (re.compile(r"\b(implement|write|build|fix)"), "generate"),
    (re.compile(r"\b(debug|root[ -]cause|why)\b"), "debug"),
    (re.compile(r"\b(refactor|rename)"), "refactor"),
)
_PROMPT_HEAD = 200


def classify_dispatch(subagent_type=None, description=None, prompt_head=None) -> str:
    """Task type for an Agent dispatch. Precedence: Explore subagent → explore; an explicit
    task-word marker via classify.classify_request (the free request-signal prior); keyword
    fallback rules; else explore. Never raises."""
    try:
        st = (subagent_type or "").strip().lower() if isinstance(subagent_type, str) else ""
        if st == "explore":
            return "explore"
        text = " ".join(x for x in (description, prompt_head) if isinstance(x, str)).lower()
        try:
            from .classify import classify_request
            words = set(re.findall(r"[a-z]+", text))
            if st:
                words.add(st)
            c = classify_request(sys_markers=words)
            if c.source == "request":
                return c.task_type
        except Exception:
            pass
        for pat, tt in _FALLBACK_RULES:
            if pat.search(text):
                return tt
    except Exception:
        pass
    return "explore"


def _response_text(resp) -> str:
    if isinstance(resp, str):
        return resp
    if isinstance(resp, list):
        return " ".join(_response_text(b) for b in resp)
    if isinstance(resp, dict):
        if isinstance(resp.get("text"), str):
            return resp["text"]
        for k in ("content", "result", "output"):
            if k in resp:
                return _response_text(resp[k])
    return ""


def _agent_id_from(resp) -> str | None:
    if isinstance(resp, dict):
        for k in ("agentId", "agent_id"):
            v = resp.get(k)
            if isinstance(v, str) and _ID_RE.match(v):
                return v
    if not isinstance(resp, str):
        return None
    m = _AGENT_ID_TEXT_RE.search(resp)
    return m.group(1) if m else None


def dispatch_outcome(resp) -> str:
    """ok | error | empty | async from an Agent tool_response (dict, list or str)."""
    if resp is None:
        return "empty"
    if isinstance(resp, dict):
        status = str(resp.get("status") or "").lower()
        if resp.get("is_error") is True or resp.get("isError") is True or \
                status in ("error", "failed", "failure"):
            return "error"
        if resp.get("isAsync") is True or status.startswith("async"):
            return "async"
    text = _response_text(resp).strip()
    if not text:
        return "empty"
    head = text[:300].lower()
    if head.startswith("error") or "<tool_use_error>" in head:
        return "error"
    if head.startswith("async agent launched"):
        return "async"
    return "ok"


def _opt_str(v, max_len=None):
    if not isinstance(v, str) or not v.strip():
        return None
    v = v.strip()
    return v[:max_len] if max_len else v


def hook_main(raw: str, *, log_path=None) -> bool:
    """Parse one PostToolUse hook payload and append one label-pending dispatch row.
    Returns True when a row was written; never raises."""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("tool_name") not in ("Agent", "Task"):
            return False
        ti = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
        resp = data.get("tool_response")
        subagent_type = _opt_str(ti.get("subagent_type"))
        description = _opt_str(ti.get("description"), _DESC_MAX)
        prompt_head = _opt_str(ti.get("prompt"), _PROMPT_HEAD)
        model_arg = _opt_str(ti.get("model"))
        start_tier = model_arg.lower() if model_arg else "inherit"
        resolved = resp.get("resolvedModel") if isinstance(resp, dict) else None
        sid = _opt_str(data.get("session_id"))
        tuid = _opt_str(data.get("tool_use_id"))
        parent = _opt_str(data.get("agent_id"))  # set when the dispatcher is itself a subagent
        return log_outcome(
            classify_dispatch(subagent_type, description, prompt_head),
            start_tier, dispatch_outcome(resp), log_path=log_path,
            note=f"agent:{subagent_type or 'general-purpose'}",
            session_id=sid, label_pending=True, surface=CLAUDE_CODE_SURFACE,
            agent_id=_agent_id_from(resp), tool_use_id=tuid, start_tier=start_tier,
            description=description, resolved_model=_opt_str(resolved),
            parent_agent_id=parent if parent and _ID_RE.match(parent) else None,
        )
    except Exception:
        return False


if __name__ == "__main__":  # pragma: no cover - exercised via the hook subprocess test
    if "--hook" in sys.argv[1:]:
        try:
            import signal
            signal.alarm(3)  # hard ceiling: a hook must never hang a session
        except Exception:
            pass
        try:
            hook_main(sys.stdin.read())
        except BaseException:
            pass
    sys.exit(0)
