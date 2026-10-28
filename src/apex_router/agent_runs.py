"""Agent-run and band-alert collector: routing and UX data from this machine, without text.

Two streams, both append-only JSONL under ~/.apex-router/labels/ (idempotent: a row is written once):

1. ``agent_runs.jsonl`` — one row per finished pi sub-agent job (``~/.apex-router/pi-jobs/<id>``):
   agent type, role, model, provider, thinking, final state, turns (assistant ``turn_end`` events,
   one rule for every pi job; xval jobs use codex model calls, marked in ``turns_source``), tool calls, tokens, cost, wall time, cost cap, and a judge verdict
   label when the answer states one: GO / NO-GO / GO-WITH-CHANGES (review gate) or AGREE / PARTIAL /
   DISAGREE (cross-validation). The two schemes are kept apart, never mapped onto each other. xval jobs also get the codex
   token counts from ``xval_runs.jsonl`` (matched by time; no USD price is invented).
2. ``band_alerts.jsonl`` — one row per context alert the datapce band shows (context >= 80 %),
   from the plugin's ``observe/*.jsonl`` measure rows, and whether context dropped below 50 %
   within 30 minutes (a /compact or a fresh session). That is a PROXY for "the user acted".

Privacy: the collector never copies the brief, the answer, file paths or descriptions. The role is
an enum read from the brief's first line ("You are judge J1" -> judge). The verdict is an enum.

Report-time mapping (stored rows are not changed): rows group by a canonical model id
(``canonical_model``: provider prefix stripped, a bare tier name resolved, the version kept). An xval
row gets USD from its codex tokens only at an audited rate (``list:`` or ``negotiated:``); a
``provisional:`` or unknown rate gives ``cost_usd`` None and the cell says "usd unknown".

Usage:  apex-router agent-runs collect [--json] [--refresh-verdicts]
        apex-router agent-runs report  [--json] [--since DAYS]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("APEX_HOME", Path.home() / ".apex-router"))
JOBS = HOME / "pi-jobs"
OBSERVE = HOME / "observe"
XVAL_RUNS = HOME / "xval_runs.jsonl"
OUT_DIR = HOME / "labels"
RUNS_OUT = OUT_DIR / "agent_runs.jsonl"
ALERTS_OUT = OUT_DIR / "band_alerts.jsonl"

SCHEMA = 1
FINAL = {"done", "failed", "killed", "killed(cost)", "timed_out(idle)", "timed_out(wall)"}
ROLE_RE = re.compile(r"\bYou are (?:the )?(judge|engineer|researcher|reviewer|scout|worker)\b", re.I)
VERDICTS = "GO-WITH-CHANGES|NO-GO|GO|AGREE|PARTIAL|DISAGREE"      # longest first: NO-GO before GO
VERDICT_RE = re.compile(rf"\b({VERDICTS})\b")
CTX_ALERT = 80.0
CTX_CLEARED = 50.0
ACT_WINDOW_MS = 30 * 60 * 1000


# Provider prefixes, stripped in any order and repeated: "anthropic/claude-x", "it-entra-claude-x".
_MODEL_PREFIXES = ("anthropic/", "openai/", "it-entra-", "claude-")
# A canonical id is a Claude tier with an optional version ("sonnet", "sonnet-5-5") or a GPT id.
_CANON_RE = re.compile(r"^(?:(haiku|sonnet|opus|fable)(?:-\d[\w.-]*)?|gpt-[\w.-]+)$")
# Rates that are a real price. "provisional:" is a placeholder (gpt-6 carries gpt-5 rates): not USD.
_AUDITED_REGIMES = ("list:", "negotiated:")


def canonical_model(raw, registry: dict | None = None):
    """One id per model version, for grouping only. Strips provider prefixes, keeps family and version:
    'it-entra-claude-sonnet-5-5', 'claude-sonnet-5-5', 'sonnet-5-5' -> 'sonnet-5-5'. A bare tier name
    ('sonnet') resolves through the model registry tier map. Other versions stay apart ('sonnet-4-6').
    A string that is not a known family comes back unchanged."""
    if not isinstance(raw, str) or not raw.strip():
        return raw
    s = raw.strip().lower()
    stripped = True
    while stripped:
        stripped = False
        for p in _MODEL_PREFIXES:
            if s.startswith(p) and len(s) > len(p):
                s, stripped = s[len(p):], True
    m = _CANON_RE.match(s)
    if not m:
        return raw
    if m.group(1) and s == m.group(1):                       # bare tier name: resolve to its version
        from . import model_registry
        mid = model_registry.tier_model(s, registry=registry if registry is not None else model_registry.load())
        if isinstance(mid, str) and mid and mid.lower() != raw.strip().lower():
            resolved = canonical_model(mid, registry)
            if isinstance(resolved, str) and _CANON_RE.match(resolved):
                return resolved
    return s


def xval_usd(model, input_tokens, cached, output) -> tuple[float | None, str]:
    """(USD, pricing regime) for codex tokens on the OpenAI wire. `input_tokens` includes `cached`
    (OpenAI usage), so fresh = input - cached. USD is None unless the rate is audited (list or
    negotiated) and the token counts are numbers: a price is never invented."""
    from .proxy_engine.readout.pricing import rates_for

    def num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0

    if not (num(input_tokens) and num(output) and (cached is None or num(cached))):
        return None, "unknown:no token counts"
    r = rates_for(model, "openai")
    if not r.pricing_regime.startswith(_AUDITED_REGIMES):
        return None, r.pricing_regime
    cached = min(cached or 0, input_tokens)
    usd = ((input_tokens - cached) * r.input + cached * r.cache_read + output * r.output) / 1e6
    return round(usd, 6), r.pricing_regime


def _argv_opt(argv: list[str], *names: str) -> str | None:
    for i, a in enumerate(argv[:-1]):
        if a in names:
            return argv[i + 1]
    return None


def _role(meta: dict) -> str:
    m = ROLE_RE.search(str(meta.get("task", ""))[:2000])
    if m:
        return m.group(1).lower()
    return {"xval": "judge", "reviewer": "reviewer", "scout": "scout"}.get(str(meta.get("agent")), "worker")


def _verdict(text: str) -> str | None:
    """The judge's verdict, enum only. Counts (a) the last line that starts with a VERDICT label, or
    (b) a last (else first) non-empty line that is only the verdict. A verdict word in prose never counts."""
    marked = re.findall(rf"^[\s*#>_-]*VERDICT[\s*_]*[:\-][\s*_]*({VERDICTS})\b", text, re.I | re.M)
    if marked:
        return marked[-1].upper()
    # result.md starts with the harness header ("# job <id> — <state>", "agent <name> · …"): skip it.
    body = [l for l in text.splitlines() if l.strip() and not re.match(r"#+ job \S+ — |agent \S+ · ", l.strip())]
    lines = [l.strip().strip("*_#").strip() for l in body]
    # A line that is only the verdict: the last line, else the first (xval answers lead with it).
    for line in (lines[-1:] + lines[:1]):
        if re.fullmatch(rf"({VERDICTS})\.?", line):
            return line.rstrip(".")
    return None


def _events(path: Path) -> tuple[int, int]:
    """(assistant turns, tool calls) from a pi events.jsonl; streaming, never loads the file."""
    turns = tools = 0
    try:
        with path.open() as f:
            for line in f:
                # cheap pre-filter: most lines are message_update deltas
                if '"turn_end"' in line[:40]:
                    turns += 1
                elif '"tool_execution_start"' in line[:60]:
                    tools += 1
    except OSError:
        pass
    return turns, tools


def _xval_match(meta: dict, xruns: list[dict]) -> dict | None:
    """The xval_runs row inside the job's run window. Two or more candidates: none (ambiguous; parallel
    xval jobs overlap in time and xval_runs carries no job id)."""
    s, e = meta.get("started"), meta.get("ended")
    if not (isinstance(s, (int, float)) and isinstance(e, (int, float))):
        return None
    lo, hi = s / 1000 - 5, e / 1000 + 10
    hits = [r for r in xruns if isinstance(r.get("ts"), (int, float)) and lo <= r["ts"] <= hi]
    return hits[0] if len(hits) == 1 else None


def run_row(job_dir: Path, xruns: list[dict]) -> dict | None:
    try:
        meta = json.loads((job_dir / "meta.json").read_text())
    except (OSError, ValueError):
        return None
    if meta.get("state") not in FINAL:
        return None
    argv = [str(a) for a in meta.get("argv") or []]
    usage = meta.get("usage") or {}
    turns, tools = _events(job_dir / "events.jsonl")
    answer = ""
    try:
        answer = (job_dir / "result.md").read_text(errors="replace")
    except OSError:
        answer = str(meta.get("final_preview") or "")
    started, ended = meta.get("started"), meta.get("ended")
    row = {
        "schema": SCHEMA,
        "id": meta.get("id") or job_dir.name,
        "ts": ended or started or meta.get("created"),
        "agent": meta.get("agent"),
        "kind": meta.get("kind"),
        "role": _role(meta),
        "model": _argv_opt(argv, "--model", "-m"),
        "provider": _argv_opt(argv, "--provider"),
        "thinking": _argv_opt(argv, "--thinking"),
        "state": meta.get("state"),
        # soft cost cap (A1): the job reached 85 % of max_cost and was asked to report; state stays "done"
        "capped": bool(meta.get("capped")) or meta.get("soft_cap_at") is not None,
        "exit_code": meta.get("exit_code"),
        "turns": turns if turns else usage.get("turns"),
        "turns_source": "pi turn_end" if turns else "pi usage",
        "tool_calls": tools,
        "input": usage.get("input"),
        "output": usage.get("output"),
        "cost_usd": round(usage["cost"], 6) if isinstance(usage.get("cost"), (int, float)) and usage.get("cost") else None,
        "max_cost": meta.get("max_cost"),
        "wall_s": round((ended - started) / 1000, 1) if isinstance(started, (int, float)) and isinstance(ended, (int, float)) else None,
        "retries": meta.get("retries"),
        "rate_limited": bool(meta.get("rate_limited_at")),
        "verdict": _verdict(answer) if _role(meta) in ("judge", "reviewer") else None,
    }
    if meta.get("kind") == "xval":
        x = _xval_match(meta, xruns)
        if x is not None:
            row.update({"xval_calls": x.get("calls"), "turns": x.get("calls"), "turns_source": "codex calls", "input": x.get("input"), "cached": x.get("cached"),
                        "output": x.get("output"), "xval_cost_units": x.get("cost"), "model": row["model"] or x.get("model")})
    return row


def _seen(path: Path, key: str) -> set:
    out = set()
    try:
        with path.open() as f:
            for line in f:
                try:
                    out.add(json.loads(line)[key])
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return out


class _locked:
    """Exclusive lock on ``<path>.lock``: an append and a refresh never interleave (a refresh
    rewrites the file from a snapshot; an append inside that window would be lost)."""

    def __init__(self, path: Path) -> None:
        self.lock = path.with_name(path.name + ".lock")

    def __enter__(self):
        import fcntl
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc) -> None:
        os.close(self.fd)                     # closing the fd releases the flock


def _append(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path), path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def collect_runs() -> int:
    xruns = []
    try:
        xruns = [json.loads(l) for l in XVAL_RUNS.read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        pass
    seen = _seen(RUNS_OUT, "id")
    new = []
    if JOBS.is_dir():
        for d in sorted(JOBS.iterdir()):
            if d.is_dir() and d.name not in seen:
                r = run_row(d, xruns)
                if r is not None:
                    new.append(r)
    _append(RUNS_OUT, new)
    return len(new)


def refresh_verdicts() -> int:
    """Re-parse the verdict of every judge/reviewer row from its job's answer (rows from an older
    parser). Rewrites ``agent_runs.jsonl`` atomically; a row whose job dir is gone stays as is.
    Returns the number of rows changed."""
    if not RUNS_OUT.is_file():
        return 0
    with _locked(RUNS_OUT):
        return _refresh_locked()


def _refresh_locked() -> int:
    try:
        lines = RUNS_OUT.read_text().splitlines()
    except OSError:
        return 0
    out, changed = [], 0
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        if isinstance(r, dict) and r.get("role") in ("judge", "reviewer") and isinstance(r.get("id"), str):
            res = JOBS / r["id"] / "result.md"
            if res.is_file():
                v = _verdict(res.read_text(errors="replace"))
                if v != r.get("verdict"):
                    r["verdict"] = v
                    changed += 1
                    line = json.dumps(r, sort_keys=True)
        out.append(line)
    if changed:
        tmp = RUNS_OUT.with_name(RUNS_OUT.name + ".tmp")
        mode = RUNS_OUT.stat().st_mode & 0o777
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # never wider than 0600
        with os.fdopen(fd, "w") as f:
            f.write("".join(x + "\n" for x in out))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, RUNS_OUT)
    return changed


def _measures() -> list[dict]:
    rows = []
    if OBSERVE.is_dir():
        for f in sorted(OBSERVE.glob("*.jsonl")):
            try:
                for line in f.read_text().splitlines():
                    if '"measure"' not in line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("ev") == "measure" and isinstance(r.get("ts"), (int, float)):
                        rows.append(r)
            except OSError:
                continue
    rows.sort(key=lambda r: r["ts"])
    return rows


def alert_rows(measures: list[dict], now_ms: float) -> list[dict]:
    """One row per rising edge of context >= 80 %, per session. `acted` is None until the window has
    passed. Rows without a session hash (`sid`, plugin >= 0.4.2) are skipped: concurrent sessions
    would mix and fake an "acted"."""
    by_sid: dict[str, list[dict]] = {}
    for r in measures:
        if isinstance(r.get("sid"), str):
            by_sid.setdefault(r["sid"], []).append(r)
    out = []
    for sid, rows in by_sid.items():
        out.extend(_alerts_one(sid, rows, now_ms))
    out.sort(key=lambda a: a["ts"])
    return out


def _alerts_one(sid: str, measures: list[dict], now_ms: float) -> list[dict]:
    out = []
    prev = None
    for i, r in enumerate(measures):
        ctx = r.get("ctx_pct")
        if not isinstance(ctx, (int, float)):
            continue
        rising = ctx >= CTX_ALERT and (prev is None or prev < CTX_ALERT)
        prev = ctx
        if not rising:
            continue
        t0 = r["ts"]
        acted = None
        lag = None
        for later in measures[i + 1:]:
            if later["ts"] - t0 > ACT_WINDOW_MS:
                break
            c = later.get("ctx_pct")
            if isinstance(c, (int, float)) and c < CTX_CLEARED:
                acted, lag = True, round((later["ts"] - t0) / 1000)
                break
        if acted is None and now_ms - t0 > ACT_WINDOW_MS:
            acted = False
        out.append({"schema": SCHEMA, "key": f"ctx-{sid}-{int(t0)}", "sid": sid, "ts": t0, "kind": "context", "ctx_pct": ctx,
                    "limit_pct": r.get("limit_pct"), "acted": acted, "lag_s": lag, "proxy": "ctx<50% within 30 min"})
    return out


def collect_alerts(now_ms: float | None = None) -> int:
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    seen = _seen(ALERTS_OUT, "key")
    # an alert is written only once its outcome is known (acted True/False), so rows never change
    new = [a for a in alert_rows(_measures(), now_ms) if a["key"] not in seen and a["acted"] is not None]
    _append(ALERTS_OUT, new)
    return len(new)


def _read(path: Path) -> list[dict]:
    try:
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


def report(since_days: float | None = None) -> dict:
    runs = _read(RUNS_OUT)
    alerts = _read(ALERTS_OUT)
    if since_days is not None:
        cut = (time.time() - since_days * 86400) * 1000
        runs = [r for r in runs if isinstance(r.get("ts"), (int, float)) and r["ts"] >= cut]
        alerts = [a for a in alerts if a["ts"] >= cut]
    from . import model_registry
    reg = model_registry.load()
    cells: dict[tuple, dict] = {}
    for r in runs:
        raw = r.get("model") or "?"
        k = (r.get("role") or "?", canonical_model(raw, reg) or "?")
        c = cells.setdefault(k, {"n": 0, "done": 0, "capped": 0, "soft_capped": 0, "turns": [], "cost": [], "wall": [], "verdicts": {},
                                 "raw": {}, "cost_unknown": 0, "regimes": set()})
        c["n"] += 1
        c["raw"][raw] = c["raw"].get(raw, 0) + 1
        if r.get("kind") == "xval" and not isinstance(r.get("cost_usd"), (int, float)):
            usd, regime = xval_usd(raw, r.get("input"), r.get("cached"), r.get("output"))
            r = {**r, "cost_usd": usd}                         # report-time only; the stored row is not changed
            c["regimes"].add(regime)
            c["cost_unknown"] += usd is None
        c["done"] += r.get("state") == "done"
        c["capped"] += r.get("state") == "killed(cost)"
        c["soft_capped"] += r.get("state") == "done" and r.get("capped") is True
        for f, src in (("turns", "turns"), ("cost", "cost_usd"), ("wall", "wall_s")):
            if isinstance(r.get(src), (int, float)):
                c[f].append(r[src])
        if r.get("verdict"):
            c["verdicts"][r["verdict"]] = c["verdicts"].get(r["verdict"], 0) + 1

    def med(xs):
        xs = sorted(xs)
        return None if not xs else xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2

    table = [{"role": k[0], "model": k[1], "n": c["n"], "done_rate": round(c["done"] / c["n"], 3),
              "cost_capped": c["capped"], "soft_capped": c["soft_capped"], "turns_med": med(c["turns"]), "cost_med": med(c["cost"]),
              "cost_sum": None if c["cost_unknown"] and not c["cost"] else round(sum(c["cost"]), 2),
              "cost_unknown": c["cost_unknown"], "cost_note": "usd unknown" if c["cost_unknown"] else None,
              "pricing": sorted(c["regimes"]), "wall_med": med(c["wall"]), "verdicts": c["verdicts"], "models_raw": c["raw"]}
             for k, c in sorted(cells.items())]
    acted = [a for a in alerts if a.get("acted") is not None]
    return {
        "runs": len(runs),
        "by_role_model": table,
        "alerts": len(alerts),
        "context_alerts_acted": f"{sum(1 for a in acted if a['acted'])}/{len(acted)}",
        "note": "acted = context fell below 50% within 30 min (proxy). Costs: pi USD; xval USD from codex tokens only at a list or "
                "negotiated rate, else 'usd unknown' (no price is invented). model = canonical id; models_raw has the raw strings.",
    }


def _print_report(rep: dict) -> None:
    print(f"agent runs: {rep['runs']}   band context alerts: {rep['alerts']} (acted {rep['context_alerts_acted']})")
    print(f"{'role':<10} {'model':<28} {'n':>4} {'done':>5} {'cap':>4} {'turns':>6} {'$ med':>7} {'$ sum':>11} {'wall s':>7}  verdicts")
    for r in rep["by_role_model"]:
        f = lambda x, d=1: "-" if x is None else f"{x:.{d}f}"  # noqa: E731
        total = "usd unknown" if r["cost_sum"] is None else f"{r['cost_sum']:.2f}"
        if r["cost_sum"] is not None and r["cost_unknown"]:
            total += f" +{r['cost_unknown']}?"
        print(f"{r['role']:<10} {r['model'][:28]:<28} {r['n']:>4} {r['done_rate']:>5.2f} {r['cost_capped']:>4} "
              f"{f(r['turns_med'], 0):>6} {f(r['cost_med'], 2):>7} {total:>11} {f(r['wall_med'], 0):>7}  {r['verdicts'] or ''}")
    print(rep["note"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="apex-router agent-runs", description="routing and band-alert data from this machine (no text)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--json", action="store_true")
    c.add_argument("--refresh-verdicts", action="store_true",
                   help="also re-parse the verdict of rows already collected (after a parser change)")
    r = sub.add_parser("report")
    r.add_argument("--json", action="store_true")
    r.add_argument("--since", type=float, default=None, help="only the last N days")
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        res = {"runs_new": collect_runs(), "alerts_new": collect_alerts(), "runs_file": str(RUNS_OUT), "alerts_file": str(ALERTS_OUT)}
        if a.refresh_verdicts:
            res["verdicts_refreshed"] = refresh_verdicts()
        tail = f", verdicts refreshed {res['verdicts_refreshed']}" if a.refresh_verdicts else ""
        print(json.dumps(res) if a.json else f"agent runs +{res['runs_new']}, band alerts +{res['alerts_new']}{tail}")
        return 0
    rep = report(a.since)
    if a.json:
        print(json.dumps(rep))
    else:
        _print_report(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---- live jobs (menu bar, studio) -----------------------------------------------------------

LIVE_STATES = ("running", "queued")
JOBS_SCAN_MAX = 60          # newest job dirs looked at (ids sort by start time)
JOBS_LIST_MAX = 5000        # names listed at most (no stat per entry)
META_MAX = 256 * 1024       # bytes read per meta.json


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def live_jobs(jobs_dir: Path | None = None, now: float | None = None) -> dict:
    """Sub-agent jobs that run or wait now: ``{"running": N, "queued": N, "items": [...]}``, newest
    first. A "running" job whose pid is gone counts as ended (a crash leaves the state behind).
    Reads only the newest ``JOBS_SCAN_MAX`` meta.json files; never raises (fails open: empty)."""
    now = time.time() if now is None else now
    d = Path(jobs_dir) if jobs_dir else Path(os.environ.get("APEX_HOME", Path.home() / ".apex-router")) / "pi-jobs"
    out = {"running": 0, "queued": 0, "items": []}
    names = []
    try:
        with os.scandir(d) as it:                     # names only: no stat, no is_dir per entry
            for i, e in enumerate(it):
                if i >= JOBS_LIST_MAX:
                    break
                names.append(e.name)
    except OSError:
        return out
    for name in sorted(names, reverse=True)[:JOBS_SCAN_MAX]:
        jd = d / name
        try:
            with open(jd / "meta.json", "rb") as f:
                raw = f.read(META_MAX + 1)
            if len(raw) > META_MAX:
                continue                              # oversized: skip, never parse
            m = json.loads(raw)
        except (OSError, ValueError):
            continue
        if not isinstance(m, dict):
            continue
        st = m.get("state")
        if st not in LIVE_STATES or (st == "running" and not _pid_alive(m.get("pid"))):
            continue
        out[st] += 1
        started = m.get("started") or m.get("created")
        started = started / 1000.0 if isinstance(started, (int, float)) and started > 1e11 else started
        last = m.get("last_event")
        last = last / 1000.0 if isinstance(last, (int, float)) and last > 1e11 else last
        out["items"].append({
            "id": str(m.get("id") or jd.name), "agent": str(m.get("agent") or "?"), "role": _role(m),
            "state": st, "session_id": m.get("session_id"),
            "age_s": round(now - started) if isinstance(started, (int, float)) else None,
            "quiet_s": round(now - last) if isinstance(last, (int, float)) else None,
        })
    return out


ENDED_STATES = ("failed", "timed_out(idle)", "timed_out(wall)", "killed(cost)", "done")


def ended_jobs(jobs_dir: Path | None = None, now: float | None = None, remaining=None) -> dict:
    """Jobs in the newest ``JOBS_SCAN_MAX`` dirs that ended failed, timed out or capped (a hard
    ``killed(cost)`` or a soft cap on a ``done`` job): ``{"items": [{id, role, state, capped,
    ended_ago_s}]}``. Only fixed fields; never the task text. A missing dir is no jobs; any other
    read error of the dir or of a meta.json raises (the caller shows "not checked"): a job that cannot
    be read must not count as "no problem". A job folder without meta.json (just made), an oversized
    meta.json or one that is not a JSON object is skipped. ``remaining`` (a callable, seconds left) is checked before
    each read; at 0 the scan stops and returns ``{"skipped": True}`` (never a partial list as a full one)."""
    now = time.time() if now is None else now
    d = Path(jobs_dir) if jobs_dir else Path(os.environ.get("APEX_HOME", Path.home() / ".apex-router")) / "pi-jobs"
    names = []
    try:
        with os.scandir(d) as it:
            for i, e in enumerate(it):
                if i >= JOBS_LIST_MAX:
                    break
                names.append(e.name)
    except FileNotFoundError:
        return {"items": []}
    out = []
    for name in sorted(names, reverse=True)[:JOBS_SCAN_MAX]:
        if remaining is not None and remaining() <= 0:
            return {"skipped": True, "error": "skipped: deadline"}
        try:
            with open(d / name / "meta.json", "rb") as f:
                raw = f.read(META_MAX + 1)
        except (FileNotFoundError, NotADirectoryError):
            continue                                  # a folder being made, or a stray file
        if len(raw) > META_MAX:
            continue
        m = json.loads(raw)       # meta.json is written by rename (atomic): a parse error is real damage
                                  # and raises, so the menu says "not checked", not a wrong 0
        if not isinstance(m, dict) or m.get("state") not in ENDED_STATES:
            continue
        capped = bool(m.get("capped")) or m.get("soft_cap_at") is not None
        if m["state"] == "done" and not capped:
            continue
        ended = m.get("ended")
        ended = ended / 1000.0 if isinstance(ended, (int, float)) and ended > 1e11 else ended
        if not isinstance(ended, (int, float)) or isinstance(ended, bool):
            continue
        out.append({"id": str(m.get("id") or name), "role": _role(m), "state": m["state"],
                    "capped": capped, "ended_ago_s": round(max(0.0, now - ended))})
    return {"items": out}
