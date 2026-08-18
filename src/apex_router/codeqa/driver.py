"""Ornith code-Q&A driver: retrieve → pin digest as frozen preamble → ask Ornith.

This is the "Ornith answers over distilled code" half. It NEVER asks Ornith to traverse
or reason architecturally on its own (both are documented Ornith weaknesses — it asserts
wrong root causes; the Agent tool can't even reach it). Instead:

  1. retriever.retrieve() turns the question into exact, cited source chunks.
  2. The repo's architecture digest is pinned as a FROZEN system preamble so mlx's
     PromptTrie serves it from cache across questions (measured 2–6× reuse; see the
     ornith-prompt-cache-reuse memory / ornith_batch.batch_over_preamble).
  3. Ornith answers grounded ONLY in the provided chunks + digest, and must cite
     file:line — playing to its verbatim-fidelity strength.

Answers are advisory/extractive. For anything load-bearing, the cited file:line lets a
human or Opus verify at ground truth — exactly the "cheap first pass you triage" posture
the Ornith memories prescribe.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass


from ..ornith import ornith_client as oc
from ..ornith import model_router as router

from .retriever import Chunk, RepoConfig, load_digest, retrieve  # noqa: E402

_SYSTEM_PREAMBLE_TEMPLATE = """\
You are a code comprehension assistant for the {name} codebase ({language}).
You are given (A) an ARCHITECTURE DIGEST of the whole system, and per question (B) a set
of EXACT source excerpts retrieved from the repo, each labelled with its file:line.

Rules — follow strictly:
- Answer ONLY from the digest and the provided excerpts. Do NOT invent files, symbols,
  functions, or behavior that are not shown. If the excerpts do not contain the answer,
  say exactly what is missing and which file/area to retrieve next.
- Cite the file:line for every concrete claim, quoting the identifier verbatim.
- Be concise and structural: name the types/functions and how they connect.
- Do NOT speculate about root causes or design intent beyond what the code shows.

=== ARCHITECTURE DIGEST ({name}) ===
{digest}
=== END DIGEST ===
"""


@dataclass
class Answer:
    question: str
    repo: str
    text: str
    chunks: list[Chunk]
    cached_tokens: int | None
    prompt_tokens: int | None

    def citations(self) -> list[str]:
        return [c.cite() for c in self.chunks]


def _build_preamble(cfg: RepoConfig) -> str:
    return _SYSTEM_PREAMBLE_TEMPLATE.format(
        name=cfg.name, language=cfg.language, digest=load_digest(cfg))


def _format_context(chunks: list[Chunk]) -> str:
    if not chunks:
        return "(No source excerpts were retrieved for this question.)"
    parts = []
    for i, ch in enumerate(chunks, 1):
        parts.append(f"[excerpt {i}] {ch.cite()}  ({ch.why})\n"
                     f"```\n{ch.text}\n```")
    return "\n\n".join(parts)


def ask(repo: str, question: str, *, max_chunks: int = 10, max_tokens: int | None = None,
        enable_thinking: bool = False) -> Answer:
    """Answer ONE question about `repo` with the local model, grounded in retrieved chunks.

    max_tokens resolution: explicit arg > the repo config's `max_tokens` > 1200 default. A repo
    whose answers get truncated (e.g. a repo of larger source files) can raise its own budget in
    its config JSON without changing the global default.

    `enable_thinking=False` by default: extraction/synthesis over provided context is the
    fidelity lane, where thinking is the runaway-budget vector (an under-budgeted thinking
    turn returns no answer → OrnithProtocolError). Turn it on only for genuinely synthetic
    questions, with a generous max_tokens.
    """
    cfg = RepoConfig.load(repo)
    if max_tokens is None:
        max_tokens = cfg.max_tokens if cfg.max_tokens is not None else 1200

    # Honest capability gate: this is a single 'extract/synthesis' item. Report a
    # mis-route rather than silently push a bad task at dense Ornith.
    digest_bytes = len(load_digest(cfg).encode())
    route = router.select(task="extract", items=1, item_bytes=digest_bytes)
    if not route.fits:
        raise RuntimeError(f"router declined: {route.reason}")

    chunks = retrieve(cfg, question, max_chunks=max_chunks)
    preamble = _build_preamble(cfg)
    user_turn = f"QUESTION: {question}\n\n=== RETRIEVED SOURCE EXCERPTS ===\n{_format_context(chunks)}"

    result = oc.chat_messages(
        [{"role": "system", "content": preamble},
         {"role": "user", "content": user_turn}],
        max_tokens=max_tokens, enable_thinking=enable_thinking, temperature=0.2,
    )
    usage = result.usage or {}
    cached = None
    details = usage.get("prompt_tokens_details") or {}
    if isinstance(details, dict):
        cached = details.get("cached_tokens")
    return Answer(question=question, repo=repo, text=result.answer, chunks=chunks,
                  cached_tokens=cached, prompt_tokens=usage.get("prompt_tokens"))


def ask_many(repo: str, questions: list[str], *, max_chunks: int = 10,
             max_tokens: int | None = None) -> list[Answer]:
    """Answer several questions, reusing the frozen digest preamble across them.

    Each question is sent as its own [frozen_preamble, per-question context] request.
    The preamble (digest) is a byte-prefix of every request → mlx's PromptTrie serves it
    from cache on questions 2+ (the only reuse path that works on Ornith). This is the
    batch_over_preamble win applied to code Q&A: pay the digest prefill once.
    """
    cfg = RepoConfig.load(repo)
    if len(questions) > router.ORNITH_MAX_ITEMS:
        router.warn_if_unbounded(items=len(questions))
    answers: list[Answer] = []
    for q in questions:
        answers.append(ask(repo, q, max_chunks=max_chunks, max_tokens=max_tokens))
    return answers
