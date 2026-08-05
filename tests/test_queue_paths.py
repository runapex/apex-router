"""queue_root — the ONE place the job-queue location is decided.

The worker and every enqueuer must resolve the same queue dir, or jobs land where nothing drains
them. Making it config-driven (env var, stable default) lets a machine run the packaged code while
keeping its queue in a fixed spot — the fix for private/public path drift.
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.ornith import queue_paths  # noqa: E402


class TestQueueRoot(unittest.TestCase):
    def test_env_override_wins(self):
        root = queue_paths.queue_root(env={"APEX_ORNITH_QUEUE": "/tmp/my-queue"})
        self.assertEqual(root, Path("/tmp/my-queue"))

    def test_default_is_stable_home_path(self):
        root = queue_paths.queue_root(env={})
        # default must be a fixed, code-location-independent path under the user's home
        self.assertEqual(root, Path.home() / ".apex-router" / "queue")

    def test_expands_user_in_env(self):
        root = queue_paths.queue_root(env={"APEX_ORNITH_QUEUE": "~/somewhere/q"})
        self.assertEqual(root, Path.home() / "somewhere" / "q")

    def test_ignores_blank_env(self):
        root = queue_paths.queue_root(env={"APEX_ORNITH_QUEUE": "   "})
        self.assertEqual(root, Path.home() / ".apex-router" / "queue")


if __name__ == "__main__":
    unittest.main()
