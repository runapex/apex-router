"""Repo-agnostic code retrieval for the Ornith code-Q&A harness.

The DETERMINISTIC tools traverse; Ornith only reads what they return. This module is
the "traverse" half — it turns a natural-language question into a small set of exact,
cited source chunks that get handed to Ornith as context.

Design (validated on a C++ and a Ruby repo):
  - HYBRID retrieval: symbol/keyword FIRST (ripgrep + optional clangd index), a vector
    similarity layer as a phase-2 fallback (seam present, see `vector.py`).
  - Repo-agnostic: everything repo-specific lives in codeqa/repos/<name>.json, so
    a C++ repo and a Ruby repo are two configs over one code path.
  - ripgrep is the common backbone (identical for C++ and Ruby); clangd/compile_commands
    is a C++-only accuracy bonus, not required.

Ornith's strength is verbatim fidelity over EXACT identifiers (measured), so keyword/
symbol retrieval is the primary path by design — it plays to that strength and avoids
the embedding-model dependency for the common case.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Repo configs live here. Default is the package's own `repos/`; CODEQA_REPOS overrides so the
# PACKAGED engine can read PRIVATE repo configs kept outside the repo (internal repo paths must not
# ship in the public package). `~` is expanded.
_repos_env = (os.environ.get("CODEQA_REPOS") or "").strip()
REPOS_DIR = Path(_repos_env).expanduser() if _repos_env else Path(__file__).resolve().parent / "repos"

# Identifier-ish tokens we extract from a question to drive symbol search.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
# Very common English words that are not useful code symbols.
_STOP = frozenset("""the and for how does what where which when who why with that this
    from into your you are was were has have had will would can could should does did
    doing done code file files function functions method methods class classes call
    calls called uses used using work works working handle handled handles happen
    happens flow show explain describe find look understand between across over under
    compute computes computed deliver delivers delivered delivery per name names key
    keys create creates created get gets getting set sets return returns store stores
    stored send sends read reads write writes list lists make makes made run runs
    process processes about into during before after each every some many much most
    node nodes data value values type types state states""".split())


class RetrievalError(RuntimeError):
    pass


@dataclass
class RepoConfig:
    name: str
    root: Path
    language: str
    digest: Path | None
    index: dict[str, Any]
    search_globs: list[str]
    exclude_globs: list[str]
    code_exts: list[str]
    definition_patterns: list[str]
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, name: str) -> "RepoConfig":
        p = REPOS_DIR / f"{name}.json"
        if not p.exists():
            avail = ", ".join(sorted(q.stem for q in REPOS_DIR.glob("*.json"))) or "(none)"
            raise RetrievalError(f"No repo config {name!r} in {REPOS_DIR} (have: {avail})")
        d = json.loads(p.read_text())
        root = Path(d["root"])
        if not root.exists():
            raise RetrievalError(f"Repo root does not exist: {root}")
        digest = Path(d["digest"]) if d.get("digest") else None
        return cls(
            name=d["name"], root=root, language=d.get("language", "unknown"),
            digest=digest if (digest and digest.exists()) else None,
            index=d.get("index", {"kind": "none"}),
            search_globs=d.get("search_globs", ["**"]),
            exclude_globs=d.get("exclude_globs", []),
            code_exts=d.get("code_exts", []),
            definition_patterns=d.get("definition_patterns", []),
            raw=d,
        )


@dataclass
class Chunk:
    file: str          # repo-relative path
    start: int         # 1-indexed first line
    end: int           # 1-indexed last line (inclusive)
    text: str
    why: str           # which retrieval signal surfaced this (symbol / keyword / vector)

    def cite(self) -> str:
        span = f"{self.start}" if self.start == self.end else f"{self.start}-{self.end}"
        return f"{self.file}:{span}"


def extract_symbols(question: str) -> list[str]:
    """Pull likely code identifiers out of a natural-language question.

    Prefers tokens that look like code: CamelCase, snake_case, ::-qualified, or ()-called.
    Falls back to any non-stopword identifier token. Order-preserving, de-duplicated.
    """
    # Explicitly code-shaped tokens first (highest precision).
    shaped = re.findall(r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)+"  # A::B
                        r"|[A-Za-z_][A-Za-z0-9_]*(?=\s*\()"                       # foo(
                        r"|[a-z]+_[a-z_]+"                                        # snake_case
                        r"|[A-Z][a-z]+[A-Z][A-Za-z]*", question)                  # CamelCase
    generic = [t for t in _IDENT.findall(question) if t.lower() not in _STOP]
    out: list[str] = []
    for t in [*shaped, *generic]:
        if t not in out:
            out.append(t)
    return out[:8]


def _rg(cfg: RepoConfig, pattern: str, *, fixed: bool, max_count: int) -> list[tuple[str, int, str]]:
    """Run ripgrep, return [(rel_path, line_no, line_text)]. Never raises on no-match."""
    # --sort path forces a STABLE, deterministic result order. Without it, ripgrep searches files in
    # parallel and returns them in nondeterministic completion order, so with --max-count caps the
    # surviving "top N" chunks vary run-to-run — the same question yields different answers, and the
    # impact A/B's retrieval-identity control (correctly) voids the run. --sort path serializes the
    # search (slower, but retrieval must be reproducible). Found via the A/B identity control, 2026-07-27.
    cmd = ["rg", "--line-number", "--no-heading", "--color", "never", "--sort", "path",
           "--max-count", str(max_count), "--max-columns", "400"]
    if fixed:
        cmd.append("--fixed-strings")
    else:
        cmd.append("--pcre2")
    # Path globs are the ONLY positive includes. ripgrep OToRs multiple positive
    # --glob includes, so mixing a path glob (app/**) with an extension glob (*.rb)
    # would match every .rb ANYWHERE — defeating the path whitelist. Instead we let
    # path globs define WHERE to search and filter extensions in Python below (a true
    # AND of path ∧ extension).
    for g in cfg.search_globs:
        cmd += ["--glob", g]
    for g in cfg.exclude_globs:
        cmd += ["--glob", f"!{g}"]
    # Search "." with cwd=root: ripgrep anchors relative --glob patterns to the CWD,
    # not to the search path. Passing an ABSOLUTE search path makes relative globs like
    # "webservices/**" silently match nothing. So we cd into the repo and search ".".
    cmd += [pattern, "."]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cfg.root))
    # rg exit 1 == no matches (normal); >1 == real error.
    if proc.returncode > 1:
        raise RetrievalError(f"ripgrep failed ({proc.returncode}): {proc.stderr.strip()[:200]}")
    exts = tuple(cfg.code_exts)
    hits: list[tuple[str, int, str]] = []
    for line in proc.stdout.splitlines():
        # format: ./rel/path:LINENO:content  (relative because we searched ".")
        try:
            path, lineno, content = line.split(":", 2)
        except ValueError:
            continue
        if exts and not path.endswith(exts):
            continue  # extension filter — the AND half the path globs can't express
        rel = str(Path(path.removeprefix("./")))
        hits.append((rel, int(lineno), content))
    return hits


def _read_window(cfg: RepoConfig, rel: str, center: int, radius: int) -> Chunk | None:
    p = cfg.root / rel
    try:
        lines = p.read_text(errors="replace").splitlines()
    except OSError:
        return None
    start = max(1, center - radius)
    end = min(len(lines), center + radius)
    text = "\n".join(lines[start - 1:end])
    return Chunk(file=rel, start=start, end=end, text=text, why="")


def find_definitions(cfg: RepoConfig, symbol: str, *, radius: int = 18) -> list[Chunk]:
    """Locate likely DEFINITION sites of a symbol using the repo's definition patterns."""
    chunks: list[Chunk] = []
    seen: set[tuple[str, int]] = set()
    for pat in cfg.definition_patterns:
        rx = pat.replace("{sym}", re.escape(symbol))
        for rel, lineno, _ in _rg(cfg, rx, fixed=False, max_count=3):
            key = (rel, lineno // (2 * radius))  # de-dupe near-adjacent hits
            if key in seen:
                continue
            seen.add(key)
            ch = _read_window(cfg, rel, lineno, radius)
            if ch:
                ch.why = f"definition of {symbol}"
                chunks.append(ch)
    return chunks


def find_references(cfg: RepoConfig, symbol: str, *, radius: int = 8, cap: int = 6) -> list[Chunk]:
    """Locate USE sites of a symbol (plain literal search)."""
    chunks: list[Chunk] = []
    seen: set[tuple[str, int]] = set()
    for rel, lineno, _ in _rg(cfg, symbol, fixed=True, max_count=4):
        bucket = (rel, lineno // (2 * radius))
        if bucket in seen:
            continue
        seen.add(bucket)
        ch = _read_window(cfg, rel, lineno, radius)
        if ch:
            ch.why = f"reference to {symbol}"
            chunks.append(ch)
        if len(chunks) >= cap:
            break
    return chunks


def retrieve(cfg: RepoConfig, question: str, *, max_chunks: int = 10,
             use_vector_fallback: bool = True) -> list[Chunk]:
    """HYBRID retrieval: symbol definitions + references, vector fallback if sparse.

    Returns a de-duplicated, size-bounded list of cited chunks ready to hand to Ornith.
    """
    symbols = extract_symbols(question)
    chunks: list[Chunk] = []
    for sym in symbols:
        chunks.extend(find_definitions(cfg, sym))
        if len(chunks) < max_chunks:
            chunks.extend(find_references(cfg, sym))
        if len(chunks) >= max_chunks:
            break

    # De-dupe by (file, overlapping span) and cap.
    deduped: list[Chunk] = []
    for ch in chunks:
        if any(o.file == ch.file and not (ch.end < o.start or ch.start > o.end) for o in deduped):
            continue
        deduped.append(ch)
    deduped = deduped[:max_chunks]

    # Phase-2 seam: if keyword retrieval came up sparse, try the vector layer.
    if use_vector_fallback and len(deduped) < 3:
        try:
            from . import vector  # local import: optional dependency
            deduped.extend(vector.similar_chunks(cfg, question, k=max_chunks - len(deduped)))
        except Exception:
            pass  # vector layer not built yet — keyword-only is the supported default

    return deduped


def load_digest(cfg: RepoConfig, *, max_bytes: int = 90_000) -> str:
    """Load the architecture digest that gets pinned as Ornith's frozen preamble.

    Bounded to stay inside Ornith's ≤100 KB/item envelope (router MAX_ITEM_BYTES).
    """
    if not cfg.digest:
        return f"(No architecture digest configured for {cfg.name}.)"
    text = cfg.digest.read_text(errors="replace")
    if len(text.encode()) > max_bytes:
        text = text.encode()[:max_bytes].decode(errors="ignore")
        text += "\n\n[digest truncated to fit Ornith item-size envelope]"
    return text
