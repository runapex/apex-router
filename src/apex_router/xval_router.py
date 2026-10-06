"""Adaptive `codex exec` wrapper for cross-validation runs: classify -> pick an arm by P2C -> run -> learn.

Why: GPT cross-validation cost is driven by how much tool output each run accumulates (rg/nl/cat/sed
output re-sent on every later call: 54% of all prompt tokens over 1,449 runs) and by runs that never
finish (40% of report reviews ended with no final answer — killed mid-exploration, all cost, no
value). Two knobs move that: Codex's per-tool-output cap (`tool_output_token_limit`, verified to
truncate head+tail) and a scoping hint in the prompt. The right setting differs by task kind, so it is
LEARNED per category instead of fixed.

  classify(argv)  -> category: diff | report | files | investigate (deterministic rules)
  ARMS            -> (tool_output_token_limit, scope) grid; BASELINE = today's behavior (10000, open)
  select()        -> P2C over arms with running probabilities:
                       pi_c(a) = softmax(E[U_a] / T) — running probability of each arm in category c
                       draw two distinct arms from pi_c, keep the one with the higher Thompson sample
                       U = p_ok~Beta(alpha,beta) - LAMBDA * cost_a / cost_baseline
  update()        -> discounted (GAMMA per observation) Beta success counts + discounted mean cost,
                     so the estimates track drift (model/gateway changes) instead of freezing.

Reward signal (from the Codex rollout, no LLM judge): ok = the run ended with a non-empty final
message; cost = effective tokens (uncached + 0.1*cached + 4*output — the GPT price ratios, so only the
RATIO between arms matters, not the dollar rate). `apex-router xval feedback <run_id> bad` overrides
`ok` when the caller judged the review useless.

State: ~/.apex-router/xval_bandit.json (flock'd), run log ~/.apex-router/xval_runs.jsonl.
Fail-open: any bandit/IO error runs the BASELINE arm; the wrapped codex exit code is returned as-is.
"""
from __future__ import annotations

import fcntl
import glob
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

CATEGORIES = ("diff", "report", "files", "investigate")
LIMITS = (2000, 4000, 6000, 10000)
SCOPES = ("open", "focused")
ARMS = tuple(f"{lim}/{scope}" for lim in LIMITS for scope in SCOPES)
BASELINE = "10000/open"

GAMMA = 0.97     # per-observation discount within a category (effective window ~33 runs)
LAMBDA = 0.5     # utility weight of relative cost vs success probability
TEMP = 0.15      # softmax temperature for the running probabilities pi_c
PRIOR_OK = (3.0, 1.0)        # Beta prior for a non-baseline arm (optimistic, weak)
PRIOR_OK_BASE = (19.0, 1.0)  # baseline starts trusted (~half the cold-start picks): cheaper arms EARN their place
FOCUS_HINT = (
    "\n\nEvidence budget: inspect only what the claim under review needs. Prefer `rg -n` with path "
    "filters and `sed -n 'START,ENDp'` ranges over whole-file `cat`/`nl`; never re-read a file region "
    "you have already seen; stop exploring once the verdict is supported and answer."
)

_REVIEW_FLAGS = ("--uncommitted", "--base", "--commit")
_FILE_RE = re.compile(r"(?:^|[\s'\"(])(?:\.{0,2}/)?[\w.-]+(?:/[\w.-]+)*\.(?:py|rb|ts|tsx|js|jsx|go|rs|java|kt|c|cc|cpp|h|hpp|sh|ya?ml|toml|sql)\b")


def home() -> Path:
    return Path(os.environ.get("APEX_ROUTER_HOME", Path.home() / ".apex-router"))


# --------------------------------------------------------------------------- classify

# `codex exec` / `codex exec review` options that take a value (codex-cli 0.146 --help). `-i` takes
# one or more files; a prompt placed after it must follow `--` (Codex's own rule), so one is consumed.
_VALUE_OPTS = {"-c", "--config", "--enable", "--disable", "-i", "--image", "-m", "--model",
               "--local-provider", "-p", "--profile", "-s", "--sandbox", "-C", "--cd", "--add-dir",
               "--output-schema", "--color", "-o", "--output-last-message", "--base", "--commit",
               "--title"}


def _prompt_index(argv: list[str]) -> int | None:
    """Index of the positional prompt (the last positional that is not the `review` subcommand)."""
    idx, i, after_dd = None, 0, False
    while i < len(argv):
        a = argv[i]
        if after_dd:
            idx = i
        elif a == "--":
            after_dd = True
        elif a in ("-i", "--image"):
            # Takes one OR MORE files: every following non-option token is an image until `--`,
            # so no positional prompt can be identified here (Codex requires `--` before it).
            i += 1
            while i < len(argv) and not argv[i].startswith("-"):
                i += 1
            continue
        elif a in _VALUE_OPTS:
            i += 1  # skip the option's value
        elif a.startswith("-"):
            pass    # flag, or --opt=value
        elif not (a == "review" and idx is None):
            idx = i
        i += 1
    return idx


def _prompt_of(argv: list[str]) -> str:
    i = _prompt_index(argv)
    return argv[i] if i is not None else ""


def classify(argv: list[str], stdin_text: str = "") -> str:
    if "review" in argv[:3] or any(f in argv for f in _REVIEW_FLAGS):
        return "diff"
    t = (_prompt_of(argv) + "\n" + stdin_text)[:20000]
    if re.search(r"\b(git diff|this diff|the diff|patch|uncommitted|staged)\b", t, re.I):
        return "diff"
    # A written artifact under review: report/analysis wording AND a document (or the word report).
    if re.search(r"\b(report|analysis|disposition|write-?up|claims?|refute)\b", t, re.I) and \
            re.search(r"\.(md|html|pdf)\b|\breport\b|\banalysis\b", t, re.I):
        return "report"
    if _FILE_RE.search(t):
        return "files"
    return "investigate"


# --------------------------------------------------------------------------- bandit state

def _fresh_arm(arm: str) -> dict:
    a, b = PRIOR_OK_BASE if arm == BASELINE else PRIOR_OK
    return {"alpha": a, "beta": b, "cost": None, "w": 0.0, "n": 0}


def _fresh_state() -> dict:
    return {"version": 1, "categories": {c: {"arms": {a: _fresh_arm(a) for a in ARMS}} for c in CATEGORIES}}


@contextmanager
def _locked_state(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(path.read_text())
            except (OSError, ValueError):
                state = _fresh_state()
            for c in CATEGORIES:  # schema-forward: new categories/arms appear with priors
                cat = state.setdefault("categories", {}).setdefault(c, {"arms": {}})
                for a in ARMS:
                    cat["arms"].setdefault(a, _fresh_arm(a))
            yield state
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
            os.replace(tmp, path)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _baseline_cost(cat: dict) -> float | None:
    """Reference cost for the category: the baseline arm's running cost, else the median of known."""
    b = cat["arms"][BASELINE]["cost"]
    if b:
        return b
    known = sorted(x["cost"] for x in cat["arms"].values() if x["cost"])
    return known[len(known) // 2] if known else None


def _rel_cost(cat: dict, arm: str) -> float:
    ref = _baseline_cost(cat)
    c = cat["arms"][arm]["cost"]
    if not ref or not c:
        return 1.0  # unknown cost: assume baseline-equal (no free lunch for unexplored arms)
    return c / ref


def probabilities(cat: dict) -> dict[str, float]:
    """Running probabilities pi_c(a): softmax of each arm's expected utility."""
    u = {}
    for a, x in cat["arms"].items():
        p_ok = x["alpha"] / (x["alpha"] + x["beta"])
        u[a] = p_ok - LAMBDA * _rel_cost(cat, a)
    m = max(u.values())
    e = {a: math.exp((v - m) / TEMP) for a, v in u.items()}
    z = sum(e.values())
    return {a: v / z for a, v in e.items()}


def select(cat: dict, rng: random.Random | None = None) -> tuple[str, dict]:
    """P2C: draw two distinct arms from pi_c, keep the higher Thompson-sampled utility."""
    rng = rng or random.Random()
    pi = probabilities(cat)
    arms = list(pi)
    first = rng.choices(arms, weights=[pi[a] for a in arms])[0]
    rest = [a for a in arms if a != first]
    second = rng.choices(rest, weights=[pi[a] for a in rest])[0]

    def sample(a: str) -> float:
        x = cat["arms"][a]
        return rng.betavariate(x["alpha"], x["beta"]) - LAMBDA * _rel_cost(cat, a)

    s1, s2 = sample(first), sample(second)
    pick = first if s1 >= s2 else second
    return pick, {"candidates": [first, second], "samples": [round(s1, 4), round(s2, 4)],
                  "pi": {a: round(pi[a], 4) for a in (first, second)}}


def update(cat: dict, arm: str, ok: bool, cost: float | None) -> None:
    """Discount every arm in the category (time passes for all), then credit the observed one."""
    cat["obs"] = cat.get("obs", 0) + 1
    for a, x in cat["arms"].items():
        pa, pb = PRIOR_OK_BASE if a == BASELINE else PRIOR_OK
        x["alpha"] = pa + GAMMA * (x["alpha"] - pa)  # decay toward the prior, not toward zero
        x["beta"] = pb + GAMMA * (x["beta"] - pb)
        x["w"] *= GAMMA
    x = cat["arms"][arm]
    x["alpha" if ok else "beta"] += 1.0
    x["n"] += 1
    if cost is not None and cost > 0:
        # Discounted running mean: weight w is the decayed observation mass.
        x["cost"] = cost if not x["cost"] or x["w"] <= 0 else (x["cost"] * x["w"] + cost) / (x["w"] + 1.0)
        x["w"] += 1.0


# --------------------------------------------------------------------------- rollout metrics

def find_rollout(session_id: str) -> Path | None:
    hits = glob.glob(str(Path.home() / ".codex" / "sessions" / "**" / f"rollout-*{session_id}.jsonl"), recursive=True)
    return Path(max(hits, key=os.path.getmtime)) if hits else None


def rollout_metrics(path: Path) -> dict:
    total, calls, ok, truncated, model = None, 0, False, 0, None
    for line in path.open():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t, p = d.get("type"), d.get("payload") or {}
        if t == "turn_context":
            model = p.get("model") or model
        elif t == "event_msg" and p.get("type") == "token_count" and (p.get("info") or {}).get("total_token_usage"):
            total = p["info"]["total_token_usage"]
            calls += 1
        elif t == "event_msg" and p.get("type") == "task_complete":
            ok = bool(p.get("last_agent_message"))
        elif t == "response_item" and p.get("type") in ("function_call_output", "custom_tool_call_output"):
            o = p.get("output")
            if isinstance(o, str) and "Warning: truncated output" in o[:400]:
                truncated += 1
    m = {"model": model, "calls": calls, "ok": ok, "truncated_outputs": truncated}
    if total:
        cached = total.get("cached_input_tokens", 0)
        m.update(input=total.get("input_tokens", 0), cached=cached, output=total.get("output_tokens", 0))
        m["cost"] = (m["input"] - cached) + 0.1 * cached + 4.0 * m["output"]
    return m


# --------------------------------------------------------------------------- run

def build_command(argv: list[str], arm: str, stdin_text: str = "",
                  codex_bin: str = "codex") -> tuple[list[str], str]:
    """(command, stdin) for an arm. The focus hint goes on the prompt — the positional argument, or
    stdin when the prompt is `-`/piped. `review` mode only gets the output cap (its prompt is Codex's)."""
    limit, scope = arm.split("/")
    cmd = [codex_bin, "exec", "-c", f"tool_output_token_limit={int(limit)}"]
    rest = list(argv)
    if scope == "focused" and "review" not in rest[:3]:
        idx = _prompt_index(rest)
        if idx is not None and rest[idx] != "-":
            rest[idx] = rest[idx] + FOCUS_HINT
        elif stdin_text.strip():
            stdin_text = stdin_text + FOCUS_HINT
    return cmd + rest, stdin_text


_SID_RE = re.compile(r"session id:\s*([0-9a-f-]{36})")


def run(argv: list[str], *, dry_run: bool = False, rng: random.Random | None = None) -> int:
    stdin_text = ""
    if not sys.stdin.isatty():
        try:
            stdin_text = sys.stdin.read()
        except OSError:
            stdin_text = ""
    category = classify(argv, stdin_text)
    run_id = uuid.uuid4().hex[:12]
    state_path = home() / "xval_bandit.json"
    try:
        with _locked_state(state_path) as st:
            arm, why = select(st["categories"][category], rng)
    except Exception as exc:  # noqa: BLE001 — bandit failure never blocks a review
        arm, why = BASELINE, {"fallback": type(exc).__name__}
    cmd, stdin_text = build_command(argv, arm, stdin_text, os.environ.get("CODEX_BIN", "codex"))
    print(f"[xval {run_id}] category={category} arm={arm} {json.dumps(why)}", file=sys.stderr)
    if dry_run:
        print(json.dumps({"run_id": run_id, "category": category, "arm": arm, "cmd": cmd}))
        return 0

    t0 = time.time()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=sys.stdout, stderr=subprocess.PIPE, text=True)

    def _feed() -> None:  # in a thread: a child that fills stderr before reading stdin can't deadlock us
        try:
            proc.stdin.write(stdin_text)
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    import threading
    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    sid = None
    for line in proc.stderr:  # tee stderr, sniff the session id
        sys.stderr.write(line)
        if sid is None:
            m = _SID_RE.search(line)
            sid = m.group(1) if m else None
    rc = proc.wait()
    feeder.join(timeout=5)

    metrics = {}
    path = find_rollout(sid) if sid else None
    if path:
        try:
            metrics = rollout_metrics(path)
        except OSError:
            metrics = {}
    ok = bool(metrics.get("ok")) and rc == 0
    row = {"ts": t0, "run_id": run_id, "session_id": sid, "category": category, "arm": arm,
           "rc": rc, "seconds": round(time.time() - t0, 1), **metrics,
           "rollout_ok": metrics.get("ok"), "ok": ok}  # `ok` = exactly what the bandit learned
    try:
        if metrics:  # never learn from a run we could not measure
            with _locked_state(state_path) as st:
                cat = st["categories"][category]
                update(cat, arm, ok, metrics.get("cost"))
                row["obs"] = cat["obs"]  # this observation's index in the category (for feedback decay)
        with open(home() / "xval_runs.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:  # noqa: BLE001
        pass
    print(f"[xval {run_id}] ok={ok} calls={metrics.get('calls')} cost={metrics.get('cost')}", file=sys.stderr)
    return rc


def feedback(run_id: str, verdict: str) -> int:
    """Override a logged run's `ok` (the caller judged the review useless/useful)."""
    log = home() / "xval_runs.jsonl"
    good = verdict == "ok"
    # Read the log, correct the state and append the feedback row under ONE lock, so two concurrent
    # feedbacks for the same run can't both see the old verdict and both apply the correction.
    with _locked_state(home() / "xval_bandit.json") as st:
        rows = [json.loads(x) for x in log.read_text().splitlines() if x.strip()] if log.exists() else []
        row = next((r for r in rows if r.get("run_id") == run_id and "arm" in r), None)  # the run
        if row is None:
            print(f"xval: unknown run_id {run_id}", file=sys.stderr)
            return 2
        if "obs" not in row:  # the run was never learned from (unmeasured) — nothing to correct
            return 0
        current = row["ok"]  # what the bandit holds: the run's ok, as revised by later feedback
        for r in rows:
            if r.get("run_id") == run_id and "feedback" in r:
                current = r["feedback"] == "ok"
        if current == good:
            return 0
        cat = st["categories"][row["category"]]
        x = cat["arms"][row["arm"]]
        # The observation has decayed by GAMMA per later category observation: move only what is left.
        w = GAMMA ** max(0, cat.get("obs", row["obs"]) - row["obs"])
        src, dst = ("alpha", "beta") if not good else ("beta", "alpha")
        x[src] = max(0.0, x[src] - w)
        x[dst] += w
        with open(log, "a") as f:
            f.write(json.dumps({"ts": time.time(), "run_id": run_id, "feedback": verdict}) + "\n")
    return 0


def stats() -> int:
    with _locked_state(home() / "xval_bandit.json") as st:
        for c in CATEGORIES:
            cat = st["categories"][c]
            pi = probabilities(cat)
            n = sum(x["n"] for x in cat["arms"].values())
            print(f"{c}  (runs {n})")
            for a in sorted(ARMS, key=lambda a: -pi[a]):
                x = cat["arms"][a]
                p_ok = x["alpha"] / (x["alpha"] + x["beta"])
                cost = f"{x['cost'] / 1e3:7.0f}k" if x["cost"] else "      ?"
                print(f"   {a:14} pi={pi[a]:.3f}  p_ok={p_ok:.2f}  cost={cost}  rel={_rel_cost(cat, a):.2f}  n={x['n']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["stats"]:
        return stats()
    if argv[:1] == ["feedback"] and len(argv) == 3 and argv[2] in ("ok", "bad"):
        return feedback(argv[1], argv[2])
    dry = False
    if argv[:1] == ["--dry-run"]:
        dry, argv = True, argv[1:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print("usage: apex-router xval [--dry-run] <codex exec args…> | stats | feedback <run_id> ok|bad",
              file=sys.stderr)
        return 2
    return run(argv, dry_run=dry)


if __name__ == "__main__":
    raise SystemExit(main())
