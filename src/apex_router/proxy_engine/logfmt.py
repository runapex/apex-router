"""Timestamped stderr for the serve process (B2).

launchd sends the proxy's stderr to one file with no timestamps of its own. Every line the serve
process writes there must start with an ISO-8601 local timestamp, so a traceback can be placed in
time. Two paths write to stderr: the `logging` module (uvicorn, asyncio, third-party libs) and
direct `print(..., file=sys.stderr)` calls in the proxy. `uvicorn_log_config()` covers the first;
`eprint()` covers the second. sys.stderr itself is never wrapped.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime


def iso_ts(epoch: float | None = None) -> str:
    """ISO-8601 local time with milliseconds and UTC offset, e.g. 2026-10-26T20:28:06.123+01:00."""
    utc = datetime.now(UTC) if epoch is None else datetime.fromtimestamp(epoch, UTC)
    return utc.astimezone().isoformat(timespec="milliseconds")  # astimezone(): local zone


def stamp(text: str, epoch: float | None = None) -> str:
    """Prefix EVERY line of `text` with one timestamp (a multi-line message stays one event)."""
    ts = iso_ts(epoch)
    return "\n".join(f"{ts} {line}" for line in text.split("\n"))


def eprint(*args: object, sep: str = " ", file=None) -> None:
    """print() to stderr with a timestamp prefix on each line. Drop-in for print(..., file=sys.stderr)."""
    out = sys.stderr if file is None else file
    print(stamp(sep.join(str(a) for a in args)), file=out, flush=True)


class StampFormatter(logging.Formatter):
    """A Formatter that puts the record time (ISO-8601 local) before every output line, also before
    each traceback line, so no stderr line from `logging` is without a timestamp."""

    def format(self, record: logging.LogRecord) -> str:
        return stamp(super().format(record), record.created)


def uvicorn_log_config() -> dict:
    """uvicorn `log_config` dict: same loggers as uvicorn's default, but every formatter stamps each
    line. The root logger gets the stderr handler at WARNING, so library warnings (asyncio, httpx)
    that before went to logging's last-resort handler without a time now get one too."""
    fmt = "%(levelname)s %(name)s: %(message)s"
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"()": StampFormatter, "fmt": fmt},
            "access": {"()": StampFormatter, "fmt": fmt},
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "class": "logging.StreamHandler",
                "formatter": "access",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
        "root": {"handlers": ["default"], "level": "WARNING"},
    }
