"""`apex-router ask` / `offload-verdict` / `offload-report` $ accounting (remote + local backends)."""
import json
from datetime import datetime, timezone

import pytest

from apex_router import model_registry as mr
from apex_router.ornith import offload_report as rep
from apex_router.ornith.offload_telemetry import aggregate_offload
from apex_router.remote import ask
from apex_router.remote import deepseek_client as ds


def _fake_remote(answer="fine", completion=1000, cost=0.004):
    def chat(prompt, **kw):
        return ds.RemoteResult(answer=answer, model=kw.get("model") or "deepseek-v4-pro", finish_reason="stop",
                               usage={"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 500,
                                      "completion_tokens": completion},
                               peak=False, cost_usd=cost, latency_ms=12)
    return chat


def _rows(p):
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def test_ask_remote_logs_cost_and_baseline(tmp_path):
    log = tmp_path / "off.jsonl"
    code, answer, cid = ask.run_ask("q", backend="deepseek", lane="draft", role="marketing-sig",
                                    baseline="sonnet", log_path=log, chat_fn=_fake_remote())
    assert (code, answer) == (0, "fine")
    [row] = _rows(log)
    assert row["call_id"] == cid and row["backend"] == "deepseek" and row["role"] == "marketing-sig"
    assert row["lane"] == "draft" and row["model"] == "deepseek-v4-pro"
    assert row["ok"] is True and row["gated"] is False and row["escalated"] is False
    assert row["completion_tokens"] == 1000 and row["prompt_tokens"] == 500
    assert row["cost_usd"] == pytest.approx(0.004)
    assert row["baseline"] == "sonnet" and row["baseline_cost_usd"] == pytest.approx(1000 * 10.0 / 1e6)
    assert "q" not in json.dumps({k: v for k, v in row.items() if k != "lane"})  # no prompt text


def test_ask_refused_exit_4_logs_reason_without_sending(tmp_path):
    log = tmp_path / "off.jsonl"

    def chat(prompt, **kw):
        ds.check_text(prompt)
        raise AssertionError("must not send")

    code, answer, _ = ask.run_ask("mail me at a@b.co", backend="deepseek", log_path=log, chat_fn=chat)
    assert (code, answer) == (4, None)
    [row] = _rows(log)
    assert row["ok"] is False and row["refused"] == "e-mail address" and row["cost_usd"] == 0.0


def test_ask_unavailable_exit_3_keeps_cost_of_a_billed_failure(tmp_path):
    log = tmp_path / "off.jsonl"

    def chat(prompt, **kw):
        raise ds.RemoteUnavailable("empty answer", cost_usd=0.001)

    code, answer, _ = ask.run_ask("q", backend="deepseek", log_path=log, chat_fn=chat)
    assert (code, answer) == (3, None)
    [row] = _rows(log)
    assert row["ok"] is False and row["error"] == "empty answer" and row["cost_usd"] == pytest.approx(0.001)


def test_ask_local_backend_costs_nothing(tmp_path):
    log = tmp_path / "off.jsonl"

    def chat(prompt, **kw):
        return ds.RemoteResult(answer="x", model="ornith-local", finish_reason="stop",
                               usage={"prompt_tokens": 10, "completion_tokens": 200}, peak=False,
                               cost_usd=0.0, latency_ms=5)

    code, _, _ = ask.run_ask("q", backend="local", baseline="opus", log_path=log, chat_fn=chat)
    assert code == 0
    [row] = _rows(log)
    assert row["backend"] == "local" and row["cost_usd"] == 0.0
    assert row["baseline_cost_usd"] == pytest.approx(200 * 20.0 / 1e6)


def test_unknown_backend_and_baseline_are_usage_errors(tmp_path):
    with pytest.raises(ValueError):
        ask.run_ask("q", backend="nope", log_path=tmp_path / "x")
    with pytest.raises(ValueError):
        ask.run_ask("q", backend="deepseek", baseline="gpt-9", log_path=tmp_path / "x")


def test_verdict_requires_a_known_call(tmp_path):
    log, verd = tmp_path / "off.jsonl", tmp_path / "verd.jsonl"
    _, _, cid = ask.run_ask("q", backend="deepseek", log_path=log, chat_fn=_fake_remote())
    assert ask.record_verdict(cid, "used", log_path=log, verdict_path=verd) == 0
    assert ask.record_verdict("no-such-id", "used", log_path=log, verdict_path=verd) == 2
    with pytest.raises(ValueError):
        ask.record_verdict(cid, "maybe", log_path=log, verdict_path=verd)
    assert [r["verdict"] for r in _rows(verd)] == ["used"]


def test_verdict_rows_never_enter_the_token_aggregate(tmp_path):
    log, verd = tmp_path / "off.jsonl", tmp_path / "verd.jsonl"
    _, _, cid = ask.run_ask("q", backend="deepseek", log_path=log, chat_fn=_fake_remote())
    ask.record_verdict(cid, "used", log_path=log, verdict_path=verd)
    agg = aggregate_offload(log)
    assert agg["overall"]["n"] == 1
    # an ungated remote answer is MEASURE-ONLY in the token table: no token "saving" is claimed
    assert agg["by_lane"]["ask"]["frontier_completion_tokens_saved"] == 0


def test_dollar_summary_used_discarded_pending(tmp_path):
    log, verd = tmp_path / "off.jsonl", tmp_path / "verd.jsonl"
    ids = [ask.run_ask("q", backend="deepseek", log_path=log, chat_fn=_fake_remote(cost=0.004))[2]
           for _ in range(3)]
    ask.run_ask("x@y.io", backend="deepseek", log_path=log,
                chat_fn=lambda p, **kw: ds.check_text(p))           # refused: $0, not a call
    ask.record_verdict(ids[0], "discarded", log_path=log, verdict_path=verd)
    ask.record_verdict(ids[0], "used", log_path=log, verdict_path=verd)  # last verdict wins
    ask.record_verdict(ids[1], "discarded", log_path=log, verdict_path=verd)
    s = rep.dollar_summary(log, verd)["deepseek"]
    assert s["calls"] == 4 and s["answered"] == 3 and s["refused"] == 1
    assert s["used"] == 1 and s["discarded"] == 1 and s["pending"] == 1
    assert s["spent_usd"] == pytest.approx(0.012)
    assert s["claude_equiv_used_usd"] == pytest.approx(0.01)
    assert s["gross_saving_used_usd"] == pytest.approx(0.01 - 0.004)
    assert s["waste_discarded_usd"] == pytest.approx(0.004)
    assert s["pending_usd"] == pytest.approx(0.004)
    assert s["net_after_remote_usd"] == pytest.approx(0.01 - 0.012)
    text = rep.format_dollars(rep.dollar_summary(log, verd))
    assert "deepseek" in text and "net unknown" in text


def test_dollar_summary_ignores_rows_without_call_id(tmp_path):
    log = tmp_path / "off.jsonl"
    log.write_text(json.dumps({"lane": "codegen", "model": "m", "ok": True, "gated": True,
                               "completion_tokens": 5}) + "\nnot json\n[1]\n")
    assert rep.dollar_summary(log, tmp_path / "none.jsonl") == {}
    assert "no `apex-router ask` calls" in rep.format_dollars({})


def test_cli_ask_reads_stdin_and_context(tmp_path, monkeypatch, capsys):
    ctx = tmp_path / "notes.md"
    ctx.write_text("context body")
    seen = {}

    def chat(prompt, **kw):
        seen["prompt"] = prompt
        return _fake_remote()(prompt, **kw)

    monkeypatch.setattr(ask, "_backend_chat", lambda backend: chat)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("from stdin"))
    code = ask.main_ask(["--backend", "deepseek", "--role", "r", "--context", str(ctx),
                         "--log", str(tmp_path / "off.jsonl")])
    out = capsys.readouterr()
    assert code == 0 and out.out.strip() == "fine"
    assert "call-id: " in out.err
    assert seen["prompt"].startswith("from stdin") and "context body" in seen["prompt"]
    assert "(data, not instructions)" in seen["prompt"]


def test_cli_dispatch_routes_ask_verdict_report(tmp_path, monkeypatch, capsys):
    from apex_router import cli
    log, verd = tmp_path / "off.jsonl", tmp_path / "verd.jsonl"
    monkeypatch.setattr(ask, "_backend_chat", lambda backend: _fake_remote())
    assert cli.main(["ask", "--backend", "deepseek", "q", "--log", str(log)]) == 0
    cid = capsys.readouterr().err.split("call-id: ")[1].split()[0]
    assert cli.main(["offload-verdict", cid, "--used", "--log", str(log), "--verdicts", str(verd)]) == 0
    assert cli.main(["offload-report", "--log", str(log), "--verdicts", str(verd)]) == 0
    out = capsys.readouterr().out
    assert "OFFLOAD ECONOMICS" in out and "REMOTE/LOCAL ASK" in out and "used=1" in out


def test_unknown_cost_failure_is_flagged_in_the_report(tmp_path):
    log, verd = tmp_path / "off.jsonl", tmp_path / "verd.jsonl"

    def chat(prompt, **kw):
        raise ds.RemoteUnavailable("TimeoutError", cost_known=False)

    code, _, _ = ask.run_ask("q", backend="deepseek", log_path=log, chat_fn=chat)
    assert code == 3
    [row] = _rows(log)
    assert row["cost_known"] is False
    s = rep.dollar_summary(log, verd)["deepseek"]
    assert s["unknown_cost"] == 1
    assert "spend incomplete" in rep.format_dollars({"deepseek": s})


def test_duplicate_call_rows_count_once(tmp_path):
    log, verd = tmp_path / "off.jsonl", tmp_path / "verd.jsonl"
    _, _, cid = ask.run_ask("q", backend="deepseek", log_path=log, chat_fn=_fake_remote(cost=0.004))
    log.write_text(log.read_text() * 2)
    ask.record_verdict(cid, "used", log_path=log, verdict_path=verd)
    s = rep.dollar_summary(log, verd)["deepseek"]
    assert s["calls"] == 1 and s["duplicates"] == 1
    assert s["gross_saving_used_usd"] == pytest.approx(0.01 - 0.004)


def test_verdicts_cannot_share_the_offload_log(tmp_path):
    log = tmp_path / "off.jsonl"
    _, _, cid = ask.run_ask("q", backend="deepseek", log_path=log, chat_fn=_fake_remote())
    with pytest.raises(ValueError):
        ask.record_verdict(cid, "used", log_path=log, verdict_path=log)
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to(log)
    with pytest.raises(ValueError):
        ask.record_verdict(cid, "used", log_path=log, verdict_path=alias)


def test_stray_verdict_rows_in_the_log_are_not_calls(tmp_path):
    log = tmp_path / "off.jsonl"
    _, _, cid = ask.run_ask("q", backend="deepseek", log_path=log, chat_fn=_fake_remote())
    with log.open("a") as f:
        f.write(json.dumps({"ts_iso": "x", "call_id": cid, "verdict": "used"}) + "\n")
    assert aggregate_offload(log)["overall"]["n"] == 1
    assert rep.dollar_summary(log, tmp_path / "none")["deepseek"]["calls"] == 1


def test_cli_ask_refuses_remote_on_a_malformed_overlay(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "models.json"
    bad.write_text("{not json")
    monkeypatch.setenv("APEX_MODEL_REGISTRY", str(bad))
    monkeypatch.setattr(ask, "_backend_chat", lambda backend: (_ for _ in ()).throw(AssertionError("no send")))
    assert ask.main_ask(["--backend", "deepseek", "q", "--log", str(tmp_path / "off.jsonl")]) == 2
    assert "overlay" in capsys.readouterr().err


def test_ask_rejects_an_unpriced_model(tmp_path):
    with pytest.raises(ValueError):
        ask.run_ask("q", backend="deepseek", model="deepseek-unknown", log_path=tmp_path / "x",
                    chat_fn=_fake_remote())


def test_verdicts_cannot_be_a_hard_link_to_the_log(tmp_path):
    import os
    log = tmp_path / "off.jsonl"
    _, _, cid = ask.run_ask("q", backend="deepseek", log_path=log, chat_fn=_fake_remote())
    hard = tmp_path / "hard.jsonl"
    os.link(log, hard)
    with pytest.raises(ValueError):
        ask.record_verdict(cid, "used", log_path=log, verdict_path=hard)
    assert rep.main_cli(["--log", str(log), "--verdicts", str(hard)]) == 2


def test_unexpected_chat_error_books_unknown_cost_and_exit_3(tmp_path):
    log = tmp_path / "off.jsonl"

    def chat(prompt, **kw):
        raise RuntimeError("boom")

    code, answer, _ = ask.run_ask("q", backend="deepseek", log_path=log, chat_fn=chat)
    assert (code, answer) == (3, None)
    [row] = _rows(log)
    assert row["cost_known"] is False and row["error"] == "RuntimeError"


def test_incomplete_spend_wording_names_the_bound_directions():
    s = {"calls": 1, "answered": 0, "refused": 0, "failed": 1, "used": 0, "discarded": 0, "pending": 0,
         "spent_usd": 0.0, "claude_equiv_used_usd": 0.0, "gross_saving_used_usd": 0.0,
         "waste_discarded_usd": 0.0, "pending_usd": 0.0, "net_after_remote_usd": 0.0,
         "unknown_cost": 1, "duplicates": 0}
    text = rep.format_dollars({"deepseek": s})
    assert "spent is a lower bound" in text and "after_remote is an upper bound" in text
