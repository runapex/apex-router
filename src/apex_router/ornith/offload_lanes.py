"""Offload lanes — route work to the local model, gate on correctness, escalate misses.

Each lane returns a LaneResult carrying the local output, a pass/fail verdict, whether it must be
escalated to the frontier, and the usage tokens — everything offload_telemetry.OffloadRecord needs.

Design constraints, all measured (see the model-routing skill + apex telemetry analysis):
  - Local codegen: thinking-OFF only (thinking-ON hangs 0/3, burns the budget in <think>).
  - Codegen is a WIN only when the generated code passes the caller's tests. So the codegen lane
    RUNS THE TESTS and escalates on any failure. A wrong local answer saves nothing.
  - Review is a good-recall / poor-precision (1/5) PRE-FILTER: its findings are always escalated
    for Opus/Codex triage, never acted on autonomously.
  - The server is single-GPU and serialized by ornith_client's file-lock — lanes are batch/async,
    never on an interactive path.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LaneResult:
    lane: str
    ok: bool                 # passed the lane's own gate (meaningful only when gated=True)
    escalate: bool           # hand to the frontier
    output: str              # local model output (code, review text, summary…)
    usage: dict | None = None
    detail: str = ""         # gate detail (test output, escalation reason)
    gated: bool = False      # did a real correctness gate run? (codegen: tests; review: no)
    truncated: bool = False  # was local output cut off (finish_reason=length)? findings NOT exhaustive
    _extra: dict = field(default_factory=dict)


def run_python_tests(code: str, tests: str, *, timeout_s: int = 30) -> tuple[bool, str]:
    """Run caller-supplied `tests` against generated `code` in an isolated subprocess.

    The CALLER's tests are the only source of truth: only `test_*` names defined in `tests` are
    executed (cross-validation). `code` runs first to populate the shared namespace; if it
    raises ANYTHING — including `SystemExit`/`raise SystemExit(0)`, a BaseException that a bare
    `except Exception` would miss and let exit 0 through — that is a failure, not a pass.

    Returns (passed, detail). Any exception, assertion failure, syntax error, empty caller suite, or
    timeout -> (False, detail). Never raises — the gate turns every failure mode into a clean False
    so the caller escalates instead of crashing. On timeout the whole process GROUP is killed so a
    grandchild the generated code spawned cannot outlive the gate (cross-validation).
    """
    # Success is decided by a PARENT-CONTROLLED SENTINEL, never the child's exit code (cross-validation): the harness writes "PASS" to argv[3] only when every caller test passed. `os._exit(0)` or
    # a replaced `sys.exit` in generated code exits 0 WITHOUT writing the sentinel -> the parent
    # reads no "PASS" and fails. Return code alone is not trusted.
    #
    # Caller tests always win over same-named code tests (cross-validation): before running the tests
    # we DELETE every `test_*` name the generated code defined, so a caller `test_add` is never
    # shadowed or dropped by a code `test_add`.
    harness = (
        "import sys\n"
        "sys.dont_write_bytecode = True\n"
        "sentinel = sys.argv[3]\n"
        "code_ns = {}\n"
        "with open(sys.argv[1]) as f: code = f.read()\n"
        "with open(sys.argv[2]) as f: tests = f.read()\n"
        "try:\n"
        "    exec(compile(code, '<gen>', 'exec'), code_ns)\n"
        "except BaseException as e:\n"
        "    print('CODE_ERROR:', repr(e)); sys.exit(1)\n"
        # strip the code's own test_* so only CALLER tests are ever collected/run.
        "for k in [k for k in code_ns if k.startswith('test_')]: del code_ns[k]\n"
        "try:\n"
        "    exec(compile(tests, '<tests>', 'exec'), code_ns)\n"
        "except BaseException as e:\n"
        "    print('TESTS_LOAD_ERROR:', repr(e)); sys.exit(1)\n"
        "fns = [v for k, v in code_ns.items() if k.startswith('test_') and callable(v)]\n"
        "if not fns:\n"
        "    print('NO_CALLER_TESTS'); sys.exit(1)\n"
        "failed = 0\n"
        "for fn in fns:\n"
        "    try:\n"
        "        fn()\n"
        "    except BaseException as e:\n"
        "        failed += 1; print('FAIL', fn.__name__, repr(e))\n"
        "if failed == 0:\n"
        "    open(sentinel, 'w').write('PASS')\n"   # only an all-pass run writes the sentinel
        "sys.exit(1 if failed else 0)\n"
    )
    proc = None
    try:
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d)
            (dp / "_gen.py").write_text(code)
            (dp / "_tests.py").write_text(tests)
            (dp / "_harness.py").write_text(harness)
            sentinel = dp / "_sentinel"
            proc = subprocess.Popen(
                [sys.executable, str(dp / "_harness.py"),
                 str(dp / "_gen.py"), str(dp / "_tests.py"), str(sentinel)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=str(dp), env={"PATH": os.environ.get("PATH", "")},
                start_new_session=True,  # own process group -> killpg reaches descendants on timeout
            )
            timed_out = False
            try:
                out, err = proc.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                # bounded drain: a detached grandchild holding the pipes must not hang us forever
                # (cross-validation). If it still won't close, abandon the pipes.
                try:
                    out, err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    out, err = "", ""
            if timed_out:
                return (False, f"timeout after {timeout_s}s")
            # SENTINEL is the source of truth, not returncode.
            passed = sentinel.exists() and sentinel.read_text() == "PASS"
        detail = ((out or "") + (err or "")).strip()
        return (passed, detail)
    except Exception as e:  # noqa: BLE001 — gate must never raise
        return (False, f"gate_error: {e!r}")


# --------------------------------------------------------------------------------------------------
# Live lanes — call the local model, gate, and return a LaneResult carrying usage for telemetry.
# These import ornith_client lazily so the pure gate above (and its tests) need no server.
# --------------------------------------------------------------------------------------------------

def codegen_lane(spec: str, tests: str, *, max_tokens: int = 1200,
                 timeout_s: int = 30) -> LaneResult:
    """Lane 2. Generate code locally (thinking-OFF), run the caller's tests, escalate on any fail.

    ok == tests passed == frontier work genuinely avoided. On failure the spec is escalated so the
    frontier produces the real answer — the local attempt cost only free local compute.
    """
    from . import ornith_client as oc
    from .ornith_code import extract_code

    prompt = (
        "Write python code for this task. Return ONLY the code in a ```python block, "
        f"no explanation.\n\nTASK:\n{spec}"
    )
    try:
        result = oc.chat_messages(
            [{"role": "user", "content": prompt}],
            max_tokens=max_tokens, enable_thinking=False, temperature=0.0,
        )
    except Exception as e:  # noqa: BLE001 — server/protocol error -> escalate, never crash the queue
        # local call never produced a gated verdict -> not gated, must escalate.
        return LaneResult("codegen", ok=False, escalate=True, output="",
                          usage=None, detail=f"local_call_failed: {e!r}", gated=False)

    code = extract_code(result.answer)
    passed, detail = run_python_tests(code, tests, timeout_s=timeout_s)
    # gated=True: the tests actually ran, so the ok verdict is earned and may count as work saved.
    return LaneResult(
        "codegen", ok=passed, escalate=not passed, output=code,
        usage=result.usage, detail=detail, gated=True,
    )


def review_lane(preamble: str, diff: str, *, max_tokens: int = 1024) -> LaneResult:
    """Lane 1. Local review PRE-FILTER. Findings ALWAYS escalate for frontier triage — measured
    precision is 1/5, so raw output is never acted on. `ok` here means "produced findings to triage"
    (recall is the value), not "verdict trusted". The escalation payload is the local findings so the
    frontier reviews a shortlist instead of the whole diff cold.

    max_tokens default 1024 (measured on the reference window): a real multi-bug diff review runs 160-260
    completion tokens; the old 512 cap truncated large-diff reviews (finish_reason=length ->
    OrnithProtocolError -> empty local output, escalated cold). A truncated review still escalates
    correctly, but yields zero local value, so the cap is sized for genuine diffs.
    """
    from . import ornith_client as oc

    try:
        result = oc.chat_messages(
            [{"role": "system", "content": preamble},
             {"role": "user", "content": diff}],
            max_tokens=max_tokens, enable_thinking=False, temperature=0.0,
            raise_on_truncation=False,   # a truncated review's partial findings still escalate usefully
        )
    except Exception as e:  # noqa: BLE001
        # any local failure (empty/server) still needs a frontier review — always escalate (Codex #10).
        return LaneResult("review", ok=False, escalate=True, output="",
                          usage=None, detail=f"local_call_failed: {e!r}", gated=False)

    findings = result.answer.strip()
    # treat an unknown/missing finish_reason as "not confirmed complete" (cross-validation):
    # only an explicit "stop" is a clean finish; anything else may be cut off.
    truncated = result.finish_reason != "stop"
    # gated=False: review is a recall pre-filter with 1/5 precision — no correctness gate runs, so
    # its tokens NEVER count as frontier work saved (it always escalates for triage).
    return LaneResult(
        "review", ok=bool(findings), escalate=True, output=findings, usage=result.usage,
        detail="prefilter: triage upstream (truncated)" if truncated else "prefilter: triage upstream",
        gated=False, truncated=truncated,
    )
