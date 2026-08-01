"""codeqa A/B — the blinded prose-correctness judge (the PRIMARY decision axis).

Groundedness (Phase-0) is blind to semantic corruption — a false claim citing a valid line scores
1.0 — so it cannot gate the build/don't-build decision (Codex A/B-F1). This module scores whether an
answer *correctly describes the code*, judged by an INDEPENDENT heavy model (Opus), NOT by the
answerer (local Ornith): a model grading its own output repeats its own blind spots.

Design (model-routing skill):
  - Judge = Opus (heavy tier): scoring correctness is a *conclude/judge* task, Ornith's measured
    weakness; and Opus is independent of the Ornith answerer.
  - BLINDED: the judge never sees the variant label or which digest produced the answer — only the
    question, the answer text, and the retrieved source. It grades correctness against the source.
  - The judge grades against the SAME retrieved chunks the answerer got, so "correct" means
    "supported by the code the model was shown", not the judge's own repo knowledge (which it lacks).

The API call is a SEAM (`call_fn`) so scoring logic is unit-testable offline; `opus_judge_fn()`
wires the real call through the target's own `claude`/`codex` CLI by default (tools disabled), or
an HTTP endpoint the user sets via CODEQA_JUDGE_BASE. Any credential comes from the environment —
this module does NOT embed one.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Callable

from .impact import parse_emitted_citations

# The judge reaches a frontier model through the target machine's OWN capabilities. By
# default that is the installed `claude` CLI (via the CLI adapter — no Foundry, no
# internal endpoint, tools disabled). A power user may instead point CODEQA_JUDGE_BASE at
# an HTTP messages endpoint they control; only then is the HTTP path used.
#
# Config is resolved at CALL time from the environment (Codex #3) — never snapshotted at
# import — so a later env change (or a test) takes effect immediately. An empty
# CODEQA_JUDGE_BASE ("") means "use the CLI adapter", same as unset.
def _judge_config():
    base = os.environ.get("CODEQA_JUDGE_BASE") or None      # "" -> None -> CLI adapter
    backend = os.environ.get("CODEQA_JUDGE_BACKEND", "claude")
    m = os.environ.get("CODEQA_JUDGE_MODEL")
    model = re.sub(r"\[.*?\]$", "", m) if m else None       # strip a trailing "[...]" marker
    return base, backend, model

# Grades against the LIVE CODE at the answer's cited locations — NOT the answerer's retrieved excerpts
# (Codex A/B-judge-F1: grading vs excerpts penalizes a CORRECT digest-derived claim that isn't in the
# excerpts, biasing the metric AGAINST the fresh digest). Grading vs the live tree is treatment-
# neutral: both variants are checked against the same current code.
_RUBRIC = """\
You are grading whether an AI answer CORRECTLY describes a codebase. You are given a QUESTION, the
ANSWER under review, and the ACTUAL CURRENT SOURCE CODE at each location the answer cited (read live
from the repository — this is ground truth, not the answer's own excerpts).

Score the answer's CORRECTNESS on a 0.0–1.0 scale, judged against the ACTUAL SOURCE:
  1.0 = every concrete claim about the code is TRUE of the actual source; names the right
        types/functions and how they connect; no false statements.
  0.5 = partially correct — some claims true, some vague or unverifiable, but nothing FALSE.
  0.0 = makes a claim that CONTRADICTS the actual source, describes behavior not present, or
        answers a different question.

A claim is correct if the ACTUAL SOURCE supports it — even if the answer's phrasing differs. A claim
is wrong if the actual source contradicts it, no matter how confidently stated or well-cited. Do not
reward fluency or citation formatting. Grade truth against the code, not against the answer's own
excerpts. If a cited location does not exist in the source shown, treat claims about it as unsupported.

Reply with ONLY a JSON object: {"score": <float 0.0-1.0>, "why": "<one sentence>"}"""

# Appended to the prompt on a protocol-repair retry (the reply had no parseable score). Opus tends to
# preamble with reasoning on hard cases; this pulls it back to the wire format without changing the task.
_JSON_ONLY_REMINDER = (
    "\n\nIMPORTANT: Your previous reply could not be parsed. Do NOT write any reasoning or preamble. "
    'Respond with ONLY the JSON object and nothing else: {"score": <float 0.0-1.0>, "why": "<one sentence>"}')


def _live_source_at_citations(repo_root: Path, answer_text: str, *, radius: int = 8) -> str:
    """For each file:line the answer CITED, read the ACTUAL current code around it from the live tree
    (ground truth). This is what the judge grades against — NOT the answerer's excerpts (Codex F1)."""
    cites = parse_emitted_citations(answer_text)
    if not cites:
        return "(the answer cited no file:line locations to verify against the source)"
    blocks = []
    seen: set[tuple[str, int, int]] = set()
    for c in cites:
        key = (c.file, c.start, c.end)
        if key in seen:
            continue
        seen.add(key)
        path = Path(repo_root) / c.file
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            blocks.append(f"[{c.cite()}] — FILE NOT FOUND in the current source.")
            continue
        lo = max(1, c.start - radius)
        hi = min(len(lines), c.end + radius)
        if lo > len(lines):
            blocks.append(f"[{c.cite()}] — line is past end of file ({len(lines)} lines).")
            continue
        body = "\n".join(lines[lo - 1:hi])
        blocks.append(f"[{c.cite()}] actual source lines {lo}-{hi}:\n```\n{body}\n```")
    return "\n\n".join(blocks)


def build_judge_prompt(question: str, answer_text: str, repo_root: Path) -> str:
    """The blinded judge user-turn: question + answer + the ACTUAL LIVE SOURCE at each cited location
    (NO variant label, NO digest, NO answerer excerpts — treatment-neutral ground truth)."""
    live = _live_source_at_citations(repo_root, answer_text)
    return (f"QUESTION:\n{question}\n\n"
            f"ANSWER UNDER REVIEW:\n{answer_text}\n\n"
            f"=== ACTUAL CURRENT SOURCE AT THE ANSWER'S CITED LOCATIONS (ground truth) ===\n{live}")


class JudgeProtocolError(ValueError):
    """The judge REPLY could not be turned into a valid score (no JSON, no score key, non-numeric or
    out-of-range score). Distinct from a TRANSPORT failure (a broken gateway response decoded inside
    `_call_opus`): this one is fixed by a corrective reprompt, not by network backoff. Subclasses
    ValueError so existing callers/tests that catch ValueError still work."""


def parse_judge_score(raw: str) -> float:
    """Extract the score from the judge's JSON reply via STRICT parsing (Codex A/B-judge-F6: a greedy
    regex accepted corrupted results — a nested {"score":0.2} in the `why` field, `99` clamped to
    1.0 hiding a protocol violation, `0.8.2` truncated to 0.8). Now: find the outermost JSON object,
    `json.loads` it, require a numeric `score` in [0,1]. Any parse/validation failure RAISES
    JudgeProtocolError — a scoring failure must be visible and reprompted per-item, never silently
    clamped into valid-looking evidence. (All internal errors — JSONDecodeError, OverflowError on a
    huge int — are normalized to JudgeProtocolError so the judge loop treats them as protocol, not
    transport, failures; Codex xval P1/P3.)"""
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise JudgeProtocolError(f"no JSON object in judge reply: {raw[:120]!r}")
    try:
        obj = json.loads(raw[start:end + 1])  # strict — malformed JSON is a protocol failure here
    except ValueError as e:
        # ValueError (not just JSONDecodeError): json.loads ALSO raises a bare ValueError for an
        # integer exceeding Python's 4300-digit str->int limit, BEFORE any JSONDecodeError (Codex
        # pass-2). Any failure to parse the judge's own reply is a protocol failure, so catch both.
        raise JudgeProtocolError(f"unparseable JSON in judge reply: {raw[:120]!r}") from e
    if "score" not in obj:
        raise JudgeProtocolError(f"judge reply has no 'score' key: {raw[:120]!r}")
    score = obj["score"]
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise JudgeProtocolError(f"judge 'score' is not numeric: {score!r}")
    try:
        score_f = float(score)  # a huge JSON int raises OverflowError — normalize to protocol failure
    except (OverflowError, ValueError) as e:
        raise JudgeProtocolError(f"judge 'score' not a finite float: {score!r}") from e
    if not (0.0 <= score_f <= 1.0):
        raise JudgeProtocolError(f"judge 'score' out of [0,1]: {score!r} — protocol violation, not clamped")
    return score_f


def _call_opus(prompt: str, *, max_tokens: int = 256, timeout: float = 60.0) -> str:
    """Grade one blinded prompt with a frontier model.

    Default path: route through the target's own `claude`/`codex` CLI (the CLI adapter) —
    no Foundry, no internal endpoint. Only if CODEQA_JUDGE_BASE is explicitly set does the
    request go over HTTP to that user-supplied messages endpoint. Never embeds a credential.
    """
    base, backend, model = _judge_config()      # resolved fresh each call (Codex #3)
    system_prompt = _RUBRIC
    if base is None:
        # CLI adapter path (the portable default) — tools disabled, model via env.
        from ..backend import cli_adapter
        full = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        try:
            return cli_adapter.model_call(full, backend=backend, model=model,
                                          timeout=timeout).content
        except cli_adapter.AdapterError as e:
            raise JudgeProtocolError(f"CLI judge call failed: {e}") from e

    # Explicit HTTP endpoint the user pointed us at (power-user override).
    body = json.dumps({
        "model": model, "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    auth = os.environ.get("CODEQA_JUDGE_AUTH")
    if auth:
        headers["Authorization"] = auth if auth.lower().startswith("bearer ") else f"Bearer {auth}"
    key = os.environ.get("CODEQA_JUDGE_APIM_KEY")
    if key:
        headers["Ocp-Apim-Subscription-Key"] = key
    req = urllib.request.Request(base.rstrip("/") + "/v1/messages",
                                 data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    return "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")


def judge_preflight(repo_root, *, call_fn: Callable[[str], str] | None = None) -> tuple[bool, str]:
    """One cheap grading call to confirm the judge is reachable + the credential works, BEFORE the
    expensive N×V answering. Returns (ok, message). Catches the exact failure the live run hit: a
    missing/expired token → every grade 401s after all the local work is already spent."""
    call = call_fn or _call_opus
    tmp = Path(repo_root)
    # a trivial gradeable prompt over a real path (the repo root always has SOME file to cite)
    probe = build_judge_prompt("preflight: is the judge reachable?",
                               "this is a connectivity probe (no real answer).", tmp)
    try:
        raw = call(probe)
    except Exception as e:  # noqa: BLE001 — surface the transport/auth failure as a clean message
        detail = getattr(e, "code", None) or type(e).__name__
        if str(detail) == "401":
            return False, ("401 Unauthorized — no valid credential reached the gateway "
                           "(CODEQA_JUDGE_AUTH not set in this shell, or the token expired).")
        return False, f"judge call failed: {detail}"
    try:
        parse_judge_score(raw)
    except ValueError:
        # a 200 that isn't a valid score is still 'reachable' — the run's per-item retry handles it
        return True, "reachable (probe reply was not a clean score, but auth works)"
    return True, "reachable"


def is_transient_judge_error(exc: BaseException) -> bool:
    """True for failures worth RETRYING (rate-limit / server / timeout / dropped connection), False
    for permanent ones (auth, bad request, protocol). A 401 must NOT burn retries (a bad credential
    won't heal), but a connection reset under load MUST be retried.

    Transient: HTTP 408/429/all 5xx (incl. Anthropic 529 'overloaded'); timeouts (TimeoutError, or
    URLError wrapping socket.timeout); dropped connections (ConnectionError/ConnectionResetError,
    bare or URLError-wrapped); a bare JSONDecodeError, which can only escape from `_call_opus`
    decoding a broken/truncated GATEWAY response (a bad *judge* reply is normalized to
    JudgeProtocolError in parse_judge_score, never reaches here). A JudgeProtocolError is NOT transient
    (it's handled by the reprompt path, not this classifier). Everything else (4xx) is permanent.

    NOTE (Step-3-rerun bug): the first cut only treated timeouts as transient URLErrors, so a
    ConnectionReset (common across 33 rapid proxied calls) was classed permanent → 0 retries → 10/33
    grades lost. Connection drops + gateway decode errors are now covered."""
    import socket
    import urllib.error
    if isinstance(exc, JudgeProtocolError):
        return False  # a bad judge REPLY — fixed by reprompt, not by network backoff
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 429) or 500 <= exc.code < 600
    if isinstance(exc, (TimeoutError, ConnectionError)):  # ConnectionResetError ⊂ ConnectionError
        return True
    if isinstance(exc, json.JSONDecodeError):  # only from a broken GATEWAY body in _call_opus
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (socket.timeout, TimeoutError, ConnectionError))
    return False


def opus_judge_fn(repo_root, *, call_fn: Callable[[str], str] | None = None,
                  max_attempts: int = 3, sleep_fn: Callable[[float], None] | None = None):
    """Build a blinded judge_fn(question, answer_text) -> [0,1] backed by Opus, grading against the
    LIVE code at `repo_root` (treatment-neutral ground truth — Codex F1). `call_fn` is the injectable
    API seam (defaults to the real proxy call); pass a fake in tests.

    Makes up to `max_attempts` TOTAL grading calls (default 3 = 1 initial + 2 retries), retrying only
    TRANSIENT transport failures with exponential backoff — a single rate-limit/timeout blip must not
    become a permanent unscored hole (the Step-3 bug: 8/33 grades lost, then decide() compared
    mismatched denominators). A PERMANENT error (401/400) or a protocol ValueError is raised
    immediately — no point retrying a bad credential or a malformed reply. `max_attempts` is floored
    at 1 (always at least one call). `sleep_fn` is injectable so tests don't actually wait."""
    call = call_fn or _call_opus
    sleep = sleep_fn or time.sleep
    attempts = max(1, max_attempts)  # always make at least one call — 0 would be a silent no-op
    root = Path(repo_root)

    def _judge(question: str, answer_text: str) -> float:
        base_prompt = build_judge_prompt(question, answer_text, root)
        prompt = base_prompt
        last_exc: BaseException | None = None
        for attempt in range(attempts):
            try:
                return parse_judge_score(call(prompt))
            except JudgeProtocolError as e:
                # PROTOCOL failure — the reply had no parseable {"score":...} (Opus preambled with
                # prose, "Let me evaluate...", on hard/'cannot answer' cases). This was THE dominant
                # Step-3-rerun failure (10/33). Retrying the SAME prompt is futile, but a CORRECTIVE
                # reprompt that re-demands JSON usually recovers it. No network backoff — it's not a
                # transport problem. NB: caught by TYPE, not bare ValueError — a JSONDecodeError from a
                # broken GATEWAY response inside call() is transport, and must NOT land here (Codex P1).
                last_exc = e
                prompt = base_prompt + _JSON_ONLY_REMINDER
            except Exception as e:  # noqa: BLE001 — transport failure: classify, then retry-or-raise
                if not is_transient_judge_error(e):
                    raise  # permanent (auth/4xx) — fail now, don't waste the run's time
                last_exc = e
                if attempt < attempts - 1:
                    sleep(0.5 * (2 ** attempt))  # 0.5s, 1s, 2s, ... — bounded exponential backoff
        # exhausted attempts (persistent transient transport OR persistent prose) — a real failure
        assert last_exc is not None  # guaranteed set: attempts ≥ 1, and a success would have returned
        raise last_exc
    return _judge
