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
  source x weak label x confidence plus the cases where the signals disagree. The same sample can
  go to an outside reviewer (``labels export-review`` / ``labels import-gold --by NAME``); every
  gold row carries ``by`` (``user`` for the owner), so model-made gold is never read as the
  owner's.

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

from .transcript_mirror import main_transcripts, source_of

OUTCOMES = ("success", "partial", "fail")
EMIT_MIN = 0.80                 # posterior needed to emit a label (else "unknown")
GOLD_FRACTION = 0.10            # gold grows with the data: 10% of tasks, at least GOLD_MIN
GOLD_MIN = 100
# Judge defaults = the judge-bench winner on gold (docs/research/2026-10-08-label-judge-bench.md):
# ornith:35b on the export-review context with the v2 decision-rule prompt, voting at >= 0.9.
JUDGE_MODEL = os.environ.get("APEX_LABEL_JUDGE", "ornith:35b")
JUDGE_URL = os.environ.get("APEX_LABEL_JUDGE_URL", "http://127.0.0.1:11434/api/chat")
CLIP = {"request": 700, "last": 900, "next": 400}


def home() -> Path:
    return Path(os.environ.get("APEX_ROUTER_HOME") or Path.home() / ".apex-router") / "labels"


# ---- extraction -----------------------------------------------------------------------------

def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, dict) and b.get("type") == "text"
                         and isinstance(b.get("text"), str))
    return ""


# Injected into the user role by the harness, not typed by the user: a skill's body, slash-command
# echoes, local-command output, notifications, a message relayed from another session. Counting
# them as requests splits real tasks and hands the judge a fake "next message".
# Claude Code marks these records ``isMeta`` (skill bodies loaded by a slash command or the Skill
# tool, image metadata after a screenshot tool result or a pasted image) or ``isCompactSummary``
# (the summary that opens a compacted session); the prefixes below catch the same bodies in
# transcripts that lack the flags. A pasted image's own record ("[Image #N] ...") is typed by the
# user and IS a request; its "[Image: source: ...]" / "[Image: original ...]" companion is not.
_SKIP_USER = ("<command-", "<local-command", "Caveat:", "<system", "<task-notification",
              "Base directory for this skill:", "Another Claude session sent a message",
              "<bash-", "# Claude in Chrome browser automation", "# Update Config Skill",
              "# Workflow authoring reference", "[Image: source: ", "[Image: original ",
              "This session is being continued from a previous conversation")
_META_FLAGS = ("isMeta", "isCompactSummary")


def is_tool_result(content) -> bool:
    """A Claude Code user record that carries tool results (not a typed request)."""
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                             for b in content)


def request_text(content, record: dict | None = None) -> str | None:
    """The request text of a user record, or None when it is not a task boundary (empty, or
    injected by the harness: a meta-flagged ``record`` or a known injected prefix). Shared with
    ``worldmodel.steps`` so both cut tasks identically."""
    if isinstance(record, dict) and any(record.get(f) is True for f in _META_FLAGS):
        return None
    t = _text(content).strip()
    if not t or t.startswith(_SKIP_USER):
        return None
    return t


def _tid(path: Path, i: int) -> str:
    return hashlib.sha1(f"{path.name}:{i}".encode()).hexdigest()[:12]


def _extract_line(line, tasks, by_call) -> None:
    """One transcript line into ``tasks`` (the open task is ``tasks[-1]``; tool calls are
    indexed in ``by_call``). Raises on a malformed record; ``extract`` skips it."""
    cur = tasks[-1] if tasks else None
    try:
        d = json.loads(line)
    except ValueError:
        return
    if not isinstance(d, dict):               # a JSON line that is not a record
        return
    m = d.get("message") if isinstance(d.get("message"), dict) else {}
    role = m.get("role") or d.get("type")
    c = m.get("content")
    if role == "user":
        if is_tool_result(c):
            for b in c:                                   # Claude Code tool results
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    call = by_call.get(b.get("tool_use_id"))
                    if call is not None:
                        call[2] = bool(b.get("is_error"))
                        call[3] = _text(b.get("content"))[-2000:] if not isinstance(
                            b.get("content"), str) else b["content"][-2000:]
            return
        t = request_text(c, d)
        if t is None:
            return
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
            elif b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                cur["last"] = b["text"]
    elif role == "toolResult" and cur is not None:              # pi
        call = by_call.get(m.get("toolCallId"))
        if call is not None:
            call[2] = m.get("isError") in (True, "True", "true")
            call[3] = _text(c)[-2000:]


def extract(path: Path) -> list[dict]:
    """Tasks of one transcript, in order. Each: id, source, session, index, ts, calls
    [(tool, command, is_error, result_head)], last assistant text, request, next request."""
    source = source_of(path)
    tasks: list[dict] = []
    by_call: dict = {}
    try:
        fh = open(path, errors="replace")
    except OSError:                                  # unreadable transcript: no tasks
        return []
    with fh:
        for line in fh:
            try:
                _extract_line(line, tasks, by_call)
            except (OSError, UnicodeError):
                break
            except Exception:  # noqa: BLE001 — one odd record never sinks the file
                continue
    out = []
    for i, t in enumerate(tasks):
        if not t["calls"]:
            continue
        t.update(id=_tid(path, i), source=source, session=path.stem[-36:], index=i,
                 path=str(path), next=tasks[i + 1]["request"] if i + 1 < len(tasks) else None)
        out.append(t)
    return out


def transcripts(user_home=None, home=None) -> list[Path]:
    """pi + Claude Code main sessions: the live dirs UNION the transcript mirror
    (``apex-router worldmodel snapshot``), one file per (source, session id), larger wins."""
    return main_transcripts(user_home, home)


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
_IMAGE = re.compile(r"\[Image[ :#][^\]]*\]")


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


# "no" that declines something the agent OFFERED at the end ("say the word and I'll push" ->
# "no more pushes") is not a verdict on the work. Both halves must hold: the final message ends
# on an offer, and the "no" is followed by a declining phrase or a bare action word. "no, still
# broken" / "no, that's wrong" keep counting as negative.
OFFER = re.compile(r"\?\s*$|say the word|want me to|should i\b|shall i\b|if you want|do you want|"
                   r"would you like|say \W?\w+\W? (when|and)|tell me (to|if|and)|"
                   r"(i can|i'?ll) .{0,60}if you", re.IGNORECASE)
DECLINE = re.compile(r"^\s*(no|nope|nah)\b[\s,.!-]*(thanks?\b|thank you|need\b|more\b|don'?t\b|"
                     r"do not\b|not (now|yet|needed)\b|later\b|skip\b|leave it\b|keep\b|just\b|"
                     r"that'?s (fine|ok)\b|(push|pushes|pr|merge|deploy|commit|commits)\b)",
                     re.IGNORECASE)


def _declines_offer(t) -> bool:
    return bool(DECLINE.search(t.get("next") or "")
                and OFFER.search((t.get("last") or "")[-400:]))


def _next_negative(t) -> bool:
    return bool(NEG.search(t.get("next") or "")) and not _declines_offer(t)


def lf_commit(t):
    cmds = "\n".join(c[1] or "" for c in t["calls"])
    return "success" if COMMIT.search(cmds) and not _next_negative(t) else None


def lf_next_negative(t):
    return "fail" if _next_negative(t) else None


def lf_next_positive(t):
    return "success" if POS.search(t.get("next") or "") else None


def lf_interrupted(t):
    return "fail" if INTERRUPT.search(t.get("next") or "") else None


def lf_repeated(t):
    """The next request restates this one (Jaccard >= 0.6 on content words): it did not land.
    Image placeholders ("[Image #3]", "[Image: original 3550x1990 ...]") are removed first, so
    two screenshots in a row, or an image-only request, are not a repeat."""
    a = _tokens(_IMAGE.sub(" ", t["request"] or ""))
    b = _tokens(_IMAGE.sub(" ", t.get("next") or ""))
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
# A judge config is (model, context, prompt): the CONTEXT builder renders the evidence block of
# one task, the PROMPT is the instruction header above it. ``labels judge-bench`` scores configs
# against gold (docs/research/2026-10-08-label-judge-bench.md); each default is env-overridable.

JUDGE_CONTEXT = os.environ.get("APEX_LABEL_JUDGE_CONTEXT", "export")
JUDGE_PROMPT_VERSION = os.environ.get("APEX_LABEL_JUDGE_PROMPT", "v2")

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
"""

# v2 states the decision rules the gold reviewer applied. The rules are generic: none quotes or
# paraphrases a gold task.
JUDGE_PROMPT_V2 = """You label the OUTCOME of one task an AI coding agent did for a user.
Use only the evidence below. Outcomes:
- "success": the request was done (or answered) and nothing in the user's next message says
  otherwise
- "partial": some of it was done, but part of what was asked is left undone or blocked, or the
  user had to correct part of it
- "fail": not done, wrong, the turn died on an error, or the user had to repeat / correct /
  revert / abandon it
- "unknown": the evidence cannot show the outcome
Decision rules, in order:
1. The user's next message is the strongest evidence. If it moves on positively - approves,
   asks a follow-up that builds on the work, answers questions the agent asked, picks an
   offered option, says continue / push / commit, or starts the next step - the outcome is
   "success" unless the final message shows the work was not done.
2. A next message saying the result is wrong, broken or stuck, or repeating the request, means
   "fail" ("partial" if part of it landed).
3. A turn cut mid-work - the next message is an interrupt marker, or a new request arriving
   while the agent was still working with no result yet - is "unknown", not "fail". It is
   "fail" only when the agent had clearly stalled, errored or refused.
4. Declining something the agent OFFERED at the end ("no thanks", "no more pushes", "not now")
   is not a verdict on the work: judge the work itself.
5. Tests that ran after the last edit and passed, or a commit/push, support "success"; tests
   that failed after the last edit support "fail" or "partial". A turn that ends in an API /
   connection error is "fail".
6. When the session ended (no next message), judge from the final message: finished, verified
   work is "success"; a status update, a waiting state, or text about other work is "unknown".
7. A question or analysis request is "success" when the final message answers it.
Answer JSON only: {"outcome": "...", "confidence": 0.0-1.0, "evidence": "next_message|tests|
final_message|tools|none"}
"""

JUDGE_PROMPTS = {"v1": JUDGE_PROMPT, "v2": JUDGE_PROMPT_V2}
EVIDENCE = ("next_message", "tests", "final_message", "tools", "none")


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


def _evidence_block(t, clip: dict, with_signals: bool) -> str:
    nxt = t.get("next")
    parts = ["USER REQUEST:", f"<<<{t['request'][:clip['request']]}>>>", "",
             f"TOOLS: {_tool_summary(t)}"]
    if with_signals:
        parts.append("SIGNALS: " + json.dumps(signals(t)))
    parts += ["", "AGENT'S FINAL MESSAGE:", f"<<<{(t['last'] or '')[-clip['last']:]}>>>", "",
              "USER'S NEXT MESSAGE (none = session ended):",
              f"<<<{(nxt or 'none')[:clip['next']]}>>>"]
    return "\n".join(parts)


def context_clip(t) -> str:
    """The original judge context: request 700 / final 900 / next 400 chars + tool summary."""
    return _evidence_block(t, CLIP, with_signals=False)


def context_export(t) -> str:
    """What ``export-review`` showed the gold reviewer: request 1500 / final 2000 / next 800
    chars, the tool summary and the rule-level ``signals()``."""
    return _evidence_block(t, EXPORT_CLIP, with_signals=True)


JUDGE_CONTEXTS = {"clip": context_clip, "export": context_export}


def judge_prompt(t, context: str | None = None, prompt: str | None = None) -> str:
    return (JUDGE_PROMPTS[prompt or JUDGE_PROMPT_VERSION] + "\n"
            + JUDGE_CONTEXTS[context or JUDGE_CONTEXT](t))


def judge_call(prompt: str, model: str, url: str | None = None, timeout: float = 180) -> tuple:
    """One deterministic judge call (temperature 0, fixed seed) -> (verdict | None, meta);
    meta = {latency_s, prompt_tokens, eval_tokens, error}. A failure is an abstention."""
    body = json.dumps({"model": model, "stream": False, "think": False, "format": "json",
                       "options": {"temperature": 0, "seed": 0, "num_ctx": 8192},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(url or JUDGE_URL, data=body,
                                 headers={"content-type": "application/json"})
    meta = {"latency_s": None, "prompt_tokens": None, "eval_tokens": None, "error": None}
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
        meta.update(prompt_tokens=resp.get("prompt_eval_count"), eval_tokens=resp.get("eval_count"))
        out = json.loads(resp["message"]["content"])
    except Exception as ex:  # noqa: BLE001 — a judge failure is an abstention
        meta.update(latency_s=round(time.time() - t0, 3), error=type(ex).__name__)
        return None, meta
    meta["latency_s"] = round(time.time() - t0, 3)
    o = out.get("outcome") if isinstance(out, dict) else None
    conf = out.get("confidence") if isinstance(out, dict) else None
    if (o not in OUTCOMES + ("unknown",) or not isinstance(conf, (int, float))
            or isinstance(conf, bool)):
        meta["error"] = "bad_output"
        return None, meta
    ev = str(out.get("evidence", ""))
    return {"outcome": o, "confidence": max(0.0, min(1.0, float(conf))),
            "evidence": ev if ev in EVIDENCE else "other"}, meta


def judge(t, url: str | None = None, model: str | None = None, timeout: float = 180,
          context: str | None = None, prompt: str | None = None) -> dict | None:
    """The configured judge on one task -> {outcome, confidence, evidence, model, context,
    prompt} or None (abstain)."""
    model, context = model or JUDGE_MODEL, context or JUDGE_CONTEXT
    prompt = prompt or JUDGE_PROMPT_VERSION
    v, _ = judge_call(judge_prompt(t, context, prompt), model, url, timeout)
    return None if v is None else {**v, "model": model, "context": context, "prompt": prompt}


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


# calibrated on gold: the lowest confidence bin from which every bin up is >= 0.75 accurate
JUDGE_MIN_CONF = float(os.environ.get("APEX_LABEL_JUDGE_MIN_CONF") or 0.9)


def _all_votes(r) -> dict:
    v = dict(r.get("votes") or {})
    j = r.get("judge")
    if isinstance(j, dict) and j.get("outcome") in OUTCOMES and j.get("confidence", 0) >= JUDGE_MIN_CONF:
        v["judge"] = j["outcome"]
    return v


SIGNAL_CONTEXTS = {"export"}    # judge contexts that show the judge the rule voters' facts


def _smoothed(acc: dict, name: str) -> float:
    k, n = acc.get(name, (0, 0))
    return min(0.99, max(0.34, (k + 2) / (n + 4)))


def voter_groups(r, acc: dict) -> list:
    """[(label, accuracy)] of the independent voters of one task. A judge that read the rule
    voters' facts (``SIGNAL_CONTEXTS``: its prompt carries ``signals()``) is not independent of
    the rules it agrees with: the judge and every rule voting its label are ONE correlated voter
    with the accuracy of the most accurate of them (max, not product). Rules that disagree with
    the judge, and every voter when the judge did not see the signals, count separately."""
    vs = _all_votes(r)
    j = r.get("judge")
    if "judge" in vs and isinstance(j, dict) and judge_config(j)[1] in SIGNAL_CONTEXTS:
        group = [n for n, v in vs.items() if v == vs["judge"]]
        out = [(vs["judge"], max(_smoothed(acc, n) for n in group))]
        return out + [(v, _smoothed(acc, n)) for n, v in vs.items() if n not in group]
    return [(v, _smoothed(acc, n)) for n, v in vs.items()]


def posterior(r, acc: dict) -> tuple:
    """Naive-Bayes combination over ``voter_groups``: each voter with accuracy a (Beta(2,2)-
    smoothed on gold) multiplies its label by a and the others by (1-a)/2. Uniform prior.
    -> (label, probability)."""
    groups = voter_groups(r, acc)
    if not groups:
        return ("unknown", 0.0)
    logp = {o: 0.0 for o in OUTCOMES}
    for v, a in groups:
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


def gold_latest(gold_rows: list | None = None) -> dict:
    """id -> the gold row in force: the newest by ``ts`` (file order breaks ties). Gold is
    append-only; a correction is a new row with ``supersedes`` = the ts of the row it replaces."""
    if gold_rows is None:
        gold_rows = _read(home() / "gold.jsonl")
    out: dict = {}
    for g in gold_rows:
        k = g.get("id")
        if not isinstance(k, str):
            continue
        cur = out.get(k)
        if cur is None or float(g.get("ts") or 0) >= float(cur.get("ts") or 0):
            out[k] = g
    return out


def _fp(r) -> tuple:
    """A text-free identity for one request: its session and its record timestamp."""
    return (r.get("session"), r.get("ts"))


def _migrate_gold(h: Path, rows: list, old: dict, remap: dict, log=print) -> dict:
    """Keep gold attached to the same REQUEST when task ids shift. A row whose task moved gets
    the new id (``id_was`` keeps the old one); a row whose request is no longer a task (it was a
    harness-injected record, or merged into another task) becomes ``orphan:<id>`` so it can never
    label a different task that now has its old id. Outcomes, authors and reasons are untouched."""
    gold_rows = _read(h / "gold.jsonl")
    if not gold_rows:
        return {}
    live = {r["id"]: r for r in rows}
    dest = []                       # where each row goes; None = orphan, "" = leave as is
    for g in gold_rows:
        gid = g.get("id")
        if not isinstance(gid, str) or gid.startswith("orphan:"):
            dest.append("")
        elif gid in remap:
            dest.append(remap[gid])
        elif gid in live and (gid not in old or _fp(old[gid]) == _fp(live[gid])):
            dest.append(gid)
        else:
            dest.append(None)
    # rows superseding each other share an id: count distinct SOURCE ids per destination
    pairs = {(g.get("id"), d) for g, d in zip(gold_rows, dest) if d}
    claimed = Counter(d for _, d in pairs)
    n = Counter()
    out = []
    for g, d in zip(gold_rows, dest):
        if d == "":
            out.append(g)
            continue
        gid = g["id"]
        if d is None or claimed[d] > 1:          # two gold rows for one task: keep neither
            g = {**g, "id": f"orphan:{gid}", "orphaned": time.time()}
            n["orphaned"] += 1
        elif d != gid:
            g = {**g, "id": d, "id_was": gid}
            n["moved"] += 1
        else:
            n["kept"] += 1
        out.append(g)
    if n["moved"] or n["orphaned"]:
        _write(h / "gold.jsonl", out)
        if log:
            log(f"  gold ids: kept {n['kept']}, moved {n['moved']} (task id shifted), "
                f"orphaned {n['orphaned']} (request is no longer a task)")
    return dict(n)


def judge_config(vote: dict | None = None) -> tuple:
    """(model, context, prompt) of a stored judge vote, or of the current judge when ``vote`` is
    None. Votes from before the context/prompt fields existed were clip / v1."""
    if vote is None:
        return (JUDGE_MODEL, JUDGE_CONTEXT, JUDGE_PROMPT_VERSION)
    return (vote.get("model"), vote.get("context") or "clip", vote.get("prompt") or "v1")


def build(judge_limit: int = 0, judge_fn=judge, log=print, rejudge: bool = False) -> dict:
    """Extract every task, apply the LFs, run the judge on up to ``judge_limit`` tasks not judged
    yet (newest first), combine with gold accuracies, write tasks/labels. Idempotent.
    ``rejudge`` drops every judge vote the CURRENT judge config did not make, so those tasks are
    judged again (up to ``judge_limit``; a task past the limit is left without a vote, never with
    a stale one). Rerunning resumes: votes the current config already made are kept."""
    h = home()
    old_rows = _read(h / "tasks.jsonl")
    old = {r["id"]: r for r in old_rows}
    by_fp = {_fp(r): r for r in old_rows if r.get("ts")}
    tasks = all_tasks()
    rows = []
    todo = []
    remap: dict = {}
    for t in tasks:
        r = _row(t)
        # Task ids are positional (transcript name + request ordinal), so a change to what counts
        # as a request shifts them. Match the previous row by (session, request timestamp) — never
        # by id alone, which may now name a different request.
        prev = old.get(r["id"])
        if prev is not None and _fp(prev) != _fp(r):
            prev = None
        if prev is None and r.get("ts"):
            prev = by_fp.get(_fp(r))
        if prev is not None and prev["id"] != r["id"]:
            remap[prev["id"]] = r["id"]
        # a judge vote carries over only when the evidence it saw is unchanged (and, with
        # ``rejudge``, only when the current judge config made it)
        if (prev is not None and prev.get("judge") and prev.get("n_calls") == r["n_calls"]
                and prev.get("has_next") == r["has_next"] and prev.get("votes") == r["votes"]
                and not (rejudge and judge_config(prev["judge"]) != judge_config())):
            r["judge"] = prev["judge"]
        elif judge_limit:
            todo.append((t, r))
        rows.append(r)
    _migrate_gold(h, rows, old, remap, log)
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
    live = {r["id"] for r in rows}
    gold = {k: g["outcome"] for k, g in gold_latest().items() if k in live}
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


def _session_paths() -> dict:
    by_session: dict = {}
    for p in transcripts():
        by_session.setdefault(p.stem[-36:], p)
    return by_session


def _load_task(r: dict, by_session: dict) -> dict | None:
    """The task's text, read from its transcript now (never stored)."""
    p = by_session.get(r["session"])
    return next((x for x in extract(p) if x["id"] == r["id"]), None) if p else None


def review(k: int = 20, inp=input, out=print) -> int:
    """Show k sampled tasks; the user types s(uccess) / p(artial) / f(ail) / u(nknown) /
    q(uit). Text is read from the transcript now and not stored."""
    h = home()
    rows = _read(h / "tasks.jsonl")
    labels = _read(h / "labels.jsonl")
    gold_rows = _read(h / "gold.jsonl")
    gold = {g["id"]: g["outcome"] for g in gold_rows}
    by_session = _session_paths()
    keys = {"s": "success", "p": "partial", "f": "fail", "u": "unknown"}
    done = 0
    for r in sample_for_review(rows, labels, gold, k):
        t = _load_task(r, by_session)
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


# ---- non-interactive review: export for a reviewer, import their gold ----------------------
# ``export-review`` writes the same sampled tasks ``review`` would show, with more context, to a
# 0600 file for a reviewer to read OUTSIDE this tool (a person, or a model acting for the owner).
# The file holds transcript text: it is for the reviewer's eyes only, must never be copied into a
# repo or into gold.jsonl, and should be deleted after the import. ``import-gold`` reads back
# ``{id, outcome, reason?}`` lines and appends them to gold.jsonl marked ``by=NAME``, so labels a
# model made are never confused with the owner's (``review`` writes ``by=user``).

EXPORT_CLIP = {"request": 1500, "last": 2000, "next": 800}
GOLD_OUTCOMES = OUTCOMES + ("unknown",)
REASON_MAX = 120


def signals(t) -> dict:
    """The rule-level facts behind the LFs, for a reviewer: counts and booleans, no text."""
    calls = t["calls"]
    last_edit = max((i for i, c in enumerate(calls) if c[0] in EDIT_TOOLS), default=-1)
    tests = lf_tests(t)
    ran = any(i > last_edit and TEST_CMD.search(c[1] or "") for i, c in enumerate(calls))
    nxt = t.get("next")
    return {"n_edits": sum(1 for c in calls if c[0] in EDIT_TOOLS),
            "n_errors": sum(1 for c in calls if c[2]),
            "tests_after_last_edit": ("passed" if tests == "success" else "failed"
                                      if tests == "fail" else "ran, unparsed" if ran else None),
            "committed": bool(COMMIT.search("\n".join(c[1] or "" for c in calls))),
            "tail_errors": lf_tail_errors(t) == "fail",
            "next_interrupt": bool(INTERRUPT.search(nxt or "")),
            "next_negative": _next_negative(t),
            "next_declines_offer": _declines_offer(t),
            "next_positive": bool(POS.search(nxt or "")),
            "next_repeats_request": lf_repeated(t) == "fail",
            "session_ended": nxt is None}


# ---- stratified sampling for a holdout -------------------------------------------------------
# ``export-review --strata suspected-fail:25,test-split:12,random:23`` draws each stratum in the
# order given, never a task with gold in force and never one an earlier stratum took:
# - suspected-fail: a rule signal of failure (tail_errors, interrupted, next_negative, repeated,
#   tests failed after the last edit), a judge vote of fail/partial at any confidence, or a judge
#   confidence below ``JUDGE_MIN_CONF``;
# - test-split: tasks in the P6 test split (``worldmodel/tasks.jsonl``, ``split == "test"``),
#   drawn in ``_test_priority`` order (no emitted label, rule fail vote, tool error, rest);
# - random: uniform over what is left.
STRATA = ("suspected-fail", "test-split", "random")
FAIL_SIGNALS = ("tail_errors", "interrupted", "next_negative", "repeated", "tests")


def wm_task_key(r: dict) -> str:
    """The P6 task id (``sid:i``) of a labels task row: ``worldmodel.steps`` cuts tasks with the
    same ordinals and gives each the labels id ``_tid(path, i)``, so (session, index) is the
    join."""
    return f"{r['session']}:{r['index']}"


def split_test_keys(path: Path | None = None) -> set:
    """``sid:i`` of every P6 task whose frozen split is ``test`` (empty if not built)."""
    if path is None:
        path = home().parent / "worldmodel" / "tasks.jsonl"
    return {x["task"] for x in _read(Path(path))
            if isinstance(x, dict) and x.get("split") == "test" and isinstance(x.get("task"), str)}


def suspected_fail(r: dict, min_conf: float | None = None) -> bool:
    min_conf = JUDGE_MIN_CONF if min_conf is None else min_conf
    v = r.get("votes") or {}
    if any(v.get(s) == "fail" for s in FAIL_SIGNALS):
        return True
    j = r.get("judge")
    if isinstance(j, dict):
        if j.get("outcome") in ("fail", "partial"):
            return True
        c = j.get("confidence")
        if isinstance(c, (int, float)) and c < min_conf:
            return True
    return False


def parse_strata(spec: str) -> list:
    """``"suspected-fail:25,test-split:12,random:23"`` -> [(name, count)]."""
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, n = part.partition(":")
        if name not in STRATA or not n.isdigit():
            raise GoldImportError(f"bad stratum {part!r}: want NAME:COUNT, NAME in "
                                  f"{'|'.join(STRATA)}")
        out.append((name, int(n)))
    if not out:
        raise GoldImportError("no strata given")
    return out


def _test_priority(r: dict, lab: dict) -> int:
    """Test-split draw order (stable within a rank): no emitted label, then a rule fail vote,
    then any tool error, then the rest — where bad tasks are, if any exist."""
    if lab.get(r["id"]) in (None, "unknown"):
        return 0
    if any((r.get("votes") or {}).get(s) == "fail" for s in FAIL_SIGNALS):
        return 1
    return 2 if (r.get("n_errors") or 0) > 0 else 3


def sample_strata(rows: list, labels: list, gold: dict, strata: list, test_keys: set,
                  seed: int = 7) -> list:
    """[(row, stratum)] drawn per ``strata`` (see ``STRATA``); a stratum with fewer candidates
    than asked gives what it has."""
    rng = random.Random(seed)
    lab = {x["id"]: x.get("label") for x in labels}
    taken = set(gold)
    out = []
    for name, n in strata:
        pool = [r for r in rows if r["id"] not in taken]
        if name == "suspected-fail":
            pool = [r for r in pool if suspected_fail(r)]
            rng.shuffle(pool)
        elif name == "test-split":
            pool = [r for r in pool if wm_task_key(r) in test_keys]
            rng.shuffle(pool)
            pool.sort(key=lambda r: _test_priority(r, lab))
        else:
            rng.shuffle(pool)
        for r in pool[:n]:
            out.append((r, name))
            taken.add(r["id"])
    return out


def export_review(k: int, out_path, seed: int = 7, ids: list | None = None,
                  strata: list | None = None, test_keys: set | None = None) -> int:
    """Write k tasks sampled by ``sample_for_review`` — or exactly the tasks in ``ids``, gold or
    not, or the tasks ``sample_strata`` draws for ``strata`` — to ``out_path`` (mode 0600), one
    JSON line each. A task that already has gold carries the row in force (outcome, ts, by) so a
    correction can name it in ``supersedes``; a stratified row carries its ``stratum``. Nothing
    is stored under the labels home. Returns the number written."""
    h = home()
    rows = _read(h / "tasks.jsonl")
    labels = _read(h / "labels.jsonl")
    latest = gold_latest()
    gold = {g: x["outcome"] for g, x in latest.items()}
    stratum: dict = {}
    if strata is not None:
        drawn = sample_strata(rows, labels, gold, strata,
                              split_test_keys() if test_keys is None else test_keys, seed=seed)
        picked = [r for r, _ in drawn]
        stratum = {r["id"]: s for r, s in drawn}
    elif ids is not None:
        by_id = {r["id"]: r for r in rows}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise GoldImportError(f"unknown task ids: {', '.join(missing[:10])}")
        picked = [by_id[i] for i in dict.fromkeys(ids)]
    else:
        picked = sample_for_review(rows, labels, gold, k, seed=seed)
    by_session = _session_paths()
    out_path = Path(out_path)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(out_path, 0o600)                     # an existing file keeps its old mode otherwise
    n = 0
    with os.fdopen(fd, "w") as fh:
        for r in picked:
            t = _load_task(r, by_session)
            if not t:
                continue
            nxt = t.get("next")
            fh.write(json.dumps({
                "id": r["id"], "source": r["source"], "n_calls": len(t["calls"]),
                "tools": _tool_summary(t),
                "request": t["request"][:EXPORT_CLIP["request"]],
                "last": (t["last"] or "")[-EXPORT_CLIP["last"]:],
                "next": nxt[:EXPORT_CLIP["next"]] if nxt is not None else None,
                "votes": votes(t), "judge": r.get("judge"), "signals": signals(t),
                "gold": ({f: latest[r["id"]].get(f) for f in ("outcome", "ts", "by")}
                         if r["id"] in latest else None),
                **({"stratum": stratum[r["id"]]} if r["id"] in stratum else {})},
                ensure_ascii=False) + "\n")
            n += 1
    return n


class GoldImportError(ValueError):
    pass


TAG_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def import_gold(path, by: str, tag: str | None = None) -> dict:
    """Append ``{id, outcome, reason?}`` lines from ``path`` to gold.jsonl as ``by=NAME`` (and
    ``tag=TAG`` on every row when given, e.g. a holdout batch), then
    ``relabel()``. All-or-nothing: any bad line rejects the whole file and nothing is written.
    Rejected: an id not in tasks.jsonl, an outcome outside success|partial|fail|unknown, an id
    already in gold or twice in the file, a reason that is not a string of <= ``REASON_MAX``
    characters. The reason must not quote the transcript; that rule is enforced by the length
    cap only (no matching against transcript text) — it is on the reviewer to keep.

    Corrections never edit a row: a line for an id that already has gold must carry
    ``supersedes`` = the ``ts`` of the row currently in force for that id; the new row is
    appended and wins (``gold_latest``). A stale or unknown ``supersedes`` is rejected."""
    by = (by or "").strip()
    if not by or len(by) > 64 or any(ch.isspace() for ch in by):
        raise GoldImportError("--by must be a non-empty name without spaces (<= 64 chars)")
    if tag is not None and not TAG_RE.match(tag):
        raise GoldImportError("--tag must be 1-64 chars of letters, digits, . _ : -")
    h = home()
    ids = {r["id"] for r in _read(h / "tasks.jsonl")}
    gold_rows = _read(h / "gold.jsonl")
    have = gold_latest(gold_rows)
    errs, new, seen = [], [], set()
    now = max([time.time()] + [float(g.get("ts") or 0) + 1e-3 for g in gold_rows])
    for i, ln in enumerate(Path(path).read_text().splitlines(), 1):
        if not ln.strip():
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            errs.append(f"line {i}: not JSON")
            continue
        if not isinstance(d, dict):
            errs.append(f"line {i}: not an object")
            continue
        tid, o, reason = d.get("id"), d.get("outcome"), d.get("reason")
        sup = d.get("supersedes")
        if not isinstance(tid, str) or tid not in ids:
            errs.append(f"line {i}: id {tid!r} is not a known task")
        elif tid in have and sup is None:
            errs.append(f"line {i}: id {tid} already has a gold label (pass supersedes=<its ts>)")
        elif sup is not None and (tid not in have or not isinstance(sup, (int, float))
                                  or abs(float(have[tid].get("ts") or 0) - sup) > 1e-6):
            errs.append(f"line {i}: supersedes {sup!r} is not the ts of the gold row in force "
                        f"for {tid}")
        elif tid in seen:
            errs.append(f"line {i}: id {tid} appears twice")
        if o not in GOLD_OUTCOMES:
            errs.append(f"line {i}: outcome {o!r} not in {'|'.join(GOLD_OUTCOMES)}")
        if reason is not None and (not isinstance(reason, str) or len(reason) > REASON_MAX):
            errs.append(f"line {i}: reason must be a string of <= {REASON_MAX} chars")
        if isinstance(tid, str):
            seen.add(tid)
        g = {"id": tid, "outcome": o, "ts": now, "by": by}
        if tag is not None:
            g["tag"] = tag
        if reason:
            g["reason"] = reason
        if sup is not None:
            g["supersedes"] = sup
        new.append(g)
    if errs:
        more = f" (+{len(errs) - 20} more)" if len(errs) > 20 else ""
        raise GoldImportError("; ".join(errs[:20]) + more)
    if not new:
        raise GoldImportError("no labels in file")
    _write(h / "gold.jsonl", gold_rows + new)
    res = relabel()
    res["imported"] = len(new)
    return res


def gold_by() -> Counter:
    """Gold labels per author (``by``); rows written before ``by`` existed count as unmarked.
    Orphaned rows (request no longer a task) are counted under ``orphaned``, not an author."""
    return Counter("orphaned" if k.startswith("orphan:") else (g.get("by") or "unmarked")
                   for k, g in gold_latest().items())          # superseded rows not counted


def gold_by_tag() -> Counter:
    """Gold labels in force per ``tag`` (untagged rows count as ``untagged``; orphans skipped)."""
    return Counter(g.get("tag") or "untagged" for k, g in gold_latest().items()
                   if not k.startswith("orphan:"))


# ---- report ---------------------------------------------------------------------------------

def class_prior_line(labels: list, gold: dict, rows: list) -> str:
    """Class shares of the weak-emitted labels vs the decided gold vs what the independent rule
    signals alone suggest (share of tasks with a rule fail vote, share with any tool error), so
    a shortage of fail labels is visible next to the evidence that bad tasks exist."""
    def shares(c: Counter) -> str:
        n = sum(c[o] for o in OUTCOMES)
        return (" / ".join(f"{c[o] / n:.2f}" for o in OUTCOMES) + f" (n {n})") if n else "– (n 0)"
    weak = Counter(x["label"] for x in labels if x.get("from") == "weak")
    gd = Counter(v for v in gold.values() if v in OUTCOMES)
    n = max(len(rows), 1)
    rule_fail = sum(1 for r in rows if any((r.get("votes") or {}).get(s) == "fail"
                                           for s in FAIL_SIGNALS))
    errs = sum(1 for r in rows if (r.get("n_errors") or 0) > 0)
    return (f"class prior s/p/f: weak-emitted {shares(weak)} · gold {shares(gd)} · independent "
            f"signals: rule-fail vote {rule_fail / n:.2f} of tasks, n_errors>0 {errs / n:.2f}")


def report(res: dict | None = None) -> str:
    h = home()
    if res is None:
        res = relabel()
    labels, acc, gold = res["labels"], res["acc"], res["gold"]
    n = len(labels)
    emitted = [x for x in labels if x["label"] != "unknown"]
    weak = sum(1 for x in emitted if x.get("from") == "weak")
    lines = [(f"tasks {n} · labeled {len(emitted)} ({100 * len(emitted) / max(n, 1):.0f}%; "
              f"weak-emitted {weak}, from gold {len(emitted) - weak}) · "
              f"gold {len(gold)} / target {gold_target(n)}")]
    gb = gold_by()
    orphaned = gb.pop("orphaned", 0)
    if gb:
        users = gb.get("user", 0)
        lines.append(f"gold by: user {users} · model/other {sum(gb.values()) - users} ("
                     + ", ".join(f"{k} {v}" for k, v in sorted(gb.items())) + ")"
                     + (f" · orphaned {orphaned} (request no longer a task)" if orphaned else ""))
    gt = gold_by_tag()
    if any(k != "untagged" for k in gt):
        lines.append("gold by tag: " + ", ".join(f"{k} {v}" for k, v in sorted(gt.items())))
    by = Counter(x["label"] for x in labels)
    lines.append("labels: " + ", ".join(f"{k} {by[k]}" for k in OUTCOMES + ("unknown",)))
    lines.append(class_prior_line(labels, gold, _read(h / "tasks.jsonl")))
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
    b.add_argument("--rejudge", action="store_true",
                   help="drop judge votes the current judge config did not make, then judge "
                        "up to N tasks (newest first)")
    sub.add_parser("report", help="coverage, voter accuracy and precision on gold")
    r = sub.add_parser("review", help="label sampled tasks by hand (the gold set)")
    r.add_argument("-n", type=int, default=20)
    e = sub.add_parser("export-review", help="write K sampled tasks with context to FILE (0600) "
                                              "for an outside reviewer; nothing is stored")
    e.add_argument("--k", type=int, default=100)
    e.add_argument("--out", required=True)
    e.add_argument("--ids", help="comma-separated task ids to export instead of sampling "
                                 "(gold or not; their gold row in force is included)")
    e.add_argument("--strata", help="stratified holdout instead of the review sample: "
                                    "NAME:COUNT,... with NAME in " + "|".join(STRATA)
                                    + " (drawn in order, never a task with gold)")
    e.add_argument("--seed", type=int, default=7)
    g = sub.add_parser("import-gold", help="append {id, outcome, reason?, supersedes?} lines to "
                                           "gold as by=NAME")
    g.add_argument("file")
    g.add_argument("--by", required=True)
    g.add_argument("--tag", default=None, help="mark every imported row tag=TAG (e.g. a holdout)")
    jb = sub.add_parser("judge-bench", help="score judge variants (model x context x prompt) "
                                            "against gold; cached, no text stored")
    jb.add_argument("--models", default=None, help=f"comma-separated (default {JUDGE_MODEL})")
    jb.add_argument("--contexts", default="clip,export")
    jb.add_argument("--prompts", default="v1,v2")
    jb.add_argument("--out", default=None, help="default ~/.apex-router/labels/judge_bench")
    jb.add_argument("--max-latency", type=float, default=20.0, metavar="S",
                    help="skip a model whose mean latency over its first 5 tasks exceeds S")
    jb.add_argument("--min-conf", type=float, default=None,
                    help=f"vote threshold for scoring (default {JUDGE_MIN_CONF})")
    jb.add_argument("--gold-tag", default=None,
                    help="score only on gold rows (in force) tagged TAG; writes results-TAG.jsonl "
                         "/ table-TAG.md next to the untagged ones (shared cache)")
    a = ap.parse_args(argv)
    if a.cmd == "judge-bench":
        from .label_judge_bench import main as _bench
        return _bench(a)
    if a.cmd == "export-review":
        ids = [x.strip() for x in a.ids.split(",") if x.strip()] if a.ids else None
        try:
            strata = parse_strata(a.strata) if a.strata else None
            n = export_review(a.k, a.out, ids=ids, strata=strata, seed=a.seed)
        except GoldImportError as ex:
            print(f"export-review: {ex}", file=sys.stderr)
            return 2
        print(f"exported {n} tasks to {a.out} (0600; it holds transcript text: delete it after "
              f"importing)")
        return 0
    if a.cmd == "import-gold":
        try:
            res = import_gold(a.file, a.by, tag=a.tag)
        except GoldImportError as ex:
            print(f"import-gold: rejected, nothing written: {ex}", file=sys.stderr)
            return 2
        print(f"imported {res['imported']} gold labels by {a.by.strip()}")
        print(report(res))
        return 0
    if a.cmd == "build":
        res = build(judge_limit=a.judge, rejudge=a.rejudge)
        print(report(res))
    elif a.cmd == "report":
        print(report())
    else:
        print(f"labeled {review(a.n)}")
        print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
