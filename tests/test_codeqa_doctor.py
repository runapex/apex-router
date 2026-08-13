"""codeqa `doctor` — post-install validation of registered repos.

For each config in CODEQA_REPOS it reports a health row: does the config parse, does the
root exist, is a digest configured+present, do the search globs actually match code files.
`--check` exits nonzero if any repo is unhealthy (root missing / config error) so it can gate
an install script or CI. Pure `repo_health()` is the testable core; the CLI just formats it.
"""
import json
import os
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.codeqa import doctor  # noqa: E402


class TestRepoHealth(unittest.TestCase):
    def _write_repo(self, repos_dir, name, root, *, digest=None, globs=None, exts=None):
        cfg = {"name": name, "root": str(root), "language": "python",
               "search_globs": globs if globs is not None else ["**"],
               "code_exts": exts if exts is not None else [".py"]}
        if digest is not None:
            cfg["digest"] = str(digest)
        (repos_dir / f"{name}.json").write_text(json.dumps(cfg))

    def test_healthy_repo_reports_ok(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            repos = d / "repos"; repos.mkdir()
            root = d / "myrepo"; root.mkdir()
            (root / "main.py").write_text("def f(): pass\n")
            self._write_repo(repos, "myrepo", root)
            rows = doctor.repo_health(repos_dir=repos)
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual(r["name"], "myrepo")
            self.assertTrue(r["ok"])
            self.assertTrue(r["root_exists"])
            self.assertGreater(r["code_files"], 0)

    def test_missing_root_is_unhealthy(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            repos = d / "repos"; repos.mkdir()
            self._write_repo(repos, "gone", d / "does_not_exist")
            r = doctor.repo_health(repos_dir=repos)[0]
            self.assertFalse(r["ok"])
            self.assertFalse(r["root_exists"])

    def test_missing_digest_flagged_but_not_fatal(self):
        # A missing/absent digest is a WARNING (codeqa degrades cleanly), not unhealthy.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            repos = d / "repos"; repos.mkdir()
            root = d / "r"; root.mkdir(); (root / "a.py").write_text("x=1\n")
            self._write_repo(repos, "r", root, digest=d / "nope.md")
            r = doctor.repo_health(repos_dir=repos)[0]
            self.assertTrue(r["ok"])            # still healthy
            self.assertFalse(r["digest_ok"])    # but digest flagged

    def test_globs_matching_no_code_is_warned(self):
        # Root exists but the globs match no code file → warn (retrieval will find nothing).
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            repos = d / "repos"; repos.mkdir()
            root = d / "empty"; root.mkdir()   # no .py files
            self._write_repo(repos, "empty", root)
            r = doctor.repo_health(repos_dir=repos)[0]
            self.assertTrue(r["root_exists"])
            self.assertEqual(r["code_files"], 0)
            self.assertFalse(r["ok"])           # no code to answer over = not usable

    def test_any_unhealthy_makes_all_healthy_false(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            repos = d / "repos"; repos.mkdir()
            good = d / "g"; good.mkdir(); (good / "a.py").write_text("x=1\n")
            self._write_repo(repos, "good", good)
            self._write_repo(repos, "bad", d / "missing")
            self.assertFalse(doctor.all_healthy(doctor.repo_health(repos_dir=repos)))


if __name__ == "__main__":
    unittest.main()
