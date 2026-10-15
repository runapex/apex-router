"""Collect the numbers for the apex-router + skills presentation page (scripts/presentation_page.py).

Every number comes from a local file. Nothing is estimated. No dollar figures (tokens and requests only).
Windows: "cur" = last 7 days, "prev" = the 7 days before. The proxy telemetry starts 2026-09-30.

    python3 scripts/presentation_data.py [--out FILE]      # prints JSON; also writes the snapshot

The snapshot (~/.apex-router/presentation/snapshot-<date>.json) lets the next run show a real delta.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

H = Path.home()
AR = H / ".apex-router"
APEX = H / ".apex"
REPO = Path(__file__).resolve().parents[1]
NOW = time.time()
DAY = 86400.0
CUR = (NOW - 7 * DAY, NOW)
PREV = (NOW - 14 * DAY, NOW - 7 * DAY)


def jl(path):
    """Yield JSON rows of a .jsonl file; skip bad lines."""
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def win(ts):
    if ts is None:
        return None
    if CUR[0] <= ts <= CUR[1]:
        return "cur"
    if PREV[0] <= ts < PREV[1]:
        return "prev"
    return None


def du(path) -> tuple[int, int]:
    """(files, bytes) under path, following no symlinks."""
    n = b = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                b += os.path.getsize(os.path.join(root, f))
                n += 1
            except OSError:
                pass
    return n, b


# ---------------------------------------------------------------------------------------------- telemetry

def pi_session_ids() -> set[str]:
    ids = set()
    for p in list(glob.glob(str(H / ".pi/agent/sessions/*/*.jsonl"))) + list(glob.glob(str(AR / "transcripts/pi/**/*.jsonl"), recursive=True)):
        m = re.search(r"_([0-9a-f]{8}-[0-9a-f-]{27})\.jsonl$", p) or re.search(r"_(apexjob-[\w-]+)\.jsonl$", p)
        if m:
            ids.add(m.group(1))
    return ids


def telemetry():
    pis = pi_session_ids()
    files = [APEX / "telemetry.jsonl.1", APEX / "telemetry.jsonl"]
    rows = 0
    first = last = None
    agg = {w: collections.defaultdict(lambda: collections.Counter()) for w in ("cur", "prev", "all")}
    ttft = collections.defaultdict(list)
    daily = collections.defaultdict(collections.Counter)
    for f in files:
        for r in jl(f):
            if r.get("ev") == "hb":
                continue
            ts = r.get("ts")
            rows += 1
            first = ts if first is None else min(first, ts)
            last = ts if last is None else max(last, ts)
            client = r.get("client") or "?"
            if client == "claude-code" and r.get("session_id") in pis:
                client = "pi"
            if client == "codex" and not r.get("session_id"):
                client = "codex batch (no session)"  # pipelines such as vuln_triage: codex exec without a session header
            model = r.get("model_resolved") or r.get("model_requested") or "?"
            u = r.get("usage") or {}
            tin = u.get("input_tokens", r.get("tokens_in")) or 0
            tout = u.get("output_tokens", r.get("tokens_out")) or 0
            cr = u.get("cache_read_tokens", r.get("cache_read_tokens")) or 0
            cw = u.get("cache_creation_tokens", r.get("cache_write_tokens")) or 0
            cause = r.get("error_cause") or ""
            is429 = "429" in str(cause) or (r.get("upstream_rejected") and "rate" in json.dumps(r.get("error_detail") or "").lower())
            for w in ("all", win(ts)):
                if not w:
                    continue
                for key in (("client", client), ("model", model), ("total", "all")):
                    c = agg[w][key]
                    c["req"] += 1
                    c["in"] += tin
                    c["out"] += tout
                    c["cache_read"] += cr
                    c["cache_write"] += cw
                    c["err"] += bool(r.get("is_error"))
                    c["rejected"] += bool(r.get("upstream_rejected"))
                    c["r429"] += bool(is429)
                    c["refusal"] += bool(r.get("refusal"))
            if win(ts) == "cur" and r.get("ttft_ms") and not r.get("is_error"):
                ttft[model].append(r["ttft_ms"])
            day = time.strftime("%m-%d", time.localtime(ts))
            daily[day]["req"] += 1
            daily[day]["out"] += tout
            daily[day]["r429"] += bool(is429)

    def pack(w, kind):
        out = []
        for (k, name), c in agg[w].items():
            if k != kind:
                continue
            d = dict(c)
            d["name"] = name
            if kind == "model":
                v = ttft.get(name) or []
                d["ttft_p50_ms"] = round(statistics.median(v)) if v else None
            out.append(d)
        return sorted(out, key=lambda d: -d["req"])

    return {
        "rows": rows,
        "first": time.strftime("%Y-%m-%d", time.localtime(first)) if first else None,
        "last": time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else None,
        "total": {w: dict(agg[w][("total", "all")]) for w in ("cur", "prev", "all")},
        "by_client": {w: pack(w, "client") for w in ("cur", "prev", "all")},
        "by_model": {w: pack(w, "model") for w in ("cur", "prev")},
        "daily": [{"day": d, **c} for d, c in sorted(daily.items())][-15:],
        "pi_sessions_known": len(pis),
    }


# ---------------------------------------------------------------------------------------------- local models

def local():
    off = {w: collections.Counter() for w in ("cur", "prev", "all")}
    off_models = collections.Counter()
    for r in jl(APEX / "offload_telemetry.jsonl"):
        for w in ("all", win(r.get("ts"))):
            if not w:
                continue
            c = off[w]
            c["calls"] += 1
            c["ok"] += bool(r.get("ok"))
            c["escalated"] += bool(r.get("escalated"))
            c["tokens"] += (r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0)
        off_models[r.get("model") or "?"] += 1
    cq = {w: collections.Counter() for w in ("cur", "prev", "all")}
    for r in jl(APEX / "codeqa_impact.jsonl"):
        g = r.get("grounding") or {}
        for w in ("all", win(r.get("ts"))):
            if not w:
                continue
            c = cq[w]
            c["asks"] += 1
            c["prompt_tokens"] += r.get("prompt_tokens") or 0
            c["grounded"] += g.get("grounded", 0)
            c["stale"] += g.get("stale", 0)
            c["hallucinated"] += g.get("hallucinated", 0)
    # codeqa validate split (local vs frontier verifier calls) is only in the daily digest: read its last block
    digest = (AR / "offload_daily.md").read_text(errors="replace") if (AR / "offload_daily.md").exists() else ""
    m = list(re.finditer(r"validate:\s+(\d+) local \+ (\d+) frontier verifier calls\s+\(local_share=(\d+)%\)\s+struck=(\d+)", digest))
    validate = dict(zip(("local", "frontier", "local_share_pct", "struck"), map(int, m[-1].groups()))) if m else None
    judge = None
    jlog = AR / "logs/labels-judge.log"
    if jlog.exists():
        mm = re.findall(r"^\s+judge\s+(\d+)\s+([\d.]+)%", jlog.read_text(errors="replace"), re.M)
        if mm:
            judge = {"judged": int(mm[-1][0]), "share_pct": float(mm[-1][1])}
    try:
        ol = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL).stdout
        installed = [ln.split()[0] + " (" + " ".join(ln.split()[2:4]) + ")" for ln in ol.splitlines()[1:] if ln.strip()]
    except Exception:
        installed = []
    env = {}
    for ln in (AR / "ornith.env").read_text().splitlines() if (AR / "ornith.env").exists() else []:
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            env[k.strip()] = v.strip()
    return {
        "offload": {w: dict(off[w]) for w in off},
        "offload_models": dict(off_models),
        "codeqa": {w: dict(cq[w]) for w in cq},
        "codeqa_validate": validate,
        "label_judge": judge,
        "installed": installed,
        "active": f"{env.get('LOCAL_FAMILY', '?')} {env.get('ORNITH_TIER', '')}".strip(),
    }


# ---------------------------------------------------------------------------------------------- data volume + classification

def steps_dataset():
    """Rebuild the P6 step dataset into a temp home (read-only on the real data) and return its manifest."""
    with tempfile.TemporaryDirectory() as t:
        for name in ("transcripts", "labels"):
            if (AR / name).exists():
                os.symlink(AR / name, Path(t) / name)
        try:
            out = subprocess.run([sys.executable, "-m", "apex_router.cli", "worldmodel", "build", "--home", t, "--no-embed", "--json"],
                                 capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL, cwd=REPO,
                                 env={**os.environ, "PYTHONPATH": str(REPO / "src")})
            return json.loads(out.stdout)
        except Exception as e:  # fail open: the page shows "not available"
            return {"error": str(e)[:200]}


def data_volume(tel):
    src = {}
    for s in ("pi", "claude", "codex"):
        n, b = du(AR / "transcripts" / s)
        src[s] = {"files": n, "bytes": b}
    live = {"pi": du(H / ".pi/agent/sessions"), "claude": du(H / ".claude/projects")}
    labels = collections.Counter(r.get("from") for r in jl(AR / "labels/labels.jsonl"))
    lab_out = collections.Counter(r.get("label") for r in jl(AR / "labels/labels.jsonl"))
    route = collections.Counter(r.get("task_type") or r.get("type") or "?" for r in jl(AR / "route_log.jsonl"))
    strata = collections.Counter()
    for f in (APEX / "telemetry.jsonl",):
        for r in jl(f):
            if r.get("ev") != "hb":
                strata[r.get("stratum") or "unknown"] += 1
    hg = collections.Counter()
    for r in jl(AR / "pi-hangguard.jsonl"):
        for c in r.get("changes", []):
            hg[c] += 1
        hg["blocked"] += bool(r.get("blocked"))
        hg["rows"] += 1
    mem = sum(1 for _ in glob.glob(str(H / ".claude/projects/*/memory/*.md")))
    return {
        "transcripts_mirror": src,
        "live_sessions": {k: {"files": v[0], "bytes": v[1]} for k, v in live.items()},
        "telemetry_rows": tel["rows"],
        "labels": {"rows": sum(labels.values()), "by_source": dict(labels), "by_label": dict(lab_out),
                   "gold_file_present": (AR / "labels/gold.jsonl").exists()},
        "route_log": {"rows": sum(route.values()), "by_task_type": dict(route.most_common(12))},
        "telemetry_strata": dict(strata.most_common()),
        "hangguard": dict(hg),
        "memory_files": mem,
        "xval_runs": sum(1 for _ in jl(AR / "xval_runs.jsonl")),
    }


# ---------------------------------------------------------------------------------------------- workflows

def workflows(steps):
    jobs = []
    for p in glob.glob(str(AR / "pi-jobs/*/meta.json")):
        try:
            jobs.append(json.load(open(p)))
        except Exception:
            pass
    jstate = collections.Counter(j.get("state") for j in jobs)
    jagent = collections.Counter(j.get("agent") for j in jobs)
    rl = sum(1 for j in jobs if re.search(r"429|rate.?limit", str(j.get("reason") or j.get("model_error") or ""), re.I))
    xv = list(jl(AR / "xval_runs.jsonl"))
    xcat = collections.Counter(r.get("category") for r in xv)
    xok = sum(bool(r.get("ok")) for r in xv)
    xcur = sum(1 for r in xv if win(r.get("ts")) == "cur")
    xprev = sum(1 for r in xv if win(r.get("ts")) == "prev")
    # Claude Code Agent / Task / Workflow dispatches and Skill calls in transcripts (live + mirror)
    disp = collections.Counter()
    skills_cc = collections.Counter()
    seen = set()
    for p in glob.glob(str(H / ".claude/projects/*/*.jsonl")) + glob.glob(str(AR / "transcripts/claude/**/*.jsonl"), recursive=True):
        b = os.path.basename(p)
        if b in seen:
            continue
        seen.add(b)
        for r in jl(p):
            msg = r.get("message") or {}
            if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
                continue
            for blk in msg["content"]:
                if blk.get("type") == "tool_use":
                    n = blk.get("name")
                    if n in ("Agent", "Task", "Workflow"):
                        disp[n] += 1
                    if n == "Skill":
                        skills_cc[(blk.get("input") or {}).get("skill") or (blk.get("input") or {}).get("command") or "?"] += 1
    return {
        "pi_jobs": {"n": len(jobs), "by_state": dict(jstate), "by_agent": dict(jagent), "rate_limited": rl},
        "xval": {"runs": len(xv), "ok": xok, "cur": xcur, "prev": xprev, "by_category": dict(xcat.most_common(8))},
        "claude_dispatch": dict(disp),
        "skill_calls_claude": dict(skills_cc),
        "workflow_arms": (steps.get("workflows") if isinstance(steps, dict) else None),
        "ed_arms": (steps.get("ed_arms") if isinstance(steps, dict) else None),
    }


# ---------------------------------------------------------------------------------------------- skills

TOOL_MARKERS = ('"toolCall"', '"function_call"', '"tool_use"', '"local_shell_call"', '"custom_tool_call"')


def skills():
    out = []
    dirs = {}
    for base in (H / ".claude/skills", H / ".agents/skills", REPO / "plugin/skills"):
        for d in sorted(glob.glob(str(base / "*"))):
            sk = Path(d) / "SKILL.md"
            if not sk.exists():
                continue
            real = os.path.realpath(d)
            if real in dirs:
                dirs[real]["where"].add(base.name if base.name != "skills" else str(base.parent.name))
                continue
            text = sk.read_text(errors="replace")
            m = re.search(r"^name:\s*(.+)$", text, re.M)
            dm = re.search(r"^description:\s*(?:>-?\s*\n\s+)?(.+)$", text, re.M)
            desc = (dm.group(1).strip() if dm else "").strip('"')
            first = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0]
            if len(first) > 170:
                first = first[:170].rsplit(" ", 1)[0].rstrip(" ,;:—-") + " …"
            n_scripts = len(glob.glob(str(Path(d) / "scripts/*")))
            dirs[real] = {"name": (m.group(1).strip() if m else Path(d).name), "desc": first, "scripts": n_scripts,
                          "where": {str(base.parent.name)}, "path": real}
    # usage: SKILL.md reads/mentions in pi + codex transcripts (live + mirror), last 14 days by file mtime
    names = {v["name"] for v in dirs.values()}
    use = collections.Counter()
    use_cur = collections.Counter()
    pat = re.compile(r"skills/(" + "|".join(map(re.escape, sorted(names, key=len, reverse=True))) + r")/(?:SKILL\.md|scripts/)")
    files = set()
    for g in (str(H / ".pi/agent/sessions/*/*.jsonl"), str(AR / "transcripts/pi/**/*.jsonl"), str(AR / "transcripts/codex/**/*.jsonl"),
              str(H / ".claude/projects/*/*.jsonl"), str(AR / "transcripts/claude/**/*.jsonl")):
        files.update(glob.glob(g, recursive=True))
    for p in files:
        try:
            if os.path.getmtime(p) < NOW - 14 * DAY:
                continue
            # only tool-call lines (pi toolCall, codex function_call, Claude tool_use): the system prompt lists every
            # skill path, so a plain text match would count every session
            txt = "\n".join(ln for ln in open(p, errors="replace") if any(k in ln for k in TOOL_MARKERS))
        except OSError:
            continue
        hits = collections.Counter(pat.findall(txt))
        for k in hits:
            use[k] += 1  # sessions that used the skill
            if os.path.getmtime(p) >= CUR[0]:
                use_cur[k] += 1
    for v in dirs.values():
        v["sessions_14d"] = use.get(v["name"], 0)
        v["sessions_7d"] = use_cur.get(v["name"], 0)
        v["where"] = sorted(v["where"])
    return sorted(dirs.values(), key=lambda v: (-v["sessions_14d"], v["name"]))


# ---------------------------------------------------------------------------------------------- changes

def commits(days=7):
    try:
        out = subprocess.run(["git", "-C", str(REPO), "log", f"--since={days} days ago", "--pretty=%h\t%ad\t%s", "--date=short"],
                             capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL).stdout
    except Exception:
        return []
    rows = [ln.split("\t", 2) for ln in out.splitlines() if ln.count("\t") >= 2]
    return [{"sha": a, "date": b, "subject": c} for a, b, c in rows]


def previous_snapshot():
    snaps = sorted(glob.glob(str(AR / "presentation/snapshot-*.json")))
    today = time.strftime("%Y-%m-%d")
    snaps = [s for s in snaps if not s.endswith(f"snapshot-{today}.json")]
    if not snaps:
        return None
    try:
        return json.load(open(snaps[-1]))
    except Exception:
        return None


def digest():
    """Last values of the nightly digest readouts (route-advise, codex context, refusals, handoff, offload)."""
    t = (AR / "offload_daily.md").read_text(errors="replace") if (AR / "offload_daily.md").exists() else ""
    out = {}
    ra = t.rfind("### route-advise")
    if ra >= 0:
        blk = t[ra:t.find("###", ra + 5)]
        out["route_advise"] = [{"task": m[0], "verdict": m[1], "n": int(m[2]), "rate": float(m[3]), "ci": [float(m[4]), float(m[5])]}
                               for m in re.findall(r"^\s+(\S+)\s+(\S+)\s+n=(\d+)\s+rate=([\d.]+) CI=\[([\d.]+),([\d.]+)\]", blk, re.M)]
    m = list(re.finditer(r"requests=(\d+)\s+ctx p50=([\d,]+) p95=([\d,]+) max=([\d,]+)\s*\nfits downshift \(<=([\d,]+) → ([^)]+)\): (\d+)/(\d+)", t))
    if m:
        g = m[-1].groups()
        out["codex_ctx"] = {"requests": int(g[0]), "p50": g[1], "p95": g[2], "max": g[3], "limit": g[4], "tier": g[5], "fits": int(g[6]), "of": int(g[7])}
    m = re.findall(r"refusals (\d+) / (\d+) v12 requests", t)
    if m:
        out["refusals"] = list(map(int, m[-1]))
    m = re.findall(r"basis: median of (\d+) sessions over (\d+)d", t)
    if m:
        out["handoff_sessions"] = int(m[-1][0])
    m = re.findall(r"total calls=(\d+)\s+gated=(\d+)\s+ok=(\d+)\s+escalated=(\d+)", t)
    if m:
        out["offload_total"] = dict(zip(("calls", "gated", "ok", "escalated"), map(int, m[-1])))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    tel = telemetry()
    steps = steps_dataset()
    d = {
        "built": time.strftime("%Y-%m-%d %H:%M %Z"),
        "commit": subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL).stdout.strip(),
        "window": {"cur": [time.strftime("%m-%d", time.localtime(CUR[0])), time.strftime("%m-%d", time.localtime(CUR[1]))],
                   "prev": [time.strftime("%m-%d", time.localtime(PREV[0])), time.strftime("%m-%d", time.localtime(PREV[1]))]},
        "telemetry": tel,
        "local": local(),
        "steps": steps,
        "data": data_volume(tel),
        "skills": skills(),
        "commits": commits(7),
        "digest": digest(),
    }
    d["workflows"] = workflows(steps)
    prev = previous_snapshot()
    d["previous_snapshot"] = {"built": prev.get("built"), "telemetry_total_all": prev["telemetry"]["total"]["all"],
                              "skills": {s["name"]: s["sessions_14d"] for s in prev.get("skills", [])},
                              "commit": prev.get("commit")} if prev else None
    (AR / "presentation").mkdir(parents=True, exist_ok=True)
    snap = AR / "presentation" / f"snapshot-{time.strftime('%Y-%m-%d')}.json"
    snap.write_text(json.dumps(d, default=str))
    s = json.dumps(d, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(s)
    else:
        print(s)


if __name__ == "__main__":
    main()
