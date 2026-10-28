"""Count studio page opens per day (a number only).

The owner asked how often the studio is opened (D18: the menu bar shows problems; the studio opens
on demand). Each GET / adds one to today's count in ``<router home>/studio/opens.json``:
``{"days": {"YYYY-MM-DD": n}}``. No path, query, token, header or time of day is stored.

Bounds: the file is read only when it is at most ``READ_MAX`` bytes (else the count starts again);
at most ``DAYS_KEEP`` days are kept; a count is capped at ``COUNT_MAX``. The write is atomic
(temp file + ``os.replace``) under an ``fcntl.flock`` on ``opens.json.lock``. Every error is
swallowed: a count never stops the page.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import re
import tempfile
from pathlib import Path

READ_MAX = 16 * 1024
DAYS_KEEP = 90
COUNT_MAX = 1_000_000
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _load(path: Path) -> dict:
    """{day: count} from the file; {} when it is missing, too large or bad."""
    try:
        with open(path, "rb") as f:
            raw = f.read(READ_MAX + 1)
        if len(raw) > READ_MAX:
            return {}
        doc = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return {}
    days = doc.get("days") if isinstance(doc, dict) else None
    if not isinstance(days, dict):
        return {}
    return {k: v for k, v in days.items()
            if isinstance(k, str) and _DAY.fullmatch(k) and isinstance(v, int) and not isinstance(v, bool)
            and 0 <= v <= COUNT_MAX}


def record(studio_dir: Path, today: _dt.date | None = None) -> bool:
    """Add one open to today's count. Returns False on any error (never raises)."""
    day = (today or _dt.date.today()).isoformat()
    try:
        studio_dir.mkdir(parents=True, exist_ok=True)
        path = studio_dir / "opens.json"
        with open(studio_dir / "opens.json.lock", "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            days = _load(path)
            days[day] = min(COUNT_MAX, days.get(day, 0) + 1)
            keep = dict(sorted(days.items())[-DAYS_KEEP:])
            fd, tmp = tempfile.mkstemp(dir=studio_dir, prefix=".opens-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump({"days": keep}, f, sort_keys=True)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        return True
    except Exception:
        return False


def summary(studio_dir: Path, today: _dt.date | None = None) -> dict:
    """{"today": n, "d7": n, "d30": n, "days_seen": n} from the file."""
    t = today or _dt.date.today()
    days = _load(studio_dir / "opens.json")

    def since(n: int) -> int:
        lo = (t - _dt.timedelta(days=n - 1)).isoformat()
        return sum(v for k, v in days.items() if lo <= k <= t.isoformat())
    return {"today": days.get(t.isoformat(), 0), "d7": since(7), "d30": since(30),
            "days_seen": sum(1 for v in days.values() if v > 0)}
