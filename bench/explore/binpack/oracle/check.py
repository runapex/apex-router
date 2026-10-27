"""Hidden oracle for bench/explore/binpack. Reads the candidate on stdin; prints one JSON line.
The generator must never see this directory."""
# --- shared runner (copied into each oracle dir; each oracle dir is self-contained) -------------
import json
import os
import subprocess
import sys
import tempfile


def run_child(candidate: str, child_code: str, payload: dict, timeout: float) -> dict:
    """Run the candidate in a separate `python -I` process in an empty temp dir.
    The child gets the candidate path and the payload on stdin. It does not get the oracle path."""
    with tempfile.TemporaryDirectory(prefix="explore-cand-") as d:
        path = os.path.join(d, "candidate.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(candidate)
        payload = dict(payload, candidate_path=path)
        try:
            p = subprocess.run([sys.executable, "-I", "-c", child_code], input=json.dumps(payload),
                               capture_output=True, text=True, timeout=timeout, cwd=d,
                               env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0"})
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

import math
import random
import secrets

SEED = 20261029
TIME_LIMIT_S = 20.0  # all 12 instances together; a timeout makes the candidate invalid

CHILD = r'''
import importlib.util, json, sys, time
P = json.loads(sys.stdin.read())
nonce = P["nonce"]
real_out = sys.stdout
sys.stdout = sys.stderr

def result(d):
    real_out.write("@@RESULT " + json.dumps(dict(d, nonce=nonce)) + "\n"); real_out.flush()
    raise SystemExit(0)

try:
    spec = importlib.util.spec_from_file_location("candidate", P["candidate_path"])
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    pack = getattr(mod, "pack")
except BaseException as e:
    result({"ok": False, "why": "load failed: " + type(e).__name__})
out = []
for i, (items, cap) in enumerate(P["cases"]):
    try:
        bins = pack(list(items), cap)
        bins = [[int(x) for x in b] for b in bins]
    except BaseException as e:
        result({"ok": False, "why": f"hidden instance {i}: raised or bad return type ({type(e).__name__})"})
    out.append(bins)
result({"ok": True, "bins": out})
'''


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


def check(cand: str) -> None:
    cases = hidden_cases()
    nonce = secrets.token_hex(8)
    r = run_child(cand, CHILD, {"nonce": nonce, "cases": cases}, TIME_LIMIT_S)
    if r.get("nonce", nonce) != nonce or not r.get("ok"):
        emit(False, 0.0, r.get("why", "bad child result"))
        return
    ratios = []
    for i, ((items, cap), bins) in enumerate(zip(cases, r["bins"])):
        flat = [x for b in bins for x in b]
        if sorted(flat) != list(range(len(items))):
            emit(False, 0.0, f"hidden instance {i}: each item index must be in exactly one bin")
            return
        for b in bins:
            if not b or sum(items[x] for x in b) > cap:
                emit(False, 0.0, f"hidden instance {i}: a bin is empty or over capacity")
                return
        ratios.append(math.ceil(sum(items) / cap) / len(bins))
    score = sum(ratios) / len(ratios)
    fam = [sum(ratios[k:k + 4]) / 4 for k in (0, 4, 8)]
    emit(True, round(score, 5), f"valid on {len(cases)} hidden instances; mean lower_bound/bins {score:.4f} "
                                f"(families: {fam[0]:.3f}, {fam[1]:.3f}, {fam[2]:.3f})")


if __name__ == "__main__":
    check(sys.stdin.read())
