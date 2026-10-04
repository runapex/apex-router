"""inference_lock must be bounded: a second caller waits at most ORNITH_LOCK_TIMEOUT_SECS, then
raises OrnithBusy (an OrnithUnavailable) so lanes escalate instead of hanging a Claude Code turn."""
import fcntl
import sys
import threading
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import apex_router.ornith.ornith_client as oc  # noqa: E402


class _Holder:
    """Hold oc.LOCK exclusively from another thread (own fd → distinct flock owner)."""
    def __init__(self):
        self.held = threading.Event()
        self.release = threading.Event()

    def run(self):
        oc.LOCK.parent.mkdir(parents=True, exist_ok=True)
        with oc.LOCK.open("a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            self.held.set()
            self.release.wait(5)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class TestBoundedLock(unittest.TestCase):
    def setUp(self):
        self._orig = oc.LOCK
        self._orig_to = oc.LOCK_TIMEOUT
        oc.LOCK_TIMEOUT = 0.2
        oc.LOCK = Path(self.id().replace(".", "_") + ".lock").resolve()
        self.h = _Holder()
        self.t = threading.Thread(target=self.h.run, daemon=True)
        self.t.start()
        self.assertTrue(self.h.held.wait(2))

    def tearDown(self):
        self.h.release.set()
        self.t.join(5)
        oc.LOCK.unlink(missing_ok=True)
        oc.LOCK = self._orig
        oc.LOCK_TIMEOUT = self._orig_to

    def test_busy_raises_within_timeout(self):
        t0 = time.monotonic()
        with self.assertRaises(oc.OrnithBusy):
            with oc.inference_lock(timeout_s=0.3):
                pass
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_busy_is_unavailable_subclass(self):
        self.assertTrue(issubclass(oc.OrnithBusy, oc.OrnithUnavailable))

    def test_zero_timeout_is_one_nonblocking_attempt(self):
        t0 = time.monotonic()
        with self.assertRaises(oc.OrnithBusy):
            with oc.inference_lock(timeout_s=0):
                pass
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_acquires_after_holder_releases(self):
        def free_soon():
            time.sleep(0.2); self.h.release.set()
        threading.Thread(target=free_soon, daemon=True).start()
        with oc.inference_lock(timeout_s=3):
            acquired = True
        self.assertTrue(acquired)

    def test_chat_messages_surfaces_busy(self):
        orig_post = oc._post
        oc._post = lambda *a, **k: self.fail("must not POST while busy")
        try:
            with self.assertRaises(oc.OrnithBusy):
                oc.chat_messages([{"role": "user", "content": "x"}], max_tokens=1)
        finally:
            oc._post = orig_post

    def test_default_timeout_env(self):
        self.assertEqual(oc._env_num("ORNITH_LOCK_TIMEOUT_SECS", "120", float), self._orig_to)


if __name__ == "__main__":
    unittest.main()
