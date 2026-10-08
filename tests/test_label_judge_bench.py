"""labels judge-bench: judge variants scored on gold, cached, text-free; build --rejudge."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path



from apex_router import label_judge_bench as B
from apex_router import labels as L
from test_labels import _pi_session


def _home():
    return Path(os.environ["HOME"])


def _fixture():
    _pi_session(_home(), "2026-10-07_cccccccc-0000-0000-0000-000000000009", [
        ("SECRET-A fix the parser", [("edit", "", False, ""),
                                     ("bash", "pytest -q", False, "4 passed")], "SECRET-DONE"),
        ("thanks, now add retries", [("bash", "ls", False, "")], "added"),
        ("SECRET-C rename it", [("bash", "ls", True, "")], "stuck"),
        ("no, that is wrong", [("bash", "ls", False, "")], "sorry")])
    L.build(log=None)
    ids = [r["id"] for r in L._read(L.home() / "tasks.jsonl")]
    gold = [{"id": ids[0], "outcome": "success", "ts": 1, "by": "m"},
            {"id": ids[1], "outcome": "success", "ts": 1, "by": "m"},
            {"id": ids[2], "outcome": "partial", "ts": 1, "by": "m"},
            {"id": ids[2], "outcome": "fail", "ts": 2, "by": "m", "supersedes": 1},   # newest wins
            {"id": ids[3], "outcome": "unknown", "ts": 1, "by": "m"},
            {"id": "orphan:abc", "outcome": "fail", "ts": 1, "by": "m"}]
    L._write(L.home() / "gold.jsonl", gold)
    return ids


def test_clip_v1_prompt_is_the_legacy_prompt_and_export_adds_signals():
    t = {"request": "R" * 2000, "last": "L" * 3000, "next": "N" * 1000,
         "calls": [["edit", "", False, ""], ["bash", "pytest", False, "2 passed"]]}
    legacy = (L.JUDGE_PROMPT + "\nUSER REQUEST:\n<<<" + "R" * 700 + ">>>\n\nTOOLS: "
              + L._tool_summary(t) + "\n\nAGENT'S FINAL MESSAGE:\n<<<" + "L" * 900
              + ">>>\n\nUSER'S NEXT MESSAGE (none = session ended):\n<<<" + "N" * 400 + ">>>")
    assert L.judge_prompt(t, "clip", "v1") == legacy
    ex = L.judge_prompt(t, "export", "v2")
    assert ex.startswith(L.JUDGE_PROMPT_V2) and "SIGNALS: {" in ex
    assert "R" * 1500 in ex and "R" * 1501 not in ex and "L" * 2000 in ex and "N" * 800 in ex
    assert "<<<none>>>" in L.judge_prompt({**t, "next": None}, "clip", "v1")


def test_judge_call_parses_and_abstains(monkeypatch):
    replies = iter([{"message": {"content": json.dumps(
                        {"outcome": "success", "confidence": 1.4, "evidence": "free text here"})},
                     "prompt_eval_count": 120, "eval_count": 9},
                    {"message": {"content": json.dumps({"outcome": "great", "confidence": 0.9})}},
                    {"message": {"content": "not json"}}])

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    sent = []

    def fake_urlopen(req, timeout):
        sent.append(json.loads(req.data))
        return R(json.dumps(next(replies)).encode())
    monkeypatch.setattr(L.urllib.request, "urlopen", fake_urlopen)
    v, meta = L.judge_call("p", "m")
    assert v == {"outcome": "success", "confidence": 1.0, "evidence": "other"}
    assert meta["prompt_tokens"] == 120 and meta["eval_tokens"] == 9 and meta["error"] is None
    assert sent[0]["options"]["temperature"] == 0 and "seed" in sent[0]["options"]
    v, meta = L.judge_call("p", "m")
    assert v is None and meta["error"] == "bad_output"
    v, meta = L.judge_call("p", "m")
    assert v is None and meta["error"] == "JSONDecodeError"
    j = {"outcome": "fail", "confidence": 0.9, "model": "x"}
    assert L.judge_config(j) == ("x", "clip", "v1")


def test_bench_scores_caches_and_stores_no_text(tmp_path):
    ids = _fixture()
    calls = []

    def fake(prompt, model):
        calls.append((model, prompt[:40]))
        # model "good" reads the evidence; model "dumb" always says success
        if model == "dumb":
            o = "success"
        elif "REQUEST:\n<<<SECRET-C" in prompt:
            o = "fail"
        elif "<<<SECRET-A" in prompt or "<<<thanks, now" in prompt:
            o = "success"
        else:
            o = "unknown"
        return ({"outcome": o, "confidence": 0.9, "evidence": "tests"},
                {"latency_s": 0.5, "prompt_tokens": 100, "eval_tokens": 10, "error": None})
    out = tmp_path / "bench"
    rows = B.bench(["good", "dumb"], ["clip", "export"], ["v1"], out, call=fake, unload=None,
                   log=None)
    assert len(calls) == 4 * 4                       # 4 gold tasks (orphan skipped) x 4 variants
    by = {r["variant"]: r for r in rows}
    g = by["good|clip|v1"]
    assert g["n"] == 4 and g["acc_vote"] == 1.0 and (g["vote_k"], g["vote_n"]) == (3, 3)
    assert g["acc4"] == 1.0 and g["unknown"] == 0.25 and g["abstain"] == 0.25
    d = by["dumb|clip|v1"]
    assert (d["vote_k"], d["vote_n"]) == (2, 3)               # superseded gold: fail, not partial
    assert d["confusion"]["fail"]["success"] == 1 and d["confusion"]["unknown"]["success"] == 1
    assert g["latency_s"] == 0.5 and g["prompt_tokens"] == 100
    # rerun: all cached, no calls; results accumulate rather than being overwritten
    B.bench(["good"], ["clip"], ["v1"], out, call=fake, unload=None, log=None)
    assert len(calls) == 16
    assert len(L._read(out / "results.jsonl")) == 4
    # a changed prompt re-judges (cache key includes the rendered prompt's hash)
    old = L.JUDGE_PROMPTS["v1"]
    L.JUDGE_PROMPTS["v1"] = old + "\nextra rule"
    try:
        B.bench(["good"], ["clip"], ["v1"], out, call=fake, unload=None, log=None)
    finally:
        L.JUDGE_PROMPTS["v1"] = old
    assert len(calls) == 20
    # no transcript text anywhere under the bench dir; files private
    for f in out.rglob("*"):
        if f.is_file():
            assert "SECRET" not in f.read_text() and "parser" not in f.read_text()
            assert oct(f.stat().st_mode)[-3:] == "600"
    assert "no variant clears the bar" in (out / "table.md").read_text()   # n=3 CI too wide
    assert ids


def test_bench_skips_slow_or_unloadable_models(tmp_path):
    t = [({"id": f"t{i}", "request": "r", "last": "l", "next": None,
           "calls": [["bash", "", False, ""]]}, "success") for i in range(8)]
    slow = lambda p, m: ({"outcome": "success", "confidence": 1.0, "evidence": "none"},  # noqa
                         {"latency_s": 30.0, "prompt_tokens": 1, "eval_tokens": 1, "error": None})
    rows = B.bench(["s"], ["clip", "export"], ["v1"], tmp_path, call=slow, unload=None,
                   tasks=t, log=None)
    assert rows[0]["status"].startswith("skipped: slow") and rows[0]["n"] == 5
    assert rows[1]["status"] == rows[0]["status"] and rows[1]["n"] == 0    # same model: not run
    dead = lambda p, m: (None, {"latency_s": 0.0, "prompt_tokens": None,  # noqa: E731
                                "eval_tokens": None, "error": "HTTPError"})
    rows = B.bench(["d"], ["clip"], ["v1"], tmp_path, call=dead, unload=None, tasks=t, log=None)
    assert [r["status"] for r in rows if r["model"] == "d"] == ["skipped: load failure (HTTPError)"]
    # transport errors are not cached: once the model answers, the same tasks are judged
    ok = lambda p, m: ({"outcome": "success", "confidence": 1.0, "evidence": "none"},  # noqa
                       {"latency_s": 1.0, "prompt_tokens": 1, "eval_tokens": 1, "error": None})
    rows = B.bench(["d"], ["clip"], ["v1"], tmp_path, call=ok, unload=None, tasks=t, log=None)
    d = [r for r in rows if r["model"] == "d"][0]
    assert d["status"] == "ok" and d["n"] == 8 and d["error"] == 0


def test_pick_winner_bar_and_tiebreak():
    base = {"status": "ok", "acc_vote_ci": [0.6, 0.85]}
    a = {**base, "variant": "a", "acc_vote": 0.751, "unknown": 0.3}
    b = {**base, "variant": "b", "acc_vote": 0.749, "unknown": 0.1}
    assert B.pick_winner([a, b])["variant"] == "b"            # tie at 2 dp -> fewer unknowns
    assert B.pick_winner([{**a, "acc_vote": 0.69}]) is None
    assert B.pick_winner([{**a, "acc_vote_ci": [0.45, 0.9]}]) is None
    assert B.pick_winner([{**a, "status": "skipped: slow"}]) is None


def test_calibration_threshold_is_lowest_bin_with_everything_above_accurate():
    def r(c, ok):
        return {"gold": "success", "outcome": "success" if ok else "fail", "confidence": c}
    res = ([r(0.55, False)] * 3 + [r(0.55, True)] + [r(0.65, True)] * 3 + [r(0.65, False)]
           + [r(0.85, True)] * 4 + [r(0.95, True)] * 9 + [r(0.95, False)])
    cal = B.calibrate(res)
    assert cal["threshold"] == 0.6
    assert [b["n"] for b in cal["bins"]] == [0, 4, 4, 0, 4, 10]
    assert B.calibrate([r(0.95, False)])["threshold"] is None


def test_build_rejudge_replaces_other_configs_votes_and_resumes(monkeypatch):
    _pi_session(_home(), "2026-10-07_dddddddd-0000-0000-0000-000000000004", [
        ("one", [("bash", "ls", False, "")], "a"), ("two", [("bash", "ls", False, "")], "b")])
    n = []

    def j(model):
        def f(t):
            n.append(model)
            return {"outcome": "success", "confidence": 0.9, "evidence": "none", "model": model,
                    "context": L.JUDGE_CONTEXT, "prompt": L.JUDGE_PROMPT_VERSION}
        return f
    monkeypatch.setattr(L, "JUDGE_MODEL", "old")
    L.build(judge_limit=10, judge_fn=j("old"), log=None)
    assert n == ["old", "old"]
    monkeypatch.setattr(L, "JUDGE_MODEL", "new")
    L.build(judge_limit=10, judge_fn=j("new"), log=None)          # without --rejudge: kept
    assert n == ["old", "old"]
    L.build(judge_limit=1, judge_fn=j("new"), log=None, rejudge=True)
    rows = L._read(L.home() / "tasks.jsonl")
    assert n.count("new") == 1 and sorted(bool(r.get("judge")) for r in rows) == [False, True]
    assert all(r["judge"]["model"] == "new" for r in rows if r.get("judge"))   # no stale vote
    L.build(judge_limit=10, judge_fn=j("new"), log=None, rejudge=True)       # resumes
    assert n.count("new") == 2


def test_cli_judge_bench(monkeypatch, tmp_path, capsys):
    _fixture()
    monkeypatch.setattr(L, "judge_call", lambda p, m: (
        {"outcome": "success", "confidence": 0.9, "evidence": "none"},
        {"latency_s": 0.1, "prompt_tokens": 5, "eval_tokens": 2, "error": None}))
    monkeypatch.setattr(B, "_unload", lambda m: None)
    assert L.main(["judge-bench", "--models", "x", "--contexts", "clip", "--prompts", "v2",
                   "--out", str(tmp_path / "o")]) == 0
    assert "x|clip|v2" in capsys.readouterr().out
    assert L.main(["judge-bench", "--contexts", "nope", "--out", str(tmp_path / "o")]) == 2


def test_gold_tag_restricts_tasks_and_writes_separate_results(tmp_path):
    ids = _fixture()
    gold = L._read(L.home() / "gold.jsonl")
    for g in gold:
        if g["id"] in (ids[2], ids[3]):
            g["tag"] = "hold"
    L._write(L.home() / "gold.jsonl", gold)
    assert sorted(t["id"] for t, _ in B.gold_tasks(log=None, tag="hold")) == sorted(ids[2:4])
    assert len(B.gold_tasks(log=None)) == 4

    def fake(prompt, model):
        o = "fail" if "SECRET-C" in prompt else "success"
        return ({"outcome": o, "confidence": 0.95, "evidence": "none"},
                {"latency_s": 0.1, "prompt_tokens": 1, "eval_tokens": 1, "error": None})
    out = tmp_path / "b"
    B.bench(["m"], ["export"], ["v2"], out, call=fake, unload=None, log=None)
    rows = B.bench(["m"], ["export"], ["v2"], out, call=fake, unload=None, log=None,
                   gold_tag="hold")
    r = rows[0]
    assert r["n"] == 2 and r["gold_tag"] == "hold" and r["acc4"] == 0.5
    assert r["per_class"]["fail"]["recall_vote"] == 1.0
    assert r["per_class"]["fail"]["precision_vote"] == 1.0
    assert r["per_class"]["unknown"]["recall_raw"] == 0.0
    assert r["base_success"] == 0.0
    assert L._read(out / "results.jsonl")[0]["n"] == 4            # in-sample row untouched
    assert L._read(out / "results-hold.jsonl")[0]["n"] == 2
    t = (out / "table-hold.md").read_text()
    assert "per class m|export|v2" in t and "calibration m|export|v2" in t
    assert "no winner is picked" in t and "winner (" not in t


def test_score_per_class_and_base_rate():
    res = [{"gold": "fail", "outcome": "fail", "confidence": 0.95},
           {"gold": "fail", "outcome": "fail", "confidence": 0.5},       # raw hit, not a vote
           {"gold": "fail", "outcome": "unknown", "confidence": 0.95},
           {"gold": "success", "outcome": "fail", "confidence": 0.95},
           {"gold": "success", "outcome": "success", "confidence": 0.99}]
    s = B.score(res, min_conf=0.9)
    f = s["per_class"]["fail"]
    assert (f["n_gold"], f["recall_raw"], f["recall_vote"]) == (3, round(2 / 3, 4), round(1 / 3, 4))
    assert (f["n_vote"], f["precision_vote"]) == (2, 0.5)
    assert (f["n_pred_raw"], f["precision_raw"]) == (3, round(2 / 3, 4))
    assert s["base_success"] == 0.4


def test_bad_sweep_lowers_only_fail_partial_threshold():
    res = [{"gold": "fail", "outcome": "fail", "confidence": 0.95},
           {"gold": "fail", "outcome": "fail", "confidence": 0.75},
           {"gold": "partial", "outcome": "fail", "confidence": 0.75},
           {"gold": "partial", "outcome": "partial", "confidence": 0.65},
           {"gold": "success", "outcome": "success", "confidence": 0.85},   # never a vote at 0.9
           {"gold": "success", "outcome": "partial", "confidence": 0.7}]
    sw = {s["t"]: s for s in B.bad_sweep(res, 0.9)}
    assert sw[0.9]["fail"] == {"recall": 0.5, "k": 1, "n_gold": 2, "n_vote": 1, "precision": 1.0}
    assert sw[0.7]["fail"]["k"] == 2 and sw[0.7]["fail"]["n_vote"] == 3
    assert sw[0.7]["fail"]["precision"] == round(2 / 3, 4)
    assert sw[0.7]["partial"]["n_vote"] == 1 and sw[0.7]["partial"]["k"] == 0
    assert sw[0.6]["partial"]["k"] == 1
    # bad = fail or partial voted on a fail-or-partial gold task
    assert (sw[0.7]["bad"]["k"], sw[0.7]["bad"]["n_vote"], sw[0.7]["bad"]["n_gold"]) == (3, 4, 4)
    assert sw[0.9]["vote_n"] == 1 and sw[0.7]["vote_n"] == 4 and sw[0.7]["acc_vote"] == 0.5
    assert "bad_sweep" in B.score(res, 0.9)
