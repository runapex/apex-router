#!/usr/bin/env bash
# memory-compact-nudge hook — Stop matcher
# When a Claude Code project's memory index (MEMORY.md) has grown large enough
# that it's an expensive per-session prefix, run the memory_compact engine to
# produce a PROPOSED compacted index, write it beside the handoff docs, and nudge
# the user to review + apply it. Advisory ONLY — never blocks, never mutates the
# memory dir (the mutating path is `memory_compact.py --apply`, human-run).
#
# Mirrors cache-handoff-nudge.sh: same Stop contract, same guards, same
# aggressive-start policy. jq + python3, no LLM call. Silent on any error.
set -euo pipefail

command -v jq >/dev/null 2>&1 || exit 0

input="$(cat)"
session_id="$(printf '%s' "$input" | jq -r '.session_id // empty')"
transcript="$(printf '%s' "$input" | jq -r '.transcript_path // empty')"
stop_active="$(printf '%s' "$input" | jq -r '.stop_hook_active // false')"

[ "$stop_active" = "true" ] && exit 0
[ -z "$session_id" ] && exit 0
# session_id feeds filenames — never trust it as a path (UUID shape only).
case "$session_id" in
  *[!A-Za-z0-9._-]* | *..* | .* ) exit 0 ;;
esac
[ -n "$transcript" ] || exit 0

# The memory dir sits beside the transcript: ~/.claude/projects/<slug>/memory/.
# Derive it from the transcript path rather than guessing the slug. Normalize with
# `cd -P` so a messy path (symlinks, ..) can't leak into the nudge or file writes.
proj_dir="$(cd -P "$(dirname "$transcript")" 2>/dev/null && pwd -P)" || exit 0
[ -n "$proj_dir" ] || exit 0
mem_dir="$proj_dir/memory"
[ -d "$mem_dir" ] || exit 0
index="$mem_dir/MEMORY.md"
[ -f "$index" ] || exit 0

# --- tunables (env-overridable) --------------------------------------------
# Policy: START AGGRESSIVE (nudge early), relax per project if premature.
INDEX_BYTES_THRESHOLD="${MEMORY_COMPACT_INDEX_BYTES:-8192}"    # ~8KB index
FILE_COUNT_THRESHOLD="${MEMORY_COMPACT_FILE_COUNT:-50}"        # or >=50 memory files
HANDOFF_DIR="${CACHE_HANDOFF_DIR:-$HOME/.claude/handoffs}"
# Resolve the engine location-independently — apex-router may be installed ANYWHERE
# (not just under $HOME). Resolution order:
#   1. MEMORY_COMPACT_ENGINE override
#   2. an install-root recorded by install.sh (~/.config/apex-router/install_dir)
#      or the APEX_ROUTER_DIR env — the authoritative pointer, wherever it lives
#   3. common install/checkout locations as a best-effort fallback
#   4. an engine already on PATH
ENGINE=""
if [ -n "${MEMORY_COMPACT_ENGINE:-}" ]; then
  ENGINE="$MEMORY_COMPACT_ENGINE"
else
  _roots=""
  [ -n "${APEX_ROUTER_DIR:-}" ] && _roots="$APEX_ROUTER_DIR"
  _cfg="${XDG_CONFIG_HOME:-$HOME/.config}/apex-router/install_dir"
  [ -f "$_cfg" ] && _roots="$_roots
$(cat "$_cfg" 2>/dev/null)"
  _roots="$_roots
$HOME/.apex-router
$HOME/dev/apex-router
$HOME/src/apex-router
/opt/apex-router
/usr/local/apex-router"
  while IFS= read -r _r; do
    [ -n "$_r" ] || continue
    if [ -f "$_r/scripts/memory_compact.py" ]; then ENGINE="$_r/scripts/memory_compact.py"; break; fi
  done <<EOF
$_roots
EOF
  # last resort: an engine on PATH
  [ -n "$ENGINE" ] || ENGINE="$(command -v memory_compact.py 2>/dev/null || true)"
fi

# Pick a python: prefer the venv that sits NEXT TO the resolved engine (works
# wherever the install lives), then an override, then a system python.
PYTHON=""
if [ -n "${MEMORY_COMPACT_PYTHON:-}" ]; then
  PYTHON="$MEMORY_COMPACT_PYTHON"
elif [ -n "$ENGINE" ] && [ -x "$(dirname "$(dirname "$ENGINE")")/.venv/bin/python" ]; then
  PYTHON="$(dirname "$(dirname "$ENGINE")")/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="python3"
else
  PYTHON="python"
fi
STAMP="$HANDOFF_DIR/.mem-nudged-$session_id"

[ -f "$STAMP" ] && exit 0

index_bytes="$(wc -c < "$index" 2>/dev/null | tr -d ' ')"
file_count="$(find "$mem_dir" -maxdepth 1 -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
: "${index_bytes:=0}" "${file_count:=0}"

triggered=0
if [ "$index_bytes" -ge "$INDEX_BYTES_THRESHOLD" ] || [ "$file_count" -ge "$FILE_COUNT_THRESHOLD" ]; then
  triggered=1
fi
[ "$triggered" -eq 0 ] && exit 0

mkdir -p "$HANDOFF_DIR" 2>/dev/null || exit 0
slug="$(basename "$proj_dir")"
proposed="$HANDOFF_DIR/memory-$slug.md"

# Produce the PROPOSED compacted index AND the size summary in ONE engine call
# (read-only; --write-proposed never touches the live index; --json returns sizes).
saved="" ; have_proposed=0
if [ -f "$ENGINE" ]; then
  saved="$("$PYTHON" "$ENGINE" --dir "$mem_dir" --write-proposed "$proposed" --json 2>/dev/null \
    | jq -r '"index \(.current_index_bytes)B -> \(.proposed_index_bytes)B; \(.archived_files) archivable"' 2>/dev/null || true)"
  [ -s "$proposed" ] && have_proposed=1   # only claim the file if it truly exists
fi
[ -n "$saved" ] || saved="index ${index_bytes}B, ${file_count} files"

touch "$STAMP" 2>/dev/null || true

# Only reference the proposed file if it was actually written; otherwise nudge
# with the apply command alone (never claim an artifact that isn't there).
# Advertise the SAME python we resolved for our own run — this box may have only
# `python3` (bare `python` => command-not-found). $PYTHON is always set above.
if [ "$have_proposed" -eq 1 ]; then
  msg="Project memory is a growing per-session prefix ($saved). Proposed compaction written to $proposed — review it, then apply with: $PYTHON $ENGINE --dir $mem_dir --apply  (auto-creates a reversible git checkpoint, advisory)."
else
  msg="Project memory is a growing per-session prefix ($saved). Review + compact with: $PYTHON $ENGINE --dir $mem_dir  (add --apply to archive cold files; auto-creates a reversible git checkpoint, advisory)."
fi
jq -n --arg m "$msg" \
  '{hookSpecificOutput: {hookEventName: "Stop", additionalContext: $m}}' 2>/dev/null || true
exit 0
