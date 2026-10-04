#!/usr/bin/env bash
# DEPRECATED in 0.4.0: the datapce plugin writes these rows (finished at turn.complete). Kept side by side
# until route-join's writer_parity shows 14 days of parity; retired in 0.4.1.
# agent-route-log hook — PostToolUse matcher "Agent"
# Label source for the outcome router: appends ONE label-pending row per Claude Code
# Agent dispatch to the route log (~/.apex-router/route_log.jsonl, override APEX_ROUTER_LOG).
# Escalation is NOT decided here — `apex-router route-join` infers it offline (a later
# same-description dispatch at a strictly higher tier) and joins proxy telemetry by
# (session_id, agent_id). See docs/RUNBOOK-route-conformance.md.
#
# Fail-safe by contract: never blocks, prints nothing (stdout/stderr discarded so the tool
# result is never altered), always exits 0.
# All JSON parsing happens in Python (`python -m apex_router.route_log --hook` reads the
# hook payload on stdin) — no shell-built JSON.
#
# Time bounds: the 3 s SIGALRM inside route_log bounds the hook BODY only (it is armed after
# imports). Interpreter startup / import time is NOT covered by it — that is bounded by the
# `timeout` on the hook entry in settings.json, so always set one (e.g. "timeout": 5).
#
# Registration (settings.json): PostToolUse with matcher "Agent" (the current tool name).
# hook_main also accepts the legacy "Task" tool name, so a "Task" matcher keeps working on
# older Claude Code builds, but new installs should use "Agent".
#   {"matcher": "Agent", "hooks": [{"type": "command", "timeout": 5,
#     "command": "/path/to/apex-router/hooks/agent-route-log.sh"}]}
set -u

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || exit 0
SRC_DIR="$HOOK_DIR/../src"

# Python resolution: explicit override > the checkout this hook lives in (route_log is
# stdlib-only, so any python3 works with src/ on PYTHONPATH) > the uv-tool install
# ($UV_TOOL_DIR if set, else uv's default ~/.local/share/uv/tools).
PY_BIN="${APEX_ROUTE_LOG_PYTHON:-}"
UV_TOOLS="${UV_TOOL_DIR:-$HOME/.local/share/uv/tools}"
if [ -z "$PY_BIN" ]; then
  if [ -f "$SRC_DIR/apex_router/route_log.py" ]; then
    PY_BIN="python3"
    export PYTHONPATH="$SRC_DIR${PYTHONPATH:+:$PYTHONPATH}"
  elif [ -x "$UV_TOOLS/apex-router/bin/python" ]; then
    PY_BIN="$UV_TOOLS/apex-router/bin/python"
  else
    PY_BIN="python3"
  fi
fi
command -v "$PY_BIN" >/dev/null 2>&1 || exit 0

"$PY_BIN" -m apex_router.route_log --hook >/dev/null 2>&1 || true
exit 0
