#!/usr/bin/env bash
# agents-statusline — prints the agent inbox segment for the Claude Code status line ("● 2 ⚠ 3", or nothing).
# Never waits for the inbox: it prints the cached line (agent-inbox-line.json) and refreshes the cache in the
# background at most once in 5 s. Use: seg="$(bash <this file>)" in the status line command.
rh="${APEX_HOME:-$HOME/.apex-router}"
bin="${APEX_ROUTER_BIN:-apex-router}"
cache="$rh/agent-inbox-line.json"
stamp="$rh/agent-inbox-line.spawn"
if [ -r "$cache" ]; then
  line="$(head -c 4096 "$cache" 2>/dev/null | python3 -c 'import json,re,sys
try: l=json.load(sys.stdin).get("line","")
except Exception: l=""
print(l if isinstance(l,str) and re.fullmatch(r"● [0-9]{1,6} ⚠ [0-9]{1,6}", l) else "")' 2>/dev/null)"
  [ -n "$line" ] && printf '%s' "$line"
fi
now=$(date +%s)
last=$(stat -f %m "$stamp" 2>/dev/null || stat -c %Y "$stamp" 2>/dev/null || echo 0)
if [ $((now - last)) -ge 5 ] && command -v "$bin" >/dev/null 2>&1; then
  mkdir -p "$rh" 2>/dev/null && : > "$stamp" 2>/dev/null
  ( "$bin" agents --line >/dev/null 2>&1 & ) >/dev/null 2>&1
fi
exit 0
