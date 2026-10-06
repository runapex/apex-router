"""scripts/sd_baseline.py on synthetic telemetry: the plan's B-lines compute and render."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sd_baseline.py"


def _mod():
    spec = importlib.util.spec_from_file_location("sd_baseline", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_baseline_lines(tmp_path, capsys):
    rows = [{"ev": "hb", "ts": 1}]
    for i in range(200):
        err = i in (50, 51, 52)
        rows.append({"ts": 1000.0 + i * 30, "client": "claude-code" if i % 2 else "codex",
                     "is_error": err, "error_cause": "ConnectError" if err else None,
                     "upstream_error_wait_ms": 500 if err else None, "connect_retries": 2 if err else 0,
                     "apex_added_ms": 10 + i % 40, "matcher_event": "client_edit", "session_id": "s1",
                     "usage": {"x": 1}, "tokens_in": 5, "cache_read_tokens": 1000,
                     "cache_write_tokens": 30000 if i == 199 else 0, "tokens_out": 100,
                     "shadow": {"context_bytes": 1000}})
    tel = tmp_path / "t.jsonl"
    tel.write_text("".join(json.dumps(r) + "\n" for r in rows))
    m = _mod()
    assert m.main(["--telemetry", str(tel), "--json"]) == 0
    b = json.loads(capsys.readouterr().out)
    assert b["calls"] == 200 and b["failed"] == 3
    assert b["apex_added"]["over_20ms"] > 0
    assert b["codex_longest_failure_run"] == 2          # codex failures at i=50 and 52 are consecutive codex rows
    assert b["time_to_fail_s"]["ConnectError"]["n"] == 3
    assert b["sessions"]["n"] == 1 and b["http_429"] == 0
    assert 0 < b["claude_cache_write_cost_share"] < 1
    assert "stage_ms" in b["missing_fields"]
    assert m.main(["--telemetry", str(tel)]) == 0
    assert "B6 not recorded yet" in capsys.readouterr().out


def test_empty(tmp_path):
    assert _mod().main(["--telemetry", str(tmp_path / "none")]) == 1
