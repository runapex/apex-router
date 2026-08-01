# apex-router

Adaptive model routing — measured, per-task-class model selection.

Instead of a hand-authored "use model X for task Y" table, `apex-router` learns which
model is actually best for each kind of task from evidence, behind a statistically
sound promotion gate, and routes to it — falling back to your hand-authored defaults
whenever the evidence is thin or uncertain. It is a **strict superset** of static
routing: it never routes to a model your machine can't run, and it defaults to your
static choice on any uncertainty.

## What it does

```
task → classify (§11) → cell → route table → resolve (fallback to static default) → model
                                    ▲
      corpus steps → replay bench → gate (out-of-sample, FDR-corrected) → route table
```

- **Pure-stdlib core.** The routing decision (classify → gate → route table → shim) has
  **zero third-party dependencies**. It runs on a machine that has only the Claude and
  Codex CLIs and no model server.
- **Sound by construction.** Promotions require an out-of-sample confirmation split,
  Benjamini-Hochberg FDR across cells, replication across capture windows, and a
  candidate that independently clears the gate — never a cheaper-but-worse model.
- **Portable route tables.** A table generated on one machine names the models it had;
  on a machine that lacks those (e.g. no Foundry, only Claude+Codex), the `known_models`
  gate falls back to a model that machine can actually run.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/runapex/apex-router/main/install.sh | bash
```

The installer is **arch-aware and idempotent**:

| Component | Installed | Notes |
|---|---|---|
| Python ≥3.11, uv | if missing | required |
| `apex-router` package | always | pure stdlib, near-instant |
| ollama + `nomic-embed-text` | if missing | embedding-refinement classifier (optional) |
| Ornith MLX server | **Apple Silicon only** | local replay bench / codegen; skipped with a notice elsewhere |
| starter route table | always | all-fallback, so routing works day one |

No Foundry is required anywhere — the target uses its own Claude + Codex subscriptions.
Pass `--no-ornith` to skip the large model download.

## Verify

```bash
apex-router status     # reports which tiers are live (routing / embedding / ornith)
apex-router verify     # exits 0 if routing works
```

## Optional extras

```bash
uv pip install "apex-router[ornith]"   # mlx-lm, Apple Silicon
uv pip install "apex-router[dev]"      # pytest
```

## License

MIT.
