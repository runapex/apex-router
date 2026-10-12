"""Item 5 (2026-10-12):
  a) `watch install-serve` rebuilt the plist ONLY from the installing shell's env, so a reinstall from a shell
     without APEX_CODEX_CACHE_KEY / APEX_RETRY_* silently dropped them from the live service (and those keys were
     not even in the carried list). Fix at the root: carry every proxy env key, and MERGE with what the installed
     unit already has (shell wins on conflict; a key is removed only by editing/uninstalling, never by omission).
  b) `pressure.tail_rows` read only the live file; right after a rotation the 15-min window was empty."""
from __future__ import annotations

import json
import plistlib
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from apex_router import pressure, watch


PROXY_KEYS = ("APEX_CODEX_CACHE_KEY", "APEX_CODEX_CACHE_KEY_SHARDS", "APEX_RETRY_AB", "APEX_RETRY_AB_ARMS",
              "APEX_RETRY_POLICY", "APEX_RETRY_WAIT_MS", "APEX_RETRY_FAST_FAIL_MS", "APEX_TELEMETRY_KEEP",
              "APEX_PRICING_OVERRIDES", "APEX_CONNECT_RETRIES", "APEX_CONNECT_BACKOFF", "APEX_CONNECT_TIMEOUT",
              "APEX_READ_TIMEOUT", "APEX_TTFT_BUDGET_MS", "APEX_HEARTBEAT_S", "APEX_RETENTION_DAYS")


def test_every_proxy_env_key_is_carried():
    missing = [k for k in PROXY_KEYS if k not in watch._SERVE_ENV_KEYS]
    assert not missing, missing


def test_serve_env_carries_cache_key_and_retry_from_shell(monkeypatch):
    for k in [k for k in watch._SERVE_ENV_KEYS]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("APEX_CODEX_CACHE_KEY", "1"); monkeypatch.setenv("APEX_RETRY_AB", "1")
    env = watch._serve_env()
    assert env["APEX_CODEX_CACHE_KEY"] == "1" and env["APEX_RETRY_AB"] == "1"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)
    for k in watch._SERVE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("ANTHROPIC_FOUNDRY_BASE_URL", raising=False)
    return tmp_path


def _ok(cmd, *a, **k):
    return subprocess.CompletedProcess(cmd, 0, "", "")


def _installed_env(home):
    return plistlib.loads((home / "Library/LaunchAgents" / f"{watch.LABEL_SERVE}.plist").read_bytes())["EnvironmentVariables"]


def test_reinstall_from_bare_shell_keeps_installed_keys(home, monkeypatch):
    monkeypatch.setenv("APEX_CODEX_CACHE_KEY", "1"); monkeypatch.setenv("APEX_CODEX_CACHE_KEY_SHARDS", "4")
    monkeypatch.setenv("APEX_ANTHROPIC_UPSTREAM", "https://gw.example/claude")
    with mock.patch.object(watch.subprocess, "run", side_effect=_ok):
        watch._launchd_install_serve()
    for k in ("APEX_CODEX_CACHE_KEY", "APEX_CODEX_CACHE_KEY_SHARDS", "APEX_ANTHROPIC_UPSTREAM"):
        monkeypatch.delenv(k)                                         # a later reinstall from a bare shell
    monkeypatch.setenv("APEX_PORT", "8788")
    with mock.patch.object(watch.subprocess, "run", side_effect=_ok):
        watch._launchd_install_serve()
    env = _installed_env(home)
    assert env["APEX_CODEX_CACHE_KEY"] == "1" and env["APEX_CODEX_CACHE_KEY_SHARDS"] == "4"
    assert env["APEX_ANTHROPIC_UPSTREAM"] == "https://gw.example/claude" and env["APEX_PORT"] == "8788"


def test_shell_value_wins_over_installed_value(home, monkeypatch):
    monkeypatch.setenv("APEX_RETRY_POLICY", "immediate")
    with mock.patch.object(watch.subprocess, "run", side_effect=_ok):
        watch._launchd_install_serve()
    monkeypatch.setenv("APEX_RETRY_POLICY", "wait")
    with mock.patch.object(watch.subprocess, "run", side_effect=_ok):
        watch._launchd_install_serve()
    assert _installed_env(home)["APEX_RETRY_POLICY"] == "wait"


def test_unrelated_or_injected_installed_keys_are_not_carried(home, monkeypatch):
    agents = home / "Library/LaunchAgents"; agents.mkdir(parents=True)
    (agents / f"{watch.LABEL_SERVE}.plist").write_bytes(plistlib.dumps({"Label": watch.LABEL_SERVE, "EnvironmentVariables": {
        "APEX_CODEX_CACHE_KEY": "1", "SOME_RANDOM": "x", "APEX_PORT": "8788\n<key>evil</key>"}}))
    with mock.patch.object(watch.subprocess, "run", side_effect=_ok):
        watch._launchd_install_serve()
    env = _installed_env(home)
    assert env.get("APEX_CODEX_CACHE_KEY") == "1" and "SOME_RANDOM" not in env and "APEX_PORT" not in env


def test_corrupt_installed_plist_is_ignored(home):
    agents = home / "Library/LaunchAgents"; agents.mkdir(parents=True)
    (agents / f"{watch.LABEL_SERVE}.plist").write_text("not a plist")
    with mock.patch.object(watch.subprocess, "run", side_effect=_ok):
        assert watch._launchd_install_serve() == [watch.LABEL_SERVE]


# ---- pressure: the 15-min window must span a rotation ----
def _row(ts):
    return json.dumps({"ts": ts, "schema_version": 12, "is_error": False, "client": "claude-code",
                       "model_resolved": "it-entra-claude-opus-5-5", "endpoint_id": "anthropic"}) + "\n"


def test_tail_rows_continues_into_newest_rotated_file(tmp_path):
    live = tmp_path / "telemetry.jsonl"
    (tmp_path / "telemetry.jsonl.1").write_text("".join(_row(100.0 + i) for i in range(5)))       # older gen
    (tmp_path / "telemetry.jsonl.2").write_text("".join(_row(1000.0 + i) for i in range(20)))     # just rotated
    live.write_text("".join(_row(1020.0 + i) for i in range(3)))
    got = [r["ts"] for r in pressure.tail_rows(live, since_ts=1000.0, slack_s=0)]
    assert len(got) == 23 and min(got) == 1000.0                     # 3 live + 20 from .2; .1 is older than the window


def test_tail_rows_unchanged_without_rotation(tmp_path):
    live = tmp_path / "telemetry.jsonl"; live.write_text("".join(_row(1000.0 + i) for i in range(4)))
    assert len(list(pressure.tail_rows(live, since_ts=1000.0, slack_s=0))) == 4


def test_tail_rows_stops_in_live_when_window_is_covered(tmp_path):
    live = tmp_path / "telemetry.jsonl"
    (tmp_path / "telemetry.jsonl.1").write_text("".join(_row(5000.0) for _ in range(50)))   # would be WRONG to read
    live.write_text("".join(_row(100.0 + i) for i in range(300)) + "".join(_row(1000.0 + i) for i in range(3)))
    got = list(pressure.tail_rows(live, since_ts=1000.0, slack_s=0))
    assert len(got) == 3                                              # window covered inside live: .1 not opened


# ---- cross-validation pass 1 (2026-10-12) ----
def test_explicitly_empty_shell_value_removes_installed_key(home, monkeypatch):
    monkeypatch.setenv("APEX_SHADOW", "1")
    with mock.patch.object(watch.subprocess, "run", side_effect=_ok):
        watch._launchd_install_serve()
    monkeypatch.setenv("APEX_SHADOW", "")                          # operator explicitly disables it
    with mock.patch.object(watch.subprocess, "run", side_effect=_ok):
        watch._launchd_install_serve()
    assert "APEX_SHADOW" not in _installed_env(home)


@pytest.mark.parametrize("bad", [{"EnvironmentVariables": ["bad"]}, {"EnvironmentVariables": "x"},
                                 {"Label": "com.other", "EnvironmentVariables": {"APEX_CODEX_CACHE_KEY": "1"}},
                                 {"Label": watch.LABEL_SERVE, "EnvironmentVariables": {"APEX_CODEX_CACHE_KEY": True}}])
def test_malformed_or_foreign_installed_env_is_ignored(home, bad):
    agents = home / "Library/LaunchAgents"; agents.mkdir(parents=True)
    (agents / f"{watch.LABEL_SERVE}.plist").write_bytes(plistlib.dumps(bad))
    with mock.patch.object(watch.subprocess, "run", side_effect=_ok):
        assert watch._launchd_install_serve() == [watch.LABEL_SERVE]
    assert "APEX_CODEX_CACHE_KEY" not in _installed_env(home)


def test_systemd_unit_merges_installed_keys(home, monkeypatch):
    d = home / ".config/systemd/user"; d.mkdir(parents=True)
    (d / "apex-router-serve.service").write_text("[Service]\nEnvironment=APEX_CODEX_CACHE_KEY=1\nEnvironment=SOME_RANDOM=x\nExecStart=x\n")
    monkeypatch.setenv("APEX_PORT", "8788")
    body = watch._systemd_serve_unit()["apex-router-serve.service"]
    assert "Environment=APEX_CODEX_CACHE_KEY=1" in body and "Environment=APEX_PORT=8788" in body and "SOME_RANDOM" not in body


def test_tail_rows_never_double_counts_when_rotation_races_the_read(tmp_path, monkeypatch):
    """Rotation renames the open live file to the newest generation before the fallback lists generations: the
    same inode must not be read twice (pass-1 reproduced [1000.0, 1000.0])."""
    import apex_router.telemetry_path as tp
    live = tmp_path / "telemetry.jsonl"; live.write_text(_row(1000.0))
    real = tp.telemetry_files

    def rotate_then_list(path, generations=None):
        if live.exists():
            live.replace(tmp_path / "telemetry.jsonl.1"); live.write_text("")
        return real(path, generations)
    monkeypatch.setattr(tp, "telemetry_files", rotate_then_list)
    got = [r["ts"] for r in pressure.tail_rows(live, since_ts=0.0, slack_s=0)]
    assert got == [1000.0]


# ---- cross-validation pass 2 (2026-10-12) ----
def test_explicit_empty_upstream_is_not_rederived_from_foundry(monkeypatch):
    for k in watch._SERVE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_BASE_URL", "https://gw.example/claude")
    assert watch._serve_env().get("APEX_ANTHROPIC_UPSTREAM") == "https://gw.example/claude"   # absent: derived
    monkeypatch.setenv("APEX_ANTHROPIC_UPSTREAM", "")
    assert "APEX_ANTHROPIC_UPSTREAM" not in watch._merged_serve_env({"APEX_ANTHROPIC_UPSTREAM": "https://old"})


def test_systemd_quoted_assignments_are_kept(home):
    d = home / ".config/systemd/user"; d.mkdir(parents=True)
    (d / "apex-router-serve.service").write_text('[Service]\nEnvironment="APEX_CODEX_CACHE_KEY=1"\nEnvironment=APEX_POLICY_PATH=/a=b\n')
    assert watch._installed_systemd_env(d / "apex-router-serve.service") == {"APEX_CODEX_CACHE_KEY": "1", "APEX_POLICY_PATH": "/a=b"}
