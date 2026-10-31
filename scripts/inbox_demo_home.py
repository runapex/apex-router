#!/usr/bin/env python3
"""Make a fictional home for the agent inbox screenshots (README). No real job, path or name.

Usage: python3 scripts/inbox_demo_home.py <dir>
Then:  HOME=<dir> APEX_HOME=<dir>/.apex-router apex-router agents
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

TASKS = [
    # (suffix, role, state, age_s, run_s, cost, task, result or None)
    ("k3pq", "worker", "running", 0, 420, 2.10, "Add retry with jitter to the upload client", None),
    ("w8zm", "xval", "running", 0, 95, 0.0, "Review the retry change on branch feat/upload-retry", None),
    ("r2fd", "worker", "done", 600, 900, 4.85, "Port the CSV export to the new storage API",
     "Export works for all 3 formats.\n\n## OPEN\n- [P1] The 2 GiB test file is not in CI yet.\n"
     "- [P2] `export.py` keeps a 64 MiB buffer; make it stream.\n- Docs need one example per format.\n"
     "VERDICT: DONE\n"),
    ("t6nv", "xval", "done", 1500, 300, 0.0, "Review the cache eviction fix in src/cache/lru.py",
     "| Severity | File:line | Input | Effect |\n|---|---|---|---|\n"
     "| P1 | `lru.py:88` | two writers | the size counter can go negative |\n\n"
     "## OPEN\n- [P1] `lru.py:88`: guard the size counter with the lock.\nVERDICT: NO-GO\n"),
    ("b5hc", "worker", "capped", 4200, 1800, 12.40, "Find the root cause of the nightly build timeout",
     "Stopped at the cost limit.\n\n## OPEN\n- [P2] The timeout comes from the docs build; not proven yet.\n"
     "- [P3] Two flaky tests in test_net.py.\nVERDICT: BLOCKED\n"),
    ("m1ya", "worker", "done", 7300, 600, 3.20, "Upgrade the date library and fix the 4 call sites",
     "All call sites use the new API.\n\n## OPEN\n- none\nVERDICT: DONE\n"),
    ("q9ke", "scout", "done", 9000, 120, 0.40, "List every caller of parse_config with file:line",
     "14 callers found.\n\nVERDICT: DONE\n"),
    ("p4tj", "xval", "done", 12000, 240, 0.0, "Review the date library upgrade", "No findings.\n\nVERDICT: GO\n"),
]


def job_id(t: float, suffix: str) -> str:
    return time.strftime("%Y%m%d%H%M%S", time.gmtime(t)) + "-" + suffix


def main(root: Path) -> None:
    now = time.time()
    rh = root / ".apex-router"
    for suffix, role, state, age, run, cost, task, result in TASKS:
        end = now - age
        start = end - run if state != "running" else now - run
        jid = job_id(start, suffix)
        d = rh / "pi-jobs" / jid
        d.mkdir(parents=True, exist_ok=True)
        cwd = "/home/dev/projects/shop-api"
        meta = {"id": jid, "state": "done" if state == "capped" else state, "agent": role, "kind": "xval" if role == "xval" else "pi",
                "task": f"TASK: {task}\nCONTEXT: (brief)", "cwd": cwd, "created": int(start * 1000),
                "started": int(start * 1000), "session_id": f"apexjob-{jid}", "usage": {"turns": 9, "cost": cost}}
        if state != "running":                        # a running job has no pid here: the inbox keeps it running
            meta["ended"] = int(end * 1000)
        if state == "capped":
            meta["capped"] = True
        (d / "meta.json").write_text(json.dumps(meta))
        (d / "events.jsonl").write_text("")
        if result is not None:
            (d / "result.md").write_text(f"# job {jid} — {state}\nagent {role} · {run} s · 9 turns\n\n{result}")
    # one Claude Code sub-agent with an open item
    sid = "5b0c1d2e-3f40-4a5b-8c6d-7e8f90a1b2c3"
    sub = root / ".claude" / "projects" / "-home-dev-projects-shop-api" / sid / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    base = {"agentId": "a1b2c3d4e5f6a7b8c", "cwd": "/home/dev/projects/shop-api", "gitBranch": "main",
            "sessionId": sid, "isSidechain": True}
    rows = [dict(base, type="user", timestamp="2026-10-30T10:00:00Z",
                 message={"role": "user", "content": "TASK: Audit the API error codes against the docs"}),
            dict(base, type="assistant", timestamp="2026-10-30T10:05:00Z",
                 message={"role": "assistant", "content": [{"type": "text", "text":
                          "Done.\n\n## OPEN\n- [P2] 3 codes are not in the docs: E409, E422, E451.\n"}]})]
    p = sub / "agent-a1b2c3d4e5f6a7b8c.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    t = now - 2400
    os.utime(p, (t, t))
    os.utime(sub, (t, t))
    print(root)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
