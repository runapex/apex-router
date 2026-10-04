# Local-handoff hangs, over-shedding, and receding handoff nudge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the four code-level causes that make an aggressive local-handoff setup hang scripts, shed work to weaker tiers, and let sessions grow past the point where cache reads dominate spend.

**Architecture:** Four independent, small changes, each behind its own test file: (1) a bounded inference lock in `ornith_client`, (2) thinking-OFF + bounded subprocess in `apex-ornith review`, (3) pressure no longer counts retried-then-succeeded rows as transport faults, (4) handoff threshold uses a clamped median instead of a self-raising p80. Plus (5) a deterministic probe that proves the local backend honours `reasoning_effort: "none"` — the foundry check that distinguishes "thinking really off" from "silently on".

**Tech Stack:** Python 3.12 stdlib (fcntl, subprocess, unittest/pytest). Tests run with `.venv/bin/pytest -q`.

**Spec:** The findings in this conversation (re-validated 2026-10-03 against source). Line references below were verified with `sed -n` on HEAD `44e7241`.

## Global Constraints

- No new dependencies. All touched modules are stdlib-only today and must stay so.
- Every change fails SAFE in the same direction the module already does: lock timeout → escalate (lane callers already catch `Exception`), subprocess timeout → empty diff → `EXIT_OK` "nothing staged", pressure → never crash → `UNKNOWN`.
- Do not change measured defaults that other modules cite (`INFER_TIMEOUT` 900 s, `review_lane` max_tokens 1024, `FLOOR` 25M).
- No `Co-Authored-By` trailer in commits (user rule).
- Commit each task separately; run `.venv/bin/pytest -q` before each commit.

## Review Focus

Inputs the spec implies but no existing test exercises; each gets a test in its owning task:

1. `ORNITH_LOCK_TIMEOUT_SECS=0` or negative → must mean "fail immediately if busy", not "wait forever" (Task 1).
2. A lock holder that dies mid-request → `flock` is released by the kernel; a waiter must proceed, not time out (Task 1, documented, no test — kernel semantics).
3. A pressure window where EVERY row is retried-then-succeeded → must be GREEN with `retried == n` (Task 3).
4. Threshold with zero sessions and with exactly `MIN_SESSIONS` sessions → floor and clamped-median respectively, never 0 (Task 4).
5. Backend that ignores `reasoning_effort` and returns legacy `<think>…</think>` inline → probe must report thinking ON (Task 5).

---

### Task 1: Bounded inference lock

**Files:**
- Modify: `src/apex_router/ornith/ornith_client.py:36-38` (constants), `:93-102` (`inference_lock`), `:77-81` (exceptions)
- Test: `tests/test_ornith_client_lock.py` (create)

**Interfaces:**
- Produces: `LOCK_TIMEOUT = _env_num("ORNITH_LOCK_TIMEOUT_SECS", "120", float)`; `class OrnithBusy(OrnithUnavailable)`; `inference_lock(timeout_s: float | None = None)` — contextmanager; raises `OrnithBusy` when the lock is not acquired within `timeout_s` (default `LOCK_TIMEOUT`). `timeout_s <= 0` means one non-blocking attempt.
- Consumers unchanged: `chat_messages` calls `inference_lock()` with no args; `offload_lanes`/`dispatch` catch `Exception` and escalate.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ornith_client_lock.py
"""inference_lock must be bounded: a second caller waits at most ORNITH_LOCK_TIMEOUT_SECS, then
raises OrnithBusy (an OrnithUnavailable) so lanes escalate instead of hanging a Claude Code turn."""
import fcntl
import sys
import threading
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import apex_router.ornith.ornith_client as oc  # noqa: E402


class _Holder:
    """Hold oc.LOCK exclusively from another thread (own fd → distinct flock owner)."""
    def __init__(self):
        self.held = threading.Event()
        self.release = threading.Event()

    def run(self):
        oc.LOCK.parent.mkdir(parents=True, exist_ok=True)
        with oc.LOCK.open("a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            self.held.set()
            self.release.wait(5)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class TestBoundedLock(unittest.TestCase):
    def setUp(self):
        self._orig = oc.LOCK
        oc.LOCK = Path(self.id().replace(".", "_") + ".lock").resolve()
        self.h = _Holder()
        self.t = threading.Thread(target=self.h.run, daemon=True)
        self.t.start()
        self.assertTrue(self.h.held.wait(2))

    def tearDown(self):
        self.h.release.set()
        self.t.join(5)
        oc.LOCK.unlink(missing_ok=True)
        oc.LOCK = self._orig

    def test_busy_raises_within_timeout(self):
        t0 = time.monotonic()
        with self.assertRaises(oc.OrnithBusy):
            with oc.inference_lock(timeout_s=0.3):
                pass
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_busy_is_unavailable_subclass(self):
        self.assertTrue(issubclass(oc.OrnithBusy, oc.OrnithUnavailable))

    def test_zero_timeout_is_one_nonblocking_attempt(self):
        t0 = time.monotonic()
        with self.assertRaises(oc.OrnithBusy):
            with oc.inference_lock(timeout_s=0):
                pass
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_acquires_after_holder_releases(self):
        def free_soon():
            time.sleep(0.2); self.h.release.set()
        threading.Thread(target=free_soon, daemon=True).start()
        with oc.inference_lock(timeout_s=3):
            acquired = True
        self.assertTrue(acquired)

    def test_chat_messages_surfaces_busy(self):
        orig_post = oc._post
        oc._post = lambda *a, **k: self.fail("must not POST while busy")
        try:
            with self.assertRaises(oc.OrnithBusy):
                oc.chat_messages([{"role": "user", "content": "x"}], max_tokens=1)
        finally:
            oc._post = orig_post

    def test_default_timeout_env(self):
        self.assertEqual(oc._env_num("ORNITH_LOCK_TIMEOUT_SECS", "120", float), oc.LOCK_TIMEOUT)
```

Note: `test_chat_messages_surfaces_busy` needs a short default — set `oc.LOCK_TIMEOUT = 0.2` in `setUp` and restore in `tearDown` (add `self._orig_to = oc.LOCK_TIMEOUT; oc.LOCK_TIMEOUT = 0.2` / restore).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ornith_client_lock.py -v`
Expected: FAIL — `AttributeError: module has no attribute 'OrnithBusy'` / `TypeError: inference_lock() got an unexpected keyword argument`.

- [ ] **Step 3: Implement**

In `ornith_client.py` after `STARTUP_RETRIES` (line 39):

```python
# Max wait for the machine-wide inference lock. Every local call serializes on it; a caller behind a
# long job (worst case INFER_TIMEOUT=900 s) used to block with no bound — from Claude Code that reads
# as a hung script. <=0 means one non-blocking attempt.
LOCK_TIMEOUT = _env_num("ORNITH_LOCK_TIMEOUT_SECS", "120", float)
```

After `OrnithAmbiguousFailure` (line 81):

```python
class OrnithBusy(OrnithUnavailable): pass             # inference lock not acquired in time
```

Replace `inference_lock` (lines 93-102):

```python
@contextmanager
def inference_lock(timeout_s: float | None = None):
    """Exclusive machine-wide inference lock, bounded. Polls LOCK_NB until `timeout_s` (default
    LOCK_TIMEOUT) elapses, then raises OrnithBusy — an OrnithUnavailable, so every lane's
    `except Exception` path escalates instead of hanging. A holder that dies releases the flock
    (kernel semantics), so a waiter never waits on a corpse."""
    deadline = time.monotonic() + max(0.0, LOCK_TIMEOUT if timeout_s is None else timeout_s)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as f:
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise OrnithBusy(f"inference lock busy for >{LOCK_TIMEOUT if timeout_s is None else timeout_s:g}s: {LOCK}")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
```

Confirm `import time` exists at top (it is used by `_get`; verify with `grep -n "^import time" ornith_client.py`).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_ornith_client_lock.py tests/test_ornith_client_truncation.py tests/test_offload_lanes.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/apex_router/ornith/ornith_client.py tests/test_ornith_client_lock.py
git commit -m "fix(ornith): bound the inference lock — OrnithBusy after ORNITH_LOCK_TIMEOUT_SECS (120 s) instead of waiting forever"
```

---

### Task 2: `apex-ornith review` — thinking OFF, bounded budget and git subprocess

**Files:**
- Modify: `src/apex_router/ornith/apex_ornith.py:68-72` (`_real_review`), `:79-83` (`_git_diff`), `:190` (`--budget` default)
- Test: `tests/test_apex_ornith_review.py` (create)

**Interfaces:**
- Produces: `_real_review(diff, *, budget)` calls `batch_over_preamble(..., enable_thinking=False, ...)`; `_git_diff(staged, base, *, timeout_s=30.0) -> str` returns `""` on `subprocess.TimeoutExpired`; `--budget` default `1024`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_apex_ornith_review.py
"""apex-ornith review must not use thinking (measured: thinking-ON hangs 0/3, burns the budget in
<think>) and must never block on git."""
import subprocess
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import apex_router.ornith.apex_ornith as ao  # noqa: E402
import apex_router.ornith.ornith_batch as ob  # noqa: E402


class _R:
    answer = "NO DEFECTS FOUND"


class TestReviewThinkingOff(unittest.TestCase):
    def test_real_review_passes_thinking_false(self):
        seen = {}
        def fake(preamble, items, **kw):
            seen.update(kw); return [_R()]
        orig = ob.batch_over_preamble
        ob.batch_over_preamble = fake
        try:
            out = ao._real_review("diff", budget=777)
        finally:
            ob.batch_over_preamble = orig
        self.assertEqual(out, "NO DEFECTS FOUND")
        self.assertIs(seen["enable_thinking"], False)
        self.assertEqual(seen["max_tokens"], 777)

    def test_budget_default_is_1024(self):
        seen = {}
        rc = ao.run(["review"], liveness_fn=lambda: True,
                    diff_fn=lambda s, b: "x",
                    select_fn=lambda **k: type("R", (), {"fits": True, "reason": ""})(),
                    review_fn=lambda d, *, budget: seen.setdefault("b", budget) and "ok",
                    emit=lambda s: None)
        self.assertEqual(rc, ao.EXIT_OK)
        self.assertEqual(seen["b"], 1024)


class TestGitDiffBounded(unittest.TestCase):
    def test_timeout_returns_empty(self):
        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        orig = subprocess.run
        subprocess.run = boom
        try:
            self.assertEqual(ao._git_diff(True, None, timeout_s=0.01), "")
        finally:
            subprocess.run = orig

    def test_passes_timeout(self):
        seen = {}
        def fake(cmd, **kw):
            seen.update(kw); return type("P", (), {"stdout": "d"})()
        orig = subprocess.run
        subprocess.run = fake
        try:
            self.assertEqual(ao._git_diff(True, None), "d")
        finally:
            subprocess.run = orig
        self.assertEqual(seen["timeout"], 30.0)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_apex_ornith_review.py -v`
Expected: FAIL — `assertIs(True, False)`, `6000 != 1024`, `TypeError: _git_diff() got an unexpected keyword argument 'timeout_s'`.

- [ ] **Step 3: Implement**

```python
def _real_review(diff: str, *, budget: int) -> str:
    from .ornith_batch import batch_over_preamble
    # thinking OFF — MEASURED (ornith_code.py): thinking-ON runs the whole budget inside <think>
    # and returns no answer; on the review path that was a 900 s hang per call.
    r = batch_over_preamble(REVIEW_PREAMBLE, [f"Review this diff:\n\n{diff}"],
                            max_tokens=budget, enable_thinking=False, temperature=0.0)
    return r[0].answer
```

```python
def _git_diff(staged: bool, base: str | None, *, timeout_s: float = 30.0) -> str:
    cmd = ["git", "diff", "--staged"] if staged and not base else \
          (["git", "diff", f"{base}...HEAD"] if base else ["git", "diff", "--staged"])
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s).stdout
    except subprocess.TimeoutExpired:
        return ""   # reads as "nothing staged" → EXIT_OK; never hang the caller
```

Line 190: `pr.add_argument("--budget", type=int, default=1024)` — matches `review_lane`'s measured cap (160–260 completion tokens for a real review; 1024 leaves headroom without the 6000-token runaway).

Also fix the stale liveness message on line 202: `emit("Ornith unavailable on :8080 — use Opus.")` → `emit(f"Ornith unavailable at {oc_base_url()} — use Opus.")` is out of scope; leave it, but change `:8080` → `(local tier)` so it stops naming the retired MLX port.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_apex_ornith_review.py -v && .venv/bin/pytest -q -k "ornith" `
Expected: PASS. If an existing test asserts `enable_thinking=True` or `budget == 6000` for review, update that assertion (it pinned the bug).

- [ ] **Step 5: Commit**

```bash
git add src/apex_router/ornith/apex_ornith.py tests/test_apex_ornith_review.py
git commit -m "fix(apex-ornith): review runs thinking-OFF with a 1024-token budget; git diff is bounded"
```

---

### Task 3: Pressure — retried-then-succeeded rows are not transport faults

**Files:**
- Modify: `src/apex_router/pressure.py:316-322`, docstring lines 7-11 and `render` footnote ~line 437
- Test: `tests/test_pressure.py` (append)

**Interfaces:**
- Produces: bucket semantics — `transport_errors` counts only rows whose `_classify` is `"transport"`; `retried` still counts rows with `connect_retries > 0`. Output dict keys unchanged.

- [ ] **Step 1: Replace the test that pins the old rule**

`tests/test_pressure.py:423` `test_retried_success_counts_as_transport_fault` (section "P3") deliberately asserts the OLD rule: 5 retried-then-succeeded rows out of 95 → `transport_errors == 5`, level AMBER. That rule is what this task reverses (a retry the client never saw is apex's recovery, not upstream pressure). Delete that test; keep `test_retried_and_failed_row_counted_once` (line 434) unchanged — it still holds. Say so in the commit message.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_pressure.py`:

```python
# ---------------------------------------------------------------- retried ≠ transport fault

def test_retried_then_succeeded_is_not_transport(tmp_path):
    rows = _ok(100, connect_retries=1)
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["level"] == "GREEN"
    assert r["overall"]["transport_errors"] == 0
    assert r["overall"]["retried"] == 100


def test_real_transport_fault_still_counts(tmp_path):
    rows = _ok(90) + _ok(10, start=NOW - 30, cause="SSLError", connect_retries=2)
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["overall"]["transport_errors"] == 10
    assert r["overall"]["retried"] == 10
    assert r["level"] == "AMBER"   # 10% transport → >= 3% amber, not > 10% red
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest tests/test_pressure.py -k "retried or transport_fault" -v`
Expected: first test FAILS (`transport_errors == 100`, level AMBER/RED).

- [ ] **Step 4: Implement**

Replace lines 316-322:

```python
            retried = _retries(row) > 0
            if retried:
                b["retried"] += 1
            # A transport FAULT is a row the client actually saw fail. A connect-retried row that
            # then succeeded is reported under `retried` only: counting it here pushed boxes with a
            # flaky keep-alive path into permanent AMBER/RED and shed work to weaker tiers for no
            # upstream reason (the retry is apex's own recovery, invisible to the client).
            if kind == "transport":
                b["transport_errors"] += 1
```

Update the module docstring line 10 to: `AMBER  429 2–10% or transport 3–10% (client-visible faults; apex-recovered retries are reported separately)`. Update the `render` footnote (~437) from `xport includes connect-retried rows` to `xport = client-visible faults only; retried shown separately`.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_pressure.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/apex_router/pressure.py tests/test_pressure.py
git commit -m "fix(pressure): retried-then-succeeded rows no longer count as transport faults"
```

---

### Task 4: Handoff threshold — clamped median, not self-raising p80

**Files:**
- Modify: `scripts/handoff_threshold.py:17-18` (constants), `:105-125` (`compute_threshold`)
- Modify: `hooks/cache-handoff-nudge.sh:42-44` (comment only: "p80" → "clamped median")
- Test: `tests/test_handoff_threshold.py` (create)

**Interfaces:**
- Produces: `compute_threshold(session_totals: dict, days: int) -> (threshold, basis, p50, p80, max_total, n)` — unchanged shape; `threshold = clamp(p50, FLOOR, CAP)` with `CAP = 100_000_000`; `basis = f"median of {n} sessions over {days}d (clamped {FLOOR}-{CAP})"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_handoff_threshold.py
"""The nudge threshold must not recede: p80 of cumulative reads rises with every long session,
so the nudge fired later and later. Use the median, clamped to [FLOOR, CAP]."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import handoff_threshold as ht  # noqa: E402


def test_cap_is_100m():
    assert ht.CAP == 100_000_000
    assert ht.FLOOR == 25_000_000


def test_no_sessions_is_floor():
    t, basis, *_ = ht.compute_threshold({}, 14)
    assert t == ht.FLOOR and basis == "insufficient-data"


def test_below_min_sessions_is_floor():
    t, basis, *_ = ht.compute_threshold({f"s{i}": 10**9 for i in range(ht.MIN_SESSIONS - 1)}, 14)
    assert t == ht.FLOOR and basis == "insufficient-data"


def test_uses_median_not_p80():
    totals = {"a": 1_000, "b": 2_000, "c": 40_000_000, "d": 600_000_000, "e": 700_000_000}
    t, basis, p50, p80, mx, n = ht.compute_threshold(totals, 14)
    assert n == 5 and p50 == 40_000_000 and p80 == 600_000_000
    assert t == 40_000_000
    assert basis.startswith("median of 5 sessions")


def test_median_is_clamped_both_ways():
    low = {f"s{i}": 10 for i in range(ht.MIN_SESSIONS)}
    high = {f"s{i}": 10**10 for i in range(ht.MIN_SESSIONS)}
    assert ht.compute_threshold(low, 14)[0] == ht.FLOOR
    assert ht.compute_threshold(high, 14)[0] == ht.CAP
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_handoff_threshold.py -v`
Expected: `test_cap_is_100m` and `test_uses_median_not_p80` FAIL (CAP is 500M; threshold is 600M).

- [ ] **Step 3: Implement**

Line 18: `CAP = 100_000_000  # the hook's static fallback; a nudge later than this is not a nudge`.

In `compute_threshold`:

```python
    if n < MIN_SESSIONS:
        threshold = FLOOR
        basis = "insufficient-data"
    else:
        # Median, not p80: p80 of CUMULATIVE reads is dominated by the longest sessions, so each
        # long session raised the bar for the next — the nudge receded instead of firing.
        threshold = min(max(p50, FLOOR), CAP)
        basis = f"median of {n} sessions over {days}d (clamped {FLOOR}-{CAP})"
```

`hooks/cache-handoff-nudge.sh:42`: replace `the nightly-computed p80 of per-session` with `the nightly-computed clamped median of per-session`. Re-embed check: `.venv/bin/pytest tests/test_handoff_state.py -q` (pins the hook heredoc; comment edits don't affect it, but verify).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_handoff_threshold.py tests/test_handoff_state.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/handoff_threshold.py hooks/cache-handoff-nudge.sh tests/test_handoff_threshold.py
git commit -m "fix(handoff): threshold is the clamped median (25M-100M), not a self-raising p80"
```

---

### Task 5: Thinking-OFF probe — prove the backend honours `reasoning_effort: "none"`

**Files:**
- Modify: `src/apex_router/ornith/ornith_client.py` (add `thinking_off_probe()` after `readiness()`, extend `__main__`)
- Test: `tests/test_ornith_thinking_probe.py` (create)

**Interfaces:**
- Produces: `thinking_off_probe() -> tuple[bool | None, str]` — sends one `enable_thinking=False` request (`max_tokens=64`, prompt `"Reply exactly: ok"`), returns `(True, "thinking off")` iff `result.reasoning` is falsy (`_parse` already folds `reasoning`, `reasoning_content` and a legacy inline `<think>` block into that field). Otherwise `(False, <why>)` (also for an "empty content" protocol error: thinking likely ON). Never raises: busy/transport/other errors return `(None, "<ExcName>: msg")` = inconclusive.
- CLI: `python -m apex_router.ornith.ornith_client --probe-thinking` prints the reason; exit 0 = thinking off, 1 = evidence of thinking (reasoning, unterminated `<think>`, or empty content), 2 = inconclusive (busy/down/other error).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ornith_thinking_probe.py
"""Every 'thinking-OFF' lane relies on the backend honouring reasoning_effort=none. If an ollama
build or chat template ignores it, every local call is thinking-ON (measured: hangs). The probe
makes that a deterministic check."""
import contextlib
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import apex_router.ornith.ornith_client as oc  # noqa: E402


def _resp(content, reasoning=None, extra=None):
    msg = {"content": content}
    if reasoning is not None:
        msg["reasoning"] = reasoning
    if extra:
        msg.update(extra)
    return {"choices": [{"message": msg, "finish_reason": "stop"}], "usage": {"total_tokens": 3}}


class TestThinkingProbe(unittest.TestCase):
    def setUp(self):
        self._post, self._lock = oc._post, oc.inference_lock
        oc.inference_lock = lambda *a, **k: contextlib.nullcontext()

    def tearDown(self):
        oc._post, oc.inference_lock = self._post, self._lock

    def test_sends_reasoning_effort_none(self):
        seen = {}
        oc._post = lambda p, body, **k: seen.update(body) or _resp("ok")
        ok, why = oc.thinking_off_probe()
        self.assertTrue(ok, why)
        self.assertEqual(seen.get("reasoning_effort"), "none")

    def test_reasoning_field_means_on(self):
        oc._post = lambda *a, **k: _resp("ok", reasoning="let me think")
        ok, why = oc.thinking_off_probe()
        self.assertFalse(ok); self.assertIn("reasoning", why)

    def test_legacy_think_tag_means_on(self):
        # _parse folds a leading <think>…</think> into ChatResult.reasoning (ornith_client.py:153)
        oc._post = lambda *a, **k: _resp("<think>hmm</think>ok")
        ok, why = oc.thinking_off_probe()
        self.assertFalse(ok); self.assertIn("reasoning", why)

    def test_reasoning_content_field_means_on(self):
        # _parse reads `reasoning` or `reasoning_content` (ornith_client.py:152)
        oc._post = lambda *a, **k: _resp("ok", extra={"reasoning_content": "hmm"})
        ok, why = oc.thinking_off_probe()
        self.assertFalse(ok); self.assertIn("reasoning", why)

    def test_transport_error_is_false_not_raise(self):
        def boom(*a, **k): raise oc.OrnithNotListening("down")
        oc._post = boom
        ok, why = oc.thinking_off_probe()
        self.assertFalse(ok); self.assertIn("OrnithNotListening", why)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_ornith_thinking_probe.py -v`
Expected: FAIL — `AttributeError: thinking_off_probe`.

- [ ] **Step 3: Implement**

Check `_parse` (lines 146-160) to see which raw field it reads into `reasoning`; the probe inspects the raw message too so a differently-named field still counts as thinking.

```python
def thinking_off_probe() -> tuple[bool, str]:
    """Prove the backend honours thinking-OFF. Returns (ok, why); never raises.

    Every lane that is 'thinking-OFF' (codegen, review, preread) assumes `reasoning_effort: none`
    is respected. When an ollama build or a chat template ignores it, the model thinks anyway —
    the measured hang/truncation signature — while the lane believes it is in safe mode. Run this
    on any box before trusting the local tier (RUNBOOK-pressure)."""
    try:
        r = chat_messages([{"role": "user", "content": "Reply exactly: ok"}],
                          max_tokens=64, enable_thinking=False, temperature=0.0,
                          raise_on_truncation=False)
    except Exception as e:  # noqa: BLE001 — a probe reports, it never raises
        return False, f"{type(e).__name__}: {e}"
    # _parse already normalises every thinking surface (`reasoning`, `reasoning_content`, a legacy
    # inline <think> block) into ChatResult.reasoning — one check covers them all.
    if r.reasoning:
        return False, f"backend returned reasoning ({len(r.reasoning)} chars) despite reasoning_effort=none: thinking is ON"
    return True, "thinking off"
```

Extend `__main__` (line 228-229):

```python
if __name__ == "__main__":
    if "--probe-thinking" in sys.argv:
        ok, why = thinking_off_probe()
        print(why)
        raise SystemExit(0 if ok else 1)
    print(chat("Reply exactly: Ornith ready", max_tokens=32).answer)
```

(Add `import sys` if absent.)

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_ornith_thinking_probe.py tests/test_ornith_client_lock.py tests/test_ornith_client_truncation.py -v`
Expected: PASS.

- [ ] **Step 5: Document in `docs/RUNBOOK-pressure.md`** — append a section:

```markdown
## Local tier: is thinking really off?

Every local lane sends `reasoning_effort: "none"`. If the serving stack ignores it, every call is
thinking-ON (measured: 0/3 complete, budget burned in `<think>`) and scripts hang up to 900 s.
Check once per box, and after any ollama upgrade:

    .venv/bin/python -m apex_router.ornith.ornith_client --probe-thinking

Exit codes: 0 = thinking off (verified); 1 = evidence thinking is ON (reasoning present, an
unterminated inline `<think>`, or an empty answer because reasoning ate the budget); 2 =
inconclusive (server busy, down, in maintenance, or another error — re-run, don't conclude).
The probe waits at most 10 s for the inference lock.

Related knobs: `ORNITH_LOCK_TIMEOUT_SECS` (default 120) bounds the wait on the inference lock;
`ORNITH_SOCKET_TIMEOUT_SECS` (default 900) bounds one inference.
```

- [ ] **Step 6: Commit**

```bash
git add src/apex_router/ornith/ornith_client.py tests/test_ornith_thinking_probe.py docs/RUNBOOK-pressure.md
git commit -m "feat(ornith): --probe-thinking proves the backend honours reasoning_effort=none"
```

---

### Task 6: CHANGELOG + full suite

**Files:**
- Modify: `CHANGELOG.md` (Unreleased section)

- [ ] **Step 1: Add entries**

```markdown
### Fixed
- ornith: inference lock is bounded (`ORNITH_LOCK_TIMEOUT_SECS`, 120 s) → `OrnithBusy` escalates instead of hanging a turn.
- apex-ornith review: thinking OFF, 1024-token budget, bounded `git diff`.
- pressure: connect-retried-then-succeeded rows no longer count as transport faults (they pushed flaky-link boxes to AMBER/RED and shed work for no upstream reason).
- handoff nudge: threshold is the clamped median (25M–100M) of per-session cache reads, not a p80 that rose with every long session.
### Added
- `python -m apex_router.ornith.ornith_client --probe-thinking` — proves `reasoning_effort: none` is honoured.
```

- [ ] **Step 2: Full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass (baseline 1292 passed, 4 skipped on 2026-08-30; expect +~20).

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for local-handoff hang/over-shed fixes"
```

---

## Not in this plan (needs measurement first)

- **Preread/review anchoring the frontier reviewer (finding 6)** and **weak caller tests passing codegen (7)** are behavioural; the fix is a recall measurement per `docs/RUNBOOK-review-preread.md`, not a code change. Run it on foundry before deciding whether to keep preread on interactive paths.
- **Escalation double-pay (9)** is inherent to the pre-filter design; Task 1-2 stop the worst case (hang + recovery turns), the rest is policy.

## Foundry checks to run before/after (verification, not implementation)

1. `python -m apex_router.ornith.ornith_client --probe-thinking` (Task 5) — exit 1 (thinking evidence) alone explains the hangs; exit 2 is inconclusive (busy/down), re-run.
2. `apex-router pressure` before and after Task 3 — expect `transport` to drop to the SSLError/ReadError rate only.
3. `cat ~/.apex-router/handoff_threshold.json` after the next nightly — expect `basis: median …`.
4. `grep -c '"escalate": true' ~/.apex/offload_telemetry.jsonl` vs total — the share of local calls that still cost a frontier turn.
