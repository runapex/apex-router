"""codeqa impact A/B — controlled digest-staleness experiment (Codex-reconciled).

The PRIMARY decision axis is prose-correctness from a blinded judge; groundedness is a SECONDARY
diagnostic that is BLIND to semantic corruption (a false claim citing a valid line scores 1.0), so
it must NOT gate the decision (Codex A/B-F1). Retrieval is retrieved ONCE and frozen (F2); uncited
answers are tracked failures, not excluded (F3); `decide` REFUSES a verdict without a correctness
axis (F4). Everything is seam-injected — offline, no live Ornith, no real git.
"""
from __future__ import annotations

from pathlib import Path

import subprocess

from apex_router.codeqa.ab import (
    Variant,
    ab_run,
    decide,
    digest_at_commit,
    retrieval_is_reproducible,
    score_answer,
)
from apex_router.codeqa.retriever import Chunk


def _chunk(file, s, e, text="", why=""):
    return Chunk(file=file, start=s, end=e, text=text, why=why)


class _FakeAnswer:
    def __init__(self, text, chunks):
        self.text = text
        self.chunks = chunks
        self.cached_tokens = 0
        self.prompt_tokens = 0


# ---------- the secondary groundedness axis + its documented BLIND SPOT ----------

def test_score_answer_grounded_vs_hallucinated(tmp_path):
    (tmp_path / "a.py").write_text("l1\nl2\nl3\n")
    chunks = [_chunk("a.py", 1, 3)]
    sc = score_answer(tmp_path, _FakeAnswer("see a.py:2 and invented.py:9", chunks))
    assert sc["n_citations"] == 2 and sc["groundedness"] == 0.5 and sc["hallucinated"] == 1


def test_groundedness_is_blind_to_semantic_corruption(tmp_path):
    # Codex A/B-F1 (the reason groundedness cannot be the gate): a SEMANTICALLY FALSE answer that
    # cites a valid line scores identically to the correct one. This test PINS the blind spot so no
    # one re-promotes groundedness to the decision axis.
    (tmp_path / "auth.py").write_text("def login(u):\n    return check_password(u)\n")
    chunks = [_chunk("auth.py", 1, 2, why="definition of login")]
    correct = score_answer(tmp_path, _FakeAnswer("login checks the password, auth.py:1", chunks))
    wrong = score_answer(tmp_path, _FakeAnswer("login sends an email, never checks it, auth.py:1", chunks))
    assert correct["groundedness"] == wrong["groundedness"] == 1.0, \
        "groundedness cannot see a false claim on a valid cite — hence it is NOT the decision gate"


def test_uncited_answer_is_none_not_perfect(tmp_path):
    sc = score_answer(tmp_path, _FakeAnswer("a vague answer, no file refs", []))
    assert sc["n_citations"] == 0 and sc["groundedness"] is None


# ---------- retrieval reproducibility is a PREFLIGHT (F2) ----------

def test_retrieval_reproducible_preflight():
    stable = [_chunk("a.py", 1, 3, text="x", why="definition of foo")]
    assert retrieval_is_reproducible(Path("/"), "q", lambda root, q: list(stable)) is True
    # a retriever that returns different TEXT (same file:line) must fail the preflight (F2: text is in
    # the prompt, so it's part of the frozen context — file:line identity is not enough).
    calls = {"n": 0}
    def flaky(root, q):
        calls["n"] += 1
        return [_chunk("a.py", 1, 3, text=("A" if calls["n"] == 1 else "B"), why="w")]
    assert retrieval_is_reproducible(Path("/"), "q", flaky) is False


# ---------- ab_run: retrieve ONCE, primary correctness + secondary diagnostics ----------

def test_ab_run_retrieves_once_and_reuses_frozen_chunks(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    pass\n")
    chunks = [_chunk("a.py", 1, 2, why="definition of foo")]
    retrieve_calls = {"n": 0}
    def retrieve_fn(root, q):
        retrieve_calls["n"] += 1
        return chunks
    seen_chunks = []
    def ask_fn(q, digest_text, chks):
        seen_chunks.append(id(chks))  # every variant must get the SAME frozen object
        return _FakeAnswer("a.py:1", chks)
    ab_run(tmp_path, ["q"], [Variant("fresh", "F"), Variant("absent", "")],
           retrieve_fn=retrieve_fn, ask_fn=ask_fn)
    assert retrieve_calls["n"] == 1, "retrieval must run ONCE per question, not per variant (F2)"
    assert len(set(seen_chunks)) == 1, "both variants must answer over the identical frozen context"


def test_ab_run_reports_primary_correctness_and_secondary_groundedness(tmp_path):
    (tmp_path / "a.py").write_text("l1\nl2\nl3\n")
    chunks = [_chunk("a.py", 1, 3, why="definition of foo")]
    # stale digest → the model writes a WRONG answer (judge scores it low) but still cites a valid line
    # (groundedness stays 1.0 — the blind spot). This proves the PRIMARY axis catches what groundedness misses.
    def ask_fn(q, digest, chks):
        return _FakeAnswer("a.py:1 " + ("correct" if digest == "FRESH" else "wrong"), chks)
    def judge_fn(q, text):  # judge grades against the live tree; ab_run passes (question, answer)
        return 1.0 if "correct" in text else 0.2  # blinded prose-correctness
    res = ab_run(tmp_path, ["q"], [Variant("fresh", "FRESH"), Variant("absent", "")],
                 retrieve_fn=lambda r, q: chunks, ask_fn=ask_fn, judge_fn=judge_fn)
    by = {p["variant"]: p for p in res["per_variant"]}
    assert by["fresh"]["mean_correctness"] == 1.0 and by["absent"]["mean_correctness"] == 0.2
    # groundedness is BLIND to the difference — both 1.0 — which is exactly why it can't be the gate
    assert by["fresh"]["mean_groundedness"] == by["absent"]["mean_groundedness"] == 1.0
    assert res["has_primary_axis"] is True


def test_ab_run_survives_a_failing_judge_call(tmp_path):
    # Codex A/B-judge-F6: a per-item judge failure (e.g. a 401/network error) must be RECORDED and
    # skipped, NOT abort the whole run (discarding all the local answering work). The run completes,
    # judge_errors is counted, and the correctness mean is over the grades that DID succeed.
    (tmp_path / "a.py").write_text("l1\nl2\n")
    def ask_fn(q, digest, chks):
        return _FakeAnswer("a.py:1", chks)
    calls = {"n": 0}
    def flaky_judge(q, text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("HTTP 401")  # first grade fails
        return 0.8                          # subsequent grades succeed
    res = ab_run(tmp_path, ["q1", "q2"], [Variant("fresh", "F")],
                 retrieve_fn=lambda r, q: [_chunk("a.py", 1, 1)],
                 ask_fn=ask_fn, judge_fn=flaky_judge)
    assert res["judge_errors"] == 1  # the failure is tracked, not swallowed silently
    p = res["per_variant"][0]
    assert p["judge_errors"] == 1 and p["n_judged"] == 1  # one failed, one succeeded
    assert p["mean_correctness"] == 0.8  # mean over the SUCCESSFUL grade only


def test_ab_run_counts_uncited_as_failure_not_exclusion(tmp_path):
    # Codex A/B-F3: an answer that stops citing must NOT be excluded (which would make a degraded
    # stale variant look BETTER). It's counted in n_uncited and drops citation_coverage.
    def ask_fn(q, digest, chks):
        return _FakeAnswer("no citations here", chks)  # uncited
    res = ab_run(tmp_path, ["q1", "q2"], [Variant("fresh", "F")],
                 retrieve_fn=lambda r, q: [], ask_fn=ask_fn)
    p = res["per_variant"][0]
    assert p["n_uncited"] == 2
    assert p["citation_coverage"] == 0.0  # 0 of 2 questions cited — the degradation is VISIBLE


# ---------- per-question records (Step 1: see WHICH question moves, not just the mean) ----------

def test_ab_run_emits_one_per_question_record_per_question_variant(tmp_path):
    # At small n a single question swings the mean; the aggregate hides WHICH. per_question keeps
    # every (question, variant) score so a 3-question run can be read question-by-question.
    (tmp_path / "a.py").write_text("l1\nl2\nl3\n")
    chunks = [_chunk("a.py", 1, 3, why="def")]
    def ask_fn(q, digest, chks):
        # q2 is where the digest matters: fresh answers correctly, absent does not.
        good = (q == "q2" and digest == "FRESH")
        return _FakeAnswer("a.py:2 " + ("correct" if good else "wrong"), chks)
    def judge_fn(q, text):
        return 1.0 if "correct" in text else 0.2
    res = ab_run(tmp_path, ["q1", "q2"], [Variant("fresh", "FRESH"), Variant("absent", "")],
                 retrieve_fn=lambda r, q: chunks, ask_fn=ask_fn, judge_fn=judge_fn)
    pq = res["per_question"]
    assert len(pq) == 4  # 2 questions × 2 variants, one record each
    rec = {(r["question"], r["variant"]): r for r in pq}
    # the record carries the primary axis, the secondary axis, and what the answer CITED
    assert rec[("q2", "fresh")]["correctness"] == 1.0
    assert rec[("q2", "absent")]["correctness"] == 0.2  # digest's effect is isolated to q2
    assert rec[("q1", "fresh")]["correctness"] == 0.2   # q1 unaffected — so the mean alone misleads
    assert rec[("q2", "fresh")]["cited_files"] == ["a.py"]
    assert rec[("q2", "fresh")]["groundedness"] == 1.0


def test_per_question_records_a_judge_failure_without_a_score(tmp_path):
    # A per-item judge failure must appear in the per-question record (judge_error=True, correctness
    # None), not silently vanish — same honesty contract as the aggregate judge_errors count.
    (tmp_path / "a.py").write_text("l1\n")
    def ask_fn(q, digest, chks):
        return _FakeAnswer("a.py:1", chks)
    def judge_fn(q, text):
        raise RuntimeError("HTTP 401")
    res = ab_run(tmp_path, ["q1"], [Variant("fresh", "F")],
                 retrieve_fn=lambda r, q: [_chunk("a.py", 1, 1)], ask_fn=ask_fn, judge_fn=judge_fn)
    rec = res["per_question"][0]
    assert rec["judge_error"] is True and rec["correctness"] is None


def test_pairing_keys_on_index_not_text_so_duplicate_questions_dont_corrupt_it(tmp_path):
    # Codex P3-pairing: two identical question LINES must stay two distinct paired slots. If pairing
    # keyed on question text, a success on the 2nd occurrence could stand in for a failure on the 1st
    # (or collapse them), inflating/miscomputing n_paired. Here q appears twice; the judge fails the
    # FIRST occurrence for fresh only. Correct behavior: that index is unpaired → n_paired == 1, and
    # the surviving paired index uses the real per-occurrence scores.
    (tmp_path / "a.py").write_text("l1\nl2\n")
    def ask_fn(q, digest, chks):
        return _FakeAnswer("a.py:1", chks)
    # a judge that fails only the very first grade (index-0, fresh); everything else scores 1.0
    calls = {"n": 0}
    def flaky_judge(q, text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient on the first occurrence")
        return 1.0
    res = ab_run(tmp_path, ["dup", "dup"], [Variant("fresh", "F"), Variant("absent", "")],
                 retrieve_fn=lambda r, q: [_chunk("a.py", 1, 1)], ask_fn=ask_fn, judge_fn=flaky_judge)
    pv = {p["variant"]: p for p in res["per_variant"]}
    # index 0 lost fresh's grade → unpaired; index 1 scored by all → paired. n_paired must be 1, not 2.
    assert pv["fresh"]["n_paired"] == 1
    assert pv["absent"]["n_paired"] == 1
    # per_question keeps a stable q_index distinguishing the two identical lines
    idxs = sorted({r["q_index"] for r in res["per_question"]})
    assert idxs == [0, 1]


def test_write_ab_jsonl_roundtrips_per_question_and_per_variant(tmp_path):
    # The --jsonl artifact must be re-readable for offline analysis: one line per per_question record
    # plus a trailing per_variant summary line, all valid JSON.
    from apex_router.codeqa.ab import write_ab_jsonl
    result = {
        "per_question": [{"question": "q1", "variant": "fresh", "correctness": 0.9,
                          "groundedness": 1.0, "cited_files": ["a.py"], "judge_error": False}],
        "per_variant": [{"variant": "fresh", "mean_correctness": 0.9}],
    }
    out = tmp_path / "ab.jsonl"
    write_ab_jsonl(out, result)
    import json
    lines = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert lines[0]["kind"] == "per_question" and lines[0]["question"] == "q1"
    assert lines[1]["kind"] == "per_variant" and lines[1]["variant"] == "fresh"


# ---------- decide: gated on correctness, REFUSES without it ----------

def test_decide_refuses_without_a_correctness_axis():
    # Codex A/B-F1/F4: with only groundedness (no judge), decide must REFUSE, not emit don't-build.
    per = [{"variant": "fresh", "mean_correctness": None, "mean_groundedness": 1.0},
           {"variant": "absent", "mean_correctness": None, "mean_groundedness": 1.0}]
    d = decide(per, margin=0.10)
    assert d["build"] is None
    assert "cannot decide" in d["rationale"].lower()


def test_decide_build_on_correctness_when_digest_helps_and_staleness_hurts():
    per = [{"variant": "fresh", "mean_correctness": 0.95},
           {"variant": "stale-20", "mean_correctness": 0.70},
           {"variant": "absent", "mean_correctness": 0.60}]
    d = decide(per, margin=0.10)
    assert d["build"] is True


def test_decide_dont_build_when_staleness_does_not_hurt_correctness():
    per = [{"variant": "fresh", "mean_correctness": 0.92},
           {"variant": "stale-20", "mean_correctness": 0.90},
           {"variant": "absent", "mean_correctness": 0.66}]
    d = decide(per, margin=0.10)
    assert d["build"] is False  # a real correctness signal → a real (null) don't-build


def test_decide_dont_build_when_digest_does_not_help_correctness():
    per = [{"variant": "fresh", "mean_correctness": 0.80},
           {"variant": "stale-20", "mean_correctness": 0.62},
           {"variant": "absent", "mean_correctness": 0.79}]
    d = decide(per, margin=0.10)
    assert d["build"] is False


# ---------- decide() must compare PAIRED means, never mismatched denominators ----------

def test_decide_prefers_paired_mean_over_unpaired_when_present(tmp_path):
    # The Step-3 bug: judge failures fell unevenly (fresh 11 judged, absent 6), so decide compared
    # fresh's 11-question mean to absent's 6-question mean — not the same test. When paired_correctness
    # (mean over questions ALL variants answered) is present, decide MUST use it, not mean_correctness.
    per = [
        {"variant": "fresh",  "mean_correctness": 0.50, "paired_correctness": 0.60, "n_paired": 9},
        {"variant": "stale-x", "mean_correctness": 0.61, "paired_correctness": 0.62, "n_paired": 9},
        {"variant": "absent", "mean_correctness": 0.68, "paired_correctness": 0.58, "n_paired": 9},
    ]
    d = decide(per, margin=0.10)
    # on the PAIRED numbers fresh(0.60) ≈ absent(0.58): digest not load-bearing, but the UNPAIRED
    # numbers (absent 0.68 ≫ fresh 0.50) would have told the opposite story. Verdict rides on paired.
    assert "0.60" in d["rationale"] and "0.58" in d["rationale"]


def test_decide_refuses_when_paired_set_too_small(tmp_path):
    # If judge failures leave too few questions answered by ALL variants, there is no honest paired
    # comparison — refuse, cite the thin set, rather than emit a verdict on 5 questions at margin 0.1.
    per = [
        {"variant": "fresh",  "mean_correctness": 0.50, "paired_correctness": 0.60, "n_paired": 5},
        {"variant": "stale-x", "mean_correctness": 0.61, "paired_correctness": 0.65, "n_paired": 5},
        {"variant": "absent", "mean_correctness": 0.68, "paired_correctness": 0.78, "n_paired": 5},
    ]
    d = decide(per, margin=0.10, min_paired=8)
    assert d["build"] is None
    assert "paired" in d["rationale"].lower() and "5" in d["rationale"]


def test_decide_still_works_on_legacy_dicts_without_paired_fields():
    # Backward compat: dicts with only mean_correctness (no paired fields) still decide as before.
    per = [{"variant": "fresh", "mean_correctness": 0.95},
           {"variant": "stale-20", "mean_correctness": 0.70},
           {"variant": "absent", "mean_correctness": 0.60}]
    d = decide(per, margin=0.10)
    assert d["build"] is True


# ---------- staleness resolves against the DIGEST's git repo, not cfg.root ----------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _make_digest_repo(tmp_path):
    """A git repo that TRACKS a digest under codeqa/digests/ with two versions in history —
    stands in for the tooling repo (ornith), which is NOT the code repo the digest describes."""
    repo = tmp_path / "tooling"
    (repo / "codeqa" / "digests").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    digest = repo / "codeqa" / "digests" / "sample-ruby-architecture.md"
    digest.write_text("STALE digest content\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "stale")
    digest.write_text("FRESH digest content\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fresh")
    return repo, digest


def test_digest_at_commit_reads_prior_content_from_the_digests_repo(tmp_path):
    # The core two-repo fix: git-show the ref against the repo that tracks the digest.
    repo, digest = _make_digest_repo(tmp_path)
    rel = "codeqa/digests/sample-ruby-architecture.md"
    assert digest_at_commit(repo, rel, "HEAD") == "FRESH digest content\n"
    assert digest_at_commit(repo, rel, "HEAD~1") == "STALE digest content\n"


def test_digest_at_commit_returns_none_for_untracked_ref(tmp_path):
    # A file that never existed at that ref → None (so the variant is dropped, and the caller warns).
    repo, _ = _make_digest_repo(tmp_path)
    assert digest_at_commit(repo, "codeqa/digests/nope.md", "HEAD") is None


def test_build_variants_warns_and_drops_stale_when_digest_not_in_git(tmp_path, monkeypatch):
    # Regression for the silent-drop bug: an unresolvable --stale ref must be REPORTED, not vanish.
    from apex_router.codeqa import ab

    class _Cfg:
        name = "x"
        root = tmp_path
        digest = tmp_path / "loose-digest.md"  # exists but tracked by no git repo
    _Cfg.digest.write_text("loose\n")
    monkeypatch.setattr(ab, "RepoConfig", None, raising=False)
    monkeypatch.setattr("apex_router.codeqa.retriever.RepoConfig.load",
                        classmethod(lambda cls, name: _Cfg()))
    monkeypatch.setattr("apex_router.codeqa.retriever.load_digest", lambda cfg: "fresh")
    warnings = []
    variants = ab.build_variants("x", stale_commits=["HEAD~1"], warn=warnings.append)
    assert [v.name for v in variants] == ["fresh", "absent"]  # stale dropped
    assert warnings and "SKIPPED" in warnings[0]  # but LOUDLY, not silently


def test_build_variants_warns_when_stale_requested_but_no_digest(tmp_path, monkeypatch):
    # F1: cfg.digest is None (unconfigured or missing from the worktree). A --stale ref must STILL
    # warn — the CLI prints a NOTE that references a preceding SKIPPED line, which must exist.
    from apex_router.codeqa import ab

    class _Cfg:
        name = "x"
        root = tmp_path
        digest = None  # retriever nulls a missing/unconfigured digest path
    monkeypatch.setattr("apex_router.codeqa.retriever.RepoConfig.load",
                        classmethod(lambda cls, name: _Cfg()))
    monkeypatch.setattr("apex_router.codeqa.retriever.load_digest", lambda cfg: "(no digest)")
    warnings = []
    variants = ab.build_variants("x", stale_commits=["HEAD~1"], warn=warnings.append)
    assert [v.name for v in variants] == ["fresh", "absent"]
    assert warnings and "SKIPPED" in warnings[0] and "no current digest" in warnings[0]
