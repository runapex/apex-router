"""A5: periodic state-store gc from the heartbeat loop. B2: timestamped stderr for the serve process."""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import logging.config
import re
import socket
import threading
import time

from apex_router.proxy_engine import logfmt
from apex_router.proxy_engine.config import Config
from apex_router.proxy_engine.proxy import app as app_mod

TS = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2} "


class _FakeTelemetry:
    KEEP = 3

    def rotate_if_large(self, prune=False):
        pass

    def heartbeat(self):
        pass

    def _gens(self):
        return []


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def _ticks(n: int) -> None:
    for _ in range(n):
        await asyncio.sleep(0.005)


# ---- A5 ------------------------------------------------------------------------------------------

def test_gc_runs_again_after_interval_off_loop_and_survives_errors(capsys):
    clock = _Clock()
    calls: list[bool] = []
    removed: list[int] = []

    def gc():
        calls.append(threading.current_thread() is not threading.main_thread())
        if len(calls) == 2:
            raise RuntimeError("disk I/O error")
        return 7

    async def run():
        task = asyncio.ensure_future(app_mod._heartbeat_loop(
            _FakeTelemetry(), 0.001, gc=gc, gc_interval_s=3600, on_gc=removed.append, clock=clock,
        ))
        await _ticks(10)
        assert calls == []                      # startup gc counts as the first run
        clock.t += 3600
        await _ticks(10)
        assert len(calls) == 1                  # one run after the interval, not one per tick
        clock.t += 3600
        await _ticks(10)
        assert len(calls) == 2                  # second run raised ...
        assert not task.done()                  # ... and the loop is still alive
        clock.t += 3600
        await _ticks(10)
        assert len(calls) == 3                  # the next interval runs gc again
        task.cancel()

    asyncio.run(run())
    assert all(calls)                           # every gc ran in a worker thread
    assert removed == [7, 7]                    # the failed run reports nothing
    err = capsys.readouterr().err
    assert "periodic state-store gc failed" in err and "disk I/O error" in err
    assert all(re.match(TS, ln) for ln in err.splitlines()), err


def test_lifespan_periodic_gc_removes_stale_session(tmp_path, monkeypatch):
    """End to end through create_app: a session that goes stale AFTER startup is removed by the
    periodic gc (interval patched to 0) and /stats gc_removed grows."""
    monkeypatch.setattr(app_mod, "GC_INTERVAL_S", 0.0)
    cfg = Config(home=tmp_path, heartbeat_s=0.005)
    app = app_mod.create_app(cfg)
    state = app.state.apex

    async def run():
        async with app.router.lifespan_context(app):
            assert state["gc_removed"] == 0
            store = state["store"]
            now = time.time()
            store.upsert_epoch("ep1", "{}", "0.0.1", "default", now=now)
            store.create_session("stale", "ep1", "claude", now=now - 20 * 86400)
            store.create_session("fresh", "ep1", "claude", now=now)
            for _ in range(200):
                await asyncio.sleep(0.005)
                if store.get_session("stale") is None:
                    break
            assert store.get_session("stale") is None
            assert store.get_session("fresh") is not None
            assert state["gc_removed"] >= 1

    asyncio.run(run())


# ---- B2 ------------------------------------------------------------------------------------------

def test_eprint_prefixes_every_line_with_iso_timestamp(capsys):
    logfmt.eprint("apex: azure token injection failed,", "x\nsecond line")
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert all(re.match(TS, ln) for ln in lines), lines
    assert lines[0].endswith("apex: azure token injection failed, x")
    assert lines[1].endswith(" second line")


def test_upstream_az_failure_stderr_has_timestamp(capsys, monkeypatch):
    """The real proxy path: the az token mint fails → fail-open message on stderr, stamped."""
    from apex_router.proxy_engine.proxy import az_auth
    from apex_router.proxy_engine.proxy.upstream import Upstream

    def boom(*_a, **_k):
        raise RuntimeError("az not found")

    monkeypatch.setattr(az_auth, "get_provider", boom)
    cfg = Config(inject_azure_token=True, anthropic_upstream="https://gw.example.internal")
    up = Upstream(cfg)
    hdrs = [(b"anthropic-version", b"2023-06-01")]
    try:
        out = asyncio.run(up.inject_auth(hdrs, "claude-code", raw_headers=hdrs))
    finally:
        asyncio.run(up.aclose())
    assert out == hdrs                          # fail-open: forwarded without auth
    err = capsys.readouterr().err
    assert re.match(TS + "apex: azure token injection failed", err), err


def test_uvicorn_log_config_stamps_every_line_incl_traceback():
    buf = io.StringIO()
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    logging.config.dictConfig(logfmt.uvicorn_log_config())
    try:
        logging.getLogger("uvicorn").handlers[0].setStream(buf)
        root.handlers[0].setStream(buf)
        log = logging.getLogger("uvicorn.error")
        log.info("Started server process")
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("Exception in ASGI application")
        logging.getLogger("asyncio").warning("Task was destroyed")  # library log via root
    finally:
        for name in ("uvicorn", "uvicorn.access"):
            logging.getLogger(name).handlers.clear()
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
    lines = buf.getvalue().splitlines()
    assert any("Traceback" in ln for ln in lines)
    assert any("ValueError: boom" in ln for ln in lines)
    assert any("Task was destroyed" in ln for ln in lines)
    assert all(re.match(TS, ln) for ln in lines), buf.getvalue()


def test_serve_stamps_banner_and_passes_log_config(monkeypatch, capsys, tmp_path):
    import uvicorn

    from apex_router.proxy_engine import cli

    monkeypatch.setenv("APEX_HOME", str(tmp_path))   # Config() in _serve → tmp, never ~/.apex

    with socket.socket() as s:                  # a free port; never the real proxy port
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    got: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: got.update(k))
    rc = cli._serve(argparse.Namespace(host="127.0.0.1", port=port, log_level="info"))
    assert rc == 0
    assert got["log_config"] == logfmt.uvicorn_log_config()
    err = capsys.readouterr().err
    assert "serving on" in err
    assert all(re.match(TS, ln) for ln in err.splitlines()), err
