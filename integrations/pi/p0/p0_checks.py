#!/usr/bin/env python3
"""P0 spike for DESIGN-pi-subagents.md: four yes/no checks against the installed pi, no model logic.

  python3 integrations/pi/p0/p0_checks.py [--only 1 2 3 4] [--model it-entra-claude-haiku-4-5]

Writes one JSON line per check to ~/.apex-router/pi-p0/results.jsonl and prints a summary.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = Path.home() / ".apex-router" / "pi-p0"
PROVIDER = "foundry"


def pi(args, model, **kw):
    return ["pi", *args, "--provider", PROVIDER, "--model", model]


def read_events(path):
    out = []
    for line in Path(path).read_text(errors="replace").split("\n"):
        line = line.rstrip("\r")
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def texts(events):
    t = []
    for e in events:
        m = e.get("message") if isinstance(e.get("message"), dict) else None
        if e.get("type") == "message_end" and m and m.get("role") == "assistant":
            t += [b.get("text", "") for b in m.get("content") or [] if isinstance(b, dict) and b.get("type") == "text"]
    return t


# ---------------------------------------------------------------- check 1: deferred sendMessage

def check1(model):
    """A timer in an extension calls pi.sendMessage 3 s after its tool returned. Does it wake the agent?"""
    out, err = WORK / "c1.jsonl", WORK / "c1.err"
    p = subprocess.Popen(pi(["--mode", "rpc", "--no-session", "-e", str(HERE / "p0-defer.ts"), "--tools", "p0_defer"],
                            model), stdin=subprocess.PIPE, stdout=open(out, "w"), stderr=open(err, "w"), text=True)
    p.stdin.write(json.dumps({"id": "1", "type": "prompt", "message":
        "Call the p0_defer tool once. Then reply with the single word WAITING and stop. If you later receive a "
        "message that contains DEFERRED-PING, reply with the single word GOT-PING."}) + "\n")
    p.stdin.flush()
    deadline, got = time.time() + 90, False
    while time.time() < deadline:
        time.sleep(2)
        if any("GOT-PING" in t for t in texts(read_events(out))):
            got = True
            break
    p.stdin.close()
    p.terminate()
    p.wait(10)
    ev = read_events(out)
    threw = "P0_SENDMESSAGE_THREW" in Path(err).read_text()
    return {"check": 1, "name": "deferred sendMessage wakes the agent", "pass": got and not threw,
            "evidence": {"assistant_texts": [t[:60] for t in texts(ev)], "sendMessage_threw": threw,
                         "agent_starts": sum(e.get("type") == "agent_start" for e in ev)}}


# ---------------------------------------------------------------- check 2: liveness during a long tool call

def check2(model):
    """Does --mode json keep emitting events while a child runs a long tool call?
    (a) a 60 s command that prints every 10 s; (b) a 60 s command that prints nothing."""
    res = {}
    for name, cmd in (("ticking", "for i in 1 2 3 4 5 6; do echo tick $i; sleep 10; done"),
                      ("silent", "sleep 60; echo done")):
        out = WORK / f"c2-{name}.jsonl"
        stamps = []
        p = subprocess.Popen(pi(["--mode", "json", "-p", "--no-session", "--tools", "bash"], model) +
                             [f"Run exactly this bash command with timeout 120, then reply DONE: {cmd}"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        with open(out, "w") as f:
            for line in p.stdout:
                stamps.append((time.time(), json.loads(line).get("type") if line.strip() else None))
                f.write(line)
        p.wait()
        ts = [t for t, _ in stamps]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        res[name] = {"events": len(ts), "max_gap_s": round(max(gaps), 1) if gaps else None,
                     "update_events": sum(k == "tool_execution_update" for _, k in stamps)}
    # pass = the ticking child shows life at least every ~15 s; silent child's gap is recorded, not judged
    ok = res["ticking"]["max_gap_s"] is not None and res["ticking"]["max_gap_s"] < 15
    return {"check": 2, "name": "json stream shows liveness during a long tool call", "pass": ok, "evidence": res}


# ---------------------------------------------------------------- check 3: resume after SIGKILL

def check3(model):
    """Kill a child mid-tool-call with SIGKILL, then resume its --session-id. Does it keep its context?"""
    sid = f"apexp0-{uuid.uuid4().hex[:8]}"
    cwd = WORK / "c3"
    cwd.mkdir(parents=True, exist_ok=True)
    out = WORK / "c3-a.jsonl"
    p = subprocess.Popen(pi(["--mode", "json", "-p", "--session-id", sid, "--tools", "bash"], model) +
                         ["Remember the code word PELICAN-7. Now run this bash command with timeout 120: sleep 60. "
                          "Then reply DONE."], cwd=cwd, stdin=subprocess.DEVNULL, stdout=open(out, "w"),
                         stderr=subprocess.DEVNULL, start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < 60:
        time.sleep(1)
        if any(e.get("type") == "tool_execution_start" for e in read_events(out)):
            break
    time.sleep(2)
    os.killpg(p.pid, signal.SIGKILL)
    p.wait()
    killed_mid_tool = not any(e.get("type") == "tool_execution_end" for e in read_events(out))
    r = subprocess.run(pi(["--mode", "json", "-p", "--session-id", sid, "--tools", "bash"], model) +
                       ["What was the code word I gave you? Reply with the code word only."], cwd=cwd,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
    out_b = WORK / "c3-b.jsonl"
    out_b.write_text(r.stdout)
    answer = " ".join(texts(read_events(out_b)))
    return {"check": 3, "name": "--session-id resumes after SIGKILL", "pass": killed_mid_tool and "PELICAN-7" in answer,
            "evidence": {"session_id": sid, "killed_mid_tool": killed_mid_tool, "resume_answer": answer[:80],
                         "resume_exit": r.returncode}}


# ---------------------------------------------------------------- check 4: process-group kill reaches grandchildren

def check4(model):
    """Start pi in its own process group; its bash tool starts a long grandchild. Kill the group.
    Does the grandchild die too?"""
    pidf = WORK / "c4-gc.pid"
    pidf.unlink(missing_ok=True)
    p = subprocess.Popen(pi(["--mode", "json", "-p", "--no-session", "--tools", "bash"], model) +
                         [f"Run exactly this bash command with timeout 300: sleep 240 & echo $! > {pidf}; wait"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < 60 and not pidf.exists():
        time.sleep(1)
    gc = int(pidf.read_text().strip()) if pidf.exists() else None
    def ps_field(pid, field):   # os.getpgid() is EPERM on macOS for a process in another session
        r = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True)
        return r.stdout.strip() or None
    gc_pgid = ps_field(gc, "pgid") if gc else None
    gc_sess = ps_field(gc, "sess") if gc else None
    os.killpg(p.pid, signal.SIGTERM)
    time.sleep(5)
    pi_exited_on_term = p.poll() is not None
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):   # macOS: EPERM when only a zombie leader is left
        pass
    p.wait()
    time.sleep(1)
    alive = bool(gc) and ps_field(gc, "pid") is not None
    if alive:
        subprocess.run(["kill", "-9", str(gc)])   # clean up what the group kill missed
    return {"check": 4, "name": "process-group kill reaches the bash grandchild", "pass": bool(gc) and not alive,
            "evidence": {"grandchild_pid": gc, "pi_pgid": p.pid, "grandchild_pgid": gc_pgid,
                         "same_group": gc_pgid == str(p.pid), "grandchild_sess": gc_sess, "alive_after_group_kill": alive,
                         "pi_exited_on_sigterm": pi_exited_on_term}}


def check5(model):
    """Same as check 4 but SIGKILL straight away (no SIGTERM). Does the detached grandchild survive?"""
    pidf = WORK / "c5-gc.pid"
    pidf.unlink(missing_ok=True)
    p = subprocess.Popen(pi(["--mode", "json", "-p", "--no-session", "--tools", "bash"], model) +
                         [f"Run exactly this bash command with timeout 300: sleep 240 & echo $! > {pidf}; wait"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < 60 and not pidf.exists():
        time.sleep(1)
    gc = int(pidf.read_text().strip()) if pidf.exists() else None
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    p.wait()
    time.sleep(1)
    r = subprocess.run(["ps", "-o", "pid=,ppid=,pgid=", "-p", str(gc)], capture_output=True, text=True) if gc else None
    alive = bool(r and r.stdout.strip())
    if alive:
        subprocess.run(["kill", "-9", str(gc)])
    # pass = the orphan risk is REAL (documents that SIGTERM-first is required); the design must not rely on SIGKILL
    return {"check": 5, "name": "SIGKILL alone orphans the bash grandchild (expected: yes)", "pass": alive,
            "evidence": {"grandchild_pid": gc, "alive_after_sigkill_only": alive,
                         "ps": r.stdout.strip() if r else None}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="+", type=int, default=[1, 2, 3, 4, 5])
    ap.add_argument("--model", default="it-entra-claude-haiku-4-5")
    a = ap.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    fns = {1: check1, 2: check2, 3: check3, 4: check4, 5: check5}
    with open(WORK / "results.jsonl", "a") as f:
        for n in a.only:
            t0 = time.time()
            try:
                r = fns[n](a.model)
            except Exception as e:  # a crashed check is a FAIL with the reason, never a silent pass
                r = {"check": n, "pass": False, "evidence": {"error": f"{type(e).__name__}: {e}"}}
            r.update(model=a.model, seconds=round(time.time() - t0, 1), ts=time.strftime("%Y-%m-%dT%H:%M:%S"))
            f.write(json.dumps(r) + "\n")
            print(json.dumps(r))


if __name__ == "__main__":
    main()
