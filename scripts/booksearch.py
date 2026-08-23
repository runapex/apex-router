#!/usr/bin/env python3
"""booksearch — local semantic index over a folder of PDF books, with local-model
Top-K references + explanations. 100% local: nomic-embed (ollama) for vectors,
the active Ornith tier (ollama) for the "why this book" explanation.

    # one-time (resumable, incremental): extract text -> chunk -> embed -> store
    booksearch ingest                      # indexes $BOOKS_DIR (default ~/books)

    # when working on a problem: Top-K book references with a local-model reason
    booksearch query "deriving the chain rule for multivariable functions"
    booksearch query "how do B-trees keep balanced?" -k 5 --json

Storage: a single SQLite file at $BOOKSEARCH_DB (default ~/.booksearch/index.sqlite).
Re-running `ingest` skips books whose path+mtime are already indexed; `--reindex`
forces a rebuild. Nothing leaves the machine.

Designed to be called from pi or the claude CLI as a plain bash command (both can
shell out): e.g. inside pi type `!booksearch query "<problem>"`.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

# pypdf logs recoverable xref/trailer repairs at WARNING ("Previous trailer cannot be
# read", "parsing for Object Streams") — noise on malformed-but-readable PDFs. Silence
# it so the ingest log shows only real progress + genuine errors.
logging.getLogger("pypdf").setLevel(logging.ERROR)
# Line-buffer stdout so `+ <title>` progress reaches a redirected log in real time
# instead of sitting in a block buffer until the process exits.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# Reuse apex-router's local, stdlib embed client + local Ornith chat client.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from apex_router.embed import embed, cosine, EmbedError  # noqa: E402

BOOKS_DIR = Path(os.environ.get("BOOKS_DIR", str(Path.home() / "books")))
DB_PATH = Path(os.environ.get("BOOKSEARCH_DB", str(Path.home() / ".booksearch" / "index.sqlite")))
# nomic-embed-text (this ollama build) 500s on prompts over ~2000 chars, so keep
# chunks comfortably under that and hard-cap the embed input as a final guard.
CHUNK_CHARS = int(os.environ.get("BOOKSEARCH_CHUNK_CHARS", "1400"))
EMBED_CHAR_CAP = int(os.environ.get("BOOKSEARCH_EMBED_CAP", "1900"))
EMBED_MODEL = os.environ.get("BOOKSEARCH_EMBED_MODEL", "nomic-embed-text")


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(str(DB_PATH))
    cx.execute("PRAGMA journal_mode=WAL")
    # FK enforcement is OFF by default in sqlite3, so `ON DELETE CASCADE` would NOT
    # fire and reindex/stale-replace would orphan chunk rows. Enable it per-connection.
    cx.execute("PRAGMA foreign_keys=ON")
    cx.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY, path TEXT UNIQUE, title TEXT,
            mtime REAL, n_chunks INTEGER, indexed_at REAL
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY, book_id INTEGER, ord INTEGER,
            page_start INTEGER, page_end INTEGER, text TEXT, embedding TEXT,
            FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS chunks_book ON chunks(book_id);
        """
    )
    return cx


def title_of(path: Path) -> str:
    return path.stem.replace("_", " ").strip()


def embed_retry(text: str, tries: int = 5):
    """Embed with backoff. Under memory pressure (a large tier resident) ollama can
    return transient 500s; retrying rather than dropping the chunk keeps the index
    complete. Raises EmbedError only after `tries` failures."""
    delay = 0.5
    last: Exception | None = None
    for _ in range(tries):
        try:
            return embed(text, model=EMBED_MODEL)
        except EmbedError as exc:
            last = exc
            time.sleep(delay)
            delay = min(delay * 2, 8)
    raise last if last else EmbedError("embed failed")


# --------------------------------------------------------------------------- #
# PDF -> pages -> chunks
# --------------------------------------------------------------------------- #
def extract_pages(path: Path, max_pages: int | None):
    """Yield (page_number, text) for a PDF. Silent on per-page decode errors."""
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.exit("pypdf not installed. Run:  uv pip install --python <venv> pypdf")
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        print(f"  ! cannot open {path.name}: {exc}", file=sys.stderr)
        return
    for i, page in enumerate(reader.pages):
        if max_pages is not None and i >= max_pages:
            break
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        if txt.strip():
            yield i + 1, txt


def _slice_text(txt: str, size: int):
    """Split a long page into <=size pieces on whitespace where possible."""
    txt = txt.strip()
    while len(txt) > size:
        cut = txt.rfind(" ", 0, size)
        if cut < size // 2:  # no good break point — hard cut
            cut = size
        yield txt[:cut].strip()
        txt = txt[cut:].strip()
    if txt:
        yield txt


def chunk_pages(pages, target_chars: int):
    """Pack page texts into <=target_chars chunks, splitting long pages, tracking
    page span. Flushes BEFORE a piece would overflow, so no chunk exceeds target."""
    buf, start_pg, end_pg, size = [], None, None, 0
    for pg, txt in pages:
        for piece in _slice_text(txt, target_chars):
            if size and size + len(piece) > target_chars:
                yield start_pg, end_pg, "\n".join(buf)
                buf, start_pg, end_pg, size = [], None, None, 0
            if start_pg is None:
                start_pg = pg
            end_pg = pg
            buf.append(piece)
            size += len(piece)
    if buf:
        yield start_pg, end_pg, "\n".join(buf)


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #
def _check_embed_model(cx, reindex: bool) -> None:
    """Guard against mixing vector spaces: all chunks must share one embed model.
    First ingest records the model; a later mismatch refuses unless --reindex (which
    wipes and rebuilds under the new model)."""
    row = cx.execute("SELECT value FROM meta WHERE key='embed_model'").fetchone()
    if row is None:
        cx.execute("INSERT OR REPLACE INTO meta VALUES('embed_model',?)", (EMBED_MODEL,))
        cx.commit()
    elif row[0] != EMBED_MODEL:
        if not reindex:
            sys.exit(
                f"index was built with embed model '{row[0]}' but EMBED_MODEL='{EMBED_MODEL}'.\n"
                f"Mixing models corrupts cosine scores. Re-run with --reindex to rebuild."
            )
        cx.execute("DELETE FROM books")  # FK cascade clears chunks
        cx.execute("INSERT OR REPLACE INTO meta VALUES('embed_model',?)", (EMBED_MODEL,))
        cx.commit()


def cmd_ingest(args) -> int:
    root = Path(args.dir).expanduser()
    if not root.is_dir():
        sys.exit(f"books dir not found: {root}")
    cx = db_connect()
    _check_embed_model(cx, args.reindex)
    pdfs = sorted(p for p in root.rglob("*.pdf"))
    if args.limit:
        pdfs = pdfs[: args.limit]
    print(f"scanning {len(pdfs)} PDFs under {root}")
    n_new = n_skip = n_empty = n_fail = n_chunks = 0
    for path in pdfs:
        try:
            mtime = path.stat().st_mtime
            row = cx.execute("SELECT id, mtime FROM books WHERE path=?", (str(path),)).fetchone()
            if row and abs(row[1] - mtime) < 1 and not args.reindex:
                n_skip += 1
                continue
            if row:  # stale or forced -> replace (FK cascade clears its chunks)
                cx.execute("DELETE FROM books WHERE id=?", (row[0],))
            title = title_of(path)
            chunks = list(chunk_pages(extract_pages(path, args.max_pages), CHUNK_CHARS))
            if args.max_chunks:
                chunks = chunks[: args.max_chunks]
            if not chunks:
                n_empty += 1
                print(f"  - {title}: no extractable text (scanned image?) — skipped")
                continue
            cur = cx.execute(
                "INSERT INTO books(path,title,mtime,n_chunks,indexed_at) VALUES(?,?,?,?,?)",
                (str(path), title, mtime, len(chunks), time.time()),
            )
            book_id = cur.lastrowid
            inserted = 0
            for ordi, (ps, pe, text) in enumerate(chunks):
                try:
                    vec = embed_retry(text[:EMBED_CHAR_CAP])
                except (EmbedError, ValueError) as exc:
                    print(f"  ! embed failed ({title} p{ps}): {exc}", file=sys.stderr)
                    continue
                cx.execute(
                    "INSERT INTO chunks(book_id,ord,page_start,page_end,text,embedding) VALUES(?,?,?,?,?,?)",
                    (book_id, ordi, ps, pe, text, json.dumps(vec)),
                )
                inserted += 1
            if inserted == 0:
                # every chunk failed to embed -> drop the book row so a later run RETRIES
                # it instead of skipping on the recorded mtime.
                cx.execute("DELETE FROM books WHERE id=?", (book_id,))
                cx.commit()
                n_fail += 1
                print(f"  ! {title}: all {len(chunks)} chunks failed to embed — not indexed (will retry)")
                continue
            cx.execute("UPDATE books SET n_chunks=? WHERE id=?", (inserted, book_id))
            cx.commit()
            n_new += 1
            n_chunks += inserted
            print(f"  + {title}: {inserted} chunks (pp {chunks[0][0]}–{chunks[-1][1]})")
        except Exception as exc:  # noqa: BLE001 — isolate one bad PDF from the batch
            cx.rollback()
            print(f"  ! {path.name}: skipped ({type(exc).__name__}: {exc})", file=sys.stderr)
            continue
    print(f"\ndone: {n_new} indexed, {n_skip} unchanged, {n_empty} no-text, "
          f"{n_fail} embed-failed, {n_chunks} new chunks")
    print(f"index: {DB_PATH}")
    return 0


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #
def cmd_query(args) -> int:
    # NOTE: linear scan + per-chunk json.loads. Fine for a personal library (tens of
    # thousands of chunks); for much larger corpora swap in an ANN index (e.g. sqlite-vec).
    cx = db_connect()
    total = cx.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if total == 0:
        sys.exit("index is empty — run:  booksearch ingest")
    try:
        qvec = embed(args.text, model=EMBED_MODEL)
    except (EmbedError, ValueError) as exc:
        sys.exit(f"could not embed query: {exc}")

    # score every chunk; keep the best chunk per book (a "reference" = a book).
    best: dict[int, dict] = {}
    rows = cx.execute(
        "SELECT c.book_id,b.title,b.path,c.page_start,c.page_end,c.text,c.embedding "
        "FROM chunks c JOIN books b ON b.id=c.book_id"
    )
    for book_id, title, path, ps, pe, text, emb in rows:
        score = cosine(qvec, json.loads(emb))
        cur = best.get(book_id)
        if cur is None or score > cur["score"]:
            best[book_id] = {"title": title, "path": path, "page_start": ps,
                             "page_end": pe, "text": text, "score": score}
    top = sorted(best.values(), key=lambda r: r["score"], reverse=True)[: args.k]

    if args.explain:
        for r in top:
            r["why"] = explain(args.text, r)

    if args.json:
        out = [{k: v for k, v in r.items() if k != "text"} for r in top]
        print(json.dumps({"query": args.text, "results": out}, indent=2))
        return 0

    print(f"\nTop {len(top)} local-book references for:\n  “{args.text}”\n")
    for i, r in enumerate(top, 1):
        loc = f"p.{r['page_start']}" + (f"–{r['page_end']}" if r["page_end"] != r["page_start"] else "")
        print(f"[{i}] {r['title']}  ({loc}, score {r['score']:.3f})")
        if r.get("why"):
            print(f"    → {r['why']}")
        snippet = " ".join(r["text"].split())[:200]
        print(f"    …{snippet}…\n")
    return 0


def explain(problem: str, ref: dict) -> str:
    """Local Ornith one-liner: why is this book relevant to the problem?"""
    try:
        from apex_router.ornith.ornith_client import chat
    except Exception as exc:  # noqa: BLE001
        return f"(explanation unavailable: {exc})"
    snippet = " ".join(ref["text"].split())[:1200]
    prompt = (
        "You recommend reference books. In ONE sentence (no preamble), say why this "
        f"book passage helps with the problem.\n\nProblem: {problem}\n\n"
        f"Book: {ref['title']}\nPassage: {snippet}\n\nOne-sentence reason:"
    )
    try:
        res = chat(prompt, max_tokens=120, enable_thinking=False, raise_on_truncation=False)
        return " ".join((res.answer or "").split()) or "(no reason returned)"
    except Exception as exc:  # noqa: BLE001
        return f"(explanation unavailable: {exc})"


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="booksearch", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="index PDFs under a folder (incremental)")
    pi.add_argument("--dir", default=str(BOOKS_DIR), help=f"books folder (default {BOOKS_DIR})")
    pi.add_argument("--reindex", action="store_true", help="re-embed even unchanged books")
    pi.add_argument("--limit", type=int, default=0, help="only the first N PDFs (debug)")
    pi.add_argument("--max-pages", type=int, default=None, help="cap pages read per book")
    pi.add_argument("--max-chunks", type=int, default=None, help="cap chunks stored per book")
    pi.set_defaults(func=cmd_ingest)

    pq = sub.add_parser("query", help="Top-K book references for a problem")
    pq.add_argument("text", help="the problem / question")
    pq.add_argument("-k", type=int, default=5, help="number of book references (default 5)")
    pq.add_argument("--no-explain", dest="explain", action="store_false",
                    help="skip the local-model explanation (faster)")
    pq.add_argument("--json", action="store_true", help="machine-readable output")
    pq.set_defaults(func=cmd_query)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
