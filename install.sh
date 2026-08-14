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
#   - Ornith MLX server    : Apple Silicon only (local replay bench / codegen); skipped
#                            with a clear notice on any other arch — never a hard failure
#
# Flags:  --no-ornith   skip the large MLX model download
#         --ornith-serve  (macOS) install the Ornith stack as always-on launchd agents
#                         (server + queue worker + nightly cycle), not just the run helper
#         --no-embed    skip ollama / nomic-embed
#         --watch       install the background watchers (drain worker + daily report)
#         --proxy       install the measuring proxy ([proxy] extra: starlette/uvicorn/…)
#         --install-hooks "R1 R2"  install the review post-commit hook into these git repos
#         --proxy-config F  wire Claude Code through a proxy via ~/.claude/settings.json
#         --skills-marketplace URL  print /plugin commands for a private team skill marketplace
#         --dir PATH    install location (default: ~/.apex-router)
#         --repo URL    git repo to clone (default: the public apex-router repo)
#         --verify-only re-run the self-check against an existing install
set -euo pipefail

# --------------------------------------------------------------------------- #
# config + arg parsing
# --------------------------------------------------------------------------- #
REPO_URL_DEFAULT="https://github.com/runapex/apex-router.git"
INSTALL_DIR="${APEX_ROUTER_DIR:-$HOME/.apex-router}"
REPO_URL="$REPO_URL_DEFAULT"
DO_ORNITH=1
DO_ORNITH_SERVE=0
DO_EMBED=1
DO_WATCH=0
DO_PROXY=0
VERIFY_ONLY=0
ORNITH_MODEL="mlx-community/Ornith-1.0-35B-4bit"
# Repos to install the review post-commit hook into (space-separated; user-supplied, none hardcoded).
HOOK_REPOS="${APEX_HOOK_REPOS:-}"
# Private team skill marketplace (Claude Code plugin repo). Public apex-router hardcodes NO private
# URL — pass --skills-marketplace <git-url> or set APEX_SKILLS_MARKETPLACE to wire your team's.
SKILLS_MARKETPLACE="${APEX_SKILLS_MARKETPLACE:-}"
# Proxy client wiring merged into ~/.claude/settings.json. Values come from a --proxy-config
# file or your environment — NOTHING is hardcoded here. Empty = skip (routing still installs).
PROXY_CONFIG="${APEX_PROXY_CONFIG:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-ornith) DO_ORNITH=0 ;;
    --ornith-serve) DO_ORNITH_SERVE=1 ;;   # macOS: install the Ornith stack as launchd agents
    --no-embed)  DO_EMBED=0 ;;
    --watch)     DO_WATCH=1 ;;
    --proxy)     DO_PROXY=1 ;;
    --install-hooks) HOOK_REPOS="$2"; shift ;;
    --skills-marketplace) SKILLS_MARKETPLACE="$2"; shift ;;
    --proxy-config) PROXY_CONFIG="$2"; shift ;;
    --dir)       INSTALL_DIR="$2"; shift ;;
    --repo)      REPO_URL="$2"; shift ;;
    --verify-only) VERIFY_ONLY=1 ;;
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

  say "creating venv + installing the package"
  # Core is dependency-free; add extras only for the tiers the user opted into.
  local extras="dev"
  [ "$DO_ORNITH" = "1" ] && [ "$IS_APPLE_SILICON" = "1" ] && extras="$extras,ornith"
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
# 4. local Ornith MLX server — Apple Silicon ONLY (graceful skip elsewhere)
# --------------------------------------------------------------------------- #
install_ornith() {
  [ "$DO_ORNITH" = "1" ] || { warn "skipping local Ornith (--no-ornith)"; return 0; }
  if [ "$IS_APPLE_SILICON" != "1" ]; then
    warn "local Ornith needs Apple Silicon (MLX); this is $OS/$ARCH — routing + embedding still installed, local bench/codegen unavailable"
    return 0
  fi
  # mlx-lm was installed as the 'ornith' extra in step 2. Download the model (resumable
  # via huggingface cache) and leave a launch helper — we don't auto-start a 20GB server.
  #
  # RECOMMENDED: export a Hugging Face token before running so this ~20GB pull isn't
  # throttled by anonymous rate limits (and to reach any gated repo). snapshot_download
  # reads it from the environment automatically — no flag needed:
  #     export HF_TOKEN=hf_xxx      # https://huggingface.co/settings/tokens (read scope)
  # Never hardcode the token here or commit it; it stays in your shell/secret store only.
  [ -n "${HF_TOKEN:-}" ] && say "using HF_TOKEN from environment for the model download" \
    || warn "no HF_TOKEN set — the ~20GB pull may be rate-limited; export HF_TOKEN to speed it up"
  say "downloading Ornith MLX model ($ORNITH_MODEL, ~20GB, resumable) — this can take a while"
  "$INSTALL_DIR/.venv/bin/python" - "$ORNITH_MODEL" <<'PY' || { warn "Ornith model download failed; local bench unavailable (re-run to resume)"; return 0; }
import sys
try:
    from huggingface_hub import snapshot_download
    snapshot_download(sys.argv[1])
except Exception as e:
    print(f"download error: {e}", file=sys.stderr); sys.exit(1)
PY
  cat > "$INSTALL_DIR/serve-ornith.sh" <<EOF
#!/usr/bin/env bash
# start the local Ornith MLX server on :8080. Model + tuning come from env (overridable)
# so the launchd unit and a manual run share one definition.
# Only flags supported across mlx-lm>=0.19.0 (the pyproject floor) are used here —
# --decode-concurrency/--prompt-concurrency arrived later and would make an older-but-valid
# mlx-lm exit on unknown args, which KeepAlive would then restart-loop (Codex #7).
exec "\${ORNITH_PYTHON:-$INSTALL_DIR/.venv/bin/python}" -m mlx_lm.server \\
  --model "\${ORNITH_MODEL:-$ORNITH_MODEL}" --host "\${ORNITH_HOST:-127.0.0.1}" --port "\${ORNITH_PORT:-8080}" \\
  --temp 0.0 --top-p 1.0
EOF
  chmod +x "$INSTALL_DIR/serve-ornith.sh"
  ok "Ornith MLX model ready — start it with: $INSTALL_DIR/serve-ornith.sh"
  # NB: a bare `[ … ] && fn` returns 1 when the test is false, which under `set -e`
  # would abort the whole installer after ornith (Codex #1). Use an if-block.
  if [ "$DO_ORNITH_SERVE" = "1" ]; then
    install_ornith_service
  fi
}

# Install the local Ornith stack as always-on launchd agents (macOS): the MLX server
# (com.ornith.server, RunAtLoad+KeepAlive), the job-queue worker (com.ornith.worker), and
# the nightly maintenance cycle (com.ornith.overnight, 01:30). All three run apex-router's
# OWN venv python and derive every path from $INSTALL_DIR — nothing machine-specific is
# hardcoded. The server Label is exactly 'com.ornith.server' because apex_router.ornith.
# model_router keys its readiness check off that label.
install_ornith_service() {
  if [ "$OS" != "Darwin" ]; then
    warn "Ornith launchd service is macOS-only; run $INSTALL_DIR/serve-ornith.sh manually on $OS"
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
<key>EnvironmentVariables</key><dict><key>ORNITH_MODEL</key><string>$ORNITH_MODEL</string><key>APEX_ORNITH_QUEUE</key><string>${APEX_ORNITH_QUEUE:-$INSTALL_DIR/queue}</string></dict>
$3
$4
<key>StandardOutPath</key><string>$logs/$1.out</string>
<key>StandardErrorPath</key><string>$logs/$1.err</string>
</dict></plist>
PLIST
  }

  _ornith_plist com.ornith.server \
    "<string>/bin/bash</string><string>$INSTALL_DIR/serve-ornith.sh</string>" \
    '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>' ''
  # Worker: NOT RunAtLoad — if it starts with the server it would drain the queue while the
  # 35B model is still loading and fail those jobs (POSTs aren't retried) (Codex #3). KeepAlive
  # keeps it up once launched; kick it off after the server is confirmed ready (see note printed
  # at the end). A future readiness-gated start can replace the manual kick.
  _ornith_plist com.ornith.worker \
    "<string>$py</string><string>-m</string><string>apex_router.ornith.ornith_worker</string>" \
    '<key>KeepAlive</key><true/>' ''
  _ornith_plist com.ornith.overnight \
    "<string>$py</string><string>-m</string><string>apex_router.ornith.overnight_cycle</string>" \
    '' '<key>StartCalendarInterval</key><dict><key>Hour</key><integer>1</integer><key>Minute</key><integer>30</integer></dict>'

  local n
  for n in com.ornith.server com.ornith.worker com.ornith.overnight; do
    plutil -lint "$agents/$n.plist" >/dev/null || { warn "  $n.plist failed lint — skipping"; continue; }
    # bootout the old label, then retry bootstrap a couple of times: an immediate re-bootstrap
    # can race a not-yet-finished bootout (Codex #4). We DON'T use `bootout --wait` — it can
    # block indefinitely; a short retry loop is safer for an installer and reports honestly if
    # the label is still stuck rather than falsely claiming success.
    launchctl bootout "gui/$uid/$n" >/dev/null 2>&1
    local tries=0 loaded=0
    while [ "$tries" -lt 3 ]; do
      if launchctl bootstrap "gui/$uid" "$agents/$n.plist" >/dev/null 2>&1; then loaded=1; break; fi
      tries=$((tries+1)); sleep 1
    done
    [ "$loaded" = "1" ] && ok "  loaded $n" \
      || warn "  failed to load $n — old instance may still be unloading; re-run: launchctl bootstrap gui/$uid $agents/$n.plist"
  done
  ok "Ornith server + overnight cycle loaded. The model takes ~1-3min to load on first start."
  echo "    The WORKER is intentionally NOT auto-started (it would drain the queue before the"
  echo "    model is ready). Once the server answers, start it with:"
  echo "        launchctl kickstart gui/$uid/com.ornith.worker"
  echo "    Verify:  launchctl print gui/$uid/com.ornith.server | grep -i state"
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

skills_hint() {
  local url="$SKILLS_MARKETPLACE"
  echo
  say "team skills (Claude Code plugin marketplace)"
  if [ -n "$url" ]; then
    echo "  add your team's private skill marketplace and install its plugins:"
    echo "    (in Claude Code)"
    echo "    /plugin marketplace add $url"
    echo "    /plugin install <plugin>@<marketplace-name>   # e.g. team-ops@skills-bundler"
  else
    echo "  apex-router ships no skills. To share team skills (internal ops, workflows) via a PRIVATE"
    echo "  Claude Code marketplace, re-run with --skills-marketplace <git-url> (or set"
    echo "  APEX_SKILLS_MARKETPLACE), then in Claude Code:"
    echo "    /plugin marketplace add <your-private-git-url>"
    echo "    /plugin install <plugin>@<marketplace-name>"
    echo "  (kept out of this public repo on purpose — internal skills stay in your private repo.)"
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
  "$INSTALL_DIR/.venv/bin/apex-router" watch install \
    && ok "watchers installed (apex-router watch status to check; watch uninstall to remove)" \
    || warn "watcher install did not complete (routing still works; try 'apex-router watch install')"
}

# --------------------------------------------------------------------------- #
main() {
  say "apex-router installer  ($OS/$ARCH; apple-silicon=$IS_APPLE_SILICON)"
  if [ "$VERIFY_ONLY" = "1" ]; then verify; exit 0; fi
  ensure_prereqs
  install_package
  install_embed
  install_ornith
  check_clients_and_table
  install_watchers
  install_hooks
  install_proxy
  setup_proxy
  verify
  skills_hint
}
main
