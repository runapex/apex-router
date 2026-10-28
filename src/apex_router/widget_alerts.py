"""One macOS notification for a NEW menu-bar problem (owner decision D18).

``snapshot --menubar --alert`` (the SwiftBar plugin) calls ``run(problems)`` after the menu is printed:

  new key    a problem ``key`` (``snapshot.problems``) not seen in the last ``DEDUPE_S`` (30 min)
  rate       at most one notification in ``RATE_S`` (10 min); a new key held back by the rate alerts
             at the next free slot (it is marked seen only when it was in a sent alert)
  state      ``~/.apex-router/widget/alerts.json`` ``{v, last_alert, seen: {key: ts}}``: at most
             ``KEYS_MAX`` keys, read at most ``STATE_BYTES_MAX`` bytes (a bad or larger file is empty),
             written atomically (temp file + ``os.replace``, mode 0600) BEFORE the notification; a
             failed write sends nothing (no state, no rate limit)
  notify     ``osascript -e <fixed script> <message>``: the message is an argument (``item 1 of argv``),
             never part of the script; it must match ``MESSAGE_RE`` (fixed-map words); the call has a
             ``NOTIFY_TIMEOUT_S`` (3 s) limit and the whole process group is stopped at it
  off        ``APEX_ROUTER_NO_ALERTS=1``: no state, no notification
Stdlib only. Never raises out of ``run`` for a bad state file; the caller still guards it.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

DEDUPE_S = 30 * 60
RATE_S = 10 * 60
KEYS_MAX = 200
STATE_BYTES_MAX = 64 * 1024
NOTIFY_TIMEOUT_S = 3
OSASCRIPT = "/usr/bin/osascript"
OFF_ENV = "APEX_ROUTER_NO_ALERTS"
TITLE = "apex-router"
# The script is a constant; the text reaches it only as argv (no string building, no injection).
SCRIPT = f'on run argv\ndisplay notification (item 1 of argv) with title "{TITLE}"\nend run'
MESSAGE_RE = re.compile(r"[a-z0-9 ]{1,80}")
KEY_RE = re.compile(r"[a-z0-9_:.-]{1,80}")
# kind -> fixed words of the notification (no numbers, no ids, no raw text)
TITLES = {
    "proxy_down": "proxy down", "worker_down": "worker down", "pressure_red": "pressure red",
    "pressure_amber": "pressure amber", "upstream_502": "upstream 502", "upstream_errors": "upstream errors",
    "job_quiet": "a job is quiet", "job_failed": "a job failed", "job_capped": "a job was capped",
    "decisions_open": "owner decisions open",
}


def path() -> Path:
    home = os.environ.get("APEX_ROUTER_HOME")
    return (Path(home) if home else Path.home() / ".apex-router") / "widget" / "alerts.json"


_state_path = path


def disabled() -> bool:
    return os.environ.get(OFF_ENV, "") not in ("", "0")


def _fnum(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def load(p: Path | None = None) -> dict:
    """The state, or ``{}`` when it is missing, bad or larger than ``STATE_BYTES_MAX``."""
    p = Path(p) if p else path()
    try:
        with open(p, "rb") as f:
            raw = f.read(STATE_BYTES_MAX + 1)
    except OSError:
        return {}
    if len(raw) > STATE_BYTES_MAX:
        return {}
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {}
    if not isinstance(doc, dict):
        return {}
    seen = doc.get("seen") if isinstance(doc.get("seen"), dict) else {}
    out = {"seen": {k: _fnum(v) for k, v in seen.items()
                    if isinstance(k, str) and KEY_RE.fullmatch(k) and _fnum(v) is not None}}
    if _fnum(doc.get("last_alert")) is not None:
        out["last_alert"] = _fnum(doc["last_alert"])
    return out


def save(state: dict, p: Path | None = None) -> bool:
    """Atomic write (temp file in the same dir, fsync, ``os.replace``), mode 0600. False on error."""
    p = Path(p) if p else path()
    tmp = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".alerts.", suffix=".tmp", dir=p.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        tmp = None
        return True
    except OSError:
        return False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _message(new: list) -> str:
    words = TITLES.get(new[0].get("kind"), "new problem")
    return words if len(new) == 1 else f"{words} and {len(new) - 1} more"


def decide(state: dict, items: list, now: float) -> tuple:
    """(new state, message or None). Pure. ``items`` are ``snapshot.problems`` items, worst first."""
    seen = {k: v for k, v in (state.get("seen") or {}).items()
            if isinstance(k, str) and _fnum(v) is not None and now - v < DEDUPE_S}
    last = _fnum(state.get("last_alert"))
    if last is not None and last > now:                  # clock went back: do not block for ever
        last = None
    cur = [it for it in items if isinstance(it, dict) and isinstance(it.get("key"), str)
           and KEY_RE.fullmatch(it["key"])]
    new = [it for it in cur if it["key"] not in seen]
    # a key already alerted stays "seen" while it is present
    for it in cur:
        if it["key"] in seen:
            seen[it["key"]] = now
    msg = None
    if new and (last is None or now - last >= RATE_S):
        msg = _message(new)
        last = now
        for it in new:
            seen[it["key"]] = now
    if len(seen) > KEYS_MAX:
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-KEYS_MAX:])
    out = {"v": 1, "seen": seen}
    if last is not None:
        out["last_alert"] = last
    return out, msg


def notify(message) -> bool:
    """One notification through osascript; the message is an argument, never script text."""
    if not isinstance(message, str) or not MESSAGE_RE.fullmatch(message):
        return False
    from . import safe_proc
    try:
        rc, _ = safe_proc.run_capped([OSASCRIPT, "-e", SCRIPT, message], NOTIFY_TIMEOUT_S, 4096)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    return rc == 0


def run(problems, now: float | None = None, path: Path | None = None, notifier=None):
    """Update the state and send at most one notification. Returns the message sent, else None."""
    if disabled():
        return None
    if not isinstance(problems, dict) or problems.get("error") or not isinstance(problems.get("items"), list):
        return None
    now = time.time() if now is None else now
    p = path or _state_path()
    state, msg = decide(load(p), problems["items"], now)
    if not save(state, p):
        return None                                   # no state, no rate limit: send nothing
    if msg:
        (notifier or notify)(msg)
    return msg
