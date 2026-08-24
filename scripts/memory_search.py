"""Memory search over a directory of markdown files via local embeddings.

Retrieval over a memory dir so archived memories are queryable (L2 cold tier).
Standard library only: json, math, sqlite3, sys, argparse, urllib.request,
pathlib, time, re.
"""

import argparse
import json
import math
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_DB = Path.home() / ".apex-router" / "memory_index.db"
EMBED_MODEL = "nomic-embed-text"
MAX_PROMPT_CHARS = 2000
FRONTMATTER_NAME = re.compile(r"^\s*name\s*:\s*(.+)$", re.MULTILINE)
FRONTMATTER_DESCRIPTION = re.compile(
    r"^\s*description\s*:\s*(.+)$", re.MULTILINE
)


class EmbedError(Exception):
    """Raised on transport or JSON parsing failures with Ollama."""

    pass


def _http_post(url: str, payload: dict) -> dict:
    """POST json `payload` to `url`, return parsed json dict.

    Uses urllib.request with a 30s timeout and Content-Type application/json.
    Raises EmbedError on transport/JSON failure.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except (OSError, ValueError, IOError) as exc:
        raise EmbedError(f"HTTP request failed: {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise EmbedError(f"Invalid JSON response: {exc}") from exc


def embed(text: str, model: str = EMBED_MODEL) -> list[float]:
    """Embed `text` via ollama /api/embeddings.

    Raises ValueError if text.strip() is empty.
    Builds url = OLLAMA_URL + "/api/embeddings" and payload
    {"model": model, "prompt": text}.
    Returns the "embedding" list of floats.
    Raises EmbedError if "embedding" is missing or not a non-empty list.
    """
    if not text.strip():
        raise ValueError("text must not be empty after stripping whitespace")
    url = f"{OLLAMA_URL}/api/embeddings"
    payload = {"model": model, "prompt": text}
    resp = _http_post(url, payload)
    embedding = resp.get("embedding")
    if not isinstance(embedding, list) or not embedding:
        raise EmbedError("response missing non-empty 'embedding' list")
    return embedding


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _extract_frontmatter(text: str):
    """Return (name, description) extracted from markdown frontmatter."""
    name_match = FRONTMATTER_NAME.search(text)
    desc_match = FRONTMATTER_DESCRIPTION.search(text)
    name = name_match.group(1).strip() if name_match else ""
    description = desc_match.group(1).strip() if desc_match else ""
    return name, description


def _build_prompt(name: str, description: str, body: str) -> str:
    """Compose the embedding prompt from name, description and body."""
    parts = [p for p in (name, description) if p]
    prompt = " ".join(parts)
    trimmed = body[:MAX_PROMPT_CHARS]
    if trimmed:
        prompt = f"{prompt} {trimmed}".strip()
    return prompt


def _init_db(conn: sqlite3.Connection) -> None:
    """Create the memories table if it does not yet exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            file_path TEXT PRIMARY KEY,
            mtime REAL,
            name TEXT,
            description TEXT,
            embedding TEXT
        )
        """
    )
    conn.commit()


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating parents as needed) the sqlite database."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    _init_db(conn)
    return conn


def _list_markdown_files(directory: Path):
    """Yield absolute paths to *.md files under `directory`."""
    for path in sorted(directory.rglob("*.md")):
        if path.is_file():
            yield path


def cmd_ingest(args: argparse.Namespace) -> int:
    """Ingest markdown files into the memory index."""
    directory = Path(args.dir)
    if not directory.is_dir():
        print(f"error: directory not found: {directory}", file=sys.stderr)
        return 2

    db_path = Path(args.db) if args.db else DEFAULT_DB
    conn = _connect(db_path)
    try:
        # Removal scope: only rows UNDER the ingested dir. Several memory dirs share one
        # index — scoping removal to this dir's subtree keeps a sibling dir's rows intact
        # (a global vanish-check made every ingest delete the previous dir's memories).
        dir_prefix = str(directory.resolve())
        existing = {
            row[0]
            for row in conn.execute("SELECT file_path FROM memories")
            if row[0] == dir_prefix or row[0].startswith(dir_prefix + "/")
        }

        added = 0
        skipped = 0
        seen: set = set()

        for file_path in _list_markdown_files(directory):
            abs_path = str(file_path.resolve())
            seen.add(abs_path)
            try:
                mtime = file_path.stat().st_mtime
            except OSError as exc:
                print(
                    f"warning: cannot stat {file_path}: {exc}",
                    file=sys.stderr,
                )
                continue

            if abs_path in existing and mtime <= mtime_for(conn, abs_path):
                skipped += 1
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(
                    f"warning: cannot read {file_path}: {exc}",
                    file=sys.stderr,
                )
                continue

            name, description = _extract_frontmatter(content)
            prompt = _build_prompt(name, description, content)
            try:
                embedding = embed(prompt)
            except (EmbedError, ValueError) as exc:
                print(
                    f"warning: embedding failed for {file_path}: {exc}",
                    file=sys.stderr,
                )
                skipped += 1
                continue

            embedding_json = json.dumps(embedding)
            conn.execute(
                """
                INSERT INTO memories
                    (file_path, mtime, name, description, embedding)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    mtime = excluded.mtime,
                    name = excluded.name,
                    description = excluded.description,
                    embedding = excluded.embedding
                """,
                (abs_path, mtime, name, description, embedding_json),
            )
            added += 1

        conn.commit()

        vanished = existing - seen
        removed = 0
        for abs_path in vanished:
            cur = conn.execute(
                "DELETE FROM memories WHERE file_path = ?", (abs_path,)
            )
            removed += cur.rowcount

        if removed:
            conn.commit()

        print(f"added: {added}")
        print(f"skipped: {skipped}")
        print(f"removed: {removed}")
        return 0
    finally:
        conn.close()


def mtime_for(conn: sqlite3.Connection, file_path: str) -> float:
    """Return the stored mtime for `file_path`, or -1.0 if absent."""
    row = conn.execute(
        "SELECT mtime FROM memories WHERE file_path = ?", (file_path,)
    ).fetchone()
    if row is None:
        return -1.0
    return row[0] or -1.0


def cmd_query(args: argparse.Namespace) -> int:
    """Query the memory index for the top-k most similar memories."""
    db_path = Path(args.db) if args.db else DEFAULT_DB
    if not db_path.exists():
        print(
            f"error: no memory index at {db_path} — run: memory_search.py ingest --dir <memory_dir>",
            file=sys.stderr,
        )
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT file_path, name, description, embedding FROM memories"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print(
            "error: memory index is empty — run: memory_search.py ingest --dir <memory_dir>",
            file=sys.stderr,
        )
        return 2

    try:
        qv = embed(args.text)
    except (EmbedError, ValueError) as exc:
        print(f"error: embedding the query failed: {exc}", file=sys.stderr)
        return 2

    scored = []
    for file_path, name, description, embedding_json in rows:
        try:
            ev = json.loads(embedding_json)
        except (ValueError, TypeError):
            continue
        scored.append((_cosine(qv, ev), file_path, name or "", description or ""))
    scored.sort(key=lambda r: r[0], reverse=True)
    top = scored[: max(1, args.k)]

    if args.json:
        print(json.dumps([
            {"score": round(s, 4), "file": f, "name": n, "description": d}
            for s, f, n, d in top
        ], indent=2))
    else:
        home = str(Path.home())
        for s, f, n, d in top:
            rel = ("~" + f[len(home):]) if f.startswith(home) else f
            label = f"{n} — {d}" if n or d else ""
            print(f"{s:.3f}  {rel}  {label}".rstrip())
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Hierarchical-memory retrieval (L2 cold tier): embed a memory dir "
                    "locally and query it, so archived memories need not live in the prefix.")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("ingest", help="embed *.md files under --dir into the index")
    pi.add_argument("--dir", required=True)
    pi.add_argument("--db", default=None)
    pq = sub.add_parser("query", help="top-k most similar memories for a query")
    pq.add_argument("text")
    pq.add_argument("-k", type=int, default=5)
    pq.add_argument("--json", action="store_true")
    pq.add_argument("--db", default=None)
    args = p.parse_args(argv)
    if args.cmd == "ingest":
        return cmd_ingest(args)
    return cmd_query(args)


if __name__ == "__main__":
    sys.exit(main())
