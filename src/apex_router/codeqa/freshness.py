"""codeqa freshness gate — validate a memory/digest's claims against live code, strike the stale ones.

Motivation (measured this session): pinning a distilled doc as answerer context does NOT add lift and
a STALE doc actively HURTS (it misleads the model into confidently-wrong answers, which in an agentic
loop change the next request → bust the provider cache). So the load-bearing value of "memory quality"
is a GATE: before a memory is pinned, check each factual claim against the live code and strike the
ones the code contradicts. A 4-arm A/B measured this recovering a corrupted memory from 0.33 → 0.64.

The proven recipe (/tmp/symbol_final.py, 3/3 end-to-end):
  1. extract the SYMBOLS a claim names (identifiers, dotted paths, filenames, numeric constants)
  2. grep their DEFINITION LINES only — precision, NOT recall. Surrounding context HURT (buried the
     decisive line in noise); the tight definition line is what lets a verifier reason correctly.
  3. a VERIFIER decides SUPPORTED / CONTRADICTED / UNVERIFIABLE. Injectable seam: a frontier model in
     prod (it clears the default-value→state inference a local 35B misses), a fake in tests.

BOUNDARY (measured, not a bug): value/constant + default-implied-state claims are code-decidable; a
pure RUNTIME-state claim ("a policy IS loaded right now") needs a runtime oracle (filesystem/status),
not source — the verifier correctly returns UNVERIFIABLE there rather than guess.
"""
from __future__ import annotations

import enum
import json
import os
import re
import signal
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


class Verdict(str, enum.Enum):
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    UNVERIFIABLE = "UNVERIFIABLE"


# Words that look like identifiers but carry no checkable code meaning — never grep-targets.
_STOP = {
    "the", "and", "for", "that", "with", "this", "from", "not", "are", "was", "has", "its", "per",
    "every", "block", "claim", "posture", "apex", "code", "policy", "field", "value", "true", "false",
    "fire", "fires", "firing", "active", "measure", "only", "current", "build", "wire", "bytes",
    "rewrite", "rewriting", "threshold", "picked", "round", "system", "generally", "works", "well",
    "overall", "team", "prefers", "small", "verified", "steps", "defaults",
}

_SYMBOL_RE = re.compile(
    r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+'   # dotted path / filename (a.b, doctor.py)
    r'|[A-Za-z_][A-Za-z0-9_]*'                       # identifier (any length; filtered below)
    r'|\d+\.\d+|\d+'                                 # numeric constant — full decimal (1.250, not 250)
)


def extract_symbols(claim: str, *, limit: int = 6) -> list[str]:
    """The checkable tokens a claim names: identifiers, dotted paths, filenames, numeric constants.
    Stopwords and short prose words are dropped so we don't grep for 'value' or 'active'. Longest
    first (most specific), capped — a claim's decisive symbol is usually its longest identifier."""
    out: list[str] = []
    for m in _SYMBOL_RE.findall(claim):
        low = m.lower()
        if low in _STOP:
            continue
        # numeric: keep decimals and 2+ digit ints (a threshold/limit); drop bare 0/1-digit noise
        if re.fullmatch(r'\d+\.\d+|\d{2,}', m):
            if m not in out:
                out.append(m)
            continue
        if m.isdigit():                              # single-digit int → not a distinctive constant
            continue
        # identifier/filename: keep if code-ish (underscore/dot) OR CamelCase/SHOUTY OR distinctive.
        # Codex P2b: was len>=6 which dropped lowercase 4-5 char names (e.g. 'floor'); now len>=4 and
        # not a stopword lets short-but-real identifiers through (stopwords already filtered above).
        if re.search(r'[_.]', m) or not m.islower() or len(m) >= 4:
            if m not in out:
                out.append(m)
    return sorted(out, key=len, reverse=True)[:limit]


# A token that is UNAMBIGUOUSLY code (not just a long English word): has an underscore/dot, is
# CamelCase or SHOUTY_CASE, or is a numeric constant. Used to tell a real code claim ('the FLOOR
# constant is 0.700', 'PolicyEngine.get') from prose that merely uses long words ('readability').
_CODE_SHAPED_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*[._][A-Za-z0-9_]|[A-Z]{2,}|[a-z][A-Z]|_|\d+\.\d+|\b\d{2,}\b')


def has_code_shaped_symbol(claim: str) -> bool:
    """True if the claim names a token that is unambiguously a CODE symbol/constant (underscore, dot,
    CamelCase, SHOUTY, or a numeric literal) — as opposed to plain English words. This is the signal
    that a claim is checkable-against-code even when it also uses preference-ish wording; extract_symbols
    alone is too permissive here (it keeps any 4+ char word) for use as a classification gate."""
    return bool(_CODE_SHAPED_RE.search(claim))


# Source extensions the gate can read (Codex P1a: was .py/.rb only, missed C++/erb/rake/pyi).
_SRC_INCLUDES = [f"--include=*.{e}" for e in
                 ("py", "pyi", "rb", "rake", "erb", "cpp", "cc", "cxx", "hpp", "h", "c", "go", "rs", "ts", "js")]


def _is_filename(sym: str) -> bool:
    return bool(re.search(r'\.[A-Za-z]{1,4}$', sym)) and "/" not in sym  # e.g. doctor.py, base.rb

def _is_number(sym: str) -> bool:
    return bool(re.fullmatch(r'\d+\.\d+|\d{3,}', sym))


def definition_lines(symbol: str, root: Path, *, per_symbol: int = 4) -> list[str]:
    """The DEFINITION sites of `symbol` under `root`. Handles three symbol KINDS distinctly (Codex
    P2a: naive `split('.')[-1]` turned `doctor.py`→`py` and `0.500`→`500`, resolving nothing):
      - identifier  → its def-site (`NAME: type` / `NAME =` / `def`/`class`/`module` NAME)
      - filename    → the assignments/consts INSIDE that file (the claim points AT the file)
      - number      → assignment lines whose value IS that literal (e.g. `... = 0.700`)
    Definition lines ONLY, no surrounding context (precision beats recall — context buried the signal).
    Returns `file:line:code` strings (grep -rnE), deduped, capped per symbol."""
    if _is_number(symbol):
        # a numeric claim: find assignment lines that set something TO this value
        pat = rf'[:=]\s*{re.escape(symbol)}\b'
        args = ["grep", "-rnE", *_SRC_INCLUDES, pat, str(root)]
    elif _is_filename(symbol):
        # the claim names a file → return that file's key definition lines (assignments/def/class)
        try:
            found = subprocess.run(["grep", "-rlE", "--include=" + symbol, r'.', str(root)],
                                   capture_output=True, text=True, timeout=15).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            found = []
        lines: list[str] = []
        for fp in found[:2]:
            try:
                d = subprocess.run(["grep", "-nE", r'^\s*([A-Z_][A-Z0-9_]+\s*=|def |class |module )', fp],
                                   capture_output=True, text=True, errors="replace", timeout=10).stdout
            except (OSError, subprocess.SubprocessError):
                d = ""
            for ln in d.splitlines():
                tagged = f"{fp}:{ln}"
                if tagged not in lines:
                    lines.append(tagged)
        return lines[:per_symbol]
    else:
        base = symbol.split(".")[-1]                          # dotted PATH → last segment (a real ident)
        pat = (rf'^\s*({re.escape(base)}\s*[:=]'              # NAME: type  |  NAME =
               rf'|(async\s+)?def\s+(self\.)?{re.escape(base)}\b'  # def / async def / def self.NAME
               rf'|(class|module)\s+{re.escape(base)}\b)')     # class NAME | module NAME (Ruby)
        args = ["grep", "-rnE", *_SRC_INCLUDES, pat, str(root)]
    try:
        out = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    lines = []
    for ln in out.splitlines():
        if ln not in lines:
            lines.append(ln)
    return lines[:per_symbol]


def _context_for(claim: str, root: Path, *, max_lines: int = 12) -> str:
    """The tight definition-line context for every symbol a claim names (capped)."""
    lines: list[str] = []
    for sym in extract_symbols(claim):
        for ln in definition_lines(sym, root):
            if ln not in lines:
                lines.append(ln)
    return "\n".join(lines[:max_lines])


# ---------- runtime-state oracle (claims code alone can't decide) ----------

# Present-tense state language: "is loaded right now", "currently running", "at the moment". These
# describe the RUNNING system, not the source — code can't decide them, but a filesystem/status oracle
# can. Deliberately narrow so a code fact ("defaults to false", "the constant is X", "the field is
# active in the dataclass") is NOT flagged: an explicit TEMPORAL anchor is required, OR a verb that is
# unambiguously about a running process (loaded/running). "is active/enabled" alone is NOT enough —
# those describe code state too; they only count paired with a temporal word ("is currently active").
_RUNTIME_RE = re.compile(
    r'\b(right now|at the moment|as of now|at runtime|on disk right now'      # explicit temporal anchors
    r'|(is|are) (currently|now|presently)\b'                                  # "is currently ..."
    r'|(currently|presently|right now) (loaded|running|active|enabled|set|present|live)'
    r'|is (loaded|running) (in|on|as|right)?'                                 # running-process verbs
    r'|running in [a-z-]+ mode)\b', re.I)


def is_runtime_claim(claim: str) -> bool:
    """True if the claim asserts present-tense RUNNING-SYSTEM state (decidable only by a runtime oracle,
    not by source). Narrow by design: code facts ('defaults to false', 'the constant is 0.700', 'the
    field is active in the dataclass') are NOT runtime claims — they're settled by definition_lines.
    Requires a temporal anchor or a running-process verb, so 'is active' alone doesn't false-match."""
    return bool(_RUNTIME_RE.search(claim))


# ---------- claim-type classifier + verifier router (measured tier assignment) ----------

class ClaimType(str, enum.Enum):
    """What KIND of claim this is — determines which verifier tier can (cheapest) decide it. Grounded
    in this session's measurements: the local 35B verifier handles direct VALUE claims but HEDGES on
    default-value→state INFERENCE (frontier got those 3/3); RUNTIME needs the oracle; NON_DERIVABLE has
    no code oracle at all (preferences / why-not) and must be skipped, not sent to any verifier."""
    RUNTIME = "runtime"            # present-tense running-system state ("loaded right now")
    INFERENCE = "inference"        # requires reasoning from code (default value → runtime behavior)
    VALUE = "value"               # a direct constant/number/name assertion (local verifier suffices)
    NON_DERIVABLE = "non_derivable"  # no code oracle (preference, decision, "we chose X") → skip


# reasoning cues: a claim whose truth follows from IMPLICATION, not a literal lookup. These are the
# ones the local model hedged on (it reads the value fine but won't chain default→state). Broadened
# (Codex #3) to catch conditional/consequence phrasings that previously fell through to VALUE:
# "when X is false, Y", "if ... then", "X disables/enables Y", "results in", "leading to".
_INFERENCE_RE = re.compile(
    r'\b(defaults?\s+to|by default|so (no|it|the|they|that)|therefore|hence|because|'
    r'means (that|it|the)|implies|as a result|results? in|which (means|disables|enables|causes)|'
    r'unless (set|explicitly)|falls?\s+back|when\s+\w+\s+is\b|if\s+.+\s+then|leading to|'
    r'setting\s+\w+|disables?|enables?|prevents?|triggers?|causes?\s+\w)\b', re.I)

# non-derivable cues: human DECISION / preference language. Strong first-person-decision or explicit
# preference words. NB (Codex #1/#2, my own catch): these fire inside legit code claims ('the team
# module sets API_TIMEOUT', 'apex should never rewrite bytes'), so NON_DERIVABLE is only chosen when
# the claim ALSO names NO checkable code symbol — a genuine preference points at no code (see below).
_NON_DERIVABLE_RE = re.compile(
    r'\b(prefers?|preference|we (chose|decided|picked|use|avoid|rejected|tried|opted)|'
    r'rationale|by convention|for readability|better to|deliberately (chose|avoid)|'
    r'our (approach|convention|preference)|chosen over|instead of)\b', re.I)

# flow / cross-component cues: a claim that traces a CHAIN of relationships across components with a
# flow arrow (`A → B → C`, `x ⇄ y`) is NOT a literal value lookup — its truth is a MULTI-HOP
# relationship the local verifier cannot resolve: it must chain several definition sites, and a flow
# frequently crosses INTO ANOTHER REPO whose symbols are not in this tree at all (a digest for one
# repo naming another repo's handlers). Given only a partial/one-repo evidence slice the local model
# HEDGES and mislabels a true chain CONTRADICTED (measured: a real cross-repo flow bullet struck by
# local, correct on frontier). So a flow claim is INFERENCE → frontier: the tier that reasons about
# missing-half evidence and returns SUPPORTED/UNVERIFIABLE instead of a false strike. Local-model
# agnostic — this is about the LOCAL tier hedging on partial evidence, not any specific model.
# Unicode flow arrows only (the digests' sole flow marker; bare ASCII '->' is code syntax, not prose).
_FLOW_RE = re.compile(r'[\u2190-\u21ff\u27f0-\u27ff]|-->|==>')


def describes_flow(claim: str) -> bool:
    """True if the claim traces a data/control FLOW across components (a flow arrow chain). Such a
    claim is a multi-hop relationship — often cross-repo — that the cheap local verifier resolves
    unreliably, so it must route to frontier rather than be lookup-checked against one repo."""
    return bool(_FLOW_RE.search(claim))


def classify_claim(claim: str) -> ClaimType:
    """Classify a claim into the verifier tier that can decide it, in PRIORITY order (most-specific
    first). Cheap and rule-based — a routing gate, not itself a model call.

      RUNTIME       — present-tense running-system state (needs the runtime oracle + frontier)
      NON_DERIVABLE — a human decision/preference with NO checkable code symbol (skip; nothing grounds
                      it). The no-symbol guard is load-bearing: a code fact that merely CONTAINS
                      preference-ish words ('apex should never rewrite', 'the team module sets X') DOES
                      name symbols, so it is NOT skipped (Codex #1/#2).
      INFERENCE     — truth follows by reasoning from code (default→state / conditional→consequence),
                      OR it traces a FLOW/cross-component chain (often cross-repo). The local model
                      HEDGES on both — and false-CONTRADICTS a chain it can only half-resolve — so
                      these route to frontier.
      VALUE         — a direct constant/name lookup the local verifier handles well (the default)
    """
    if is_runtime_claim(claim):
        return ClaimType.RUNTIME
    # NON_DERIVABLE only when it reads as a decision AND names no CODE-SHAPED symbol. A real preference
    # names no code token ('we prefer small steps'); a code fact with 'better to'/'instead of' still
    # names a code-shaped symbol ('retrieval_ceiling', 'API_TIMEOUT'). Using has_code_shaped_symbol —
    # not extract_symbols — because the latter keeps any 4+ char English word ('readability').
    if _NON_DERIVABLE_RE.search(claim) and not has_code_shaped_symbol(claim):
        return ClaimType.NON_DERIVABLE
    # A flow/cross-component chain is reasoning over MULTIPLE (often cross-repo) definition sites, not a
    # literal lookup — route it to frontier so a partial one-repo evidence slice doesn't make the cheap
    # local verifier false-strike a true chain (the misroute that struck correct cross-repo bullets).
    if _INFERENCE_RE.search(claim) or describes_flow(claim):
        return ClaimType.INFERENCE
    return ClaimType.VALUE


def route_verifier(claim_type: "ClaimType", *, local=None, frontier=None):
    """Pick the verifier for a claim type (the measured tier assignment):
      VALUE         → local (cheap, on-device, sufficient) — falls back to frontier if no local given
      INFERENCE     → frontier (local hedges on default→state reasoning)
      RUNTIME       → frontier (same inference class, plus it reads runtime facts)
      NON_DERIVABLE → None (no verifier can ground it; the caller skips it)
    Returns the chosen verifier object (or None to skip). Never drops a checkable claim: a VALUE claim
    with no local verifier still routes to frontier rather than going unchecked."""
    if claim_type is ClaimType.NON_DERIVABLE:
        return None
    if claim_type is ClaimType.VALUE:
        return local if local is not None else frontier
    return frontier if frontier is not None else local  # INFERENCE / RUNTIME prefer frontier


# Claim-kind → tier_router task-kind (the SECOND axis: which frontier tier decides the claim). VALUE
# lookups are cheap (haiku); INFERENCE needs a reasoner (sonnet); RUNTIME is the hardest (opus). Only
# reached for claims that actually go to the frontier verifier (VALUE-on-frontier when no local model).
_CTYPE_TASK = {
    ClaimType.VALUE: "value",
    ClaimType.INFERENCE: "inference",
    ClaimType.RUNTIME: "runtime",
}


_STATUS_READ_CAP = 4096  # bytes — cap the status read so a faulty endpoint can't exhaust memory/hang
_CMD_OUTPUT_CAP = 800     # chars — cap command output so a runaway command can't flood the prompt


def _default_fetch(url: str, *, timeout: float = 5.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # nosec — read-only status probe
        return r.read(_STATUS_READ_CAP).decode("utf-8", "replace")  # bounded read (Codex P2c)


def run_oracle_command(cmd: str, *, cap: int = _CMD_OUTPUT_CAP, timeout: float = 10.0) -> str:
    """Run a READ-ONLY oracle command and return its output as a fact string. Fail-open and BOUNDED: a
    nonzero exit, a timeout, an OS error, or a malformed command becomes a reported fact, never a raise
    (Codex P2-7). Output is truncated to `cap` chars; the pipe is read with a hard byte cap so a chatty
    command can't consume unbounded memory DURING execution (Codex P2-5). On timeout the whole process
    GROUP is killed so a backgrounded child can't survive (Codex P2-6). The oracle spec is trusted,
    local config (same trust boundary as status_url) — commands must be read-only by the config author;
    this runner does not sandbox, it bounds + fails-open."""
    if not isinstance(cmd, str) or "\x00" in cmd:              # P2-7: non-str / embedded NUL → a fact
        return "(invalid command spec)"
    read_cap = max(cap, 0) + 4096                              # bounded pipe read (P2-5)
    try:
        # start_new_session so we can kill the whole group on timeout (P2-6)
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace", start_new_session=True)  # nosec — trusted config
    except (OSError, ValueError) as e:
        return f"(command error: {type(e).__name__})"
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)   # kill the group, not just the shell
        except (OSError, ProcessLookupError):
            proc.kill()
        proc.communicate()
        return f"(timeout after {timeout}s)"
    except (OSError, ValueError) as e:
        return f"(command error: {type(e).__name__})"
    out = (out or "")[:read_cap].strip()
    if proc.returncode != 0:
        e = (err or "")[:120].strip()
        return f"(exit {proc.returncode}{': ' + e if e else ''}){(' ' + out) if out else ''}"[:cap]
    return out[:cap] if out else "(no output)"


def gather_runtime_facts(spec: dict, *, fetch_fn: Callable[[str], str] = _default_fetch,
                         run_fn: Callable[[str], str] = run_oracle_command) -> str:
    """Build a plain-text RUNTIME-FACTS block from a repo's runtime-oracle `spec`:
      {"files": [existence checks], "status_url": "http://.../status",
       "commands": [{"label": "...", "cmd": "read-only shell"}]}
    Read-only and fail-open: a missing file reports ABSENT, an unreachable status/command reports its
    failure — the gate never crashes or mutates state because the running system is down. Returns "" when
    the spec yields NO observations, so an empty oracle is NOT mistaken for evidence (Codex P2a). A
    malformed spec is tolerated, not crashed (Codex P2b). `fetch_fn`/`run_fn` are injectable seams."""
    body_lines: list[str] = []
    files = spec.get("files")
    if isinstance(files, (list, tuple)):                      # tolerate files=None / non-iterable (P2b)
        for f in files:
            if not f:
                continue
            name = Path(str(f)).name
            body_lines.append(f"  - {name}: {'EXISTS' if Path(str(f)).exists() else 'ABSENT'}")
    url = spec.get("status_url")
    if url:
        try:
            body = fetch_fn(url).strip()
            body_lines.append(f"  - {url} → {body[:400]}")
        except Exception as e:  # noqa: BLE001 — a down proxy is a fact, not a crash
            body_lines.append(f"  - {url} → unreachable ({type(e).__name__})")
    commands = spec.get("commands")
    if isinstance(commands, (list, tuple)):
        for c in commands:
            if not isinstance(c, dict) or not c.get("cmd"):
                continue
            label = c.get("label") or c["cmd"]
            body_lines.append(f"  - {label}: {run_fn(c['cmd'])}")
    if not body_lines:
        return ""                                             # no observations → not evidence (P2a)
    return "RUNTIME FACTS (the running system, not the source):\n" + "\n".join(body_lines)


def memory_fingerprint(path, root, *, code_marker: str | None = None) -> str:
    """A stable content fingerprint for auto-wire caching: only re-validate a memory when it (or the
    code it describes) changed. Folds the memory bytes + a `code_marker` (e.g. the repo's git HEAD) so
    a byte-identical memory still re-validates when CODE moved (a claim goes stale because code did)."""
    import hashlib
    h = hashlib.sha256()
    try:
        h.update(Path(path).read_bytes())
    except OSError:
        h.update(b"<unreadable>")
    h.update(b"\x00")
    h.update(str(Path(root)).encode("utf-8", "replace"))       # same memory vs different repo → distinct
    h.update(b"\x00")
    h.update((code_marker or "").encode("utf-8", "replace"))
    return h.hexdigest()[:16]


# ---------- the verifier seam ----------

_VERIFIER_SYS = (
    "You are a fact-checker for a codebase memory. Given a CLAIM and the DEFINITION LINES of the "
    "symbols it names, reason about what the code IMPLIES (a field DEFAULTING to False means that "
    "state is off unless explicitly set; a constant's value is what the code assigns). Answer with "
    "ONLY one word: SUPPORTED, CONTRADICTED, or UNVERIFIABLE (only if the definitions are truly "
    "silent). Prefer CONTRADICTED when the code shows the opposite."
)


def frontier_verifier(claim: str, code: str) -> str:
    """Frontier verifier over a user-configured HTTP endpoint (OPT-IN via CODEQA_JUDGE_BASE).

    Like the judge, there is deliberately NO agentic-CLI path: a verifier call embeds
    scanned source that may be adversarial, and the local `claude`/`codex` CLI cannot be
    safely isolated from repo-local hooks/plugins/MCP. If CODEQA_JUDGE_BASE is unset, this
    returns "" (-> CANNOT-DECIDE upstream); use the LOCAL verifier instead. Credentials, if
    the endpoint needs them, come from the env — never embedded. Returns a raw verdict word."""
    # Single source of truth for config + HTTP handling so judge/verifier never diverge.
    from .judge import _judge_config, _http_post_json, _extract_text, JudgeProtocolError
    from . import tier_router
    base, _ = _judge_config()      # endpoint (base); model + effort come from the tier router
    prompt = f"CLAIM:\n{claim}\n\nDEFINITION LINES:\n{code}\n\nOne word:"

    if base is None:
        return ""      # no frontier endpoint configured -> CANNOT-DECIDE (use --local)

    # Tier by claim-kind: VALUE→haiku (cheap lookup), INFERENCE→sonnet, RUNTIME→opus (see _CTYPE_TASK).
    route = tier_router.resolve(_CTYPE_TASK.get(classify_claim(claim), "value"))
    max_tokens = max(8, tier_router.min_max_tokens(route))
    timeout = max(60, tier_router.timeout_for(route))
    body_obj = {
        "model": route.model, "max_tokens": max_tokens, "system": _VERIFIER_SYS,
        "messages": [{"role": "user", "content": prompt}],
    }
    body_obj.update(tier_router.request_extras(route))   # effort + adaptive thinking (sonnet/opus)
    body = json.dumps(body_obj).encode()
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    auth = os.environ.get("CODEQA_JUDGE_AUTH")
    if auth:
        headers["Authorization"] = auth if auth.lower().startswith("bearer ") else f"Bearer {auth}"
    key = os.environ.get("CODEQA_JUDGE_APIM_KEY")
    if key:
        headers["Ocp-Apim-Subscription-Key"] = key
    try:
        payload = _http_post_json(base.rstrip("/") + "/v1/messages", body, headers, timeout=timeout)
    except (JudgeProtocolError, OSError):
        return ""      # unreachable/malformed -> CANNOT-DECIDE
    return _extract_text(payload)


def _normalize(raw: str) -> Verdict:
    v = (raw or "").strip().upper()
    if "CONTRADICT" in v:
        return Verdict.CONTRADICTED
    if "SUPPORT" in v:
        return Verdict.SUPPORTED
    return Verdict.UNVERIFIABLE


# Errors from a verifier call that are EXPECTED (transport/decode) — degrade to UNVERIFIABLE. A
# programming bug (TypeError/AttributeError/…) is NOT in here, so it propagates instead of silently
# disabling the gate (Codex P1b: swallowing every Exception hid a broken endpoint behind a clean exit).
_VERIFY_EXPECTED_ERRORS = (
    OSError, urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError, ValueError,
)


def check_claim(claim: str, root: Path, *,
                verify_fn: Callable[[str, str], str] = frontier_verifier,
                runtime_facts: str | None = None) -> tuple[Verdict, bool]:
    """(verdict, verifier_ran) for a single claim.

    Evidence = the tight definition lines for the claim's symbols, PLUS `runtime_facts` when supplied
    (the running-system oracle — filesystem/status). A RUNTIME claim ("a policy is loaded right now")
    is code-undecidable: without runtime_facts it stays (UNVERIFIABLE, False) and spends no verifier
    call (don't guess from source); WITH runtime_facts it becomes decidable. A code claim needs a
    resolved definition line as before. An EXPECTED verifier failure (network/decode) → (UNVERIFIABLE,
    True) — ran but couldn't decide, never strikes. A programming error propagates."""
    code = _context_for(claim, Path(root)).strip()
    runtime = (runtime_facts or "").strip()
    # Decide on whatever EVIDENCE is available — code definitions and/or the runtime oracle. Verifiable
    # iff EITHER resolves; if neither, don't guess. This naturally handles both cases without an
    # is_runtime_claim branch here: a pure runtime-state claim ('is loaded right now') names no
    # resolvable code symbol, so with no oracle it has no evidence → UNVERIFIABLE; and a claim that
    # merely READS present-tense ('the field is active') but DID resolve a code definition is still
    # checked against that code (no false-positive skip). is_runtime_claim is used by validate_memory
    # to decide which no-symbol lines are worth sending to the oracle.
    evidence = "\n\n".join(p for p in (code, runtime) if p)
    if not evidence:
        return Verdict.UNVERIFIABLE, False               # no code and no runtime facts → don't guess
    try:
        return _normalize(verify_fn(claim, evidence)), True
    except _VERIFY_EXPECTED_ERRORS:
        return Verdict.UNVERIFIABLE, True


# ---------- the gate ----------

@dataclass
class ValidationResult:
    text: str                                   # the memory with CONTRADICTED claims struck/flagged
    n_checked: int = 0                          # claims that had checkable symbols (verifier ran)
    n_struck: int = 0                           # claims the code CONTRADICTED
    struck_claims: list[str] = field(default_factory=list)
    n_skipped: int = 0                          # bullet claims routed to NON_DERIVABLE (no oracle)
    skipped_claims: list[str] = field(default_factory=list)  # so a mis-skip is VISIBLE, not silent (Codex #5)
    n_local: int = 0                            # claims routed to the LOCAL verifier (free) — the cost split
    n_frontier: int = 0                         # claims routed to the FRONTIER verifier (paid tokens)
    tier_calls: dict = field(default_factory=dict)  # frontier tier → count (haiku/sonnet/opus) — the model-picker split


_STRIKE = "  ~~[STALE: contradicted by live code — removed by freshness gate]~~"
_BULLET = re.compile(r'^\s*[-*]\s+\S')


def validate_memory(text: str, root: Path, *,
                    verify_fn: Callable[[str, str], str] = frontier_verifier,
                    local_verify_fn: Callable[[str, str], str] | None = None,
                    runtime_facts: str | None = None,
                    min_len: int = 40,
                    max_workers: int = 8) -> ValidationResult:
    """Check each substantive bullet CLAIM in `text` against the live code under `root` (and, when
    `runtime_facts` is supplied, the running-system oracle for present-tense state claims); replace the
    ones CONTRADICTED with a struck marker (original recorded in struck_claims). Only bullet lines
    longer than `min_len` that name a checkable symbol OR make a runtime-state assertion are verified —
    headers, prose, and non-code notes pass through untouched (a preference/why-not has no oracle).

    COST ROUTING (measured −62% frontier tokens, no accuracy loss vs all-frontier): when
    `local_verify_fn` is supplied, each claim is routed by classify_claim — VALUE claims (direct
    constant/name lookups) go to the free local verifier, INFERENCE/RUNTIME claims (which the local
    model hedges on) go to the frontier `verify_fn`. Without a local verifier, everything uses
    `verify_fn` as before.

    THREAD-SAFETY CONTRACT (Codex): claims are verified CONCURRENTLY (max_workers>1), so `verify_fn`
    and `local_verify_fn` may be invoked from multiple threads at once. They MUST be reentrant /
    thread-safe — build per-call state, don't rely on a shared mutable cursor/session. The shipped
    verifiers satisfy this: `frontier_verifier` builds a fresh request per call; the local (Ornith)
    verifier is serialized by ornith_client's inference file-lock. A stateful custom verifier must
    either be made thread-safe or run serially (pass max_workers=1)."""
    from concurrent.futures import ThreadPoolExecutor
    root = Path(root)
    lines = text.splitlines()
    n_skipped = 0
    skipped: list[str] = []

    # PASS 1 (fast, serial, deterministic): decide each line's disposition. Fence + skip logic stays
    # here so it's identical to the serial version; only the model-bound check_claim is deferred.
    # jobs[i] = (line_idx, claim_str, chosen_verifier, claim_type)
    jobs = []
    in_fence = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            in_fence = not in_fence
            continue
        checkable = extract_symbols(s) or (runtime_facts and is_runtime_claim(s))
        if not in_fence and _BULLET.match(line) and len(s) >= min_len and checkable:
            ctype = classify_claim(s)
            chosen = route_verifier(ctype, local=local_verify_fn, frontier=verify_fn) \
                if local_verify_fn is not None else verify_fn
            if chosen is None:                           # NON_DERIVABLE → skip (Codex #5: recorded)
                n_skipped += 1
                skipped.append(s)
                continue
            jobs.append((i, s, chosen, ctype))

    # PASS 2 (concurrent): the check_claim calls are independent + I/O-bound (model calls), so run them
    # in a thread pool. Order-independent — each result is keyed back to its line index. A single claim
    # (or max_workers<=1) runs inline so the trivial/test path spawns no threads.
    def _run(job):
        _, s, chosen, _ = job
        verdict, ran = check_claim(s, root, verify_fn=chosen, runtime_facts=runtime_facts)
        # CONFIRM-BEFORE-STRIKE: a CONTRADICTED from the LOCAL verifier is a DESTRUCTIVE verdict
        # (it strikes the claim) produced by the tier that measurably HEDGES on partial / one-repo /
        # cross-repo evidence — the exact failure that struck true cross-repo claims. So a local strike
        # is not trusted on its own: escalate it to the frontier verifier and keep the strike ONLY if
        # frontier CONFIRMS. Leniency (SUPPORTED/UNVERIFIABLE) is free and never escalated, so cost is
        # bounded by the (rare) strike rate, not the claim count. Local-model agnostic: 'local' is
        # whatever verifier was injected (qwen via ollama here, or any other) — the escalation is about
        # the LOCAL tier's known hedging, not a specific model. Only fires in routed mode (a distinct
        # local+frontier pair exists); pure --local or pure-frontier runs are unaffected.
        escalated = False
        if (verdict is Verdict.CONTRADICTED and local_verify_fn is not None
                and chosen is local_verify_fn and verify_fn is not local_verify_fn):
            verdict, ran = check_claim(s, root, verify_fn=verify_fn, runtime_facts=runtime_facts)
            escalated = True
        return verdict, ran, escalated
    if len(jobs) > 1 and max_workers > 1:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(jobs))) as ex:
            results = list(ex.map(_run, jobs))
    else:
        results = [_run(j) for j in jobs]

    # PASS 3 (serial, deterministic): fold results back in original order — counts + strike markers.
    from . import tier_router
    n_checked = n_struck = n_local = n_frontier = 0
    tier_calls: dict[str, int] = {}                      # frontier tier → count (the model-picker split)
    struck: list[str] = []
    strike_at: dict[int, str] = {}                       # line_idx → replacement marker line
    for (i, s, _chosen, ctype), (verdict, ran, escalated) in zip(jobs, results):
        if ran:
            n_checked += 1
            # an escalated claim spent a PAID frontier call (the free local one preceded it) → count it
            # as frontier so est_frontier_tokens stays honest; only a non-escalated VALUE call is free.
            if local_verify_fn is not None and ctype is ClaimType.VALUE and not escalated:
                n_local += 1
            else:
                n_frontier += 1
                # Same deterministic route the frontier verifier used → tally which tier decided it. An
                # escalated VALUE claim was confirmed by the frontier route for its own type (value).
                tier = tier_router.resolve(_CTYPE_TASK.get(ctype, "value")).tier
                tier_calls[tier] = tier_calls.get(tier, 0) + 1
        if verdict is Verdict.CONTRADICTED:
            n_struck += 1
            struck.append(s)
            line = lines[i]
            marker = line[:len(line) - len(line.lstrip())] + line.lstrip()[0]
            strike_at[i] = marker + _STRIKE

    out_lines = [strike_at.get(i, line) for i, line in enumerate(lines)]
    return ValidationResult(text="\n".join(out_lines), n_checked=n_checked,
                            n_struck=n_struck, struck_claims=struck,
                            n_skipped=n_skipped, skipped_claims=skipped,
                            n_local=n_local, n_frontier=n_frontier, tier_calls=tier_calls)


# ---------- metrics: a differentiable, benchmarkable record per validation run ----------

# One frontier verifier call ≈ 268 input + 8 output tokens (measured this session). Used to derive a
# comparable est_frontier_tokens so runs — and the routing token-saving — are benchmarkable over time.
_FRONTIER_TOKENS_PER_CALL = 276


_METRICS_MAX_STRUCK = 20      # cap struck-claim list per record so one huge run can't bloat the file
_METRICS_MAX_BYTES = 5_000_000  # rotate the metrics file past ~5 MB so it can't grow unbounded (P2-5)


def metrics_record(path, run: dict, *, ts: str) -> None:
    """Append ONE JSONL metrics line for a validation run to `path` (created if absent). `run` carries
    the differentiators — repo, file, n_struck, n_local, n_frontier, n_skipped, cached, routed — and
    `ts` is an ISO timestamp (passed in; the module never calls the clock so it stays deterministic).
    Adds a derived `est_frontier_tokens` (n_frontier × per-call estimate) so the token cost of a run,
    and the routing saving, are comparable across runs.

    Robustness (Codex): the struck list is capped per record (P2-5a); the file is rotated past a size
    cap so it can't grow unbounded (P2-5b); the append is a SINGLE os.write under an advisory lock so
    concurrent hooks/cron can't interleave records (P2-4). Fail-open throughout — metrics are
    observability, never a gate that can break the run."""
    import fcntl
    import json
    rec = dict(run)
    rec["ts"] = ts
    rec["est_frontier_tokens"] = int(rec.get("n_frontier", 0)) * _FRONTIER_TOKENS_PER_CALL
    if isinstance(rec.get("struck"), list) and len(rec["struck"]) > _METRICS_MAX_STRUCK:
        rec["struck"] = rec["struck"][:_METRICS_MAX_STRUCK] + [f"…(+{len(rec['struck']) - _METRICS_MAX_STRUCK} more)"]
    payload = (json.dumps(rec) + "\n").encode("utf-8")
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:                                            # size-based rotation (keep one .1 backup)
            if p.exists() and p.stat().st_size > _METRICS_MAX_BYTES:
                p.replace(p.with_suffix(p.suffix + ".1"))
        except OSError:
            pass
        # O_APPEND + a single write is atomic for small records on local FS; the flock guards the
        # rare cross-FS/large-write case where POSIX doesn't guarantee it (P2-4).
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                pass                                    # locking unsupported (some FS) → best-effort append
            os.write(fd, payload)
        finally:
            os.close(fd)
    except OSError:
        pass
