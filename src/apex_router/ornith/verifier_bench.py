"""Measure each verifier's FALSE-ACCEPT rate on an independently-labeled adversarial set (spec F7).

'never accepts wrong' is not a claim we make — we MEASURE how often each verifier passes a case a
human/frontier labeled wrong, and report it. A non-near-zero rate means that verifier's passes must
route to cross-validation, not straight acceptance. Cases carry their own truth label, so the harness
is deterministic and offline; a per-case `ground_fn` seam lets citation cases inject a controlled
grounding verdict without a live repo.
"""
from __future__ import annotations

from .verifiers import verify


def measure_false_accept(cases: list[dict]) -> dict:
    """cases: [{"type": str, "lane_result": obj, "is_wrong": bool, "ground_fn"?: callable}].
    Returns {task_type: {"n", "wrong", "false_accepts", "false_accept_rate"}}, where a false accept =
    the verifier ACCEPTED (applicable AND passed) a case labeled wrong. Rate is over WRONG cases
    only (None when there are none — an undefined rate is not 0)."""
    out: dict[str, dict] = {}
    for c in cases:
        tt = c["type"]
        # is_wrong MUST be an explicit bool label (Codex xval F4/F5): string-truthiness ("false" is
        # truthy) or a missing label would silently mis-bucket a case and skew the rate. A case without
        # a real bool label is a harness error, not a silent "correct".
        if not isinstance(c.get("is_wrong"), bool):
            raise ValueError(f"case for {tt!r} must set is_wrong to a bool, got {c.get('is_wrong')!r}")
        cell = out.setdefault(tt, {"n": 0, "wrong": 0, "false_accepts": 0, "false_accept_rate": None})
        cell["n"] += 1
        v = verify(tt, c["lane_result"], ground_fn=c.get("ground_fn"))
        accepted = v.applicable and v.passed
        if c["is_wrong"]:
            cell["wrong"] += 1
            if accepted:
                cell["false_accepts"] += 1
    for cell in out.values():
        cell["false_accept_rate"] = (cell["false_accepts"] / cell["wrong"]) if cell["wrong"] else None
    return out


def format_bench(result: dict) -> str:
    lines = ["VERIFIER FALSE-ACCEPT RATES (measured on labeled-wrong cases)"]
    for tt, c in sorted(result.items()):
        rate = c["false_accept_rate"]
        rate_s = "n/a" if rate is None else f"{rate:.2f}"
        note = ("" if not rate else
                "  -> non-zero: this verifier's passes route to CROSS-VALIDATION, not straight "
                "acceptance")
        lines.append(f"  {tt:12s} false_accept={c['false_accepts']}/{c['wrong']} (rate={rate_s}){note}")
    return "\n".join(lines)
