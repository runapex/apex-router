"""scripts/az-token-cached (A1): single flight, one deadline, kernel lock, no orphan keeps the call open.

A fake `az` (shell script) stands in for the real CLI. No network, no real az.
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "az-token-cached"
pytestmark = pytest.mark.skipif(not all(shutil.which(b) for b in ("perl", "shasum")),
                                reason="needs perl and shasum")

FAKE_AZ = """#!/bin/sh
echo call >> "$FAKE_LOG"
[ -n "${FAKE_ORPHAN:-}" ] && { sleep 30 & }
sleep "${FAKE_SLEEP:-1}"
[ "${FAKE_FAIL:-0}" = 1 ] && exit 1
e=$(( $(date +%s) + 3600 )); printf '{"accessToken":"TOK%s","expires_on":"%s"}' "$e" "$e"
"""


@pytest.fixture
def env(tmp_path):
    az = tmp_path / "az"
    az.write_text(FAKE_AZ)
    az.chmod(0o755)
    e = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache"),
         "AZ_BIN": str(az), "FAKE_LOG": str(tmp_path / "calls")}
    return e


def run(env, **extra):
    t = time.monotonic()
    p = subprocess.run([str(SCRIPT)], env={**env, **extra}, capture_output=True, text=True, timeout=30, check=False)
    return p.returncode, p.stdout, time.monotonic() - t


def calls(env):
    f = Path(env["FAKE_LOG"])
    return len(f.read_text().splitlines()) if f.exists() else 0


def cache(env):
    d = Path(env["XDG_CACHE_HOME"]) / "az-token"
    key = subprocess.run("printf %s https://cognitiveservices.azure.com | shasum | cut -c1-16", shell=True,
                         capture_output=True, text=True, check=False).stdout.strip()
    return d / f"{key}.token", d / f"{key}.exp", d / f"{key}.flock"


def test_cold_cache_parallel_callers_one_az_call(env):
    with cf.ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda _: run(env), range(8)))
    assert calls(env) == 1
    assert {r[0] for r in res} == {0}
    assert len({r[1] for r in res}) == 1 and res[0][1].startswith("TOK")


def test_fresh_cache_no_call(env):
    run(env)
    rc, _, el = run(env)
    assert (rc, calls(env)) == (0, 1) and el < 1.5


def test_refresh_window_serves_old_token_and_refreshes_once(env):
    run(env)
    tok, exp, _ = cache(env)
    old = tok.read_text()
    exp.write_text(str(int(time.time()) + 600))
    with cf.ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda _: run(env), range(8)))
    assert {r[1] for r in res} == {old} and max(r[2] for r in res) < 1.5
    time.sleep(2.5)
    assert calls(env) == 2  # 1 cold + 1 background refresh for 8 callers
    assert int(exp.read_text()) - time.time() > 3000


def test_hung_az_is_bounded_by_the_deadline(env):
    rc, _, el = run(env, FAKE_SLEEP="30", AZ_TOKEN_DEADLINE_S="2")
    assert rc == 1 and el < 4


def test_orphan_child_holding_stdout_does_not_block(env):
    # az ends, but its child keeps the inherited stdout open: the call must still return at once
    rc, out, el = run(env, FAKE_ORPHAN="1", FAKE_SLEEP="0")
    assert rc == 0 and out.startswith("TOK") and el < 3


def test_failed_az_keeps_valid_old_token(env):
    tok, exp, _ = cache(env)
    tok.parent.mkdir(parents=True)
    tok.write_text("OLDTOK")
    exp.write_text(str(int(time.time()) + 300))
    rc, out, _ = run(env, FAKE_FAIL="1")
    assert (rc, out) == (0, "OLDTOK")
    time.sleep(2)
    assert tok.read_text() == "OLDTOK"


HOLD_LOCK = 'open(my $l,">>",$ARGV[0]) or die; flock($l,LOCK_EX) or die; print "ok\\n"; $|=1; sleep 30'


def test_waiter_never_runs_az_while_lock_is_held(env):
    _, _, lock = cache(env)
    lock.parent.mkdir(parents=True)
    holder = subprocess.Popen(["perl", "-MFcntl=:flock", "-e",
                               HOLD_LOCK, str(lock)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline() == "ok\n"
        rc, _, el = run(env, AZ_TOKEN_DEADLINE_S="3")
        assert rc == 1 and calls(env) == 0 and 2 <= el < 5
    finally:
        holder.kill()
        holder.wait()


def test_dead_lock_holder_does_not_block(env):
    # a holder that died (SIGKILL) leaves the lock file; the kernel lock is gone, so az runs at once
    _, _, lock = cache(env)
    lock.parent.mkdir(parents=True)
    subprocess.run(["perl", "-MFcntl=:flock", "-e", 'open(my $l,">>",$ARGV[0]); flock($l,LOCK_EX); kill "KILL",$$',
                    str(lock)], check=False)
    assert lock.exists()
    rc, out, el = run(env, FAKE_SLEEP="0")
    assert rc == 0 and out.startswith("TOK") and calls(env) == 1 and el < 3


def test_takeover_after_holder_fails_stays_inside_one_deadline(env):
    # holder: az fails after 2 s; waiter then takes over, but the whole call stays inside DEADLINE_S
    with cf.ThreadPoolExecutor(2) as ex:
        a = ex.submit(run, env, FAKE_SLEEP="2", FAKE_FAIL="1", AZ_TOKEN_DEADLINE_S="4")
        time.sleep(0.3)
        b = ex.submit(run, env, FAKE_SLEEP="30", AZ_TOKEN_DEADLINE_S="4")
        ra, rb = a.result(), b.result()
    assert ra[0] == 1 and rb[0] == 1
    assert rb[2] < 5.5  # one deadline (4 s) + slack, not 2 x deadline


def test_killed_supervisor_keeps_the_lock_while_az_runs(env, tmp_path):
    # the perl supervisor dies (SIGKILL) while az runs: az keeps the lock fd, so no 2nd az starts
    a = subprocess.Popen([str(SCRIPT)], env={**env, "FAKE_SLEEP": "6", "AZ_TOKEN_DEADLINE_S": "8"},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            if calls(env) == 1:
                break
            time.sleep(0.1)
        assert calls(env) == 1
        az_pid = subprocess.run(["pgrep", "-f", env["AZ_BIN"]], capture_output=True, text=True,
                                check=False).stdout.split()[0]
        sup = subprocess.run(["ps", "-o", "ppid=", "-p", az_pid], capture_output=True, text=True,
                             check=False).stdout.strip()
        os.kill(int(sup), 9)   # the perl supervisor of THIS fake az only (never a live az-token-cached)
        rc, _, _ = run(env, AZ_TOKEN_DEADLINE_S="2", FAKE_SLEEP="0")
        assert rc == 1 and calls(env) == 1   # waited; never ran az while the orphan held the lock
    finally:
        a.kill()
        a.wait()
        subprocess.run(["pkill", "-KILL", "-f", env["AZ_BIN"]], check=False)


def test_old_output_files_are_removed(env):
    tok, _, _ = cache(env)
    tok.parent.mkdir(parents=True)
    stale = tok.with_name(tok.name + ".out.12345")
    stale.write_text("{}")
    os.utime(stale, (time.time() - 300, time.time() - 300))
    rc, _, _ = run(env, FAKE_SLEEP="0")
    assert rc == 0 and not stale.exists()
