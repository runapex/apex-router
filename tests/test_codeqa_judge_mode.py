"""codeqa validate — "screen, then adjudicate" judge modes (CODEQA_JUDGE_MODE).

A pinned CODEQA_JUDGE_MODEL used to bypass the tier router entirely (every claim to the pinned,
usually heaviest, model). Now:
  pinned → today's behaviour (every claim to the pinned model)
  screen → (default when pinned) each claim goes to the routed tier first; only claims the screen
           did not clear (CONTRADICTED / UNVERIFIABLE / unparsable / empty) are re-judged by the pinned
           model, whose verdict wins
  both   → every claim to both; agreement recorded; pinned wins (calibration)
Unset CODEQA_JUDGE_MODEL → unchanged.

All offline: verifiers are fakes; the HTTP seam is monkeypatched.
"""
from __future__ import annotations

import json

import pytest

from apex_router.codeqa import freshness, tier_router
from apex_router.codeqa.freshness import validate_memory

PIN = "claude-pinned-test"


# ---------- tier_router: honor_override + judge_mode ----------

def test_resolve_can_ignore_the_pin():
    env = {"CODEQA_JUDGE_MODEL": PIN}
    assert tier_router.resolve("value", env=env).model == PIN               # today: pin wins
    r = tier_router.resolve("value", env=env, honor_override=False)
    assert r.tier == "haiku" and not r.fixed                                 # screen: routed tier


@pytest.mark.parametrize("env,expected", [
    ({}, None),                                                               # no pin → no judge mode
    ({"CODEQA_JUDGE_MODE": "both"}, None),                                    # mode without pin → inert
    ({"CODEQA_JUDGE_MODEL": PIN}, "screen"),                                  # NEW default when pinned
    ({"CODEQA_JUDGE_MODEL": PIN, "CODEQA_JUDGE_MODE": "pinned"}, "pinned"),
    ({"CODEQA_JUDGE_MODEL": PIN, "CODEQA_JUDGE_MODE": "BOTH"}, "both"),
    ({"CODEQA_JUDGE_MODEL": PIN, "CODEQA_JUDGE_MODE": " screen "}, "screen"),
    ({"CODEQA_JUDGE_MODEL": PIN, "CODEQA_JUDGE_MODE": "bogus"}, "screen"),   # invalid → safe default
])
def test_judge_mode_contract(env, expected):
    assert tier_router.judge_mode(env) == expected


# ---------- freshness: the verifier seams ----------

def _capture_http(monkeypatch):
    from apex_router.codeqa import judge
    sent = []

    def fake_post(url, body, headers, *, timeout):
        sent.append(json.loads(body))
        return {"content": [{"type": "text", "text": "SUPPORTED"}]}
    monkeypatch.setattr(judge, "_http_post_json", fake_post)
    monkeypatch.setenv("CODEQA_JUDGE_BASE", "https://judge.example.invalid")
    return sent


def test_routed_frontier_verifier_ignores_the_pin(monkeypatch):
    sent = _capture_http(monkeypatch)
    monkeypatch.setenv("CODEQA_JUDGE_MODEL", PIN)
    assert freshness.routed_frontier_verifier("the MAX_ITEMS constant is 30", "x") == "SUPPORTED"
    assert sent[-1]["model"] == tier_router.resolve("value", honor_override=False).model
    assert sent[-1]["model"] != PIN


def test_pinned_verifier_sends_the_pinned_model(monkeypatch):
    sent = _capture_http(monkeypatch)
    assert freshness.pinned_verifier(PIN)("has_policy defaults to false so no transforms fire", "x") \
        == "SUPPORTED"
    assert sent[-1]["model"] == PIN
    assert "output_config" not in sent[-1]                                    # pin: no effort (as today)


def test_frontier_verifier_still_honors_the_pin(monkeypatch):
    sent = _capture_http(monkeypatch)
    monkeypatch.setenv("CODEQA_JUDGE_MODEL", PIN)
    freshness.frontier_verifier("the MAX_ITEMS constant is 30", "x")
    assert sent[-1]["model"] == PIN                                           # pinned mode unchanged


# ---------- validate_memory: screen / both ----------

def _repo(tmp_path):
    (tmp_path / "conf.py").write_text("MAX_ITEMS = 30\nTIMEOUT_SECS = 5\nRETRY_LIMIT = 7\n")
    return tmp_path


_MEM = (
    "- The MAX_ITEMS constant is set to 30 in conf.py for the batch envelope.\n"     # screen SUPPORTED
    "- The TIMEOUT_SECS constant is set to 99 in conf.py for every request.\n"       # screen CONTRADICTED
    "- The RETRY_LIMIT constant is set to 7 in conf.py for the worker loop.\n"       # screen garbage
)


def _screen(claim, code):
    if "TIMEOUT_SECS" in claim:
        return "CONTRADICTED"
    if "RETRY_LIMIT" in claim:
        return "lol idk"                       # unparsable → UNVERIFIABLE → must be adjudicated
    return "SUPPORTED"


def test_screen_only_adjudicates_claims_the_screen_did_not_clear(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEQA_JUDGE_MODEL", raising=False)
    adjudicated = []

    def pinned(claim, code):
        adjudicated.append(claim)
        return "SUPPORTED" if "RETRY_LIMIT" in claim else "CONTRADICTED"

    res = validate_memory(_MEM, _repo(tmp_path), verify_fn=_screen, adjudicate_fn=pinned,
                          judge_mode="screen", adjudicator_model=PIN, max_workers=1)
    assert len(adjudicated) == 2 and not any("MAX_ITEMS" in c for c in adjudicated)
    assert res.judge_mode == "screen"
    assert res.n_screened == 3
    assert res.n_adjudicated == 2
    assert res.n_screen_struck == 1
    assert res.n_adjudicated_struck == 1
    assert res.n_struck == 1 and "TIMEOUT_SECS" in res.struck_claims[0]
    haiku = tier_router.resolve("value", honor_override=False).model
    assert res.model_calls == {haiku: 3, PIN: 2}                              # keyed by real model id
    assert res.tier_calls == {"haiku": 3, PIN: 2}
    assert res.n_frontier == 3                                                # claims touching frontier
    assert res.n_frontier_calls == 5                                          # paid calls actually made


def test_pinned_verdict_wins_and_can_clear_a_screen_strike(tmp_path):
    res = validate_memory(_MEM, _repo(tmp_path), verify_fn=_screen,
                          adjudicate_fn=lambda c, e: "SUPPORTED", judge_mode="screen",
                          adjudicator_model=PIN, max_workers=1)
    assert res.n_screen_struck == 1 and res.n_struck == 0 and res.n_adjudicated_struck == 0


def test_both_mode_judges_every_claim_twice_and_records_agreement(tmp_path):
    res = validate_memory(_MEM, _repo(tmp_path), verify_fn=_screen,
                          adjudicate_fn=lambda c, e: "SUPPORTED", judge_mode="both",
                          adjudicator_model=PIN, max_workers=1)
    assert res.n_adjudicated == 3
    assert res.n_agree == 1 and res.n_compared == 3                           # only MAX_ITEMS agreed
    assert res.agreement == pytest.approx(1 / 3)
    assert res.n_struck == 0                                                  # pinned wins


def test_screen_with_local_skips_the_intermediate_frontier_confirm(tmp_path):
    # Routed (local) screen: a local strike goes STRAIGHT to the adjudicator — no extra routed-frontier
    # confirm call in between (the adjudicator IS the confirm).
    frontier_calls = []
    res = validate_memory(
        "- The TIMEOUT_SECS constant is set to 99 in conf.py for every request.\n",
        _repo(tmp_path), verify_fn=lambda c, e: frontier_calls.append(c) or "SUPPORTED",
        local_verify_fn=lambda c, e: "CONTRADICTED", adjudicate_fn=lambda c, e: "CONTRADICTED",
        judge_mode="screen", adjudicator_model=PIN, local_model="ornith-test", max_workers=1)
    assert frontier_calls == []
    assert res.n_struck == 1 and res.n_local == 0 and res.n_frontier == 1
    assert res.model_calls == {"ornith-test": 1, PIN: 1}


def test_screen_skips_adjudication_when_screen_model_is_the_pinned_model(tmp_path, monkeypatch):
    opus = tier_router.resolve("runtime", honor_override=False).model
    calls = []
    res = validate_memory(
        "- The worker daemon is currently loaded and draining the inbox right now.\n",
        _repo(tmp_path), verify_fn=lambda c, e: "UNVERIFIABLE",
        adjudicate_fn=lambda c, e: calls.append(c) or "SUPPORTED", runtime_facts="worker: loaded",
        judge_mode="screen", adjudicator_model=opus, max_workers=1)
    assert calls == [] and res.n_adjudicated == 0 and res.n_screened == 1


def test_no_adjudicator_keeps_legacy_shape(tmp_path):
    res = validate_memory(_MEM, _repo(tmp_path), verify_fn=_screen, max_workers=1)
    assert res.judge_mode is None and res.n_adjudicated == 0 and res.agreement is None
    assert res.n_struck == 1
    assert sum(res.tier_calls.values()) == res.n_frontier == res.n_frontier_calls


# ---------- dry-run plan ----------

def test_plan_validation_calls_no_model(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEQA_JUDGE_MODEL", PIN)
    rows = freshness.plan_validation(
        _MEM + "- has_policy defaults to false so no transforms fire at all.\n"
               "- we prefer small verified steps as a team overall.\n",
        _repo(tmp_path), judge_mode="screen", local=False)
    by = {r["claim"][2:16]: r for r in rows}
    assert by["The MAX_ITEMS "]["screen"] == tier_router.resolve("value", honor_override=False).model
    assert by["The MAX_ITEMS "]["adjudicate"] == f"if not SUPPORTED → {PIN}"
    assert by["The MAX_ITEMS "]["evidence"] is True
    assert by["has_policy def"]["evidence"] is False                         # unresolved → no call
    assert by["has_policy def"]["screen"] == tier_router.resolve("inference",
                                                                 honor_override=False).model


def test_plan_validation_pinned_and_local(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEQA_JUDGE_MODEL", PIN)
    rows = freshness.plan_validation(_MEM, _repo(tmp_path), judge_mode="pinned", local=False)
    assert {r["screen"] for r in rows} == {PIN} and {r["adjudicate"] for r in rows} == {"-"}
    rows = freshness.plan_validation(
        _MEM + "- we prefer small verified steps as a team overall.\n", _repo(tmp_path),
        judge_mode="both", local=True, local_model="ornith-x")
    assert rows[-1]["screen"] == "skip (non-derivable)" and rows[-1]["adjudicate"] == "-"
    assert {r["screen"] for r in rows[:-1]} == {"local:ornith-x"}
    assert {r["adjudicate"] for r in rows[:-1]} == {f"always → {PIN}"}


# ---------- metrics ----------

def test_metrics_record_prices_actual_paid_calls(tmp_path):
    out = tmp_path / "m.jsonl"
    freshness.metrics_record(out, {"n_frontier": 3, "n_frontier_calls": 5}, ts="t")
    assert json.loads(out.read_text())["est_frontier_tokens"] == 5 * 276


def test_emit_metrics_row_has_judge_fields(tmp_path, monkeypatch):
    from apex_router.codeqa import cli
    monkeypatch.setattr(cli, "_METRICS_PATH", tmp_path / "m.jsonl")
    res = validate_memory(_MEM, _repo(tmp_path), verify_fn=_screen,
                          adjudicate_fn=lambda c, e: "SUPPORTED", judge_mode="both",
                          adjudicator_model=PIN, max_workers=1)
    cli._emit_metrics("r", "f.md", res, cached=False, routed=False, local_only=False, runtime=False,
                      judge_model=PIN)
    row = json.loads((tmp_path / "m.jsonl").read_text())
    for k in ("n_checked", "n_struck", "n_local", "n_frontier", "n_skipped", "tier_calls", "routed",
              "local_only", "cached", "runtime", "est_frontier_tokens"):
        assert k in row                                                       # old fields kept
    assert row["judge_mode"] == "both" and row["judge_model"] == PIN
    assert row["n_screened"] == 3 and row["n_adjudicated"] == 3
    assert row["n_screen_struck"] == 1 and row["n_adjudicated_struck"] == 0
    assert row["agreement"] == pytest.approx(1 / 3)
    assert row["model_calls"][PIN] == 3
    assert row["est_frontier_tokens"] == 6 * 276


def test_readers_tolerate_new_fields(tmp_path, capsys):
    from apex_router.codeqa import metrics_report
    from apex_router.ornith import offload_report
    p = tmp_path / "m.jsonl"
    rows = [
        {"repo": "old", "n_checked": 20, "n_struck": 0, "n_local": 0, "n_frontier": 20,
         "tier_calls": {"haiku": 18, "sonnet": 2}, "est_frontier_tokens": 5520},
        {"repo": "new", "n_checked": 64, "n_struck": 1, "n_local": 0, "n_frontier": 64,
         "n_frontier_calls": 70, "tier_calls": {"haiku": 64, "claude-opus-5-5": 6},
         "model_calls": {"claude-haiku-4-5": 64, "claude-opus-5-5": 6}, "judge_mode": "screen",
         "judge_model": "claude-opus-5-5", "n_screened": 64, "n_adjudicated": 6,
         "n_screen_struck": 2, "n_adjudicated_struck": 1, "agreement": None,
         "est_frontier_tokens": 70 * 276},
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert metrics_report.main(str(p)) == 0
    out = capsys.readouterr().out
    assert "claude-opus-5-5" in out and "adjudicated" in out
    s = offload_report.summarize_codeqa_validate(p)
    assert s["n_frontier"] == 84 and s["est_frontier_tokens"] == 5520 + 70 * 276


def test_metrics_report_prices_model_ids_by_family():
    from apex_router.codeqa import metrics_report
    assert metrics_report._rate("claude-haiku-4-5") == metrics_report._rate("haiku")
    assert metrics_report._rate("claude-opus-5-5") == metrics_report._rate("opus")
    assert metrics_report._rate("mystery") == metrics_report._rate("opus")    # unknown → opus floor


# ---------- cli wiring ----------

@pytest.mark.parametrize("env,local,route,expected", [
    ({}, False, False, (None, None, False, False)),                                   # unchanged
    ({"CODEQA_LOCAL_SCREEN": "1"}, False, False, (None, None, False, False)),         # inert unpinned
    ({"CODEQA_JUDGE_MODEL": PIN}, False, False, ("screen", PIN, True, False)),
    ({"CODEQA_JUDGE_MODEL": PIN, "CODEQA_LOCAL_SCREEN": "1"}, False, False, ("screen", PIN, True, True)),
    ({"CODEQA_JUDGE_MODEL": PIN, "CODEQA_JUDGE_MODE": "pinned", "CODEQA_LOCAL_SCREEN": "1"},
     False, False, ("pinned", PIN, False, False)),
    ({"CODEQA_JUDGE_MODEL": PIN}, True, False, ("screen", PIN, False, False)),        # --local: no paid
    ({"CODEQA_JUDGE_MODEL": PIN, "CODEQA_JUDGE_MODE": "both"}, False, True, ("both", PIN, True, True)),
])
def test_cli_judge_setup(env, local, route, expected):
    from apex_router.codeqa import cli
    assert cli._judge_setup(local=local, route=route, env=env) == expected


def _cli_env(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from apex_router.codeqa import cli, retriever
    (tmp_path / "repo").mkdir()
    root = _repo(tmp_path / "repo")
    mem = tmp_path / "mem.md"
    mem.write_text(_MEM)
    monkeypatch.setattr(retriever.RepoConfig, "load",
                        classmethod(lambda cls, name: SimpleNamespace(root=root, raw={}, digest=None)))
    monkeypatch.setattr(cli, "_METRICS_PATH", tmp_path / "m.jsonl")
    monkeypatch.setattr(cli, "_FRESHNESS_CACHE", tmp_path / "cache.json")
    monkeypatch.setenv("CODEQA_JUDGE_MODEL", PIN)
    for k in ("CODEQA_JUDGE_MODE", "CODEQA_LOCAL_SCREEN"):
        monkeypatch.delenv(k, raising=False)
    return cli, mem


def test_cli_plan_calls_no_model_and_writes_nothing(tmp_path, monkeypatch, capsys):
    cli, mem = _cli_env(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise AssertionError("--plan must not call a model")
    monkeypatch.setattr(freshness, "_frontier_call", boom)
    monkeypatch.setattr(cli, "_local_verifier", boom)
    assert cli.main(["validate", "r", str(mem), "--plan"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and f"if not SUPPORTED → {PIN}" in out
    assert not (tmp_path / "m.jsonl").exists() and not (tmp_path / "cache.json").exists()


def test_cli_screen_mode_wires_routed_screen_and_pinned_adjudicator(tmp_path, monkeypatch):
    cli, mem = _cli_env(tmp_path, monkeypatch)
    sent = []

    def fake_call(claim, code, route):
        sent.append(route.model)
        return "SUPPORTED" if route.model == PIN else _screen(claim, code)
    monkeypatch.setattr(freshness, "_frontier_call", fake_call)
    assert cli.main(["validate", "r", str(mem), "--no-cache"]) == 0
    haiku = tier_router.resolve("value", honor_override=False).model
    assert sorted(sent) == sorted([haiku] * 3 + [PIN] * 2)
    row = json.loads((tmp_path / "m.jsonl").read_text())
    assert row["judge_mode"] == "screen" and row["judge_model"] == PIN
    assert row["n_adjudicated"] == 2 and row["n_struck"] == 0 and row["n_screen_struck"] == 1
    assert row["model_calls"] == {haiku: 3, PIN: 2}


def test_cli_pinned_mode_is_legacy(tmp_path, monkeypatch):
    cli, mem = _cli_env(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEQA_JUDGE_MODE", "pinned")
    sent = []
    monkeypatch.setattr(freshness, "_frontier_call",
                        lambda claim, code, route: sent.append(route.model) or "SUPPORTED")
    cli.main(["validate", "r", str(mem), "--no-cache"])
    assert sent == [PIN] * 3
    row = json.loads((tmp_path / "m.jsonl").read_text())
    assert row["judge_mode"] == "pinned" and row["tier_calls"] == {PIN: 3} and row["n_adjudicated"] == 0


# ---------- F1: a FAILED adjudication must not overwrite the screen verdict ----------

_STALE = "- The TIMEOUT_SECS constant is set to 99 in conf.py for every request.\n"


def _transport_fail(claim, code):
    raise ConnectionError("adjudicator endpoint unreachable")


@pytest.mark.parametrize("adjudicator", [
    _transport_fail,                                     # transport error (check_claim swallows it)
    lambda c, e: "",                                     # empty reply (no endpoint configured)
    lambda c, e: "lol idk",                              # unparsable reply
], ids=["transport", "empty", "unparsable"])
def test_screen_strike_survives_a_failed_adjudication(tmp_path, adjudicator):
    res = validate_memory(_STALE, _repo(tmp_path), verify_fn=lambda c, e: "CONTRADICTED",
                          adjudicate_fn=adjudicator, judge_mode="screen",
                          adjudicator_model=PIN, max_workers=1)
    assert res.n_struck == 1 and "TIMEOUT_SECS" in res.struck_claims[0]      # screen strike kept
    assert "STALE" in res.text
    assert res.n_adjudicate_failed == 1
    assert res.n_adjudicated == 0 and res.n_adjudicated_struck == 0
    assert res.n_frontier_calls == 1                                          # only the screen call
    assert PIN not in res.model_calls and PIN not in res.tier_calls


def test_screen_strike_cleared_by_a_real_adjudication(tmp_path):
    res = validate_memory(_STALE, _repo(tmp_path), verify_fn=lambda c, e: "CONTRADICTED",
                          adjudicate_fn=lambda c, e: "SUPPORTED", judge_mode="screen",
                          adjudicator_model=PIN, max_workers=1)
    assert res.n_struck == 0 and res.n_adjudicated == 1 and res.n_adjudicate_failed == 0
    assert res.n_frontier_calls == 2


def test_screen_unverifiable_with_failed_adjudication_stays_unverifiable(tmp_path):
    res = validate_memory(_STALE, _repo(tmp_path), verify_fn=lambda c, e: "UNVERIFIABLE",
                          adjudicate_fn=_transport_fail, judge_mode="screen",
                          adjudicator_model=PIN, max_workers=1)
    assert res.n_struck == 0 and res.n_checked == 1
    assert res.n_adjudicate_failed == 1 and res.n_adjudicated == 0


def test_explicit_unverifiable_from_adjudicator_counts_as_judged(tmp_path):
    res = validate_memory(_STALE, _repo(tmp_path), verify_fn=lambda c, e: "CONTRADICTED",
                          adjudicate_fn=lambda c, e: "UNVERIFIABLE", judge_mode="screen",
                          adjudicator_model=PIN, max_workers=1)
    assert res.n_struck == 0 and res.n_adjudicated == 1 and res.n_adjudicate_failed == 0


def test_both_mode_failed_adjudication_keeps_screen_and_skips_agreement(tmp_path):
    res = validate_memory(_MEM, _repo(tmp_path), verify_fn=_screen,
                          adjudicate_fn=_transport_fail, judge_mode="both",
                          adjudicator_model=PIN, max_workers=1)
    assert res.n_adjudicate_failed == 3 and res.n_adjudicated == 0
    assert res.n_compared == 0 and res.agreement is None
    assert res.n_struck == 1 and "TIMEOUT_SECS" in res.struck_claims[0]


def test_cli_failed_adjudication_strikes_exits_nonzero_and_writes_no_cache(tmp_path, monkeypatch):
    cli, mem = _cli_env(tmp_path, monkeypatch)

    def fake_call(claim, code, route):
        if route.model == PIN:
            return ""                                    # _frontier_call's transport-failure reply
        return _screen(claim, code)
    monkeypatch.setattr(freshness, "_frontier_call", fake_call)
    assert cli.main(["validate", "r", str(mem), "--check"]) != 0
    row = json.loads((tmp_path / "m.jsonl").read_text())
    assert row["n_struck"] == 1 and row["n_adjudicate_failed"] == 2 and row["n_adjudicated"] == 0
    haiku = tier_router.resolve("value", honor_override=False).model
    assert row["n_frontier_calls"] == 3 and row["model_calls"] == {haiku: 3}
    cache = json.loads((tmp_path / "cache.json").read_text()) if (tmp_path / "cache.json").exists() \
        else {}
    assert not cache                                     # next run retries adjudication


# ---------- F2: the cache fingerprint must track the tier model ids ----------

def test_validate_fingerprint_tracks_tier_model_ids():
    from apex_router.codeqa import cli
    env = {"CODEQA_JUDGE_MODEL": PIN}
    a = cli._model_fingerprint(local=False, local_screen=False, pin=PIN, jm="screen", env=env)
    assert a == cli._model_fingerprint(local=False, local_screen=False, pin=PIN, jm="screen", env=env)
    bumped = dict(env, CODEQA_TIER_MODELS="opus=claude-opus-9-9")
    assert a != cli._model_fingerprint(local=False, local_screen=False, pin=PIN, jm="screen",
                                       env=bumped)
    for tier in ("haiku", "sonnet"):
        assert a != cli._model_fingerprint(local=False, local_screen=False, pin=PIN, jm="screen",
                                           env=dict(env, CODEQA_TIER_MODELS=f"{tier}=x-new"))
    assert a != cli._model_fingerprint(local=False, local_screen=False, pin="other-pin", jm="screen",
                                       env={"CODEQA_JUDGE_MODEL": "other-pin"})
    # unpinned frontier runs route through the same tiers, so they must track them too
    u = cli._model_fingerprint(local=False, local_screen=False, pin=None, jm=None, env={})
    assert u != cli._model_fingerprint(local=False, local_screen=False, pin=None, jm=None,
                                       env={"CODEQA_TIER_MODELS": "haiku=x-new"})


def test_validate_fingerprint_tracks_local_model_when_local_screening(monkeypatch):
    from apex_router.codeqa import cli
    env = {"CODEQA_JUDGE_MODEL": PIN}
    monkeypatch.setattr(cli, "_local_model_id", lambda: "ornith-a")
    a = cli._model_fingerprint(local=False, local_screen=True, pin=PIN, jm="screen", env=env)
    monkeypatch.setattr(cli, "_local_model_id", lambda: "ornith-b")
    assert a != cli._model_fingerprint(local=False, local_screen=True, pin=PIN, jm="screen", env=env)
