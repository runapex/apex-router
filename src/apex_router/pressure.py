"""Upstream pressure readout — a signal an orchestrator consults *before* a fan-out.

Every Claude Code / Codex request on this machine passes through the measuring proxy, which
appends one JSON row per request to ``~/.apex/telemetry.jsonl``. This module tail-reads the last
``window`` minutes of that file (seeking from the end; it never parses the whole file), computes
429 / transport-error rates per model family and overall, and maps them to a level with a
recommendation the model-routing skill quotes verbatim:

  GREEN  429 < 2% and transport < 3%            -> dispatch as planned
  AMBER  429 2–10% or transport 3–10%           -> shed one tier down; cap heavy parallelism at 2
  RED    either > 10%, or retry-after seen <2m  -> no new heavy fan-out; serialize; wait
  UNKNOWN telemetry missing/unreadable, or the readout itself crashed

Fewer than MIN_SAMPLE requests in the window cannot support a rate: the level is GREEN with an
"insufficient sample" note (a fresh retry-after still forces RED). ``apex-router pressure --check``
exits 0/1/2/3 for GREEN/AMBER/RED/UNKNOWN; 4 is a usage error. A shell ``if`` can gate a
dispatch. The latest readout is also written atomically to ``~/.apex-router/pressure.json`` so a
hook can read it without re-scanning telemetry. Stdlib only; read-only over telemetry.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import tempfile
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

from .telemetry_path import telemetry_path

# ---- thresholds (fractions). AMBER at >= amber; RED at > red. -------------------------------
AMBER_429 = 0.02
AMBER_TRANSPORT = 0.03
RED_RATE = 0.10
RECENT_S = 120.0          # retry-after freshness for RED, and the in-flight agent window
DEFAULT_WINDOW_MIN = 15
# Rows are appended when a request finishes, but `ts` may be the request start; a long streaming
# request can land after newer ones. Keep reading this far past the window edge before stopping.
ORDER_SLACK_S = 600.0
# One old row must never end the scan (a long stream finishing now is the newest line but carries
# its START ts). Stop only after this many consecutive rows older than the slack edge...
STOP_AFTER_OLD_ROWS = 200
# ...or on any row older than the slack edge by more than this (no request lives this long).
HARD_STOP_S = 6 * 3600.0
MIN_SAMPLE = 10            # below this many requests a rate is noise: report GREEN + note
MAX_RETRY_AFTER_S = 86400  # a retry-after beyond a day is treated as unparseable
DEFAULT_RETRY_WAIT_S = 60

FAMILIES = ("haiku", "sonnet", "opus", "fable", "other")
LEVELS = ("GREEN", "AMBER", "RED", "UNKNOWN")
EXIT_CODES = {"GREEN": 0, "AMBER": 1, "RED": 2, "UNKNOWN": 3}
EXIT_USAGE = 4

# Quoted verbatim by the model-routing skill. RED's "{wait}" is the retry-after seconds when one
# was seen in the last 2 minutes, else 60.
RECOMMENDATIONS = {
    "GREEN": "dispatch as planned",
    "AMBER": ("shed mechanical and exploration subagents one tier down (opus→sonnet, "
              "sonnet→haiku); cap parallel heavy agents at 2"),
    "RED": ("no new heavy fan-out; serialize; mechanical work to haiku or the local tier; "
            "wait {wait} s before retrying heavy"),
    "UNKNOWN": ("no pressure signal (telemetry unreadable); dispatch conservatively, "
                "as for AMBER"),
}
# PoolTimeout = our own connection pool was exhausted: local back-pressure, not an upstream fault.
LOCAL_CAUSES = frozenset({"PoolTimeout"})

_HEADER_KEYS = ("retry-after",)
_HEADER_PREFIX = "anthropic-ratelimit-"


def default_telemetry_path() -> Path:
    return telemetry_path()


def default_state_path() -> Path:
    return Path.home() / ".apex-router" / "pressure.json"


def family(model) -> str:
    m = (model or "").lower() if isinstance(model, str) else ""
    for fam in ("haiku", "sonnet", "opus", "fable"):
        if fam in m:
            return fam
    return "other"


def _env_frac(name: str):
    try:
        v = float(os.environ.get(name, ""))
    except ValueError:
        return None
    return v if 0.0 <= v <= 1.0 else None


def thresholds() -> dict:
    """Effective thresholds. APEX_PRESSURE_AMBER sets both amber floors (429 and transport);
    APEX_PRESSURE_RED sets the shared red ceiling. Unparseable values fall back to defaults."""
    amber = _env_frac("APEX_PRESSURE_AMBER")
    red = _env_frac("APEX_PRESSURE_RED")
    return {
        "amber_429": AMBER_429 if amber is None else amber,
        "amber_transport": AMBER_TRANSPORT if amber is None else amber,
        "red": RED_RATE if red is None else red,
    }


def parse_retry_after(value, now: float | None = None):
    """Seconds from a Retry-After value (delta-seconds or HTTP-date); None if unparseable,
    non-finite (inf/nan) or absurd (> MAX_RETRY_AFTER_S)."""
    if value is None:
        return None
    s = str(value).strip()
    try:
        secs = max(0, math.ceil(float(s)))
    except (ValueError, OverflowError):
        try:
            dt = parsedate_to_datetime(s)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if dt is None:
            return None
        try:
            secs = max(0, round(dt.timestamp() - (time.time() if now is None else now)))
        except (ValueError, OverflowError, OSError):
            return None
    return secs if secs <= MAX_RETRY_AFTER_S else None


# ---- tail read -------------------------------------------------------------------------------

def _parse(line: bytes):
    try:
        row = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(row, dict) or row.get("ev") == "hb" or "client" not in row:
        return None
    ts = row.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    return row


def tail_rows(path, since_ts: float, block: int = 1 << 16, slack_s: float = ORDER_SLACK_S,
              stop_after: int = STOP_AFTER_OLD_ROWS, hard_stop_s: float = HARD_STOP_S):
    """Yield request rows with ts >= since_ts, newest-first, reading the file backwards in
    blocks. A trailing line without a newline (writer mid-append) is dropped.

    Rows are appended at request END but carry the request START ts, so the file is only roughly
    ordered. With ``stop_ts = since_ts - slack_s``, the scan stops after ``stop_after``
    CONSECUTIVE rows older than stop_ts, or on any row older than ``stop_ts - hard_stop_s``.
    Raises OSError if the file cannot be opened (the caller reports UNKNOWN)."""
    f = open(path, "rb")
    with f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        carry = b""
        first = True
        stop_ts = since_ts - slack_s
        hard_ts = stop_ts - hard_stop_s
        old_run = 0
        while pos > 0:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + carry
            if first:
                # Drop an unterminated last line; if this block has no newline at all, the whole
                # block is part of that partial line — keep it in carry and drop it later.
                cut = data.rfind(b"\n")
                if cut == -1:
                    carry = data
                    continue
                data = data[:cut + 1] if not data.endswith(b"\n") else data
                first = False
            lines = data.split(b"\n")
            carry = lines[0] if pos > 0 else b""
            body = lines[1:] if pos > 0 else lines
            for line in reversed(body):
                if not line.strip():
                    continue
                row = _parse(line)
                if row is None:
                    continue
                ts = row["ts"]
                if ts < stop_ts:
                    if ts < hard_ts:
                        return
                    old_run += 1
                    if old_run >= stop_after:
                        return
                    continue
                old_run = 0
                if ts >= since_ts:
                    yield row
        # if `first` is still set the file was one unterminated line: nothing to yield


# ---- aggregation -----------------------------------------------------------------------------

def _classify(row) -> str | None:
    cause = row.get("error_cause")
    if not cause:
        return None
    cause = str(cause)
    if cause in LOCAL_CAUSES:
        return "local"
    if cause == "http_429":
        return "rate_limited"
    if cause.startswith("http_5"):
        return "transport"          # 5xx/529 overloaded: upstream-side failure, not our request
    if cause.startswith("http_"):
        return None                 # 4xx client errors (400/404/413) are not pressure
    return "transport"              # SSLError, ReadError, ConnectError, timeouts…


def _rate_headers(row) -> dict:
    det = row.get("error_detail")
    hdrs = det.get("headers") if isinstance(det, dict) else None
    if not isinstance(hdrs, dict):
        return {}
    out = {}
    for k, v in hdrs.items():
        lk = str(k).lower()
        if lk in _HEADER_KEYS or lk.startswith(_HEADER_PREFIX):
            out[lk] = str(v)
    return out


def _retries(row) -> int:
    v = row.get("connect_retries")
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _new_bucket():
    return {"requests": 0, "rate_limited": 0, "transport_errors": 0, "retried": 0, "local": 0,
            "upstream_rejected": 0, "_ttft": [], "_agents": set(), "_retry_recent": False}


def _level(b, th) -> str:
    n = b["requests"]
    if b["_retry_recent"]:
        return "RED"              # the provider told us to back off: no sample floor applies
    if n < MIN_SAMPLE:
        return "GREEN"            # insufficient sample; rates are still reported
    r429 = b["rate_limited"] / n
    rtr = b["transport_errors"] / n
    if r429 > th["red"] or rtr > th["red"]:
        return "RED"
    if r429 >= th["amber_429"] or rtr >= th["amber_transport"]:
        return "AMBER"
    return "GREEN"


def _finish(b, th) -> dict:
    n = b["requests"]
    return {
        "requests": n,
        "rate_limited": b["rate_limited"],
        "rate_limited_rate": round(b["rate_limited"] / n, 4) if n else 0.0,
        "transport_errors": b["transport_errors"],
        "transport_rate": round(b["transport_errors"] / n, 4) if n else 0.0,
        "retried": b["retried"],
        "local": b["local"],
        "upstream_rejected": b["upstream_rejected"],
        "p50_ttft_ms": round(statistics.median(b["_ttft"]), 1) if b["_ttft"] else None,
        "inflight_agents": len(b["_agents"]),
        "level": _level(b, th),
        "insufficient_sample": n < MIN_SAMPLE,
    }


def compute(telemetry=None, now: float | None = None,
            window_min: float = DEFAULT_WINDOW_MIN) -> dict:
    now = time.time() if now is None else now
    path = Path(telemetry) if telemetry else default_telemetry_path()
    th = thresholds()
    since = now - window_min * 60.0
    overall = _new_bucket()
    fams = {}
    headers: dict = {}
    headers_ts = None
    retry_after_raw, retry_after_ts = None, None
    try:
        rows = list(tail_rows(path, since))       # newest-first
    except OSError as e:
        return _unknown(path, now, window_min, th, f"{type(e).__name__}: {e}")
    for row in rows:
        ts = row["ts"]
        fam = family(row.get("model_requested"))
        fb = fams.setdefault(fam, _new_bucket())
        kind = _classify(row)
        recent = now - ts <= RECENT_S
        h = _rate_headers(row)
        if h:
            if headers_ts is None:
                headers_ts = ts
            for k, v in h.items():
                headers.setdefault(k, v)          # newest value per key wins
            if "retry-after" in h:
                if retry_after_ts is None:
                    retry_after_raw, retry_after_ts = h["retry-after"], ts
                if recent:
                    overall["_retry_recent"] = fb["_retry_recent"] = True
        for b in (overall, fb):
            b["requests"] += 1
            if kind == "rate_limited":
                b["rate_limited"] += 1
            elif kind == "local":
                b["local"] += 1
            retried = _retries(row) > 0
            if retried:
                b["retried"] += 1
            # A row is one transport fault if its cause is transport OR the proxy had to retry
            # the connect (a retried-then-succeeded row has error_cause None). Counted once.
            if kind == "transport" or retried:
                b["transport_errors"] += 1
            if row.get("upstream_rejected") is True:
                b["upstream_rejected"] += 1
            t = row.get("ttft_ms")
            if (kind is None and not row.get("is_error") and not row.get("error_cause")
                    and isinstance(t, (int, float)) and not isinstance(t, bool)):
                b["_ttft"].append(float(t))
            if recent and row.get("agent_id"):
                b["_agents"].add(row["agent_id"])

    ov = _finish(overall, th)
    level = ov.pop("level")
    insufficient = ov.pop("insufficient_sample")
    retry_s = parse_retry_after(retry_after_raw, now=now)
    retry_recent = retry_after_ts is not None and now - retry_after_ts <= RECENT_S
    wait = retry_s if (retry_recent and retry_s) else DEFAULT_RETRY_WAIT_S
    return {
        "level": level,
        "insufficient_sample": insufficient,
        "min_sample": MIN_SAMPLE,
        "recommendation": RECOMMENDATIONS[level].format(wait=wait),
        "generated_at": now,
        "window_min": window_min,
        "telemetry": str(path),
        "thresholds": th,
        "overall": ov,
        "families": {f: _finish(fams[f], th) for f in FAMILIES if f in fams},
        "headers": headers,
        "headers_age_s": round(now - headers_ts, 1) if headers_ts is not None else None,
        "retry_after_s": retry_s,
        "retry_after_recent": retry_recent,
    }


def _unknown(path, now, window_min, th, error) -> dict:
    return {
        "level": "UNKNOWN",
        "error": error,
        "insufficient_sample": True,
        "min_sample": MIN_SAMPLE,
        "recommendation": RECOMMENDATIONS["UNKNOWN"],
        "generated_at": now,
        "window_min": window_min,
        "telemetry": str(path),
        "thresholds": th,
        "overall": {k: v for k, v in _finish(_new_bucket(), th).items()
                    if k not in ("level", "insufficient_sample")},
        "families": {},
        "headers": {},
        "headers_age_s": None,
        "retry_after_s": None,
        "retry_after_recent": False,
    }


def level_label(level: str, insufficient: bool) -> str:
    if level == "GREEN" and insufficient:
        return f"GREEN (insufficient sample: n<{MIN_SAMPLE})"
    return level


def write_state(readout: dict, path=None) -> Path:
    """Atomically replace the state file (tmp in the same dir + os.replace)."""
    path = Path(path) if path else default_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pressure.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(readout, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _pct(x) -> str:
    return f"{100 * x:.1f}%"


def render(r: dict) -> str:
    ov = r["overall"]
    if r["level"] == "UNKNOWN":
        return "\n".join([f"pressure: UNKNOWN ({r.get('error', 'no telemetry')})",
                          f"telemetry: {r['telemetry']}",
                          f"recommendation: {r['recommendation']}"])
    label = level_label(r["level"], r.get("insufficient_sample", False))
    lines = [f"pressure: {label}  (last {r['window_min']:g} min, {ov['requests']} requests)",
             f"recommendation: {r['recommendation']}", ""]
    hdr = f"{'family':<8} {'req':>5} {'429':>5} {'429%':>6} {'xport':>5} {'xport%':>7} " \
          f"{'retried':>7} {'local':>5} {'p50ttft':>8} {'agents2m':>8}  level"
    lines.append(hdr)
    rows = list(r["families"].items()) + [
        ("ALL", dict(ov, level=r["level"], insufficient_sample=r.get("insufficient_sample")))]
    for name, b in rows:
        ttft = f"{b['p50_ttft_ms']:.0f}" if b["p50_ttft_ms"] is not None else "-"
        lines.append(f"{name:<8} {b['requests']:>5} {b['rate_limited']:>5} "
                     f"{_pct(b['rate_limited_rate']):>6} {b['transport_errors']:>5} "
                     f"{_pct(b['transport_rate']):>7} {b['retried']:>7} {b['local']:>5} "
                     f"{ttft:>8} {b['inflight_agents']:>8}  "
                     f"{level_label(b['level'], b.get('insufficient_sample', False))}")
    if r["headers"]:
        lines.append("")
        lines.append(f"newest rate-limit headers ({r['headers_age_s']:.0f}s ago):")
        for k in sorted(r["headers"]):
            lines.append(f"  {k}: {r['headers'][k]}")
    th = r["thresholds"]
    lines.append("")
    lines.append(f"thresholds: AMBER 429>={_pct(th['amber_429'])} or transport>="
                 f"{_pct(th['amber_transport'])}; RED either>{_pct(th['red'])} or "
                 f"retry-after within {RECENT_S:.0f}s; n<{MIN_SAMPLE} -> GREEN (insufficient "
                 f"sample); xport includes connect-retried rows, excludes local PoolTimeout")
    return "\n".join(lines)


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """Usage errors exit 4 (not argparse's 2, which --check uses for RED)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise _UsageError(message)


def _build_parser() -> argparse.ArgumentParser:
    ap = _Parser(
        prog="apex-router pressure",
        description="Upstream rate-limit pressure from proxy telemetry; gate a fan-out on it.")
    ap.add_argument("--window", type=float, default=DEFAULT_WINDOW_MIN,
                    help=f"minutes of telemetry to read (default {DEFAULT_WINDOW_MIN})")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--check", action="store_true",
                    help="exit 0 GREEN / 1 AMBER / 2 RED / 3 UNKNOWN (usage error: 4)")
    ap.add_argument("--telemetry", type=Path, default=None,
                    help="telemetry JSONL (default $APEX_TELEMETRY, else "
                         "$APEX_HOME/telemetry.jsonl, else ~/.apex/telemetry.jsonl)")
    ap.add_argument("--state", type=Path, default=None,
                    help="where to write the latest readout (default ~/.apex-router/pressure.json)")
    ap.add_argument("--no-write", action="store_true", help="do not write the state file")
    return ap


def _emit(text: str) -> None:
    try:
        print(text)
    except (BrokenPipeError, OSError):
        pass


def main(argv=None, now: float | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
    except _UsageError:
        return EXIT_USAGE
    try:
        r = compute(args.telemetry, now=now, window_min=args.window)
        if not args.no_write:
            try:
                write_state(r, args.state)
            except OSError as e:
                print(f"pressure: state file not written ({e})", file=sys.stderr)
        _emit(json.dumps(r, indent=2, sort_keys=True) if args.json else render(r))
        return EXIT_CODES[r["level"]] if args.check else 0
    except Exception as e:  # noqa: BLE001 — a crashed gate must read UNKNOWN, never GREEN
        msg = f"{type(e).__name__}: {e}"
        _emit(json.dumps({"level": "UNKNOWN", "error": msg}) if args.json
              else f"pressure: UNKNOWN ({msg})")
        return EXIT_CODES["UNKNOWN"]


if __name__ == "__main__":
    sys.exit(main())
