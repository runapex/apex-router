"""The pilot bench tasks (bench/explore/*): oracle is deterministic, isolated, and separates a known
slow/weak candidate (no hit) from a known good one (hit). Runs the real oracle commands; no model."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from apex_router.explore.oracle import is_inside
from apex_router.explore.run import Task, prepare_workdir

BENCH = Path(__file__).resolve().parents[1] / "bench" / "explore"

LIS_FAST = """import bisect
def solve(a):
    t = []
    for x in a:
        i = bisect.bisect_left(t, x)
        if i == len(t):
            t.append(x)
        else:
            t[i] = x
    return len(t)
"""
LIS_WRONG = LIS_FAST.replace("bisect_left", "bisect_right")  # non-strict: wrong on duplicates
PAIRSUM_FAST = """import bisect
def solve(a, lo, hi):
    if lo > hi:
        return 0
    b = sorted(a)
    c = 0
    for i, x in enumerate(b):
        c += bisect.bisect_right(b, hi - x, i + 1) - bisect.bisect_left(b, lo - x, i + 1)
    return c
"""
BIN_FFD = """def pack(items, cap):
    bins, loads = [], []
    for i in sorted(range(len(items)), key=lambda i: -items[i]):
        for b in range(len(bins)):
            if loads[b] + items[i] <= cap:
                bins[b].append(i); loads[b] += items[i]; break
        else:
            bins.append([i]); loads.append(items[i])
    return bins
"""
BIN_FILL = """import bisect
def pack(items, cap):
    rem = sorted(range(len(items)), key=lambda i: items[i])
    bins = []
    while rem:
        b = [rem.pop()]; left = cap - items[b[0]]
        while True:
            vals = [items[i] for i in rem]
            best = None
            k = bisect.bisect_right(vals, left) - 1
            if k >= 0:
                best = (vals[k], [k])
            lo, hi = 0, len(vals) - 1
            while lo < hi:
                s = vals[lo] + vals[hi]
                if s <= left:
                    if best is None or s > best[0]:
                        best = (s, [lo, hi])
                    lo += 1
                else:
                    hi -= 1
            if best is None:
                break
            for k in sorted(best[1], reverse=True):
                b.append(rem.pop(k))
            left -= best[0]
            if left == 0:
                break
        bins.append(b)
    return bins
"""


def _ref(task: Task) -> str:
    body = task.goal.split("```python\n", 1)[1].split("```", 1)[0]
    return body


@pytest.mark.parametrize("name", ["speedup-lis", "speedup-pairsum", "binpack"])
def test_task_layout_and_isolation(name):
    t = Task.load(BENCH / name)
    assert t.tau > 0 and t.entry and t.oracle_cmd and (t.oracle_dir / "check.py").exists()
    assert "oracle" not in t.goal.lower()
    wd = prepare_workdir(t)
    try:
        assert sorted(p.name for p in wd.iterdir()) == ["goal.md"]
        assert not is_inside(t.oracle_dir, wd) and not is_inside(wd, t.oracle_dir)
    finally:
        import shutil
        shutil.rmtree(wd)


@pytest.mark.parametrize("name,good,bad_valid", [
    ("speedup-lis", LIS_FAST, None),
    ("speedup-pairsum", PAIRSUM_FAST, None),
    ("binpack", BIN_FILL, BIN_FFD),
], ids=["speedup-lis", "speedup-pairsum", "binpack"])
def test_oracle_separates_hit_from_no_hit(name, good, bad_valid):
    t = Task.load(BENCH / name)
    orc = t.oracle_fn()
    g = orc(good)
    assert g.valid and g.score >= t.tau, g
    assert orc(good) == g  # deterministic
    weak = bad_valid if bad_valid is not None else _ref(t)  # the slow reference itself: ratio 1
    w = orc(weak)
    assert w.valid and w.score < t.tau, w


def test_oracle_rejects_wrong_and_cost_tamper():
    t = Task.load(BENCH / "speedup-lis")
    orc = t.oracle_fn()
    r = orc(LIS_WRONG)
    assert not r.valid and "wrong answer" in r.detail
    # was: the candidate turned off the line tracer. The cost is now child CPU (os.wait4 rusage); the tamper
    # on that measure is to move the work to another process, which the sandbox denies.
    r = orc("import subprocess\nsubprocess.run(['/usr/bin/true'])\n" + LIS_FAST)
    assert not r.valid, r
    assert not orc("def solve(:\n").valid


SHARED_START, SHARED_END = "# --- shared runner", "# --- end shared runner"


def _shared_block(name: str) -> str:
    src = (BENCH / name / "oracle" / "check.py").read_text()
    a = src.index(SHARED_START)
    return src[a:src.index("\n", src.index(SHARED_END, a)) + 1]


def test_f1_shared_runner_is_byte_identical_in_the_3_oracles():
    blocks = [_shared_block(n) for n in ("speedup-lis", "speedup-pairsum", "binpack")]
    assert blocks[0] == blocks[1] == blocks[2]
    assert "def run_child(" in blocks[0] and "os.wait4(" in blocks[0] and "(deny process-fork)" in blocks[0]


GOOD = {"speedup-lis": LIS_FAST, "speedup-pairsum": PAIRSUM_FAST, "binpack": BIN_FILL}
# The candidate reads the child's own payload (so it also has any nonce) and prints a forged verdict.
FORGE_RESULT = """import __main__, json, os, sys
_p = getattr(__main__, "P", {})
sys.__stdout__.write("@@RESULT " + json.dumps({"ok": True, "ref": 10**9, "cand": 1, "nonce": _p.get("nonce"),
                     "bins": []}) + "\\n")
sys.__stdout__.flush()
os._exit(0)
def solve(*a):
    return 0
def pack(items, cap):
    return []
"""
# The same attack on the new child: forge the raw-output line with made-up outputs.
FORGE_OUT = """import __main__, json, os, sys
_n = len(getattr(__main__, "P", {}).get("calls", [0]))
for _fd in range(1, 10):  # also the parent's real stdout, wherever the child keeps it
    try:
        os.write(_fd, ("@@OUT " + json.dumps([0] * _n) + "\\n").encode())
    except OSError:
        pass
os._exit(0)
def solve(*a):
    return 0
def pack(items, cap):
    return []
"""


@pytest.mark.parametrize("name", ["speedup-lis", "speedup-pairsum", "binpack"])
def test_f1_candidate_cannot_forge_the_verdict(name):
    orc = Task.load(BENCH / name).oracle_fn()
    for forged in (FORGE_RESULT, FORGE_OUT):
        r = orc(forged)
        assert not r.valid and r.score == 0.0, (forged[:40], r)


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")
@pytest.mark.parametrize("name", ["speedup-lis", "speedup-pairsum", "binpack"])
def test_f1_sandbox_blocks_oracle_dir_read_fork_and_spawn(name):
    t = Task.load(BENCH / name)
    orc = t.oracle_fn()
    check = str((t.oracle_dir / "check.py").resolve())
    for probe in (f"open({check!r}).read()\n", "import os\nif os.fork() == 0:\n    os._exit(0)\n",
                  "import subprocess\nsubprocess.run(['/usr/bin/true'])\n"):
        r = orc(probe + GOOD[name])
        assert not r.valid, (probe, r)


def test_f2_oracle_detail_never_holds_candidate_output():
    # the candidate prints (a copy of) its source to stdout and stderr, then crashes
    leak = ("import os, sys\nprint('LEAK_SRC_STDOUT def solve(a)')\n"
            "sys.stderr.write('LEAK_SRC_STDERR def solve(a): return 42\\n'); sys.stderr.flush()\nos._exit(1)\n")
    for name in ("speedup-lis", "binpack"):
        r = Task.load(BENCH / name).oracle_fn()(leak + GOOD[name])
        assert not r.valid and "LEAK_SRC" not in r.detail and "def solve" not in r.detail, r


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")
@pytest.mark.parametrize("name", ["speedup-lis", "speedup-pairsum", "binpack"])
def test_oracle_sandbox_blocks_write_outside_tmp_and_network(name, tmp_path):
    # model-written code runs in the oracle: a write outside its temp dir or a socket must fail
    t = Task.load(BENCH / name)
    target = tmp_path / "escape.txt"
    probe = (f"import socket\nopen({str(target)!r}, 'w').write('x')\n"
             "socket.create_connection(('127.0.0.1', 8788), timeout=2)\n")
    r = t.oracle_fn()(probe + {"binpack": BIN_FILL}.get(name, LIS_FAST if name == "speedup-lis" else PAIRSUM_FAST))
    assert not r.valid, r
    assert not target.exists()
    net_only = "import socket\nsocket.create_connection(('127.0.0.1', 8788), timeout=2)\n"
    r = t.oracle_fn()(net_only + {"binpack": BIN_FILL}.get(name, LIS_FAST if name == "speedup-lis" else PAIRSUM_FAST))
    assert not r.valid, r


@pytest.mark.parametrize("name", ["speedup-lis", "speedup-pairsum"])
def test_xval2_a_fast_wrong_cost_repeat_does_not_set_the_score(name, monkeypatch, capsys):
    # xval pass 2 probe: repeats fast+wrong, slow+right, slow+right must not score 100 (it is 1x)
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"oracle_{name.replace('-', '_')}", BENCH / name / "oracle" / "check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    real = mod.run_child
    seq = {"cand": 0}

    def fake(source, fn, calls, timeout):
        r = real(source, fn, calls, timeout)
        if source.startswith("# cand"):
            seq["cand"] += 1
            if len(calls) == 1 and seq["cand"] == 2:     # first cost repeat: 11 ms, wrong output
                return {"ok": True, "cpu": 0.011, "out": [None]}
        return r
    monkeypatch.setattr(mod, "run_child", fake)
    ref = mod.REFERENCE
    cand = "# cand\n" + ref
    cost_args = [list(range(50))] + ([0, 10] if name == "speedup-pairsum" else [])
    mod.speedup_check(cand, ref, mod.hidden_cases()[:6], cost_args)
    d = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert not d["valid"] and d["score"] == 0.0, d
