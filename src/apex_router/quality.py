"""Quality over the last 24 h for the menu-bar widget: routing classification, tier conformance,
retries / errors, and verification accuracy. Read-only and fail-open, like the rest of the
snapshot: every source is a log another component already writes, and each block reports its own
``error`` instead of raising.

Sources (all local):

- **classification** — ``route_log.jsonl`` (``apex-router route-log`` / the pi + hook surfaces):
  one row per routed task with its ``task_type``, the model it ran on and whether it ``escalated``
  (bounced to a bigger tier). Escalation rate = the router's misclassification signal.
- **conformance** — ``conformance.jsonl``: did the resolved model match the tier the router asked
  for (``matched``)? Rows without an observed model (``matched`` null) are kept out of the
  denominator, exactly as ``route-check`` does.
- **reliability** — proxy telemetry: requests, connect / read retries (``connect_retries``),
  errors by ``error_cause``, upstream rejections (4xx/5xx; a 400 is not ``is_error``) and
  broken streams (``midstream_*``).
- **accuracy** — ``xval_runs.jsonl`` (cross-validation reviews: finished with a verdict = ``ok``,
  marked ``bad`` by feedback) and ``codeqa_impact.jsonl`` (citations of codeqa answers checked
  against the code: grounded / stale / hallucinated).

What it does NOT know: whether an answer was *correct* beyond those checks. "accuracy" here is
the share of verifiable claims that verified, and of reviews that reached a verdict.
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import Counter
from pathlib import Path

from . import pressure

WINDOW_S = 24 * 3600
TAIL_BYTES = 4 * 1024 * 1024        # per small log: far more than a day of rows


def _home() -> Path:
    return Path(os.environ.get("APEX_ROUTER_HOME") or Path.home() / ".apex-router")


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return None
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


def _ts(row) -> float | None:
    t = _num(row.get("ts"))
    if t is None:
        return None
    return t / 1000 if t > 1e11 else t


def _bool(v):
    if isinstance(v, bool):
        return v
    if v in ("True", "true"):
        return True
    if v in ("False", "false"):
        return False
    return None


def _recent(path: Path, since: float) -> list:
    """Rows of a small jsonl log with ts >= since (reads at most TAIL_BYTES from the end)."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - TAIL_BYTES))
        data = fh.read()
    lines = data.split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]
    out = []
    for ln in lines:
        try:
            row = json.loads(ln)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(row, dict) and (_ts(row) or 0) >= since:
            out.append(row)
    return out


def _guard(fn, *a):
    try:
        return fn(*a)
    except FileNotFoundError:
        return {"missing": True}
    except Exception as e:  # noqa: BLE001 — a quality source fails open on its own
        return {"error": f"{type(e).__name__}: {e}"}


# ---- blocks ---------------------------------------------------------------------------------

def classification(since: float, home: Path) -> dict:
    rows = [r for r in _recent(home / "route_log.jsonl", since)
            if r.get("label_pending") is not True and isinstance(r.get("task_type"), str)
            and isinstance(_bool(r.get("escalated")), bool)]
    by = Counter(r["task_type"] for r in rows)
    esc = [r for r in rows if _bool(r.get("escalated"))]
    why = Counter(str(r.get("note") or "?").split(": ", 1)[-1] for r in esc)
    return {"n": len(rows), "types": dict(by.most_common()), "escalated": len(esc),
            "escalated_by_type": dict(Counter(r["task_type"] for r in esc).most_common()),
            "escalation_causes": dict(why.most_common())}


def conformance(since: float, home: Path) -> dict:
    rows = _recent(home / "conformance.jsonl", since)
    obs = [r for r in rows if isinstance(_bool(r.get("matched")), bool)]
    miss = [r for r in obs if not _bool(r.get("matched"))]
    top = Counter(f"{r.get('task_type')}: {r.get('requested_tier')} → {r.get('resolved_model')}"
                  for r in miss)
    return {"n": len(rows), "observed": len(obs), "mismatched": len(miss),
            "mismatches": dict(top.most_common(3))}


def reliability(since: float, telemetry=None) -> dict:
    path = Path(telemetry) if telemetry else pressure.default_telemetry_path()
    rows = [r for r in pressure.tail_rows(path, since) if "model_requested" in r]
    retried = [r for r in rows if (_num(r.get("connect_retries")) or 0) > 0]
    failed_rows = [r for r in rows if r.get("is_error") or r.get("upstream_rejected")]
    failed = len(failed_rows)
    causes = Counter(str(r["error_cause"]) for r in failed_rows if r.get("error_cause"))
    return {"requests": len(rows), "failed": failed,
            "retried_requests": len(retried),
            "retries": int(sum(_num(r.get("connect_retries")) or 0 for r in rows)),
            "retry_wait_ms": round(sum(_num(r.get("connect_backoff_ms")) or 0 for r in rows)),
            "midstream": sum(n for c, n in causes.items() if c.startswith("midstream_")),
            "causes": dict(causes.most_common())}


def verification(since: float, home: Path) -> dict:
    out: dict = {}
    try:
        xv = _recent(home / "xval_runs.jsonl", since)
        ok = [r for r in xv if _bool(r.get("ok")) is True]
        bad = [r for r in xv if str(r.get("feedback") or "") == "bad"]
        out["xval"] = {"runs": len(xv), "ok": len(ok), "bad_feedback": len(bad),
                       "truncated": sum(1 for r in xv if (_num(r.get("truncated_outputs")) or 0)),
                       "median_s": _median([_num(r.get("seconds")) for r in xv])}
    except FileNotFoundError:
        out["xval"] = {"missing": True}
    except Exception as e:  # noqa: BLE001
        out["xval"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        cq = _recent(Path.home() / ".apex" / "codeqa_impact.jsonl", since)
        g = Counter()
        for r in cq:
            gr = r.get("grounding") if isinstance(r.get("grounding"), dict) else {}
            for k in ("grounded", "stale", "hallucinated"):
                g[k] += int(_num(gr.get(k)) or 0)
        out["codeqa"] = {"questions": len(cq), **{k: g[k] for k in
                                                  ("grounded", "stale", "hallucinated")}}
    except FileNotFoundError:
        out["codeqa"] = {"missing": True}
    except Exception as e:  # noqa: BLE001
        out["codeqa"] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def collect(now: float | None = None, telemetry=None, home=None) -> dict:
    now = time.time() if now is None else now
    since = now - WINDOW_S
    h = Path(home) if home else _home()
    return {"window_h": WINDOW_S // 3600,
            "classification": _guard(classification, since, h),
            "conformance": _guard(conformance, since, h),
            "reliability": _guard(reliability, since, telemetry),
            "verification": _guard(verification, since, h)}
