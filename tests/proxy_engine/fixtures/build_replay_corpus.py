"""Replay-corpus builder — the proxy_engine tuner test fixture.

Turns Claude-Code-style transcript JSONL into the `Request` corpus the composition/compiler tests
replay. It is the SOURCE of the synthetic (and, where present, real) corpora those tests profile,
so it must agree with the production freeze decomposition (`session_frontiers` / `diagnose` in
`apex_router.proxy_engine.tuner.composition`) turn-for-turn. It reuses the production content-class
and byte-stratum functions directly (never a private copy that could drift), and adds a genuine
memory-bounded STREAMING profiler proven equivalent to the batch path in the tests.

Contract distilled from the tests:
  - A request is the bytes the client sent to PRODUCE an assistant turn, so its `content` ends on
    the triggering USER turn and never contains the assistant completion (test_corpus_frontier_phase).
  - `content` is the growing concatenation of the session's user whole-messages, with
    `message_boundaries` marking each message end (so `session_frontiers` extracts the frontier
    message-structurally; on this append-only shape that is byte-identical to byte subtraction).
  - REGIME is a per-session, band-tied property (`session_regime`); PROJECT is a separate row label.
  - The compact/streaming corpus carries each turn's frontier block + context byte length instead of
    the full growing prefix, so `diagnose` is identical while memory is O(final transcript), not O(Σ prefixes).
  - `CorpusStats.canonical` is authority-side and fail-closed: defaults to False; only a full
    (`limit_sessions=None`) build stamps it True.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import accumulate
from pathlib import Path
from typing import Iterator

# Reuse the PRODUCTION content-class + byte-stratum + token functions so the fixture cannot drift
# from what the compiler and runtime actually route on.
from apex_router.proxy_engine.policy import classify, size_stratum_bytes
from apex_router.proxy_engine.tuner.replay import Request
from apex_router.proxy_engine.tuner.tokens import estimate_tokens

# Where real captured Claude Code transcripts live, when a test names a project without a projects_root
# (the real-corpus equivalence tests). Absent on a fresh machine → those builds return an empty corpus
# and the tests no-op rather than erroring.
DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"


# --------------------------------------------------------------------------- regime + stats

def session_regime(n_turns: int) -> str:
    """Band-tied session-regime classifier (lesson #9): 1 turn = single, 2..12 = shallow,
    >=13 = conversational. Regime is a MEASURED session property, not a project-name assumption."""
    if n_turns <= 1:
        return "single"
    if n_turns <= 12:
        return "shallow"
    return "conversational"


@dataclass
class CorpusStats:
    """Provenance-bearing stats for a built corpus.

    `canonical` is AUTHORITY-CLASS (it gates evidence-grade signing via CorpusProvenance.from_stats),
    so it defaults to False — fail-closed. A truncated (`limit_sessions=N`) build must never
    masquerade as the canonical population; only the full sorted glob is canonical.
    """

    n_sessions: int
    n_requests: int
    total_bytes: int
    max_tokens: int
    canonical: bool = False
    source: str = "build_corpus"


# --------------------------------------------------------------------------- transcript parsing

def _read_jsonl(path: str) -> Iterator[dict]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


def _block_text(block) -> str:
    """Textual payload of one content block — what `classify` sees. Handles the shapes real and
    synthetic transcripts use: a raw string, a `tool_result` {"content": str|list}, a text block,
    or a tool_use {"input": {...}} (serialized as JSON)."""
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(_block_text(b) for b in c)
    if isinstance(block.get("text"), str):
        return block["text"]
    if "input" in block:
        return json.dumps(block["input"], sort_keys=True)
    return ""


def _user_text(rec: dict) -> str:
    """The user turn's text — the frontier of the request it triggers."""
    msg = rec.get("message", rec)
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_block_text(b) for b in content)
    return ""


def _iter_turns(path: str) -> Iterator[tuple[list[bytes], str, int]]:
    """Yield one (new_user_messages, model, prompt_tokens) per assistant turn.

    `new_user_messages` are the whole user messages appended since the previous assistant turn (the
    frontier of the request that produced this assistant completion). The assistant completion itself
    is NEVER part of any request's content — on the live wire the request ends on the newest user turn.
    """
    pending: list[bytes] = []
    for rec in _read_jsonl(path):
        t = rec.get("type")
        if t == "user":
            pending.append(_user_text(rec).encode("utf-8"))
        elif t == "assistant":
            msg = rec.get("message", {})
            model = msg.get("model", "") or ""
            usage = msg.get("usage", {}) or {}
            tokens = int(usage.get("input_tokens", 0) or 0)
            yield pending, model, tokens
            pending = []


def _count_turns(path: str) -> int:
    return sum(1 for rec in _read_jsonl(path) if rec.get("type") == "assistant")


# --------------------------------------------------------------------------- per-session builders

def transcript_to_requests(path: str, project: str | None = None) -> list[Request]:
    """One session's transcript → growing-prefix `Request`s (batch corpus). Each request's `content`
    is the concatenation of the user whole-messages up to and including its triggering turn, with
    `message_boundaries` at each message end; `regime` is the whole session's band-tied regime."""
    parts: list[bytes] = []
    specs: list[tuple[bytes, tuple[int, ...], str, int, float]] = []
    for ti, (new_msgs, model, tokens) in enumerate(_iter_turns(path)):
        parts.extend(new_msgs)
        content = b"".join(parts)
        boundaries = tuple(accumulate(len(p) for p in parts))
        specs.append((content, boundaries, model, tokens, float(ti)))
    regime = session_regime(len(specs))
    return [
        Request(session_id=path, content=content, tokens=tokens, ts=ts,
                model=model or "unknown", message_boundaries=boundaries,
                regime=regime, project=project)
        for (content, boundaries, model, tokens, ts) in specs
    ]


def transcript_to_frontier_requests(path: str, project: str | None = None) -> list[Request]:
    """One session's transcript → COMPACT frontier `Request`s: `content=b""`, the turn's
    `frontier_block` and its `context_bytes` (the full growing-prefix length) carried directly, so
    `session_frontiers`/`diagnose` produce exactly the batch result without materializing prefixes."""
    parts: list[bytes] = []
    specs: list[tuple[bytes, int, str, int, float]] = []
    for ti, (new_msgs, model, tokens) in enumerate(_iter_turns(path)):
        parts.extend(new_msgs)
        content_len = sum(len(p) for p in parts)
        frontier = b"".join(new_msgs)
        specs.append((frontier, content_len, model, tokens, float(ti)))
    regime = session_regime(len(specs))
    return [
        Request(session_id=path, content=b"", tokens=tokens, ts=ts, model=model or "unknown",
                frontier_block=frontier, context_bytes=content_len, diverged_hint=False,
                regime=regime, project=project)
        for (frontier, content_len, model, tokens, ts) in specs
    ]


def stream_transcript_frontiers(path: str) -> Iterator[tuple[bytes, int, str]]:
    """Streaming, memory-bounded frontier extraction: yield (frontier_block, context_bytes, regime)
    per turn while holding only a running length counter and the current block — never the growing
    prefixes (the O(n²)-memory batch path OOM-killed a 6818-turn session). Behaviorally identical to
    `session_frontiers(transcript_to_requests(path))` on the append-only transcript shape."""
    regime = session_regime(_count_turns(path))
    running = 0
    for new_msgs, _model, _tokens in _iter_turns(path):
        block = b"".join(new_msgs)
        running += len(block)
        yield block, running, regime


# --------------------------------------------------------------------------- corpus assembly

def _select_files(project: str, projects_root: str | None, limit_sessions: int | None,
                  exclude_contaminated: bool) -> list[Path]:
    root = Path(projects_root) if projects_root else DEFAULT_PROJECTS_ROOT
    proj_dir = root / project
    if not proj_dir.is_dir():
        return []
    files = sorted(proj_dir.glob("*.jsonl"))
    if exclude_contaminated:
        files = [f for f in files if not _is_contaminated(f)]
    if limit_sessions is not None:
        files = files[:limit_sessions]
    return files


def _is_contaminated(path: Path) -> bool:
    """A session is contaminated if its transcript declares it (a `{"contaminated": true}` marker on
    the first record). Belt-and-suspenders for the real-corpus builds; synthetic corpora never set it."""
    for rec in _read_jsonl(str(path)):
        return bool(rec.get("contaminated"))
    return False


def build_corpus(project: str, *, projects_root: str | None = None, min_turns: int = 1,
                 limit_sessions: int | None = None,
                 exclude_contaminated: bool = False) -> tuple[list[Request], CorpusStats]:
    """Build a batch replay corpus from every session of `project`. Sessions shorter than
    `min_turns` are dropped. `canonical` is True only for the full population (`limit_sessions=None`);
    a truncation is a probe and stamps canonical=False so it can never sign an evidence-grade bundle."""
    corpus: list[Request] = []
    n_sessions = 0
    for f in _select_files(project, projects_root, limit_sessions, exclude_contaminated):
        reqs = transcript_to_requests(str(f), project=project)
        if len(reqs) < min_turns:
            continue
        corpus.extend(reqs)
        n_sessions += 1
    stats = CorpusStats(
        n_sessions=n_sessions,
        n_requests=len(corpus),
        total_bytes=sum(len(r.content) for r in corpus),
        max_tokens=max((r.tokens for r in corpus), default=0),
        canonical=(limit_sessions is None),
        source=(f"build_corpus(limit_sessions={limit_sessions})"
                if limit_sessions is not None else "build_corpus"),
    )
    return corpus, stats


def build_streaming_corpus(project: str, *, projects_root: str | None = None, min_turns: int = 1,
                           limit_sessions: int | None = None,
                           exclude_contaminated: bool = False) -> tuple[list[Request], CorpusStats]:
    """Like `build_corpus` but with COMPACT frontier rows — same `diagnose` result, O(final transcript)
    memory instead of O(Σ growing prefixes)."""
    corpus: list[Request] = []
    n_sessions = 0
    for f in _select_files(project, projects_root, limit_sessions, exclude_contaminated):
        n_turns = _count_turns(str(f))
        if n_turns < min_turns:
            continue
        corpus.extend(transcript_to_frontier_requests(str(f), project=project))
        n_sessions += 1
    stats = CorpusStats(
        n_sessions=n_sessions,
        n_requests=len(corpus),
        total_bytes=sum(len(r.frontier_block or b"") for r in corpus),
        max_tokens=max((r.tokens for r in corpus), default=0),
        canonical=(limit_sessions is None),
        source=(f"build_streaming_corpus(limit_sessions={limit_sessions})"
                if limit_sessions is not None else "build_streaming_corpus"),
    )
    return corpus, stats


def stream_composition(project: str, *, projects_root: str | None = None, min_turns: int = 1,
                       limit_sessions: int | None = None,
                       exclude_contaminated: bool = False) -> dict:
    """Streaming composition profiler: the per-(class × stratum) frontier tally, computed one block at
    a time (never materializing growing prefixes). Its `cells` equal
    `diagnose(build_corpus(...)).snapshot()["cells"]` by construction — same classify / size_stratum /
    token functions on the same frontier blocks — and it additionally slices the tally by REGIME."""
    cells: dict[tuple[str, str], dict] = {}
    by_regime: dict[str, dict] = {}
    total_bytes = 0
    total_blocks = 0
    for f in _select_files(project, projects_root, limit_sessions, exclude_contaminated):
        if _count_turns(str(f)) < min_turns:
            continue
        for block, context_bytes, regime in stream_transcript_frontiers(str(f)):
            if not block:
                continue
            text = block.decode("utf-8", "replace")
            cls = classify(text)
            st = size_stratum_bytes(context_bytes)
            cell = cells.setdefault((cls, st), {"n": 0, "bytes": 0, "tokens": 0})
            cell["n"] += 1
            cell["bytes"] += len(block)
            cell["tokens"] += estimate_tokens(text)
            rb = by_regime.setdefault(regime, {"classes": {}, "bytes": 0, "n": 0})
            rb["classes"][cls] = rb["classes"].get(cls, 0) + 1
            rb["bytes"] += len(block)
            rb["n"] += 1
            total_bytes += len(block)
            total_blocks += 1
    return {
        "total_bytes": total_bytes,
        "total_frontier_blocks": total_blocks,
        "cells": {f"{cls}/{st}": v for (cls, st), v in sorted(cells.items())},
        "by_regime": by_regime,
    }
