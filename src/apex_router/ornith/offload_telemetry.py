"""Per-lane offload telemetry — measure-first substrate for local-model (Ornith) offload.

WHY THIS EXISTS
    Local offload is only a WIN when the local answer is CORRECT — a wrong local result the
    frontier must redo costs more than never offloading. So the one thing we must measure is a
    per-call, per-lane pass/fail alongside the token counts. codeqa/impact.py already proved the
    seam (fail-open JSONL, no source text) but recorded only prompt/cached tokens for one lane.
    This module generalizes it to any lane AND captures `completion_tokens` — the 5x-billed output
    slice that gated-codegen offload is meant to move off the frontier.

DOCTRINE (inherited from impact.py)
    - Records counts / verdicts / lane / model ONLY. No source text, no prompt content.
    - The writer is FAIL-OPEN: an instrument must never break the tool it measures.

TOKEN SHAPE (verified against the live :8080 MLX server, the reference window):
    usage = {"prompt_tokens": P, "completion_tokens": C, "total_tokens": T,
             "prompt_tokens_details": {"cached_tokens": K}}
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Default sink lives beside the existing apex/codeqa telemetry so one place holds local-model spend.
DEFAULT_OFFLOAD_LOG = Path.home() / ".apex" / "offload_telemetry.jsonl"


def usage_tokens(usage: dict | None) -> tuple[int, int, int]:
    """Extract (prompt_tokens, completion_tokens, cached_tokens) from a server usage dict.

    Degrades to 0 for any missing/malformed field — never raises. `cached_tokens` is nested under
    `prompt_tokens_details` (OpenAI-compat shape); a non-dict there is treated as no cache info.
    """
    if not isinstance(usage, dict):
        return (0, 0, 0)

    def _int(v) -> int:
        # never raises (cross-validation): a non-numeric token value like "bad" coerces to 0, not
        # a ValueError that would break the instrument's "never raises" contract.
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    prompt = _int(usage.get("prompt_tokens"))
    completion = _int(usage.get("completion_tokens"))
    details = usage.get("prompt_tokens_details")
    cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    return (prompt, completion, cached)


@dataclass
class OffloadRecord:
    """One local-model offload outcome. Counts + verdict only — no content.

    gated     : did a CORRECTNESS GATE actually run on this call (codegen ran the tests; codeqa
                verified citations)? A raw served completion with no gate is `gated=False` and can
                NEVER count as frontier work saved — otherwise every completion inflates the metric
                (cross-validation). Only a gated call has an earned verdict.
    ok        : did the local answer PASS its gate (tests passed / grounded). Meaningless unless
                `gated` is True.
    escalated : was the item handed to the frontier anyway. A call that escalates saved NO frontier
                generation, even if ok (the review pre-filter is ok+escalated by design, Codex #4).
    """
    ts: float
    lane: str
    model: str
    ok: bool
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    latency_ms: int
    escalated: bool = False
    gated: bool = False
    ts_iso: str = ""
    _extra: dict = field(default_factory=dict)

    def to_json_obj(self) -> dict:
        d = asdict(self)
        d.pop("_extra", None)
        return d


def write_offload(log_path, rec: OffloadRecord | None = None) -> None:
    """Append one offload record as JSONL. Fail-open — an instrument must never break the tool.

    Two call forms, disambiguated by TYPE (not by None — `write_offload(path, None)` must not be
    mistaken for the single-arg form, cross-validation):
        write_offload(path, rec)   # explicit sink (tests)
        write_offload(rec)         # default sink DEFAULT_OFFLOAD_LOG (worker/lanes)
    """
    if isinstance(log_path, OffloadRecord):   # single-arg form: the sole positional IS the record
        log_path, rec = DEFAULT_OFFLOAD_LOG, log_path
    if not isinstance(rec, OffloadRecord):    # nothing serializable — drop silently (fail-open)
        return
    try:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec.to_json_obj(), separators=(",", ":")) + "\n")
    except Exception:  # noqa: BLE001 — fail-open covers OSError, TypeError, serialization, etc.
        pass


def aggregate_offload(log_path: Path) -> dict:
    """Aggregate an offload log into per-lane economics.

    The load-bearing number is `frontier_completion_tokens_saved`. Frontier generation is avoided
    ONLY when a call is (a) `gated` — a real correctness gate ran, so the verdict is earned; (b)
    `ok` — it passed that gate; and (c) NOT `escalated` — the item was not also sent upstream. All
    three are required (cross-validation): a raw served completion (gated=False) or a review that
    always escalates (ok but escalated) saved NO frontier tokens. Counting either would inflate the
    metric — the exact measurement-integrity trap this whole effort exists to avoid.

    Every field is read defensively: a malformed record (null, list, string token count) is skipped,
    never allowed to abort the report (cross-validation).
    """
    empty = {"overall": {"n": 0, "ok": 0, "escalated": 0, "gated": 0}, "by_lane": {}}
    try:
        # errors="replace" so a single invalid-UTF-8 byte can't abort the whole report
        # (cross-validation). Catch broadly — reading the log must never raise.
        text = Path(log_path).read_text(errors="replace")
    except Exception:  # noqa: BLE001
        return empty

    def _int(v) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    def _flag(v) -> bool:
        # STRICT: only a literal JSON `true` counts (cross-validation). A string "false" is truthy
        # under bool() and would flip a failed call into a saving one — the exact metric-inflation
        # this design exists to prevent.
        return v is True

    lanes: dict[str, dict] = {}
    n = ok = escalated = gated = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(d, dict):            # a bare JSON scalar/list is not a record
            continue
        n += 1
        lane = d.get("lane") or "unknown"
        if not isinstance(lane, str):
            lane = "unknown"
        L = lanes.setdefault(lane, {
            "n": 0, "ok": 0, "escalated": 0, "gated": 0,
            "frontier_completion_tokens_saved": 0,
            "escalated_completion_tokens": 0,   # waste: local tokens spent on calls that escalated
            "prompt_tokens": 0, "cached_tokens": 0,
        })
        L["n"] += 1
        L["prompt_tokens"] += _int(d.get("prompt_tokens"))
        L["cached_tokens"] += _int(d.get("cached_tokens"))
        is_gated = _flag(d.get("gated"))
        is_ok = _flag(d.get("ok"))
        is_esc = _flag(d.get("escalated"))
        if is_gated:
            gated += 1
            L["gated"] += 1
        if is_ok:
            ok += 1
            L["ok"] += 1
        if is_esc:
            escalated += 1
            L["escalated"] += 1
            # WASTE (spec F2): local completion tokens spent on a call that still escalated. A gross
            # "saved" number that ignores this overstates the win — surface it so net stays honest.
            L["escalated_completion_tokens"] += _int(d.get("completion_tokens"))
        # frontier work avoided requires ALL THREE: gated, passed, and not also escalated.
        if is_gated and is_ok and not is_esc:
            L["frontier_completion_tokens_saved"] += _int(d.get("completion_tokens"))

    for L in lanes.values():
        # ok_rate is only meaningful over GATED calls (an ungated completion has no earned verdict).
        L["ok_rate"] = (L["ok"] / L["gated"]) if L["gated"] else None

    return {
        "overall": {"n": n, "ok": ok, "escalated": escalated, "gated": gated},
        "by_lane": lanes,
    }
