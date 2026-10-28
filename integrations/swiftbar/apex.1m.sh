#!/usr/bin/env bash
# <swiftbar.title>apex-router</swiftbar.title>
# <swiftbar.version>v1</swiftbar.version>
# <swiftbar.author>apex-router</swiftbar.author>
# <swiftbar.desc>Upstream pressure, running agents and limit meters from apex-router; click a row for a detail page.</swiftbar.desc>
# <swiftbar.dependencies>apex-router</swiftbar.dependencies>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>false</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
#
# Refreshes every minute (the ".1m." in the file name). Prints whatever
# `apex-router snapshot --menubar` prints; a gray dot when the binary is missing or fails.
# Each run appends one bounded sample to ~/.apex-router/widget/history.jsonl (the sparklines);
# add --no-history below to turn that off.
# --alert: a NEW problem (not seen in 30 min) sends one macOS notification, at most one in 10 min;
# state in ~/.apex-router/widget/alerts.json. Set APEX_ROUTER_NO_ALERTS=1 to turn it off.

gray() {
  echo "● | color=#8E8E93"
  echo "---"
  echo "$1"
  echo "---"
  echo "Refresh | refresh=true"
}

bin="$HOME/.local/bin/apex-router"
if [ ! -x "$bin" ]; then
  bin="$(command -v apex-router 2>/dev/null || true)"
fi
if [ -z "$bin" ] || [ ! -x "$bin" ]; then
  gray "apex-router missing"
  exit 0
fi

# Rows click through to `apex-router snapshot --detail <id>` with this exact binary; the
# formatter accepts it only as a plain absolute path.
export APEX_ROUTER_BIN="$bin"

if ! out="$("$bin" snapshot --menubar --alert 2>/dev/null)" || [ -z "$out" ]; then
  gray "apex-router snapshot failed"
  exit 0
fi
printf '%s\n' "$out"
