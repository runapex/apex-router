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


# Rotated generations: `telemetry.jsonl.<N>`, append-only, HIGHER N = NEWER (proxy_engine/telemetry/events.py).
# Nothing is renumbered, so a file name, once seen, always means the same data.


def telemetry_files(path, generations=None) -> list:
    """The live file plus its rotated generations that exist, OLDEST FIRST, live file last.

    Only exact `<name>.<int>` siblings count (not `.pre-v2-bak`, `.1.tmp`, ...). `generations` caps how many
    rotated files are included, NEWEST first (None = all; 1 = only the newest). Readers default to 1 so a window
    right after a rotation still spans the previous file, without parsing every archived generation.
    """
    import re
    p = Path(path)
    pat = re.compile(re.escape(p.name) + r"\.(\d+)$")
    gens = sorted((int(m.group(1)), q) for q in (p.parent.glob(p.name + ".*") if p.parent.is_dir() else [])
                  if (m := pat.fullmatch(q.name)) and q.is_file())
    if generations is not None:
        gens = gens[len(gens) - max(0, int(generations)):] if int(generations) > 0 else []
    out = [q for _, q in gens]
    if p.is_file():
        out.append(p)
    return out


def iter_telemetry_lines(path, generations=1):
    """Raw lines of the newest `generations` rotated files plus the live file, oldest first.

    Snapshot-safe against a concurrent rotation: the LIVE file is opened first, then the rotated files are
    listed and opened, skipping any that is the same inode as the live handle (it was rotated in between, and
    is already being read through that handle). Missing/unreadable files are skipped."""
    import os
    p = Path(path)
    live = None
    handles = []
    try:
        try:
            live = open(p, encoding="utf-8", errors="replace")
            live_id = os.fstat(live.fileno())[1:3]  # (st_ino, st_dev)
        except OSError:
            live, live_id = None, None
        rotated = [f for f in telemetry_files(p, None) if f != p]
        picked = []
        for f in reversed(rotated):  # newest first
            if len(picked) >= max(0, int(generations if generations is not None else len(rotated))):
                break
            try:
                fh = open(f, encoding="utf-8", errors="replace")
            except OSError:
                continue
            if live_id is not None and os.fstat(fh.fileno())[1:3] == live_id:
                fh.close()
                continue
            picked.append(fh)
        handles = list(reversed(picked)) + ([live] if live is not None else [])
        for fh in handles:
            yield from fh
    finally:
        for fh in handles or ([live] if live is not None else []):
            fh.close()
