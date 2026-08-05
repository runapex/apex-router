"""file_read gutter-strip — wired into both planes (compiler pricing + runtime dispatch).

The transform exists (test_file_read_strip); this pins that it's REGISTERED so admission can price the
file_read cell and decide() can dispatch it once a cell is signed enabled. As a ccr_retrieval (lossy)
transform it stays INERT at runtime until a resolver is registered (Δ1 capability gate) and unsigned
until behavioral evidence lands (Δ14) — so registering it changes no live behavior, only makes the
cell PRICEABLE.
"""
from __future__ import annotations

from apex_router.proxy_engine.pipeline import decide as decide_mod
from apex_router.proxy_engine.pipeline.transforms import file_read_strip


def _guttered(n):
    return "\n".join(f"{i}\tdef f_{i}(): pass" for i in range(1, n + 1))


def test_runtime_registry_dispatches_file_read_strip():
    """decide()'s transform-name registry maps 'file_read_strip' to the module, so a signed rule
    naming it can be dispatched (was absent → would fail-open as unknown_transform)."""
    assert decide_mod._BY_NAME.get("file_read_strip") is file_read_strip


def test_compiler_prices_the_file_read_cell():
    """The compiler's transform registry gives file_read a real transform (was (None, None) → the
    cell could never compress). block_econs now produces compressing blocks for guttered file reads."""
    from apex_router.proxy_engine.tuner.compiler import _TRANSFORMS, block_econs
    from apex_router.proxy_engine.tuner.replay import Request

    xf, _tool = _TRANSFORMS["file_read"]
    assert xf is file_read_strip  # the cell has a transform to price

    # a corpus of guttered file reads → block_econs sees compressing blocks (reduction > floor)
    corpus = [
        Request("s0", (_guttered(40)).encode("utf-8"), 500, ts=float(t), model="opus")
        for t in range(4)
    ]
    econs = block_econs(corpus, "file_read", min_bytes=1, ratio_floor=0.02)
    assert econs, "expected file_read blocks in the corpus"
    assert any(e.compresses for e in econs), "gutter-strip should compress guttered file reads"


def test_lossy_file_read_is_inert_without_resolver():
    """decide() ships a file_read_strip rule RAW with capability_missing until a resolver registers —
    the Δ1 gate: a lossy cell that can't serve retrievals must not drop bytes."""
    from apex_router.proxy_engine.pipeline.decide import decide
    from apex_router.proxy_engine.policy import ClassRule, PolicyVersion

    # build a minimal policy whose file_read/<stratum> rule enables file_read_strip
    content = _guttered(40)
    # a hand-made rule naming the lossy transform, marked ccr_retrieval
    rule = ClassRule(transform="file_read_strip", enabled=True, min_bytes=1, ratio_floor=0.0,
                     retrieval_ceiling=0.0, knobs={}, transform_version="", validator_id=None,
                     validator_version="", fidelity_class="ccr_retrieval")

    class _Pol:
        policy_epoch = 0
        def rule_for(self, cls, stratum):
            return rule

    decide_mod._RESOLVERS.pop("file_read_strip", None)  # ensure no resolver
    em = decide(content, _Pol(), context_bytes=len(content), tool_name="tool_result", frozen=False)
    assert em.reason == "capability_missing"
    assert em.text == content  # shipped raw — no bytes dropped
