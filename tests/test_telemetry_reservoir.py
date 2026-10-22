"""Milestone 4: rotation folds a generation into the stratified reservoir before it deletes it."""
from __future__ import annotations

import json
import os
import stat

from apex_router import telemetry_reservoir as T
from apex_router.core import reservoir as R
from apex_router.proxy_engine.telemetry.events import TelemetryWriter

SECRET_SESSION = "sess-7f3a9c-SECRET"


def _req(i, cause=None, model="it-entra-claude-opus-5-5", client="claude-code"):
    return {"schema_version": 11, "ts": 1000.0 + i, "request_id": f"req-{i}",
            "session_id": SECRET_SESSION, "agent_id": "agent-x", "epoch_id": "ep",
            "client": client, "model_requested": model, "model_resolved": model, "stratum": "l",
            "tokens_in": 100 + i, "tokens_out": 5, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "ttft_ms": 12.5, "is_error": cause is not None,
            "error_cause": cause, "upstream_rejected": False,
            "error_detail": {"body": "secret provider text"} if cause else None,
            "usage": {"x": 1}, "shadow": {"context_bytes": 1}, "transforms": [],
            "stop_reason": "tool_use"}


def _gen_file(path, rows, heartbeats=3, junk=1):
    lines = [json.dumps(r) for r in rows]
    lines += [json.dumps({"schema_version": 11, "ts": 1.0, "ev": "heartbeat", "requests": 3,
                          "errors": 0})] * heartbeats
    lines += ["{not json"] * junk
    path.write_text("\n".join(lines) + "\n")


def test_row_sample_keeps_only_flat_fields_and_strata_by_cause():
    key, row = T.row_sample(_req(1, cause="ConnectError"))
    assert key == "ConnectError|it-entra-claude-opus-5-5|claude-code"
    for gone in ("session_id", "request_id", "agent_id", "epoch_id", "error_detail", "usage",
                 "shadow", "transforms"):
        assert gone not in row
    assert row["tokens_in"] == 101 and row["is_error"] is True and row["stop_reason"] == "tool_use"
    assert T.row_sample(_req(2))[0] == "ok|it-entra-claude-opus-5-5|claude-code"
    assert T.row_sample({"ev": "heartbeat", "ts": 1.0}) is None
    odd = _req(3)
    odd["model_resolved"] = "vendor/model..x y"                  # not a model name: "other"
    assert T.row_sample(odd)[1]["model_resolved"] == "other"


def test_fold_generation_counts_once_and_never_stores_identifiers(tmp_path):
    tel = tmp_path / "telemetry.jsonl"
    gen = tmp_path / "telemetry.jsonl.1"
    rows = [_req(i) for i in range(500)] + [_req(1000 + i, cause="http_429") for i in range(3)]
    _gen_file(gen, rows)
    store = T.store_path(tel)
    assert T.fold_generation(gen) is True
    assert T.fold_generation(gen) is True                       # second call: already counted
    doc, res = T.load(store)
    assert len(doc["folded"]) == 1
    assert doc["folded"][0] | {"at": 0} == {"gen": T.gen_id(gen), "name": "telemetry.jsonl.1",
                                            "at": 0, "rows": 503, "skipped": 3, "bad": 1}
    assert doc["folded"][0]["gen"].startswith("sha256:")
    assert doc["by_cause"] == {"ok": 500, "http_429": 3}
    pop = res.population()
    assert pop["ok|it-entra-claude-opus-5-5|claude-code"] == 500
    assert pop["http_429|it-entra-claude-opus-5-5|claude-code"] == 3
    assert len(res.strata["http_429|it-entra-claude-opus-5-5|claude-code"]["rows"]) == 3   # all kept
    assert len(res.strata["ok|it-entra-claude-opus-5-5|claude-code"]["rows"]) == T.CAP
    text = store.read_text()
    assert SECRET_SESSION not in text and "secret provider text" not in text and "req-1" not in text
    assert stat.S_IMODE(store.stat().st_mode) == 0o600


def test_rotation_folds_before_it_deletes(tmp_path):
    p = tmp_path / "t.jsonl"
    w = TelemetryWriter(p)
    w.ROTATE_BYTES = 100
    w.KEEP = 2
    for k, cause in enumerate(["ReadTimeout", None, None, None]):
        _gen_file(p, [_req(10 * k + i, cause=cause) for i in range(5)])
        w.rotate_if_large()
    gens = sorted(x.name for x in tmp_path.glob("t.jsonl.*") if x.name.rsplit(".", 1)[1].isdigit())
    assert gens == ["t.jsonl.3", "t.jsonl.4"]                   # 1 and 2 pruned
    doc, res = T.load(tmp_path / T.STORE_NAME)
    assert [f["name"] for f in doc["folded"]] == ["t.jsonl.1", "t.jsonl.2"]
    pop = res.population()
    assert pop["ReadTimeout|it-entra-claude-opus-5-5|claude-code"] == 5   # rare cause survives
    assert pop["ok|it-entra-claude-opus-5-5|claude-code"] == 5


def test_unreadable_store_keeps_the_generation(tmp_path):
    p = tmp_path / "t.jsonl"
    w = TelemetryWriter(p)
    w.ROTATE_BYTES = 100
    w.KEEP = 2                                                  # 3 files <= 2 x KEEP: guard idle
    (tmp_path / T.STORE_NAME).write_text("{broken")
    for k in range(3):
        _gen_file(p, [_req(10 * k + i) for i in range(5)])
        w.rotate_if_large()
    gens = sorted(x.name for x in tmp_path.glob("t.jsonl.*") if x.name.rsplit(".", 1)[1].isdigit())
    assert gens == ["t.jsonl.1", "t.jsonl.2", "t.jsonl.3"]     # nothing deleted uncounted
    assert (tmp_path / T.STORE_NAME).read_text() == "{broken"  # bad store never overwritten


def test_disk_guard_deletes_beyond_twice_keep_when_the_fold_keeps_failing(tmp_path, monkeypatch):
    p = tmp_path / "t.jsonl"
    w = TelemetryWriter(p)
    w.ROTATE_BYTES = 100
    w.KEEP = 2
    monkeypatch.setattr(T, "fold_generation", lambda gen, store=None: False)
    for k in range(8):
        _gen_file(p, [_req(k)])
        w.rotate_if_large()
    gens = sorted(int(x.name.rsplit(".", 1)[1]) for x in tmp_path.glob("t.jsonl.*")
                  if x.name.rsplit(".", 1)[1].isdigit())
    assert len(gens) == 2 * w.KEEP and gens[-1] == 8           # bounded, newest kept


def test_backfill_then_prune_never_double_counts(tmp_path, monkeypatch, capsys):
    tel = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("APEX_TELEMETRY", str(tel))
    for n in (1, 2):
        _gen_file(tmp_path / f"telemetry.jsonl.{n}", [_req(100 * n + i) for i in range(7)])
    _gen_file(tel, [_req(999)])
    assert T.main(["fold", "--backfill"]) == 0
    assert "2/2 generation(s) counted" in capsys.readouterr().out
    assert T.fold_generation(tmp_path / "telemetry.jsonl.1") is True   # prune time: skipped
    _, res = T.load(T.store_path(tel))
    assert sum(res.population().values()) == 14                # live file not folded
    assert T.main(["report", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["generations"] == 2 and rep["rows"] == 14 and rep["by_cause"] == {"ok": 14}
    assert T.main(["report"]) == 0
    assert "14 rows" in capsys.readouterr().out


def test_weighted_estimate_from_the_store_matches_exact_counts(tmp_path):
    gen = tmp_path / "telemetry.jsonl.1"
    rows = ([_req(i) for i in range(2000)] + [_req(5000 + i, cause="ConnectError")
                                               for i in range(40)])
    _gen_file(gen, rows, heartbeats=0, junk=0)
    assert T.fold_generation(gen)
    _, res = T.load(tmp_path / T.STORE_NAME)
    est = R.estimate(res.snapshot(), lambda r: r["is_error"])
    assert abs(est["mean"] - 40 / 2040) < 1e-12               # exact: constant within each stratum
    unweighted = (sum(r["is_error"] for _, _, r in res.snapshot()["rows"])
                  / len(res.snapshot()["rows"]))
    assert unweighted > 0.25                                  # 40 of 140 kept rows: the distortion


def test_concurrent_folds_do_not_lose_updates(tmp_path):
    import threading
    gens = []
    for n in range(1, 7):
        g = tmp_path / f"telemetry.jsonl.{n}"
        _gen_file(g, [_req(100 * n + i) for i in range(30)], heartbeats=0, junk=0)
        gens.append(g)
    ts = [threading.Thread(target=T.fold_generation, args=(g,)) for g in gens]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    doc, res = T.load(tmp_path / T.STORE_NAME)
    assert len(doc["folded"]) == 6 and sum(res.population().values()) == 180
    assert not [x for x in os.listdir(tmp_path) if ".tmp" in x]


# ---- xval pass 1 regressions --------------------------------------------------------------------

def test_free_text_in_enum_fields_never_reaches_the_store(tmp_path):
    # F2: a model name from a request body, or any other field, cannot carry free text
    bad = _req(1, cause="alice@example.com")
    bad.update(model_requested="customer-secret-123", model_resolved="alice@example.com",
               client="Bob Smith", stop_reason="see notes of user bob", stratum="secret",
               endpoint_id=["x"], matcher_event={"a": 1})
    key, row = T.row_sample(bad)
    assert key == "other|other|other"
    assert {row[f] for f in ("model_requested", "model_resolved", "client", "stop_reason",
                             "stratum", "endpoint_id", "matcher_event", "error_cause")} == {"other"}
    gen = tmp_path / "telemetry.jsonl.1"
    _gen_file(gen, [bad], heartbeats=0, junk=0)
    assert T.fold_generation(gen)
    text = (tmp_path / T.STORE_NAME).read_text()
    for secret in ("alice", "customer-secret", "Bob", "notes of user"):
        assert secret not in text
    # real values pass
    ok = T.row_sample(_req(2, cause="midstream_ReadError", model="it-entra-gpt-5.6-luna",
                           client="codex"))
    assert ok[0] == "midstream_ReadError|it-entra-gpt-5.6-luna|codex"
    assert T.row_sample(_req(3, cause="http_429", model="claude-opus-5-5"))[0].startswith("http_429|claude")


def test_many_models_never_merge_error_causes(tmp_path, monkeypatch):
    # F3: beyond FINE_STRATA, a cause keeps its own cause-only stratum and its exact count
    monkeypatch.setattr(T, "FINE_STRATA", 4)
    gen = tmp_path / "telemetry.jsonl.1"
    models = sorted(T.KNOWN_MODELS)
    rows = ([_req(i, model=models[i % len(models)]) for i in range(300)]
            + [_req(999, cause="ConnectError", model=models[-1])])
    _gen_file(gen, rows, heartbeats=0, junk=0)
    assert T.fold_generation(gen)
    doc, res = T.load(tmp_path / T.STORE_NAME)
    assert doc["by_cause"] == {"ok": 300, "ConnectError": 1}
    assert len(res.strata) > 4 and res.population()["ConnectError|other|other"] == 1
    rep = T.report(tmp_path / T.STORE_NAME)
    assert rep["by_cause"] == {"ok": 300, "ConnectError": 1} and abs(rep["error_share"] - 1 / 301) < 1e-12


def test_ledger_is_never_cut_and_identity_follows_content(tmp_path):
    # F4 + F5: a renamed copy is not counted again; new content under an old name is counted
    store = tmp_path / T.STORE_NAME
    for n in range(1, 1206):                                    # more than the old 1000 cap
        doc_path = tmp_path / f"g{n}.jsonl.1"
        _gen_file(doc_path, [_req(n)], heartbeats=0, junk=0)
        assert T.fold_generation(doc_path, store)
        doc_path.unlink()
    first = tmp_path / "again.jsonl.7"
    _gen_file(first, [_req(1)], heartbeats=0, junk=0)          # same bytes as g1, other name
    assert T.fold_generation(first, store)
    doc, res = T.load(store)
    assert len(doc["folded"]) == 1205 and sum(res.population().values()) == 1205
    _gen_file(first, [_req(77777)], heartbeats=0, junk=0)      # same name, new content
    assert T.fold_generation(first, store)
    assert sum(T.load(store)[1].population().values()) == 1206


def test_locked_store_keeps_the_generation_and_never_blocks_forever(tmp_path, monkeypatch):
    # F1: a held lock defers the fold (file kept) after a bounded wait
    import fcntl
    monkeypatch.setattr(T, "LOCK_WAIT_S", 0.2)
    gen = tmp_path / "telemetry.jsonl.1"
    _gen_file(gen, [_req(1)])
    lock = tmp_path / (T.STORE_NAME + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        import time as _t
        t0 = _t.monotonic()
        assert T.fold_generation(gen) == T.DEFERRED
        assert _t.monotonic() - t0 < 2.0
    finally:
        os.close(fd)
    assert T.fold_generation(gen) is True


def test_proxy_tick_prunes_off_the_event_loop(tmp_path, monkeypatch):
    # F1: the heartbeat tick renames on the loop and runs prune() through asyncio.to_thread
    import asyncio
    import threading

    from apex_router.proxy_engine.proxy import app as app_mod
    p = tmp_path / "t.jsonl"
    w = TelemetryWriter(p)
    w.ROTATE_BYTES = 100
    w.KEEP = 1
    for k in range(3):                                         # 3 gens on disk, no prune yet
        _gen_file(p, [_req(k)])
        w.rotate_if_large(prune=False)
    _gen_file(p, [_req(9)])
    seen = {}
    real = w.prune

    def spy():
        seen["thread"] = threading.current_thread() is not threading.main_thread()
        real()

    w.prune = spy

    async def run():
        task = asyncio.ensure_future(app_mod._heartbeat_loop(w, 0.01))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if "thread" in seen:
                break
        task.cancel()

    asyncio.run(run())
    assert seen == {"thread": True}
    gens = [x for x in tmp_path.glob("t.jsonl.*") if x.name.rsplit(".", 1)[1].isdigit()]
    assert len(gens) == 1
    assert sum(T.load(tmp_path / T.STORE_NAME)[1].population().values()) == 3


# ---- xval pass 2 regressions --------------------------------------------------------------------

def test_model_and_word_fields_are_closed_vocabularies():
    for bad in ("gpt-patient-alice-hiv-positive", "claude-customer-secret", "alice-smith",
                "private-diagnosis", "gpt-5-alice", "claude-opus-5-5-ssn123x",
                "gpt-123-45-6789", "claude-4111-1111-1111-1111", "gpt-6.1-sol-1"):
        assert T._enum("model_resolved", bad) == "other", bad
    for good in ("it-entra-claude-opus-5-5", "it-entra-gpt-5.6-luna", "it-entra-gpt-6-astra",
                 "it-entra-gpt-6.1-sol", "claude-haiku-4-5", "gpt-5.6-terra",
                 "it-entra-claude-sonnet-5-5", "it-entra-gpt-6-nova", "kimi-k2.6",
                 "claude-fable-5-1"):
        assert T._enum("model_resolved", good) == good, good
    assert all(T._MODEL_ID.match(m) for m in T.KNOWN_MODELS)    # every id is a valid key part
    assert T._registry_models() and "hf.co/x/y:q4" not in T.KNOWN_MODELS
    assert T._enum("stop_reason", "private-diagnosis") == "other"
    assert T._enum("client", "alice") == "other" and T._enum("client", "codex") == "codex"
    assert T._enum("matcher_event", "client_edit") == "client_edit"
    assert T._enum("retry_arm", "wait") == "wait"


def test_busy_lock_keeps_files_up_to_the_cap_and_disk_stays_bounded(tmp_path, monkeypatch, capsys):
    # reconciled P2: one rule for any uncounted generation (failed or deferred)
    import fcntl
    monkeypatch.setattr(T, "LOCK_WAIT_S", 0.02)
    p = tmp_path / "t.jsonl"
    w = TelemetryWriter(p)
    w.ROTATE_BYTES = 100
    w.KEEP = 2

    def gens():
        return sorted(int(x.name.rsplit(".", 1)[1]) for x in tmp_path.glob("t.jsonl.*")
                      if x.name.rsplit(".", 1)[1].isdigit())

    lock = tmp_path / (T.STORE_NAME + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)                               # a stalled lock holder
    try:
        for k in range(4):                                       # up to 2 x KEEP: all kept
            _gen_file(p, [_req(k)])
            w.rotate_if_large()
        assert gens() == [1, 2, 3, 4]
        for k in range(4, 30):                                   # stalled for long: bounded
            _gen_file(p, [_req(k)])
            w.rotate_if_large()
        assert len(gens()) == 2 * w.KEEP and gens()[-1] == 30
        assert "deleted without a reservoir fold (deferred" in capsys.readouterr().err
    finally:
        os.close(fd)
    _gen_file(p, [_req(99)])
    w.rotate_if_large()                                          # lock free: normal again
    assert gens() == [30, 31]                                   # 27..29 folded, then deleted
    assert sum(T.load(tmp_path / T.STORE_NAME)[1].population().values()) == 3


def test_two_prunes_at_once_are_safe(tmp_path):
    # correctness comes from the store lock + count-once + missing_ok; the prune lock only saves work
    import threading
    p = tmp_path / "t.jsonl"
    w = TelemetryWriter(p)
    w.ROTATE_BYTES = 100
    w.KEEP = 1
    for k in range(6):
        _gen_file(p, [_req(k)])
        w.rotate_if_large(prune=False)
    errs = []

    def run():
        try:
            w.prune()
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=run) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    w.prune()
    gens = [x for x in tmp_path.glob("t.jsonl.*") if x.name.rsplit(".", 1)[1].isdigit()]
    assert errs == [] and len(gens) == 1
    assert sum(T.load(tmp_path / T.STORE_NAME)[1].population().values()) == 5


def test_one_unkeyable_row_is_bad_and_the_rest_still_count(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "KNOWN_MODELS", T.KNOWN_MODELS | {"x/y:z"})   # cannot be a key part
    gen = tmp_path / "telemetry.jsonl.1"
    _gen_file(gen, [_req(1), _req(2, model="x/y:z"), _req(3)], heartbeats=0, junk=0)
    assert T.fold_generation(gen) is True
    doc, res = T.load(tmp_path / T.STORE_NAME)
    assert doc["folded"][0]["bad"] == 1 and sum(res.population().values()) == 2
