#!/usr/bin/env bash
# <swiftbar.title>apex-router</swiftbar.title>
# <swiftbar.version>v1</swiftbar.version>
# <swiftbar.author>apex-router</swiftbar.author>
# <swiftbar.desc>Upstream pressure, running agents and limit meters from apex-router (read-only).</swiftbar.desc>
# <swiftbar.dependencies>apex-router</swiftbar.dependencies>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>false</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>false</swiftbar.hideSwiftBar>
#
# Refreshes every minute (the ".1m." in the file name). Prints whatever
# `apex-router snapshot --menubar` prints; a gray dot when the binary is missing or fails.

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

if ! out="$("$bin" snapshot --menubar 2>/dev/null)" || [ -z "$out" ]; then
  gray "apex-router snapshot failed"
  exit 0
fi
printf '%s\n' "$out"
