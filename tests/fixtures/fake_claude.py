#!/usr/bin/env python3
"""Stand-in for `claude -p` in the mesh worker tests. Never calls a model.

Reads the prompt from stdin. Directives in the task text:
  DONE_AT=<step>   print DONE when this step is at or above <step> on the ladder, else BLOCKED
  ALWAYS_DONE      always DONE
  SLEEP=<s>        sleep before the answer (wall-cap test)
  NOCOMMIT         do not commit
  BIGPATCH         commit a 700 KiB file
  BIGREPORT        print a 100 KiB report before the last line
  AGENT_ERROR      exit 1 with is_error true
It always commits ``fake-<step>.txt`` (unless NOCOMMIT). <step> = "opus" for the Opus model, else
the --effort value. Each call appends one JSON row to $FAKE_CLAUDE_LOG (argv, cwd, step, a
snapshot of the worker ledger and running state).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

LADDER = ["medium", "high", "xhigh", "max", "opus"]


def _arg(name: str) -> str:
    a = sys.argv[1:]
    return a[a.index(name) + 1] if name in a else ""


def main() -> int:
    prompt = sys.stdin.read()
    model, effort = _arg("--model"), _arg("--effort")
    step = "opus" if "opus" in model else effort
    home = Path(os.environ.get("APEX_MESH_HOME", "/nonexistent"))
    wdir = home / "worker"
    row = {"argv": sys.argv[1:], "cwd": os.getcwd(), "step": step, "pid": os.getpid(),
           "prompt_len": len(prompt),
           "seen": (wdir / "seen.jsonl").read_text() if (wdir / "seen.jsonl").exists() else "",
           "running": json.loads((wdir / "running.json").read_text())
           if (wdir / "running.json").exists() else None,
           "results": {p.stem: json.loads(p.read_text()).get("status")
                       for p in (wdir / "results").glob("*.json")}
           if (wdir / "results").is_dir() else {}}
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(row) + "\n")
    m = re.search(r"SLEEP=(\d+(?:\.\d+)?)", prompt)
    if m:
        time.sleep(float(m.group(1)))
    if "NOCOMMIT" not in prompt:
        Path(f"fake-{step}.txt").write_text(f"step {step}\n")
        if "BIGPATCH" in prompt:
            Path("big.txt").write_text(os.urandom(350 * 1024).hex())
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"fake {step}"], check=True)
    m = re.search(r"DONE_AT=([a-z]+)", prompt)
    if "ALWAYS_DONE" in prompt:
        done = True
    elif m and m.group(1) in LADDER and step in LADDER:
        done = LADDER.index(step) >= LADDER.index(m.group(1))
    else:
        done = False
    body = ("x" * 100 * 1024 + "\n") if "BIGREPORT" in prompt else f"worked at {step}\n"
    last = "DONE" if done else "BLOCKED: fake says no"
    err = "AGENT_ERROR" in prompt
    print(json.dumps({"type": "result", "subtype": "success", "is_error": err,
                      "result": body + last, "total_cost_usd": 0.01,
                      "usage": {"input_tokens": 10, "output_tokens": 5,
                                "cache_read_input_tokens": 2}}))
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main())
