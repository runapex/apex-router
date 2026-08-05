"""Tests for amr.bench — the §4.2 single-step paired replay bench.

For each corpus step x each candidate model: replay -> score -> append a reward row.
Then deltas_from_rows pairs each candidate against the incumbent ON THE SAME STEP
(single-step paired, finding #8) to produce the {model: [deltas]} the gate consumes.

Network (replay_fn) and scoring (score_fn) are injected seams so tests are hermetic,
mirroring codeqa/ab.py (retrieve_fn/ask_fn/judge_fn) and amr/embed.py (post_fn).
"""
import pytest

from apex_router import bench, store


def _step(step_id, cell_id="c1", venue="proxy", split="promotion", oracle_kind="tests",
          window_id="w1", provenance="objective"):
    return bench.Step(step_id=step_id, venue=venue, cell_id=cell_id, split=split,
                      context={"messages": [{"role": "user", "content": step_id}]},
                      oracle={"kind": oracle_kind}, window_id=window_id,
                      provenance=provenance)


def _replay_ok(step, model):
    # deterministic fake: 'output' encodes the model so score_fn can differentiate.
    return bench.Replay(output=f"{model}:{step.step_id}",
                        cost_usd=0.01, tokens_in=100, tokens_out=20, latency=1.2)


def _score_by_model(scores):
    """score_fn returning a fixed objective score per model (0..1), ignoring step."""
    def score(step, model, replay):
        return {"score": scores[model], "pass": scores[model] >= 0.5}
    return score


# --------------------------------------------------------------------------- #
# run_bench — orchestration + row schema
# --------------------------------------------------------------------------- #
def test_run_bench_replays_every_step_through_every_candidate(tmp_path):
    steps = [_step("s1"), _step("s2")]
    rows = bench.run_bench(steps, candidate_set=["opus", "sonnet"],
                           replay_fn=_replay_ok, score_fn=_score_by_model({"opus": 0.9, "sonnet": 0.6}),
                           bench_run_id="r1", corpus_snapshot="snap1",
                           store_path=tmp_path / "outcomes.jsonl")
    # 2 steps x 2 candidates = 4 rows
    assert len(rows) == 4
    pairs = {(r["step_id"], r["model"]) for r in rows}
    assert pairs == {("s1", "opus"), ("s1", "sonnet"), ("s2", "opus"), ("s2", "sonnet")}


def test_run_bench_row_has_required_schema(tmp_path):
    rows = bench.run_bench([_step("s1")], candidate_set=["opus"],
                           replay_fn=_replay_ok, score_fn=_score_by_model({"opus": 0.9}),
                           bench_run_id="r1", corpus_snapshot="snap1",
                           store_path=tmp_path / "o.jsonl")
    r = rows[0]
    for key in ("step_id", "venue", "model", "cell_id", "outcome", "cost_usd",
                "latency", "bench_run_id", "corpus_snapshot", "ts"):
        assert key in r, f"missing {key}"
    assert r["outcome"]["score"] == 0.9
    assert r["cost_usd"] == 0.01


def test_run_bench_writes_reward_rows_to_store(tmp_path):
    p = tmp_path / "o.jsonl"
    bench.run_bench([_step("s1")], candidate_set=["opus"],
                    replay_fn=_replay_ok, score_fn=_score_by_model({"opus": 0.9}),
                    bench_run_id="r1", corpus_snapshot="snap1", store_path=p)
    persisted = store.read_rows(p)
    assert len(persisted) == 1
    assert persisted[0]["model"] == "opus"
    assert "outcome" in persisted[0]      # written via append_reward (finding #16)


def test_run_bench_carries_ts_from_injected_clock(tmp_path):
    rows = bench.run_bench([_step("s1")], candidate_set=["opus"],
                           replay_fn=_replay_ok, score_fn=_score_by_model({"opus": 0.9}),
                           bench_run_id="r1", corpus_snapshot="snap1",
                           store_path=tmp_path / "o.jsonl", now_fn=lambda: "2026-07-31T00:00:00Z")
    assert rows[0]["ts"] == "2026-07-31T00:00:00Z"


def test_run_bench_survives_a_failing_replay(tmp_path):
    # A replay that raises for one (step, model) must drop THAT row, not abort the run.
    def flaky(step, model):
        if model == "sonnet":
            raise RuntimeError("upstream 500")
        return _replay_ok(step, model)
    rows = bench.run_bench([_step("s1"), _step("s2")], candidate_set=["opus", "sonnet"],
                           replay_fn=flaky, score_fn=_score_by_model({"opus": 0.9, "sonnet": 0.6}),
                           bench_run_id="r1", corpus_snapshot="snap1",
                           store_path=tmp_path / "o.jsonl")
    models = {r["model"] for r in rows}
    assert models == {"opus"}             # sonnet rows dropped, opus survived
    assert len(rows) == 2


# --------------------------------------------------------------------------- #
# objective_score — §5.1 oracle scorer
# --------------------------------------------------------------------------- #
def test_objective_score_pass_is_one_fail_is_zero():
    assert bench.objective_score(True)["score"] == 1.0
    assert bench.objective_score(False)["score"] == 0.0
    assert bench.objective_score(True)["pass"] is True


# --------------------------------------------------------------------------- #
# deltas_from_rows — the single-step paired bridge to the gate
# --------------------------------------------------------------------------- #
def _row(step_id, model, score, split="promotion", cell="c1"):
    return {"step_id": step_id, "model": model, "cell_id": cell, "split": split,
            "outcome": {"score": score}}


def test_deltas_pair_candidate_vs_incumbent_on_same_step():
    rows = [_row("s1", "opus", 0.4), _row("s1", "sonnet", 0.9),
            _row("s2", "opus", 0.5), _row("s2", "sonnet", 0.7)]
    d = bench.deltas_from_rows(rows, incumbent="opus", split="promotion", cell_id="c1")
    # sonnet - opus on each shared step: (0.9-0.4)=0.5, (0.7-0.5)=0.2
    assert d["sonnet"] == pytest.approx([0.5, 0.2])
    assert "opus" not in d                 # incumbent isn't a candidate vs itself


def test_deltas_skip_steps_missing_the_incumbent():
    # s2 has no incumbent row -> that step can't be paired -> contributes no delta.
    rows = [_row("s1", "opus", 0.4), _row("s1", "sonnet", 0.9),
            _row("s2", "sonnet", 0.7)]
    d = bench.deltas_from_rows(rows, incumbent="opus", split="promotion", cell_id="c1")
    assert d["sonnet"] == pytest.approx([0.5])   # only s1 pairs


def test_deltas_skip_steps_missing_the_candidate():
    rows = [_row("s1", "opus", 0.4), _row("s1", "sonnet", 0.9),
            _row("s2", "opus", 0.5)]              # s2 has no sonnet
    d = bench.deltas_from_rows(rows, incumbent="opus", split="promotion", cell_id="c1")
    assert d["sonnet"] == pytest.approx([0.5])


def test_deltas_filter_by_split():
    rows = [_row("s1", "opus", 0.4, split="promotion"),
            _row("s1", "sonnet", 0.9, split="promotion"),
            _row("s2", "opus", 0.5, split="confirmation"),
            _row("s2", "sonnet", 0.9, split="confirmation")]
    promo = bench.deltas_from_rows(rows, incumbent="opus", split="promotion", cell_id="c1")
    confirm = bench.deltas_from_rows(rows, incumbent="opus", split="confirmation", cell_id="c1")
    assert promo["sonnet"] == pytest.approx([0.5])
    assert confirm["sonnet"] == pytest.approx([0.4])


def test_deltas_multiple_candidates():
    rows = [_row("s1", "opus", 0.4), _row("s1", "sonnet", 0.9), _row("s1", "haiku", 0.2),
            _row("s2", "opus", 0.5), _row("s2", "sonnet", 0.7), _row("s2", "haiku", 0.6)]
    d = bench.deltas_from_rows(rows, incumbent="opus", split="promotion", cell_id="c1")
    assert d["sonnet"] == pytest.approx([0.5, 0.2])
    assert d["haiku"] == pytest.approx([-0.2, 0.1])


def test_deltas_empty_rows_returns_empty():
    assert bench.deltas_from_rows([], incumbent="opus", split="promotion", cell_id="c1") == {}


def test_deltas_duplicate_step_model_raises():
    # Two rows for the same (step, model) in one split+cell is a corpus/bench bug —
    # pairing would silently pick one. It must be rejected, not guessed.
    rows = [_row("s1", "opus", 0.4), _row("s1", "opus", 0.5), _row("s1", "sonnet", 0.9)]
    with pytest.raises(ValueError):
        bench.deltas_from_rows(rows, incumbent="opus", split="promotion", cell_id="c1")


# --------------------------------------------------------------------------- #
# Regression — confirmed by Codex adversarial cross-validation (the reference window)
# --------------------------------------------------------------------------- #
def test_p1_deltas_are_cell_local_no_cross_cell_contamination():
    # BUG (Codex #1/#2): pairing keyed only on step_id let rows from DIFFERENT cells
    # (same step_id) pair. deltas_from_rows must scope to one cell_id and ignore others.
    rows = [_row("s1", "opus", 0.1, cell="A"),      # incumbent in cell A
            _row("s1", "sonnet", 0.9, cell="B")]     # candidate in a DIFFERENT cell B
    d_a = bench.deltas_from_rows(rows, incumbent="opus", split="promotion", cell_id="A")
    d_b = bench.deltas_from_rows(rows, incumbent="opus", split="promotion", cell_id="B")
    # In cell A: only the incumbent is present -> no candidate pairs.
    assert d_a == {}
    # In cell B: only the candidate is present, no incumbent -> no pair either.
    assert d_b == {}


def test_p1_duplicate_step_across_cells_does_not_raise():
    # BUG (Codex #2): two complete cells sharing a step_id wrongly raised a global
    # duplicate error. Scoped to one cell, each cell's own step is unambiguous.
    rows = [_row("s1", "opus", 0.4, cell="A"), _row("s1", "sonnet", 0.9, cell="A"),
            _row("s1", "opus", 0.5, cell="B"), _row("s1", "sonnet", 0.7, cell="B")]
    d_a = bench.deltas_from_rows(rows, incumbent="opus", split="promotion", cell_id="A")
    d_b = bench.deltas_from_rows(rows, incumbent="opus", split="promotion", cell_id="B")
    assert d_a["sonnet"] == pytest.approx([0.5])
    assert d_b["sonnet"] == pytest.approx([0.2])


def test_p5_deltas_are_order_independent():
    # BUG (Codex #5): delta list order followed row insertion order, and a permuted
    # delta list changes the fixed-seed bootstrap p-value. Deltas must be ordered
    # deterministically (by step_id) regardless of input row order.
    rows_fwd = [_row("s1", "opus", 0.4), _row("s1", "sonnet", 0.9),
                _row("s2", "opus", 0.5), _row("s2", "sonnet", 0.7)]
    rows_rev = list(reversed(rows_fwd))
    d_fwd = bench.deltas_from_rows(rows_fwd, incumbent="opus", split="promotion", cell_id="c1")
    d_rev = bench.deltas_from_rows(rows_rev, incumbent="opus", split="promotion", cell_id="c1")
    assert d_fwd["sonnet"] == d_rev["sonnet"]     # identical order regardless of input


def test_p3_scorer_bug_raises_not_silently_dropped():
    # BUG (Codex #3): a broad except turned a score_fn BUG (KeyError, etc.) into a
    # silently dropped row, biasing deltas. A scorer that returns a malformed outcome
    # (no numeric 'score') must raise, not vanish.
    def bad_score(step, model, replay):
        return {}                                  # missing 'score' -> contract violation
    with pytest.raises((ValueError, KeyError)):
        bench.run_bench([_step("s1")], candidate_set=["opus"],
                        replay_fn=_replay_ok, score_fn=bad_score,
                        bench_run_id="r1", corpus_snapshot="snap1",
                        store_path=None, now_fn=lambda: "t")


def test_p3_infra_replay_failure_still_dropped_not_raised():
    # Complement: a genuine REPLAY (infrastructure/transport) failure is still tolerated
    # and drops that row — only SCORER contract violations surface.
    def flaky(step, model):
        if model == "sonnet":
            raise RuntimeError("upstream 500")
        return _replay_ok(step, model)
    rows = bench.run_bench([_step("s1")], candidate_set=["opus", "sonnet"],
                           replay_fn=flaky, score_fn=_score_by_model({"opus": 0.9, "sonnet": 0.6}),
                           bench_run_id="r1", corpus_snapshot="snap1", store_path=None,
                           now_fn=lambda: "t")
    assert {r["model"] for r in rows} == {"opus"}


def test_p4_row_carries_window_id_and_provenance(tmp_path):
    # BUG (Codex #4): rows lacked the window_id and provenance the gate needs. They must
    # be present so gate evidence can be reconstructed from rows (not fabricated).
    rows = bench.run_bench([_step("s1", window_id="w7", provenance="judge")],
                           candidate_set=["opus"], replay_fn=_replay_ok,
                           score_fn=_score_by_model({"opus": 0.9}), bench_run_id="r1",
                           corpus_snapshot="snap1", store_path=tmp_path / "o.jsonl",
                           now_fn=lambda: "t")
    assert rows[0]["window_id"] == "w7"
    assert rows[0]["provenance"] == "judge"


def test_p6_candidate_set_generator_is_fully_consumed(tmp_path):
    # BUG (Codex #6): a generator candidate_set was drained after the first model, so
    # only 'opus' was replayed. It must be materialized so every candidate runs.
    gen = (m for m in ["opus", "sonnet", "haiku"])
    rows = bench.run_bench([_step("s1")], candidate_set=gen, replay_fn=_replay_ok,
                           score_fn=_score_by_model({"opus": 0.9, "sonnet": 0.6, "haiku": 0.3}),
                           bench_run_id="r1", corpus_snapshot="snap1",
                           store_path=tmp_path / "o.jsonl", now_fn=lambda: "t")
    assert {r["model"] for r in rows} == {"opus", "sonnet", "haiku"}


def test_p6_score_fn_missing_numeric_score_raises(tmp_path):
    # BUG (Codex #6): score_fn returning a non-numeric/absent 'score' was persisted, then
    # crashed downstream. It must be validated at write time.
    def bad(step, model, replay):
        return {"pass": True}                      # no numeric 'score'
    with pytest.raises((ValueError, KeyError)):
        bench.run_bench([_step("s1")], candidate_set=["opus"], replay_fn=_replay_ok,
                        score_fn=bad, bench_run_id="r1", corpus_snapshot="snap1",
                        store_path=tmp_path / "o.jsonl", now_fn=lambda: "t")


# --------------------------------------------------------------------------- #
# Regression — Codex PASS 2 (validating pass-1 fixes; found residual defects)
# --------------------------------------------------------------------------- #
def _row2(step_id, model, score, split="promotion", cell="c1", run="r1", snap="snapA"):
    return {"step_id": step_id, "model": model, "cell_id": cell, "split": split,
            "outcome": {"score": score}, "bench_run_id": run, "corpus_snapshot": snap,
            "window_id": "w1", "provenance": "objective"}


def test_p2f1_no_cross_run_or_snapshot_pairing():
    # BUG (cross-validation#1): pairing scoped cell+split but NOT bench_run_id/corpus_snapshot,
    # so an incumbent from run r1/snapA paired with a candidate from run r2/snapB. With no
    # explicit run/snapshot, mixed-run rows must REFUSE (not silently guess a pairing).
    rows = [_row2("s1", "opus", 0.2, run="r1", snap="snapA"),
            _row2("s1", "sonnet", 0.9, run="r2", snap="snapB")]
    with pytest.raises(ValueError):
        bench.deltas_from_rows(rows, incumbent="opus", split="promotion", cell_id="c1")


def test_p2f1_explicit_run_scope_isolates_one_run():
    # When a run is named explicitly, only that run's rows are considered — the other
    # run's candidate cannot pair in, so the cross-run delta never forms.
    rows = [_row2("s1", "opus", 0.2, run="r1", snap="snapA"),
            _row2("s1", "sonnet", 0.9, run="r2", snap="snapB")]
    d = bench.deltas_from_rows(rows, incumbent="opus", split="promotion", cell_id="c1",
                               bench_run_id="r1", corpus_snapshot="snapA")
    assert d == {}                      # only opus is in r1/snapA -> no candidate pairs


def test_p2f1_same_run_and_snapshot_pairs():
    rows = [_row2("s1", "opus", 0.4, run="r1", snap="snapA"),
            _row2("s1", "sonnet", 0.9, run="r1", snap="snapA")]
    d = bench.deltas_from_rows(rows, incumbent="opus", split="promotion", cell_id="c1")
    assert d["sonnet"] == pytest.approx([0.5])


def test_p2f2_cell_evidence_assembled_from_rows_not_fabricated():
    # BUG (cross-validation#2): deltas_from_rows dropped window_id/provenance, so the gate's
    # CellEvidence (windows, provenance) had to be hand-fabricated. An assembler must
    # build CellEvidence straight from bench rows — windows and provenance included.
    rows = []
    for split in ("promotion", "confirmation"):
        for i in range(10):
            w = "wA" if i % 2 == 0 else "wB"       # 2 distinct windows for sonnet
            rows.append({"step_id": f"{split}-{i}", "model": "opus", "cell_id": "c1",
                         "split": split, "outcome": {"score": 0.5}, "bench_run_id": "r1",
                         "corpus_snapshot": "snapA", "window_id": w, "provenance": "objective"})
            rows.append({"step_id": f"{split}-{i}", "model": "sonnet", "cell_id": "c1",
                         "split": split, "outcome": {"score": 0.8}, "bench_run_id": "r1",
                         "corpus_snapshot": "snapA", "window_id": w, "provenance": "objective"})
    ev = bench.cell_evidence_from_rows(rows, cell_id="c1", parent_task_type="generate",
                                       incumbent="opus")
    assert ev.incumbent_model == "opus"
    assert ev.provenance == "objective"
    assert ev.windows_for("sonnet") == {"wA", "wB"}       # reconstructed from rows
    assert ev.promo_deltas["sonnet"] == pytest.approx([0.3] * 10)
    assert ev.confirm_deltas["sonnet"] == pytest.approx([0.3] * 10)


def test_p2f3_empty_window_id_is_rejected_by_step():
    # BUG (cross-validation#3): Step.window_id defaulted to "" and the gate counted "" as a
    # real window. A blank window id must be rejected at Step construction.
    with pytest.raises(ValueError):
        bench.Step("s1", "proxy", "c1", "promotion", {}, window_id="")


def test_p2f3_gate_ignores_empty_window_ids():
    from apex_router import gate
    # An evidence bundle whose windows include "" must not let "" count toward replication.
    c = gate.CellEvidence("c1", "generate", "opus",
                          {"sonnet": [0.3] * 10}, {"sonnet": [0.3] * 10},
                          {"sonnet": {"", "w1"}})       # "" is not a real window
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    assert v.promotable is False                        # only 1 real window -> fails M=2


def test_p2f4_scorer_failure_persists_no_rows(tmp_path):
    # BUG (cross-validation#4): a scorer failure mid-run left earlier rows already persisted.
    # Validation must happen BEFORE any persistence — a failed run writes nothing.
    from apex_router import store
    p = tmp_path / "o.jsonl"
    def score(step, model, replay):
        return {"score": 0.9} if model == "opus" else {}    # sonnet malformed
    with pytest.raises((ValueError, KeyError)):
        bench.run_bench([_step("s1")], candidate_set=["opus", "sonnet"],
                        replay_fn=_replay_ok, score_fn=score, bench_run_id="r1",
                        corpus_snapshot="snap1", store_path=p, now_fn=lambda: "t")
    assert store.read_rows(p) == []                     # nothing persisted


def test_p2f5_candidate_key_order_is_deterministic():
    # BUG (cross-validation#5): candidate KEY order followed row order, so gate tie-break
    # picked different equal-mean winners. deltas_from_rows must order candidates
    # deterministically regardless of row order.
    rows_fwd = [_row2("s1", "opus", 0.4), _row2("s1", "sonnet", 0.9), _row2("s1", "haiku", 0.9)]
    rows_rev = list(reversed(rows_fwd))
    d_fwd = bench.deltas_from_rows(rows_fwd, incumbent="opus", split="promotion", cell_id="c1")
    d_rev = bench.deltas_from_rows(rows_rev, incumbent="opus", split="promotion", cell_id="c1")
    assert list(d_fwd.keys()) == list(d_rev.keys())    # same candidate key order


def test_p2f6_overflowing_int_score_is_rejected(tmp_path):
    # BUG (cross-validation#6): 10**10000 passed validation, then OverflowError'd in the gate.
    def score(step, model, replay):
        return {"score": 10 ** 10000}
    with pytest.raises(ValueError):
        bench.run_bench([_step("s1")], candidate_set=["opus"], replay_fn=_replay_ok,
                        score_fn=score, bench_run_id="r1", corpus_snapshot="snap1",
                        store_path=None, now_fn=lambda: "t")


# --------------------------------------------------------------------------- #
# Integration — the whole measured path: bench -> store -> deltas -> gate
# --------------------------------------------------------------------------- #
def test_bench_to_gate_end_to_end_promotes_a_genuine_winner(tmp_path):
    from apex_router import gate

    def steps(split, n):
        # alternate two capture windows so confirmation replication (M=2) is satisfied
        # from REAL row evidence, not a fabricated window set.
        return [_step(f"{split}-s{i}", split=split, window_id=("wA" if i % 2 == 0 else "wB"))
                for i in range(n)]

    def score_fn(step, model, replay):
        base = {"opus": 0.5, "sonnet": 0.8}[model]
        jitter = 0.1 * ((hash(step.step_id) % 5) - 2) / 2.0   # deterministic small spread
        return {"score": max(0.0, min(1.0, base + jitter)), "pass": True}

    p = tmp_path / "outcomes.jsonl"
    rows = []
    for split in ("promotion", "confirmation"):
        rows += bench.run_bench(steps(split, 12), candidate_set=["opus", "sonnet"],
                                replay_fn=_replay_ok, score_fn=score_fn, bench_run_id="r1",
                                corpus_snapshot="snap1", store_path=p,
                                now_fn=lambda: "2026-07-31T00:00:00Z")
    assert len(rows) == 48                                   # 24 steps x 2 models
    # Assemble gate evidence straight from the rows — windows/provenance reconstructed,
    # nothing fabricated at the call site (cross-validation#2).
    cell = bench.cell_evidence_from_rows(rows, cell_id="c1", parent_task_type="generate",
                                         incumbent="opus")
    assert cell.windows_for("sonnet") == {"wA", "wB"}        # 2 real windows from rows
    result = gate.run_gate([cell], k=8, m_windows=2, alpha=0.05)[0]
    assert result.promoted is True
    assert result.chosen_model == "sonnet"
