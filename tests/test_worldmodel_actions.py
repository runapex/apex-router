"""P6 E1 action classifier: validation set agreement, wrapper traps, test-output parsing, phases."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from apex_router.worldmodel import actions as A

VALIDATION = Path(__file__).parent / "fixtures" / "worldmodel" / "actions_validation.jsonl"


def _rows():
    return [json.loads(x) for x in VALIDATION.read_text().splitlines() if x.strip()]


def _predict(r):
    return A.classify(r["tool"], {"command": r["cmd"]} if r["tool"] in A.BASH_TOOLS else {})


def test_validation_set_agreement():
    """Owner-signed 2026-10-08 after two blind re-labels (DESIGN-worldmodel-P6.md §2): >= 95%."""
    rows = _rows()
    assert len(rows) == 306                 # 300 + the six review additions (2026-10-07)
    assert all(r["cls"] in A.CLASSES for r in rows)
    wrong = [(r["cls"], _predict(r), r["cmd"][:120]) for r in rows if _predict(r) != r["cls"]]
    agree = 1 - len(wrong) / len(rows)
    pairs = Counter((g, p) for g, p, _ in wrong)
    msg = (f"agreement {agree:.3f} ({len(wrong)} wrong); confusion (gold -> predicted): "
           f"{dict(pairs)}\n" + "\n".join(f"  {g} -> {p}: {c!r}" for g, p, c in wrong))
    assert agree >= 0.95, msg


def test_validation_set_holds_shapes_not_text():
    """Every argument is a placeholder: no literal home path, URL, address or e-mail."""
    bad = re.compile(r"/Users/|/home/|https?://|\b\d{1,3}(\.\d{1,3}){3}\b|@[A-Za-z0-9-]+\.")
    for r in _rows():
        assert not bad.search(r["cmd"]), r["cmd"]
    cover = " ".join(r["cmd"] for r in _rows())
    for trap in (".venv/bin/pytest", "python -m pytest", "uv run pytest", "python -m unittest",
                 "claude plugin test", "npm test", "make test", "go test", "cargo test", "jest",
                 "rspec", "timeout 60", "echo", "cd <path>", "<VAR>=<arg>", "| tail", "$P",
                 "/usr/bin/env", "/usr/bin/time", "bash <<EOF", "`git"):
        assert trap in cover, trap


@pytest.mark.parametrize("cmd,cls", [
    # the lesson's traps: prefixes and wrappers must not hide the test run
    ("cd repo && .venv/bin/pytest -q 2>&1 | tail -5", "test"),
    ("timeout 600 python3 -m pytest tests/x.py -x", "test"),
    ("echo '== run ==' ; PYTHONPATH=src .venv/bin/python -m pytest -q", "test"),
    ("X=1 Y=2 uv run --with hypothesis pytest -k foo", "test"),
    ("env CI=1 npm test", "test"),
    ("npm run test:unit -- --watch=false", "test"),
    ("pnpm test", "test"),
    ("make -C sub test", "test"),
    ("make check", "test"),
    ("go test ./... -run TestX", "test"),
    ("cargo test --release", "test"),
    ("npx jest src/a.test.ts", "test"),
    ("bundle exec rspec spec/a_spec.rb", "test"),
    ("(cd plugin && claude plugin test .) 2>&1 | tail -3", "test"),
    ("P=/x/.venv/bin/pytest; $P -q tests/", "test"),
    ("$PY -m pytest -q", "test"),
    ("bash -lc 'cd x && go test ./...'", "test"),
    ("time (A=1 uv run python -m unittest discover -s tests 2>&1 | tail -5)", "test"),
    ("t(){ .venv/bin/pytest -q \"$@\"; }; t tests/a.py", "test"),
    ("for i in $(seq 1 3); do pytest -q; done", "test"),
    # review 761c4e3: absolute-path prefixes, heredocs fed to a shell, backticks
    ("/usr/bin/env python -m pytest -q", "test"),
    ("/usr/bin/time pytest -x", "test"),
    ("/usr/bin/env FOO=1 /usr/bin/time -p .venv/bin/pytest", "test"),
    ("bash <<'EOF'\ncd x && .venv/bin/pytest -q\nEOF", "test"),
    ("ssh host bash -s <<EOF\ngo test ./...\nEOF", "test"),
    ("cat <<'EOF'\npytest is great\nEOF", "other"),
    ("echo \"built at `git rev-parse HEAD`\"", "vcs"),
    ("X=`date +%s`; ls", "read"),
    ("echo 'not `git status` in single quotes'", "other"),
    # not tests even though "pytest" appears
    ("ls .venv/bin/pytest", "read"),
    ("grep -rn pytest pyproject.toml", "search"),
    ("git commit -m \"$(cat <<'EOF'\nrun pytest before merge\nEOF\n)\"", "vcs"),
    ("python3 - <<'EOF'\nimport subprocess; subprocess.run(['pytest'])\nEOF", "run"),
    ("test -f x && echo yes", "other"),
    # the rest of the ladder
    ("make", "build"), ("cargo build", "build"), ("go vet ./...", "build"), ("ruff check src",
                                                                             "build"),
    ("uv pip install -e .", "build"), ("npm ci", "build"), ("python -m compileall -q src", "build"),
    ("uv pip list", "read"), ("uv tool list", "read"),
    ("git status --short", "vcs"), ("gh pr view 3", "vcs"), ("git -C x log --oneline", "vcs"),
    ("git grep -n foo", "search"),
    ("curl -s localhost:8788/health | jq .", "remote"), ("ssh host uptime", "remote"),
    ("rsync -a src/ host:/dst/", "remote"), ("rsync -a src/ dst/", "write"),
    ("openssl s_client -connect h:443", "remote"), ("openssl x509 -in c.pem -noout", "read"),
    ("python3 scripts/x.py", "run"), ("./run.sh", "run"), ("node -e 1", "run"),
    ("sed -i '' 's/a/b/' f.py", "edit"), ("perl -pi -e 's/a/b/' f", "edit"),
    ("cat > f.py <<'EOF'\nprint(1)\nEOF", "write"), ("echo x >> notes.md", "write"),
    ("mkdir -p a/b && cp x a/b/", "write"), ("python3 x.py > out.txt", "run"),
    ("rg -n foo src | head", "search"), ("find . -name '*.py' | xargs grep -l bar", "search"),
    ("cat a.py", "read"), ("sed -n 1,80p a.py", "read"), ("ls -la 2>/dev/null", "read"),
    ("echo hi", "other"), ("cd x", "other"), ("sleep 5; echo done", "other"), ("", "other"),
])
def test_bash_classes(cmd, cls):
    assert A.classify("bash", {"command": cmd}) == cls
    assert A.classify("Bash", {"command": cmd}) == cls


def test_tool_names():
    assert A.classify("Read") == "read" and A.classify("read") == "read"
    assert A.classify("Grep") == "search" and A.classify("Glob") == "search"
    assert A.classify("Edit") == "edit" and A.classify("edit") == "edit"
    assert A.classify("NotebookEdit") == "edit" and A.classify("MultiEdit") == "edit"
    assert A.classify("Write") == "write" and A.classify("write") == "write"
    for t in ("Agent", "Task", "Workflow"):
        assert A.classify(t) == "delegate" and A.is_spawn(t) == 1
    assert A.is_spawn("Bash") == 0 and A.is_spawn("SendMessage") == 0
    assert A.classify("AskUserQuestion") == "ask"
    assert A.classify("StructuredOutput") == "ask" and A.classify("SubagentHandback") == "ask"
    assert A.classify("Skill") == "plan" and A.classify("TodoWrite") == "plan"
    assert A.classify("WebFetch") == "remote" and A.classify("WebSearch") == "search"
    assert A.classify("mcp__claude-in-chrome__navigate") == "remote"
    assert A.classify("mcp__other__thing") == "other" and A.classify(None) == "other"
    assert A.classify("Bash", None) == "other"


@pytest.mark.parametrize("text,want", [
    ("....F\n=== 1 failed, 4 passed in 0.31s ===", (1, 4)),
    ("1858 passed, 4 skipped in 61.20s", (0, 1858)),
    ("2 failed, 10 passed, 1 error in 3.0s", (3, 10)),
    ("first run 9 passed in 1s\n...\n1 failed, 9 passed in 1.2s", (1, 9)),
    ("no tests ran in 0.01s", (0, 0)),
    ("Tests:       1 failed, 2 passed, 3 total\nTime: 1s", (1, 2)),
    (" Test Files  1 passed (1)\n      Tests  2 failed | 10 passed (12)\n", (2, 10)),
    ("test result: ok. 5 passed; 0 failed; 0 ignored\ntest result: FAILED. 2 passed; 1 failed; 0",
     (1, 7)),
    ("=== RUN TestA\n--- PASS: TestA (0.00s)\n--- FAIL: TestB (0.00s)\nFAIL", (1, 1)),
    ("ok  \tgithub.com/x/a\t0.01s\nFAIL\tgithub.com/x/b\t0.02s\n", (1, 1)),
    ("Finished in 0.1 seconds\n12 examples, 2 failures", (2, 10)),
    ("......\n------\nRan 6 tests in 0.001s\n\nOK", (0, 6)),
    ("Ran 6 tests in 0.001s\n\nFAILED (failures=1, errors=1)", (2, 4)),
    ("FAILED (errors=8)", (8, None)),
    ("Ran 257 tests across 17 files. [2.14s]\n 255 pass\n 2 fail", (2, 255)),
    ("  3 passing (20ms)\n  1 failing", (1, 3)),
    ("collected 0 items / grep filtered everything", None),
    # summary cut by tail/grep: per-test lines (pytest -v, bun) still count
    ("tests/a.py::test_x PASSED  [ 50%]\ntests/a.py::test_y FAILED  [100%]", (1, 1)),
    ("(pass) adds > two\n(pass) adds > three\n(fail) subtracts", (1, 2)),
    ("", None), (None, None),
])
def test_parse_tests(text, want):
    assert A.parse_tests(text) == want


def test_tests_field():
    assert A.tests_field("run", "3 passed in 1s") == {"ran": 0, "failed": None, "passed": None}
    assert A.tests_field("test", "3 passed in 1s") == {"ran": 1, "failed": 0, "passed": 3}
    assert A.tests_field("test", "garbage") == {"ran": 1, "failed": None, "passed": None}


def test_size_buckets():
    assert [A.size_bucket(n) for n in (0, 999, 1000, 9999, 10_000, 99_999, 100_000, 10**7)] == \
        [0, 0, 1, 1, 2, 2, 3, 3]


def test_phases():
    acts = ["read", "search", "edit", "read", "test", "run", "write", "build", "vcs", "ask"]
    assert A.phases(acts) == ["explore", "explore", "edit", "other", "verify", "verify", "edit",
                              "verify", "deliver", "deliver"]
    # no edit so far: tests are not "verify"; vcs/ask is "deliver" wherever it occurs (causal)
    assert A.phases(["vcs", "read", "test", "vcs", "ask"]) == \
        ["deliver", "explore", "other", "deliver", "deliver"]
    assert A.phases([]) == []
    assert set(A.phases(["delegate", "plan", "remote"])) == {"other"}


def test_phases_are_causal():
    """Appending later steps never changes an earlier step's phase (no lookahead)."""
    import random
    rng = random.Random(0)
    for _ in range(500):
        acts = [rng.choice(A.CLASSES) for _ in range(rng.randint(1, 30))]
        full = A.phases(acts)
        for k in range(1, len(acts) + 1):
            assert A.phases(acts[:k]) == full[:k], acts
