"""Read-only status blocks that the LAN mesh serves (``mesh.datasets``): pressure, errors, measure.

Moved unchanged from ``snapshot.py`` (the menu-bar widget, retired by owner decision D25). Nothing
here writes: no ``pressure.json``, no cache, no log.

  pressure   ``pressure.compute()`` over the last 15 min of proxy telemetry (level, sample, causes;
             ``families`` maps family -> GREEN/AMBER/RED like the plugin's ``parsePressure``)
  errors15m  client-visible failures (``is_error`` rows) in the last 15 min, by ``error_cause``
  measure    the newest plugin ``measure`` event in ``~/.apex-router/observe/*.jsonl`` (5h/7d limit
             meter, cost) with its age — only as fresh as the last open plugin session
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import Counter
from pathlib import Path

from . import pressure
from .agent_resources.base import _num

ERRORS_WINDOW_S = 15 * 60
OBSERVE_FILES_MAX = 3
OBSERVE_TAIL_BYTES = 1 << 20
_PLUGIN_LEVELS = ("GREEN", "AMBER", "RED")


def router_home() -> Path:
    return Path(os.environ.get("APEX_ROUTER_HOME") or Path.home() / ".apex-router")


def _epoch_s(v):
    """Seconds since the epoch from seconds or milliseconds; None if not a number."""
    v = _num(v)
    if v is None or not math.isfinite(v):  # json accepts Infinity/NaN
        return None
    return v / 1000.0 if v > 1e12 else float(v)


def pressure_block(telemetry=None, now: float | None = None) -> dict:
    r = pressure.compute(telemetry, now=now)          # compute() never writes pressure.json
    ov = r.get("overall") or {}
    out = {
        "level": r.get("level", "UNKNOWN"),
        "insufficient_sample": bool(r.get("insufficient_sample", True)),
        "min_sample": r.get("min_sample"),
        "window_min": r.get("window_min"),
        "n": ov.get("requests", 0),
        "causes": {k: ov.get(k) for k in ("rate_limited", "rate_limited_rate", "transport_errors",
                                          "transport_rate", "retried", "local",
                                          "upstream_rejected", "upstream_rejected_rate")},
        "p50_ttft_ms": ov.get("p50_ttft_ms"),
        "retry_after_s": r.get("retry_after_s"),
        "retry_after_recent": r.get("retry_after_recent", False),
        "recommendation": r.get("recommendation"),
        "families": {f: b.get("level") for f, b in (r.get("families") or {}).items()
                     if isinstance(b, dict) and b.get("level") in _PLUGIN_LEVELS},
    }
    if r.get("error"):
        out["error"] = r["error"]
    return out


def errors_block(telemetry=None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    path = Path(telemetry) if telemetry else pressure.default_telemetry_path()
    causes: Counter = Counter()
    for row in pressure.tail_rows(path, now - ERRORS_WINDOW_S):
        if row.get("is_error"):
            causes[str(row.get("error_cause") or "unlabeled")] += 1
    return {"n": sum(causes.values()), "by_cause": dict(causes.most_common()),
            "window_min": ERRORS_WINDOW_S // 60}


def _tail_lines(path: Path, max_bytes: int = OBSERVE_TAIL_BYTES) -> list:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        data = fh.read()
    lines = data.split(b"\n")
    if size > max_bytes:
        lines = lines[1:]                               # first line may be cut mid-row
    return lines


def _pick(row: dict, *keys):
    for k in keys:
        if row.get(k) is not None:
            return row[k]
    return None


def measure_block(observe_dir=None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    d = Path(observe_dir) if observe_dir else router_home() / "observe"
    try:
        files = sorted(d.glob("*.jsonl"), reverse=True)[:OBSERVE_FILES_MAX]
    except OSError:
        files = []
    for f in files:
        try:
            lines = _tail_lines(f)
        except OSError:
            continue
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(row, dict) or row.get("ev") != "measure":
                continue
            ts = _epoch_s(row.get("ts"))
            return {
                "limit_kind": _pick(row, "limit_kind", "limitKind"),
                "limit_pct": _num(_pick(row, "limit_pct", "limit_percent", "limitPercent")),
                "resets_at": _pick(row, "resets_at", "resetsAt"),
                "cost_usd": _num(_pick(row, "cost_usd", "costUsd")),
                "ctx_pct": _num(_pick(row, "ctx_pct", "ctx_percent", "ctxPercent")),
                "ts": ts,
                "age_s": round(max(0.0, now - ts), 1) if ts is not None else None,
            }
    return {"limit_kind": None, "limit_pct": None, "resets_at": None, "cost_usd": None,
            "ctx_pct": None, "ts": None, "age_s": None, "missing": True}
