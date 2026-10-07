"""Task outcome labels at the scale of the logs (P0 of PLAN-workflow-graph-optimizer.md).

A **task** is one user turn in a main session (pi or Claude Code) that ran at least one tool. Its
outcome is one of ``success`` / ``partial`` / ``fail`` / ``unknown``.

Labels come from weak signals, never from one guess:

- **labeling functions (LFs)**: deterministic rules over the task's own tool calls and the user's
  NEXT message — tests passed / failed after the last edit, a commit or push, the next message
  negative / positive, an interrupt, the same request repeated, errors at the tail. Each LF votes
  or abstains.
- **judge**: a local model (ollama, never leaves the machine) reads the request, the last
  assistant message, the tool summary and the next user message, and votes with a confidence.
- **gold**: tasks the user labels by hand (``apex-router labels review``), sampled stratified by
  source x weak label x confidence plus the cases where the signals disagree.

The label model weights each voter by its accuracy MEASURED on the gold set (a Beta(2,2) prior
until there is gold) and emits a label only when the posterior clears ``EMIT_MIN`` — coverage is
traded for precision, not the other way round. ``labels report`` prints coverage and, per voter
and per label, precision on gold with a Wilson interval, so the cost of every extra label is
visible.

Privacy: ``tasks.jsonl`` / ``labels.jsonl`` / ``gold.jsonl`` under ``~/.apex-router/labels/``
store ids, counts, votes and labels — no prompt, command or file text. Text is read from the
transcripts at the moment it is needed (judge, review) and not kept.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

OUTCOMES = ("success", "partial", "fail")
EMIT_MIN = 0.80                 # posterior needed to emit a label (else "unknown")
GOLD_FRACTION = 0.10            # gold grows with the data: 10% of tasks, at least GOLD_MIN
GOLD_MIN = 100
JUDGE_MODEL = os.environ.get("APEX_LABEL_JUDGE", "qwen3.8:27b-mlx")
JUDGE_URL = os.environ.get("APEX_LABEL_JUDGE_URL", "http://127.0.0.1:11434/api/chat")
CLIP = {"request": 700, "last": 900, "next": 400}


def home() -> Path:
    return Path(os.environ.get("APEX_ROUTER_HOME") or Path.home() / ".apex-router") / "labels"


# ---- extraction -----------------------------------------------------------------------------

def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


# Injected into the user role by the harness, not typed by the user: a skill's body, slash-command
# echoes, local-command output, notifications, a message relayed from another session. Counting
# them as requests splits real tasks and hands the judge a fake "next message".
_SKIP_USER = ("<command-", "<local-command", "Caveat:", "<system", "<task-notification",
              "Base directory for this skill:", "Another Claude session sent a message",
              "[Image #", "<bash-")


def _tid(path: Path, i: int) -> str:
    return hashlib.sha1(f"{path.name}:{i}".encode()).hexdigest()[:12]


def extract(path: Path) -> list[dict]:
    """Tasks of one transcript, in order. Each: id, source, session, index, ts, calls
    [(tool, command, is_error, result_head)], last assistant text, request, next request."""
    source = "pi" if "/.pi/" in str(path) else "claude"
    tasks: list[dict] = []
    cur = None
    by_call: dict = {}
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("message") if isinstance(d.get("message"), dict) else {}
            role = m.get("role") or d.get("type")
            c = m.get("content")
            if role == "user":
                if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                               for b in c):
                    for b in c:                                   # Claude Code tool results
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            call = by_call.get(b.get("tool_use_id"))
                            if call is not None:
                                call[2] = bool(b.get("is_error"))
                                call[3] = _text(b.get("content"))[-2000:] if not isinstance(
                                    b.get("content"), str) else b["content"][-2000:]
                    continue
                t = _text(c).strip()
                if not t or t.startswith(_SKIP_USER):
                    continue
                cur = {"request": t, "calls": [], "last": "", "ts": d.get("timestamp")}
                tasks.append(cur)
            elif role == "assistant" and cur is not None and isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") in ("toolCall", "tool_use"):
                        a = b.get("arguments") or b.get("input") or {}
                        cmd = a.get("command", "") if isinstance(a, dict) else ""
                        call = [b.get("name"), cmd if isinstance(cmd, str) else "", False, ""]
                        cur["calls"].append(call)
                        by_call[b.get("id")] = call
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        cur["last"] = b["text"]
            elif role == "toolResult" and cur is not None:              # pi
                call = by_call.get(m.get("toolCallId"))
                if call is not None:
                    call[2] = m.get("isError") in (True, "True", "true")
                    call[3] = _text(c)[-2000:]
    out = []
    for i, t in enumerate(tasks):
        if not t["calls"]:
            continue
        t.update(id=_tid(path, i), source=source, session=path.stem[-36:], index=i,
                 path=str(path), next=tasks[i + 1]["request"] if i + 1 < len(tasks) else None)
        out.append(t)
    return out


def transcripts() -> list[Path]:
    h = Path.home()
    return sorted(list((h / ".pi" / "agent" / "sessions").glob("**/*.jsonl"))
                  + list((h / ".claude" / "projects").glob("*/*.jsonl")))


def all_tasks() -> list[dict]:
    return [t for p in transcripts() for t in extract(p)]


# ---- labeling functions ---------------------------------------------------------------------
# Each returns "success" / "partial" / "fail" or None (abstain). They see one task.

TEST_CMD = re.compile(r"\b(pytest|rspec|go test|cargo test|npm (run )?test|make test|unittest)\b")
_PYTEST = re.compile(r"(\d+) failed|(\d+) error", re.IGNORECASE)
_PASS = re.compile(r"\b(\d+) passed\b|\b(\d+) examples?, 0 failures\b|\bOK \(\d+ tests?\)", re.IGNORECASE)
_FAIL = re.compile(r"\b[1-9]\d* (failed|errors?)\b|\b\d+ examples?, [1-9]\d* failures?\b|"
                   r"\bFAILED\b", re.IGNORECASE)
EDIT_TOOLS = {"edit", "write", "Edit", "Write", "MultiEdit", "NotebookEdit"}
COMMIT = re.compile(r"\bgit (commit|push)\b")
NEG = re.compile(r"^\s*(no\b|nope|wrong|doesn'?t work|didn'?t work|still (broken|fail|not|doesn)|"
                 r"not working|revert|undo|that'?s not|you broke|it'?s broken|why did you|"
                 r"you (didn'?t|forgot|missed)|i don'?t see|that is wrong|this is wrong)", re.IGNORECASE)
POS = re.compile(r"^\s*(thanks|thank you|great|perfect|nice|lgtm|awesome|works\b|good job|"
                 r"(yes|ok|okay)[,.!]?\s*(push|commit|go ahead|do it|apply|proceed)|"
                 r"push( changes)?\b|commit( and push)?\b)", re.IGNORECASE)
INTERRUPT = re.compile(r"^\[Request interrupted", re.IGNORECASE)


def _tokens(s: str) -> set:
    return set(re.findall(r"[a-z0-9]{3,}", (s or "").lower()))


def lf_tests(t):
    """The LAST test run after the last edit: passed -> success, failed -> fail."""
    last_edit = max((i for i, c in enumerate(t["calls"]) if c[0] in EDIT_TOOLS), default=-1)
    runs = [c for i, c in enumerate(t["calls"]) if i > last_edit and TEST_CMD.search(c[1] or "")]
    if not runs:
        return None
    out = runs[-1][3] or ""
    if _FAIL.search(out) or runs[-1][2]:
        return "fail"
    if _PASS.search(out):
        return "success"
    return None


def lf_commit(t):
    cmds = "\n".join(c[1] or "" for c in t["calls"])
    return "success" if COMMIT.search(cmds) and not NEG.search(t.get("next") or "") else None


def lf_next_negative(t):
    return "fail" if NEG.search(t.get("next") or "") else None


def lf_next_positive(t):
    return "success" if POS.search(t.get("next") or "") else None


def lf_interrupted(t):
    return "fail" if INTERRUPT.search(t.get("next") or "") else None


def lf_repeated(t):
    """The next request restates this one (Jaccard >= 0.6 on content words): it did not land."""
    a, b = _tokens(t["request"]), _tokens(t.get("next") or "")
    if len(a) < 4 or len(b) < 4:
        return None
    return "fail" if len(a & b) / len(a | b) >= 0.6 else None


def lf_tail_errors(t):
    tail = t["calls"][-3:]
    return "fail" if len(tail) == 3 and all(c[2] for c in tail) else None


LFS = {f.__name__[3:]: f for f in (lf_tests, lf_commit, lf_next_negative, lf_next_positive,
                                   lf_interrupted, lf_repeated, lf_tail_errors)}


def votes(t) -> dict:
    out = {}
    for name, f in LFS.items():
        try:
            v = f(t)
        except Exception:  # noqa: BLE001 — a rule that cannot read a task abstains
            v = None
        if v:
            out[name] = v
    return out


# ---- judge ----------------------------------------------------------------------------------

JUDGE_PROMPT = """You label the OUTCOME of one task an AI coding agent did for a user.
Use only the evidence below. Decide:
- "success": the request was done and the user accepted it or moved on to a follow-up that
  builds on it
- "partial": some of it was done, or it was done with problems the user had to point out
- "fail": not done, wrong, or the user had to repeat / correct / revert it
- "unknown": the evidence does not show which (e.g. the session ended, or the next message
  starts an unrelated topic without saying anything about this one)
Answer JSON only: {"outcome": "...", "confidence": 0.0-1.0, "evidence": "next_message|tests|
final_message|tools|none"}

USER REQUEST:
<<<{request}>>>

TOOLS: {tools}

AGENT'S FINAL MESSAGE:
<<<{last}>>>

USER'S NEXT MESSAGE (none = session ended):
<<<{next}>>>"""


def _tool_summary(t) -> str:
    n = Counter(c[0] for c in t["calls"])
    errs = sum(1 for c in t["calls"] if c[2])
    tests = lf_tests(t)
    bits = [", ".join(f"{k} {v}" for k, v in n.most_common(5)), f"errors {errs}"]
    if tests:
        bits.append(f"last test run after last edit: {'passed' if tests == 'success' else 'failed'}")
    if COMMIT.search("\n".join(c[1] or "" for c in t["calls"])):
        bits.append("committed/pushed")
    return " · ".join(bits)


def judge(t, url: str = JUDGE_URL, model: str = JUDGE_MODEL, timeout: float = 180) -> dict | None:
    prompt = JUDGE_PROMPT                       # replace(), not format(): the prompt has JSON braces
    for k, v in (("{request}", t["request"][:CLIP["request"]]), ("{tools}", _tool_summary(t)),
                 ("{last}", (t["last"] or "")[-CLIP["last"]:]),
                 ("{next}", (t.get("next") or "none")[:CLIP["next"]])):
        prompt = prompt.replace(k, v)
    body = json.dumps({"model": model, "stream": False, "think": False, "format": "json",
                       "options": {"temperature": 0, "num_ctx": 8192},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(json.loads(r.read())["message"]["content"])
    except Exception:  # noqa: BLE001 — a judge failure is an abstention
        return None
    o = out.get("outcome")
    conf = out.get("confidence")
    if o not in OUTCOMES + ("unknown",) or not isinstance(conf, (int, float)):
        return None
    return {"outcome": o, "confidence": max(0.0, min(1.0, float(conf))),
            "evidence": str(out.get("evidence", ""))[:20], "model": model}


# ---- label model ----------------------------------------------------------------------------

def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    """95% Wilson interval for k successes in n."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def voter_accuracy(rows: list, gold: dict) -> dict:
    """Per voter: (correct, total) on gold tasks where it voted. The judge votes only above
    ``JUDGE_MIN_CONF``; its "unknown" is an abstention."""
    acc: dict = {}
    for r in rows:
        g = gold.get(r["id"])
        if g not in OUTCOMES:
            continue
        for name, v in _all_votes(r).items():
            k, n = acc.get(name, (0, 0))
            acc[name] = (k + (v == g), n + 1)
    return acc


JUDGE_MIN_CONF = 0.6


def _all_votes(r) -> dict:
    v = dict(r.get("votes") or {})
    j = r.get("judge")
    if isinstance(j, dict) and j.get("outcome") in OUTCOMES and j.get("confidence", 0) >= JUDGE_MIN_CONF:
        v["judge"] = j["outcome"]
    return v


def posterior(r, acc: dict) -> tuple:
    """Naive-Bayes combination: each voter with accuracy a (Beta(2,2)-smoothed on gold) multiplies
    its label by a and the others by (1-a)/2. Uniform prior. -> (label, probability)."""
    vs = _all_votes(r)
    if not vs:
        return ("unknown", 0.0)
    logp = {o: 0.0 for o in OUTCOMES}
    for name, v in vs.items():
        k, n = acc.get(name, (0, 0))
        a = min(0.99, max(0.34, (k + 2) / (n + 4)))
        for o in OUTCOMES:
            logp[o] += math.log(a if o == v else (1 - a) / 2)
    m = max(logp.values())
    z = sum(math.exp(x - m) for x in logp.values())
    best = max(logp, key=logp.get)
    return (best, math.exp(logp[best] - m) / z)


# ---- store ----------------------------------------------------------------------------------

def _read(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    for ln in p.read_text().splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def _write(p: Path, rows: list) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def _row(t) -> dict:
    """What is stored: ids, counts, votes — no text."""
    return {"id": t["id"], "source": t["source"], "session": t["session"], "index": t["index"],
            "ts": t.get("ts"), "n_calls": len(t["calls"]),
            "n_errors": sum(1 for c in t["calls"] if c[2]), "has_next": t.get("next") is not None,
            "votes": votes(t)}


def build(judge_limit: int = 0, judge_fn=judge, log=print) -> dict:
    """Extract every task, apply the LFs, run the judge on up to ``judge_limit`` tasks not judged
    yet (newest first), combine with gold accuracies, write tasks/labels. Idempotent."""
    h = home()
    old = {r["id"]: r for r in _read(h / "tasks.jsonl")}
    tasks = all_tasks()
    rows = []
    todo = []
    for t in tasks:
        r = _row(t)
        if old.get(t["id"], {}).get("judge"):
            r["judge"] = old[t["id"]]["judge"]
        elif judge_limit:
            todo.append((t, r))
        rows.append(r)
    todo.sort(key=lambda x: str(x[0].get("ts") or ""), reverse=True)
    t0 = time.time()
    for i, (t, r) in enumerate(todo[:judge_limit]):
        j = judge_fn(t)
        if j:
            r["judge"] = j
        if log and (i + 1) % 25 == 0:
            log(f"  judged {i + 1}/{min(judge_limit, len(todo))} ({time.time() - t0:.0f}s)")
            _write(h / "tasks.jsonl", rows)                   # checkpoint
    _write(h / "tasks.jsonl", rows)
    return relabel(rows)


def relabel(rows=None) -> dict:
    h = home()
    rows = rows if rows is not None else _read(h / "tasks.jsonl")
    gold = {g["id"]: g["outcome"] for g in _read(h / "gold.jsonl")}
    acc = voter_accuracy(rows, gold)
    labels = []
    for r in rows:
        lab, p = posterior(r, acc)
        src = "gold" if r["id"] in gold else "weak"
        if src == "gold":
            lab, p = gold[r["id"]], 1.0
        labels.append({"id": r["id"], "source": r["source"], "session": r["session"],
                       "label": lab if p >= EMIT_MIN or src == "gold" else "unknown",
                       "p": round(p, 3), "from": src, "voters": sorted(_all_votes(r))})
    _write(h / "labels.jsonl", labels)
    return {"tasks": len(rows), "labels": labels, "acc": acc, "gold": gold}


# ---- gold sampling + review -----------------------------------------------------------------

def gold_target(n_tasks: int) -> int:
    return max(GOLD_MIN, math.ceil(GOLD_FRACTION * n_tasks))


def sample_for_review(rows: list, labels: list, gold: dict, k: int, seed: int = 7) -> list:
    """Stratified by (source, weak label, confidence band), plus every disagreement between
    voters first — those are where a gold label moves the voter accuracies most."""
    lab = {x["id"]: x for x in labels}
    pool = [r for r in rows if r["id"] not in gold]
    dis = [r for r in pool if len(set(_all_votes(r).values())) > 1]
    rng = random.Random(seed)
    rng.shuffle(dis)
    strata: dict = {}
    for r in pool:
        x = lab.get(r["id"], {})
        band = "hi" if x.get("p", 0) >= EMIT_MIN else "lo" if _all_votes(r) else "none"
        strata.setdefault((r["source"], x.get("label"), band), []).append(r)
    for v in strata.values():
        rng.shuffle(v)
    picked, seen = [], set()
    for r in dis[: k // 3]:
        picked.append(r)
        seen.add(r["id"])
    keys = sorted(strata)
    while len(picked) < k and any(strata[s] for s in keys):
        for s in keys:
            while strata[s] and strata[s][-1]["id"] in seen:
                strata[s].pop()
            if strata[s] and len(picked) < k:
                r = strata[s].pop()
                picked.append(r)
                seen.add(r["id"])
    return picked


def review(k: int = 20, inp=input, out=print) -> int:
    """Show k sampled tasks; the user types s(uccess) / p(artial) / f(ail) / u(nknown) /
    q(uit). Text is read from the transcript now and not stored."""
    h = home()
    rows = _read(h / "tasks.jsonl")
    labels = _read(h / "labels.jsonl")
    gold_rows = _read(h / "gold.jsonl")
    gold = {g["id"]: g["outcome"] for g in gold_rows}
    by_session = {}
    for p in transcripts():
        by_session.setdefault(p.stem[-36:], p)
    keys = {"s": "success", "p": "partial", "f": "fail", "u": "unknown"}
    done = 0
    for r in sample_for_review(rows, labels, gold, k):
        p = by_session.get(r["session"])
        t = next((x for x in extract(p) if x["id"] == r["id"]), None) if p else None
        if not t:
            continue
        out("\n" + "=" * 100)
        out(f"[{done + 1}/{k}] {r['source']} · {len(t['calls'])} tool calls · {_tool_summary(t)}")
        out(f"REQUEST: {t['request'][:CLIP['request']]}")
        out(f"FINAL:   {(t['last'] or '')[-CLIP['last']:]}")
        out(f"NEXT:    {(t.get('next') or '(session ended)')[:CLIP['next']]}")
        a = (inp("outcome [s/p/f/u, q quits]: ") or "").strip().lower()[:1]
        if a == "q":
            break
        if a in keys:
            gold_rows.append({"id": r["id"], "outcome": keys[a], "ts": time.time(),
                              "by": "user"})
            _write(h / "gold.jsonl", gold_rows)
            done += 1
    relabel()
    return done


# ---- report ---------------------------------------------------------------------------------

def report(res: dict | None = None) -> str:
    h = home()
    if res is None:
        res = relabel()
    labels, acc, gold = res["labels"], res["acc"], res["gold"]
    n = len(labels)
    emitted = [x for x in labels if x["label"] != "unknown"]
    lines = [(f"tasks {n} · labeled {len(emitted)} ({100 * len(emitted) / max(n, 1):.0f}%) · "
              f"gold {len(gold)} / target {gold_target(n)}")]
    by = Counter(x["label"] for x in labels)
    lines.append("labels: " + ", ".join(f"{k} {by[k]}" for k in OUTCOMES + ("unknown",)))
    cov = Counter(v for x in labels for v in x["voters"])
    lines.append("voter            votes  coverage   gold n  accuracy  95% CI")
    for name in list(LFS) + ["judge"]:
        k, m = acc.get(name, (0, 0))
        lo, hi = wilson(k, m)
        a = f"{k / m:.2f}" if m else "  – "
        lines.append(f"  {name:15} {cov[name]:5d}  {100 * cov[name] / max(n, 1):6.1f}%  {m:7d}  "
                     f"{a:>8}  [{lo:.2f}, {hi:.2f}]")
    # precision of EMITTED weak labels, measured on gold that the weak model also labeled
    wk = {x["id"]: x for x in labels}
    rows = _read(h / "tasks.jsonl")
    acc_wo = {}
    hit = tot = 0
    per = Counter()
    per_n = Counter()
    for r in rows:
        g = gold.get(r["id"])
        if g not in OUTCOMES:
            continue
        # leave-one-out: the voter accuracies without this gold task
        acc_wo = voter_accuracy([x for x in rows if x["id"] != r["id"] and x["id"] in gold],
                                {k: v for k, v in gold.items() if k != r["id"]})
        lab, p = posterior(r, acc_wo)
        if p >= EMIT_MIN:
            tot += 1
            hit += lab == g
            per_n[lab] += 1
            per[lab] += lab == g
    if tot:
        lo, hi = wilson(hit, tot)
        lines.append(f"emitted-label precision on gold (leave-one-out): {hit}/{tot} = "
                     f"{hit / tot:.2f} [{lo:.2f}, {hi:.2f}]")
        for o in OUTCOMES:
            if per_n[o]:
                lo, hi = wilson(per[o], per_n[o])
                lines.append(f"  {o:8} {per[o]}/{per_n[o]} = {per[o] / per_n[o]:.2f} "
                             f"[{lo:.2f}, {hi:.2f}]")
    else:
        lines.append("emitted-label precision on gold: no gold yet — run `apex-router labels "
                     "review` (precision is unmeasured until then)")
    _ = wk
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="apex-router labels",
                                 description="task outcome labels: weak rules + local judge + gold")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="extract tasks, apply rules, judge up to N new tasks")
    b.add_argument("--judge", type=int, default=0, metavar="N")
    sub.add_parser("report", help="coverage, voter accuracy and precision on gold")
    r = sub.add_parser("review", help="label sampled tasks by hand (the gold set)")
    r.add_argument("-n", type=int, default=20)
    a = ap.parse_args(argv)
    if a.cmd == "build":
        res = build(judge_limit=a.judge)
        print(report(res))
    elif a.cmd == "report":
        print(report())
    else:
        print(f"labeled {review(a.n)}")
        print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
