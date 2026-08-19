"""Prepend retrieved corrections as few-shot (user->assistant) pairs before the live user turn.

The few-shot pairs sit BETWEEN the system message and the live user turn, so the model sees prior
(original question -> frontier-corrected answer) demonstrations before answering the current one. No
exemplars -> [system, user] unchanged, so a cold correction store is a clean no-op.
"""
from __future__ import annotations


def _orig_text(ex: dict) -> str:
    msgs = ex.get("messages") or []
    return " ".join(m.get("content", "") for m in msgs if isinstance(m, dict))


def build_messages_with_exemplars(system: str, user: str, exemplars: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for ex in exemplars:
        out.append({"role": "user", "content": _orig_text(ex)})
        out.append({"role": "assistant", "content": ex.get("corrected_answer", "")})
    out.append({"role": "user", "content": user})
    return out
