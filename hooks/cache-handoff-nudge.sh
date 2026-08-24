#!/usr/bin/env bash
# cache-handoff-nudge hook — Stop matcher
# When a Claude Code session has grown large enough that its re-read-every-turn
# prefix is expensive (cache-read tokens), write a handoff doc and nudge the user
# to start a fresh session. Advisory ONLY — never blocks, never resets (Claude
# Code cannot be externally reset), emits additionalContext only.
#
# Signal (primary): the apex proxy session_id is byte-identical to the Claude
# Code session_id, so we look up this session's cumulative cache-read tokens in
# ~/.apex/telemetry.jsonl. Fallback: if the session isn't in telemetry yet
# (proxy not in path / timing), count assistant messages in the transcript.
#
# Free (jq + python3 for the tail scan), no LLM call. Stays silent on any error.
set -euo pipefail

command -v jq >/dev/null 2>&1 || exit 0

input="$(cat)"
session_id="$(printf '%s' "$input" | jq -r '.session_id // empty')"
transcript="$(printf '%s' "$input" | jq -r '.transcript_path // empty')"
stop_active="$(printf '%s' "$input" | jq -r '.stop_hook_active // false')"

# Guard against a feedback loop: if we're already inside a stop-hook-triggered
# continuation, do nothing.
[ "$stop_active" = "true" ] && exit 0
[ -z "$session_id" ] && exit 0

# session_id comes from JSON stdin and is used to build filenames — never trust
# it as a path. Claude Code session ids are UUIDs; require that shape and bail on
# anything with slashes, dots-only, or path-traversal characters (Codex xval #2).
case "$session_id" in
  *[!A-Za-z0-9._-]* | *..* | .* ) exit 0 ;;
esac

# --- tunables (env-overridable) --------------------------------------------
# Policy: START AGGRESSIVE (nudge early), RELAX over time only if signals show
# the nudges are premature. 100M read tokens ~ $50 of accumulated read cost —
# catches the fat-tail sessions, not just the single largest. This is a
# deliberately low initial cap, NOT a data-fit; the per-repo adaptive threshold
# (proposed nightly from cache_report.py once >=7d of data exist) raises it per
# key as the measured distribution justifies. Override per repo/task via env.
# Adaptive threshold (B2): env override wins; else the nightly-computed p80 of per-session
# cumulative reads (scripts/handoff_threshold.py → ~/.apex-router/handoff_threshold.json);
# else the static 100M fallback. Extract with python3 (jq is not guaranteed); any failure
# falls through to the static default — the nudge is advisory and must never break a Stop.
ADAPTIVE_FILE="${APEX_HANDOFF_THRESHOLD_FILE:-$HOME/.apex-router/handoff_threshold.json}"
if [ -z "${CACHE_HANDOFF_READ_THRESHOLD:-}" ] && [ -f "$ADAPTIVE_FILE" ]; then
  _adaptive=$(python3 -c "
import json,sys
try:
    d=json.load(open('$ADAPTIVE_FILE'))
    t=d.get('threshold_tokens')
    print(int(t) if isinstance(t,(int,float)) and t>0 else '')
except Exception:
    pass
" 2>/dev/null)
  READ_TOKEN_THRESHOLD="${_adaptive:-100000000}"
else
  READ_TOKEN_THRESHOLD="${CACHE_HANDOFF_READ_THRESHOLD:-100000000}"
fi
MSG_THRESHOLD="${CACHE_HANDOFF_MSG_THRESHOLD:-200}"   # fallback proxy (aggressive)
TELEMETRY="${APEX_TELEMETRY:-$HOME/.apex/telemetry.jsonl}"
HANDOFF_DIR="${CACHE_HANDOFF_DIR:-$HOME/.claude/handoffs}"
STAMP="$HANDOFF_DIR/.nudged-$session_id"   # once per session — don't re-nag

# Already nudged this session → silent.
[ -f "$STAMP" ] && exit 0

# --- primary signal: cumulative cache-read tokens for this session ----------
read_tokens=0
if [ -f "$TELEMETRY" ]; then
  read_tokens="$(
    SID="$session_id" TEL="$TELEMETRY" python3 - <<'PY' 2>/dev/null || echo 0
import json, os
sid = os.environ["SID"]; tel = os.environ["TEL"]
tot = 0
try:
    with open(tel, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue          # tolerate a half-written trailing line
            if d.get("ev") == "hb":
                continue          # heartbeats carry no read tokens
            if d.get("session_id") == sid:
                v = d.get("cache_read_tokens")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    tot += v
except FileNotFoundError:
    pass
print(int(tot))
PY
  )"
fi

# --- fallback signal: assistant-message count in the transcript -------------
# ONLY when telemetry has no cache-read for this session (read == 0, i.e. no
# apex proxy in the path). When telemetry HAS a read count, that count IS the
# authoritative cost signal — a below-threshold read means "not expensive",
# regardless of turn count, so the turn-count proxy must NOT fire as a second
# trigger. (Codex xval #4 argued the opposite; e2e on a real 135M-read/1611-turn
# session proved that fix wrong — it fired a below-cost session with a false
# "telemetry unavailable" message. Reverted to the read-authoritative gate.)
msg_count=0
if [ "$read_tokens" -eq 0 ] && [ -n "$transcript" ] && [ -f "$transcript" ]; then
  msg_count="$(
    TRANSCRIPT="$transcript" python3 - <<'PY' 2>/dev/null || echo 0
import json, os
path = os.environ["TRANSCRIPT"]
n = 0
try:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") == "assistant":
                n += 1
except FileNotFoundError:
    pass
print(n)
PY
  )"
fi

# --- decide -----------------------------------------------------------------
triggered=0
reason=""
if [ "$read_tokens" -ge "$READ_TOKEN_THRESHOLD" ]; then
  triggered=1
  # portable thousands-separator (BSD/macOS sed lacks GNU \B\{3\}); python does it.
  pretty="$(READ="$read_tokens" python3 - <<'PY' 2>/dev/null || echo "$read_tokens"
import os
print(f'{int(os.environ["READ"]):,}')
PY
)"
  reason="$(printf 'has re-read %s cache tokens' "$pretty")"
elif [ "$msg_count" -ge "$MSG_THRESHOLD" ]; then
  triggered=1
  reason="$(printf 'has %s assistant turns (telemetry unavailable, using transcript size)' "$msg_count")"
fi

[ "$triggered" -eq 0 ] && exit 0

# --- write the handoff doc (best-effort) ------------------------------------
mkdir -p "$HANDOFF_DIR" 2>/dev/null || exit 0
doc="$HANDOFF_DIR/$session_id.md"
{
  echo "# Session handoff — $session_id"
  echo
  echo "This session $reason. Its growing prefix is re-read every turn (cache-read"
  echo "cost scales ~O(turns^2)). Starting a fresh session caps that growth."
  echo
  echo "## To continue in a fresh session"
  echo "1. Skim the last few exchanges below for open threads."
  echo "2. Start a new Claude Code session and paste the summary you need."
  echo
  echo "## Recent context (transcript tail)"
  if [ -n "$transcript" ] && [ -f "$transcript" ]; then
    TRANSCRIPT="$transcript" python3 - <<'PY' 2>/dev/null || true
import json, os
path = os.environ["TRANSCRIPT"]
rows = []
try:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            m = d.get("message")
            role = m.get("role") if isinstance(m, dict) else None
            if role in ("user", "assistant"):
                content = m.get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                text = " ".join(text.split())[:200]
                if text:
                    rows.append((role, text))
except FileNotFoundError:
    pass
for role, text in rows[-6:]:
    print(f"- **{role}**: {text}")
PY
  fi
} > "$doc" 2>/dev/null || exit 0

# --- nudge (advisory, non-blocking) -----------------------------------------
# Emit BEFORE stamping so a jq failure can't both swallow the output and leave a
# stamp that suppresses every future retry (Codex xval #3). The `|| true` keeps
# `set -e` from turning a broken-pipe/jq error into a nonzero hook exit.
jq -n --arg d "$doc" --arg r "$reason" \
  '{hookSpecificOutput: {hookEventName: "Stop",
    additionalContext: ("This session " + $r + ". Prefix re-read cost grows with session length — consider starting a fresh session. Handoff written to " + $d)}}' \
  && touch "$STAMP" 2>/dev/null || true
exit 0
