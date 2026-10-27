"""The pilot bench tasks (bench/explore/*): oracle is deterministic, isolated, and separates a known
slow/weak candidate (no hit) from a known good one (hit). Runs the real oracle commands; no model."""
from __future__ import annotations

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
])
def test_oracle_separates_hit_from_no_hit(name, good, bad_valid):
    t = Task.load(BENCH / name)
    orc = t.oracle_fn()
    g = orc(good)
    assert g.valid and g.score >= t.tau, g
    assert orc(good) == g  # deterministic
    weak = bad_valid if bad_valid is not None else _ref(t)  # the slow reference itself: ratio 1
    w = orc(weak)
    assert w.valid and w.score < t.tau, w


def test_oracle_rejects_wrong_and_tracer_tamper():
    t = Task.load(BENCH / "speedup-lis")
    orc = t.oracle_fn()
    r = orc(LIS_WRONG)
    assert not r.valid and "wrong answer" in r.detail
    r = orc("import sys\n" + LIS_FAST.replace("    t = []", "    sys.settrace(None)\n    t = []"))
    assert not r.valid and "tracer" in r.detail
    assert not orc("def solve(:\n").valid
