#!/usr/bin/env bash
# git POST-COMMIT hook — auto-enqueue the just-committed diff to the local review lane.
#
# Fire-and-forget: the drain worker reviews the diff off the critical path and its findings escalate
# for frontier triage. This hook NEVER blocks or fails a commit — every path exits 0, enqueue is
# best-effort.
#
# Install per repo:   apex-router install-hooks <repo> [<repo> ...]
#   (or by hand:      ln -sf "$(apex-router --hook-path)" <repo>/.git/hooks/post-commit)
# Disable ad hoc:     ORNITH_REVIEW_ENQUEUE=0
set -u

[ "${ORNITH_REVIEW_ENQUEUE:-1}" = "1" ] || exit 0

# Find the installed apex-router (its venv carries queue_task). Prefer an explicit
# APEX_ROUTER_PY, else the venv beside a discoverable apex-router on PATH, else `python3 -m`.
if [ -n "${APEX_ROUTER_PY:-}" ] && [ -x "${APEX_ROUTER_PY}" ]; then
  PY="$APEX_ROUTER_PY"
elif command -v apex-router >/dev/null 2>&1; then
  # apex-router is a console-script in the install venv's bin; the python is its sibling.
  _bin="$(command -v apex-router)"; PY="$(dirname "$_bin")/python"
  [ -x "$PY" ] || PY="python3"
else
  PY="python3"
fi

# Queue location — same resolver the daemon uses (default ~/.apex-router/queue).
export APEX_ORNITH_QUEUE="${APEX_ORNITH_QUEUE:-$HOME/.apex-router/queue}"

# The diff of the commit that just landed. Skip merges (huge, low signal) and empty diffs.
if git rev-parse -q --verify HEAD^ >/dev/null 2>&1; then
  diff="$(git diff HEAD^ HEAD 2>/dev/null)"
else
  diff="$(git show --format= HEAD 2>/dev/null)"   # root commit
fi
[ -n "$diff" ] || exit 0

# Size guard: the review lane's fit envelope is ~100 KB/item. Skip oversized diffs (bulk refactors)
# rather than truncate — a partial diff yields misleading findings.
bytes=$(printf '%s' "$diff" | wc -c | tr -d ' ')
if [ "$bytes" -gt 100000 ]; then
  exit 0
fi

tmp="$(mktemp -t ornith-review.XXXXXX)" || exit 0
printf '%s' "$diff" >"$tmp"

# Enqueue in the background so the hook returns instantly; never surface errors to the commit.
( "$PY" -m apex_router.ornith.queue_task --lane review --diff-file "$tmp" --max-tokens 700 >/dev/null 2>&1
  rm -f "$tmp" ) &

exit 0
