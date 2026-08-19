"""Tests for the claim-grounding oracle (apex_router.codeqa.ground_claims).

The oracle takes an arbitrary finding/report text, pulls its file:line citations, resolves each to a
registered repo (via CODEQA_REPOS), and checks DETERMINISTICALLY whether that file+span exists in the
live tree. It is NOT a model call — the authoritative verdict is a file existence + line-count check.

Repos are set up under a temp CODEQA_REPOS dir and the retriever/ground modules are reloaded so
REPOS_DIR picks up the override (matching the pattern in test_codeqa_doctor.py).
"""
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path


def _write_repo(repos_dir: Path, name: str, files: dict[str, int]) -> Path:
    """Create a repo root with `files` ({relpath: n_lines}) and its config JSON under repos_dir."""
    root = repos_dir.parent / name
    for rel, n in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(f"line {i}" for i in range(1, n + 1)) + "\n")
    (repos_dir / f"{name}.json").write_text(json.dumps({
        "name": name, "root": str(root), "language": "python",
        "search_globs": ["**"], "code_exts": [".py"], "definition_patterns": [],
    }))
    return root


class GroundOracleTest(unittest.TestCase):
    def _ground(self, text, repos):
        """Set CODEQA_REPOS to `repos`, reload the modules, and ground `text`. Returns the verdict."""
        os.environ["CODEQA_REPOS"] = str(repos)
        import apex_router.codeqa.retriever as rmod
        import apex_router.codeqa.ground_claims as gmod
        importlib.reload(rmod)
        gmod = importlib.reload(gmod)
        return gmod.ground_text(text)

    def tearDown(self):
        os.environ.pop("CODEQA_REPOS", None)
        import apex_router.codeqa.retriever as rmod
        import apex_router.codeqa.ground_claims as gmod
        importlib.reload(rmod)
        importlib.reload(gmod)

    def test_grounded_when_file_and_span_exist(self):
        with tempfile.TemporaryDirectory() as d:
            repos = Path(d) / "repos"; repos.mkdir()
            _write_repo(repos, "repo_a", {"repo_a/mod.py": 100})
            v = self._ground("The bug is at repo_a/mod.py:50-66.", repos)
            self.assertEqual(len(v.citations), 1)
            self.assertEqual(v.citations[0].verdict, "grounded")
            self.assertTrue(v.has_grounded)
            self.assertFalse(v.has_problem)

    def test_stale_when_span_past_eof_is_a_real_problem(self):
        with tempfile.TemporaryDirectory() as d:
            repos = Path(d) / "repos"; repos.mkdir()
            _write_repo(repos, "repo_a", {"repo_a/mod.py": 30})
            v = self._ground("The check is at repo_a/mod.py:900.", repos)
            self.assertEqual(v.citations[0].verdict, "stale")
            self.assertTrue(v.has_problem)   # stale is the authoritative, rejectable defect

    def test_name_owned_absent_is_unverified_not_hallucinated(self):
        # A name-owned but absent file is 'unverified' (advisory), never 'hallucinated' — a
        # deterministic oracle can't tell an invented path from a real file in an unregistered
        # sibling repo. has_problem stays False so the loop never rejects on it.
        with tempfile.TemporaryDirectory() as d:
            repos = Path(d) / "repos"; repos.mkdir()
            _write_repo(repos, "repo_a", {"repo_a/mod.py": 100})
            v = self._ground("See repo_a/nonexistent.py:12 for the handler.", repos)
            self.assertEqual(v.citations[0].verdict, "unverified")
            self.assertFalse(v.has_problem)

    def test_not_applicable_when_no_registered_citations(self):
        with tempfile.TemporaryDirectory() as d:
            repos = Path(d) / "repos"; repos.mkdir()
            _write_repo(repos, "repo_a", {"repo_a/mod.py": 100})
            v = self._ground("Look at unknown/foo.py:5 and just prose.", repos)
            self.assertFalse(v.applicable)
            self.assertEqual(v.citations, [])
            self.assertFalse(v.has_problem)

    def test_no_citations_at_all_is_not_applicable(self):
        with tempfile.TemporaryDirectory() as d:
            repos = Path(d) / "repos"; repos.mkdir()
            _write_repo(repos, "repo_a", {"repo_a/mod.py": 100})
            v = self._ground("Pure prose about a race condition.", repos)
            self.assertFalse(v.applicable)
            self.assertFalse(v.has_problem)

    def test_foreign_suffix_does_not_ground_to_wrong_repo(self):
        # A bare suffix that exists in one repo must NOT ground a citation whose leading segment
        # names a different (or no) repo.
        with tempfile.TemporaryDirectory() as d:
            repos = Path(d) / "repos"; repos.mkdir()
            _write_repo(repos, "repo_a", {"repo_a/mod.py": 100})
            v = self._ground("See unknown/mod.py:1 for the logic.", repos)
            self.assertFalse(v.applicable)
            self.assertFalse(v.has_problem)

    def test_absolute_and_dotdot_paths_are_dropped(self):
        # Absolute paths and '..' traversal must never resolve inside a repo (containment).
        with tempfile.TemporaryDirectory() as d:
            repos = Path(d) / "repos"; repos.mkdir()
            root = _write_repo(repos, "repo_a", {"repo_a/mod.py": 100})
            v = self._ground(f"Look at {root}/repo_a/mod.py:1 and ../repo_a/mod.py:1", repos)
            self.assertFalse(v.applicable)
            self.assertFalse(v.has_problem)

    def test_real_file_via_repo_name_prefix_grounds(self):
        # A real file cited with the repo-name prefix must ground (nested <root>/<name>/ layout).
        with tempfile.TemporaryDirectory() as d:
            repos = Path(d) / "repos"; repos.mkdir()
            _write_repo(repos, "repo_a", {"repo_a/mod.py": 100})
            v = self._ground("repo_a/mod.py:50 loads the config.", repos)
            self.assertEqual(v.citations[0].verdict, "grounded")


if __name__ == "__main__":
    unittest.main()
