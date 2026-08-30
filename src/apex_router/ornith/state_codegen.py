"""SKILL.state-style codegen lane — explicit execution state instead of a growing transcript.

Prototype of arXiv:2608.26263 (SKILL.state) applied to the codegen lane. The one-shot
`offload_lanes.codegen_lane` generates once, tests, and escalates cold on failure. A naive
self-repair retry would APPEND test failures to a growing chat history — the exact anti-pattern
the paper measures (O(T²) tokens; stale reasoning anchoring; 5–8 turn recovery hallucination
when the environment contradicts history). This lane instead runs the paper's loop:

    prompt_t = (immutable spec P, current state Σ_t, latest observation O_t)
    model emits ΔΣ_t ONLY — a JSON patch, never the full state, never reasoning to keep
    runtime validates ΔΣ, merges server-side (null deletes a key), runs the tests
    O_{t+1} = test output; the model's reasoning is DISCARDED after each step

State schema (authored once per lane, per the paper's "schema per domain"):

    code         str        current candidate — REQUIRED once set; a patch cannot delete it
    fix_summary  str        one line describing the last change
    open_issues  [str]      known remaining problems (null clears)

Measured-defense design (paper §5.7, open-weight error taxonomy — 68% premature state
overwrite / 20% schema-type / 12% JSON syntax):
  - PATCH-ONLY updates make the 68% overwrite mode structural­ly impossible: the model can
    never omit a key, because keys it doesn't mention are preserved by the server-side merge.
  - Every patch is validated against the schema BEFORE merging; a rejected patch is fed back
    as the next observation (bounded by max_patch_retries) instead of corrupting the state.
  - A model that ignores the JSON contract and emits a bare fenced code block is SALVAGED
    as {"code": ...} and the step is counted under the `json_syntax` taxonomy — that count is
    the degradation signal for "this local model can't do structured output" (the paper's
    motivation for constrained decoding).

Verdict doctrine is unchanged from codegen_lane: ok == the caller's tests passed == gated.
Every model call's usage is SUMMED into LaneResult.usage so the worker's telemetry books the
full retry cost — a state loop that passes on attempt 3 spent 3 calls and the economics must
show that. Per-step detail (attempts, taxonomy, final state) rides in LaneResult._extra for
the bench harness; the worker ignores it.

Env: ORNITH_CODEGEN_MAX_ATTEMPTS (default 3), ORNITH_CODEGEN_PATCH_RETRIES (default 2),
ORNITH_CODEGEN_STATE_OBS_CHARS (observation truncation, default 1500).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from .offload_lanes import LaneResult, run_python_tests

# ----------------------------------------------------------------------------------------------
# State schema — the paper's "authored once per domain" artifact, here per lane.
# ----------------------------------------------------------------------------------------------

SCHEMA: dict[str, type] = {"code": str, "fix_summary": str, "open_issues": list}
NON_DELETABLE = {"code"}  # deleting the candidate = the paper's "premature overwrite" mode

# Taxonomy labels match paper §5.7 so bench rows are directly comparable to its Table.
TAX_OVERWRITE = "premature_overwrite"
TAX_SCHEMA = "schema_type"
TAX_JSON = "json_syntax"

_CODE_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def merge_patch(state: dict, patch: dict) -> dict:
    """Σ_{t+1} = Σ_t ⊕ ΔΣ_t — flat merge, null deletes. Assumes patch already validated."""
    out = dict(state)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


def validate_patch(patch, state: dict) -> tuple[str, str] | None:
    """Validate ΔΣ against the lane schema. Returns (taxonomy_kind, detail) or None if valid.

    Checked BEFORE merging — a bad patch never touches the state (the paper's runtime
    'deterministically validates the proposed state transition').
    """
    if not isinstance(patch, dict):
        return (TAX_SCHEMA, f"patch is {type(patch).__name__}, not a JSON object")
    for k, v in patch.items():
        if k not in SCHEMA:
            return (TAX_SCHEMA, f"unknown key {k!r} (schema: {sorted(SCHEMA)})")
        if v is None:
            if k in NON_DELETABLE:
                return (TAX_OVERWRITE, f"patch deletes required key {k!r}")
            continue
        want = SCHEMA[k]
        if not isinstance(v, want):
            return (TAX_SCHEMA, f"key {k!r} must be {want.__name__}, got {type(v).__name__}")
        if k == "open_issues" and not all(isinstance(x, str) for x in v):
            return (TAX_SCHEMA, "open_issues must be a list of strings")
    merged = merge_patch(state, patch)
    if not merged.get("code"):
        return (TAX_SCHEMA, "patch leaves state with no 'code' to test")
    return None


def parse_model_patch(raw: str) -> tuple[dict | None, str | None]:
    """Parse the model's answer into a patch. Returns (patch, salvage_kind).

    (patch, None)        — clean structured output
    (patch, TAX_JSON)    — JSON contract failed but a fenced code block was salvaged as
                           {"code": ...}; the step still counts against the json_syntax taxonomy
    (None, TAX_JSON)     — nothing usable
    """
    text = raw.strip()
    # Strip a single wrapping fence if the model fenced its JSON (```json ... ```).
    m = _CODE_BLOCK.search(text)
    candidates = [text]
    if m and m.group(1).strip().startswith("{"):
        candidates.insert(0, m.group(1).strip())
    # Also try the widest {...} span — models often pad prose around the object.
    lo, hi = text.find("{"), text.rfind("}")
    if 0 <= lo < hi:
        candidates.append(text[lo:hi + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return (obj, None)
    # Salvage path: bare fenced code (the one-shot lane's output format).
    if m and m.group(1).strip():
        return ({"code": m.group(1).strip(), "fix_summary": "(salvaged from fenced block)"},
                TAX_JSON)
    return (None, TAX_JSON)


# ----------------------------------------------------------------------------------------------
# Prompt construction — (P, Σ_t, O_t), in that byte order so the immutable prefix stays
# cache-friendly. Reasoning is never carried: each prompt is built from state alone.
# ----------------------------------------------------------------------------------------------

_INSTRUCTIONS = """\
=== INSTRUCTIONS ===
Reply with ONLY a JSON object: a PATCH to the state above.
- "code": the FULL replacement Python code as a string (required — your current best candidate).
- "fix_summary": one line describing what you changed and why.
- "open_issues": list of remaining known problems, or null to clear it.
- A null value DELETES a key. Never delete "code". Do not repeat keys you are not changing.
- No prose, no markdown: the JSON object only."""


def build_prompt(spec: str, state: dict, observation: str) -> str:
    """The paper's A_t = (P, Σ_t, O_t): spec, structured state, latest observation — nothing else."""
    return (
        f"=== TASK SPEC (immutable) ===\n{spec}\n\n"
        f"=== CURRENT STATE (JSON) ===\n{json.dumps(state, indent=2)}\n\n"
        f"=== LATEST OBSERVATION ===\n{observation}\n\n"
        f"{_INSTRUCTIONS}"
    )


def _tail(text: str, chars: int) -> str:
    """Keep the END of test output — the failure signature lives there, not at the top."""
    text = (text or "").strip()
    return text if len(text) <= chars else "…" + text[-chars:]


@dataclass
class StepMetrics:
    """Per-model-call bookkeeping for the bench harness (rides in LaneResult._extra)."""
    calls: int = 0
    attempts: int = 0          # valid patches applied + tests run
    rejected: int = 0          # patches that failed parse/validation
    taxonomy: dict = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    def bump_tax(self, kind: str) -> None:
        self.taxonomy[kind] = self.taxonomy.get(kind, 0) + 1

    def add_usage(self, usage: dict | None) -> None:
        from .offload_telemetry import usage_tokens
        p, c, k = usage_tokens(usage)
        self.prompt_tokens += p
        self.completion_tokens += c
        self.cached_tokens += k

    def usage_dict(self) -> dict:
        return {"prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
                "prompt_tokens_details": {"cached_tokens": self.cached_tokens}}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def state_codegen_lane(spec: str, tests: str, *, max_tokens: int = 1200,
                       timeout_s: int = 30, max_attempts: int | None = None,
                       max_patch_retries: int | None = None, chat=None) -> LaneResult:
    """Codegen lane with SKILL.state self-repair: patch-only state updates, tests as observation.

    max_attempts     — gated test runs before escalation (env ORNITH_CODEGEN_MAX_ATTEMPTS, def 3).
    max_patch_retries — CONSECUTIVE invalid patches tolerated before escalation (env
                       ORNITH_CODEGEN_PATCH_RETRIES, def 2); the counter resets on any valid
                       patch, and rejections never count as attempts. A model that can't emit a
                       valid patch within this budget escalates immediately — its structured-
                       output failure is the taxonomy signal, not something to burn calls on.
    chat             — injectable `chat(messages, *, max_tokens, enable_thinking)` for tests /
                       the bench; None = live ornith_client (thinking-OFF, temp 0.0 — unchanged
                       from codegen_lane's measured settings).
    """
    if max_attempts is None:
        max_attempts = _env_int("ORNITH_CODEGEN_MAX_ATTEMPTS", 3)
    if max_patch_retries is None:
        max_patch_retries = _env_int("ORNITH_CODEGEN_PATCH_RETRIES", 2)
    obs_chars = _env_int("ORNITH_CODEGEN_STATE_OBS_CHARS", 1500)

    if chat is None:
        from . import ornith_client as oc

        def chat(messages, *, max_tokens, enable_thinking):  # noqa: A002 — shadow for closure
            return oc.chat_messages(messages, max_tokens=max_tokens,
                                    enable_thinking=enable_thinking, temperature=0.0,
                                    raise_on_truncation=False)

    state: dict = {}
    obs = "(initial generation — no test results yet)"
    m = StepMetrics()
    consecutive_rejects = 0
    tests_ran = False
    last_detail = ""

    try:
        while m.attempts < max_attempts and consecutive_rejects <= max_patch_retries:
            m.calls += 1
            result = chat([{"role": "user", "content": build_prompt(spec, state, obs)}],
                          max_tokens=max_tokens, enable_thinking=False)
            m.add_usage(getattr(result, "usage", None))
            raw = getattr(result, "answer", "") or ""

            patch, salvage = parse_model_patch(raw)
            if salvage is not None:
                m.bump_tax(salvage)
            if patch is None:
                m.rejected += 1
                consecutive_rejects += 1
                obs = (f"PATCH REJECTED ({TAX_JSON}): no JSON object or code block found. "
                       "Reply with ONLY the JSON patch object.")
                continue
            err = validate_patch(patch, state)
            if err is not None:
                kind, detail = err
                m.bump_tax(kind)
                m.rejected += 1
                consecutive_rejects += 1
                obs = (f"PATCH REJECTED ({kind}): {detail}. "
                       "Reply with ONLY a valid JSON patch object.")
                continue

            consecutive_rejects = 0
            state = merge_patch(state, patch)
            m.attempts += 1
            tests_ran = True
            passed, last_detail = run_python_tests(state["code"], tests, timeout_s=timeout_s)
            if passed:
                return LaneResult(
                    "codegen", ok=True, escalate=False, output=state["code"],
                    usage=m.usage_dict(), gated=True,
                    detail=f"state lane: passed on attempt {m.attempts} "
                           f"({m.calls} calls, {m.rejected} rejected patches)",
                    _extra={"attempts": m.attempts, "calls": m.calls, "rejected": m.rejected,
                            "taxonomy": dict(m.taxonomy), "state": dict(state)})
            obs = (f"TESTS FAILED (attempt {m.attempts}/{max_attempts}). "
                   f"Test output (tail):\n{_tail(last_detail, obs_chars)}")
    except Exception as e:  # noqa: BLE001 — server/protocol error mid-loop: escalate, never crash
        return LaneResult("codegen", ok=False, escalate=True, output=state.get("code", ""),
                          usage=m.usage_dict(), gated=tests_ran,
                          detail=f"state_lane_call_failed: {e!r}",
                          _extra={"attempts": m.attempts, "calls": m.calls,
                                  "rejected": m.rejected, "taxonomy": dict(m.taxonomy),
                                  "state": dict(state)})

    # Budget exhausted without a pass -> escalate. The payload is the STRUCTURED STATE (what was
    # tried, what failed), not a transcript — the frontier gets the head start without the bloat.
    escalation = {"attempts": m.attempts, "calls": m.calls, "rejected_patches": m.rejected,
                  "taxonomy": dict(m.taxonomy), "fix_summary": state.get("fix_summary"),
                  "open_issues": state.get("open_issues"),
                  "last_failure": _tail(last_detail, 800)}
    return LaneResult(
        "codegen", ok=False, escalate=True, output=state.get("code", ""),
        usage=m.usage_dict(), gated=tests_ran,
        detail="state lane escalated: " + json.dumps(escalation, separators=(",", ":")),
        _extra={"attempts": m.attempts, "calls": m.calls, "rejected": m.rejected,
                "taxonomy": dict(m.taxonomy), "state": dict(state)})
