#!/usr/bin/env bash
# Run the pytest suite with a throwaway HOME, so the isolation guard (tests/conftest.py) watches a home that
# no live process writes to. Why (2026-10-26/27): on a dev Mac, live pi jobs, xval, mesh-up and pi agents
# write ~/.apex-router during a 6-minute run; the guard compares mtimes, so it failed (rc=1) with every test
# green. A process heuristic cannot prove which process wrote a file (xval NO-GO twice); a separate HOME can.
# The guard is unchanged (same rules, same exclusions: logs/, queue/, append-only files) on the throwaway home.
# Limit: code that finds the real home without $HOME (pwd.getpwuid, a hard-coded /Users/<name>) is not
# redirected. Plain `uv run pytest` keeps the guard on the real home (CI, or a quiet machine).
# Usage: scripts/test.sh [pytest args...]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REAL_HOME="$HOME"
FAKE="$(mktemp -d "${TMPDIR:-/tmp}/apex-test-real-home.XXXXXX")"
trap 'rm -rf "$FAKE"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$FAKE/.apex-router" "$FAKE/.apex" "$FAKE/.claude"
# keep tools that live under the real home working: uv cache + python installs, git identity
export UV_CACHE_DIR="${UV_CACHE_DIR:-$REAL_HOME/.cache/uv}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$REAL_HOME/.local/share/uv/python}"
export GIT_CONFIG_GLOBAL="${GIT_CONFIG_GLOBAL:-$REAL_HOME/.gitconfig}"
cd "$ROOT"
HOME="$FAKE" _APEX_REAL_HOME="$FAKE" "$ROOT/.venv/bin/python" -m pytest "$@"
