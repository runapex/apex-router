#!/usr/bin/env python3
"""Stand-in for `codex exec` in the mesh worker tests. Never calls a model.

Expects ``exec ... -o FILE -`` and reads the prompt from stdin. The ladder step is the name of the
worktree (cwd). Directives in the task text are the same as tests/fixtures/fake_claude.py:
  DONE_AT=<step>, ALWAYS_DONE, SLEEP=<s>, NOCOMMIT, BIGREPORT,
  AGENT_ERROR   print an error and turn.failed event, write no -o file, exit 1
  NO_LAST_FILE  do not write the -o file (the report comes from the agent_message event)
Prints JSONL events like codex exec --json: thread.started, turn.started, item.completed
(agent_message), turn.completed with usage. Each call appends one JSON row to
$FAKE_CLAUDE_LOG (argv, cwd, step, pid, the running state).
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


def _emit(ev: dict) -> None:
    print(json.dumps(ev), flush=True)


def main() -> int:
    a = sys.argv[1:]
    if a[:1] != ["exec"] or a[-1:] != ["-"]:
        print("fake codex: want `exec ... -`", file=sys.stderr)
        return 2
    prompt = sys.stdin.read()
    step = Path.cwd().name
    wdir = Path(os.environ.get("APEX_MESH_HOME", "/nonexistent")) / "worker"
    row = {"argv": a, "cwd": os.getcwd(), "step": step, "pid": os.getpid(),
           "prompt_len": len(prompt),
           "running": json.loads((wdir / "running.json").read_text())
           if (wdir / "running.json").exists() else None}
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(row) + "\n")
    _emit({"type": "thread.started", "thread_id": "fake"})
    _emit({"type": "turn.started"})
    m = re.search(r"SLEEP=(\d+(?:\.\d+)?)", prompt)
    if m:
        time.sleep(float(m.group(1)))
    if "AGENT_ERROR" in prompt:
        _emit({"type": "error", "message": "fake failure"})
        _emit({"type": "turn.failed", "error": {"message": "fake failure"}})
        return 1
    if "NOCOMMIT" not in prompt:
        Path(f"fake-{step}.txt").write_text(f"step {step}\n")
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
    text = body + ("DONE" if done else "BLOCKED: fake says no")
    _emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",
                                              "text": text}})
    _emit({"type": "turn.completed",
           "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7}})
    if "NO_LAST_FILE" not in prompt:
        Path(_arg("-o")).write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
