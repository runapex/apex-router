# apex-router

Adaptive model routing — measured, per-task-class model selection — plus the local
code-Q&A / freshness toolkit the routing evidence is built from.

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

## The toolkit

Alongside the routing core, `apex-router` bundles the local tools the routing evidence is
built from:

- **`apex_router.codeqa`** — grounded code-Q&A over a repo (ripgrep retrieval + a local
  model answering with `file:line` citations) and a **freshness gate** that checks a
  doc/digest's claims against the live code. Run: `python -m apex_router.codeqa.cli ask …`
  / `… validate …`.
- **`apex_router.ornith`** — a thin client + batch/codegen helpers for a local MLX model
  server (the offline answerer/bench backend), plus a capability-fit router. Apple Silicon
  for the server; the client is portable.

**Security posture — no agentic grading of untrusted code.** codeqa's frontier
"judge"/verifier is **opt-in and HTTP-only**: you point `CODEQA_JUDGE_BASE` at an
Anthropic-messages endpoint you control. It deliberately does **not** route grading
through the local `claude`/`codex` CLIs — those are agentic (tools, hooks, MCP), and
scanned source may be adversarial, so feeding it to an agentic CLI could trigger code
execution. With no endpoint configured, codeqa uses its **local verifier** instead. The
HTTP path strips credentials on cross-origin redirects, bounds response size, and warns on
plaintext `http://`.

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
