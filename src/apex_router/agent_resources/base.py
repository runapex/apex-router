"""Shared plumbing: the refresh deadline, the read-only command / loopback-HTTP runners and
small number helpers. ``_sleep`` / ``_clock`` are package attributes tests replace."""
from __future__ import annotations

import subprocess
import sys
import time
import urllib.request


PROCS_TOP = 3
GRAPH_PROCS_MAX = 5
GRAPH_MODELS_MAX = 8
GRAPH_SESSIONS_MAX = 30
# All subprocess + network timeouts + the rate sample together. A full refresh measures
# 0.36-0.46 s, but endpoint security (Defender / CrowdStrike) makes each spawn 40-80 ms, so a
# 0.45 s budget skipped ps args / ioreg / ollama / /healthz on ~half the refreshes and the menu
# showed live services as "not running". The plugin refreshes once a minute; 1.2 s is cheap.
DEADLINE_S = 1.2
DEADLINE_MIN_S = 0.05             # a source is skipped when less than this remains
_MB = 1024 * 1024
_LSTART_FMT = "%a %b %d %H:%M:%S %Y"

_sleep = time.sleep               # re-exported by the package, whose attributes tests fake
_clock = time.monotonic


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


def _mb(n_bytes):
    return round(n_bytes / _MB, 1) if isinstance(n_bytes, (int, float)) else None


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def run_cmd(argv, timeout: float = 2.0) -> str:
    """stdout of a read-only command. Raises on failure — callers record it and fail open."""
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                       errors="replace")
    if r.returncode != 0 and not r.stdout:
        raise RuntimeError(f"{argv[0]} exit {r.returncode}")
    return r.stdout


def http_get(url: str, timeout: float = 1.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(256 * 1024)


class DeadlineSkip(Exception):
    """A source was not started: the shared deadline had (nearly) passed."""


class Deadline:
    """One time budget shared by every subprocess / network call of a refresh."""

    def __init__(self, budget: float = DEADLINE_S, clock=None):
        # the package attribute, looked up now: tests replace ``agent_resources._clock``
        self.clock = clock or getattr(sys.modules.get(__package__), "_clock", _clock)
        self.end = self.clock() + budget

    def remaining(self) -> float:
        return self.end - self.clock()

    def timeout(self, cap: float) -> float:
        """min(cap, time left); raises DeadlineSkip when less than DEADLINE_MIN_S is left."""
        left = self.remaining()
        if left < DEADLINE_MIN_S:
            raise DeadlineSkip("deadline exceeded, source skipped")
        return min(cap, left)
