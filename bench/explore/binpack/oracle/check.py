"""Hidden oracle for bench/explore/binpack. Reads the candidate on stdin; prints one JSON line.
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

import math
import random

SEED = 20261029
TIME_LIMIT_S = 20.0  # all 12 instances together; a timeout makes the candidate invalid


def hidden_cases():
    rng = random.Random(SEED)
    cases = []
    for _ in range(4):  # uniform
        cases.append(([rng.randint(20, 100) for _ in range(200)], 150))
    for _ in range(4):  # triplets that fill a bin exactly
        items = []
        for _ in range(60):
            a, b = rng.randint(250, 499), rng.randint(250, 499)
            c = 1000 - a - b
            if not 250 <= c <= 499:
                a, b, c = 333, 333, 334
            items += [a, b, c]
        rng.shuffle(items)
        cases.append((items, 1000))
    for _ in range(4):  # mid-size items + small fillers
        items = [rng.randint(35, 65) for _ in range(120)] + [rng.randint(1, 20) for _ in range(80)]
        rng.shuffle(items)
        cases.append((items, 100))
    return cases


def _bins_ok(bins, n: int, cap: int, items: list) -> bool:
    """The parent checks the child's raw bins: list of non-empty int lists, each index once, no overflow."""
    if not isinstance(bins, list) or not all(isinstance(b, list) and b for b in bins):
        return False
    flat = [x for b in bins for x in b]
    if not all(type(x) is int for x in flat) or sorted(flat) != list(range(n)):
        return False
    return all(sum(items[x] for x in b) <= cap for b in bins)


def check(cand: str) -> None:
    """The child returns bins only. The parent checks them and computes the score."""
    cases = hidden_cases()
    r = run_child(cand, "pack", [[list(items), cap] for items, cap in cases], TIME_LIMIT_S)
    if not r["ok"]:
        emit(False, 0.0, r["why"])
        return
    ratios = []
    for i, ((items, cap), bins) in enumerate(zip(cases, r["out"])):
        if not _bins_ok(bins, len(items), cap, items):
            emit(False, 0.0, f"hidden instance {i}: bins are not a valid packing")
            return
        ratios.append(math.ceil(sum(items) / cap) / len(bins))
    score = sum(ratios) / len(ratios)
    fam = [sum(ratios[k:k + 4]) / 4 for k in (0, 4, 8)]
    emit(True, round(score, 5), f"valid on {len(cases)} hidden instances; mean lower_bound/bins {score:.4f} "
                                f"(families: {fam[0]:.3f}, {fam[1]:.3f}, {fam[2]:.3f})")


if __name__ == "__main__":
    check(sys.stdin.read())
