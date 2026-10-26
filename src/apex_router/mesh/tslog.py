"""One timestamp prefix for the log lines of the long-running ``mesh up`` and ``mesh worker``.

The prefix is ISO-8601 local time with the UTC offset, then one space; the line text after it is
unchanged. Only lines of the two daemons get it; one-shot commands print as before.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from typing import TextIO


def stamp(text: str, now: float | None = None) -> str:
    """``2026-10-26T20:31:05+01:00 <text>`` (local time, seconds)."""
    t = time.time() if now is None else float(now)
    return f"{datetime.fromtimestamp(t).astimezone().isoformat(timespec='seconds')} {text}"


def say(text: str, *, file: TextIO | None = None) -> None:
    """Print one stamped line and flush (the log file is the reader, not a terminal)."""
    print(stamp(text), file=file if file is not None else sys.stdout, flush=True)
