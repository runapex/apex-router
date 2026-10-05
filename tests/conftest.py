"""Suite-wide isolation of the live install, plus a guard that proves it held.

Tests must never read-modify-write the real state dirs (~/.apex-router, ~/.apex, ~/.claude).
Two layers:

1. Isolation: every env var the code honours for state locations, and HOME itself, is pointed at
   a throwaway dir. This happens at conftest import time (before any test module is imported) so
   module-level constants that capture ``Path.home()`` / ``os.environ`` at import also resolve
   to the sandbox, and again per test (autouse fixture) so a test that mutates them is reset.
2. Guard: the REAL dirs are resolved before any override; at session end the run FAILS if a
   non-daemon file in them is newer than session start.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

# ---- real paths + session start: computed BEFORE any env override -------------------------
_REAL_HOME = Path(os.path.expanduser("~"))
_SESSION_START = time.time()

# Files that live daemons / the host CLI legitimately write while a suite runs (the proxy, the
# route logger, Claude Code itself). A blanket mtime check would be flaky against them, so the
# guard ignores exactly these and watches everything else.
_APEX_IGNORE_NAMES = {
    "route_log.jsonl", "telemetry.jsonl", "state.db", "state.db-wal", "state.db-shm",
}
_IGNORE_DIRS = {".git", ".venv", ".pytest_cache", "__pycache__", "logs"}
# ~/.claude is owned by Claude Code (transcripts, history, sessions churn constantly). Guard only
# the configuration surface that tests could plausibly clobber.
_CLAUDE_GUARDED_FILES = {"settings.json", "settings.local.json", "CLAUDE.md", "keybindings.json"}


# ---- isolation ----------------------------------------------------------------------------
# Env vars that redirect state locations. Path overrides are dropped (so they fall back to the
# sandboxed HOME); the roots are pointed at the sandbox.
_PATH_OVERRIDES = (
    "APEX_TELEMETRY", "APEX_ROUTER_LOG", "APEX_LABELED_TABLE", "APEX_ORNITH_TIER_FILE",
    "APEX_ROUTE_TABLE", "APEX_MODEL_REGISTRY", "APEX_LOCAL_POINTER", "APEX_LEARN_CHAIN_LOG",
    "APEX_CHAIN_PAYLOADS", "APEX_RAG_CYCLE_LOG", "APEX_POLICY_PATH", "APEX_CONFORMANCE_LOG",
    "APEX_JUDGE_PROBE_BASELINE", "ORNITH_ROOT", "CODEQA_REPOS", "CODEQA_DIR", "APEX_ORNITH_QUEUE",
)
_SANDBOX = Path(tempfile.mkdtemp(prefix="apex-test-home-"))


def _apply_sandbox(home: Path) -> dict:
    """Point HOME & friends at ``home``; returns {var: value|None} to restore."""
    new = {
        "HOME": str(home), "USERPROFILE": str(home), "APEX_HOME": str(home / ".apex"),
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"), "XDG_STATE_HOME": str(home / ".local" / "state"),
    }
    prev = {k: os.environ.get(k) for k in (*new, *_PATH_OVERRIDES)}
    os.environ.update(new)
    for k in _PATH_OVERRIDES:
        os.environ.pop(k, None)
    return prev


def _restore(prev: dict) -> None:
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# At import time, before any test module (and its module-level path constants) is imported.
_apply_sandbox(_SANDBOX)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path):
    """Fresh sandbox HOME per test, so no test sees (or leaks) another's state."""
    prev = _apply_sandbox(tmp_path)
    try:
        yield
    finally:
        _restore(prev)


def _guard_candidates():
    for sub in (".apex-router", ".apex"):
        root = _REAL_HOME / sub
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
            for fn in filenames:
                if fn in _APEX_IGNORE_NAMES:
                    continue
                yield Path(dirpath) / fn
    claude = _REAL_HOME / ".claude"
    if claude.is_dir():
        for fn in _CLAUDE_GUARDED_FILES:
            if (claude / fn).is_file():
                yield claude / fn
        plugins = claude / "plugins"
        if plugins.is_dir():
            for dirpath, dirnames, filenames in os.walk(plugins):
                dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
                for fn in filenames:
                    yield Path(dirpath) / fn


def _touched_since_start():
    bad = []
    for p in _guard_candidates():
        try:
            if p.stat().st_mtime > _SESSION_START:
                bad.append(str(p))
        except OSError:
            pass
    return sorted(bad)


def pytest_sessionfinish(session, exitstatus):
    bad = _touched_since_start()
    if not bad:
        return
    session.config._isolation_violations = bad
    if session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    bad = getattr(config, "_isolation_violations", None)
    if bad:
        terminalreporter.section("ISOLATION GUARD FAILED", red=True)
        terminalreporter.write_line(
            "tests modified the live install (mtime newer than session start):")
        for p in bad:
            terminalreporter.write_line(f"  {p}")
