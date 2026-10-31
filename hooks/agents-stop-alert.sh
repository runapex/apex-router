#!/usr/bin/env bash
# agents-stop-alert — Claude Code Stop hook (AGENTS-INBOX.md section 6).
# Shows new unresolved blockers of pi jobs and Claude Code agents to the user (systemMessage), at most 5 lines.
# Advisory only: never blocks, never feeds the model, exit 0 on any error. Bounded: 5 s wall clock.
input="$(cat 2>/dev/null)"
case "$input" in *'"stop_hook_active":true'* | *'"stop_hook_active": true'*) exit 0 ;; esac
bin="${APEX_ROUTER_BIN:-apex-router}"
command -v "$bin" >/dev/null 2>&1 || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
out="$(APEX_AGENTS_BIN="$bin" python3 - <<'PY' 2>/dev/null
import json, os, subprocess
try:
    r = subprocess.run([os.environ["APEX_AGENTS_BIN"], "agents", "--new-blockers"], capture_output=True,
                       timeout=5, stdin=subprocess.DEVNULL)
except Exception:
    raise SystemExit(0)
if r.returncode != 0:
    raise SystemExit(0)
lines = [l for l in r.stdout[:8192].decode("utf-8", "replace").splitlines() if l.strip()][:5]
lines = ["".join(ch for ch in l if ch.isprintable())[:160] for l in lines]
if lines:
    print(json.dumps({"systemMessage": "agents need you:\n" + "\n".join(lines) + "\n(/agents for the list)"}))
PY
)"
[ -n "$out" ] && printf '%s\n' "$out"
exit 0
