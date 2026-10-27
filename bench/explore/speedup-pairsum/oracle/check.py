"""Hidden oracle for bench/explore/speedup-pairsum. Reads the candidate on stdin; prints one JSON line.
The generator must never see this directory."""
# --- shared runner (byte-identical in each oracle dir; tests/test_explore_bench.py checks it) -----
# The child process only runs model-written code and returns raw outputs. The parent (this oracle)
# decides correctness and cost itself. The child never gets the expected answers, the oracle path or a
# verdict to print. A child that prints a forged line can only forge outputs; the parent checks them.
import json
import os
import subprocess
import sys
import tempfile
import time

# sandbox-exec profile: no network, writes only in the temp dir D, no reads of the oracle dir O,
# no fork/spawn. CPU seconds are capped. Without sandbox-exec the oracle fails closed unless
# EXPLORE_ALLOW_UNSANDBOXED=1.
SANDBOX = ('(version 1)(allow default)(deny network*)(deny file-write*)'
           '(allow file-write* (subpath (param "D")) (literal "/dev/null"))'
           '(deny file-read* (subpath (param "O")))(deny process-fork)')
ORACLE_DIR = os.path.dirname(os.path.realpath(__file__))
OUT_TAG = "@@OUT "
CHILD = r'''
import importlib.util, json, os, sys
P = json.loads(sys.stdin.read())
out = os.fdopen(os.dup(1), "w")
os.dup2(2, 1)  # candidate prints go to stderr; the parent discards stderr
spec = importlib.util.spec_from_file_location("candidate", P["path"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
fn = getattr(mod, P["entry"])
res = [fn(*args) for args in P["calls"]]
out.write("@@OUT " + json.dumps(res) + "\n")
out.flush()
'''


def sandboxed(d: str) -> list:
    if os.path.exists("/usr/bin/sandbox-exec"):
        return ["/usr/bin/sandbox-exec", "-D", "D=" + os.path.realpath(d), "-D", "O=" + ORACLE_DIR,
                "-p", SANDBOX]
    if os.environ.get("EXPLORE_ALLOW_UNSANDBOXED") == "1":
        return []
    raise RuntimeError("no sandbox-exec; set EXPLORE_ALLOW_UNSANDBOXED=1 to run unsandboxed")


def _limits(timeout: float) -> None:
    import resource
    cpu = int(timeout) + 5
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 << 20, 64 << 20))


def run_child(source: str, entry: str, calls: list, timeout: float) -> dict:
    """Run `entry(*args)` for each args in `calls` from `source` in a sandboxed `python -I -S` child in an
    empty temp dir. -> {"ok": True, "out": [raw results], "cpu": child user+sys s} or {"ok": False, "why"}.
    `cpu` comes from os.wait4 rusage of the child, never from the child. "why" is a fixed string."""
    with tempfile.TemporaryDirectory(prefix="explore-cand-") as d:
        path = os.path.join(d, "candidate.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(source)
        with tempfile.TemporaryFile("w+") as fin, tempfile.TemporaryFile("w+b") as fout:
            fin.write(json.dumps({"path": path, "entry": entry, "calls": calls}))
            fin.seek(0)
            p = subprocess.Popen(sandboxed(d) + [sys.executable, "-I", "-S", "-c", CHILD], stdin=fin,
                                 stdout=fout, stderr=subprocess.DEVNULL, cwd=d,
                                 env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0"},
                                 preexec_fn=lambda: _limits(timeout))
            end, timed_out = time.monotonic() + timeout, False
            while True:
                pid, status, ru = os.wait4(p.pid, os.WNOHANG)
                if pid:
                    break
                if time.monotonic() >= end:
                    p.kill()
                    timed_out = True
                    pid, status, ru = os.wait4(p.pid, 0)
                    break
                time.sleep(0.005)
            p.returncode = os.waitstatus_to_exitcode(status)  # reaped here; Popen must not wait again
            cpu = ru.ru_utime + ru.ru_stime
            if timed_out:
                return {"ok": False, "why": "timeout"}
            fout.seek(0)
            lines = [x for x in fout.read().decode("utf-8", "replace").splitlines() if x.startswith(OUT_TAG)]
    if p.returncode != 0 or len(lines) != 1:
        return {"ok": False, "why": "candidate crashed or printed no result"}
    try:
        out = json.loads(lines[0][len(OUT_TAG):])
    except ValueError:
        return {"ok": False, "why": "candidate result is not JSON"}
    if not isinstance(out, list) or len(out) != len(calls):
        return {"ok": False, "why": "candidate result has the wrong shape"}
    return {"ok": True, "out": out, "cpu": cpu}


def emit(valid: bool, score: float, detail: str) -> None:
    print(json.dumps({"valid": bool(valid), "score": float(score), "detail": detail[:300]}))
# --- end shared runner ----------------------------------------------------------------------------

NOOP = "def solve(*args):\n    return None\n"
FLOOR_S = 0.005    # candidate net CPU below 5 ms counts as 5 ms (timer noise)
SCORE_CAP = 100.0  # an honest O(n log n) candidate reaches ~195x; the cap keeps the score deterministic
REPEATS = 3        # cost = min over repeats (noise)


def _same(a, b) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _cost(source: str, calls: list, timeout: float, expect=None):
    """-> (min child CPU s, raw outputs) or (None, why). Every repeat must give the same output, and the
    expected output when `expect` is set: a fast wrong repeat must not set the minimum (xval pass 2)."""
    best, out = None, None
    for _ in range(REPEATS):
        r = run_child(source, "solve", calls, timeout)
        if not r["ok"]:
            return None, r["why"]
        if out is not None and not _same(r["out"], out):
            return None, "outputs differ between repeats"
        if expect is not None and not _same(r["out"], expect):
            return None, "wrong answer"
        best = r["cpu"] if best is None else min(best, r["cpu"])
        out = r["out"]
    return best, out


def speedup_check(candidate: str, reference: str, cases: list, cost_args: list) -> None:
    """valid = same output as the reference on every hidden case and on the cost input.
    score = (reference CPU - no-op CPU) / max(candidate CPU - no-op CPU, 5 ms), cap 100.
    CPU = os.wait4 rusage (user + sys) of the same sandboxed child shape. The parent decides."""
    ref_ns: dict = {}
    exec(reference, ref_ns)
    want = [ref_ns["solve"](*[list(a) if isinstance(a, list) else a for a in args]) for args in cases]
    r = run_child(candidate, "solve", cases, 60.0)
    if not r["ok"]:
        emit(False, 0.0, r["why"])
        return
    for i, (got, w) in enumerate(zip(r["out"], want)):
        if not _same(got, w):
            emit(False, 0.0, f"hidden case {i}: wrong answer")
            return
    noop, _ = _cost(NOOP, [cost_args], 30.0)
    if noop is None:
        emit(False, 0.0, "no-op run failed")
        return
    ref_cpu, ref_out = _cost(reference, [cost_args], 60.0)
    if ref_cpu is None:
        emit(False, 0.0, "reference run failed")
        return
    cand_cpu, cand_out = _cost(candidate, [cost_args], 30.0, expect=ref_out)
    if cand_cpu is None:
        emit(False, 0.0, "cost input: " + cand_out)
        return
    ratio = min(SCORE_CAP, max(ref_cpu - noop, FLOOR_S) / max(cand_cpu - noop, FLOOR_S))
    emit(True, round(ratio, 3), f"correct on {len(cases)} hidden cases and the cost input; score = "
                                f"reference / candidate child CPU (no-op subtracted, 5 ms floor, cap 100)")

import random

SEED = 20261028
REFERENCE = '''
def solve(a, lo, hi):
    count = 0
    n = len(a)
    for i in range(n):
        for j in range(i + 1, n):
            s = a[i] + a[j]
            if lo <= s <= hi:
                count += 1
    return count
'''


def hidden_cases():
    rng = random.Random(SEED)
    cases = [[[], 0, 10], [[1], 0, 10], [[1, 1], 2, 2], [[5, -5, 0], 0, 0], [[3, 3, 3], 7, 1]]
    for _ in range(35):
        n = rng.randint(2, 300)
        r = rng.choice([5, 100, 10**6])
        a = [rng.randint(-r, r) for _ in range(n)]
        lo = rng.randint(-2 * r, 2 * r)
        cases.append([a, lo, lo + rng.randint(0, r)])
    return cases


if __name__ == "__main__":
    cand = sys.stdin.read()
    rng = random.Random(SEED + 1)
    # n=7500: the O(n^2) reference takes ~1 s child CPU on an M-series Mac (cost input sized for the 5 ms floor)
    a = [rng.randint(-10**6, 10**6) for _ in range(7500)]
    speedup_check(cand, REFERENCE, hidden_cases(), [a, -300000, 400000])
