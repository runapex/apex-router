"""Tests for amr.consumer — the read-through shim both consumers use (§7).

resolve_model composes: classify the task -> map to a cell -> read the route table ->
fall back to the parent task-type's static default. It is a STRICT SUPERSET of today's
static behavior: any uncertainty (low classifier confidence, table off/absent, cell
unpromoted) resolves to the hand-authored default, never a worse route.

The classifier and table are injected seams so tests are hermetic. The real consumers
(the model-routing skill, the apex proxy) call this with their own embedder + table path;
wiring into those live systems is a separate, explicitly-approved step.
"""
import pytest

from apex_router import consumer


def _fixed_classifier(task_type, confidence, source="request"):
    from apex_router import classify
    def clf(text, tools=None, sys_markers=None):
        return classify.Classification(task_type, confidence, source)
    return clf


# The static default map a consumer passes in (its today's hand-authored table).
_STATIC = {"debug": "opus", "explore": "sonnet", "review": "opus",
           "refactor": "opus", "generate": "sonnet"}


def _boom_reader(cell):
    raise AssertionError("route_reader must NOT be called on the low-confidence path")


def test_low_classifier_confidence_abstains_to_static_default():
    # finding #14: below the confidence floor, don't even consult the table -> the
    # classified task-type's STATIC default (here debug->opus).
    clf = _fixed_classifier("debug", 0.3)
    got = consumer.resolve_model(
        "some task", tools=[], sys_markers=[], classifier=clf,
        static_default_map=_STATIC, route_reader=_boom_reader, min_confidence=0.7)
    assert got == "opus"                 # debug static default, table NOT consulted


def test_confident_classification_consults_table():
    clf = _fixed_classifier("generate", 0.9)
    seen = {}
    def reader(cell):
        seen["cell"] = cell
        return "haiku"                   # the table's promoted route for this cell
    got = consumer.resolve_model(
        "write a function", tools=["Edit"], sys_markers=[], classifier=clf,
        static_default_map=_STATIC, route_reader=reader, min_confidence=0.7)
    assert got == "haiku"
    assert seen["cell"] == "task:generate"     # cell id from the classified task-type


def test_table_cannot_decide_returns_static_default():
    # The reader signals CANNOT-DECIDE by returning None (unambiguous — a valid model is
    # always a non-empty string). The shim then uses the task-type's STATIC model.
    clf = _fixed_classifier("review", 0.9)
    got = consumer.resolve_model(
        "review this", tools=["ReportFindings"], sys_markers=[], classifier=clf,
        static_default_map=_STATIC, route_reader=lambda cell: None,   # CANNOT-DECIDE
        min_confidence=0.7)
    assert got == "opus"                 # review -> opus (static)


def test_unknown_task_type_falls_back_to_a_safe_default():
    # If the classifier ever yields a type not in the static map, resolve to the provided
    # safe fallback rather than crashing.
    clf = _fixed_classifier("mystery", 0.9)
    got = consumer.resolve_model(
        "x", tools=[], sys_markers=[], classifier=clf, static_default_map=_STATIC,
        route_reader=lambda cell: None, min_confidence=0.7, safe_default="opus")
    assert got == "opus"


def test_table_promoted_model_overrides_static_default():
    # The whole point: a confident classification + a promoted table route uses the
    # measured model, which can differ from the static default.
    clf = _fixed_classifier("generate", 0.95)
    got = consumer.resolve_model(
        "gen", tools=["Write"], sys_markers=[], classifier=clf, static_default_map=_STATIC,
        route_reader=lambda cell: "opus",   # measured route beats static 'sonnet'
        min_confidence=0.7)
    assert got == "opus"
    assert _STATIC["generate"] == "sonnet"          # confirm it really differs


def test_confidence_exactly_at_floor_consults_table():
    # boundary: confidence == min_confidence is "confident enough" (>=, not >).
    clf = _fixed_classifier("debug", 0.7)
    got = consumer.resolve_model(
        "x", tools=[], sys_markers=[], classifier=clf, static_default_map=_STATIC,
        route_reader=lambda cell: "haiku", min_confidence=0.7)
    assert got == "haiku"


def test_resolve_reports_provenance():
    # The shim returns a decision object (or a model) whose PROVENANCE says whether the
    # table or the static default decided — so a consumer can log/measure it.
    clf = _fixed_classifier("debug", 0.3)
    d = consumer.resolve(
        "x", tools=[], sys_markers=[], classifier=clf, static_default_map=_STATIC,
        route_reader=lambda cell: "haiku", min_confidence=0.7)
    assert d.model == "opus"
    assert d.source == "static_default_low_confidence"


def test_resolve_provenance_table():
    clf = _fixed_classifier("generate", 0.9)
    d = consumer.resolve(
        "x", tools=["Edit"], sys_markers=[], classifier=clf, static_default_map=_STATIC,
        route_reader=lambda cell: "haiku", min_confidence=0.7)
    assert d.model == "haiku"
    assert d.source == "route_table"


# --------------------------------------------------------------------------- #
# Regression — confirmed by Codex cross-validation (the reference window). The 'strict
# superset of defaults' guarantee must hold at EVERY boundary: any surprise
# resolves to a valid model default, never a crash or an unvalidated route.
# --------------------------------------------------------------------------- #
def test_h1_raising_classifier_falls_back_to_safe_default():
    # BUG (Codex #1): a raising classifier propagated. It must resolve to safe_default.
    def boom(text, tools=None, sys_markers=None):
        raise RuntimeError("classifier exploded")
    got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=boom,
                                 static_default_map=_STATIC, route_reader=lambda c: "z",
                                 min_confidence=0.7, safe_default="opus")
    assert got == "opus"


def test_h1_raising_reader_falls_back_to_static_default():
    # BUG (Codex #1): a raising route_reader propagated. A confident classification whose
    # reader raises must fall back to the task-type's static default (not crash).
    clf = _fixed_classifier("review", 0.9)
    def boom(cell):
        raise RuntimeError("reader exploded")
    got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                 static_default_map=_STATIC, route_reader=boom,
                                 min_confidence=0.7)
    assert got == "opus"                 # review's static default


def test_h2_malformed_reader_output_is_rejected():
    # BUG (Codex #2): a reader returning a non-model (None/empty/int) became the route.
    # None means CANNOT-DECIDE (-> static); "" and 42 are invalid -> static default too.
    clf = _fixed_classifier("review", 0.9)
    for bad in (None, "", "   ", 42, [], object()):
        got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                     static_default_map=_STATIC, route_reader=lambda c: bad,
                                     min_confidence=0.7)
        assert got == "opus", f"reader-> {bad!r} did not fall back"


def test_h2_malformed_classification_falls_back():
    # BUG (Codex #2): Classification(None/"" , .9) produced routes / bogus cells.
    from apex_router import classify
    for bad_type in (None, "", 123):
        def clf(text, tools=None, sys_markers=None, _t=bad_type):
            return classify.Classification(_t, 0.9, "request")
        got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                     static_default_map=_STATIC, route_reader=lambda c: "haiku",
                                     min_confidence=0.7, safe_default="opus")
        assert got == "opus"             # invalid task-type -> safe default, table not trusted


def test_h3_missing_static_mapping_on_table_path_uses_safe_default():
    # BUG (Codex #3): CANNOT-DECIDE with an EMPTY static map skipped safe_default.
    clf = _fixed_classifier("generate", 0.9)
    got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                 static_default_map={}, route_reader=lambda c: None,
                                 min_confidence=0.7, safe_default="opus")
    assert got == "opus"


def test_h4_model_named_like_task_type_is_a_real_route():
    # BUG (Codex #4): the old routed==task_type sentinel collided with a legit model named
    # after a task-type. With the None-sentinel contract, a promoted model literally named
    # "generate" is a REAL route, not a decline.
    clf = _fixed_classifier("generate", 0.9)
    got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                 static_default_map=_STATIC, route_reader=lambda c: "generate",
                                 min_confidence=0.7)
    assert got == "generate"             # honored as the measured route


def test_h5_nonfinite_confidence_abstains():
    # BUG (Codex #5): NaN/negative confidence slipped past the `<` floor and consulted the
    # table. A non-finite/invalid confidence must ABSTAIN (fail closed to static default).
    from apex_router import classify
    for bad_conf in (float("nan"), float("inf"), -0.5, None):
        def clf(text, tools=None, sys_markers=None, _c=bad_conf):
            return classify.Classification("debug", _c, "request")
        got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                     static_default_map=_STATIC, route_reader=_boom_reader,
                                     min_confidence=0.7)
        assert got == "opus"             # debug static default; reader NOT called


def test_h5_invalid_min_confidence_rejected():
    # BUG (Codex #5): min_confidence=NaN disabled abstention. An invalid floor must raise
    # at entry (a misconfigured consumer should fail loudly, not silently route everything).
    clf = _fixed_classifier("debug", 0.9)
    for bad_floor in (float("nan"), None, "high"):
        with pytest.raises((ValueError, TypeError)):
            consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                   static_default_map=_STATIC, route_reader=lambda c: "z",
                                   min_confidence=bad_floor)


def test_h6_invalid_safe_default_rejected():
    # BUG (Codex #6): safe_default=None returned Decision.model=None. An invalid ultimate
    # fallback must raise at entry — the shim cannot guarantee a valid model without one.
    clf = _fixed_classifier("debug", 0.9)
    for bad in (None, "", 42):
        with pytest.raises((ValueError, TypeError)):
            consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                   static_default_map=_STATIC, route_reader=lambda c: "z",
                                   safe_default=bad)


def test_h7_static_map_is_snapshotted_against_reader_mutation():
    # BUG (Codex #7): a stateful reader could mutate static_default_map mid-resolve and
    # poison the fallback. The shim snapshots the map so a reader can't change the default
    # it will fall back to.
    clf = _fixed_classifier("review", 0.9)
    live = dict(_STATIC)
    def evil(cell):
        live["review"] = "haiku"         # try to poison the fallback...
        return None                      # ...then decline so the fallback is used
    got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                 static_default_map=live, route_reader=evil, min_confidence=0.7)
    assert got == "opus"                 # snapshot -> original review default, not haiku


# --------------------------------------------------------------------------- #
# Regression — Codex PASS 2 + the known_models gate (the cross-machine safety)
# --------------------------------------------------------------------------- #
def test_p2_1_routed_model_must_be_known_else_static():
    # BUG (cross-validation#1): any string from the reader was routed with no check it is an
    # actually-runnable model. When known_models is given, a routed model NOT in it falls
    # back to the static default.
    clf = _fixed_classifier("debug", 0.9)
    got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                 static_default_map=_STATIC, route_reader=lambda c: "worse-model",
                                 min_confidence=0.7, known_models={"opus", "sonnet", "haiku"})
    assert got == "opus"                 # 'worse-model' not known -> static debug default


def test_p2_1_cross_machine_gateway_only_model_falls_back():
    # THE distribution safety: a route table generated on a machine behind an enterprise
    # gateway names gateway-prefixed model ids (e.g. 'gw-provider-opus'); on a target with
    # only the standard Claude+Codex CLIs that model is unknown, so the shim MUST fall back
    # to the target's static default, never route an unrunnable model.
    clf = _fixed_classifier("generate", 0.9)
    target_known = {"claude-opus", "claude-sonnet"}      # no gateway-prefixed models here
    got = consumer.resolve_model(
        "gen", tools=["Write"], sys_markers=[], classifier=clf,
        static_default_map={"generate": "claude-sonnet"},
        route_reader=lambda c: "gw-provider-opus",       # from the source machine's table
        min_confidence=0.7, known_models=target_known,
        safe_default="claude-opus")      # safe_default must itself be a known model
    assert got == "claude-sonnet"        # falls back to a model this machine can actually run


def test_p2_1_known_model_is_routed():
    clf = _fixed_classifier("generate", 0.9)
    got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                 static_default_map=_STATIC, route_reader=lambda c: "haiku",
                                 min_confidence=0.7, known_models={"opus", "sonnet", "haiku"})
    assert got == "haiku"                # known -> routed


def test_p2_1_no_known_models_accepts_any_string_model():
    # Backward-compatible: without known_models, any non-empty string model is accepted
    # (the gate is opt-in; a consumer that can't enumerate its models keeps old behavior).
    clf = _fixed_classifier("debug", 0.9)
    got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                 static_default_map=_STATIC, route_reader=lambda c: "anything",
                                 min_confidence=0.7)
    assert got == "anything"


def test_p2_1_static_default_also_validated_against_known():
    # If even the static default isn't in known_models, fall through to safe_default
    # (which must itself be known, else it's the caller's contract to fix).
    clf = _fixed_classifier("debug", 0.9)
    got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                 static_default_map={"debug": "not-on-this-box"},
                                 route_reader=lambda c: None, min_confidence=0.7,
                                 known_models={"opus"}, safe_default="opus")
    assert got == "opus"                 # static 'not-on-this-box' unknown -> safe_default


def test_p2_3_broken_static_map_degrades_gracefully_and_still_routes():
    # BUG (cross-validation#3): a static_default_map that raises on copy escaped. It must be
    # tolerated (degrade to an empty map) — a broken FALLBACK must not block a valid table
    # route, and it must never crash. Here the table route 'haiku' is known -> routed.
    clf = _fixed_classifier("debug", 0.9)
    class BadMap(dict):
        def __iter__(self):
            raise RuntimeError("map explode")
    got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                 static_default_map=BadMap(), route_reader=lambda c: "haiku",
                                 min_confidence=0.7, safe_default="opus")
    assert got == "haiku"                # broken map tolerated; valid route still taken


def test_p2_3_broken_map_and_declining_reader_yields_safe_default():
    # Broken static map AND the reader declines -> nothing to fall back to except safe.
    clf = _fixed_classifier("debug", 0.9)
    class BadMap(dict):
        def __iter__(self):
            raise RuntimeError("map explode")
    got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                 static_default_map=BadMap(), route_reader=lambda c: None,
                                 min_confidence=0.7, safe_default="opus")
    assert got == "opus"                 # degraded map -> safe_default on CANNOT-DECIDE


def test_p2_4_out_of_range_confidence_abstains():
    # BUG (cross-validation#4): confidence=-0.5 (floor -1) or 1.5 consulted the table.
    # Confidence must be validated to [0,1]; out-of-range -> abstain.
    from apex_router import classify
    for bad in (-0.5, 1.5, 10 ** 1000):
        def clf(text, tools=None, sys_markers=None, _c=bad):
            return classify.Classification("debug", _c, "request")
        got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                     static_default_map=_STATIC, route_reader=_boom_reader,
                                     min_confidence=0.7)
        assert got == "opus"             # out-of-range conf -> abstain, reader not called


def test_p2_4_min_confidence_out_of_range_rejected():
    clf = _fixed_classifier("debug", 0.9)
    for bad in (-0.1, 1.1):
        with pytest.raises(ValueError):
            consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                   static_default_map=_STATIC, route_reader=lambda c: "z",
                                   min_confidence=bad)


def test_p2_6_resolve_model_is_always_a_valid_string():
    # The overarching guarantee: whatever happens, resolve_model returns a non-empty str.
    clf = _fixed_classifier("debug", 0.9)
    for reader in (lambda c: None, lambda c: "", lambda c: 42,
                   lambda c: (_ for _ in ()).throw(RuntimeError())):
        got = consumer.resolve_model("x", tools=[], sys_markers=[], classifier=clf,
                                     static_default_map=_STATIC, route_reader=reader,
                                     min_confidence=0.7, safe_default="opus")
        assert isinstance(got, str) and got.strip()


# --------------------------------------------------------------------------- #
# Integration — the WHOLE production path: bench -> gate -> emit -> read via the
# shim, with the REAL classifier and REAL read_route bound to a persisted table.
# --------------------------------------------------------------------------- #
def test_full_consumer_path_with_real_classifier_and_table(tmp_path):
    from apex_router import bench, gate, route_table, classify

    def steps(split, n):
        return [bench.Step(f"{split}-{i}", "proxy", "task:generate", split, {"messages": []},
                           window_id=("wA" if i % 2 == 0 else "wB")) for i in range(n)]

    costs = {"sonnet": 0.01, "opus": 0.05}
    lats = {"sonnet": 1.0, "opus": 2.0}

    def replay(s, m):
        return bench.Replay(m, costs[m], 100, 20, lats[m])

    def score(s, m, r):
        base = {"sonnet": 0.5, "opus": 0.8}[m]
        return bench.objective_score((base + 0.1 * ((hash(s.step_id) % 5) - 2) / 2.0) >= 0.65)

    rows = []
    for sp in ("promotion", "confirmation"):
        rows += bench.run_bench(steps(sp, 12), candidate_set=["sonnet", "opus"],
                                replay_fn=replay, score_fn=score, bench_run_id="r1",
                                corpus_snapshot="s1", store_path=None, now_fn=lambda: "t")
    cell = bench.cell_evidence_from_rows(rows, cell_id="task:generate",
                                         parent_task_type="generate", incumbent="sonnet")
    results = gate.run_gate([cell], k=8, m_windows=2)
    assert results[0].promoted and results[0].chosen_model == "opus"

    mstats = {m: (sum(1 for r in rows if r["model"] == m and r["outcome"]["score"] == 1.0),
                  sum(1 for r in rows if r["model"] == m), costs[m], lats[m])
              for m in ("sonnet", "opus")}
    p = tmp_path / "route_table.proxy.json"
    route_table.emit_route_table(results, {"task:generate": route_table.build_ranking(mstats)},
                                 venue="proxy", generated_from={}, path=p)

    # Bind read_route to the shim's reader contract: return the model, or None for
    # CANNOT-DECIDE (read_route returns the parent sentinel, which we map to None).
    def reader(cell_id):
        m = route_table.read_route(p, cell_id=cell_id, parent_task_type="\x00cannot-decide")
        return None if m == "\x00cannot-decide" else m
    clf = lambda t, tools=None, sys_markers=None: classify.classify_request(tools=tools, sys_markers=sys_markers)

    # CONFIDENT generate (system marker -> 0.9) consults the table and gets the MEASURED
    # model 'opus', overriding the static 'sonnet'.
    d = consumer.resolve("gen", tools=["Write"], sys_markers=["generate"], classifier=clf,
                         static_default_map=_STATIC, route_reader=reader)
    assert d.model == "opus" and d.source == "route_table"

    # A no-signal task abstains to the static default (table not consulted).
    d2 = consumer.resolve("", tools=[], sys_markers=[], classifier=clf,
                          static_default_map=_STATIC, route_reader=reader)
    assert d2.source == "static_default_low_confidence"
