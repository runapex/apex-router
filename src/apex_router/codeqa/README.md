# codeqa — repo-agnostic code Q&A over local Ornith

Ask natural-language questions about a codebase and get **grounded, file:line-cited**
answers from the local Ornith model. Deterministic tools do the traversal; Ornith only
reads the retrieved chunks and answers.

## Why it's built this way

Ornith is a stateless MLX chat server (`:8080`) — it has **no tools, no file access, and
the Claude Agent tool cannot dispatch to it**. It also can't reliably *reason* about
architecture (measured: it asserts wrong root causes). Its strength is **verbatim
fidelity over exact identifiers**. So this harness plays to that:

```
question ─► retriever (ripgrep + optional clangd index)  ─► exact cited chunks
                                                              │
        architecture digest (frozen preamble, cache-reused) ─┤
                                                              ▼
                                                     Ornith answers, cites file:line
```

- **Hybrid retrieval:** symbol/keyword first (plays to Ornith's fidelity strength);
  vector-similarity fallback is a phase-2 seam (`vector.py`) for keyword-less questions.
- **Frozen-preamble cache reuse:** the repo's architecture digest is pinned as the system
  turn of every request, so mlx's PromptTrie serves it from cache (measured here:
  **8169/10127 prompt tokens cached** on a warm question). This is the
  `ornith_batch.batch_over_preamble` pattern applied to code Q&A.
- **Repo-agnostic:** all repo specifics live in `repos/<name>.json`. C++ (sample-cpp) and
  Ruby (sample-ruby) are two configs over one code path.

## Answers are advisory — verify at ground truth

Per the measured Ornith posture, treat answers as a **cheap first pass with citations you
verify**, not an autonomous authority. Every claim carries a `file:line`; open it. For
anything load-bearing, confirm with Opus/human at the cited source.

## Usage

```bash
cd <your-repo>
PY=python

$PY -m codeqa.cli repos                                   # list registered repos
$PY -m codeqa.cli retrieve sample-cpp "how is rollback done" # show retrieved chunks (no Ornith)
$PY -m codeqa.cli ask      sample-cpp "What does Firewall::reset do?"
$PY -m codeqa.cli batch    sample-cpp questions.txt          # one Q/line, digest cache-reused
```

Flags: `--max-tokens N` (default 1200 — **do not set too low**: a truncated answer raises
`OrnithProtocolError(finish_reason=length)`; 1200 is a safe floor for a cited answer).
`--think` enables Ornith reasoning (slow; synthesis questions only — thinking + tight
budget = no answer).

## Registering a repo

Drop `repos/<name>.json`:

```json
{
  "name": "<name>", "root": "/abs/path", "language": "cpp|ruby|…",
  "digest": "/abs/path/to/architecture-digest.md",
  "index": {"kind": "clangd|none", "compile_commands": "…optional…"},
  "search_globs": ["app/**", "lib/**"], "exclude_globs": ["vendor/**", "**/test/**"],
  "code_exts": [".rb"],
  "definition_patterns": ["(class|module)\\s+{sym}\\b", "def\\s+(self\\.)?{sym}\\b"]
}
```

`{sym}` is substituted with the searched identifier (regex-escaped). `definition_patterns`
are how the retriever finds *definition* sites vs. plain references — tune per language.

## Registered repos

| Repo | Lang | Digest | Index | Status |
|---|---|---|---|---|
| sample-cpp | C++ | `docs/architecture/sample-cpp-architecture.md` ✓ | clangd (`cmake-index/`) | **live, tested** |
| sample-ruby | Ruby | `docs/architecture/sample-ruby-architecture.md` ✓ | ripgrep-only | **live, tested** |

Both repos are at full parity — grounded digest + retrieval + live Ornith Q&A, verified
end-to-end with prompt-cache reuse active (6–8k tokens/question served from cache).

## Two retrieval bugs fixed during sample-ruby bring-up (both affected sample-cpp too)

Surfaced by a live sample-ruby test that returned junk (build scripts, `obscure.rb`):
1. **Extension globs OR'd with path globs.** ripgrep OToRs multiple positive `--glob`
   includes, so passing `*.rb` alongside `app/**` matched every Ruby file anywhere,
   defeating the path whitelist. Fix: path globs are the only rg includes; extensions
   are filtered in Python (a true path ∧ extension AND).
2. **Absolute search path silently voided relative globs.** rg anchors relative `--glob`
   patterns to the CWD, not the search path — so searching an absolute repo root with
   `--glob webservices/**` matched nothing (exit 0, no output). Fix: run rg with
   `cwd=repo_root` and search `.`.
Also expanded the stopword list so generic question words ("compute", "policy", "deliver")
aren't treated as code symbols.

## Files
- `retriever.py` — repo-agnostic ripgrep/index retrieval → cited `Chunk`s
- `driver.py` — retrieve → pin digest preamble → Ornith; `ask()` / `ask_many()`
- `vector.py` — phase-2 vector-similarity fallback (seam/stub)
- `cli.py` — `ask` / `batch` / `retrieve` / `repos`
- `repos/*.json` — per-repo configs
