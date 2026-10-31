"""Claude Code surfaces of the agent inbox (docs/design/AGENTS-INBOX.md section 6): the Stop-hook alert, the
status line segment, the /agents command file and the install.sh --agents-hooks flag."""
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STOP = ROOT / "hooks" / "agents-stop-alert.sh"
LINE = ROOT / "hooks" / "agents-statusline.sh"
INSTALL = (ROOT / "install.sh").read_text()


def _fake_bin(tmp: Path, body: str, name: str = "fake-apex-router") -> Path:
    b = tmp / name
    b.write_text("#!/bin/sh\n" + body + "\n")
    b.chmod(0o755)
    return b


def _env(tmp: Path, bin_: Path) -> dict:
    return {"PATH": os.environ["PATH"], "HOME": str(tmp), "APEX_HOME": str(tmp / "rh"), "APEX_ROUTER_BIN": str(bin_)}


def _stop(tmp, bin_, stdin='{"session_id":"s","stop_hook_active":false}'):
    return subprocess.run(["bash", str(STOP)], input=stdin, capture_output=True, text=True, env=_env(tmp, bin_),
                          timeout=20)


def test_stop_alert_shows_new_blockers_as_a_system_message(tmp_path):
    b = _fake_bin(tmp_path, 'printf "⚠ 2026-x xval: verdict NO-GO\\n⚠ 2026-y worker: [P1] fix\\033[31m it\\n"')
    r = _stop(tmp_path, b)
    assert r.returncode == 0
    msg = json.loads(r.stdout)["systemMessage"]
    assert "verdict NO-GO" in msg and "[P1] fix" in msg and "\x1b" not in msg and "/agents" in msg
    assert set(json.loads(r.stdout)) == {"systemMessage"}           # never blocks, never adds model context


def test_stop_alert_is_silent_with_nothing_new_or_on_error(tmp_path):
    for body in ("exit 0", "echo boom >&2; exit 3", "sleep 30"):
        t = time.monotonic()
        r = _stop(tmp_path, _fake_bin(tmp_path, body))
        assert r.returncode == 0 and r.stdout == "" and time.monotonic() - t < 10, body
    r = _stop(tmp_path, tmp_path / "missing-bin")
    assert r.returncode == 0 and r.stdout == ""


def test_stop_alert_does_nothing_inside_a_stop_continuation(tmp_path):
    b = _fake_bin(tmp_path, f'echo called > "{tmp_path}/called"; echo "⚠ x"')
    r = _stop(tmp_path, b, '{"stop_hook_active": true}')
    assert r.returncode == 0 and r.stdout == "" and not (tmp_path / "called").exists()


def test_stop_alert_caps_lines_and_length(tmp_path):
    b = _fake_bin(tmp_path, 'for i in 1 2 3 4 5 6 7 8; do printf "⚠ %0300d\\n" $i; done')
    msg = json.loads(_stop(tmp_path, b).stdout)["systemMessage"]
    body = msg.splitlines()[1:-1]
    assert len(body) == 5 and all(len(x) <= 160 for x in body)


def test_status_line_prints_the_cache_and_never_waits(tmp_path):
    rh = tmp_path / "rh"
    rh.mkdir()
    (rh / "agent-inbox-line.json").write_text(json.dumps({"at": 1, "line": "● 2 ⚠ 3"}))
    b = _fake_bin(tmp_path, f'sleep 3; echo ran > "{tmp_path}/ran"')
    t = time.monotonic()
    r = subprocess.run(["bash", str(LINE)], capture_output=True, text=True, env=_env(tmp_path, b), timeout=10)
    assert time.monotonic() - t < 2 and r.returncode == 0
    assert r.stdout == "● 2 ⚠ 3"                                    # only the safe characters
    # a second call within 5 s does not start another refresh
    (rh / "agent-inbox-line.spawn").touch()
    b2 = _fake_bin(tmp_path, f'echo second > "{tmp_path}/second"', "fake2")   # own file: sh reads lazily
    subprocess.run(["bash", str(LINE)], capture_output=True, text=True, env=_env(tmp_path, b2), timeout=10)
    time.sleep(0.5)
    assert not (tmp_path / "second").exists()


def test_status_line_is_empty_with_no_cache_or_a_bad_cache(tmp_path):
    b = _fake_bin(tmp_path, "exit 0")
    rh = tmp_path / "rh"
    assert subprocess.run(["bash", str(LINE)], capture_output=True, text=True, env=_env(tmp_path, b),
                          timeout=10).stdout == ""
    (rh / "agent-inbox-line.json").write_text("{not json")
    assert subprocess.run(["bash", str(LINE)], capture_output=True, text=True, env=_env(tmp_path, b),
                          timeout=10).stdout == ""
    (rh / "agent-inbox-line.json").write_text(json.dumps({"at": 1, "line": "● 2 ⚠ 3\x1b]0;x\x07"}))
    assert subprocess.run(["bash", str(LINE)], capture_output=True, text=True, env=_env(tmp_path, b),
                          timeout=10).stdout == ""


def test_agents_command_passes_no_user_arguments_to_the_shell():
    text = (ROOT / "integrations" / "claude" / "agents-inbox.md").read_text()
    assert "!`apex-router agents`" in text and "$ARGUMENTS" not in text


def test_install_flag_wires_the_stop_hook_and_the_command(tmp_path):
    start = INSTALL.index("\ninstall_agents_hooks() {") + 1
    fn = INSTALL[start:INSTALL.index("\n}\n", start) + 3]
    stubs = ('warn(){ printf "WARN %s\\n" "$*"; }\nok(){ printf "OK %s\\n" "$*"; }\nsay(){ :; }\n'
             '_wire_stop_hook(){ printf "WIRED %s\\n" "$1"; }\n')
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    on = subprocess.run(["bash", "-c", stubs + fn + f"\nINSTALL_DIR={ROOT}\nDO_AGENTS_HOOKS=1\ninstall_agents_hooks\n"],
                        capture_output=True, text=True, env=env, timeout=10)
    assert f"WIRED {ROOT}/hooks/agents-stop-alert.sh" in on.stdout
    assert (tmp_path / ".claude" / "commands" / "agents.md").read_text().startswith("---")
    off = subprocess.run(["bash", "-c", stubs + fn + f"\nINSTALL_DIR={ROOT}\nDO_AGENTS_HOOKS=0\ninstall_agents_hooks\n"],
                         capture_output=True, text=True, env=env, timeout=10)
    assert "WIRED" not in off.stdout


def test_status_line_does_not_block_on_a_fifo_cache(tmp_path):
    rh = tmp_path / "rh"
    rh.mkdir()
    os.mkfifo(rh / "agent-inbox-line.json")
    t = time.monotonic()
    r = subprocess.run(["bash", str(LINE)], capture_output=True, text=True,
                       env=_env(tmp_path, _fake_bin(tmp_path, "exit 0")), timeout=10)
    assert r.returncode == 0 and r.stdout == "" and time.monotonic() - t < 3
