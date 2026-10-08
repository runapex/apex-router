"""Phase-0 labeled training table — join route_log outcomes with conformance rows.

Two joins feed one table:
  * classic route_log rows (pi / CLI) ⋈ conformance rows (task_type + |Δts| ≤ 300 s);
  * claude-code dispatch rows (label_pending, written by hooks/agent-route-log.sh) ⋈ proxy
    telemetry by (session_id, agent_id) — resolved model, request count, output tokens,
    error count — with escalation INFERRED offline: a row is escalated when it started on an
    EXPLICIT cheap tier (haiku/sonnet from the Agent `model` arg — never `inherit`, whose tier
    comes from the parent session) and a later Agent dispatch in the same session, within
    2 hours, has the same normalized description at a strictly higher tier (explicit, or an
    inherit whose resolved model is strictly higher). Tier order: haiku < sonnet < opus < fable.
    Rows whose outcome is error/empty, or async rows with no telemetry join, get
    `label_status: "unlabeled"` — kept in the table, excluded from rate counts.

Fail-safe: malformed lines are skipped, unjoinable rows are counted, and every public
function returns an empty container rather than raising on failure. Read failures of the
route log / telemetry are surfaced in stats (`route_log_error`, `telemetry_error` +
`*_error_name`). `refresh_labeled_table` — the single write path for route-join and the
nightly — atomically replaces `labeled_table.jsonl` beside the route log so route-readout /
route-advise pick up the resolved claude-code labels, but refuses to clobber an existing
non-empty table with an empty one or one built from a failed read.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import stats
from .route_conformance import default_conformance_path
from .route_log import (ALL_TIER_RANK, CHEAP_START_TIERS, CLAUDE_CODE_SURFACE, GPT_FAMILY,
                        OTHER_TIER, default_labeled_path, default_log_path, is_cross_family,
                        is_gpt_family, tier_family, tier_of)
from .telemetry_path import telemetry_path as _resolve_telemetry_path

_JOIN_WINDOW_S = 300.0
# A higher-tier redo further out than this is a new task that happens to share a description,
# not an escalation of the earlier dispatch.
_ESCALATION_WINDOW_S = 2 * 3600.0
LABELED = "labeled"
UNLABELED = "unlabeled"
_DISPATCH_STR_FIELDS = ("surface", "agent_id", "tool_use_id", "start_tier", "description",
                        "resolved_model", "outcome", "parent_agent_id")


def default_telemetry_path() -> Path:
    """Proxy telemetry: APEX_TELEMETRY > $APEX_HOME/telemetry.jsonl > ~/.apex/telemetry.jsonl
    (the shared resolver — the proxy writes under APEX_HOME)."""
    return _resolve_telemetry_path()


def _is_finite_ts(value: Any) -> bool:
    """True for finite int/float timestamps (bools and non-finites rejected)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _parse_route_log(path: Path, errors: Optional[Dict[str, str]] = None
                     ) -> Tuple[List[Dict[str, Any]], int]:
    """Stream a route_log JSONL into validated rows plus a malformed skip count.

    A missing file or mid-read failure yields what was parsed so far; when `errors` is given
    the exception name is recorded under errors["route_log"] so callers can tell an empty /
    truncated read from a genuinely empty log.

    A row is kept when it is a dict with str task_type/model and bool escalated.
    Rows with missing/non-finite ts are KEPT (they are unjoinable, not malformed)
    and counted later by join_labels. Wrongly-typed optional fields make the line
    malformed.
    """
    rows: List[Dict[str, Any]] = []
    skipped = 0
    try:
        with path.open("r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    skipped += 1
                    continue
                if not isinstance(rec, dict):
                    skipped += 1
                    continue
                tt = rec.get("task_type")
                model = rec.get("model")
                escalated = rec.get("escalated")
                if not (isinstance(tt, str) and isinstance(model, str)
                        and isinstance(escalated, bool)):
                    skipped += 1
                    continue
                cs = rec.get("context_size")
                sid = rec.get("session_id")
                if cs is not None and (isinstance(cs, bool) or not isinstance(cs, int) or cs < 0):
                    skipped += 1
                    continue
                if sid is not None and not isinstance(sid, str):
                    skipped += 1
                    continue
                if any(rec.get(k) is not None and not isinstance(rec.get(k), str)
                       for k in _DISPATCH_STR_FIELDS):
                    skipped += 1
                    continue
                lp = rec.get("label_pending")
                if lp is not None and not isinstance(lp, bool):
                    skipped += 1
                    continue
                rows.append(rec)
    except Exception as e:
        # Fail-safe: unreadable files yield whatever we parsed so far (often nothing) — but
        # the failure is reported, so the writer can refuse to clobber a good table.
        if errors is not None:
            errors["route_log"] = type(e).__name__
    return rows, skipped


def _parse_conformance(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Stream a conformance JSONL into validated rows plus a malformed skip count.

    A conformance row must have a finite ts, str surface/task_type/requested_tier,
    and resolved_model/matched that are either None or the expected types.
    Agent-surface intent-only rows (matched=None) are parsed here and filtered out
    by the caller so they can be counted.
    """
    rows: List[Dict[str, Any]] = []
    skipped = 0
    try:
        with path.open("r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    skipped += 1
                    continue
                if not isinstance(rec, dict):
                    skipped += 1
                    continue
                ts = rec.get("ts")
                surface = rec.get("surface")
                tt = rec.get("task_type")
                tier = rec.get("requested_tier")
                resolved = rec.get("resolved_model")
                matched = rec.get("matched")
                if not (_is_finite_ts(ts) and isinstance(surface, str)
                        and isinstance(tt, str) and isinstance(tier, str)):
                    skipped += 1
                    continue
                if resolved is not None and not isinstance(resolved, str):
                    skipped += 1
                    continue
                if matched is not None and not isinstance(matched, bool):
                    skipped += 1
                    continue
                cs = rec.get("context_size")
                sid = rec.get("session_id")
                if cs is not None and (isinstance(cs, bool) or not isinstance(cs, int) or cs < 0):
                    skipped += 1
                    continue
                if sid is not None and not isinstance(sid, str):
                    skipped += 1
                    continue
                rows.append(rec)
    except Exception:
        pass
    return rows, skipped


def _build_joined(route_row: Dict[str, Any], conf_row: Dict[str, Any]) -> Dict[str, Any]:
    """Materialize one joined row following the Phase-0 schema contract."""
    out: Dict[str, Any] = {
        "ts": route_row["ts"],
        "task_type": route_row["task_type"],
        "model": route_row["model"],
        "escalated": route_row["escalated"],
        "label": "hard" if route_row["escalated"] else "easy",
        "surface": conf_row.get("surface"),
        "requested_tier": conf_row.get("requested_tier"),
        "resolved_model": conf_row.get("resolved_model"),
        "matched": conf_row.get("matched"),
    }
    # The outcome's tier is the model that RAN (route_log `model`): on a subscription overlay a
    # pi `sonnet` cue runs gpt-5.6-terra, so the row is gpt-terra's, with `family` as the label.
    tier = tier_of(route_row.get("start_tier")) or tier_of(route_row.get("model"))
    out["tier"] = tier
    # Family is a fact about the ids, even when no tier is known (a new GPT id): the same evidence
    # route_log._accumulate uses, so the raw and joined headlines cannot disagree.
    out["tier_family"] = tier_family(tier) or (
        GPT_FAMILY if is_gpt_family(route_row.get("start_tier"), route_row.get("model")) else None)
    out["cross_family"] = False
    fam = route_row.get("family")
    if isinstance(fam, str):
        out["family"] = fam
    # context_size: conformance preferred, else route_log.
    cs = conf_row.get("context_size")
    if cs is None:
        cs = route_row.get("context_size")
    if cs is not None:
        out["context_size"] = cs
    # session_id: present if either side has it (conformance preferred).
    sid = conf_row.get("session_id")
    if sid is None:
        sid = route_row.get("session_id")
    if sid is not None:
        out["session_id"] = sid
    return out


def normalize_description(desc: Any) -> str:
    """Lowercase, punctuation → space, collapsed whitespace. '' for non-str."""
    if not isinstance(desc, str):
        return ""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", desc.lower()).split())


def _row_is_error(rec: Dict[str, Any]) -> bool:
    """A telemetry request row failed: is_error, a 429, or any upstream rejection (v8)."""
    return (rec.get("is_error") is True or rec.get("error_cause") == "http_429"
            or rec.get("upstream_rejected") is True)


def _telemetry_index(path: Path, wanted: set, errors: Optional[Dict[str, str]] = None
                     ) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Stream telemetry once, aggregating request rows whose (session_id, agent_id) is wanted.
    Heartbeats (ev == "hb") and malformed lines are skipped. Missing/unreadable file → what
    was aggregated so far, with the exception name recorded in errors["telemetry"]."""
    agg: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if not wanted:
        return agg
    try:
        with path.open("r", errors="replace") as fh:
            for line in fh:
                # Cheap pre-filter: skip lines that can't carry an agent id before json.loads.
                if '"agent_id"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict) or rec.get("ev") == "hb":
                    continue
                key = (rec.get("session_id"), rec.get("agent_id"))
                if key not in wanted:
                    continue
                a = agg.setdefault(key, {"requests": 0, "output_tokens": 0, "error_count": 0,
                                         "models": Counter(), "last_ts": None,
                                         "last_is_error": None})
                a["requests"] += 1
                usage = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
                out = usage.get("output_tokens")
                if isinstance(out, bool) or not isinstance(out, int):
                    out = rec.get("tokens_out")
                if isinstance(out, int) and not isinstance(out, bool) and out > 0:
                    a["output_tokens"] += out
                is_err = _row_is_error(rec)
                if is_err:
                    a["error_count"] += 1
                m = rec.get("model_resolved")
                if isinstance(m, str) and m:
                    a["models"][m] += 1
                ts = rec.get("ts")
                if _is_finite_ts(ts) and (a["last_ts"] is None or ts >= a["last_ts"]):
                    a["last_ts"] = ts
                    a["last_is_error"] = is_err
    except Exception as e:
        if errors is not None:
            errors["telemetry"] = type(e).__name__
    return agg


_FINISHED_OUTCOMES = ("ok", "error")


def _dispatch_rank(r: Dict[str, Any]) -> Tuple[int, int]:
    """Which of two rows for one dispatch to keep: a finished outcome (ok/error) over
    empty over async/absent, then the datapce plugin's row (it carries inject_arm) over the
    agent-route-log hook's."""
    outcome = r.get("outcome")
    finished = 2 if outcome in _FINISHED_OUTCOMES else 1 if outcome == "empty" else 0
    return finished, 1 if "inject_arm" in r else 0


def _dedupe_dispatch(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """One row per (session_id, tool_use_id): the agent-route-log hook and the datapce
    plugin can both log the same Agent dispatch while both are installed. Rows missing
    either id are kept as they are; the kept row takes the first one's place in order."""
    kept: List[Dict[str, Any]] = []
    at: Dict[Tuple[str, str], int] = {}
    dropped = 0
    for r in rows:
        sid, tuid = r.get("session_id"), r.get("tool_use_id")
        if not (isinstance(sid, str) and isinstance(tuid, str)):
            kept.append(r)
            continue
        i = at.get((sid, tuid))
        if i is None:
            at[(sid, tuid)] = len(kept)
            kept.append(r)
            continue
        dropped += 1
        if _dispatch_rank(r) > _dispatch_rank(kept[i]):
            kept[i] = r
    return kept, dropped


def writer_parity(rows: List[Dict[str, Any]], today: Optional[date] = None) -> Dict[str, Any]:
    """0.4.2 retirement gate. Claude-code dispatch rows by writer, before dedupe: the datapce plugin
    (rows carry inject_arm) vs the agent-route-log hook. Writers are compared by (session_id, tool_use_id)
    key, not by raw counts, and a key is filed under the UTC day of its plugin row (else of its earliest
    row): the plugin stamps the spawn and the hook the PostToolUse end, so one dispatch can straddle
    00:00 UTC. Per day, plugin = keys with a plugin row, hook = keys with a hook row, workflow = Workflow
    rows (plugin-only; the hook cannot see them). A day disagrees when some hook key has no plugin row
    (rows missing an id cannot pair, so a hook row without ids always disagrees). The trailing streak
    walks back over days with rows while plugin > 0 and the hook never disagrees; days without rows, and
    Workflow-only days (plugin 0 and hook 0), are quiet and neither break nor count. parity_days counts the
    streak days with plugin rows (plugin-only days included: no hook disagreement). parity_until is the
    last such day; gate_open = span >= 14 and parity_days >= 10 and parity_until within 2 days of
    `today` (default: UTC today)."""
    keyed: Dict[Tuple[str, str], Dict[str, Any]] = {}
    loose: List[Tuple[float, bool]] = []  # (ts, is_plugin) for rows that cannot pair
    workflow: List[float] = []
    for r in rows:
        ts = r.get("ts")
        if not _is_finite_ts(ts):
            continue
        ts = float(ts)
        is_plugin = "inject_arm" in r
        if is_plugin and r.get("source") == "workflow":
            workflow.append(ts)
            continue
        sid, tuid = r.get("session_id"), r.get("tool_use_id")
        if not (isinstance(sid, str) and isinstance(tuid, str)):
            loose.append((ts, is_plugin))
            continue
        k = keyed.setdefault((sid, tuid), {"plugin": None, "hook": None})
        w = "plugin" if is_plugin else "hook"
        k[w] = ts if k[w] is None else min(k[w], ts)

    def _utc(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    days: Dict[str, Dict[str, int]] = {}
    disagree: set = set()

    def _d(day: str) -> Dict[str, int]:
        return days.setdefault(day, {"plugin": 0, "hook": 0, "workflow": 0})

    for k in keyed.values():
        day = _utc(k["plugin"] if k["plugin"] is not None else k["hook"])
        d = _d(day)
        if k["plugin"] is not None:
            d["plugin"] += 1
        if k["hook"] is not None:
            d["hook"] += 1
            if k["plugin"] is None:
                disagree.add(day)
    for ts, is_plugin in loose:
        d = _d(_utc(ts))
        if is_plugin:
            d["plugin"] += 1
        else:
            d["hook"] += 1
            disagree.add(_utc(ts))
    for ts in workflow:
        _d(_utc(ts))["workflow"] += 1
    ordered = sorted(days)
    streak: List[str] = []
    for day in reversed(ordered):
        d = days[day]
        if d["plugin"] == 0 and d["hook"] == 0:
            continue
        if d["plugin"] == 0 or day in disagree:
            break
        streak.append(day)
    span = (date.fromisoformat(streak[0]) - date.fromisoformat(streak[-1])).days + 1 if streak else 0
    until = streak[0] if streak else None
    today = today or datetime.now(timezone.utc).date()
    gate_open = bool(streak) and span >= 14 and len(streak) >= 10 \
        and (today - date.fromisoformat(until)).days <= 2
    return {"days": {k: days[k] for k in ordered}, "parity_since": streak[-1] if streak else None,
            "parity_until": until, "parity_span_days": span, "parity_days": len(streak), "gate_open": gate_open}


def _build_dispatch_rows(rows: List[Dict[str, Any]], telemetry_path: Path,
                         errors: Optional[Dict[str, str]] = None
                         ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Materialize claude-code dispatch rows: dedupe, telemetry join + offline escalation inference."""
    parity = writer_parity(rows)
    rows, deduped = _dedupe_dispatch(rows)
    wanted = {(r.get("session_id"), r.get("agent_id")) for r in rows
              if isinstance(r.get("session_id"), str) and isinstance(r.get("agent_id"), str)}
    tel = _telemetry_index(telemetry_path, wanted, errors)

    out: List[Dict[str, Any]] = []
    for r in rows:
        start = r.get("start_tier") if isinstance(r.get("start_tier"), str) else r["model"]
        t = tel.get((r.get("session_id"), r.get("agent_id")))
        resolved: Optional[str] = None
        if t and t["models"]:
            resolved = t["models"].most_common(1)[0][0]
        elif isinstance(r.get("resolved_model"), str):
            resolved = r["resolved_model"]
        requested = tier_of(start) if start != "inherit" else None
        effective = requested or tier_of(resolved)
        resolved_tier = tier_of(resolved)
        hook_outcome = r.get("outcome") if isinstance(r.get("outcome"), str) else "ok"
        if hook_outcome in ("error", "empty"):
            eff_outcome = hook_outcome
        elif t and t["requests"] > 0:
            eff_outcome = "error" if t["last_is_error"] else "ok"
        else:
            eff_outcome = hook_outcome  # "async" stays unknown without telemetry
        # Only a known-good outcome is a usable "did it bounce?" label: an errored/empty
        # dispatch or an async launch we never saw finish must not become a fake "ok".
        label_status = LABELED if eff_outcome == "ok" else UNLABELED
        row: Dict[str, Any] = {
            "ts": r["ts"],
            "task_type": r["task_type"],
            "model": r["model"],
            "escalated": False,
            "label": "easy",
            "surface": r.get("surface") or CLAUDE_CODE_SURFACE,
            "requested_tier": start,
            "resolved_model": resolved,
            "matched": (resolved_tier == requested) if (requested and resolved_tier) else None,
            "effective_tier": effective,
            "tier": effective,
            # Asked for a Claude tier, ran a GPT model (or vice versa): not this tier's outcome.
            "cross_family": is_cross_family(requested, resolved_tier),
            "session_id": r.get("session_id"),
            "agent_id": r.get("agent_id"),
            "tool_use_id": r.get("tool_use_id"),
            "description": r.get("description"),
            "outcome": hook_outcome,
            "outcome_effective": eff_outcome,
            "label_status": label_status,
            "telemetry_joined": bool(t),
            "requests": t["requests"] if t else 0,
            "output_tokens": t["output_tokens"] if t else 0,
            "error_count": t["error_count"] if t else 0,
            "escalated_by": None,
        }
        cs = r.get("context_size")
        if cs is not None:
            row["context_size"] = cs
        out.append(row)

    # Escalation inference: same session + same normalized description + strictly later ts
    # within _ESCALATION_WINDOW_S + strictly higher tier. The EARLIER row must have started on
    # an explicit cheap tier (an inherit row's tier is the parent session's model, not a choice
    # to start cheap); the later row's tier is its explicit tier, else its resolved tier.
    # Unknown tiers / missing session or description never escalate (conservative: no label
    # is better than a fabricated one).
    by_session: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in out:
        if isinstance(row["session_id"], str):
            by_session[row["session_id"]].append(row)
    escalated = 0
    for group in by_session.values():
        group.sort(key=lambda x: x["ts"])
        for i, a in enumerate(group):
            a_start = a["requested_tier"]
            if a_start not in CHEAP_START_TIERS:
                continue
            ra = ALL_TIER_RANK[a_start]
            da = normalize_description(a["description"])
            if not da:
                continue
            for b in group[i + 1:]:
                dt = b["ts"] - a["ts"]
                if dt > _ESCALATION_WINDOW_S:
                    break  # sorted by ts: everything later is out of the window too
                tb = b["effective_tier"]
                rb = ALL_TIER_RANK.get(tb)
                if not (dt > 0 and rb is not None and normalize_description(b["description"]) == da):
                    continue
                if is_cross_family(a_start, tb):
                    # A redo in the OTHER family: ranks are not comparable across families, so
                    # this is recorded, never counted as an escalation (and kept out of rates).
                    a["cross_family"] = True
                    continue
                if rb > ra:
                    a["escalated"] = True
                    a["label"] = "hard"
                    a["escalated_by"] = b["tool_use_id"]
                    escalated += 1
                    break
    return out, {
        "claude_code_rows": len(out),
        "dispatch_deduped": deduped,
        "writer_parity": parity,
        "telemetry_joined": sum(1 for r in out if r["telemetry_joined"]),
        "escalated_inferred": escalated,
        "unlabeled": sum(1 for r in out if r["label_status"] == UNLABELED),
        "cross_family": sum(1 for r in out if r["cross_family"]),
    }


def join_labels(route_log_path=None, conformance_path=None, telemetry_path=None) -> dict:
    """Join route_log rows to conformance rows for the Phase-0 training table.

    Returns {"table": [...], "stats": {...}} on success, {} on any failure.
    Join rules:
      - task_type equal and |Δts| <= 300 s
      - prefer rows sharing a non-null session_id over ts-only proximity
      - each route_log row matches at most one conformance row (nearest ts)
      - agent-surface conformance rows with matched=None are excluded
      - route_log rows with missing/non-finite ts are unjoinable (counted as null_ts)
      - label_pending (claude-code dispatch) rows skip the conformance join; they are
        joined to telemetry by (session_id, agent_id) and get escalation inferred offline
        (see _build_dispatch_rows), then appended to the same table
    """
    try:
        log_p = Path(route_log_path) if route_log_path is not None else default_log_path()
        conf_p = Path(conformance_path) if conformance_path is not None else default_conformance_path()

        tel_p = Path(telemetry_path) if telemetry_path is not None else default_telemetry_path()

        read_errors: Dict[str, str] = {}
        route_rows, route_skipped = _parse_route_log(log_p, read_errors)
        conf_rows, conf_skipped = _parse_conformance(conf_p)
        dispatch_rows = [r for r in route_rows if r.get("label_pending") is True]
        classic_rows = [r for r in route_rows if r.get("label_pending") is not True]

        # Honesty invariant: agent intent-only rows are excluded from the join.
        usable_conf: List[Dict[str, Any]] = []
        excluded_agent_intent = 0
        for r in conf_rows:
            if r.get("surface") == "agent" and r.get("matched") is None:
                excluded_agent_intent += 1
                continue
            usable_conf.append(r)

        # Index usable conformance rows by task_type.
        conf_by_task: Dict[str, List[Tuple[int, Dict[str, Any]]]] = defaultdict(list)
        for idx, r in enumerate(usable_conf):
            conf_by_task[r["task_type"]].append((idx, r))

        used_conf_indices = set()
        table: List[Dict[str, Any]] = []
        null_ts = 0
        no_partner = 0

        for r in classic_rows:
            ts = r.get("ts")
            if not _is_finite_ts(ts):
                null_ts += 1
                continue

            candidates = conf_by_task.get(r["task_type"], [])
            best_idx: int | None = None
            best_diff = float("inf")

            # First pass: session-id match (must still satisfy the ts window).
            route_sid = r.get("session_id")
            if isinstance(route_sid, str):
                for idx, c in candidates:
                    if idx in used_conf_indices:
                        continue
                    c_sid = c.get("session_id")
                    if not isinstance(c_sid, str) or c_sid != route_sid:
                        continue
                    diff = abs(ts - c["ts"])
                    if diff <= _JOIN_WINDOW_S and diff < best_diff:
                        best_diff = diff
                        best_idx = idx

            # Second pass: ts-only proximity fallback.
            if best_idx is None:
                for idx, c in candidates:
                    if idx in used_conf_indices:
                        continue
                    diff = abs(ts - c["ts"])
                    if diff <= _JOIN_WINDOW_S and diff < best_diff:
                        best_diff = diff
                        best_idx = idx

            if best_idx is None:
                no_partner += 1
                continue

            used_conf_indices.add(best_idx)
            table.append(_build_joined(r, usable_conf[best_idx]))

        conformance_joined = len(table)
        timed_dispatch = []
        for r in dispatch_rows:
            if _is_finite_ts(r.get("ts")):
                timed_dispatch.append(r)
            else:
                null_ts += 1
        cc_table, cc_stats = _build_dispatch_rows(timed_dispatch, tel_p, read_errors)
        table.extend(cc_table)

        return {
            "table": table,
            "stats": {
                "route_rows": len(route_rows),
                "route_skipped": route_skipped,
                "conformance_rows": len(conf_rows),
                "conformance_skipped": conf_skipped,
                "excluded_agent_intent": excluded_agent_intent,
                "joined": conformance_joined,
                "table_rows": len(table),
                "null_ts": null_ts,
                "no_partner": no_partner,
                "claude_code_rows": cc_stats["claude_code_rows"],
                "dispatch_deduped": cc_stats["dispatch_deduped"],
                "writer_parity": cc_stats["writer_parity"],
                "telemetry_joined": cc_stats["telemetry_joined"],
                "escalated_inferred": cc_stats["escalated_inferred"],
                "unlabeled": cc_stats["unlabeled"],
                "cross_family": sum(1 for r in table if r.get("cross_family") is True),
                "route_log_error": "route_log" in read_errors,
                "route_log_error_name": read_errors.get("route_log"),
                "telemetry_error": "telemetry" in read_errors,
                "telemetry_error_name": read_errors.get("telemetry"),
            },
        }
    except Exception:
        return {}


def cell_rates(table) -> dict:
    """Aggregate a joined table into per-task-type escalation rates with Wilson CIs.

    Returns {task_type: {n, escalated, rate, ci, with_context, by_tier}}. The headline
    n/escalated/rate exclude GPT-tier rows (route_log.read_rates' contract: a GPT outcome is
    never priced as a Claude one); `by_tier` holds {tier: {n, escalated, rate}} for every tier,
    "other" for rows with no known tier. cross_family rows count nowhere. Fail-safe: any
    failure yields {}.
    """
    try:
        rates: Dict[str, Dict[str, Any]] = {}
        for row in table:
            if not isinstance(row, dict):
                continue
            tt = row.get("task_type")
            escalated = row.get("escalated")
            if not isinstance(tt, str) or not isinstance(escalated, bool):
                continue
            if row.get("label_status") == UNLABELED:
                continue
            if row.get("cross_family") is True:
                continue
            cell = rates.setdefault(tt, {
                "n": 0, "escalated": 0, "rate": 0.0,
                "ci": (0.0, 0.0), "with_context": 0, "by_tier": {},
            })
            tier = row.get("tier")
            if not (isinstance(tier, str) and tier in ALL_TIER_RANK):
                tier = tier_of(row.get("model"))
            sub = cell["by_tier"].setdefault(tier or OTHER_TIER, {"n": 0, "escalated": 0, "rate": 0.0})
            sub["n"] += 1
            sub["escalated"] += 1 if escalated else 0
            sub["rate"] = sub["escalated"] / sub["n"]
            if (tier_family(tier) == GPT_FAMILY or row.get("tier_family") == GPT_FAMILY
                    or (tier is None and is_gpt_family(row.get("model")))):
                continue
            cell["n"] += 1
            if escalated:
                cell["escalated"] += 1
            if row.get("context_size") is not None:
                cell["with_context"] += 1

        for cell in rates.values():
            n = cell["n"]
            k = cell["escalated"]
            cell["rate"] = k / n if n else 0.0
            try:
                cell["ci"] = stats.wilson_ci(k, n) if n > 0 else (0.0, 0.0)
            except Exception:
                cell["ci"] = (0.0, 0.0)
        return rates
    except Exception:
        return {}


def write_table(table, path) -> bool:
    """Atomically replace `path` with the table as JSONL (readers never see a partial file)."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp{os.getpid()}")
        with tmp.open("w", encoding="utf-8") as fh:
            for row in table:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def _has_rows(path: Path) -> bool:
    """True when `path` is a file with at least one non-blank line."""
    try:
        if not path.is_file():
            return False
        with path.open("r", errors="replace") as fh:
            return any(line.strip() for line in fh)
    except Exception:
        return True  # can't tell — treat as precious rather than clobber it


def refresh_labeled_table(result, path=None) -> Tuple[bool, str]:
    """The single write path for labeled_table.jsonl (route-join and the nightly).

    Refuses to replace an existing non-empty table when the new table is empty or was built
    from a failed read (stats route_log_error / telemetry_error), so a transient read failure
    can never silently strip labels route-advise depends on. Returns (written, note)."""
    try:
        p = Path(path) if path is not None else default_labeled_path()
        if not isinstance(result, dict) or not result:
            return False, f"kept previous table {p}: join failed"
        table = result.get("table") or []
        st = result.get("stats") or {}
        reasons = []
        if st.get("route_log_error"):
            reasons.append(f"route_log read failed ({st.get('route_log_error_name') or 'error'})")
        if st.get("telemetry_error"):
            reasons.append(f"telemetry read failed ({st.get('telemetry_error_name') or 'error'})")
        if not table:
            reasons.append("new table is empty")
        if reasons and _has_rows(p):
            return False, f"kept previous table {p}: " + "; ".join(reasons)
        ok = write_table(table, p)
        return ok, (f"wrote {len(table)} rows to {p}" if ok else f"write failed: {p}")
    except Exception as e:  # noqa: BLE001
        return False, f"labeled table not refreshed ({type(e).__name__})"


def main(argv=None) -> int:
    """CLI readout for the Phase-0 join: human table, --json, or --out JSONL."""
    import argparse
    ap = argparse.ArgumentParser(
        prog="route-join",
        description="Phase-0 labeled training table: route_log x conformance join",
    )
    ap.add_argument("--json", action="store_true",
                    help="dump machine-readable join result to stdout")
    ap.add_argument("--out", type=Path,
                    help="also write the labeled table as JSONL to PATH")
    ap.add_argument("--telemetry", type=Path,
                    help="proxy telemetry JSONL (default $APEX_TELEMETRY, else "
                         "$APEX_HOME/telemetry.jsonl, else ~/.apex/telemetry.jsonl)")
    ap.add_argument("--no-write", action="store_true",
                    help="do not refresh the default labeled table beside the route log")
    args = ap.parse_args(argv)

    try:
        result = join_labels(telemetry_path=args.telemetry)
        if not isinstance(result, dict):
            result = {}
        table = result.get("table", [])
        st = result.get("stats", {})

        if args.out:
            write_table(table, args.out)
        if not args.no_write:
            # route-readout / route-advise read resolved claude-code labels from here.
            written, note = refresh_labeled_table(result, default_labeled_path())
            if not written:
                print(f"route-join: {note}", file=sys.stderr)

        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        print("route-join: Phase-0 labeled training table (route_log x conformance)")
        print(f"  route_log rows:     parsed={st.get('route_rows', 0)}  skipped={st.get('route_skipped', 0)}")
        print(f"  conformance rows:   parsed={st.get('conformance_rows', 0)}  skipped={st.get('conformance_skipped', 0)}  "
              f"excluded_agent_intent={st.get('excluded_agent_intent', 0)}")
        print(f"  join result:        joined={st.get('joined', 0)}  null_ts={st.get('null_ts', 0)}  "
              f"no_partner={st.get('no_partner', 0)}")
        print(f"  claude-code rows:   {st.get('claude_code_rows', 0)}  "
              f"telemetry_joined={st.get('telemetry_joined', 0)}  "
              f"escalated_inferred={st.get('escalated_inferred', 0)}  "
              f"unlabeled={st.get('unlabeled', 0)}")
        wp = st.get("writer_parity") or {}
        print(f"  writer parity:      plugin>=hook since {wp.get('parity_since')} "
              f"until {wp.get('parity_until')} ({wp.get('parity_span_days', 0)} d span, "
              f"{wp.get('parity_days', 0)} clean days; gate {'OPEN' if wp.get('gate_open') else 'closed'}: the 0.4.2 hook "
              f"retirement needs span >= 14, matched >= 10 and the last matched day within 2 days)")
        for k in ("route_log", "telemetry"):
            if st.get(f"{k}_error"):
                print(f"  WARNING: {k} read failed ({st.get(f'{k}_error_name')})")

        if not table:
            print("route-join: no joinable rows yet")
            return 0

        rates = cell_rates(table)
        print(f"\n{'task_type':<12} {'n':>5} {'escalated':>10} {'rate':>7} {'95% CI':>15} {'with_ctx':>9}")
        for tt in sorted(rates):
            r = rates[tt]
            lo, hi = r["ci"]
            print(f"{tt:<12} {r['n']:>5} {r['escalated']:>10} {r['rate']:>7.2f} "
                  f"[{lo:>6.2f},{hi:>6.2f}] {r['with_context']:>9}")
            by_tier = r.get("by_tier") or {}
            for tier in sorted(by_tier, key=lambda t: (tier_family(t) or "~", ALL_TIER_RANK.get(t, 99), t)):
                b = by_tier[tier]
                print(f"  {tier:<11}{b['n']:>5} {b['escalated']:>10} {b['rate']:>7.2f}")
        if st.get("cross_family"):
            print(f"\n  cross_family rows (Claude <-> GPT, excluded from every rate): {st['cross_family']}")
    except Exception:
        # Fail-safe: never let a readout failure become a caller failure.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
