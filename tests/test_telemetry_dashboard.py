"""Smoke test for scripts/telemetry_dashboard.py on synthetic telemetry (skips without matplotlib)."""
from __future__ import annotations

import importlib.util
import json
import random
import time
from pathlib import Path

import pytest

pytest.importorskip("matplotlib")
pytest.importorskip("numpy")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "telemetry_dashboard.py"


def _load():
    spec = importlib.util.spec_from_file_location("telemetry_dashboard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dashboard_renders_self_contained_html(tmp_path):
    rng = random.Random(0)
    now = time.time()
    rows = [{"ev": "hb", "ts": now}]
    for i in range(600):
        client = "codex" if i % 3 else "claude-code"
        err = rng.random() < 0.03
        rows.append({"ts": now - 4 * 86400 + i * 500, "client": client, "is_error": err,
                     "error_cause": rng.choice([None, "ReadError"]) if err else None,
                     "stratum": rng.choice(["s", "m", "l"]),
                     "session_id": f"s{i // 40}" if client == "claude-code" else None,
                     "ttft_ms": rng.lognormvariate(7, 0.5), "t_upstream_ttfb_ms": rng.lognormvariate(7, 0.5),
                     "apex_added_ms": rng.lognormvariate(3, 0.7), "tokens_in": 50000, "tokens_out": 400,
                     "cache_read_tokens": 40000, "cache_write_tokens": 0, "usage": {"captured": True}})
    tel = tmp_path / "telemetry.jsonl"
    tel.write_text("".join(json.dumps(r) + "\n" for r in rows))
    xv = tmp_path / "xval_runs.jsonl"
    xv.write_text(json.dumps({"arm": "2000/open", "ok": True, "cost": 10.0}) + "\n")
    out = tmp_path / "d.html"
    assert _load().main(["--telemetry", str(tel), "--xval-runs", str(xv), "-o", str(out), "--days", "10"]) == 0
    page = out.read_text()
    assert page.count("data:image/png;base64,") == 11
    assert "p99 [95% CI]" in page and "not</b> whether answers were right" in page
    assert str(tmp_path) not in page            # sources shown by file name only


def test_dashboard_empty_window_exits_1(tmp_path):
    assert _load().main(["--telemetry", str(tmp_path / "none.jsonl"), "-o", str(tmp_path / "x.html")]) == 1
