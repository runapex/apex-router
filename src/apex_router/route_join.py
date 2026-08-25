"""Phase-0 labeled training table — join route_log outcomes with conformance rows.

Fail-safe read-only module: malformed lines are skipped, unjoinable rows are counted,
and every public function returns an empty container rather than raising on failure.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import stats
from .route_conformance import default_conformance_path
from .route_log import default_log_path

_JOIN_WINDOW_S = 300.0


def _is_finite_ts(value: Any) -> bool:
    """True for finite int/float timestamps (bools and non-finites rejected)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _parse_route_log(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Stream a route_log JSONL into validated rows plus a malformed skip count.

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
                rows.append(rec)
    except Exception:
        # Fail-safe: unreadable files yield whatever we parsed so far (often nothing).
        pass
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


def join_labels(route_log_path=None, conformance_path=None) -> dict:
    """Join route_log rows to conformance rows for the Phase-0 training table.

    Returns {"table": [...], "stats": {...}} on success, {} on any failure.
    Join rules:
      - task_type equal and |Δts| <= 300 s
      - prefer rows sharing a non-null session_id over ts-only proximity
      - each route_log row matches at most one conformance row (nearest ts)
      - agent-surface conformance rows with matched=None are excluded
      - route_log rows with missing/non-finite ts are unjoinable (counted as null_ts)
    """
    try:
        log_p = Path(route_log_path) if route_log_path is not None else default_log_path()
        conf_p = Path(conformance_path) if conformance_path is not None else default_conformance_path()

        route_rows, route_skipped = _parse_route_log(log_p)
        conf_rows, conf_skipped = _parse_conformance(conf_p)

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

        for r in route_rows:
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

        return {
            "table": table,
            "stats": {
                "route_rows": len(route_rows),
                "route_skipped": route_skipped,
                "conformance_rows": len(conf_rows),
                "conformance_skipped": conf_skipped,
                "excluded_agent_intent": excluded_agent_intent,
                "joined": len(table),
                "null_ts": null_ts,
                "no_partner": no_partner,
            },
        }
    except Exception:
        return {}


def cell_rates(table) -> dict:
    """Aggregate a joined table into per-task-type escalation rates with Wilson CIs.

    Returns {task_type: {n, escalated, rate, ci, with_context}}. Fail-safe: any
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
            cell = rates.setdefault(tt, {
                "n": 0, "escalated": 0, "rate": 0.0,
                "ci": (0.0, 0.0), "with_context": 0,
            })
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
                    help="write the labeled table as JSONL to PATH")
    args = ap.parse_args(argv)

    try:
        result = join_labels()
        if not isinstance(result, dict):
            result = {}
        table = result.get("table", [])
        st = result.get("stats", {})

        if args.out:
            with args.out.open("w", encoding="utf-8") as fh:
                for row in table:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        print("route-join: Phase-0 labeled training table (route_log x conformance)")
        print(f"  route_log rows:     parsed={st.get('route_rows', 0)}  skipped={st.get('route_skipped', 0)}")
        print(f"  conformance rows:   parsed={st.get('conformance_rows', 0)}  skipped={st.get('conformance_skipped', 0)}  "
              f"excluded_agent_intent={st.get('excluded_agent_intent', 0)}")
        print(f"  join result:        joined={st.get('joined', 0)}  null_ts={st.get('null_ts', 0)}  "
              f"no_partner={st.get('no_partner', 0)}")

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
    except Exception:
        # Fail-safe: never let a readout failure become a caller failure.
        pass
    return 0
