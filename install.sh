#!/usr/bin/env bash
# apex-router one-command installer.
#
#   curl -fsSL https://raw.githubusercontent.com/<owner>/apex-router/main/install.sh | bash
#
# Provisions the full stack, arch-aware and IDEMPOTENT (anything already present is
# skipped). No model gateway is required anywhere — the target uses its own Claude + Codex
# subscriptions; the known-models gate keeps route tables from pointing at models this
# machine can't run.
#
# Tiers:
#   - apex-router package  : ALWAYS (pure Python stdlib core; routing needs no services)
#   - ollama + nomic-embed : embedding-refinement classifier (optional; skipped on failure)
#   - Ornith 1.5 tiers     : local replay bench / codegen, served by the SAME ollama. Two sizes
#                            (small 9B, large 35B-A3B) switchable via `apex-router ornith-tier`.
#                            No longer Apple-Silicon-only: the MLX server it used to need is
#                            retired, so this now works anywhere ollama does.
#
# Flags:  --no-ornith   skip the local Ornith model pulls
#         --ornith-tier N  which tier to pull+activate: small (~5.6GB, default) | large (~21GB) | both
#         --ornith-serve  (macOS) install the Ornith stack as always-on launchd agents
#                         (queue worker + nightly cycle), not just the model pull
#         --no-embed    skip ollama / nomic-embed
#         --watch       install the background watchers (drain worker + daily report)
#         --proxy       install the measuring proxy ([proxy] extra: starlette/uvicorn/…)
#         --install-hooks "R1 R2"  install the review post-commit hook into these git repos
#         --cache-handoff-hook  wire the cache-cost session-handoff Stop hook into ~/.claude/settings.json
#         --memory-compact-hook  wire the project-memory compaction Stop hook into ~/.claude/settings.json
#         --pi-integration  install the pi per-task router extension + models.json wiring (needs `pi`)
#         --books-index  install the local booksearch tool ([books] extra + wrapper + pi/claude commands)
#         --proxy-config F  wire Claude Code through a proxy via ~/.claude/settings.json
#         --skills-marketplace URL  add another Claude Code skill marketplace (repeatable). The public
#                                   apex-router-skills marketplace is added by default.
#         --no-skills   skip the default public skill marketplace (also APEX_NO_SKILLS=1)
#         --dir PATH    install location (default: ~/.apex-router)
#         --repo URL    git repo to clone (default: the public apex-router repo)
#         --verify-only re-run the self-check against an existing install
#         --skills-only (re)wire Claude Code skill marketplaces only (update path for an existing install)
set -euo pipefail

# --------------------------------------------------------------------------- #
# config + arg parsing
# --------------------------------------------------------------------------- #
REPO_URL_DEFAULT="https://github.com/runapex/apex-router.git"
INSTALL_DIR="${APEX_ROUTER_DIR:-$HOME/.apex-router}"
REPO_URL="$REPO_URL_DEFAULT"
DO_ORNITH=1
DO_ORNITH_SERVE=0
# Set to 1 ONLY when install_ornith_service actually installs the launchd worker (macOS + not
# short-circuited). install_watchers gates --no-drain on THIS, not on the requested --ornith-serve:
# on Linux/Intel-mac / --no-ornith / a bootstrap that never ran, no worker exists, so the watcher
# drainer must stay or the queue is never drained (Codex xval P1).
ORNITH_WORKER_INSTALLED=0
DO_EMBED=1
DO_WATCH=0
DO_PROXY=0
DO_CACHE_HANDOFF=0   # --cache-handoff-hook: wire the cache-cost session-handoff Stop hook
DO_MEMORY_COMPACT=0  # --memory-compact-hook: wire the project-memory compaction Stop hook
DO_PI=0              # --pi-integration: install the pi per-task router extension + models.json wiring
DO_BOOKS=0           # --books-index: install the local booksearch tool + pi/claude commands
VERIFY_ONLY=0
SKILLS_ONLY=0   # --skills-only: just (re)wire Claude Code skill marketplaces on an existing install
NL='
'               # a literal newline — the internal separator for the repeatable --skills-marketplace
# Ornith 1.5 tiers, served by ollama. There is NO 27B — upstream ships 9B / 35B-A3B / 397B, and
# 397B does not fit a single workstation. Q4_K_M is the quality/size knee and the only quant
# present in both GGUF repos. Keep these in sync with src/apex_router/ornith/local_tier.py.
ORNITH_MODEL_SMALL="hf.co/ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M"
ORNITH_MODEL_LARGE="hf.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M"
ORNITH_TIER="small"   # --ornith-tier small|large|both
# Repos to install the review post-commit hook into (space-separated; user-supplied, none hardcoded).
HOOK_REPOS="${APEX_HOOK_REPOS:-}"
# Skill marketplaces (Claude Code plugin repos), wired via the `claude plugin` CLI.
# apex-router ships ONE public marketplace by DEFAULT (workflow-discipline skills), and supports
# ADDING MORE: --skills-marketplace can be passed repeatedly, and APEX_SKILLS_MARKETPLACE may hold a
# space-separated list. The default plugin (apex-workflow) is installed from the public marketplace;
# extra marketplaces are added and their plugins are left for the user to `claude plugin install`.
# --no-skills / APEX_NO_SKILLS=1 opts out of the default public marketplace entirely.
APEX_PUBLIC_MARKETPLACE="runapex/apex-router-skills"   # public default (github owner/repo)
APEX_DEFAULT_PLUGIN="apex-workflow@apex-router-skills" # plugin@marketplace to install by default
# Extra marketplaces, stored NEWLINE-separated internally so a source path with spaces stays intact.
# The env var (back-compat) is space-separated; normalize it to newlines up front. The repeatable
# --skills-marketplace flag appends newline-separated (so a quoted spacey path is preserved verbatim).
SKILLS_MARKETPLACES="$(printf '%s' "${APEX_SKILLS_MARKETPLACE:-}" | tr ' ' '\n')"
DO_SKILLS="${APEX_NO_SKILLS:+0}"; DO_SKILLS="${DO_SKILLS:-1}"  # 1 = install default public marketplace
# Proxy client wiring merged into ~/.claude/settings.json. Values come from a --proxy-config
# file or your environment — NOTHING is hardcoded here. Empty = skip (routing still installs).
PROXY_CONFIG="${APEX_PROXY_CONFIG:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-ornith) DO_ORNITH=0 ;;
    --ornith-tier) ORNITH_TIER="${2:-small}"; shift ;;
    --ornith-serve) DO_ORNITH_SERVE=1 ;;   # macOS: install the Ornith stack as launchd agents
    --no-embed)  DO_EMBED=0 ;;
    --watch)     DO_WATCH=1 ;;
    --proxy)     DO_PROXY=1 ;;
    --cache-handoff-hook) DO_CACHE_HANDOFF=1 ;;   # wire the cache-cost Stop hook into settings.json
    --memory-compact-hook) DO_MEMORY_COMPACT=1 ;; # wire the project-memory compaction Stop hook
    --pi-integration) DO_PI=1 ;;                   # install the pi per-task router + models.json wiring
    --books-index) DO_BOOKS=1 ;;                   # install the local booksearch tool + pi/claude commands
    --install-hooks) HOOK_REPOS="$2"; shift ;;
    # Accumulate NEWLINE-separated (not space) so a local marketplace path containing spaces stays
    # one argument through the consumption loop (Codex pass-2). The env-var form stays space-separated
    # for back-compat and is normalized to newlines before the loop. NL holds a literal newline
    # (command substitution would strip a trailing one, collapsing the separator).
    --skills-marketplace) SKILLS_MARKETPLACES="${SKILLS_MARKETPLACES:+$SKILLS_MARKETPLACES$NL}$2"; shift ;;
    --no-skills) DO_SKILLS=0 ;;
    --proxy-config) PROXY_CONFIG="$2"; shift ;;
    --dir)       INSTALL_DIR="$2"; shift ;;
    --repo)      REPO_URL="$2"; shift ;;
    --verify-only) VERIFY_ONLY=1 ;;
    --skills-only) SKILLS_ONLY=1 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -30; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

OS="$(uname -s)"
ARCH="$(uname -m)"
IS_APPLE_SILICON=0
[ "$OS" = "Darwin" ] && [ "$ARCH" = "arm64" ] && IS_APPLE_SILICON=1

# --------------------------------------------------------------------------- #
# 1. prerequisites: git, python >=3.11, uv
# --------------------------------------------------------------------------- #
ensure_prereqs() {
  have git || die "git is required but not installed."

  if ! have python3; then
    if [ "$OS" = "Darwin" ] && have brew; then say "installing python3 via brew"; brew install python@3.12;
    elif have apt-get; then say "installing python3 via apt"; sudo apt-get update -qq && sudo apt-get install -y python3 python3-venv;
    else die "python3 is required (>=3.11). Please install it and re-run."; fi
  fi

  # uv is our installer/runtime (fast, self-contained venvs).
  if ! have uv; then
    say "installing uv (astral.sh)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv installs to ~/.local/bin or ~/.cargo/bin — make it visible for this run.
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    have uv || die "uv installed but not on PATH; add ~/.local/bin to PATH and re-run."
  fi
  ok "prerequisites: git, python3, uv"
}

# --------------------------------------------------------------------------- #
# 2. the apex-router package (pure stdlib core — this alone makes routing work)
# --------------------------------------------------------------------------- #
install_package() {
  if [ -d "$INSTALL_DIR/.git" ]; then
    say "updating existing checkout at $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only || warn "git pull failed; using existing checkout"
  else
    say "cloning $REPO_URL -> $INSTALL_DIR"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi

  # Record where apex-router lives so hooks/tools can find the engine wherever it
  # was installed (not just under $HOME) — the memory-compact hook reads this.
  local _cfgdir="${XDG_CONFIG_HOME:-$HOME/.config}/apex-router"
  mkdir -p "$_cfgdir" 2>/dev/null && printf '%s\n' "$INSTALL_DIR" > "$_cfgdir/install_dir" 2>/dev/null \
    && ok "recorded install dir → $_cfgdir/install_dir"

  say "creating venv + installing the package"
  # Core is dependency-free; add extras only for the tiers the user opted into.
  local extras="dev"
  # The [ornith] extra is mlx-lm, needed only by the RETIRED MLX server. Local inference is ollama
  # now (an external binary, not a Python dep) and nothing under src/ imports mlx_lm, so a fresh
  # install no longer pulls it. The extra stays declared in pyproject for the legacy replay path.
  # The measuring proxy pulls starlette/uvicorn/httpx/brotli/numpy (+scipy/tiktoken for the tuner);
  # only with --proxy so a plain install stays lean.
  [ "$DO_PROXY" = "1" ] && extras="$extras,proxy,tuner"

  # Idempotent + self-healing. Re-running the installer (e.g. to adopt --ornith-serve on an
  # existing install) must NOT abort on uv's "venv already exists" guard, so don't recreate a
  # usable venv up front — reuse it for a fast reinstall. Create one only when missing/broken
  # (--clear also wipes a present-but-broken dir). If the reinstall then fails because the
  # existing venv is incompatible (e.g. its Python predates pyproject's requires-python), rebuild
  # once with --clear and retry — react to the actual failure instead of guessing the version.
  [ -x "$INSTALL_DIR/.venv/bin/python" ] || uv venv --clear "$INSTALL_DIR/.venv" >/dev/null
  if ! uv pip install --python "$INSTALL_DIR/.venv/bin/python" -e "$INSTALL_DIR[$extras]" >/dev/null 2>&1; then
    warn "existing venv unusable for this build — rebuilding it"
    uv venv --clear "$INSTALL_DIR/.venv" >/dev/null
    uv pip install --python "$INSTALL_DIR/.venv/bin/python" -e "$INSTALL_DIR[$extras]" >/dev/null
  fi
  ok "apex-router package installed (extras: $extras)"

  # Expose the `apex-router` CLI on PATH (~/.local/bin) via a uv tool install, so bare
  # `apex-router …` works from any shell — the model-routing skill's `route-log`/`route-readout`
  # assume it's on PATH, not just inside the venv.
  #
  # Install the tool WITH THE SAME EXTRAS the user chose (not the bare core), so the PATH
  # binary is a SUPERSET of the venv one — otherwise a `--proxy` user's bare `apex-router serve`
  # would resolve to a lean tool env missing starlette/uvicorn and report "needs the [proxy]
  # extra" despite having asked for it (Codex #1). `--force` reinstalls in place (idempotent);
  # it re-points any existing `apex-router` tool at THIS install, which is intended — the
  # installer is the source of truth for the command.
  # Best-effort: a failure here doesn't fail the install (the venv binary still works).
  if uv tool install --force "$INSTALL_DIR[$extras]" >/dev/null 2>&1; then
    tool_bin="$(command -v apex-router 2>/dev/null || true)"
    if [ -n "$tool_bin" ]; then
      ok "apex-router on PATH: $tool_bin"
    else
      warn "apex-router installed as a uv tool but its bin dir is not on PATH — run"
      warn "  'uv tool update-shell' (or add uv's tool-bin dir to PATH) so bare 'apex-router' resolves"
    fi
  else
    warn "could not put apex-router on PATH via uv tool — the venv binary still works at"
    warn "  $INSTALL_DIR/.venv/bin/apex-router ; add it to PATH or alias it so the skill's"
    warn "  'route-log'/'route-readout' calls resolve"
  fi
}

# --------------------------------------------------------------------------- #
# 3. embedding classifier: ollama + nomic-embed-text (optional)
# --------------------------------------------------------------------------- #
install_embed() {
  [ "$DO_EMBED" = "1" ] || { warn "skipping embedding tier (--no-embed)"; return 0; }

  if ! have ollama; then
    if [ "$OS" = "Darwin" ] && have brew; then say "installing ollama via brew"; brew install ollama || { warn "ollama install failed; embedding tier skipped"; return 0; }
    elif [ "$OS" = "Linux" ]; then say "installing ollama"; curl -fsSL https://ollama.com/install.sh | sh || { warn "ollama install failed; embedding tier skipped"; return 0; }
    else warn "ollama not available for this OS; embedding tier skipped"; return 0; fi
  fi

  # ensure the server is up, then pull the small (274MB) embedding model.
  (ollama serve >/dev/null 2>&1 &) || true
  sleep 2
  say "pulling nomic-embed-text (~274MB)"
  ollama pull nomic-embed-text || { warn "nomic-embed-text pull failed; embedding tier degraded"; return 0; }
  ok "embedding classifier ready (ollama + nomic-embed-text)"
}

# --------------------------------------------------------------------------- #
# 4. local Ornith 1.5 tiers — served by ollama (no longer Apple-Silicon-only)
# --------------------------------------------------------------------------- #
install_ornith() {
  [ "$DO_ORNITH" = "1" ] || { warn "skipping local Ornith (--no-ornith)"; return 0; }
  # The MLX server is RETIRED. It pinned ONE model at process start, which is exactly what made a
  # tier switch impossible without a rebuild, and it confined local inference to Apple Silicon.
  # ollama serves both tiers on :11434 and loads/unloads on demand — so this step is now just a
  # pull plus a written tier, and it works wherever ollama does.
  if ! have ollama; then
    warn "local Ornith needs ollama (install it or drop --no-embed so step 3 installs it) — routing still works"
    return 0
  fi
  (ollama serve >/dev/null 2>&1 &) || true
  sleep 2

  # Pull only what was asked for: the large tier is ~21GB and most machines want the small one.
  local want_small=0 want_large=0
  case "$ORNITH_TIER" in
    small) want_small=1 ;;
    large) want_large=1 ;;
    both)  want_small=1; want_large=1 ;;
    *) warn "unknown --ornith-tier '$ORNITH_TIER' (small|large|both) — defaulting to small"; want_small=1; ORNITH_TIER="small" ;;
  esac

  # A failed pull is a WARNING, never a hard failure: routing is pure-stdlib and does not need a
  # local model. `ollama pull` is resumable, so a re-run continues rather than restarting.
  if [ "$want_small" = "1" ]; then
    say "pulling Ornith 1.5 small tier ($ORNITH_MODEL_SMALL, ~5.6GB, resumable)"
    ollama pull "$ORNITH_MODEL_SMALL" || warn "small-tier pull failed; re-run to resume"
  fi
  if [ "$want_large" = "1" ]; then
    say "pulling Ornith 1.5 large tier ($ORNITH_MODEL_LARGE, ~21GB, resumable) — this can take a while"
    ollama pull "$ORNITH_MODEL_LARGE" || warn "large-tier pull failed; re-run to resume"
  fi

  # Write the active tier. This file is the single source of truth every consumer reads
  # (local_tier.resolve); the launchd units carry no model id of their own, so switching a tier
  # never means editing a plist.
  local active="$ORNITH_TIER"; [ "$active" = "both" ] && active="small"
  "$INSTALL_DIR/.venv/bin/python" -c "
import sys; sys.path.insert(0, '$INSTALL_DIR/src')
from apex_router.ornith import local_tier, tier_switch
tier_switch.write_state(local_tier.TIERS['$active'])
" || warn "could not write the tier file; 'apex-router ornith-tier $active' will fix it"

  # The retired MLX launch helper. Left as a loud stub rather than deleted so an existing launchd
  # unit or shell alias that still calls it fails with an explanation instead of a confusing
  # 'no such file' or, worse, silently starting a second resident model.
  cat > "$INSTALL_DIR/serve-ornith.sh" <<'EOF'
#!/usr/bin/env bash
# RETIRED. The Ornith MLX server (mlx_lm.server on :8080) has been replaced by ollama on :11434,
# which serves both tiers and can switch between them without a restart.
echo "serve-ornith.sh is retired — Ornith is served by ollama now." >&2
echo "  apex-router ornith-tier          # show the active tier" >&2
echo "  apex-router ornith-tier large    # switch (small|large)" >&2
exit 1
EOF
  chmod +x "$INSTALL_DIR/serve-ornith.sh"
  ok "Ornith 1.5 ready (tier: $active) — switch with: apex-router ornith-tier small|large"
  # NB: a bare `[ … ] && fn` returns 1 when the test is false, which under `set -e`
  # would abort the whole installer after ornith (Codex #1). Use an if-block.
  if [ "$DO_ORNITH_SERVE" = "1" ]; then
    install_ornith_service
  fi
}

# Install the local Ornith stack as launchd agents (macOS): the job-queue worker
# (com.ornith.worker) and the nightly maintenance cycle (com.ornith.overnight, 01:30). Both run
# apex-router's OWN venv python and derive every path from $INSTALL_DIR — nothing machine-specific
# is hardcoded.
#
# com.ornith.server (the MLX model server) is GONE: ollama owns model serving now and is already
# supervised by its own launchd/brew service, so a second always-on unit would just be a way to
# have two resident models. Any leftover unit from an older install is booted out below, because
# leaving it loaded would silently hold ~20GB against the tier budget.
install_ornith_service() {
  if [ "$OS" != "Darwin" ]; then
    warn "Ornith launchd agents are macOS-only; on $OS run the worker manually: $INSTALL_DIR/.venv/bin/python -m apex_router.ornith.ornith_worker"
    return 0
  fi
  local agents="$HOME/Library/LaunchAgents" logs="$INSTALL_DIR/logs" uid; uid="$(id -u)"
  local py="$INSTALL_DIR/.venv/bin/python"
  mkdir -p "$agents" "$logs"

  _ornith_plist() {  # name  program-args-xml  keepalive-xml  schedule-xml
    cat > "$agents/$1.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$1</string>
<key>ProgramArguments</key><array>$2</array>
<key>WorkingDirectory</key><string>$INSTALL_DIR</string>
<key>EnvironmentVariables</key><dict><key>ORNITH_URL</key><string>http://127.0.0.1:11434</string><key>APEX_ORNITH_QUEUE</key><string>${APEX_ORNITH_QUEUE:-$INSTALL_DIR/queue}</string></dict>
$3
$4
<key>StandardOutPath</key><string>$logs/$1.out</string>
<key>StandardErrorPath</key><string>$logs/$1.err</string>
</dict></plist>
PLIST
  }

  # NOTE: no ORNITH_API_MODEL here, deliberately. The active tier lives in ~/.apex-router/ornith.env
  # and is read at import by local_tier.resolve(), so switching tiers is one file write plus a
  # restart — not a plist rewrite. A model id baked in here would silently outrank the switch.

  # Boot out a com.ornith.server left over from the MLX era. If it survives, it holds ~20GB of
  # weights that the tier budget knows nothing about, and `apex-router ornith-tier` will refuse to
  # switch. The plist is renamed rather than deleted so the change is reversible.
  if launchctl print "gui/$uid/com.ornith.server" >/dev/null 2>&1; then
    say "retiring the MLX server unit (com.ornith.server) — ollama serves the model now"
    launchctl bootout "gui/$uid/com.ornith.server" >/dev/null 2>&1 || true
  fi
  # if-block, not `[ … ] && mv`: a bare test-and-command returns 1 when the test is false, which
  # under `set -e` aborts the whole installer (Codex #1, same trap as the ornith call site).
  if [ -f "$agents/com.ornith.server.plist" ]; then
    mv "$agents/com.ornith.server.plist" "$agents/com.ornith.server.plist.retired-mlx"
  fi

  # Worker: must NOT start at bootstrap — it would drain the queue while the tier is still cold and
  # fail those jobs (POSTs aren't retried) (Codex #3).
  # There is NO launchd keepalive form that both stays idle at load AND self-restarts: a bare
  # <key>KeepAlive</key><true/> starts immediately at load (regardless of RunAtLoad), and a KeepAlive
  # *dict* (e.g. SuccessfulExit=false) also runs once at load — launchd must run a job to ever observe
  # its exit. So install the worker with NEITHER RunAtLoad NOR KeepAlive: it loads IDLE and is started
  # on demand by the `launchctl kickstart` the installer prints once the server is ready. (Trade-off:
  # no automatic crash-restart; a future readiness-gated supervisor is the intended replacement.)
  _ornith_plist com.ornith.worker \
    "<string>$py</string><string>-m</string><string>apex_router.ornith.ornith_worker</string>" \
    '' ''
  _ornith_plist com.ornith.overnight \
    "<string>$py</string><string>-m</string><string>apex_router.ornith.overnight_cycle</string>" \
    '' '<key>StartCalendarInterval</key><dict><key>Hour</key><integer>1</integer><key>Minute</key><integer>30</integer></dict>'

  local n
  for n in com.ornith.worker com.ornith.overnight; do
    plutil -lint "$agents/$n.plist" >/dev/null || { warn "  $n.plist failed lint — skipping"; continue; }
    # bootout the old label, then retry bootstrap a couple of times: an immediate re-bootstrap
    # can race a not-yet-finished bootout (Codex #4). We DON'T use `bootout --wait` — it can
    # block indefinitely; a short retry loop is safer for an installer and reports honestly if
    # the label is still stuck rather than falsely claiming success.
    # bootout is best-effort cleanup of a prior instance; on a fresh machine the label
    # isn't loaded and bootout exits 3 ("No such process"). Under `set -e` an unguarded
    # failure here aborts the whole installer before ANY agent is bootstrapped (and before
    # watchers/proxy/verify run), so swallow it — the retry-bootstrap below is what matters.
    launchctl bootout "gui/$uid/$n" >/dev/null 2>&1 || true
    local tries=0 loaded=0
    while [ "$tries" -lt 3 ]; do
      if launchctl bootstrap "gui/$uid" "$agents/$n.plist" >/dev/null 2>&1; then loaded=1; break; fi
      tries=$((tries+1)); sleep 1
    done
    [ "$loaded" = "1" ] && ok "  loaded $n" \
      || warn "  failed to load $n — old instance may still be unloading; re-run: launchctl bootstrap gui/$uid $agents/$n.plist"
  done
  ORNITH_WORKER_INSTALLED=1   # the drain-owning worker is now installed → watchers may skip drain
  ok "Ornith worker + overnight cycle loaded (model serving is ollama's job now)."
  echo "    The WORKER is intentionally NOT auto-started (it would drain the queue before the"
  echo "    tier is warm). Warm the tier, then start it:"
  echo "        apex-router ornith-tier $ORNITH_TIER      # loads + waits for the model to answer"
  echo "        launchctl kickstart gui/$uid/com.ornith.worker"
  echo "    Verify:  apex-router ornith-tier --json"
}

# --------------------------------------------------------------------------- #
# 5. Claude + Codex presence (no gateway needed) + starter route table
# --------------------------------------------------------------------------- #
check_clients_and_table() {
  have claude && ok "claude CLI detected" || warn "claude CLI not found — install it for the routing consumer to dispatch"
  have codex  && ok "codex CLI detected"  || warn "codex CLI not found — install it for cross-validation / Codex-backed scoring"

  # Ship a starter (all-fallback) route table so the shim resolves from day one.
  mkdir -p "$INSTALL_DIR/tables"
  for venue in skill proxy; do
    local f="$INSTALL_DIR/tables/route_table.$venue.json"
    [ -f "$f" ] || printf '{"schema_version":1,"venue":"%s","generated_from":{},"cells":[],"dropped_routes":[]}\n' "$venue" > "$f"
  done
  ok "starter route tables at $INSTALL_DIR/tables (all-fallback until a bench runs)"
}

# --------------------------------------------------------------------------- #
# 6. verify
# --------------------------------------------------------------------------- #
verify() {
  say "verifying install"
  # The proxy_engine subtree needs the [proxy] extra (starlette/httpx/numpy/...) even to
  # COLLECT — pytest aborts the whole run on its import errors. Gate on the INTENT flag,
  # not an import probe: with --proxy the extra was installed (uv pip install ran under
  # `set -e`, so the deps are complete), so run the full suite; otherwise skip that subtree
  # so a lean install doesn't emit a false "did not pass".
  # (if/else, not an `--ignore=$var` array, so it stays correct under quoting and bash 3.2.)
  local tests_ok=0
  if [ "$DO_PROXY" = "1" ]; then
    "$INSTALL_DIR/.venv/bin/python" -m pytest "$INSTALL_DIR/tests" -q >/dev/null 2>&1 && tests_ok=1
  else
    "$INSTALL_DIR/.venv/bin/python" -m pytest "$INSTALL_DIR/tests" \
      --ignore="$INSTALL_DIR/tests/proxy_engine" -q >/dev/null 2>&1 && tests_ok=1
  fi
  [ "$tests_ok" = 1 ] && ok "test suite passed" || warn "test suite did not fully pass (routing may still work)"
  "$INSTALL_DIR/.venv/bin/apex-router" status
  ok "apex-router installed at $INSTALL_DIR"
  echo
  echo "  routing works now (pure-stdlib). Optional tiers depend on their services:"
  echo "    - embedding: start ollama; local bench: run $INSTALL_DIR/serve-ornith.sh (Apple Silicon)"
  echo "  add to PATH:  export PATH=\"$INSTALL_DIR/.venv/bin:\$PATH\""
}

# --------------------------------------------------------------------------- #
# 7. watchers (opt-in) — cross-platform background jobs: drain worker + daily report
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# team skill marketplace (optional) — print the /plugin wiring, never hardcode a private URL
# --------------------------------------------------------------------------- #
install_hooks() {
  # Install the review post-commit hook into the user-named repos. Nothing hardcoded — repos come
  # from --install-hooks / APEX_HOOK_REPOS. Symlinked so a hook update propagates; never clobbers an
  # existing real (non-symlink) post-commit.
  [ -n "$HOOK_REPOS" ] || {
    echo "  review git-hooks NOT installed (pass --install-hooks \"<repo> <repo>\" to enable)."
    return 0
  }
  local hook="$INSTALL_DIR/hooks/ornith-review-enqueue.sh"
  [ -f "$hook" ] || { warn "hook script missing at $hook"; return 0; }
  chmod +x "$hook" 2>/dev/null
  say "installing review post-commit hook"
  local r dest
  for r in $HOOK_REPOS; do
    r="${r/#\~/$HOME}"
    if [ ! -d "$r/.git" ]; then warn "  $r is not a git repo — skipped"; continue; fi
    dest="$r/.git/hooks/post-commit"
    if [ -e "$dest" ] && [ ! -L "$dest" ]; then
      warn "  $r already has a real post-commit — skipped (remove it to use the review hook)"
    else
      ln -sf "$hook" "$dest" && ok "  hooked $r"
    fi
  done
}

_wire_stop_hook() {
  # Shared: idempotently merge a Stop hook into ~/.claude/settings.json (preserve
  # existing hooks + unrelated keys, append as its own group, .apex-bak backup).
  # $1 = absolute hook path, $2 = basename to dedupe on.
  local hook="$1" base="$2" settings="$HOME/.claude/settings.json"
  [ -f "$hook" ] || { warn "hook missing at $hook"; return 1; }
  chmod +x "$hook" 2>/dev/null
  "$INSTALL_DIR/.venv/bin/python" - "$settings" "$hook" "$base" <<'PY'
import json, os, sys
settings_path, hook_path, base = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(os.path.dirname(settings_path), exist_ok=True)
try:
    with open(settings_path) as f:
        s = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    s = {}
stop = s.setdefault("hooks", {}).setdefault("Stop", [])
already = any(h.get("command", "").endswith(base)
              for g in stop if isinstance(g, dict) for h in g.get("hooks", []))
if not already:
    if os.path.exists(settings_path):
        with open(settings_path + ".apex-bak", "w") as b:
            json.dump(s, b, indent=2)
    stop.append({"hooks": [{"type": "command", "command": hook_path}]})
    tmp = settings_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2); f.write("\n")
    os.replace(tmp, settings_path)
PY
}

install_cache_handoff_hook() {
  # Wire the cache-cost session-handoff Stop hook into ~/.claude/settings.json.
  # Opt-in (advisory hook; never blocks a session). Idempotent — re-running is a no-op.
  [ "$DO_CACHE_HANDOFF" = "1" ] || {
    echo "  cache-handoff Stop hook NOT wired (pass --cache-handoff-hook to enable)."
    return 0
  }
  say "wiring cache-handoff Stop hook into settings.json"
  if _wire_stop_hook "$INSTALL_DIR/hooks/cache-handoff-nudge.sh" "cache-handoff-nudge.sh"; then
    ok "cache-handoff hook wired"
    echo "     starts with an AGGRESSIVE (low) cap; relax per repo/task as signals show — see docs/RUNBOOK-cache-cost.md"
    echo "     inspect per-session read distribution: python $INSTALL_DIR/scripts/cache_report.py --days 7"
  else
    warn "settings.json merge failed — wire it manually (see docs/RUNBOOK-cache-cost.md)"
  fi
}

install_memory_compact_hook() {
  # Wire the project-memory compaction Stop hook into ~/.claude/settings.json.
  # Opt-in, advisory (never mutates memory — the mutating path is memory_compact.py --apply).
  [ "$DO_MEMORY_COMPACT" = "1" ] || {
    echo "  memory-compact Stop hook NOT wired (pass --memory-compact-hook to enable)."
    return 0
  }
  say "wiring memory-compact Stop hook into settings.json"
  if _wire_stop_hook "$INSTALL_DIR/hooks/memory-compact-nudge.sh" "memory-compact-nudge.sh"; then
    ok "memory-compact hook wired"
    echo "     advisory: nudges when a project's MEMORY.md grows large; run"
    echo "     'python $INSTALL_DIR/scripts/memory_compact.py --dir <memory>' to review — see docs/RUNBOOK-cache-cost.md"
  else
    warn "settings.json merge failed — wire it manually (see docs/RUNBOOK-cache-cost.md)"
  fi
}

install_proxy() {
  # The measuring proxy is a LIVE data plane on your request path — we install it (via the
  # [proxy] extra above) but never auto-start it. Print how to run it + verify it imports.
  [ "$DO_PROXY" = "1" ] || {
    echo "  measuring proxy NOT installed (re-run with --proxy to add the [proxy] extra)."
    return 0
  }
  say "measuring proxy installed ([proxy] extra)"
  if "$INSTALL_DIR/.venv/bin/python" -c "import apex_router.proxy_engine.proxy.app" >/dev/null 2>&1; then
    ok "proxy imports OK — start it with:  apex-router serve   (defaults to 127.0.0.1:8788)"
    echo "     point Claude Code at it:      apex-router setup-proxy   (or --proxy-config <file>)"
    echo "     cost report from telemetry:   apex-router proxy doctor"
    echo "     NOTE: the proxy keeps a local SQLite store with request content — treat ~/.apex/ as sensitive."
  else
    warn "proxy extra did not import cleanly — check 'pip install apex-router[proxy]'"
  fi
}

setup_proxy() {
  # Merge proxy client env into ~/.claude/settings.json IF the user provided config.
  # Values come from --proxy-config file or the environment; apex-router hardcodes none.
  if [ -z "$PROXY_CONFIG" ]; then
    # still run if the proxy env keys are already exported (env-only setup)
    "$INSTALL_DIR/.venv/bin/apex-router" setup-proxy --dry-run >/dev/null 2>&1 && {
      echo "  proxy env detected in environment — apply with: apex-router setup-proxy"
    }
    return 0
  fi
  say "wiring Claude Code through your proxy (from $PROXY_CONFIG)"
  "$INSTALL_DIR/.venv/bin/apex-router" setup-proxy --config "$PROXY_CONFIG" \
    && ok "settings.json updated (a .apex-bak backup was written)" \
    || warn "proxy setup did not complete (routing still works)"
}

install_pi() {
  # Wire the pi coding agent for per-task model/family switching on top of apex-router:
  #   1. install the apex-route extension (persisted in pi's settings), and
  #   2. seed ~/.pi/agent/models.json to route anthropic+moonshotai via the proxy.
  # Opt-in. NEVER clobbers an existing models.json (merge is the user's call) — mirrors the
  # "merge, never overwrite" posture of setup-proxy. See docs/RUNBOOK-pi-integration.md.
  [ "$DO_PI" = "1" ] || {
    echo "  pi integration NOT installed (pass --pi-integration to add the per-task router)."
    return 0
  }
  local src="$INSTALL_DIR/integrations/pi"
  if ! have pi; then
    warn "pi not on PATH — skipping. Install pi, then: pi install $src/apex-route.ts"
    echo "     and merge $src/models.json into ~/.pi/agent/models.json (see docs/RUNBOOK-pi-integration.md)"
    return 0
  fi
  say "installing pi per-task router extension"
  if pi install "$src/apex-route.ts" >/dev/null 2>&1; then
    ok "apex-route extension installed (>>local / >>kimi / >>frontier / >>deep, and /apex-route)"
  else
    warn "pi install failed — add it manually: pi install $src/apex-route.ts"
  fi
  # models.json: seed only when absent; never overwrite a user's providers.
  local pim="$HOME/.pi/agent/models.json"
  if [ -f "$pim" ]; then
    echo "     $pim exists — NOT overwritten. Merge the 'providers' block from:"
    echo "       $src/models.json   (routes anthropic+moonshotai via the proxy on :8788)"
  else
    mkdir -p "$(dirname "$pim")" 2>/dev/null || true
    if cp "$src/models.json" "$pim" 2>/dev/null; then
      ok "seeded $pim (proxied anthropic+moonshotai + local Ornith tiers)"
    else
      warn "could not write $pim — copy $src/models.json there by hand"
    fi
  fi
  echo "     start the proxy first:  apex-router serve   (per-task routing needs it on :8788)"
}

install_booksearch() {
  # Local semantic index over a folder of PDF books (scripts/booksearch.py): the [books]
  # extra (pypdf), a `booksearch` wrapper on PATH, and the pi + claude slash commands.
  # Ingest is a heavy one-time step we do NOT auto-run — we print the command.
  [ "$DO_BOOKS" = "1" ] || {
    echo "  booksearch NOT installed (pass --books-index for local-book references)."
    return 0
  }
  say "installing booksearch ([books] extra + wrapper)"
  uv pip install --python "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR[books]" >/dev/null 2>&1 \
    && ok "pypdf installed" || warn "could not install [books] extra (pypdf) — PDF ingest will fail"
  local bin="$HOME/.local/bin/booksearch"
  mkdir -p "$HOME/.local/bin" 2>/dev/null || true
  cat > "$bin" <<EOF
#!/usr/bin/env bash
# booksearch — local semantic index over \$BOOKS_DIR (default ~/books). All local.
exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/booksearch.py" "\$@"
EOF
  chmod +x "$bin" && ok "wrapper installed: $bin"
  case ":$PATH:" in *":$HOME/.local/bin:"*) : ;; *) echo "     add ~/.local/bin to PATH to call 'booksearch' directly";; esac
  # pi command (best-effort)
  if have pi; then
    pi install "$INSTALL_DIR/integrations/pi/booksearch.ts" >/dev/null 2>&1 \
      && ok "pi /books command installed" || echo "     add pi cmd: pi install $INSTALL_DIR/integrations/pi/booksearch.ts"
  fi
  # claude slash command (best-effort; never clobber a user-customised copy)
  local cmddir="$HOME/.claude/commands"
  if mkdir -p "$cmddir" 2>/dev/null; then
    if [ -e "$cmddir/books.md" ]; then
      echo "     claude /books exists at $cmddir/books.md — not overwritten"
    elif cp "$INSTALL_DIR/integrations/claude/books.md" "$cmddir/books.md" 2>/dev/null; then
      ok "claude /books command installed"
    fi
  fi
  echo "     now index your library:  booksearch ingest   (one-time; ~/books by default)"
  echo "     then query:              booksearch query \"<your problem>\"   — see docs/RUNBOOK-booksearch.md"
}

# Register one marketplace via the non-interactive `claude plugin marketplace add` CLI. Idempotent:
# `add` on an already-registered marketplace is a no-op that returns 0, so re-running the installer
# (or installing when apex-router is already present) just refreshes it. Prints the manual command
# as a fallback when the claude CLI isn't on PATH.
_marketplace_add() {  # <source> (github owner/repo, git URL, or local path)
  local src="$1"
  if have claude; then
    if claude plugin marketplace add "$src" >/dev/null 2>&1; then
      ok "  marketplace added: $src"
    else
      # already-registered is fine; a real failure is worth surfacing with the manual command.
      claude plugin marketplace list 2>/dev/null | grep -qiF "$src" \
        && ok "  marketplace already registered: $src" \
        || warn "  could not add $src automatically — in Claude Code: /plugin marketplace add $src"
    fi
  else
    echo "    /plugin marketplace add $src"
  fi
}

# Wire Claude Code skill marketplaces. apex-router ships ONE public marketplace by default
# (workflow-discipline skills) and supports MULTIPLE: any --skills-marketplace / APEX_SKILLS_MARKETPLACE
# entries are added alongside it. Everything goes through the `claude plugin` CLI (non-interactive);
# if that CLI is absent, the exact commands are printed instead.
install_skills_marketplaces() {
  echo
  say "Claude Code skill marketplaces"
  have claude || echo "  (claude CLI not found — run these in Claude Code yourself:)"

  # 1) Public default marketplace + its plugin (unless opted out).
  if [ "$DO_SKILLS" = "1" ]; then
    _marketplace_add "$APEX_PUBLIC_MARKETPLACE"
    if have claude; then
      claude plugin install "$APEX_DEFAULT_PLUGIN" --scope user >/dev/null 2>&1 \
        && ok "  installed $APEX_DEFAULT_PLUGIN (verify-claims, cross-validate, disciplined-execution)" \
        || warn "  could not install $APEX_DEFAULT_PLUGIN — in Claude Code: /plugin install $APEX_DEFAULT_PLUGIN"
    else
      echo "    /plugin install $APEX_DEFAULT_PLUGIN"
    fi
  else
    echo "  (default public skills skipped: --no-skills / APEX_NO_SKILLS)"
  fi

  # 2) Any extra marketplaces (e.g. a private team repo). Added, not auto-installed — the user picks
  #    which plugins to install from them (their names aren't known here). Iterate NEWLINE-delimited
  #    (via read, not word-splitting) so a source path with spaces survives as one argument.
  if [ -n "$SKILLS_MARKETPLACES" ]; then
    # SKILLS_MARKETPLACES is already newline-separated (env normalized at init; flag appends newlines),
    # so read line-by-line — a source path containing spaces stays one argument.
    printf '%s\n' "$SKILLS_MARKETPLACES" | while IFS= read -r url; do
      [ -n "$url" ] || continue
      _marketplace_add "$url"
    done
    echo "  then install plugins from the added marketplace(s):"
    echo "    (in Claude Code)  /plugin install <plugin>@<marketplace-name>"
  fi
}

install_watchers() {
  [ "$DO_WATCH" = "1" ] || {
    echo "  background watchers NOT installed (run 'apex-router watch install' to enable, or"
    echo "  re-run the installer with --watch). They drain the local job queue + write a daily report."
    return 0
  }
  say "installing background watchers ($OS)"
  # launchd on macOS, systemd --user on Linux — the CLI picks the right one.
  # When the Ornith launchd stack (--ornith-serve) is also being installed, it already provides a
  # queue drainer (com.ornith.worker) on the SAME inbox; installing the drain watcher too would run
  # two daemons on one single-GPU queue. Pass --no-drain so the watcher contributes only the daily
  # report and the Ornith worker owns draining.
  # Skip the watcher drainer ONLY when the Ornith worker actually installed (macOS, not
  # short-circuited) — not merely when --ornith-serve was requested. Otherwise a Linux/Intel-mac /
  # --no-ornith / failed-bootstrap run would strip the drainer with nothing to replace it, leaving
  # the queue undrained while the installer reports success (Codex xval P1).
  local no_drain=""
  [ "$ORNITH_WORKER_INSTALLED" = "1" ] && no_drain="--no-drain"
  "$INSTALL_DIR/.venv/bin/apex-router" watch install $no_drain \
    && ok "watchers installed${no_drain:+ (drain skipped — Ornith worker drains the queue)} (apex-router watch status to check; watch uninstall to remove)" \
    || warn "watcher install did not complete (routing still works; try 'apex-router watch install')"
}

# --------------------------------------------------------------------------- #
main() {
  say "apex-router installer  ($OS/$ARCH; apple-silicon=$IS_APPLE_SILICON)"
  if [ "$VERIFY_ONLY" = "1" ]; then verify; exit 0; fi
  # Update path (Option B): (re)wire skill marketplaces only, for a machine that already has
  # apex-router. Idempotent — `claude plugin marketplace add` on an existing one is a no-op.
  if [ "$SKILLS_ONLY" = "1" ]; then install_skills_marketplaces; exit 0; fi
  ensure_prereqs
  install_package
  install_embed
  install_ornith
  check_clients_and_table
  install_watchers
  install_hooks
  install_cache_handoff_hook
  install_memory_compact_hook
  install_proxy
  setup_proxy
  install_pi
  install_booksearch
  verify
  install_skills_marketplaces
}
main
