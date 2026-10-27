"""Hidden oracle for bench/explore/speedup-lis. Reads the candidate on stdin; prints one JSON line.
The generator must never see this directory."""
# --- shared runner (copied into each oracle dir; each oracle dir is self-contained) -------------
import json
import os
import subprocess
import sys
import tempfile


# Model-written code runs here. On macOS, sandbox-exec denies network and any file write outside the
# temp dir; CPU seconds are capped everywhere. Without sandbox-exec (not macOS) the oracle fails closed
# unless EXPLORE_ALLOW_UNSANDBOXED=1.
SANDBOX = '(version 1)(allow default)(deny network*)(deny file-write*)' \
          '(allow file-write* (subpath (param "D")) (literal "/dev/null"))'


def sandboxed(d: str) -> list:
    if os.path.exists("/usr/bin/sandbox-exec"):
        return ["/usr/bin/sandbox-exec", "-D", "D=" + os.path.realpath(d), "-p", SANDBOX]
    if os.environ.get("EXPLORE_ALLOW_UNSANDBOXED") == "1":
        return []
    raise RuntimeError("no sandbox-exec; set EXPLORE_ALLOW_UNSANDBOXED=1 to run unsandboxed")


def _limits(timeout: float) -> None:
    import resource
    cpu = int(timeout) + 5
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))


def run_child(candidate: str, child_code: str, payload: dict, timeout: float) -> dict:
    """Run the candidate in a separate `python -I` process in an empty temp dir.
    The child gets the candidate path and the payload on stdin. It does not get the oracle path."""
    with tempfile.TemporaryDirectory(prefix="explore-cand-") as d:
        path = os.path.join(d, "candidate.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(candidate)
        payload = dict(payload, candidate_path=path)
        try:
            p = subprocess.run(sandboxed(d) + [sys.executable, "-I", "-c", child_code], input=json.dumps(payload),
                               capture_output=True, text=True, timeout=timeout, cwd=d,
                               env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0"},
                               preexec_fn=lambda: _limits(timeout))
        except subprocess.TimeoutExpired:
            return {"ok": False, "why": f"timeout after {timeout:g} s"}
        lines = [x for x in p.stdout.splitlines() if x.startswith("@@RESULT ")]
        if not lines:
            err = (p.stderr.strip().splitlines() or ["no output"])[-1][:160]
            return {"ok": False, "why": f"candidate crashed: {err}"}
        return json.loads(lines[-1][len("@@RESULT "):])


def emit(valid: bool, score: float, detail: str) -> None:
    print(json.dumps({"valid": bool(valid), "score": float(score), "detail": detail[:300]}))
# ------------------------------------------------------------------------------------------------
SPEEDUP_CHILD = r'''
import importlib.util, json, sys
P = json.loads(sys.stdin.read())
nonce = P["nonce"]
real_out = sys.stdout
sys.stdout = sys.stderr  # candidate prints go to stderr

def result(d):
    real_out.write("@@RESULT " + json.dumps(dict(d, nonce=nonce)) + "\n"); real_out.flush()
    raise SystemExit(0)

try:
    spec = importlib.util.spec_from_file_location("candidate", P["candidate_path"])
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    solve = getattr(mod, "solve")
except BaseException as e:
    result({"ok": False, "why": "load failed: " + type(e).__name__})
ref_ns = {}
exec(P["reference"], ref_ns)
ref = ref_ns["solve"]
for i, (args, want) in enumerate(P["cases"]):
    try:
        got = solve(*[list(a) if isinstance(a, list) else a for a in args])
    except BaseException as e:
        result({"ok": False, "why": f"hidden case {i}: raised {type(e).__name__}"})
    if got != want:
        result({"ok": False, "why": f"hidden case {i}: wrong answer (n={len(args[0])})"})

def cost(fn, args):
    n = [0]
    def tr(frame, event, arg):
        if event in ("call", "line"):
            n[0] += 1
        return tr
    sys.settrace(tr)
    try:
        fn(*[list(a) for a in args[:1]], *args[1:])
        still = sys.gettrace() is tr
    finally:
        sys.settrace(None)
    return n[0], still

try:
    c_ref, _ = cost(ref, P["cost_args"])
    c_cand, still = cost(solve, P["cost_args"])
except BaseException as e:
    result({"ok": False, "why": "cost run raised " + type(e).__name__})
if not still:
    result({"ok": False, "why": "candidate changed the tracer"})
result({"ok": True, "ref": c_ref, "cand": max(1, c_cand)})
'''


def speedup_check(candidate: str, reference: str, cases: list, cost_args: list, timeout: float = 120.0) -> None:
    """valid = right on every hidden case; score = traced line events of reference / candidate (cap 1e4)."""
    import secrets
    nonce = secrets.token_hex(8)
    ref_ns: dict = {}
    exec(reference, ref_ns)
    cases = [(args, ref_ns["solve"](*[list(a) if isinstance(a, list) else a for a in args])) for args in cases]
    r = run_child(candidate, SPEEDUP_CHILD, {"nonce": nonce, "reference": reference, "cases": cases,
                                             "cost_args": cost_args}, timeout)
    if r.get("nonce", nonce) != nonce or not r.get("ok"):
        emit(False, 0.0, r.get("why", "bad child result"))
        return
    ratio = min(1e4, r["ref"] / r["cand"])
    emit(True, round(ratio, 3), f"correct on {len(cases)} hidden cases; traced line events "
                                f"reference {r['ref']} vs candidate {r['cand']} (ratio {ratio:.1f}x)")

import random

SEED = 20261027
REFERENCE = '''
def solve(a):
    n = len(a)
    if n == 0:
        return 0
    best = [1] * n
    for i in range(n):
        for j in range(i):
            if a[j] < a[i] and best[j] + 1 > best[i]:
                best[i] = best[j] + 1
    return max(best)
'''


def hidden_cases():
    rng = random.Random(SEED)
    cases = [[[]], [[5]], [[1, 2, 3, 4]], [[4, 3, 2, 1]], [[2, 2, 2, 2]], [[1, 3, 2, 3, 1, 4]]]
    for _ in range(34):
        n = rng.randint(2, 300)
        hi = rng.choice([3, 50, 10**6])
        cases.append([[rng.randint(-hi, hi) for _ in range(n)]])
    return cases


if __name__ == "__main__":
    cand = sys.stdin.read()
    rng = random.Random(SEED + 1)
    speedup_check(cand, REFERENCE, hidden_cases(), [[rng.randint(0, 10**6) for _ in range(600)]])
