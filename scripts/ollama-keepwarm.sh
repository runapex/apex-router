#!/usr/bin/env bash
# Keep the active local model resident in ollama so bursty pi/codex turns never hit a multi-minute
# cold-load stall. Root cause (telemetry-confirmed): an idle gap lets ollama unload a large local
# model (default keep_alive 5m); the next turn then blocks MINUTES on the reload, which reads as a
# "hang until cancel/retry" — slow local TTFT correlated with the idle gap (median ~144s), NOT with
# request concurrency. This agent re-applies a per-request keep_alive:-1 pin on a schedule.
#
# Why a scheduled pin and not OLLAMA_KEEP_ALIVE in ollama's launchd plist: Homebrew regenerates that
# plist from its signed JSON API cache on every `brew services` start/restart and strips any
# hand-added env var. A per-request keep_alive:-1 is the documented, brew-proof pin; re-applying it
# every few minutes (under the 5m default) survives ollama restarts too.
#
# Config (install.sh bakes APEX_KEEPWARM_MODEL into the launchd plist; all env-overridable):
#   APEX_KEEPWARM_MODEL  the ollama model tag to pin (REQUIRED; unset => quiet no-op, never guesses)
#   APEX_KEEPWARM_URL    ollama base URL (default http://127.0.0.1:11434)
set -euo pipefail

MODEL="${APEX_KEEPWARM_MODEL:-}"
URL="${APEX_KEEPWARM_URL:-http://127.0.0.1:11434}"

# No model configured => nothing to pin. Silent success (never a machine-specific default baked in
# the public repo; the installer supplies the resolved model for THIS machine).
[ -n "$MODEL" ] || exit 0

# A zero-token generate with keep_alive:-1 loads (if needed) and pins the model. Fail-open: any error
# (ollama down, model missing, network blip) is swallowed — a keepalive must never spew or wedge.
curl -fsS -m 600 "${URL}/api/generate" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"${MODEL}\",\"prompt\":\"\",\"keep_alive\":-1,\"stream\":false,\"options\":{\"num_predict\":0}}" \
  >/dev/null 2>&1 || true
