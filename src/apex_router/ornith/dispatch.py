"""Lane dispatch — route a queued job to its lane and return a LaneResult the worker records.

The worker owns the filesystem state machine (inbox->running->done/failed) and telemetry; THIS owns
the "which lane, what verdict" decision. Lane runners are parameters so the routing is unit-testable
without a live server (tests inject stubs; production passes the real ornith_client/offload_lanes).

Verdict doctrine (unchanged from offload_lanes / the aggregator):
  - codegen  -> gated=True  (tests actually run); ok/escalate from whether they passed.
  - review   -> gated=False (recall pre-filter, 1/5 precision); always escalate for triage.
  - adhoc/unknown -> raw chat, gated=False, ok=False (a served completion is never an earned pass;
    it cannot count as frontier work saved).
"""
from __future__ import annotations

import os

from .offload_lanes import LaneResult


def _review_lane_enabled(env=None) -> bool:
    """Opt-in gate for the local review pre-filter. ONLY affirmative tokens enable it, so a
    typo/mis-set env fails SAFE (lane stays off — the measured-negative default)."""
    e = os.environ if env is None else env
    return (e.get("ORNITH_REVIEW_LANE") or "").strip().lower() in ("1", "true", "yes", "on")


def _default_chat(messages, *, max_tokens, enable_thinking):
    from . import ornith_client as oc
    # adhoc output is advisory — a truncated answer is still partially usable, so keep it rather than
    # discarding the whole job (measured: adhoc jobs were failing ~43% on finish_reason=length).
    return oc.chat_messages(messages, max_tokens=max_tokens, enable_thinking=enable_thinking,
                            raise_on_truncation=False)


def _default_codegen(spec, tests, *, max_tokens=1200, timeout_s=30):
    from .offload_lanes import codegen_lane
    return codegen_lane(spec, tests, max_tokens=max_tokens, timeout_s=timeout_s)


def _default_review(preamble, diff, *, max_tokens=512):
    from .offload_lanes import review_lane
    return review_lane(preamble, diff, max_tokens=max_tokens)


_DEFAULT_REVIEW_PREAMBLE = (
    "You are a code reviewer. Report concrete bugs with exact line refs, verbatim. "
    "Do not invent identifiers."
)


def run_job(job: dict, *, chat=_default_chat, codegen=_default_codegen,
            review=_default_review) -> LaneResult:
    """Dispatch one job dict to its lane and return a LaneResult. Never raises for routing reasons —
    a lane runner may raise (server error); the worker's try/except handles that as a FAILED job."""
    lane = job.get("lane") or "adhoc"
    max_tokens = job.get("max_tokens", 4096)

    if lane == "codegen":
        spec, tests = job.get("spec"), job.get("tests")
        if not spec or not tests:
            # cannot gate without tests -> do NOT run ungated code; escalate to the frontier.
            return LaneResult("codegen", ok=False, escalate=True, output="",
                              usage=None, detail="codegen job missing spec/tests", gated=False)
        return codegen(spec, tests, max_tokens=min(max_tokens, 2048))

    if lane == "review":
        # DEFAULT-OFF (measured): the review pre-filter always escalates for frontier triage,
        # so its local tokens are booked pure cost (-5,383 net on the live log) while its only
        # possible benefit — making the frontier triage cheaper — is never measured. Until that
        # delta is instrumented, the lane spends NO local tokens: it escalates immediately so the
        # frontier still does the review. ORNITH_REVIEW_LANE=on re-enables the local pre-filter.
        if not _review_lane_enabled():
            return LaneResult("review", ok=False, escalate=True, output="", usage=None,
                              gated=False,
                              detail="review lane disabled by default (measured net-negative; "
                                     "ORNITH_REVIEW_LANE=on to re-enable the local pre-filter)")
        diff = job.get("diff") or job.get("context") or ""
        preamble = job.get("preamble") or _DEFAULT_REVIEW_PREAMBLE
        return review(preamble, diff, max_tokens=min(max_tokens, 1024))

    if lane in ("citation", "search", "extraction"):
        # Retrieval-style sub-task: the local model answers with file:line citations; the GROUNDING
        # verifier (not an in-lane gate) decides acceptance. ok=True means "produced an answer to
        # verify", NOT "verified" — gated=False, so the composed adjudicator gates it via the type
        # verifier. Thinking-OFF (extraction/citation is the fidelity lane).
        messages = job.get("messages") or [{"role": "user", "content": job.get("task", "")}]
        r = chat(messages, max_tokens=max_tokens, enable_thinking=False)
        # ok=False, gated=False: the lane ran NO correctness gate, so it has no EARNED verdict (Codex
        # xval P1b — OffloadRecord.ok means "passed its gate"; an ungated pass would corrupt telemetry
        # and let the async worker record an unverified citation as ok). The GROUNDING verifier is the
        # gate, applied by the orchestrator's composed_adjudicate. On the fire-and-forget WORKER path
        # (no adjudicator), a citation stays ungated (never an earned ok) — offload of citations is an
        # orchestrator-path capability.
        return LaneResult(lane, ok=False, escalate=False, output=getattr(r, "answer", ""),
                          usage=getattr(r, "usage", None), gated=False,
                          detail="citation lane; gate is the grounding verifier (orchestrator path)")

    # adhoc / unknown lane -> raw chat, thinking-OFF unless the job explicitly opts in.
    messages = job.get("messages") or [{"role": "user", "content": job.get("task", "")}]
    r = chat(messages, max_tokens=max_tokens,
             enable_thinking=bool(job.get("enable_thinking", False)))
    return LaneResult("adhoc", ok=False, escalate=False, output=getattr(r, "answer", ""),
                      usage=getattr(r, "usage", None), gated=False,
                      detail=f"raw chat finish={getattr(r, 'finish_reason', None)}")
