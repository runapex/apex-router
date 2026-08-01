#!/usr/bin/env bash
# apex-router one-command installer.
#
#   curl -fsSL https://raw.githubusercontent.com/<owner>/apex-router/main/install.sh | bash
#
# Provisions the full stack, arch-aware and IDEMPOTENT (anything already present is
# skipped). No Foundry is required anywhere — the target uses its own Claude + Codex
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
#         --no-embed    skip ollama / nomic-embed
#         --dir PATH    install location (default: ~/.apex-router)
#         --repo URL    git repo to clone (default: the public apex-router repo)
#         --verify-only re-run the self-check against an existing install
set -euo pipefail

# --------------------------------------------------------------------------- #
# config + arg parsing
# --------------------------------------------------------------------------- #
REPO_URL_DEFAULT="https://github.com/OWNER/apex-router.git"   # <- set on publish
INSTALL_DIR="${APEX_ROUTER_DIR:-$HOME/.apex-router}"
REPO_URL="$REPO_URL_DEFAULT"
DO_ORNITH=1
DO_EMBED=1
VERIFY_ONLY=0
ORNITH_MODEL="mlx-community/Ornith-1.0-35B-4bit"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-ornith) DO_ORNITH=0 ;;
    --no-embed)  DO_EMBED=0 ;;
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
  uv venv "$INSTALL_DIR/.venv" >/dev/null
  # Core is dependency-free; add the ornith extra only where MLX can run.
  local extras="dev"
  [ "$DO_ORNITH" = "1" ] && [ "$IS_APPLE_SILICON" = "1" ] && extras="dev,ornith"
  uv pip install --python "$INSTALL_DIR/.venv/bin/python" -e "$INSTALL_DIR[$extras]" >/dev/null
  ok "apex-router package installed (extras: $extras)"
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
# start the local Ornith MLX server on :8080
exec "$INSTALL_DIR/.venv/bin/python" -m mlx_lm.server --model "$ORNITH_MODEL" --port 8080
EOF
  chmod +x "$INSTALL_DIR/serve-ornith.sh"
  ok "Ornith MLX model ready — start it with: $INSTALL_DIR/serve-ornith.sh"
}

# --------------------------------------------------------------------------- #
# 5. Claude + Codex presence (no Foundry needed) + starter route table
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
  "$INSTALL_DIR/.venv/bin/python" -m pytest "$INSTALL_DIR/tests" -q >/dev/null 2>&1 \
    && ok "test suite passed" || warn "test suite did not fully pass (routing may still work)"
  "$INSTALL_DIR/.venv/bin/apex-router" status
  ok "apex-router installed at $INSTALL_DIR"
  echo
  echo "  routing works now (pure-stdlib). Optional tiers depend on their services:"
  echo "    - embedding: start ollama; local bench: run $INSTALL_DIR/serve-ornith.sh (Apple Silicon)"
  echo "  add to PATH:  export PATH=\"$INSTALL_DIR/.venv/bin:\$PATH\""
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
  verify
}
main
