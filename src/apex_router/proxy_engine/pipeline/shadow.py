"""Shadow-mode pipeline — compute-and-log, never emit (M6b Stage A, wire-switch rung A).

Runs the full enforcement decision (`decide()`) over a LIVE request without changing a single byte
forwarded to the provider (passthrough emission — the whole point of shadow: zero live risk while
the evidence base accrues). Its outputs are telemetry only, and they are exactly what the first
downstream gates consume:

  - `bytes_by_class` — the whole request's content bytes decomposed by content class. This is R1's
    regressor **X**: the wire-usage reconciliation regression fits eff-tokens/byte/class OFFLINE by
    regressing this against the provider's observed `usage.input_tokens` (billed on the whole
    prefix, so X must be whole-request, not frontier). Available from the FIRST shadow request.
  - a per-FRONTIER-block `decide()` diff — cell key, chosen transform, predicted BYTES saved, floor
    outcome — the "predicted delta" an operator watches before any cell emits for real. Frontier
    only, because under prefix-freeze only the newest turn is addressable; running decide() over
    frozen history would resurrect the applicability illusion the composition diagnostic kills.

PLANE-CLEAN by construction (`test_plane_separation`): imports `apex_router.proxy_engine.policy` + `apex_router.proxy_engine.pipeline`
`.decide` only — byte-only, tokenizer-free. Tokens are NEVER computed here; that is R1's offline
job, fed by the (bytes_by_class, usage) pairs this module + the usage scanner log. The unit here
is BYTES (the F-i lesson: label the unit; the hot path speaks bytes, the compiler speaks tokens).

Frontier basis is `last_message` — the newest turn's blocks, an honest approximation of the freeze
frontier that needs no session state. The exact frontier (matcher + ledger committed-prefix
bookkeeping) is the eventual Stage-A state; until it wires in, the report carries `frontier_basis`
so a reader never mistakes the approximation for the ledger-exact frontier.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from apex_router.proxy_engine.pipeline.decide import decide
from apex_router.proxy_engine.policy import PolicyVersion, classify, size_stratum_bytes


def _block_text_and_tool(block: object) -> tuple[str, str | None]:
    """Flatten one wire content block to (text, tool_name) — the bytes a transform would see.

    Mirrors the offline corpus builder's `_block_text` so runtime classification matches the
    population the compiler priced. A tool_result carries the addressable output (json/terminal/
    file_read); tool_use carries the call args. Unknown shapes serialize verbatim (fail-open — a
    block we can't read is still counted, never dropped)."""
    if isinstance(block, str):
        return block, None
    if not isinstance(block, dict):
        return json.dumps(block), None
    t = block.get("type")
    if t == "text":
        return block.get("text", ""), None
    if t == "thinking":
        return block.get("thinking", ""), None
    if t == "tool_use":
        return json.dumps(block.get("input", {})), block.get("name")
    if t == "tool_result":
        c = block.get("content")
        if isinstance(c, str):
            return c, "tool_result"
        if isinstance(c, list):
            return "\n".join(_block_text_and_tool(b)[0] for b in c), "tool_result"
        return json.dumps(c), "tool_result"
    return json.dumps(block), None


def _message_blocks(message: object) -> list[tuple[str, str | None]]:
    """A user/assistant message → its (text, tool_name) blocks. `content` is a str or block list."""
    if not isinstance(message, dict):
        return []
    c = message.get("content")
    if isinstance(c, str):
        return [(c, None)]
    if isinstance(c, list):
        return [_block_text_and_tool(b) for b in c]
    return []


def _responses_item_blocks(item: object) -> list[tuple[str, str | None]]:
    """One OpenAI Responses `input` item → its (text, tool_name) blocks (cross-validation).

    The Responses API `input` list carries typed items, NOT bare content blocks:
      - a bare string (the simple item form);
      - `{type: "message", role, content: [{type: "input_text"|"output_text"|"text", text}, …]}`;
      - `{type: "function_call", …}` / `{type: "function_call_output", output}` — tool traffic.
    The content parts use `input_text`/`output_text` (not Anthropic's `text`/`tool_result`), so they
    need their own extractor — flattening them through `_block_text_and_tool` would serialize the
    wrapper as JSON and misclassify the prose. Unknown shapes serialize verbatim (fail-open —
    counted, never dropped)."""
    if isinstance(item, str):
        return [(item, None)]
    if not isinstance(item, dict):
        return [(json.dumps(item), None)]
    t = item.get("type")
    if t in ("function_call", "function_call_output"):
        # tool call/result — the addressable output is the args or the output payload
        payload = item.get("output") if t == "function_call_output" else item.get("arguments")
        if isinstance(payload, str):
            return [(payload, "tool_result")]
        return [(json.dumps(payload if payload is not None else item), "tool_result")]
    content = item.get("content")
    if isinstance(content, str):
        return [(content, None)]
    if isinstance(content, list):
        out: list[tuple[str, str | None]] = []
        for part in content:
            if isinstance(part, str):
                out.append((part, None))
            elif isinstance(part, dict):
                # input_text / output_text / text → the text field; else serialize the part
                txt = part.get("text")
                out.append((txt, None) if isinstance(txt, str) else (json.dumps(part), None))
        return out
    # a message item with no content list, or an unknown item → serialize verbatim (counted)
    return [(json.dumps(item), None)]


@dataclass(frozen=True)
class WireBlock:
    text: str
    tool_name: str | None
    content_class: str


def decompose(body: bytes) -> tuple[list[WireBlock], list[WireBlock]]:
    """Parse a request body into (all_blocks, frontier_blocks).

    `all_blocks` is every billed content block (R1's X population — the whole prefix
    `usage.input_tokens` bills, so it MUST include the Anthropic top-level `system` field, which is
    billed input the runtime never re-renders); `frontier_blocks` is the newest ADDRESSABLE turn
    under prefix-freeze (the last message, or the OpenAI `input`). `system` is deliberately in
    `all_blocks` but NOT `frontier` — it is cached-prefix, not the turn being decided, so decide()
    never touches it (routing it would resurrect the applicability illusion). Non-JSON or a body
    with no messages/input yields ([], []) — fail-open, the caller logs nothing and forwards raw.
    Deterministic and side-effect free.

    Two wires:
      - Anthropic (`/v1/messages`): top-level `system` (str | block list) + `messages` (the frontier
        is `messages[-1]`).
      - OpenAI/Codex (`/v1/chat/completions`, `/v1/responses`): `messages` OR `input` (Responses API
        uses `input` for the turn; string or block list). Whichever is present is the frontier.
    """
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return [], []
    if not isinstance(obj, dict):
        return [], []

    def to_wire(pairs: list[tuple[str, str | None]]) -> list[WireBlock]:
        out = []
        for text, tool in pairs:
            if not text:
                continue
            out.append(WireBlock(text, tool, classify(text, "")))
        return out

    all_blocks: list[WireBlock] = []
    # Top-level `tools` schemas: billed input on BOTH wires (Anthropic render order is
    # tools→system→messages; OpenAI bills the function schemas too), part of X, never the frontier —
    # they are the stable cached prefix, not the addressable turn (cross-validation). Serialize each schema
    # to the bytes it occupies on the wire and classify it (json — tool schemas are JSON objects).
    tools = obj.get("tools")
    if isinstance(tools, list):
        all_blocks.extend(to_wire([(json.dumps(t), "tools") for t in tools if t]))

    # Anthropic top-level `system`: billed input, part of X, never the frontier.
    system = obj.get("system")
    if isinstance(system, str) and system:
        all_blocks.append(WireBlock(system, "system", classify(system, "")))
    elif isinstance(system, list):
        all_blocks.extend(to_wire(_message_blocks({"content": system})))

    messages = obj.get("messages")
    frontier: list[WireBlock] = []
    if isinstance(messages, list) and messages:
        for m in messages:
            all_blocks.extend(to_wire(_message_blocks(m)))
        frontier = to_wire(_message_blocks(messages[-1]))
    else:
        # OpenAI Responses API: the turn is under `input` — a string shortcut, OR a structured list
        # of items (`{type:"message", content:[{type:"input_text", text:…}]}`). The structured form
        # must be flattened item-by-item (cross-validation): passing raw items to _block_text_and_tool
        # serializes the wrappers as JSON and misclassifies prose as json.
        inp = obj.get("input")
        if isinstance(inp, str) and inp:
            frontier = to_wire([(inp, None)])
        elif isinstance(inp, list):
            pairs: list[tuple[str, str | None]] = []
            for item in inp:
                pairs.extend(_responses_item_blocks(item))
            frontier = to_wire(pairs)
        all_blocks.extend(frontier)

    if not all_blocks:
        return [], []
    return all_blocks, frontier


@dataclass(frozen=True)
class ShadowBlock:
    """One frontier block's shadow decision — byte-only (the hot-path unit)."""

    cell: str  # "class/stratum" — the compiled cell key this block routes to
    content_class: str
    stratum: str
    transform: str | None  # the rule's transform name (None = raw cell)
    transformed: bool  # did decide() choose to emit a rendering (vs raw)?
    reason: str  # decide()'s reason tag (emit / below_floor / not_addressable / …)
    orig_bytes: int
    emitted_bytes: int  # bytes decide() WOULD emit (never actually sent in shadow)

    @property
    def bytes_saved(self) -> int:
        return self.orig_bytes - self.emitted_bytes


# Observation budget — a DECISION, not an apologetic cap. `run_shadow`'s decompose/classify cost is
# ~linear in body size; on this machine the measured curve is `ms ≈ 17.7·MB + 0.36` (single-block
# frontier, 2026-07-19 benchmark). A multi-MB single block blows the G3 25ms gate. Above this
# threshold we SKIP decompose and LABEL it (`oversize_skipped` + `frontier_bytes`) so telemetry
# COUNTS what it didn't inspect rather than silently thinning `bytes_by_class` (R1's X).
#
# THRESHOLD IS A BOUND, NOT A POLICY (register): derived from the curve at a ~10ms compute budget
# (well under the 25ms gate, leaving the reference proxy for the rest of request handling):
#   solve 10 = 17.718·(bytes/1e6) + 0.36  →  bytes ≈ 544_118. Pinned to that measured value (NOT a
# round number — the 3.0-vs-3.2 lesson: a bound is computed from the measurement, not chosen tidy).
OVERSIZE_FRONTIER_BYTES = 544_118


@dataclass
class ShadowReport:
    """The per-request shadow diff logged to telemetry. All byte-denominated."""

    frontier_basis: str = "last_message"
    context_bytes: int = 0
    policy_epoch: int | None = None
    has_policy: bool = False
    n_blocks: int = 0  # whole-request block count (X population size)
    n_frontier: int = 0
    predicted_bytes_saved: int = 0  # Σ over frontier blocks decide() would compress
    bytes_by_class: dict[str, int] = field(default_factory=dict)  # R1's X (whole request)
    blocks: list[ShadowBlock] = field(default_factory=list)  # per-frontier decisions
    # Observation-budget labels: True when the body exceeded OVERSIZE_FRONTIER_BYTES and decompose
    # SKIPPED; `frontier_bytes` records the un-inspected size so the composition gap is COUNTED, not
    # silent. R1 must treat `oversize_skipped` rows as an exclusion (its X is missing exactly the
    # biggest blocks — the ones it most wants — so ignoring them biases the coefficients).
    oversize_skipped: bool = False
    frontier_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            "frontier_basis": self.frontier_basis,
            "context_bytes": self.context_bytes,
            "policy_epoch": self.policy_epoch,
            "has_policy": self.has_policy,
            "n_blocks": self.n_blocks,
            "n_frontier": self.n_frontier,
            "predicted_bytes_saved": self.predicted_bytes_saved,
            "bytes_by_class": dict(self.bytes_by_class),
            "oversize_skipped": self.oversize_skipped,
            "frontier_bytes": self.frontier_bytes,
            "blocks": [
                {
                    "cell": b.cell,
                    "class": b.content_class,
                    "stratum": b.stratum,
                    "transform": b.transform,
                    "transformed": b.transformed,
                    "reason": b.reason,
                    "orig_bytes": b.orig_bytes,
                    "emitted_bytes": b.emitted_bytes,
                    "bytes_saved": b.bytes_saved,
                }
                for b in self.blocks
            ],
        }


def _utf8(s: str) -> int:
    return len(s.encode("utf-8", "surrogatepass"))


def run_shadow(body: bytes, policy: PolicyVersion | None) -> ShadowReport:
    """Compute the shadow report for one request body. Pure, deterministic, byte-only.

    `context_bytes` is the sum of all block-text bytes — the same quantity the compiler binned cells
    on (`size_stratum_bytes(len(req.content))`, where req.content is the concatenated message text),
    so a frontier block routes to the cell whose evidence covers it. When `policy` is None (no
    signed bundle yet), the frontier decisions are all raw (`no_policy`), but `bytes_by_class` and
    `context_bytes` are STILL computed — R1 calibrates on raw wire bytes from request one, before
    any cell admits. Fail-open: a body we can't parse yields an empty report, never an error.

    Observation budget: above OVERSIZE_FRONTIER_BYTES, SKIP decompose (whose cost is ~linear in body
    size and would blow the G3 gate on a multi-MB block) and LABEL the skip — `bytes_by_class`
    stays empty (the composition gap is COUNTED via `frontier_bytes`, not silently thinned). A
    DECISION with a derived byte bound, not an apologetic cap.
    """
    if len(body) > OVERSIZE_FRONTIER_BYTES:
        return ShadowReport(
            has_policy=policy is not None,
            oversize_skipped=True,
            frontier_bytes=len(body),
        )
    all_blocks, frontier = decompose(body)
    report = ShadowReport(has_policy=policy is not None)
    if not all_blocks:
        return report

    bytes_by_class: dict[str, int] = {}
    context_bytes = 0
    for wb in all_blocks:
        n = _utf8(wb.text)
        context_bytes += n
        bytes_by_class[wb.content_class] = bytes_by_class.get(wb.content_class, 0) + n
    report.context_bytes = context_bytes
    report.bytes_by_class = bytes_by_class
    report.n_blocks = len(all_blocks)
    report.n_frontier = len(frontier)
    if policy is not None:
        report.policy_epoch = policy.policy_epoch

    stratum = size_stratum_bytes(context_bytes)
    for wb in frontier:
        orig = _utf8(wb.text)
        if policy is None:
            report.blocks.append(
                ShadowBlock(
                    cell=f"{wb.content_class}/{stratum}",
                    content_class=wb.content_class,
                    stratum=stratum,
                    transform=None,
                    transformed=False,
                    # Raw telemetry reason (correct for the jsonl). If a HUMAN surface ever renders
                    # per-block reasons (e.g. the doctor's divergence section), TRANSLATE this to
                    # "measure-only posture (no transforms admitted)" — the raw string is doctrine
                    # shorthand a reader without the decision log won't parse. `/status` already
                    # does this translation for the endpoint case (app.py status()).
                    reason="no_policy",
                    orig_bytes=orig,
                    emitted_bytes=orig,
                )
            )
            continue
        rule = policy.rule_for(wb.content_class, stratum)
        em = decide(
            wb.text,
            policy,
            context_bytes=context_bytes,
            tool_name=wb.tool_name,
            frozen=False,
        )
        emitted = _utf8(em.text)
        blk = ShadowBlock(
            cell=f"{wb.content_class}/{stratum}",
            content_class=wb.content_class,
            stratum=stratum,
            transform=rule.transform,
            transformed=em.transformed,
            reason=em.reason,
            orig_bytes=orig,
            emitted_bytes=emitted,
        )
        report.blocks.append(blk)
        if em.transformed and emitted < orig:
            report.predicted_bytes_saved += orig - emitted
    return report
