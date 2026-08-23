# RUNBOOK — booksearch (local semantic references over your book library)

Index a folder of **PDF books plus code samples** (Jupyter notebooks, `.py`, `.m`,
`.r`, `.md`, `.txt`, and other source/text) locally and, while working on a problem,
pull the **Top-K most relevant references with a one-line reason for each**. PDFs are
located by page; notebooks/source/text by line span. Everything runs on this machine:

- **Retrieval:** `nomic-embed-text` via ollama (the same embed client the router uses).
- **Explanation:** the active **Ornith tier** via ollama.
- **Storage:** one SQLite file at `$BOOKSEARCH_DB` (default `~/.booksearch/index.sqlite`).

Nothing is sent to a remote model. DRM-protected Kindle files are *not* usable here
(encrypted); this works on readable PDFs (or any folder of `*.pdf`).

## 1. Install

```bash
# PDF extractor into the router venv (the only third-party dep):
uv pip install --python ~/.apex-router/.venv/bin/python 'apex-router[books]'
# or: the installer wires the wrapper + pi/claude commands for you
./install.sh --books-index
```

The `booksearch` command (a wrapper at `~/.local/bin/booksearch`) calls
`scripts/booksearch.py` with the router venv. Override the corpus with `BOOKS_DIR`
and the index location with `BOOKSEARCH_DB`.

## 2. Ingest (one-time, incremental, resumable)

```bash
booksearch ingest                    # indexes ~/books (override: --dir /path or BOOKS_DIR)
```

- **File types:** PDFs (by page) + `.ipynb` (cells flattened), `.py`, `.m`, `.r`, `.jl`,
  `.md`, `.txt`, `.sql`, and common source extensions (by line span). `.git`,
  `.ipynb_checkpoints`, `__pycache__`, `node_modules` and binaries are skipped.
- **Incremental:** re-running skips books whose path+mtime are unchanged; `--reindex`
  forces a rebuild.
- **Robust:** per-page/per-file errors are logged and skipped; scanned-image PDFs with
  no text layer are reported as `no extractable text`.
- **Chunking:** pages are packed into ≤1400-char chunks (long pages are split) so each
  fits nomic-embed's input window; the embed input is hard-capped as a final guard.
- **Big corpus:** it's a background-friendly one-time cost. Run it detached and watch:
  ```bash
  nohup booksearch ingest > ~/.booksearch/ingest.log 2>&1 &
  tail -f ~/.booksearch/ingest.log
  ```

**Memory note:** bulk ingest only needs `nomic-embed` (~0.4 GB). If a large Ornith
tier is resident on a small-RAM box it competes for memory; free it during ingest
with `apex-router ornith-tier --unload` (it reloads on demand when you next query).

## 3. Query — Top-K references while you work

```bash
booksearch query "deriving the chain rule for multivariable functions"
booksearch query "how do B-trees stay balanced?" -k 5
booksearch query "CAP theorem tradeoffs" --no-explain     # skip local-model reason (faster)
booksearch query "monads for error handling" --json       # machine-readable
```

Output is one entry per book: **title, page span, similarity score, a local-model
reason, and a snippet**. `-k` sets how many books; `--no-explain` drops the Ornith
call for speed.

## 4. Call it from pi / claude

Both agents can shell out, so the plain command works inside either:

```
!booksearch query "the problem I'm on"        # pi: run bash inline
```

For a first-class command that injects the references into the conversation:

**pi** — install the extension:
```bash
pi install ~/.apex-router/integrations/pi/booksearch.ts
# then, in a session:
/books how do B-trees stay balanced?
```

**Claude Code** — drop the slash command:
```bash
mkdir -p ~/.claude/commands && cp ~/.apex-router/integrations/claude/books.md ~/.claude/commands/
# then, in a session:
/books how do B-trees stay balanced?
```

## 5. Configuration

| Env | Default | Meaning |
|-----|---------|---------|
| `BOOKS_DIR` | `~/books` | corpus folder (`ingest` walks `*.pdf` recursively) |
| `BOOKSEARCH_DB` | `~/.booksearch/index.sqlite` | index location |
| `BOOKSEARCH_EMBED_MODEL` | `nomic-embed-text` | ollama embedding model |
| `BOOKSEARCH_CHUNK_CHARS` | `1400` | target chunk size |
| `BOOKSEARCH_BIN` (pi ext) | `~/.local/bin/booksearch` | wrapper path |

## 6. How it works

```
ingest:  *.pdf ──pypdf──► pages ──split/pack──► ≤1400-char chunks
                                   │
                          nomic-embed (ollama) ──► vectors ──► SQLite
query:   problem ──nomic-embed──► vector
                                   │  cosine vs every chunk
                          best chunk per book ──► Top-K
                                   │
                          Ornith tier (ollama) ──► one-line "why this book"
```
