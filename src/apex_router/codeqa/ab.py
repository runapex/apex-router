"""codeqa impact A/B — the controlled experiment that CAN causally answer "does the frozen digest's
staleness degrade answers, i.e. is the dynamic-index build worth it?"

The passive Phase-0 impact log is a diagnostic, not a causal gate (it varies question/repo, not
digest age). This harness holds question + tree + RETRIEVAL fixed and varies ONLY the digest pinned
as Ornith's preamble — so any change in answer quality is attributable to digest staleness.

Cost (see the spec §5.5): answering is LOCAL Ornith (≈0 paid tokens, V× local compute); the
groundedness axis here is DETERMINISTIC (reuses the Phase-0 verifier, zero tokens). A prose judge is
a separate opt-in seam, defaulting to local/human — not built into this deterministic core.

Everything is seam-injected (retrieve_fn / ask_fn / digest variants) so the harness is unit-testable
offline without a live Ornith server or a real git repo.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .impact import parse_emitted_citations, verify_emitted_citation


@dataclass(frozen=True)
class Variant:
    """One digest variant pinned as the preamble. `name` labels it (fresh / stale-N / absent);
    `digest_text` is the digest content (empty string = the `absent` floor case)."""
    name: str
    digest_text: str


# ---------- the deterministic groundedness axis (zero tokens) ----------

def score_answer(repo_root: Path, answer) -> dict:
    """Score ONE answer on the deterministic groundedness axis: of the file:line citations the model
    EMITTED, what fraction are grounded (supplied by retrieval AND real in the live tree)? Reuses the
    Phase-0 verifier — no model, zero tokens. `groundedness` is None when the answer cited nothing
    (uncited ≠ perfect — an answer with no citation is not scored 1.0)."""
    cites = parse_emitted_citations(answer.text)
    verdicts = [verify_emitted_citation(repo_root, c, answer.chunks) for c in cites]
    n = len(verdicts)
    grounded = sum(1 for v in verdicts if v == "grounded")
    return {
        "n_citations": n,
        "grounded": grounded,
        "stale": sum(1 for v in verdicts if v == "stale"),
        "hallucinated": sum(1 for v in verdicts if v == "hallucinated"),
        "groundedness": (grounded / n) if n else None,  # None = uncited, NOT 1.0
    }


# ---------- the controlled run (retrieval fixed, digest varied) ----------

def ab_run(
    repo_root: Path,
    questions: list[str],
    variants: list[Variant],
    *,
    retrieve_fn: Callable[[Path, str], list],
    ask_fn: Callable[[str, str, list], object],
    judge_fn: Callable[[str, str], float] | None = None,
) -> dict:
    """Run each question under each digest variant, holding RETRIEVAL BYTE-FROZEN (Codex A/B-F2:
    retrieve ONCE per question, reuse the exact same chunk objects for every variant — do NOT
    re-retrieve and hope they match). Returns per-variant PRIMARY prose-correctness (from judge_fn)
    and SECONDARY groundedness diagnostics.

    retrieve_fn(repo_root, question) -> chunks : called ONCE per question; frozen and reused.
    ask_fn(question, digest_text, chunks) -> answer : answers grounded in (digest, chunks). Local.
    judge_fn(question, answer_text) -> [0,1] : the PRIMARY axis — a blinded prose-correctness score
      (does the answer correctly describe the code, graded against the LIVE tree, NOT the answerer's
      excerpts — Codex A/B-judge-F1). None here → no primary axis, and `decide` REFUSES a verdict
      (groundedness is blind to semantic corruption, so it CANNOT be the decision gate).
    """
    per_variant_agg: dict[str, dict] = {
        v.name: {"n_questions": 0, "n_uncited": 0, "ground_sum": 0.0, "ground_n": 0,
                 "judge_sum": 0.0, "judge_n": 0, "judge_errors": 0} for v in variants}
    # Per-(question, variant) records — at small n the aggregate mean hides WHICH question moved
    # (one question swings a 3-question mean by 0.33). Kept so a run is readable question-by-question.
    per_question: list[dict] = []

    for q_index, q in enumerate(questions):
        chunks = retrieve_fn(repo_root, q)  # ONCE — the frozen context for every variant
        for v in variants:
            answer = ask_fn(q, v.digest_text, chunks)
            agg = per_variant_agg[v.name]
            agg["n_questions"] += 1
            g = score_answer(repo_root, answer)
            if g["groundedness"] is None:
                agg["n_uncited"] += 1  # Codex A/B-F3: uncited is NOT excluded — it's a tracked failure
            else:
                agg["ground_sum"] += g["groundedness"]; agg["ground_n"] += 1
            correctness, judge_error = None, False
            if judge_fn is not None:
                # Codex A/B-judge-F6: a per-item judge FAILURE (network/auth/protocol) must be RECORDED
                # and skipped, NOT abort the whole run — a run discards all the local answering work if
                # one grade throws. Track judge_errors; the mean is over successful grades only, and
                # `decide` sees the error count via judge_n vs n_questions.
                try:
                    correctness = judge_fn(q, answer.text)
                    agg["judge_sum"] += correctness; agg["judge_n"] += 1
                except Exception:  # noqa: BLE001 — a bad grade must not take down the experiment
                    agg["judge_errors"] += 1
                    judge_error = True
            per_question.append({
                "q_index": q_index,                  # stable position — pairing keys on THIS, not text
                "question": q,                       # (duplicate question text must not collapse a pair)
                "variant": v.name,
                "correctness": correctness,          # PRIMARY axis for this item (None if not judged)
                "judge_error": judge_error,          # the grade FAILED (network/auth) — not "scored 0"
                "groundedness": g["groundedness"],   # SECONDARY (None = uncited)
                "cited_files": _cited_files(answer),  # what the answer actually pointed at
            })

    # PAIRED set: questions every variant correctness-scored (no judge_error, score present). Comparing
    # variant means over DIFFERENT surviving-question subsets is apples-to-oranges (the Step-3 bug);
    # decide() uses paired_correctness so the comparison holds the question set fixed across variants.
    # Keyed on q_index (position), NOT question text — duplicate question lines must not collapse a
    # pair or let one occurrence's success stand in for another's failure.
    scored: dict[tuple, float] = {
        (r["q_index"], r["variant"]): r["correctness"]
        for r in per_question if r["correctness"] is not None and not r["judge_error"]}
    paired_idx = [i for i in range(len(questions))
                  if all((i, v.name) in scored for v in variants)] if judge_fn else []

    per_variant = []
    for v in variants:
        a = per_variant_agg[v.name]
        paired_vals = [scored[(i, v.name)] for i in paired_idx]
        per_variant.append({
            "variant": v.name,
            "n_questions": a["n_questions"],
            "n_uncited": a["n_uncited"],
            "judge_errors": a["judge_errors"],  # grades that FAILED (network/auth/protocol)
            # PRIMARY: mean prose-correctness over SUCCESSFULLY-JUDGED answers (None if none)
            "mean_correctness": (a["judge_sum"] / a["judge_n"]) if a["judge_n"] else None,
            "n_judged": a["judge_n"],
            # PAIRED PRIMARY: mean over questions ALL variants scored — the honest cross-variant compare
            "paired_correctness": (sum(paired_vals) / len(paired_vals)) if paired_vals else None,
            "n_paired": len(paired_idx),
            # SECONDARY diagnostic: groundedness over CITED answers, plus the uncited count beside it
            "mean_groundedness": (a["ground_sum"] / a["ground_n"]) if a["ground_n"] else None,
            "citation_coverage": (a["ground_n"] / a["n_questions"]) if a["n_questions"] else None,
        })

    total_judge_errors = sum(a["judge_errors"] for a in per_variant_agg.values())
    # Retrieval identity is now a PREFLIGHT concern (retrieve is called once here), verified
    # separately by retrieval_is_reproducible(); a single retrieval can't differ across variants.
    return {"per_variant": per_variant,
            "per_question": per_question,
            "has_primary_axis": judge_fn is not None and total_judge_errors == 0
            or any(p["mean_correctness"] is not None for p in per_variant),
            "judge_errors": total_judge_errors}


def _cited_files(answer) -> list[str]:
    """Distinct files the answer CITED (order-preserving) — so a per-question record shows what the
    model pointed at. Reused for the 'did the answer stop citing the digest-only source' analysis."""
    out: list[str] = []
    for c in parse_emitted_citations(answer.text):
        if c.file not in out:
            out.append(c.file)
    return out


def write_ab_jsonl(path, result: dict) -> None:
    """Write a re-readable A/B artifact for offline analysis: one JSON line per per-question record
    (kind='per_question'), then one per per-variant summary (kind='per_variant'). Enables reading the
    per-question spread — the thing that separates a real signal from one question swinging the mean."""
    import json
    # Spread the record FIRST, then set kind — so the discriminator tag can never be shadowed by a
    # future field named 'kind' in a record (today none has one; this keeps that guarantee robust).
    lines = []
    for rec in result.get("per_question", []):
        lines.append(json.dumps({**rec, "kind": "per_question"}))
    for rec in result.get("per_variant", []):
        lines.append(json.dumps({**rec, "kind": "per_variant"}))
    Path(path).write_text("\n".join(lines) + ("\n" if lines else ""))


def retrieval_is_reproducible(repo_root: Path, question: str, retrieve_fn, *, n: int = 3) -> bool:
    """PREFLIGHT (Codex A/B-F2): retrieval must be reproducible for the frozen-context design to be
    valid. Call retrieve_fn n times and check the FULL serialized identity (file, span, text, why —
    everything that enters the prompt), not just file:line."""
    keys = {_context_key(retrieve_fn(repo_root, question)) for _ in range(n)}
    return len(keys) == 1


def _context_key(chunks: list) -> tuple:
    """Full serialized identity of a chunk set — everything that reaches the prompt, IN ORDER (Codex
    A/B-F2: _format_context numbers excerpts positionally, and `why` is in the prompt, so order and
    text and why all matter — not just file:line)."""
    return tuple((c.file, c.start, c.end, c.text, c.why) for c in chunks)


# ---------- the pre-registered decision rule ----------

def decide(per_variant: list[dict], *, margin: float = 0.10, min_paired: int = 8) -> dict:
    """Pre-registered rule, gated on the PRIMARY prose-correctness axis.

    CRITICAL (Codex A/B-F1): the decision is made ONLY on prose correctness, NOT on groundedness —
    groundedness is blind to semantic corruption (a false claim citing a valid line scores 1.0), so
    it cannot detect the failure mode the experiment exists to find. If no correctness score is
    present (no judge was run), this REFUSES to decide rather than emit a verdict on the wrong axis.

    Build IFF the digest demonstrably HELPS correctness (fresh − absent ≥ margin) AND its staleness
    demonstrably HURTS correctness (fresh − stale ≥ margin). A 'don't build' from a real correctness
    signal is a SUCCESS (it saves the build); a 'cannot decide' from a missing axis is NOT a
    don't-build — it is an honest refusal (Codex A/B-F4: absence of evidence ≠ equivalence)."""
    have_primary = any(p.get("mean_correctness") is not None for p in per_variant)
    if not have_primary:
        return {"build": None, "rationale":
                "CANNOT DECIDE: no prose-correctness judge was run. Groundedness alone cannot gate "
                "this — it is blind to a semantically wrong answer that cites valid lines (Codex "
                "A/B-F1). Run with a blinded correctness judge (judge_fn) before deciding. "
                "(Groundedness/coverage remain as secondary diagnostics.)"}

    # PAIRED comparison (Step-3 bug fix): judge failures fall unevenly across variants, so comparing
    # each variant's mean over its OWN surviving questions is apples-to-oranges (fresh's 11-question
    # mean vs absent's 6-question mean). When ab_run supplies paired_correctness (mean over questions
    # ALL variants answered), decide MUST use it — and refuse if too few questions survive for all.
    # Enter paired mode only when EVERY variant carries the paired fields (a well-formed ab_run
    # result), not just one — a mixed schema would otherwise compare a paired mean to a None. And
    # n_paired must agree across variants (ab_run emits it identically); if a hand-built result
    # disagrees, refuse rather than trust the first value.
    use_paired = all("paired_correctness" in p for p in per_variant)
    if use_paired:
        n_paired_vals = {p.get("n_paired", 0) for p in per_variant}
        if len(n_paired_vals) != 1:
            return {"build": None, "rationale":
                    f"CANNOT DECIDE: inconsistent paired-set sizes across variants ({sorted(n_paired_vals)}) "
                    f"— the paired comparison is only valid when every variant is scored on the SAME "
                    f"questions. Re-run to regenerate a consistent result."}
        n_paired = n_paired_vals.pop()
        if n_paired < min_paired:
            return {"build": None, "rationale":
                    f"CANNOT DECIDE: only {n_paired} question(s) were correctness-scored by ALL "
                    f"variants (paired set < {min_paired}); the rest lost grades to judge failures. "
                    f"Comparing variants over different question subsets is not the same experiment. "
                    f"Re-run (judge now retries transient failures) or lower the bar deliberately."}
        by = {p["variant"]: p.get("paired_correctness") for p in per_variant}
    else:
        by = {p["variant"]: p.get("mean_correctness") for p in per_variant}
    fresh = by.get("fresh")
    absent = by.get("absent")
    stale = next((v for k, v in by.items() if k.startswith("stale")), None)

    if fresh is None:
        return {"build": None, "rationale": "CANNOT DECIDE: no 'fresh' variant correctness score."}

    helps = absent is not None and (fresh - absent) >= margin
    hurts = stale is not None and (fresh - stale) >= margin

    if helps and hurts:
        return {"build": True, "rationale":
                f"BUILD: digest helps correctness (fresh {fresh:.2f} vs absent {absent:.2f}, "
                f"Δ={fresh-absent:.2f}≥{margin}) AND staleness hurts it (fresh vs stale "
                f"Δ={fresh-stale:.2f}≥{margin})."}
    if absent is not None and not helps:
        return {"build": False, "rationale":
                f"DON'T BUILD: the digest is not load-bearing for correctness — absent {absent:.2f} ≈ "
                f"fresh {fresh:.2f} (Δ={fresh-absent:.2f}<{margin})."}
    if stale is not None and not hurts:
        return {"build": False, "rationale":
                f"DON'T BUILD: staleness does not degrade correctness — stale {stale:.2f} ≈ fresh "
                f"{fresh:.2f} (Δ={fresh-stale:.2f}<{margin}); descope to occasional regeneration."}
    return {"build": None, "rationale":
            "CANNOT DECIDE: need fresh, a stale-N, AND absent variants all correctness-scored."}


# ---------- real seams (git-backed digest variants + Ornith answerer) ----------

def digest_at_commit(repo_root: Path, digest_rel_path: str, commit: str) -> str | None:
    """The digest file's content as of `commit` — for building a `stale-N` variant. None if the path
    wasn't tracked at that commit (git show → nonzero) OR the lookup errored (git missing, timeout).
    Decodes with errors='replace' to match load_digest — a non-UTF-8 historical blob must not crash
    the run. `commit` may be a ref like 'HEAD~20'."""
    try:
        p = subprocess.run(["git", "-C", str(repo_root), "show", f"{commit}:{digest_rel_path}"],
                           capture_output=True, text=True, errors="replace", timeout=10)
        return p.stdout if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def real_retrieve_fn(repo: str):
    """Bind a retrieve_fn(repo_root, question) over the codeqa retriever for `repo`."""
    from .retriever import RepoConfig, retrieve
    cfg = RepoConfig.load(repo)

    def _retrieve(repo_root, question):
        return retrieve(cfg, question)
    return _retrieve


def real_ask_fn(repo: str, *, max_tokens: int = 1200):
    """Bind an ask_fn(question, digest_text, chunks) that answers via LOCAL Ornith with the given
    digest pinned as the frozen preamble — the same driver path, but the digest is INJECTED per
    variant instead of read from the config (so we can pin fresh / stale-N / absent)."""
    from .driver import Answer, _SYSTEM_PREAMBLE_TEMPLATE, _format_context
    from .retriever import RepoConfig
    from ..ornith import ornith_client as oc
    cfg = RepoConfig.load(repo)

    def _ask(question, digest_text, chunks):
        preamble = _SYSTEM_PREAMBLE_TEMPLATE.format(
            name=cfg.name, language=cfg.language,
            digest=digest_text or f"(No architecture digest — 'absent' variant for {cfg.name}.)")
        user_turn = (f"QUESTION: {question}\n\n=== RETRIEVED SOURCE EXCERPTS ===\n"
                     f"{_format_context(chunks)}")
        result = oc.chat_messages(
            [{"role": "system", "content": preamble}, {"role": "user", "content": user_turn}],
            max_tokens=max_tokens, enable_thinking=False, temperature=0.2)
        usage = result.usage or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        return Answer(question=question, repo=repo, text=result.answer, chunks=chunks,
                      cached_tokens=cached, prompt_tokens=usage.get("prompt_tokens"))
    return _ask


def build_variants(repo: str, *, stale_commits: list[str] | None = None,
                   warn: Callable[[str], None] | None = None) -> list[Variant]:
    """Construct the digest variants for a repo: `fresh` (current digest), one `stale-<ref>` per given
    commit ref (its digest content then), and `absent` (empty).

    Staleness resolves against the git repo that TRACKS the digest — which is often NOT `cfg.root`
    (digests live in the tooling repo under codeqa/digests/, not in the code repo they describe). A
    ref that can't be resolved is skipped; when `warn` is supplied (the CLI always does) each skip is
    reported, so a run never quietly collapses to two variants and masquerades as the three-variant
    experiment the verdict needs. Callers that omit `warn` opt out of that reporting."""
    from .retriever import RepoConfig, load_digest
    cfg = RepoConfig.load(repo)
    variants = [Variant("fresh", load_digest(cfg))]
    if stale_commits:
        # cfg.digest is None when the digest path isn't configured OR the file is absent from the
        # worktree (retriever nulls a non-existent path). Either way there is no digest to age — say
        # so, so every requested --stale ref gets a matching SKIPPED line (the CLI NOTE promises one).
        ctx = _digest_git_context(cfg) if cfg.digest else None
        for ref in stale_commits:
            txt = digest_at_commit(ctx[0], ctx[1], ref) if ctx else None
            if txt is not None:
                variants.append(Variant(f"stale-{ref}", txt))
            elif warn is not None:
                if not cfg.digest:
                    warn(f"stale variant '{ref}' SKIPPED: no current digest for '{repo}' (path "
                         f"unconfigured or missing from the worktree) — nothing to age.")
                elif ctx is None:
                    warn(f"stale variant '{ref}' SKIPPED: the digest for '{repo}' ({cfg.digest}) is "
                         f"not inside any git repo (or git is unavailable) — cannot git-show a "
                         f"stale copy.")
                else:
                    # None here means git-show did not yield content: the path was untracked at the
                    # ref, the ref is unknown, or the lookup errored — we can't distinguish, so we
                    # don't assert one cause. Point the user at the exact command to reproduce.
                    warn(f"stale variant '{ref}' SKIPPED: `git -C {ctx[0]} show {ref}:{ctx[1]}` "
                         f"yielded no content (path untracked at that ref, unknown ref, or git "
                         f"error). Run it to see which.")
    variants.append(Variant("absent", ""))
    return variants


def _digest_git_context(cfg) -> tuple[Path, str] | None:
    """Locate the git repo that actually TRACKS the digest, plus the digest's path within it.

    The digest usually lives OUTSIDE the code repo it describes (e.g. tracked in the tooling repo
    under codeqa/digests/, not under cfg.root). Staleness refs must resolve against THAT repo — not
    cfg.root — or `git show <ref>:<path>` runs in the wrong repo and every stale variant is dropped.
    Returns (digest_repo_root, digest_rel_path), or None if the digest is not under any git repo."""
    if not cfg.digest:
        return None
    digest = Path(cfg.digest).resolve()
    try:
        p = subprocess.run(["git", "-C", str(digest.parent), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
        if p.returncode != 0:
            return None
        top = Path(p.stdout.strip()).resolve()
        return top, str(digest.relative_to(top))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
