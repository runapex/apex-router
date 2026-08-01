"""codeqa Phase 0 delivery: ask → verify citations against the live tree → record impact.

This is the DELIVERY surface (what `apex ask` shells to). It wraps `driver.ask` with the two
Phase-0 additions — citation verification (staleness guard) and the impact log (the measurement
that gates the larger dynamic-index build) — and changes nothing inside the harness.

`deliver()` takes injectable seams (ask_fn / verify_fn / write_fn / git_fn / clock) so the
orchestration is unit-testable offline without a live Ornith server or a real git repo.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .impact import (
    EmittedCite,
    ImpactRecord,
    parse_emitted_citations,
    verify_emitted_citation,
    write_impact,
)
from .retriever import Chunk, RepoConfig

DEFAULT_IMPACT_LOG = Path.home() / ".apex" / "codeqa_impact.jsonl"


@dataclass
class VerifiedCitation:
    cite: str
    verdict: str  # grounded | stale | hallucinated

    def marker(self) -> str:
        return {"grounded": "✓ grounded", "stale": "✗ STALE",
                "hallucinated": "✗ HALLUCINATED"}.get(self.verdict, "? unknown")


@dataclass
class Delivery:
    """The delivered answer plus its verification — what the CLI renders and the log records."""
    question: str
    repo: str
    text: str
    citations: list[VerifiedCitation]
    cached_tokens: int | None
    prompt_tokens: int | None
    latency_ms: int
    git_head: str | None
    digest_commits_behind: int | None

    def has_problem(self) -> bool:
        """Any emitted citation that is stale OR hallucinated — the answer cited code that isn't
        there (moved/gone) or was never given to the model (invented)."""
        return any(c.verdict in ("stale", "hallucinated") for c in self.citations)

    def citation_validity(self) -> float | None:
        """grounded / total emitted citations. None when the answer emitted no citation at all —
        which is itself a signal (an answer that cites nothing is not 100% valid, it's uncited)."""
        n = len(self.citations)
        if not n:
            return None
        return sum(1 for c in self.citations if c.verdict == "grounded") / n


def _git(root: Path, *args: str) -> str | None:
    """Run a git command in `root`; return stripped stdout, or None on any failure (fail-open)."""
    try:
        p = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, text=True, timeout=5)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _git_head(root: Path) -> str | None:
    return _git(root, "rev-parse", "--short", "HEAD")


def _digest_commits_behind(cfg: RepoConfig) -> int | None:
    """How many commits behind HEAD the digest is, measured in the git repo that CONTAINS the digest
    — which is the codeqa tooling repo now that digests live under codeqa/digests/, NOT the product
    root (`cfg.root`). Running git in cfg.root against a digest path outside it always returns None,
    silently killing the drift signal. So resolve the digest's own repo dir and query there. None
    when the digest isn't tracked / not in a git repo."""
    if not cfg.digest:
        return None
    digest_dir = Path(cfg.digest).resolve().parent  # the tooling repo (or wherever the digest lives)
    last = _git(digest_dir, "log", "-1", "--format=%H", "--", str(cfg.digest))
    if not last:
        return None  # digest not committed yet → no drift history (expected until first commit)
    count = _git(digest_dir, "rev-list", "--count", f"{last}..HEAD")
    try:
        return int(count) if count is not None else None
    except ValueError:
        return None


def deliver(
    repo: str,
    question: str,
    *,
    impact_log: Path | None = None,
    max_tokens: int = 1200,
    enable_thinking: bool = False,
    ask_fn: Callable | None = None,
    verify_fn: Callable[[Path, EmittedCite, list[Chunk]], str] | None = None,
    write_fn: Callable[[Path, ImpactRecord], None] | None = None,
    githead_fn: Callable[[Path], str | None] | None = None,
    behind_fn: Callable[[RepoConfig], int | None] | None = None,
    clock: Callable[[], float] | None = None,
) -> Delivery:
    """Ask `question` about `repo`, verify every citation against the live tree, log the impact
    record, and return a Delivery. Seams default to the real implementations; tests inject fakes."""
    ask_fn = ask_fn or _default_ask
    # verify_fn(repo_root, EmittedCite, chunks) -> verdict; injectable for tests.
    verify_fn = verify_fn or verify_emitted_citation
    write_fn = write_fn or write_impact
    githead_fn = githead_fn or _git_head
    behind_fn = behind_fn or _digest_commits_behind
    clock = clock or time.time
    impact_log = Path(impact_log) if impact_log is not None else DEFAULT_IMPACT_LOG

    cfg = RepoConfig.load(repo)
    t0 = clock()
    answer = ask_fn(repo, question, max_tokens=max_tokens, enable_thinking=enable_thinking)
    latency_ms = int((clock() - t0) * 1000)

    # Verify the citations the MODEL EMITTED in its answer (Codex xval rev 2), not the retrieved
    # chunks. Each emitted cite is classified grounded / stale / hallucinated against the chunks it
    # was given + the live tree.
    emitted = parse_emitted_citations(answer.text)
    verified = [
        VerifiedCitation(cite=c.cite(), verdict=verify_fn(cfg.root, c, answer.chunks))
        for c in emitted
    ]
    git_head = githead_fn(cfg.root)
    behind = behind_fn(cfg)

    rec = ImpactRecord(
        ts=clock(), repo=repo, git_head=git_head,
        question_len=len(question),  # length ONLY — never the question string
        n_chunks=len(answer.chunks),
        citations=[{"cite": v.cite, "verdict": v.verdict} for v in verified],
        cached_tokens=answer.cached_tokens, prompt_tokens=answer.prompt_tokens,
        latency_ms=latency_ms, digest_commits_behind=behind,
    )
    write_fn(impact_log, rec)

    return Delivery(
        question=question, repo=repo, text=answer.text, citations=verified,
        cached_tokens=answer.cached_tokens, prompt_tokens=answer.prompt_tokens,
        latency_ms=latency_ms, git_head=git_head, digest_commits_behind=behind,
    )


def _default_ask(repo: str, question: str, *, max_tokens: int, enable_thinking: bool):
    from .driver import ask
    return ask(repo, question, max_tokens=max_tokens, enable_thinking=enable_thinking)


def resolve_repo_from_cwd(cwd: Path | None = None) -> str | None:
    """Match cwd (or a parent) to a registered repo's root. Returns the repo name or None."""
    from .retriever import REPOS_DIR, RepoConfig
    cwd = Path(cwd or Path.cwd()).resolve()
    for p in sorted(REPOS_DIR.glob("*.json")):
        try:
            cfg = RepoConfig.load(p.stem)
        except Exception:  # noqa: BLE001 — a broken config must not block resolution of others
            continue
        root = cfg.root.resolve()
        if cwd == root or root in cwd.parents:
            return cfg.name
    return None
