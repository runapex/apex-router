"""scripts/az-token-cached (A1): single flight, one deadline, kernel lock, no orphan keeps the call open.

A fake `az` (shell script) stands in for the real CLI. No network, no real az.
"""
from __future__ import annotations

import concurrent.futures as cf
import contextlib
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
if [ -n "${FAKE_DETACH:-}" ]; then
  perl -MPOSIX -e 'POSIX::setsid() > 0 or exit 1; open(my $f, ">", "$ARGV[0].tmp") or exit 1; print $f $$; close $f;
                   rename("$ARGV[0].tmp", $ARGV[0]); sleep 30' "$FAKE_DETACH" &
  i=0; while [ ! -s "$FAKE_DETACH" ] && [ $i -lt 100 ]; do sleep 0.05; i=$((i+1)); done
fi
sleep "${FAKE_SLEEP:-1}"
[ "${FAKE_FAIL:-0}" = 1 ] && exit 1
[ "$(wc -l < "$FAKE_LOG")" -le "${FAKE_FAIL_FIRST:-0}" ] && exit 1
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
    exp.write_text(str(int(time.time()) + 200))   # inside the default 240 s refresh window
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
    exp.write_text(str(int(time.time()) + 200))   # valid, inside the refresh window: refresh fails
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


def test_detached_child_of_az_never_holds_the_lock(env, tmp_path):
    # a child of az that leaves the process group (setsid) survives the group kill. It must not
    # hold the lock: the next refresh runs az at once (xval 2026-10-26, pass 3)
    pidf = tmp_path / "detached.pid"
    try:
        rc, out, _ = run(env, FAKE_DETACH=str(pidf), FAKE_SLEEP="0")
        assert rc == 0 and out.startswith("TOK")
        pid = int(pidf.read_text())          # the child detached (setsid done) before az ended
        os.kill(pid, 0)                      # and it is still alive
        _, exp, _ = cache(env)
        exp.write_text("0")
        rc, out, el = run(env, FAKE_SLEEP="0")
        assert rc == 0 and calls(env) == 2 and el < 3
        os.kill(pid, 0)                      # still alive during the 2nd refresh: the lock was not its
    finally:
        if pidf.exists():
            with contextlib.suppress(OSError):
                os.kill(int(pidf.read_text()), 9)


def test_old_output_files_are_removed(env):
    tok, _, _ = cache(env)
    tok.parent.mkdir(parents=True)
    stale = tok.with_name(tok.name + ".out.12345")
    other = tok.with_name("unrelated.out.1")   # not ours: must stay
    other.write_text("x")
    os.utime(other, (time.time() - 300, time.time() - 300))
    stale.write_text("{}")
    os.utime(stale, (time.time() - 300, time.time() - 300))
    rc, _, _ = run(env, FAKE_SLEEP="0")
    assert rc == 0 and not stale.exists() and other.exists()


def test_default_refresh_window_is_inside_msal_window(env):
    # MSAL returns the SAME token while more than 300 s are left: a refresh there is a wasted az call
    run(env)
    _, exp, _ = cache(env)
    exp.write_text(str(int(time.time()) + 280))
    rc, _, _ = run(env)
    time.sleep(1.5)
    assert rc == 0 and calls(env) == 1


def test_keepwarm_min_s_refreshes_synchronously(env):
    # keep-warm mode: below AZ_TOKEN_MIN_S the call itself runs az (rule 3) and returns the new token
    run(env)
    tok, exp, _ = cache(env)
    tok.write_text("OLDTOK")
    exp.write_text(str(int(time.time()) + 200))
    rc, out, _ = run(env, FAKE_SLEEP="0", AZ_TOKEN_MIN_S="240", AZ_TOKEN_REFRESH_S="240")
    assert rc == 0 and calls(env) == 2 and out.startswith("TOK") and out == tok.read_text()
    assert int(exp.read_text()) - time.time() > 3000


def test_keepwarm_min_s_no_call_when_token_is_fresh(env):
    run(env)
    rc, _, _ = run(env, AZ_TOKEN_MIN_S="240", AZ_TOKEN_REFRESH_S="240")
    assert rc == 0 and calls(env) == 1


def test_min_s_above_refresh_s_is_never_served_below_min_s(env):
    run(env)
    tok, exp, _ = cache(env)
    tok.write_text("OLDTOK")
    exp.write_text(str(int(time.time()) + 280))       # > default REFRESH_S 240, < MIN_S 300
    rc, out, _ = run(env, FAKE_SLEEP="0", AZ_TOKEN_MIN_S="300")
    assert rc == 0 and out.startswith("TOK") and calls(env) == 2


def test_pi_call_is_not_blocked_while_keepwarm_refreshes(env):
    # keep-warm holds the lock with a slow az; a pi call with > 60 s left answers from cache at once
    run(env)
    tok, exp, _ = cache(env)
    tok.write_text("OLDTOK")
    exp.write_text(str(int(time.time()) + 200))
    with cf.ThreadPoolExecutor(2) as ex:
        kw = ex.submit(run, env, FAKE_SLEEP="3", AZ_TOKEN_MIN_S="240", AZ_TOKEN_DEADLINE_S="30")
        time.sleep(0.5)
        rc, out, el = run(env)
        assert (rc, out) == (0, "OLDTOK") and el < 1.5
        krc, kout, _ = kw.result()
    assert krc == 0 and kout.startswith("TOK") and calls(env) == 2


KEEPWARM = SCRIPT.with_name("az-token-keepwarm.sh")


def keepwarm_env(env, tmp_path):
    bindir = tmp_path / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, bindir / "az-token-cached")
    return {**env, "AZ_KEEPWARM_NO_LOAD": "1", "PATH": f"{tmp_path}:{env['PATH']}"}


def test_keepwarm_run_retries_a_failed_refresh_at_once(env, tmp_path):
    e = keepwarm_env(env, tmp_path)
    t = time.monotonic()
    p = subprocess.run(["bash", str(KEEPWARM), "run"], env={**e, "FAKE_SLEEP": "0", "FAKE_FAIL_FIRST": "1"},
                       capture_output=True, text=True, timeout=60)
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "") and calls(env) == 2
    assert 4 < time.monotonic() - t < 15                       # one 5 s pause, not a 60 s tick
    assert cache(env)[0].read_text().startswith("TOK")


def test_keepwarm_run_logs_one_line_without_token_after_last_try(env, tmp_path):
    e = keepwarm_env(env, tmp_path)
    p = subprocess.run(["bash", str(KEEPWARM), "run"], env={**e, "FAKE_SLEEP": "0", "FAKE_FAIL": "1",
                       "AZ_KEEPWARM_TRIES": "2"}, capture_output=True, text=True, timeout=60)
    assert p.returncode == 1 and p.stdout == "" and calls(env) == 2
    assert p.stderr.count("\n") == 1 and "after 2 tries" in p.stderr and "TOK" not in p.stderr


@pytest.mark.skipif(not shutil.which("plutil"), reason="needs macOS plutil")
def test_keepwarm_install_plist_runs_without_extra_env(env, tmp_path):
    import plistlib
    home = tmp_path / "we&ird <home>"                         # XML escaping
    home.mkdir()
    e = {**env, "HOME": str(home), "AZ_KEEPWARM_NO_LOAD": "1", "PATH": f"{tmp_path}:{env['PATH']}"}
    p = subprocess.run(["bash", str(KEEPWARM), "install"], env=e, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    bindir = home / ".local" / "bin"
    assert (bindir / "az-token-cached").read_bytes() == SCRIPT.read_bytes()
    assert (bindir / "az-token-keepwarm").read_bytes() == KEEPWARM.read_bytes()
    plist = plistlib.loads((home / "Library/LaunchAgents/com.apex.az-token-keepwarm.plist").read_bytes())
    assert plist["StandardOutPath"] == "/dev/null" and plist["StartInterval"] == 60
    envd = plist["EnvironmentVariables"]
    assert envd["XDG_CACHE_HOME"] == env["XDG_CACHE_HOME"] and envd["AZ_BIN"] == env["AZ_BIN"]
    # launchd's view: only HOME + the plist env (+ the fake az knobs). No XDG injected by the test.
    r = subprocess.run(plist["ProgramArguments"], env={**envd, "HOME": str(home), "FAKE_LOG": env["FAKE_LOG"],
                       "FAKE_SLEEP": "0"}, capture_output=True, text=True, timeout=60)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "") and calls(env) == 1
    assert cache(env)[0].read_text().startswith("TOK")         # same cache dir as pi
    assert not list((home / "Library/LaunchAgents").glob("*.tmp"))


def test_refresh_that_returns_too_short_a_token_fails(env):
    # MSAL can return the SAME token; if it has <= MIN_S left, the call must not report success
    tok, exp, _ = cache(env)
    tok.parent.mkdir(parents=True)
    rc, out, _ = run(env, FAKE_SLEEP="0", AZ_TOKEN_MIN_S="4000")   # fake az gives 3600 s
    assert rc == 1 and out == "" and calls(env) == 1
    assert tok.read_text().startswith("TOK")                     # stored for callers with a lower MIN_S


# ---- D3 (2026-10-28): the failure line names the power state ---------------------------------------------

@pytest.mark.parametrize("last,word", [("DarkWake", "dark-wake"), ("Wake", "awake"), ("Sleep", "unknown"), ("", "unknown")])
def test_keepwarm_failure_line_names_the_power_state(env, tmp_path, last, word):
    e = keepwarm_env(env, tmp_path)
    fake = tmp_path / "fake-pmset"
    lines = ["2026-10-28 02:11:00 -0700 Sleep                \tEntering Sleep"]
    if last:
        lines.append(f"2026-10-28 02:11:20 -0700 {last:<20}\tsomething")
    fake.write_text("#!/bin/sh\nprintf '%s\\n' " + " ".join(f"'{l}'" for l in lines) + "\n")
    fake.chmod(0o755)
    p = subprocess.run(["bash", str(KEEPWARM), "run"], env={**e, "FAKE_SLEEP": "0", "FAKE_FAIL": "1",
                       "AZ_KEEPWARM_TRIES": "1", "AZ_KEEPWARM_PMSET": str(fake)},
                       capture_output=True, text=True, timeout=60)
    want = word if last != "Sleep" else "unknown"
    assert p.returncode == 1 and p.stderr.strip().endswith(f"(power: {want})"), p.stderr
    assert "TOK" not in p.stderr


def test_keepwarm_failure_line_survives_a_missing_pmset(env, tmp_path):
    e = keepwarm_env(env, tmp_path)
    p = subprocess.run(["bash", str(KEEPWARM), "run"], env={**e, "FAKE_SLEEP": "0", "FAKE_FAIL": "1",
                       "AZ_KEEPWARM_TRIES": "1", "AZ_KEEPWARM_PMSET": str(tmp_path / "nope")},
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 1 and p.stderr.strip().endswith("(power: unknown)")


def test_keepwarm_power_state_finds_a_wake_far_back(env, tmp_path):
    """Live check found the newest Wake 5 h and thousands of lines back: the state must not depend on a tail."""
    e = keepwarm_env(env, tmp_path)
    log = tmp_path / "pmset.log"
    log.write_text("2026-10-28 09:44:15 -0700 Wake                \tDarkWake to FullWake\n"
                   + "2026-10-28 10:00:00 -0700 Assertions          \tPID 1 noise\n" * 5000)
    fake = tmp_path / "fake-pmset"
    fake.write_text(f"#!/bin/sh\ncat '{log}'\n")
    fake.chmod(0o755)
    p = subprocess.run(["bash", str(KEEPWARM), "run"], env={**e, "FAKE_SLEEP": "0", "FAKE_FAIL": "1",
                       "AZ_KEEPWARM_TRIES": "1", "AZ_KEEPWARM_PMSET": str(fake)},
                       capture_output=True, text=True, timeout=60)
    assert p.stderr.strip().endswith("(power: awake)"), p.stderr
