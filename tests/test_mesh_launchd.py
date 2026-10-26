"""`apex-router mesh launchd` (src/apex_router/mesh/launchd.py).

Plists go to tmp_path; launchctl and ps are fakes. No test calls the real launchctl or touches
~/Library/LaunchAgents.
"""
from __future__ import annotations

import io
import plistlib
import subprocess

import pytest

from apex_router.mesh import launchd


@pytest.fixture(autouse=True)
def no_real_launchctl(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("real launchctl/ps called in a test")
    monkeypatch.setattr(launchd, "_launchctl", boom)
    monkeypatch.setattr(launchd, "_ps", boom)
    monkeypatch.setattr(launchd, "agents_dir", lambda: (_ for _ in ()).throw(AssertionError()))


class FakeLaunchctl:
    def __init__(self, loaded=(), fail=()):
        self.calls: list[list[str]] = []
        self.loaded = dict.fromkeys(loaded, 4242)
        self.fail = set(fail)

    def __call__(self, argv):
        self.calls.append(list(argv))
        verb = argv[1]
        if verb == "print":
            label = argv[2].rsplit("/", 1)[1]
            if label in self.loaded:
                return subprocess.CompletedProcess(argv, 0, f"{label} = {{\n\tpid = "
                                                   f"{self.loaded[label]}\n\tlast exit code = 0\n}}\n", "")
            return subprocess.CompletedProcess(argv, 113, "", "Could not find service")
        if verb == "bootstrap":
            label = argv[3].rsplit("/", 1)[1].removesuffix(".plist")
            if label in self.fail:
                return subprocess.CompletedProcess(argv, 5, "", "Bootstrap failed: 5: I/O error")
            self.loaded[label] = 999
            return subprocess.CompletedProcess(argv, 0, "", "")
        if verb == "bootout":
            self.loaded.pop(argv[2].rsplit("/", 1)[1], None)
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


def _install(tmp_path, *, lc=None, ps_text="", dry=False, platform="darwin", monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(launchd.sys, "platform", platform)
    out = io.StringIO()
    lc = lc or FakeLaunchctl()
    rc = launchd.install(agents=tmp_path / "LaunchAgents", uid=501, run=lc, ps=lambda: ps_text,
                         exe="/Users/u/.local/bin/apex-router", mesh_home=tmp_path / "mesh",
                         path_env="/opt/homebrew/bin:/usr/bin", home_dir=tmp_path,
                         dry_run=dry, out=out)
    return rc, out.getvalue(), lc


def test_install_writes_both_plists_and_bootstraps(tmp_path, monkeypatch):
    rc, _, lc = _install(tmp_path, monkeypatch=monkeypatch)
    assert rc == 0
    for role, label in launchd.LABELS.items():
        p = tmp_path / "LaunchAgents" / f"{label}.plist"
        doc = plistlib.loads(p.read_bytes())
        assert doc["Label"] == label
        assert doc["ProgramArguments"] == ["/Users/u/.local/bin/apex-router", "mesh", role]
        assert doc["KeepAlive"] is True and doc["RunAtLoad"] is True
        assert doc["ThrottleInterval"] == 30
        log = str(tmp_path / "mesh" / launchd.LOGS[role])
        assert doc["StandardOutPath"] == log and doc["StandardErrorPath"] == log
        assert doc["EnvironmentVariables"] == {"PATH": "/opt/homebrew/bin:/usr/bin"}
        assert doc["WorkingDirectory"] == str(tmp_path)
        assert ["launchctl", "bootstrap", "gui/501", str(p)] in lc.calls
    assert launchd.LOGS == {"up": "up.log", "worker": "worker.log"}


def test_install_refuses_with_processes_outside_launchd(tmp_path, monkeypatch):
    ps = ("  101 /Users/u/.local/bin/apex-router mesh up\n"
          "  102 /usr/bin/python3 /Users/u/.local/bin/apex-router mesh worker --poll-s 5\n"
          "  103 /Users/u/.local/bin/apex-router mesh status\n"
          "  104 vim mesh up\n")
    rc, out, lc = _install(tmp_path, ps_text=ps, monkeypatch=monkeypatch)
    assert rc == 1 and "pid 101" in out and "pid 102" in out
    assert "pid 103" not in out and "pid 104" not in out
    assert "kill 101 102" in out
    assert not (tmp_path / "LaunchAgents").exists()
    assert not any(c[1] in ("bootstrap", "bootout") for c in lc.calls)   # never stops anything


def test_install_refuses_when_already_loaded(tmp_path, monkeypatch):
    lc = FakeLaunchctl(loaded=[launchd.LABELS["up"]])
    rc, out, _ = _install(tmp_path, lc=lc, monkeypatch=monkeypatch)
    assert rc == 1 and "uninstall" in out and not (tmp_path / "LaunchAgents").exists()


def test_install_dry_run_prints_plists_and_writes_nothing(tmp_path, monkeypatch):
    rc, out, lc = _install(tmp_path, dry=True, platform="linux", monkeypatch=monkeypatch)
    assert rc == 0 and lc.calls == [] and not (tmp_path / "LaunchAgents").exists()
    assert out.count("<?xml") == 2 and "com.apex-router.mesh-worker" in out
    assert "<key>ThrottleInterval</key>" in out


def test_install_reports_a_failed_bootstrap(tmp_path, monkeypatch):
    lc = FakeLaunchctl(fail=[launchd.LABELS["worker"]])
    rc, out, _ = _install(tmp_path, lc=lc, monkeypatch=monkeypatch)
    assert rc == 1 and "bootstrap com.apex-router.mesh-worker failed" in out
    assert "loaded com.apex-router.mesh-up" in out


def test_install_is_macos_only(tmp_path, monkeypatch):
    rc, _, lc = _install(tmp_path, platform="linux", monkeypatch=monkeypatch)
    assert rc == 2 and lc.calls == []


def test_uninstall_boots_out_and_removes(tmp_path, monkeypatch):
    lc = FakeLaunchctl()
    assert _install(tmp_path, lc=lc, monkeypatch=monkeypatch)[0] == 0
    out = io.StringIO()
    assert launchd.uninstall(agents=tmp_path / "LaunchAgents", uid=501, run=lc, out=out) == 0
    assert ["launchctl", "bootout", "gui/501/com.apex-router.mesh-up"] in lc.calls
    assert not list((tmp_path / "LaunchAgents").glob("*.plist"))


def test_status_lists_agents_and_outside_processes(tmp_path):
    lc = FakeLaunchctl(loaded=[launchd.LABELS["up"]])
    ps = "4242 /x/apex-router mesh up\n77 /x/apex-router mesh worker\n"
    doc = launchd.status(agents=tmp_path, uid=501, run=lc, ps=lambda: ps)
    up, wk = doc["agents"]
    assert up["loaded"] is True and up["pid"] == 4242 and up["last_exit"] == "0"
    assert wk["loaded"] is False and wk["installed"] is False
    assert [o["pid"] for o in doc["outside"]] == [77]                  # 4242 is launchd's


def test_cli_parses_launchd_and_injects(tmp_path, monkeypatch, capsys):
    from apex_router.mesh import cli as mesh_cli
    seen = {}
    real_cmd = launchd.cmd

    def fake_cmd(action, **kw):
        seen.update(action=action, **kw)
        return 0
    monkeypatch.setattr(launchd, "cmd", fake_cmd)
    assert mesh_cli.main(["launchd", "install", "--dry-run"]) == 0
    assert seen == {"action": "install", "dry_run": True, "as_json": False}
    out = io.StringIO()
    lc = FakeLaunchctl()
    assert real_cmd("status", agents=tmp_path, run=lc, ps=lambda: "", out=out) == 0
    assert "com.apex-router.mesh-up" in out.getvalue() and "loaded=no" in out.getvalue()
