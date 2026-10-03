"""Single resolver for the proxy telemetry file, shared by every offline reader.

The proxy writes to ``$APEX_HOME/telemetry.jsonl`` (proxy_engine/config.py). Readers used to
disagree (route_join read APEX_TELEMETRY, pressure read APEX_HOME), so a user with APEX_HOME set
silently joined nothing. Precedence: APEX_TELEMETRY (explicit file) > APEX_HOME/telemetry.jsonl
> ~/.apex/telemetry.jsonl.
"""
from __future__ import annotations

import os
from pathlib import Path


def telemetry_path(env=None) -> Path:
    e = os.environ if env is None else env
    explicit = e.get("APEX_TELEMETRY")
    if explicit:
        return Path(explicit).expanduser()
    home = e.get("APEX_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".apex"
    return base / "telemetry.jsonl"
