"""§4 matcher wiring — content-derived session identity ON the request path.

The matcher (`session.matcher`) and the durable store existed since M0 but were never called
from a handler, so every telemetry row carried `matcher_event="unwired"` and header-less
traffic (Codex, and any client without `x-claude-code-session-id`) had `session_id=null` —
56% of rows on the live log, including the single most expensive session. This module is the
thin, fail-open glue the handlers call:

  request body → messages → matcher.identify() → apply the Match to the store → ids

Doctrine preserved from the matcher: content is the primary signal, the client session
header is a partition key + wire-hint tiebreaker, and ANY doubt fails open (returns None —
the handler keeps the header id or null, traffic is never affected).

Applying a Match:
  new         → create_session + set chain
  extend      → touch (turn++) + re-set chain to G (stored chain is a prefix of G)
  client_edit → invalidate_from(edit_pos) (drops diverged tail + derived state) + re-set chain
  compaction  → rebase chain to G + touch
"""
from __future__ import annotations

import json
import time

from apex_router.proxy_engine.session import matcher
from apex_router.proxy_engine.session.identity import hash_obj


def _messages_of(obj: dict) -> list | None:
    """The message list both wires carry under `messages` (anthropic + openai chat)."""
    msgs = obj.get("messages")
    return msgs if isinstance(msgs, list) and msgs else None


def _sys_prompt_hash(obj: dict) -> str | None:
    """Anthropic `system` (str or block list) → lineage hash; absent → None (matches the
    store's NULL handling: None candidates only match None requests)."""
    sysp = obj.get("system")
    if sysp is None:
        return None
    try:
        return hash_obj(sysp)
    except Exception:
        return None


def identify_into_store(
    *,
    body: bytes,
    client: str,
    wire_hint: str | None,
    agent_id: str | None,
    store,
    epoch_id: str,
    now: float | None = None,
) -> tuple[str, int, str] | None:
    """Identify the request's session and persist the result. Returns
    (session_id, turn, matcher_event), or None on ANY doubt (fail-open: identity is an
    attribution aid, never worth affecting traffic). Never raises."""
    try:
        obj = json.loads(body)
        if not isinstance(obj, dict):
            return None
        msgs = _messages_of(obj)
        if msgs is None:
            return None
        now = time.time() if now is None else now
        sys_hash = _sys_prompt_hash(obj)
        m = matcher.identify(
            msgs,
            client=client,
            sys_prompt_hash=sys_hash,
            wire_hint=wire_hint,
            store=store,
            now=now,
            agent_id=agent_id,
            client_session_id=wire_hint,  # split-only partition: two client sessions never merge
        )
        g = matcher.chain_of(msgs)
        if m.is_new:
            store.create_session(
                m.session_id, epoch_id, client,
                sys_prompt_hash=sys_hash, agent_id=agent_id,
                client_session_id=wire_hint, wire_hint=wire_hint, now=now,
            )
            store.replace_chain(m.session_id, g)
        elif m.event == "extend":
            store.touch_session(m.session_id, turn=m.turn, now=now)
            store.replace_chain(m.session_id, g)
        elif m.event == "client_edit":
            store.invalidate_from(m.session_id, m.edit_pos if m.edit_pos is not None else 0)
            store.replace_chain(m.session_id, g)
            store.touch_session(m.session_id, turn=m.turn, now=now)
        elif m.event == "compaction":
            store.replace_chain(m.session_id, m.rebase_chain if m.rebase_chain else g)
            store.touch_session(m.session_id, turn=m.turn, now=now)
        else:
            return None
        return (m.session_id, m.turn, m.event)
    except Exception:  # noqa: BLE001 — fail-open is the contract
        return None
