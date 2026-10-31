#!/usr/bin/env bash
# agents-statusline — prints the agent inbox segment for the Claude Code status line ("● 2 ⚠ 3", or nothing).
# Never waits for the inbox: it prints the cached line (agent-inbox-line.json) and refreshes the cache in the
# background at most once in 5 s. Use: seg="$(bash <this file>)" in the status line command.
rh="${APEX_HOME:-$HOME/.apex-router}"
bin="${APEX_ROUTER_BIN:-apex-router}"
cache="$rh/agent-inbox-line.json"
stamp="$rh/agent-inbox-line.spawn"
# xval x2e6: "head" blocked on a FIFO. Open non-blocking, no symlink, regular files only.
line="$(python3 - "$cache" <<'PY' 2>/dev/null
import json, os, re, stat, sys
try:
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        raw = os.read(fd, 4096) if stat.S_ISREG(st.st_mode) and st.st_size <= 4096 else b""
    finally:
        os.close(fd)
    l = json.loads(raw).get("line", "")
except Exception:
    l = ""
print(l if isinstance(l, str) and re.fullmatch(r"● [0-9]{1,6} ⚠ [0-9]{1,6}", l) else "")
PY
)"
[ -n "$line" ] && printf '%s' "$line"
now=$(date +%s)
last=$(stat -f %m "$stamp" 2>/dev/null || stat -c %Y "$stamp" 2>/dev/null || echo 0)
if [ $((now - last)) -ge 5 ] && command -v "$bin" >/dev/null 2>&1; then
  mkdir -p "$rh" 2>/dev/null && : > "$stamp" 2>/dev/null
  ( "$bin" agents --line >/dev/null 2>&1 & ) >/dev/null 2>&1
fi
exit 0
