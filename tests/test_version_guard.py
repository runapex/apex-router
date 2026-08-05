"""Source version-guard — a long-lived daemon must refuse to run stale code.

The daemon fingerprints its source files at startup; when any changes on disk, the fingerprint
differs and the daemon exits so its supervisor (launchd KeepAlive / systemd Restart) relaunches a
fresh process. This is the fix for a daemon that silently ran ~19h-old code after a bugfix landed.
"""
import sys
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.ornith import version_guard  # noqa: E402


class TestFingerprint(unittest.TestCase):
    def test_stable_when_nothing_changes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "a.py").write_text("x = 1\n")
            (p / "b.py").write_text("y = 2\n")
            fp1 = version_guard.fingerprint(p)
            fp2 = version_guard.fingerprint(p)
            self.assertEqual(fp1, fp2)
            self.assertTrue(fp1)  # non-empty

    def test_changes_when_a_file_content_changes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            f = p / "a.py"
            f.write_text("x = 1\n")
            fp1 = version_guard.fingerprint(p)
            time.sleep(0.01)
            f.write_text("x = 2\n")  # edited
            self.assertNotEqual(version_guard.fingerprint(p), fp1)

    def test_changes_when_a_file_is_added(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "a.py").write_text("x = 1\n")
            fp1 = version_guard.fingerprint(p)
            (p / "c.py").write_text("z = 3\n")  # new module
            self.assertNotEqual(version_guard.fingerprint(p), fp1)

    def test_ignores_non_python_and_pycache(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "a.py").write_text("x = 1\n")
            fp1 = version_guard.fingerprint(p)
            (p / "notes.txt").write_text("irrelevant")           # non-.py
            (p / "__pycache__").mkdir()
            (p / "__pycache__" / "a.pyc").write_text("bytecode")  # compiled
            self.assertEqual(version_guard.fingerprint(p), fp1)   # unchanged


class TestGuard(unittest.TestCase):
    def test_is_stale_false_then_true_after_edit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            f = p / "a.py"
            f.write_text("x = 1\n")
            g = version_guard.Guard(p)          # snapshots at construction
            self.assertFalse(g.is_stale())      # nothing changed yet
            time.sleep(0.01)
            f.write_text("x = 999\n")
            self.assertTrue(g.is_stale())       # code changed on disk

    def test_guard_survives_transient_read_error(self):
        # a file deleted mid-scan must not raise — a stale check failure should never crash the daemon
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "a.py").write_text("x = 1\n")
            g = version_guard.Guard(p)
            # fingerprint of a now-missing dir must not raise
            missing = p / "gone"
            try:
                version_guard.fingerprint(missing)
            except Exception as e:  # noqa: BLE001
                self.fail(f"fingerprint of missing dir raised {e!r}")


if __name__ == "__main__":
    unittest.main()
