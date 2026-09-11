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
import time
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
    exit when stale so the supervisor restarts with fresh code.

    DEBOUNCE (`settle_s`): a daemon whose watched tree is ALSO the operator's live edit checkout
    would exit on the FIRST byte of a multi-save edit, then launchd relaunches it into the next
    save — an observable restart-thrash loop (see drain_worker.log). With `settle_s > 0`, a change
    is only reported stale once the SAME changed fingerprint has persisted unchanged for `settle_s`
    seconds — i.e. the operator stopped typing. `settle_s=0.0` (default) preserves the original
    fire-immediately behavior and every existing test. The guard still catches a landed bugfix; it
    just waits for the edit burst to settle first.
    """

    def __init__(self, root: Path, settle_s: float = 0.0):
        self.root = Path(root)
        self.baseline = fingerprint(self.root)
        self.settle_s = max(0.0, float(settle_s))
        # Debounce bookkeeping: the last observed CHANGED fingerprint and when we first saw it.
        self._pending_fp: str | None = None
        self._pending_since: float = 0.0

    def is_stale(self) -> bool:
        current = fingerprint(self.root)
        if current == self.baseline:
            self._pending_fp = None  # reverted to baseline (edit undone / temp file gone)
            return False
        if self.settle_s <= 0.0:
            return True  # original behavior: any change is immediately stale
        now = time.monotonic()
        if current != self._pending_fp:
            # A new (or first) changed state — start/restart the settle timer. A still-moving
            # edit burst keeps resetting this, so we never exit mid-save.
            self._pending_fp = current
            self._pending_since = now
            return False
        # Same changed fingerprint as last check — stale only once it has held for settle_s.
        return (now - self._pending_since) >= self.settle_s
