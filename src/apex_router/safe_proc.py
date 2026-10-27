"""One bounded way to run a helper command: time, output bytes and the whole process tree.

``run_capped(argv, timeout, out_max)`` -> (rc, bytes) or raises subprocess.TimeoutExpired.
  * the child runs in its own process group (start_new_session); at the timeout the whole group gets
    SIGKILL, so a grandchild that holds the pipe cannot keep the read open;
  * at most ``out_max`` bytes are kept; with ``stop_at_cap`` the group is killed at the cap, else the
    rest is read and dropped so the child does not block on a full pipe;
  * the timer is made inside ``try``; ``finally`` always kills the group if it still runs, waits at
    most 1 s and closes the pipe. stderr goes to the same pipe (``merge_stderr``) or to /dev/null.
Earlier review findings on two copies of this code (studio/records.py, preflight.py): a grandchild kept
the pipe open past the timeout; a timer start before ``try`` skipped the cleanup.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time


def _kill_group(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def run_capped(argv: list, timeout: float, out_max: int, *, merge_stderr: bool = False,
               stop_at_cap: bool = False) -> tuple[int | None, bytes]:
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
                         start_new_session=True)
    keep, timer, end, rc = bytearray(), None, time.monotonic() + timeout, None
    try:
        timer = threading.Timer(timeout, _kill_group, (p,))
        timer.daemon = True
        timer.start()
        while True:
            if stop_at_cap and len(keep) >= out_max:
                break
            chunk = p.stdout.read(65536)
            if not chunk:
                break
            if len(keep) < out_max:
                keep += chunk[: out_max - len(keep)]
        if not (stop_at_cap and len(keep) >= out_max):
            rc = p.wait(timeout=max(0.1, end - time.monotonic()))
    finally:
        if timer is not None:
            timer.cancel()
        if p.poll() is None:
            _kill_group(p)
        try:
            p.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        p.stdout.close()
    if time.monotonic() > end:
        raise subprocess.TimeoutExpired(argv, timeout)
    return rc, bytes(keep)
