"""Suite-wide isolation of the live install, plus a guard that proves it held.

Tests must never read-modify-write the real state dirs (~/.apex-router, ~/.apex, ~/.claude).
Two layers:

1. Isolation: every env var the code honours for state locations, and HOME itself, is pointed at
   a throwaway dir. This happens at conftest import time (before any test module is imported) so
   module-level constants that capture ``Path.home()`` / ``os.environ`` at import also resolve
   to the sandbox, and again per test (autouse fixture) so a test that mutates them is reset.
   NOTE: import-time constants bind to the SESSION sandbox (shared across tests), not the
   per-test one; tests needing per-test state must override explicitly. A test that spawns a
   subprocess with an explicit ``env=`` (e.g. tests/test_handoff_state.py's CLI runner passes no
   HOME) bypasses this layer; such a subprocess falls back to the real home -- only safe if the
   command never writes state.
2. Guard: the REAL dirs are resolved before any override (and exported in ``_APEX_REAL_HOME`` so
   xdist workers, which import this module after the parent overrode HOME, agree). At session end
   the run FAILS if:
   - any file under ~/.apex-router or ~/.apex (except the exact live-writer paths below) is newer
     than session start;
   - an exact append-only live file (route_log.jsonl, telemetry.jsonl, state.db*) shrank or was
     replaced (inode changed);
   - a watched ~/.claude config file changed content (sha256 snapshot, not mtime). The watch
     list is deliberately narrow: settings.json, settings.local.json, CLAUDE.md,
     keybindings.json, plugins/installed_plugins.json, plugins/known_marketplaces.json and
     marketplace/plugin manifests (.claude-plugin/{marketplace,plugin}.json). These are what a
     test could plausibly clobber and Claude Code does not rewrite on its own. NOT watched:
     plugins/**/.in_use/<pid>, plugins/.last_inuse_sweep, plugin data/store dirs, caches,
     projects/ transcripts, todos, statsig, shell-snapshots -- Claude Code churns these
     continuously while any session is open, so hashing them made the guard fail (rc=1 with all
     tests passing) whenever the developer had Claude open.
   If a live daemon (daily agent / ornith overnight) ran during the session, rewrites of the
   files it legitimately regenerates are downgraded to a printed WARNING.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

# ---- real paths + session start: computed BEFORE any env override -------------------------
_REAL_HOME = Path(os.environ.get("_APEX_REAL_HOME") or os.path.expanduser("~"))
os.environ.setdefault("_APEX_REAL_HOME", str(_REAL_HOME))
_SESSION_START = float(os.environ.setdefault("_APEX_SESSION_START", str(time.time())))

_AR, _AX, _CL = _REAL_HOME / ".apex-router", _REAL_HOME / ".apex", _REAL_HOME / ".claude"
# Exact paths the live proxy / route logger append to while a suite runs.
_APPEND_ONLY = [_AR / "route_log.jsonl", _AX / "telemetry.jsonl",
                _AX / "state.db", _AX / "state.db-wal", _AX / "state.db-shm"]
# Root-level dirs the live system writes constantly (widget/: the SwiftBar plugin appends a
# history sample and rewrites detail pages every minute while the menu bar is up; transcripts/:
# the com.apex-router.snapshot agent mirrors agent transcripts there daily).
_IGNORE_ROOT_DIRS = {_AR / "observe", _AR / "queue", _AR / "logs", _AR / "widget",
                     _AR / "transcripts"}
_IGNORE_ANY_DIRS = {".git", ".venv", ".pytest_cache", "__pycache__"}
# Files the daily agent / ornith overnight legitimately regenerate.
_DAEMON_REGENERATED = {_AR / n for n in (
    "labeled_table.jsonl", "handoff_threshold.json", "offload_daily.md", "memory_index.db")}
_DAEMON_LOGS = [_AR / "logs" / n for n in (
    "com.apex-router.daily.log", "com.apex-router.daily.err",
    "com.ornith.overnight.out", "com.ornith.overnight.err")]
_CLAUDE_GUARDED_FILES = ("settings.json", "settings.local.json", "CLAUDE.md", "keybindings.json")


def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames
                       if d not in _IGNORE_ANY_DIRS and (here / d) not in _IGNORE_ROOT_DIRS]
        for fn in filenames:
            yield here / fn


_CLAUDE_PLUGIN_FILES = ("plugins/installed_plugins.json", "plugins/known_marketplaces.json")
_CLAUDE_MANIFEST_GLOBS = (
    "plugins/marketplaces/*/.claude-plugin/marketplace.json",
    "plugins/marketplaces/*/plugins/*/.claude-plugin/plugin.json",
    "plugins/cache/*/*/*/.claude-plugin/plugin.json",
)


def _claude_watch_files(claude_dir: Path):
    """Config files under ``claude_dir`` a test could clobber and Claude Code doesn't churn."""
    names = [*_CLAUDE_GUARDED_FILES, *_CLAUDE_PLUGIN_FILES]
    found = [claude_dir / n for n in names if (claude_dir / n).is_file()]
    for pat in _CLAUDE_MANIFEST_GLOBS:
        found.extend(p for p in sorted(claude_dir.glob(pat)) if p.is_file())
    return found


def _claude_files():
    yield from _claude_watch_files(_CL)


def _sha(p: Path):
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _claude_changed(snapshot: dict, claude_dir: Path):
    """Watched files under ``claude_dir`` whose content differs from ``snapshot``."""
    return sorted(p for p in set(snapshot) | set(_claude_watch_files(claude_dir))
                  if snapshot.get(p) != _sha(p))


def _stat_sig(p: Path):
    try:
        st = p.stat()
        return (st.st_ino, st.st_size)
    except OSError:
        return None


_CLAUDE_SNAPSHOT = {p: _sha(p) for p in _claude_files()}
_APPEND_SNAPSHOT = {p: _stat_sig(p) for p in _APPEND_ONLY}


def _daemon_active() -> bool:
    for p in _DAEMON_LOGS:
        try:
            if p.stat().st_mtime > _SESSION_START:
                return True
        except OSError:
            pass
    try:
        r = subprocess.run(["pgrep", "-f", "apex_router.watch --run-daily"],
                           capture_output=True, timeout=5)
        return r.returncode == 0 and bool(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _violations():
    """Return (failures, warnings)."""
    fails, warns = [], []
    daemon = None
    ignored = set(_APPEND_ONLY)
    for root in (_AR, _AX):
        if not root.is_dir():
            continue
        for p in _walk(root):
            if p in ignored:
                continue
            try:
                if p.stat().st_mtime <= _SESSION_START:
                    continue
            except OSError:
                continue
            if p in _DAEMON_REGENERATED:
                if daemon is None:
                    daemon = _daemon_active()
                if daemon:
                    warns.append(f"{p} (rewritten; a live daily/overnight job ran this session)")
                    continue
            fails.append(f"{p} (mtime newer than session start)")
    for p, before in _APPEND_SNAPSHOT.items():
        now = _stat_sig(p)
        if before is None or now is None:
            continue
        if now[0] != before[0]:
            fails.append(f"{p} (replaced: inode changed)")
        elif now[1] < before[1]:
            fails.append(f"{p} (shrank {before[1]} -> {now[1]} bytes)")
    for p in _claude_changed(_CLAUDE_SNAPSHOT, _CL):
        fails.append(f"{p} (content changed)")
    return sorted(fails), sorted(warns)


def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput"):   # xdist worker: the controller reports
        return
    fails, warns = _violations()
    session.config._isolation_warnings = warns
    if fails:
        session.config._isolation_violations = fails
        if session.exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    bad = getattr(config, "_isolation_violations", None)
    if bad:
        terminalreporter.section("ISOLATION GUARD FAILED", red=True)
        terminalreporter.write_line("tests modified the live install:")
        for p in bad:
            terminalreporter.write_line(f"  {p}")
    for w in getattr(config, "_isolation_warnings", None) or []:
        terminalreporter.write_line(f"WARNING isolation guard: {w}", yellow=True)


# ---- isolation ----------------------------------------------------------------------------
# Env vars that redirect state locations. Path overrides are dropped (so they fall back to the
# sandboxed HOME); the roots are pointed at the sandbox.
_PATH_OVERRIDES = (
    "APEX_TELEMETRY", "APEX_ROUTER_LOG", "APEX_LABELED_TABLE", "APEX_ORNITH_TIER_FILE",
    "APEX_ROUTE_TABLE", "APEX_MODEL_REGISTRY", "APEX_LOCAL_POINTER", "APEX_LEARN_CHAIN_LOG",
    "APEX_CHAIN_PAYLOADS", "APEX_RAG_CYCLE_LOG", "APEX_POLICY_PATH", "APEX_CONFORMANCE_LOG",
    "APEX_JUDGE_PROBE_BASELINE", "ORNITH_ROOT", "CODEQA_REPOS", "CODEQA_DIR", "APEX_ORNITH_QUEUE", "APEX_HANDOFF_THRESHOLD_FILE",
    "APEX_ROUTER_DIR", "ORNITH_HEALTH_PATH", "APEX_PROXY_CONFIG", "APEX_ROUTE_LOG_PYTHON",
    "APEX_ROUTER_PY", "APEX_GROUND_PYTHON", "CLAUDE_CONFIG_DIR", "BOOKS_DIR", "BOOKSEARCH_DB",
    "CLASSIFIER_PANEL",
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
