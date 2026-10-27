"""safe_proc.run_capped: bounded time, bytes and process tree (one copy for every helper command)."""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from apex_router.safe_proc import run_capped

GRANDCHILD = ("import subprocess, sys, time; "
              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)']); time.sleep(30)")


def test_output_is_capped_and_rc_kept():
    rc, out = run_capped([sys.executable, "-c", "import sys; sys.stdout.write('a' * 3_000_000)"], 10, 1000)
    assert rc == 0 and out == b"a" * 1000


def test_stop_at_cap_stops_a_long_writer_early():
    t = time.monotonic()
    rc, out = run_capped([sys.executable, "-c", "import sys,time\nwhile True: sys.stdout.write('a'*65536); sys.stdout.flush()"],
                         10, 1000, stop_at_cap=True)
    assert rc is None and len(out) == 1000 and time.monotonic() - t < 3


@pytest.mark.parametrize("argv", [[sys.executable, "-c", GRANDCHILD], ["/bin/sh", "-c", "sleep 60 & wait"]])
def test_timeout_kills_the_whole_tree(argv):
    t = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_capped(argv, 0.5, 1000)
    assert time.monotonic() - t < 3


def test_cleanup_runs_when_the_timer_cannot_start(monkeypatch):
    import apex_router.safe_proc as S
    seen = []
    real = subprocess.Popen

    def popen(*a, **k):
        seen.append(real(*a, **k))
        return seen[-1]
    monkeypatch.setattr(S.subprocess, "Popen", popen)

    class NoThread:
        def __init__(self, *a, **k):
            raise RuntimeError("can't start new thread")
    monkeypatch.setattr(S.threading, "Timer", NoThread)
    with pytest.raises(RuntimeError):
        run_capped(["/bin/sleep", "30"], 5, 100)
    assert seen[0].poll() is not None and seen[0].stdout.closed
