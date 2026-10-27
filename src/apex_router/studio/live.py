"""Live numbers for apex flow studio, aggregated by window (15m / 1h / 24h). Read-only.

Sources (every one optional; a missing or bad file gives a muted "no data" state, never an error):
  proxy telemetry  <proxy home>/telemetry.jsonl (+ the newest rotated generation when the window
                   reaches past the live file); request rows and {"ev":"hb"} heartbeat rows
  local telemetry  the local proxy (:8789) home, for the ollama node
  route log        <router home>/route_log.jsonl          (pi turns, ok / escalated)
  conformance      <router home>/conformance.jsonl        (resolve calls)
  pi-guard/, pi-compact/  per-day jsonl files (ts in ms)
  pressure.json, widget/history.jsonl, xval_bandit.json

Reads are bounded: at most TAIL_BYTES from the end of each file, and a line is parsed only when
its ts is inside the window. No network call (the proxy state comes from heartbeat age).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from .flow import EDGES, NODES, Paths, resolve_paths, tilde

WINDOWS = {"15m": 15 * 60, "1h": 3600, "24h": 24 * 3600}
BUCKETS = 24
TAIL_BYTES = 64 * 1024 * 1024
SMALL_TAIL_BYTES = 8 * 1024 * 1024
HB_ALIVE_S = 90
_TS_RE = re.compile(rb'"ts"\s*:\s*(-?[0-9][0-9.eE+-]*)')
_UNREACHABLE = {"ConnectError", "ConnectTimeout", "PoolTimeout", "ReadTimeout", "RemoteProtocolError"}

_cache: dict = {}
_lock = threading.Lock()


def _norm_ts(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v / 1000.0 if v > 1e11 else float(v)


def _lines(path: Path, cap: int):
    """Complete lines from the last ``cap`` bytes of a file (the first, partial line dropped)."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            start = max(0, size - cap)
            f.seek(start)
            data = f.read(cap)
    except OSError:
        return []
    lines = data.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]
    return [ln for ln in lines if ln.strip()]


def _parse_lines(lines: list, since: float) -> tuple[list, float | None]:
    out, first = [], None
    for ln in lines:
        m = _TS_RE.search(ln[:400]) or _TS_RE.search(ln)
        if not m:
            continue
        try:
            ts = _norm_ts(float(m.group(1)))
        except ValueError:
            continue
        if ts is None:
            continue
        first = ts if first is None else min(first, ts)
        if ts < since:
            continue
        try:
            row = json.loads(ln)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(row, dict):
            row["_ts"] = ts
            out.append(row)
    return out, first


def rows_in_window(path: Path, since: float, cap: int = TAIL_BYTES, rotated: bool = False) -> tuple[list, bool]:
    """(rows with ts >= since, oldest first; any file present). With ``rotated`` the newest
    ``<name>.<N>`` file is read too, but only when the live file starts inside the window."""
    path = Path(path)
    present = path.is_file()
    out, first = _parse_lines(_lines(path, cap), since) if present else ([], None)
    if rotated and (first is None or first > since) and path.parent.is_dir():
        pat = re.compile(re.escape(path.name) + r"\.(\d+)$")
        try:
            gens = sorted((int(m.group(1)), q) for q in path.parent.iterdir()
                          if (m := pat.fullmatch(q.name)) and q.is_file())
        except OSError:
            gens = []
        if gens:
            older, _ = _parse_lines(_lines(gens[-1][1], cap), since)
            out = older + out
            present = True
    out.sort(key=lambda r: r["_ts"])
    return out, present


def _bucket(ts: float, now: float, span: float) -> int:
    i = int((ts - (now - span)) / span * BUCKETS)
    return min(BUCKETS - 1, max(0, i))


def _pct(vals: list, q: float):
    if not vals:
        return None
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[k]


def telemetry_stats(path: Path, now: float, span: float) -> dict:
    rows, present = rows_in_window(path, now - span, rotated=True)
    reqs = [r for r in rows if r.get("ev") != "hb" and "client" in r]
    hbs = [r for r in rows if r.get("ev") == "hb"]
    series = [0] * BUCKETS
    err_series = [0] * BUCKETS
    ttft_b: list = [[] for _ in range(BUCKETS)]
    clients: dict = {}
    endpoints: dict = {}
    causes: dict = {}
    ttft = []
    n_err = n_retry = n_unreach = n_shadow = n_ck = 0
    agents = set()
    n_agent_req = 0
    tok_in = tok_out = cache_read = 0
    for r in reqs:
        b = _bucket(r["_ts"], now, span)
        series[b] += 1
        c = str(r.get("client") or "?")
        clients[c] = clients.get(c, 0) + 1
        ep = str(r.get("endpoint_id") or "anthropic")
        endpoints[ep] = endpoints.get(ep, 0) + 1
        if r.get("is_error"):
            n_err += 1
            err_series[b] += 1
            cause = str(r.get("error_cause") or "unlabeled")
            causes[cause] = causes.get(cause, 0) + 1
            if cause in _UNREACHABLE:
                n_unreach += 1
        try:
            n_retry += int(r.get("connect_retries") or 0)
        except (TypeError, ValueError):
            pass
        if isinstance(r.get("shadow"), dict):
            n_shadow += 1
        if r.get("cache_key_rewrite"):
            n_ck += 1
        if r.get("agent_id"):
            n_agent_req += 1
            agents.add(str(r["agent_id"]))
        t = r.get("ttft_ms")
        if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 and not r.get("is_error"):
            ttft.append(float(t))
            ttft_b[b].append(float(t))
        for k, acc in (("tokens_in", "i"), ("tokens_out", "o"), ("cache_read_tokens", "c")):
            v = r.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                if acc == "i":
                    tok_in += v
                elif acc == "o":
                    tok_out += v
                else:
                    cache_read += v
    last_hb = max((r["_ts"] for r in hbs), default=None)
    n = len(reqs)
    return {
        "file": tilde(path), "present": present, "requests": n, "errors": n_err,
        "error_pct": round(100.0 * n_err / n, 1) if n else None,
        "retries": n_retry, "unreachable": n_unreach, "shadow": n_shadow, "cache_key": n_ck,
        "agent_requests": n_agent_req, "agents": len(agents),
        "ttft_p50": round(_pct(ttft, 0.5)) if ttft else None,
        "ttft_p90": round(_pct(ttft, 0.9)) if ttft else None,
        "tokens_in": tok_in, "tokens_out": tok_out, "cache_read": cache_read,
        "clients": clients, "endpoints": endpoints,
        "causes": dict(sorted(causes.items(), key=lambda kv: -kv[1])[:6]),
        "series": series, "err_series": err_series,
        "ttft_series": [round(_pct(b, 0.5)) if b else 0 for b in ttft_b],
        "rows": len(rows), "heartbeats": len(hbs),
        "last_hb_age_s": round(now - last_hb, 1) if last_hb else None,
        "alive": bool(last_hb and now - last_hb <= HB_ALIVE_S),
    }


def _jsonl_stats(path: Path, now: float, span: float) -> tuple[list, bool]:
    return rows_in_window(path, now - span, cap=SMALL_TAIL_BYTES)


def _dir_rows(d: Path, now: float, span: float, max_files: int = 8) -> tuple[list, bool]:
    if not d.is_dir():
        return [], False
    try:
        files = sorted((p for p in d.iterdir() if p.suffix == ".jsonl" and p.is_file()),
                       key=lambda p: p.stat().st_mtime)[-max_files:]
    except OSError:
        return [], True
    out = []
    for f in files:
        if f.stat().st_mtime < now - span:
            continue
        rows, _ = rows_in_window(f, now - span, cap=SMALL_TAIL_BYTES)
        out += rows
    return out, True


def _series(rows: list, now: float, span: float) -> list:
    s = [0] * BUCKETS
    for r in rows:
        s[_bucket(r["_ts"], now, span)] += 1
    return s


def _fmt(v, unit=""):
    if v is None:
        return "—"
    if isinstance(v, float) and not v.is_integer():
        return f"{v:.1f}"
    v = int(v)
    if v >= 1_000_000:
        return f"{v / 1e6:.1f}M"
    if v >= 10_000:
        return f"{v / 1e3:.0f}k"
    if v >= 1000:
        return f"{v / 1e3:.1f}k"
    return str(v)


def collect(window: str = "15m", proxy: str = "main", paths: Paths | None = None,
            now: float | None = None) -> dict:
    paths = paths or resolve_paths()
    if window not in WINDOWS:
        window = "15m"
    proxy = proxy if proxy in ("main", "local") else "main"
    now = time.time() if now is None else now
    span = WINDOWS[window]
    rh = paths.router_home
    tel = telemetry_stats(paths.telemetry(proxy), now, span)
    loc = tel if proxy == "local" else telemetry_stats(paths.telemetry("local"), now, span)
    rl, rl_present = _jsonl_stats(rh / "route_log.jsonl", now, span)
    pi_rows = [r for r in rl if r.get("family") or r.get("surface") == "pi"]
    esc = sum(1 for r in pi_rows if r.get("escalated"))
    conf, conf_present = _jsonl_stats(rh / "conformance.jsonl", now, span)
    res_rows = [r for r in conf if r.get("surface") == "resolve"]
    guard, guard_present = _dir_rows(rh / "pi-guard", now, span)
    hang, hang_present = _dir_rows(rh / "pi-hangguard", now, span)
    comp, comp_present = _dir_rows(rh / "pi-compact", now, span)
    hist, hist_present = _jsonl_stats(rh / "widget" / "history.jsonl", now, span)
    pressure = None
    pj = rh / "pressure.json"
    try:
        p = json.loads(pj.read_text())
        if isinstance(p, dict):
            gen = _norm_ts(p.get("generated_at"))
            pressure = {"level": str(p.get("level") or "UNKNOWN"),
                        "age_s": round(now - gen) if gen else None,
                        "insufficient": bool(p.get("insufficient_sample"))}
    except (OSError, ValueError):
        pressure = None
    xval = None
    try:
        xb = json.loads((rh / "xval_bandit.json").read_text())
        cats = xb.get("categories") if isinstance(xb, dict) else None
        if isinstance(cats, dict):
            pulls = 0
            for c in cats.values():
                for a in ((c or {}).get("arms") or {}).values():
                    if isinstance(a, dict) and isinstance(a.get("n"), (int, float)):
                        pulls += int(a["n"])
            xval = {"categories": len(cats), "pulls": pulls,
                    "age_s": round(now - (rh / "xval_bandit.json").stat().st_mtime)}
    except (OSError, ValueError, AttributeError):
        xval = None

    metrics = {
        "pi.turns": len(pi_rows) if rl_present else None,
        "pi.escalated": esc if rl_present else None,
        "conf.resolve": len(res_rows) if conf_present else None,
        "conf.fallback": None,   # resolve does not log its source field; shown as "not logged"
        "pi.guard": (len(guard) + len(hang)) if (guard_present or hang_present) else None,
        "pi.compact": len(comp) if comp_present else None,
        "tel.requests": tel["requests"] if tel["present"] else None,
        "tel.rows": tel["rows"] if tel["present"] else None,
        "tel.error_pct": tel["error_pct"],
        "tel.retries": tel["retries"] if tel["present"] else None,
        "tel.unreachable": tel["unreachable"] if tel["present"] else None,
        "tel.shadow": tel["shadow"] if tel["present"] else None,
        "tel.cachekey": tel["cache_key"] if tel["present"] else None,
        "tel.agents": tel["agent_requests"] if tel["present"] else None,
        "tel.ttft_p50": tel["ttft_p50"],
        "tel.client.claude-code": tel["clients"].get("claude-code", 0) if tel["present"] else None,
        "tel.client.codex": tel["clients"].get("codex", 0) if tel["present"] else None,
        "tel.endpoint.anthropic": tel["endpoints"].get("anthropic", 0) if tel["present"] else None,
        "tel.endpoint.openai": tel["endpoints"].get("openai", 0) if tel["present"] else None,
        "local.requests": loc["requests"] if loc["present"] else None,
        "routelog.rows": len(rl) if rl_present else None,
        "history.rows": len(hist) if hist_present else None,
        "xval.pulls": xval["pulls"] if xval else None,
        "pressure.level": pressure["level"] if pressure else None,
    }
    tel_series = tel["series"]
    series_of = {
        "pi.turns": _series(pi_rows, now, span), "conf.resolve": _series(res_rows, now, span),
        "pi.guard": _series(guard + hang, now, span), "pi.compact": _series(comp, now, span),
        "tel.requests": tel_series, "tel.rows": tel_series, "tel.shadow": tel_series,
        "tel.cachekey": tel_series, "tel.agents": tel_series, "tel.error_pct": tel["err_series"],
        "tel.ttft_p50": tel["ttft_series"], "tel.client.claude-code": tel_series, "tel.client.codex": tel_series,
        "tel.endpoint.anthropic": tel_series, "tel.endpoint.openai": tel_series,
        "local.requests": loc["series"], "routelog.rows": _series(rl, now, span),
        "history.rows": _series(hist, now, span),
    }
    nodes = {}
    for n in NODES:
        key = n["metric"]
        v = metrics.get(key) if key else None
        if n["locked"]:
            status = "locked"
        elif key is None:
            status = "nodata"
        elif v is None:
            status = "nodata"
        elif key == "pressure.level":
            status = {"GREEN": "ok", "AMBER": "warn", "RED": "error"}.get(v, "nodata")
            if pressure and pressure.get("age_s") and pressure["age_s"] > 3600:
                status = "warn"
        elif key == "tel.error_pct":
            status = "ok" if v < 2 else "warn" if v < 10 else "error"
        elif v == 0:
            status = "idle"
        else:
            status = "ok"
        if key and key.startswith("tel.") and tel["present"] and tel["error_pct"] and status == "ok" \
                and key in ("tel.requests", "tel.ttft_p50"):
            status = "warn" if tel["error_pct"] >= 5 else "ok"
        text = v if isinstance(v, str) else _fmt(v)
        nodes[n["id"]] = {"value": v, "text": text, "unit": n["unit"], "status": status,
                          "series": series_of.get(key) if key else None}
    if pressure and pressure.get("age_s") is not None:
        nodes["sink.pressure"]["unit"] = f"{_age(pressure['age_s'])} old"
    if xval:
        nodes["sink.xval"]["unit"] = "pulls total"
    edges = {}
    for e in EDGES:
        v = metrics.get(e["vol"]) if e["vol"] else None
        edges[e["id"]] = {"vol": v if isinstance(v, (int, float)) else None,
                          "active": isinstance(v, (int, float)) and v > 0}
    return {
        "schema": 1, "window": window, "window_s": span, "proxy": proxy, "generated_at": now,
        "buckets": BUCKETS, "metrics": metrics, "nodes": nodes, "edges": edges,
        "telemetry": {k: v for k, v in tel.items() if k not in ("series", "err_series", "ttft_series")},
        "local": {"file": loc["file"], "present": loc["present"], "requests": loc["requests"],
                  "alive": loc["alive"]},
        "pi": {"turns": metrics["pi.turns"], "escalated": metrics["pi.escalated"],
               "route_log_present": rl_present},
        "pressure": pressure, "xval": xval,
        "sources": {
            "telemetry": tel["present"], "local_telemetry": loc["present"], "route_log": rl_present,
            "conformance": conf_present, "pi_guard": guard_present or hang_present,
            "pi_compact": comp_present, "pressure": pressure is not None,
            "history": hist_present, "xval": xval is not None,
        },
        "empty": not any([tel["present"], rl_present, conf_present, pressure, hist_present, xval]),
    }


def _age(s) -> str:
    s = int(s)
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def collect_cached(window: str, proxy: str, paths: Paths, ttl: float = 8.0) -> dict:
    key = (window, proxy, str(paths.router_home), str(paths.telemetry(proxy)))
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    out = collect(window, proxy, paths, now)
    with _lock:
        _cache[key] = (now, out)
        if len(_cache) > 16:
            _cache.pop(next(iter(_cache)))
    return out
