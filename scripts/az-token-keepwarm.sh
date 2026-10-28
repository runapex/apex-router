#!/usr/bin/env bash
# Keep the az-token-cached token warm, so a pi request never runs the slow network refresh.
# Why (incident 2026-10-26 15:44, job 20261026221508-v6kn): az (MSAL) gets a new token only in the last
# 300 s. After an idle gap (no pi request in that window) the next request found < 60 s left, ran the
# network refresh inside its 8 s deadline, the deadline stopped az, and pi failed with
# "Failed to resolve API key".
# This agent runs every 60 s. When less than 240 s are left it refreshes synchronously (rule 3 of
# az-token-cached), 30 s deadline per try, up to 3 tries (5 s, 10 s apart). A failed tick is retried at
# once, not 60 s later. A pi call meanwhile still gets the cached token while > 60 s are left.
# Known limit: after Mac sleep longer than the token life, the first pi request can still race the
# first keep-warm run (launchd starts it at wake). Only pi itself can wait longer than 10 s.
# Usage:
#   az-token-keepwarm.sh install     copy az-token-cached and this script to ~/.local/bin; write + load the agent
#   az-token-keepwarm.sh uninstall   unload and delete the agent (the scripts in ~/.local/bin stay)
#   az-token-keepwarm.sh run         one keep-warm tick (what launchd runs)
# Env: AZ_KEEPWARM_RESOURCE (default https://cognitiveservices.azure.com), AZ_KEEPWARM_NO_LOAD=1 (write the
# plist only; for tests), AZ_KEEPWARM_TRIES (default 3). macOS only (launchd).
set -euo pipefail

LABEL=com.apex.az-token-keepwarm
HERE="$(cd "$(dirname "$0")" && pwd)"
BINDIR="$HOME/.local/bin"
BIN="$BINDIR/az-token-cached"
SELF="$BINDIR/az-token-keepwarm"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/$LABEL.plist"
LOG="$HOME/.apex-router/logs/$LABEL.log"
RES="${AZ_KEEPWARM_RESOURCE:-https://cognitiveservices.azure.com}"
UID_="$(id -u)"

load() { [ "${AZ_KEEPWARM_NO_LOAD:-0}" = 1 ] || launchctl "$@"; }

# power_state: dark-wake | awake | unknown, from the newest Sleep/Wake/DarkWake line of `pmset -g log`.
# AZ_KEEPWARM_PMSET replaces the pmset command (tests). Any error gives "unknown", never a wrong word.
power_state() {
  local last
  # the whole log (about 17k lines, < 1 s, only on a failure): a Wake can be hours of other lines back
  # bounded: perl alarm stops the command after 5 s (a stalled pmset must not hold the failure line)
  local cmd
  read -r -a cmd <<< "${AZ_KEEPWARM_PMSET:-pmset -g log}"      # words only: no shell runs the value
  last=$(perl -e 'alarm 5; exec { $ARGV[0] } @ARGV' "${cmd[@]}" 2>/dev/null \
         | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8} [+-][0-9]{4} (Sleep|Wake|DarkWake) ' | tail -n 1 \
         | awk '{print $4}') || true
  case "$last" in
    DarkWake) echo dark-wake ;;
    Wake) echo awake ;;
    *) echo unknown ;;
  esac
}

case "${1:-}" in
  run)
    # stdout of az-token-cached is the token: never print it. One stderr line only after the last try.
    tries="${AZ_KEEPWARM_TRIES:-3}"; i=1
    while :; do
      AZ_TOKEN_MIN_S=240 AZ_TOKEN_REFRESH_S=240 AZ_TOKEN_DEADLINE_S=30 "$BIN" "$RES" >/dev/null 2>&1 && exit 0
      [ "$i" -ge "$tries" ] && break
      sleep $(( i * 5 )); i=$((i + 1))
    done
    # D3 (2026-10-28): 40 of 43 such lines came in a DarkWake (the Mac wakes for maintenance, often with no
    # network). The line names the power state, so a real failure (awake) is not lost in that noise.
    # The state is the last Sleep/Wake/DarkWake line of `pmset -g log` (read only on a failure).
    state=$(power_state)
    echo "$(date '+%Y-%m-%dT%H:%M:%S%z') az-token-keepwarm: refresh failed after $tries tries (power: $state)" >&2
    exit 1
    ;;
  install)
    [ "$(uname -s)" = Darwin ] || { echo "az-token-keepwarm: macOS only (launchd)" >&2; exit 2; }
    AZ="$(command -v az || true)"
    [ -n "$AZ" ] || { echo "az-token-keepwarm: az not found on PATH" >&2; exit 2; }
    mkdir -p "$BINDIR" "$AGENTS" "$(dirname "$LOG")"
    # the agent needs AZ_TOKEN_MIN_S support: install the repo copy, never keep an old one
    install -m 0755 "$HERE/az-token-cached" "$BIN"
    install -m 0755 "$HERE/az-token-keepwarm.sh" "$SELF"
    # Same cache as pi: pi resolves XDG_CACHE_HOME from its own env; bake the value seen at install.
    CACHE="${XDG_CACHE_HOME:-$HOME/.cache}"
    # plistlib does the XML escaping; write a temp file, lint it, then rename (no half-written plist).
    python3 - "$PLIST.tmp" "$LABEL" "$SELF" "$RES" "$AZ" "$CACHE" "$LOG" <<'PY'
import plistlib, sys
out, label, self_, res, az, cache, log = sys.argv[1:]
d = {"Label": label, "ProgramArguments": ["/bin/bash", self_, "run"],
     "EnvironmentVariables": {"AZ_BIN": az, "AZ_KEEPWARM_RESOURCE": res, "XDG_CACHE_HOME": cache,
                              "PATH": az.rsplit("/", 1)[0] + ":/usr/bin:/bin:/usr/sbin:/sbin"},
     "RunAtLoad": True, "StartInterval": 60, "ProcessType": "Background",
     "StandardOutPath": "/dev/null", "StandardErrorPath": log}
with open(out, "wb") as f:
    plistlib.dump(d, f)
PY
    plutil -lint "$PLIST.tmp" >/dev/null || { rm -f "$PLIST.tmp"; echo "az-token-keepwarm: bad plist" >&2; exit 1; }
    mv -f "$PLIST.tmp" "$PLIST"
    load bootout "gui/$UID_/$LABEL" >/dev/null 2>&1 || true
    load bootstrap "gui/$UID_" "$PLIST"
    echo "az-token-keepwarm: installed $BIN, $SELF and $PLIST (every 60 s, refresh below 240 s)"
    ;;
  uninstall)
    load bootout "gui/$UID_/$LABEL" >/dev/null 2>&1 || true
    rm -f "$PLIST"
    echo "az-token-keepwarm: removed $PLIST"
    ;;
  *) echo "usage: $0 install|uninstall|run" >&2; exit 2 ;;
esac
