"""Source version-guard for long-lived daemons.

A daemon that runs for hours will keep executing the code it loaded at startup — even after a
bugfix lands on disk — until something restarts it. That silent-stale-code failure is real (a
truncation fix sat unused for ~19h while the daemon ran old code and kept hitting the bug).

The guard fingerprints the daemon's own source tree at startup; the daemon calls `is_stale()` each
poll and exits cleanly when the fingerprint changes, so its supervisor (launchd KeepAlive / systemd
Restart=always) relaunches a fresh process on the new code. Pure stdlib.

Fingerprint = a hash of the CONTENT of every `*.py` under the tree (excluding `__pycache__`).
Content-hashing, not size+mtime: a one-line bugfix that changes a line without changing the file
size — applied within the same clock second — would slip past an mtime/size heuristic (exactly the
kind of fix that was missed). Reading the source bytes is cheap for a code tree and unambiguous. A
read error (file vanished mid-scan) degrades to skipping that file rather than raising — a guard must
never crash the daemon it protects.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def fingerprint(root: Path) -> str:
    """A stable hash of the CONTENT of every `*.py` file under `root` (excluding `__pycache__`).
    Never raises: a missing/unreadable tree yields a well-defined (possibly empty-input) hash."""
    h = hashlib.sha256()
    try:
        files = sorted(Path(root).rglob("*.py"))
    except OSError:
        return h.hexdigest()
    for f in files:
        if "__pycache__" in f.parts:
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue  # file vanished mid-scan — skip, don't crash
        h.update(str(f).encode())
        h.update(b"\0")
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()


class Guard:
    """Snapshots a source tree's fingerprint at construction; `is_stale()` reports whether the code
    on disk has changed since. Intended use: construct at daemon startup, check each loop iteration,
    exit when stale so the supervisor restarts with fresh code."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.baseline = fingerprint(self.root)

    def is_stale(self) -> bool:
        return fingerprint(self.root) != self.baseline
