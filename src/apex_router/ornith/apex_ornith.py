"""apex-ornith — live Ornith review + verified code-gen in the Apex dev loop.

Two subcommands over the measured Ornith primitives (`model_router`, `ornith_batch`,
`ornith_code`):

  apex-ornith review [--staged | --base REF]   advisory diff auditor — ALWAYS exits 0
  apex-ornith gen "SPEC" --test T [--out F]     spec->function offload — emits ONLY verified code

SAFETY INVARIANT — Ornith output is NEVER trusted:
  - `review` never blocks ON FINDINGS: it prints findings under an UNVERIFIED banner and exits 0
    regardless of what it found (measured 1/5 precision — a human/Opus triages). It exits
    non-zero only on GATE failures (server down = 3, router decline = 4), never because it
    flagged something.
  - `gen` hard-refuses without --test and emits code ONLY after the draft clears pre-execution
    guards (no module-scope side-effects; a meaningful test) AND passes `ruff` + `pytest` (the
    repo's own bar). A failing draft is discarded, its temp dir cleaned; escalate to Opus.
    Note: verification runs the draft via pytest — the guards block obvious module-scope hazards
    but this is NOT a security sandbox (Ornith is low-trust, not adversarial).

`run()` takes injectable seams (generate_fn/review_fn/diff_fn/liveness_fn/select_fn/emit) so the
safety logic is unit-testable offline with fakes; `main()` wires the real implementations.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

import os

_ML = Path(__file__).resolve().parents[1]
# The verify gate needs pytest+ruff. Prefer an explicit APEX_ORNITH_VERIFY_PY (a venv that
# has them), else fall back to the current interpreter. No machine-specific path is baked in.
_VERIFY_PY = os.environ.get("APEX_ORNITH_VERIFY_PY") or sys.executable
if str(_ML) not in sys.path:
    sys.path.insert(0, str(_ML))
if str(_ML / "ornith") not in sys.path:
    sys.path.insert(0, str(_ML / "ornith"))

REVIEW_PREAMBLE = (
    "You are an adversarial code reviewer auditing a git diff. Find REAL correctness defects: "
    "hot-path crashes, unhandled edge cases, fail-open/contract violations, cache-safety bugs. "
    "Cite the exact function/line. Be specific, not speculative. List each as "
    "'DEFECT <n>: <function> — <one-sentence failure>'. If none, reply exactly: NO DEFECTS FOUND."
)
_ADVISORY = ("Ornith review — UNVERIFIED pre-filter (measured ~1/5 precision). "
             "Triage each finding before trusting; this NEVER blocks a commit.")

# Exit codes: 0 ok/advisory, 1 gen verification failed, 2 gen misuse (no --test),
# 3 server unavailable, 4 router declined.
EXIT_OK, EXIT_VERIFY_FAIL, EXIT_MISUSE, EXIT_UNAVAILABLE, EXIT_DECLINED = 0, 1, 2, 3, 4


# ── real seams (wired by main; overridable in tests) ───────────────────────────
def _real_liveness() -> bool:
    from . import ornith_client as oc
    try:
        return oc.liveness()
    except Exception:
        return False


def _real_select(**kw):
    from . import model_router
    return model_router.select(**kw)


def _real_review(diff: str, *, budget: int) -> str:
    from .ornith_batch import batch_over_preamble
    r = batch_over_preamble(REVIEW_PREAMBLE, [f"Review this diff:\n\n{diff}"],
                            max_tokens=budget, enable_thinking=True, temperature=0.0)
    return r[0].answer


def _real_generate(spec: str) -> str:
    from .ornith_code import generate_code
    return generate_code(spec, enable_thinking=False)


def _git_diff(staged: bool, base: str | None) -> str:
    cmd = ["git", "diff", "--staged"] if staged and not base else \
          (["git", "diff", f"{base}...HEAD"] if base else ["git", "diff", "--staged"])
    return subprocess.run(cmd, capture_output=True, text=True).stdout


# Module-level constructs that execute arbitrary effects at IMPORT time (before any test verdict).
# pytest imports the draft, so these run unsandboxed during verification — refuse before executing.
# NOTE: this is a GUARD, not a sandbox (Codex xval #1). It blocks the obvious module-scope hazards;
# it does not contain a determined adversary. Ornith output is low-trust, not hostile — but the
# guard means a fluke `os.system(...)` at module scope is refused rather than run.
_DANGER = re.compile(
    r"^\s*(?:import\s+(?:os|subprocess|shutil|socket|sys)\b"
    r"|from\s+(?:os|subprocess|shutil|socket|sys)\s+import"
    r"|__import__\s*\(|open\s*\([^)]*['\"][wax])",
    re.M,
)


def _looks_dangerous(code: str) -> str | None:
    """Return a reason string if the draft has a module-scope side-effect hazard, else None.
    Only the module (top) level matters — indented lines (inside functions) run at call time,
    under the tests, not at import. So we scan unindented statements only."""
    top = "\n".join(ln for ln in code.splitlines() if ln[:1] not in (" ", "\t", ""))
    m = _DANGER.search(top)
    return f"module-level side-effect hazard: {m.group(0).strip()!r}" if m else None


def _test_is_meaningful(test_src: str, module_name: str) -> str | None:
    """Return a reason if the test won't meaningfully verify the module, else None. Guards the
    vacuous-pass hole (Codex xval #4): the test must import the module under test AND contain a
    test_* function that asserts."""
    if module_name not in test_src:
        return f"test file does not reference the module under test ({module_name!r})"
    if not re.search(r"def\s+test_\w*\s*\(", test_src):
        return "test file has no test_* function"
    if "assert" not in test_src:
        return "test file contains no assertions"
    return None


# ── verify gate for gen ────────────────────────────────────────────────────────
def _verify(code: str, test_file: str, module_name: str, emit: Callable[[str], None],
            *, require_lint: bool = True, timeout_s: float = 30.0) -> bool:
    """Write `code` as <module_name>.py in a temp dir, run ruff + pytest(test_file) against it.
    Returns True iff both pass AND the draft/test clear the pre-execution guards. The temp dir is
    always cleaned up. The test file is expected to `import <module_name>`."""
    if not re.fullmatch(r"[a-zA-Z_]\w*", module_name):        # #5: no path traversal via module-name
        emit(f"invalid --module-name {module_name!r}: must be a bare python identifier")
        return False
    danger = _looks_dangerous(code)                            # #1: refuse module-scope hazards
    if danger:
        emit(f"refusing to verify — {danger}")
        return False
    tsrc = Path(test_file).read_text()
    bad_test = _test_is_meaningful(tsrc, module_name)          # #4: reject vacuous tests
    if bad_test:
        emit(f"test file rejected — {bad_test}")
        return False

    d = Path(tempfile.mkdtemp(prefix="apex_ornith_gen_"))
    try:
        (d / f"{module_name}.py").write_text(code)
        (d / "t_verify.py").write_text(tsrc)
        py = _VERIFY_PY
        ruff = subprocess.run([py, "-m", "ruff", "check", str(d / f"{module_name}.py")],
                              capture_output=True, text=True, timeout=timeout_s)
        if ruff.returncode != 0:
            missing = "No module named ruff" in (ruff.stderr or "")
            if missing and not require_lint:                  # #3: ruff required unless opted out
                emit("warning: ruff unavailable and --no-lint set — skipping lint.")
            elif missing:
                emit("ruff unavailable — lint is required (pass --no-lint to skip). Failing.")
                return False
            else:
                emit("ruff failed:\n" + (ruff.stdout or ruff.stderr))
                return False
        try:
            pt = subprocess.run([py, "-m", "pytest", "-q", str(d / "t_verify.py")],
                                capture_output=True, text=True, cwd=str(d), timeout=timeout_s)
        except subprocess.TimeoutExpired:
            emit(f"pytest timed out after {timeout_s}s — failing.")
            return False
        if pt.returncode != 0:
            emit("pytest failed:\n" + pt.stdout[-1500:])
            return False
        return True
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)                  # #5: no lingering drafts


# ── the CLI core (pure w.r.t. its seams) ───────────────────────────────────────
def run(argv, *, generate_fn=None, review_fn=None, diff_fn=None,
        liveness_fn=None, select_fn=None, verify_fn=None,
        emit: Callable[[str], None] = print) -> int:
    generate_fn = generate_fn or _real_generate
    review_fn = review_fn or _real_review
    diff_fn = diff_fn or _git_diff
    liveness_fn = liveness_fn or _real_liveness
    select_fn = select_fn or _real_select
    # verify_fn(code, test_file, module_name, emit) -> bool. Default runs ruff+pytest in a
    # verifier interpreter (the apex venv has both); injectable so unit tests need neither.
    verify_fn = verify_fn or _verify

    p = argparse.ArgumentParser(prog="apex-ornith")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("review", help="advisory diff auditor (never blocks)")
    pr.add_argument("--staged", action="store_true", default=True)
    pr.add_argument("--base", default=None)
    pr.add_argument("--budget", type=int, default=6000)
    pg = sub.add_parser("gen", help="spec->function offload (emits only verified code)")
    pg.add_argument("spec")
    pg.add_argument("--test", default=None)
    pg.add_argument("--out", default=None)
    pg.add_argument("--module-name", default="gen_mod")
    pg.add_argument("--no-lint", action="store_true",
                    help="skip ruff if it's unavailable (pytest still gates)")
    args = p.parse_args(argv)

    if not liveness_fn():
        emit("Ornith unavailable on :8080 — use Opus.")
        return EXIT_UNAVAILABLE

    if args.cmd == "review":
        diff = diff_fn(args.staged, args.base)
        if not diff.strip():
            emit("nothing staged to review.")
            return EXIT_OK
        route = select_fn(task="review", items=1, item_bytes=len(diff.encode()))
        if not route.fits:
            emit(f"declined: {route.reason}")
            return EXIT_DECLINED
        emit(_ADVISORY)
        emit(review_fn(diff, budget=args.budget))
        return EXIT_OK

    # gen
    if not args.test:
        emit("gen needs a --test file to verify — it never emits unverified code.")
        return EXIT_MISUSE
    route = select_fn(task="code", items=1, item_bytes=len(args.spec.encode()))
    if not route.fits:
        emit(f"declined: {route.reason}")
        return EXIT_DECLINED
    code = generate_fn(args.spec)
    ok = verify_fn(code, args.test, args.module_name, emit, require_lint=not args.no_lint) \
        if verify_fn is _verify else verify_fn(code, args.test, args.module_name, emit)
    if not ok:
        emit("Ornith draft failed verification → escalate to Opus.")
        return EXIT_VERIFY_FAIL
    if args.out:
        Path(args.out).write_text(code)
        emit(f"verified → wrote {args.out}")
    else:
        emit(code)
    return EXIT_OK


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
