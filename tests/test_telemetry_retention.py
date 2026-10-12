"""Item 1: rotating telemetry must never destroy history, and offline readers must see the previous generation.

Incident 2026-10-12: rotation did `Path.replace(... '.1')`, silently overwriting the previous `.1` (the
09-27..09-30 rows were lost), and every reader opened only the live file (a 0.7 h window after rotation).
Design (after two cross-validation passes): append-only generations `.<N>`, HIGHER N = NEWER, one atomic rename
per rotation, nothing renumbered; readers open live first, then the newest rotated file(s), deduped by inode."""
from __future__ import annotations

import json

from apex_router.proxy_engine.telemetry.events import TelemetryWriter
from apex_router.telemetry_path import iter_telemetry_lines, telemetry_files


def _fill(p, tag):
    p.write_text(json.dumps({"ts": 1.0, "tag": tag, "is_error": False}) + "\n" + "x" * 200)


def _tag(p):
    return json.loads(p.read_text().splitlines()[0])["tag"]


def _gens(d, name="t.jsonl"):
    return sorted(int(x.name.rsplit(".", 1)[1]) for x in d.iterdir() if x.name.startswith(name + ".") and x.name.rsplit(".", 1)[1].isdigit())


# ---- writer ----
def test_rotation_never_overwrites_and_newest_is_highest(tmp_path):
    p = tmp_path / "t.jsonl"; w = TelemetryWriter(p); w.ROTATE_BYTES = 100
    for tag in "ABC":
        _fill(p, tag); w.rotate_if_large()
    assert _gens(tmp_path) == [1, 2, 3]
    assert [_tag(tmp_path / f"t.jsonl.{n}") for n in (1, 2, 3)] == ["A", "B", "C"]


def test_existing_legacy_dot1_becomes_the_oldest(tmp_path):
    p = tmp_path / "t.jsonl"; _fill(tmp_path / "t.jsonl.1", "LEGACY")      # left by the old writer
    w = TelemetryWriter(p); w.ROTATE_BYTES = 100; _fill(p, "NEW"); w.rotate_if_large()
    assert _tag(tmp_path / "t.jsonl.1") == "LEGACY" and _tag(tmp_path / "t.jsonl.2") == "NEW"


def test_retention_cap_drops_only_the_oldest(tmp_path):
    p = tmp_path / "t.jsonl"; w = TelemetryWriter(p); w.ROTATE_BYTES = 100; w.KEEP = 2
    for tag in "ABCD":
        _fill(p, tag); w.rotate_if_large()
    assert _gens(tmp_path) == [3, 4] and _tag(tmp_path / "t.jsonl.3") == "C"


def test_lowering_keep_prunes_older_generations(tmp_path):
    p = tmp_path / "t.jsonl"; w = TelemetryWriter(p); w.ROTATE_BYTES = 100; w.KEEP = 4
    for tag in "ABCD":
        _fill(p, tag); w.rotate_if_large()
    w.KEEP = 2; _fill(p, "E"); w.rotate_if_large()
    assert _gens(tmp_path) == [4, 5] and _tag(tmp_path / "t.jsonl.5") == "E"


def test_keep_env_override_and_floor(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_TELEMETRY_KEEP", "garbage")
    assert TelemetryWriter(tmp_path / "t.jsonl").KEEP == TelemetryWriter.DEFAULT_KEEP
    monkeypatch.setenv("APEX_TELEMETRY_KEEP", "0")
    assert TelemetryWriter(tmp_path / "t.jsonl").KEEP == 1
    monkeypatch.setenv("APEX_TELEMETRY_KEEP", "7")
    assert TelemetryWriter(tmp_path / "t.jsonl").KEEP == 7


def test_failed_rename_never_drops_history_even_repeatedly(tmp_path, monkeypatch):
    p = tmp_path / "t.jsonl"; w = TelemetryWriter(p); w.ROTATE_BYTES = 100; w.KEEP = 3
    for tag in "ABC":
        _fill(p, tag); w.rotate_if_large()
    _fill(p, "D")
    monkeypatch.setattr(type(p), "replace", lambda self, tgt: (_ for _ in ()).throw(OSError("busy")))
    for _ in range(5):
        w.rotate_if_large()
    assert _gens(tmp_path) == [1, 2, 3] and [_tag(tmp_path / f"t.jsonl.{n}") for n in (1, 2, 3)] == ["A", "B", "C"] and p.exists()


def test_failed_prune_only_leaves_extra_files_and_recovers(tmp_path, monkeypatch):
    p = tmp_path / "t.jsonl"; w = TelemetryWriter(p); w.ROTATE_BYTES = 100; w.KEEP = 2
    for tag in "AB":
        _fill(p, tag); w.rotate_if_large()
    real_unlink = type(p).unlink
    monkeypatch.setattr(type(p), "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(OSError("ro")))
    _fill(p, "C"); w.rotate_if_large()
    assert _gens(tmp_path) == [1, 2, 3]                                    # nothing lost, one extra file
    monkeypatch.setattr(type(p), "unlink", real_unlink)
    _fill(p, "D"); w.rotate_if_large()
    assert _gens(tmp_path) == [3, 4]                                       # next tick catches up


def test_rotation_below_threshold_is_a_no_op(tmp_path):
    p = tmp_path / "t.jsonl"; p.write_text("{}\n"); w = TelemetryWriter(p)
    w.rotate_if_large()
    assert _gens(tmp_path) == [] and p.exists()


# ---- file listing ----
def test_telemetry_files_oldest_first_and_live_last(tmp_path):
    for n in ("t.jsonl", "t.jsonl.1", "t.jsonl.3", "t.jsonl.2", "t.jsonl.10", "t.jsonl.pre-v2-bak", "t.jsonl.1.tmp", "other.jsonl.1"):
        (tmp_path / n).write_text("{}\n")
    assert [x.name for x in telemetry_files(tmp_path / "t.jsonl")] == ["t.jsonl.1", "t.jsonl.2", "t.jsonl.3", "t.jsonl.10", "t.jsonl"]
    assert [x.name for x in telemetry_files(tmp_path / "t.jsonl", 1)] == ["t.jsonl.10", "t.jsonl"]   # numeric, not lexical
    assert [x.name for x in telemetry_files(tmp_path / "t.jsonl", 0)] == ["t.jsonl"]


def test_telemetry_files_missing_live_or_all(tmp_path):
    assert telemetry_files(tmp_path / "t.jsonl") == []
    (tmp_path / "t.jsonl.1").write_text("{}\n")
    assert [x.name for x in telemetry_files(tmp_path / "t.jsonl")] == ["t.jsonl.1"]


# ---- readers ----
def _req(ts, err=False):
    return json.dumps({"ts": ts, "schema_version": 12, "is_error": err, "client": "codex", "endpoint_id": "openai",
                       "model_resolved": "it-entra-gpt-6-astra", "session_id": "s", "tokens_in": 1, "tokens_out": 1,
                       "usage": {"captured": True, "input_tokens": 1, "cache_read_tokens": 0, "output_tokens": 1}}) + "\n"


def _rotated_pair(tmp_path):
    live = tmp_path / "telemetry.jsonl"
    (tmp_path / "telemetry.jsonl.1").write_text(_req(1.0) * 4)        # older generation: not read by default
    (tmp_path / "telemetry.jsonl.2").write_text(_req(2.0) * 10)       # the newest rotated generation
    live.write_text(_req(3.0) * 3 + json.dumps({"ev": "hb", "ts": 3.0}) + "\n")
    return live


def test_doctor_load_rows_spans_newest_generation(tmp_path):
    from apex_router.proxy_engine.readout.doctor import load_rows
    rows = load_rows(str(_rotated_pair(tmp_path)))
    assert len(rows) == 13 and rows[0]["ts"] == 2.0


def test_retry_ab_and_zeno_span_newest_generation(tmp_path):
    from apex_router import retry_ab, zeno
    live = _rotated_pair(tmp_path)
    assert len(list(retry_ab._rows(live, 0.0))) == 13
    assert len(list(zeno._request_rows(live, 0.0))) == 13


def test_readers_unchanged_without_rotation(tmp_path):
    from apex_router.proxy_engine.readout.doctor import load_rows
    live = tmp_path / "telemetry.jsonl"; live.write_text(_req(3.0) * 5)
    assert len(load_rows(str(live))) == 5


def test_reader_survives_rotation_between_opening_live_and_listing(tmp_path, monkeypatch):
    """Pass-2 race: rotation after the live file is opened but before the rotated files are listed. The old
    live is then BOTH the open live handle and the newest rotated file: it must be read once, and the older
    generation must still be included."""
    import apex_router.telemetry_path as tp
    live = _rotated_pair(tmp_path)
    real = tp.telemetry_files
    w = TelemetryWriter(live); w.ROTATE_BYTES = 1

    def rotate_then_list(path, generations=None):
        w.rotate_if_large(); return real(path, generations)
    monkeypatch.setattr(tp, "telemetry_files", rotate_then_list)
    lines = list(iter_telemetry_lines(live, generations=1))
    assert len(lines) == 14                                              # 10 (.2) + 3 rows + hb (old live), once


def test_reader_survives_rotation_mid_read(tmp_path):
    live = _rotated_pair(tmp_path)
    it = iter_telemetry_lines(live, generations=1)
    next(it)
    w = TelemetryWriter(live); w.ROTATE_BYTES = 1; w.rotate_if_large()
    assert 1 + len(list(it)) == 14
