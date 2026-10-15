"""Live check (needs a model): background agent_spawn in rpc mode, parent turn ends, completion message wakes it.
python3 integrations/pi/tests/live/push_rpc.py [--model it-entra-claude-haiku-4-5]
PASS when: (1) parent turn 1 ends before the job ends, (2) an apex-agent-done message arrives, (3) the parent
answers with the scout's number in a later turn, (4) no bash sleep call in the parent."""
import json, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else "it-entra-claude-haiku-4-5"
out = Path("/tmp/apex-push-rpc.jsonl")
cmd = ["pi", "--mode", "rpc", "--no-session", "--no-extensions", "--provider", "foundry", "--model", model]
for e in ("apex-ste", "apex-hangguard", "apex-agents"):
    cmd += ["-e", str(ROOT / f"{e}.ts")]
p = subprocess.Popen(cmd, cwd=ROOT.parents[1], stdin=subprocess.PIPE, stdout=open(out, "w"), stderr=subprocess.DEVNULL, text=True)
p.stdin.write(json.dumps({"id": "1", "type": "prompt", "message":
    "Call agent_spawn with agent=scout and task 'TASK: Count the .ts files in integrations/pi/lib (not tests). RESULT: the number only.' "
    "Do not wait. Then reply with the single word SPAWNED and stop. When a sub-agent result message arrives, reply 'RESULT: <number>'."}) + "\n")
p.stdin.flush()

def events():
    return [json.loads(l) for l in out.read_text().splitlines() if l.strip().startswith("{")]

def texts(ev):
    for e in ev:
        if e.get("type") == "message_end" and e["message"].get("role") == "assistant":
            yield "".join(b.get("text", "") for b in e["message"]["content"] if b.get("type") == "text")

t0, ok = time.time(), False
while time.time() - t0 < 240:
    time.sleep(2)
    if any("RESULT:" in t for t in texts(events())):
        ok = True
        break
p.stdin.close(); p.terminate(); p.wait(10)
ev = events()
order = [e["type"] if e["type"] != "message_end" else f"msg:{e['message'].get('role')}" for e in ev if e.get("type") in ("agent_end", "message_end")]
calls = [e.get("toolName") for e in ev if e.get("type") == "tool_execution_start"]
bash_sleep = [e for e in ev if e.get("type") == "tool_execution_start" and e.get("toolName") == "bash" and "sleep" in json.dumps(e.get("args"))]
custom = [e for e in ev if e.get("type") == "message_end" and e["message"].get("role") == "custom"]
first_end = order.index("agent_end") if "agent_end" in order else -1
res = {"pass": ok and len(custom) >= 1 and first_end >= 0 and not bash_sleep and "agent_wait" not in calls,
       "result_text": [t for t in texts(ev) if "RESULT" in t][:1], "custom_msgs": len(custom), "tool_calls": calls,
       "sequence": order, "secs": round(time.time() - t0, 1)}
print(json.dumps(res, indent=1))
sys.exit(0 if res["pass"] else 1)
