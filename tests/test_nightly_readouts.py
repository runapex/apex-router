"""Daily digest carries the retry-A/B verdict and the v12 refusal panel (step 3 of the 2026-10-12
readout: no one re-runs `retry-ab report` / `apex doctor` by hand to see them)."""
from __future__ import annotations

import json
import time

from apex_router import nightly


def _row(**kw):
    base = {"ts": time.time() - 60, "schema_version": 12, "client": "claude-code", "endpoint_id": "anthropic",
            "model_resolved": "claude-opus-5-5", "is_error": False,
            "usage": {"input_tokens": 10, "output_tokens": 5}}
    base.update(kw)
    return base


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_retry_ab_section_reports_verdict_and_arms(tmp_path, monkeypatch):
    tel = tmp_path / "telemetry.jsonl"
    rows = []
    for arm, rec in (("immediate", 7), ("wait", 9)):
        for i in range(10):
            failed = i >= rec  # a send_stream raise = the retry did NOT recover
            rows.append(_row(retry_policy="ab", retry_arm=arm, retry_propensity=0.5,
                             connect_backoff_ms=300 if arm == "immediate" else 1300,
                             is_error=failed, connect_retries=1,
                             **({"error_cause": "ReadError"} if failed else {})))
    _write(tel, rows)
    monkeypatch.setattr("apex_router.telemetry_path.telemetry_path", lambda: tel)
    text = nightly._retry_ab_section()
    assert "immediate  n=10   recovered=70.0%" in text
    assert "wait       n=10   recovered=90.0%" in text
    assert "wait − immediate (recovered): +20.0 pts" in text
    assert "verdict: INCONCLUSIVE (min 30/arm" in text


def test_retry_ab_section_never_raises(monkeypatch):
    monkeypatch.setattr("apex_router.retry_ab.report", lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
    text = nightly._retry_ab_section()
    assert "unavailable" in text and "RuntimeError" in text


def test_refusals_section_counts_only_v12_rows(tmp_path, monkeypatch):
    tel = tmp_path / "telemetry.jsonl"
    _write(tel, [_row(refusal=True, stop_reason="refusal"), _row(refusal=False, stop_reason="end_turn"),
                 _row(schema_version=11)])  # pre-v12 row: not observed, never read as "no refusal"
    monkeypatch.setattr("apex_router.telemetry_path.telemetry_path", lambda: tel)
    text = nightly._refusals_section()
    assert "refusals 1 / 2 v12 requests" in text
    assert "claude-opus-5-5" in text


def test_refusals_section_names_unpriced_refusals(tmp_path, monkeypatch):
    tel = tmp_path / "telemetry.jsonl"
    _write(tel, [_row(refusal=True, stop_reason="refusal", model_resolved="some-unknown-model")])
    monkeypatch.setattr("apex_router.telemetry_path.telemetry_path", lambda: tel)
    text = nightly._refusals_section()
    assert "refusals 1 / 1 v12 requests" in text and "1 unpriced" in text


def test_retry_ab_section_without_ab_rows_does_not_claim_the_env_is_unset(tmp_path, monkeypatch):
    tel = tmp_path / "telemetry.jsonl"
    _write(tel, [_row(connect_retries=1)])  # a legacy retried row, no arm
    monkeypatch.setattr("apex_router.telemetry_path.telemetry_path", lambda: tel)
    text = nightly._retry_ab_section()
    assert "no randomised rows" in text and "or APEX_RETRY_AB is unset" in text


def test_refusals_section_says_when_nothing_observed(tmp_path, monkeypatch):
    tel = tmp_path / "telemetry.jsonl"
    _write(tel, [_row(schema_version=11)])
    monkeypatch.setattr("apex_router.telemetry_path.telemetry_path", lambda: tel)
    assert "no v12 rows" in nightly._refusals_section()


def test_run_includes_both_sections(monkeypatch):
    monkeypatch.setattr(nightly, "_retry_ab_section", lambda: "  RA")
    monkeypatch.setattr(nightly, "_refusals_section", lambda: "  RF")
    monkeypatch.setattr(nightly, "_route_join_section", lambda: "  RJ")
    monkeypatch.setattr(nightly, "_advise_section", lambda: "  AD")
    monkeypatch.setattr(nightly, "_run_script", lambda *a, **k: (0, ""))
    monkeypatch.setattr(nightly, "_memory_dirs", lambda env=None: [])
    monkeypatch.setattr(nightly, "_codex_context_watch", lambda now, **k: "  CW")
    from apex_router import skillopt_bench
    monkeypatch.setattr(skillopt_bench, "run_nightly", lambda: "\n### skill-quality bench\n  SB\n")
    out = nightly.run(now=0.0)
    assert "### retry A/B" in out and "  RA" in out
    assert "### refusals (v12)" in out and "  RF" in out
