#!/usr/bin/env bash
# ground-check hook — Stop matcher
# When the assistant's last message cites code (file:line), run the codeqa grounding
# oracle on it: a DETERMINISTIC check (no model) that each cited file exists and the
# cited span is within the live file. A STALE citation means the finding cites a line
# that isn't there — surface it as additionalContext so the model can correct before
# the user trusts the report. Advisory only: never blocks, stays silent on any error,
# and self-skips when nothing is groundable (no citations / no registered repos).
set -euo pipefail

command -v jq >/dev/null 2>&1 || exit 0

PY_BIN="${APEX_GROUND_PYTHON:-$HOME/.local/share/uv/tools/apex-router/bin/python}"
[ -x "$PY_BIN" ] || PY_BIN="python3"
REPOS="${CODEQA_REPOS:-$HOME/.apex/codeqa-repos}"
[ -d "$REPOS" ] || exit 0   # no registered repos -> the oracle self-skips anyway; stay silent

input="$(cat)"
transcript="$(printf '%s' "$input" | jq -r '.transcript_path // empty')"
stop_active="$(printf '%s' "$input" | jq -r '.stop_hook_active // false')"
[ "$stop_active" = "true" ] && exit 0
[ -n "$transcript" ] && [ -f "$transcript" ] || exit 0

# Last assistant text from the transcript (JSONL; assistant entries carry message.content
# blocks). Bail if it has no file:line citation — the oracle is for cited claims only.
last_text="$(python3 - "$transcript" <<'PY' 2>/dev/null
import json, re, sys
text = ""
for line in open(sys.argv[1], errors="replace"):
    try:
        rec = json.loads(line)
    except Exception:
        continue
    msg = rec.get("message") or {}
    if msg.get("role") != "assistant":
        continue
    parts = [b.get("text", "") for b in (msg.get("content") or [])
             if isinstance(b, dict) and b.get("type") == "text"]
    if parts:
        text = "\n".join(parts)
if re.search(r"[\w./-]+\.[A-Za-z]\w*:\d+", text):
    print(text)
PY
)"
[ -n "$last_text" ] || exit 0

# Run the oracle. --check exits 2 when any citation is STALE (cited span past EOF).
verdict="$(printf '%s' "$last_text" | CODEQA_REPOS="$REPOS" "$PY_BIN" \
  -m apex_router.codeqa.cli ground 2>/dev/null || true)"
[ -n "$verdict" ] || exit 0

case "$verdict" in
  *STALE*)
    summary="$(printf '%s\n' "$verdict" | head -1)"
    stale_lines="$(printf '%s\n' "$verdict" | grep -i stale | head -5)"
    jq -n --arg ctx "grounding oracle: ${summary}. Stale citation(s) — the cited line does not exist in the live file:
${stale_lines}
Treat the affected finding(s) as factually broken regardless of how plausible the prose is; re-check the code and correct them before the user relies on this report." \
      '{hookSpecificOutput: {hookEventName: "Stop", additionalContext: $ctx}}'
    ;;
esac
exit 0
