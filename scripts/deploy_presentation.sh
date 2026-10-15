#!/bin/bash
# Publish the apex-router + skills page next to the triage explorer: http://10.2.27.132:8090/apex-router.html
# Writes ONLY ~/triage_site/apex-router.html on the box (via a .new file + mv). index.html (triage explorer) is never
# touched: its served hash is checked before and after, and the deploy fails if it changed.
#   scripts/deploy_presentation.sh [page.html]     (default: build a fresh page)
set -euo pipefail
HOST=10.2.27.132; URL="http://$HOST:8090"; NAME=apex-router.html
cd "$(dirname "$0")/.."
PAGE="${1:-}"
if [ -z "$PAGE" ]; then
  .venv/bin/python scripts/presentation_data.py --out /tmp/pres.json < /dev/null
  .venv/bin/python scripts/presentation_page.py /tmp/pres.json > /tmp/$NAME
  PAGE=/tmp/$NAME
fi
[ -s "$PAGE" ] || { echo "no page: $PAGE"; exit 1; }
grep -q '\$[0-9]' "$PAGE" && { echo "refusing: page contains a dollar figure"; exit 1; }
h() { curl -fsS --connect-timeout 10 -m 60 "$1" < /dev/null | shasum -a 256 | cut -c1-16; }
IDX0=$(h "$URL/")
# ship the page inside a small script (pce_ssh.sh -f handles the password and the scp); it writes .new, then mv
X=$(mktemp /tmp/apex_pres_push.XXXXXX.sh)
{ echo 'set -e; cd ~/triage_site'; echo "base64 -d > $NAME.new <<'B64'"; base64 < "$PAGE"; echo 'B64'
  echo "mv $NAME.new $NAME && ls -la ~/triage_site/ | grep html"; } > "$X"
"$HOME/.claude/skills/avenger-ssh-troubleshooting/scripts/pce_ssh.sh" vagrant@$HOST -f "$X" < /dev/null 2>&1 | grep -E "\.html|DONE" || true
rm -f "$X"
IDX1=$(h "$URL/")
A=$(shasum -a 256 "$PAGE" | cut -c1-16); B=$(h "$URL/$NAME")
[ "$IDX0" = "$IDX1" ] || { echo "WARNING: index.html changed during deploy ($IDX0 -> $IDX1)"; exit 2; }
[ "$A" = "$B" ] && echo "deployed: $URL/$NAME ($A); index.html unchanged ($IDX1)" || { echo "MISMATCH served $B != built $A"; exit 1; }
