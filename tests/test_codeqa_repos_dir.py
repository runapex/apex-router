"""codeqa REPOS_DIR is config-driven — the packaged engine can read repo configs from an EXTERNAL
directory (CODEQA_REPOS), so a machine can run the public codeqa engine over PRIVATE repo configs
kept outside the repo. Without this, the packaged engine only sees the package's own `repos/`.
"""
import importlib
import os
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


class TestReposDir(unittest.TestCase):
    def _reload(self):
        # REPOS_DIR is resolved at import from the env — reload to observe the env override
        import apex_router.codeqa.retriever as r
        return importlib.reload(r)

    def tearDown(self):
        os.environ.pop("CODEQA_REPOS", None)
        self._reload()  # restore default for other tests

    def test_default_is_package_repos(self):
        os.environ.pop("CODEQA_REPOS", None)
        r = self._reload()
        self.assertEqual(r.REPOS_DIR.name, "repos")
        self.assertIn("codeqa", str(r.REPOS_DIR))  # inside the package

    def test_env_override_points_elsewhere(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.environ["CODEQA_REPOS"] = d
            r = self._reload()
            self.assertEqual(r.REPOS_DIR, Path(d))

    def test_env_override_expands_user(self):
        os.environ["CODEQA_REPOS"] = "~/some/repos"
        r = self._reload()
        self.assertEqual(r.REPOS_DIR, Path.home() / "some" / "repos")


if __name__ == "__main__":
    unittest.main()
