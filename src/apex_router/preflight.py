"""Pre-analysis before a build (owner order 2026-10-27): stop a known mistake before it is made again.

``apex-router preflight --files A B ... [--task TEXT] [--agent worker --model M --thinking T] [--json]``

It reads only what this machine already has, and writes nothing:
  * the earlier review findings (``~/.apex-router/pi-jobs/*/result.md`` of xval jobs), sorted into
    fault types, for the given files (path match) and over all files;
  * the earlier jobs on the same files, and how they ended (done, cost limit, failed, timed out);
  * the review rounds per change in git (commit subjects that name an xval finding);
  * the cost advice of ``cost_advice`` (p75/p90 of done jobs for the agent/model/thinking cell).

The output is a brief block: SUCCESS and THREAT SCOPE (to fill in, before the build), KNOWN FAULT
TYPES with counts, and the TESTS TO WRITE FIRST for each type. It shows only fault-type names,
counts, job ids and repo paths, never finding text (the text can quote code or data).

Bounds: newest JOBS_MAX job dirs (names only, no stat), RESULT_MAX bytes per file, META_MAX per
meta.json, git log of the last GIT_DAYS days with a 10 s timeout. Fail open: a missing source gives
an empty section, never an error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

JOBS_MAX = 2000
RESULT_MAX = 64 * 1024
META_MAX = 256 * 1024
GIT_DAYS = 60
ROUND_LIMIT = 3     # owner rule: after round 3 an open finding goes to the owner as one question

# (name, regex on the lower-cased finding line, tests to write first)
FAULTS: tuple = (
    ("deadline or hang", r"deadline|hang|stall|timeout|block(s|ed|ing)? (the|a|until)|never returns",
     "a slow or stuck source (sleep, a held pipe) returns within the budget"),
    ("auth, origin or token", r"origin|token|auth|csrf|host header|unauthenticated",
     "a request with no token, a wrong token and a foreign Origin each get 403"),
    ("unbounded read or listing", r"unbounded|no (byte )?cap|without a (byte |size )?limit|whole file|entire|"
                                  r"reads? (every|all)|memory|enumerat",
     "an oversized file and a huge directory stay within the byte and entry caps"),
    ("raw text or secret in output", r"secret|leak|raw (text|row)|credential|unmasked|prompt text",
     "a row with a key-like string and long text shows only the fixed-map words"),
    ("signal or cleanup path", r"sigterm|signal|finally|cleanup|keyboardinterrupt|leak(s|ed)? (a|the) (process|child|fd)",
     "kill at each stage leaves no file, lock or child behind"),
    ("restart or rollback", r"rollback|bootout|restart|drain|plist",
     "a failure after each write step restores the earlier state"),
    ("race or check-then-use gap", r"race|toctou|between .{1,40} and|check.{0,20}then|replaced between|swap",
     "the object changes between the check and the use; the use refuses it"),
    ("path or symlink escape", r"symlink|realpath|outside (the|its) root|travers",
     "a symlink and a ../ path out of the root are refused"),
    ("wrong number or double count", r"double|counted twice|wrong (count|number|total)|miscount|incorrect (time|total)",
     "a fixture with known totals gives exactly those totals"),
    ("fail counted as pass", r"forge|crash(ed)? .{0,30}pass|false (pass|positive|retire)|accepts? .{0,30}(fail|invalid)",
     "a crash, a forged result and an invalid input each give FAIL"),
    ("data silently lost or merged", r"silently|lost|drops?|deactivat|removes? independent|overwrite",
     "every input item is still present (or reported) after the change"),
)
_FAULT_RX = tuple((n, re.compile(rx), t) for n, rx, t in FAULTS)
_FINDING = re.compile(r"\bP[01]\b\s*(\([^)]{0,20}\))?\s*[:\-—*]|\*\*P[01]\b|^\s*[-\d.]*\s*\**P[01]\b")
_STATUS = re.compile(r"\b(none|closed|fixed|no p0|no new|not a newly|confirms?)\b", re.I)
_PATH = re.compile(r"(?:src|tests|integrations|scripts|docs|bench)/[A-Za-z0-9_./-]+\.(?:py|ts|html|sh|md|json)")
_ROUND_RX = re.compile(r"xval (?:pass (\d+)|P[012]\b)|\(xval")


def router_home() -> Path:
    return Path(os.environ.get("APEX_ROUTER_HOME") or Path.home() / ".apex-router")


def _read(p: Path, cap: int) -> str | None:
    try:
        with open(p, "rb") as f:
            raw = f.read(cap + 1)
    except OSError:
        return None
    return raw[:cap].decode("utf-8", "replace")


def classify(line: str) -> list[str]:
    low = line.lower()
    return [n for n, rx, _ in _FAULT_RX if rx.search(low)]


def finding_lines(text: str) -> list[str]:
    """P0/P1 finding lines; status lines ("none", "closed") are not findings."""
    out = []
    for ln in text.splitlines():
        if _FINDING.search(ln) and not (_STATUS.search(ln) and not re.search(r"\*\*P[01]", ln)):
            out.append(ln)
    return out


def _norm(p: str) -> str:
    """Repo-relative path: drop any worktree or checkout prefix."""
    m = _PATH.search(p)
    return m.group(0) if m else p.strip("/")


def scan_jobs(jobs_dir: Path | None = None) -> list[dict]:
    """[{id, agent, state, cost, files, faults}] for the newest JOBS_MAX jobs (fail open)."""
    d = jobs_dir or router_home() / "pi-jobs"
    try:
        with os.scandir(d) as it:
            names = sorted((e.name for e in it), reverse=True)[:JOBS_MAX]
    except OSError:
        return []
    out = []
    for name in names:
        raw = _read(d / name / "meta.json", META_MAX)
        try:
            m = json.loads(raw) if raw else None
        except ValueError:
            m = None
        if not isinstance(m, dict):
            continue
        task = str(m.get("task") or "")[:20000]
        res = _read(d / name / "result.md", RESULT_MAX) or ""
        files = {_norm(x) for x in _PATH.findall(task + "\n" + res)}
        faults: Counter = Counter()
        per_file: dict = defaultdict(Counter)
        if m.get("agent") == "xval":
            for ln in finding_lines(res):
                cls = classify(ln) or ["other"]
                faults.update(cls)
                for f in {_norm(x) for x in _PATH.findall(ln)}:
                    per_file[f].update(cls)
        u = m.get("usage") if isinstance(m.get("usage"), dict) else {}
        cost = u.get("cost") if isinstance(u.get("cost"), (int, float)) else None
        out.append({"id": name, "agent": str(m.get("agent") or ""), "state": str(m.get("state") or ""),
                    "cost": cost, "files": sorted(files), "faults": dict(faults),
                    "per_file": {k: dict(v) for k, v in per_file.items()}})
    return out


def _touches(job_files, targets) -> bool:
    return any(t == f or f.endswith("/" + t) or t.endswith("/" + f) for t in targets for f in job_files)


def review_rounds(repo: Path, targets: list[str]) -> dict:
    """Commits in the last GIT_DAYS days whose subject names an xval finding, per target file."""
    try:
        p = subprocess.run(["git", "-C", str(repo), "log", f"--since={GIT_DAYS} days ago", "--name-only",
                            "--format=\x1e%s"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {"fix_commits": 0, "by_file": {}}
    by_file: Counter = Counter()
    n = 0
    for block in p.stdout.split("\x1e")[1:][:5000]:
        subj, _, rest = block.partition("\n")
        if not _ROUND_RX.search(subj):
            continue
        n += 1
        files = [ln.strip() for ln in rest.splitlines() if ln.strip()]
        for t in targets:
            if any(f == t or f.endswith("/" + t) for f in files):
                by_file[t] += 1
    return {"fix_commits": n, "by_file": dict(by_file)}


def rank(here: Counter, all_f: Counter, k: int = 6) -> list[str]:
    """Top-k fault types: 2 x share in these files + share over all files (variant C of 3 fixed
    variants, backtest on 41 earlier reviews, only earlier jobs used: recall of the real fault types in
    the top 6 was 78 %; global only 74 %; files-else-global 64 %; a random 6 of 11, 57 %)."""
    th, ta = sum(here.values()) or 1, sum(all_f.values()) or 1
    s = {n: 2 * here[n] / th + all_f[n] / ta for n in set(here) | set(all_f) if n != "other"}
    return [n for n, _ in sorted(s.items(), key=lambda x: (-x[1], x[0]))][:k]


def analyse(targets: list[str], jobs: list[dict] | None = None, repo: Path | None = None,
            cost: dict | None = None) -> dict:
    targets = [_norm(t) for t in targets]
    jobs = scan_jobs() if jobs is None else jobs
    all_f: Counter = Counter()
    here: Counter = Counter()
    for j in jobs:
        all_f.update(j["faults"])
        for f, c in j["per_file"].items():
            if _touches([f], targets):
                here.update(c)
    related = [j for j in jobs if j["agent"] != "xval" and _touches(j["files"], targets)]
    states = Counter(j["state"] for j in related)
    lost = round(sum(j["cost"] or 0 for j in related if j["state"].startswith("killed(cost")), 2)
    rounds = review_rounds(repo, targets) if repo else {"fix_commits": 0, "by_file": {}}
    top = rank(here, all_f)
    tests = {n: t for n, _, t in FAULTS if n in top}
    return {"targets": targets, "faults_here": dict(here), "faults_all": dict(all_f),
            "jobs_here": {"n": len(related), "states": dict(states), "usd_lost_at_cost_limit": lost,
                          "ids": [j["id"] for j in related[:5]]},
            "review_rounds": rounds, "round_limit": ROUND_LIMIT, "top": top, "tests_first": tests,
            "cost_advice": cost}


def brief_block(a: dict) -> str:
    """The text to paste into a brief (STE)."""
    src = a["faults_here"] or a["faults_all"]
    scope = "these files" if a["faults_here"] else "all files (none recorded for these files)"
    lines = ["SUCCESS (write before the build; the owner checks these):",
             "1. <what the user sees, in plain words>",
             "2. <invariants that must stay true>",
             "3. <budgets: time, memory, bytes, cost>",
             "THREAT SCOPE (set now; fixed for every review round):",
             "- In scope: <...>",
             "- Out of scope: <...>",
             f"KNOWN FAULT TYPES (earlier review findings on {scope}; check each before you commit):"]
    for n in a["top"]:
        lines.append(f"- {n} ({src.get(n, 0)})")
    lines.append("TESTS TO WRITE FIRST (each must fail on the old code):")
    for i, n in enumerate(a["top"], 1):
        lines.append(f"{i}. {a['tests_first'][n]}.")
    j = a["jobs_here"]
    if j["n"]:
        st = ", ".join(f"{k} {v}" for k, v in sorted(j["states"].items()))
        lines.append(f"EARLIER JOBS ON THESE FILES: {j['n']} ({st}); ${j['usd_lost_at_cost_limit']} lost at the cost limit.")
    r = a["review_rounds"]["by_file"]
    if r:
        lines.append("REVIEW-FIX COMMITS ON THESE FILES (last %d days): %s." % (
            GIT_DAYS, ", ".join(f"{k} {v}" for k, v in sorted(r.items(), key=lambda x: -x[1]))))
    c = a.get("cost_advice") or {}
    if c.get("advice"):
        lines.append(f"COST: done jobs p75 ${c['p75']:.2f}, p90 ${c['p90']:.2f} (n={c['n']}). "
                     "Set max_cost at p90 or more. Commit after every step.")
    lines.append(f"ROUNDS: after review round {a['round_limit']}, send an open finding to the owner as one question.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="apex-router preflight", description=__doc__.split("\n\n")[0])
    ap.add_argument("--files", nargs="+", required=True, help="repo paths the change touches")
    ap.add_argument("--agent", default="worker")
    ap.add_argument("--model", default=os.environ.get("PI_MODEL", ""))
    ap.add_argument("--thinking", default="high")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        from . import cost_advice
        cost = cost_advice.advice(a.agent, a.model, a.thinking) if a.model else None
    except Exception:  # noqa: BLE001 — advice is extra
        cost = None
    res = analyse(a.files, repo=Path(a.repo), cost=cost)
    print(json.dumps(res, indent=2, sort_keys=True) if a.json else brief_block(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
