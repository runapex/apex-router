#!/usr/bin/env bash
# WP9 — QLoRA training on Apple Silicon (MLX-LM), path (a): fuse -> GGUF -> ollama tag.
#
# Called by overnight_cycle.py (which expects ROOT/'training/train.sh'). Serving stays
# ONE stack: the fine-tuned adapter is fused into the local base model (whatever
# ORNITH_TRAIN_BASE points at), converted to GGUF at the SAME quant as the incumbent,
# and imported as a SHADOW ollama tag (local-candidate:<id>).
# qlora_serve.py then gates it (probe + amr.gate) before repointing the `local` family.
#
#   train.sh --data <dir> --cycle-id <id> [--dry-run]
#
# --dry-run validates deps/data/base/disk and creates NOTHING. Missing mlx-lm is a hard
# error (exit 1) even in dry-run, so a non-Apple-Silicon box fails loudly, not silently.
set -euo pipefail

DATA="" ; CYCLE_ID="" ; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --data) DATA="$2"; shift ;;
    --cycle-id) CYCLE_ID="$2"; shift ;;
    --dry-run) DRY=1 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

err()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
info() { printf '▸ %s\n' "$*"; }

# --- config (env-overridable) ----------------------------------------------
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"                 # ornith package dir
VENV_PY="${ORNITH_TRAIN_PYTHON:-$HOME/.apex-router/.venv/bin/python}"
BASE_MODEL="${ORNITH_TRAIN_BASE:-}"                          # HF/MLX base (safetensors) — set ORNITH_TRAIN_BASE to your local base model
INCUMBENT_QUANT="${ORNITH_INCUMBENT_QUANT:-Q4_K_M}"
LORA_RANK="${ORNITH_LORA_RANK:-16}"
ITERS="${ORNITH_TRAIN_ITERS:-300}"
ADAPTER_OUT="${ORNITH_ADAPTER_OUT:-$ROOT_DIR/training/adapters/$CYCLE_ID}"

[ -n "$DATA" ] || { err "--data <dir> required"; exit 2; }
[ -n "$CYCLE_ID" ] || { err "--cycle-id <id> required"; exit 2; }

# --- validation (runs in both dry-run and real) ----------------------------
info "validating prerequisites (cycle $CYCLE_ID)"
[ -f "$DATA/train.jsonl" ] || { err "missing $DATA/train.jsonl (run prepare_data.py)"; exit 3; }
[ -f "$DATA/valid.jsonl" ] || { err "missing $DATA/valid.jsonl (run prepare_data.py)"; exit 3; }
[ -f "$DATA/attestation.json" ] || { err "missing $DATA/attestation.json (contamination scan not run)"; exit 3; }
ok "data + attestation present"

"$VENV_PY" -c "import mlx_lm" 2>/dev/null \
  || { err "mlx-lm not installed (Apple-Silicon only). Install: pip install 'apex-router[ornith]' or mlx-lm"; exit 1; }
ok "mlx-lm importable"

[ -n "$BASE_MODEL" ] || { err "set ORNITH_TRAIN_BASE to the MLX/HF base model (safetensors)"; exit 1; }
ok "base model configured: $BASE_MODEL"

# rough disk check (need a few GB for adapters + GGUF)
avail_kb="$(df -k "$ROOT_DIR" | awk 'NR==2{print $4}')"
[ "${avail_kb:-0}" -gt 8000000 ] || { err "low disk (<8GB free) — aborting"; exit 4; }
ok "disk ok"

if [ "$DRY" = "1" ]; then
  ok "dry-run OK — would train LoRA rank=$LORA_RANK iters=$ITERS on $BASE_MODEL, fuse -> GGUF($INCUMBENT_QUANT) -> ollama create local-candidate:$CYCLE_ID"
  exit 0
fi

# --- real training (path a) -------------------------------------------------
mkdir -p "$ADAPTER_OUT"
info "QLoRA train (rank=$LORA_RANK iters=$ITERS)"
"$VENV_PY" -m mlx_lm.lora \
  --model "$BASE_MODEL" --train --fine-tune-type lora \
  --data "$DATA" --iters "$ITERS" --adapter-path "$ADAPTER_OUT" \
  --learning-rate 1e-5

info "fuse adapter into base"
FUSED="$ADAPTER_OUT/fused"
"$VENV_PY" -m mlx_lm.fuse --model "$BASE_MODEL" --adapter-path "$ADAPTER_OUT" --save-path "$FUSED"

# GGUF convert + ollama shadow tag (requires llama.cpp convert + ollama on PATH).
info "convert to GGUF ($INCUMBENT_QUANT) and import as shadow tag"
GGUF="$ADAPTER_OUT/model-$INCUMBENT_QUANT.gguf"
if command -v llama-quantize >/dev/null 2>&1 && command -v ollama >/dev/null 2>&1; then
  "$VENV_PY" -m mlx_lm.convert --hf-path "$FUSED" --gguf "$GGUF" --quantize "$INCUMBENT_QUANT" 2>/dev/null \
    || { err "GGUF convert failed — inspect $FUSED"; exit 5; }
  printf 'FROM %s\n' "$GGUF" > "$ADAPTER_OUT/Modelfile"
  ollama create "local-candidate:$CYCLE_ID" -f "$ADAPTER_OUT/Modelfile"
  ok "shadow tag created: local-candidate:$CYCLE_ID — now run: python -m apex_router.qlora_serve evaluate --cycle-id $CYCLE_ID"
else
  err "llama-quantize/ollama not found — adapter fused at $FUSED; convert+import manually"
  exit 5
fi
