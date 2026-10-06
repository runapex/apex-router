"""Opt-in stable `prompt_cache_key` for the Codex/Responses wire (APEX_CODEX_CACHE_KEY=1).

Why: Codex sends `prompt_cache_key = <thread id>`, a fresh UUID per `codex exec` run and per
subagent. The Responses API routes a cached prefix by (key, leading tokens), so two runs with a
byte-identical ~16k-token prefix (instructions + tools + permissions + environment context) never
share it: measured on 1,449 runs, 98% of first calls had 0 cached tokens. A live probe confirmed
the mechanism: same key -> 15,503/15,512 cached, different key -> 0.

What: replace the key with `apex:<prefix-hash>:<shard>` where
  - prefix-hash = sha256(model, instructions, tools, input[0], input[1]) — the run-invariant head
    (permissions + environment_context, which carries cwd and date), so different repos/days/models
    never collide and a run keeps ONE key for every call (its head never changes mid-run);
  - shard = sha256(input[2]) mod N — the first task prompt. Calls of the same task (including a
    forked subagent, which copies the parent's history) stay together; unrelated runs spread over N
    keys so no single key exceeds the per-key routing rate (~15 req/min) and spills to cold machines.

This is the ONLY place the measuring proxy edits request bytes, and it is OFF by default (the
research note's rule: no byte mutation in the measuring proxy without an explicit decision). The
edit is a single in-place substitution of the existing key's JSON string value; every other byte is
forwarded unchanged. Fail-open: anything unexpected (not JSON, no key, key not found exactly once in
the raw bytes) returns the original body untouched.
"""
from __future__ import annotations

import hashlib
import json
import re

KEY_PREFIX = "apex:"


def _h(*parts: object) -> hashlib._Hash:
    h = hashlib.sha256()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
        h.update(b"\x00")
    return h


def _content(item: object) -> object:
    """An input item minus its per-run `id` (Codex mints a fresh msg_<uuid> per run; ids are not
    prompt tokens, so they must not split the key of byte-identical prompts)."""
    if isinstance(item, dict):
        return {k: v for k, v in item.items() if k != "id"}
    return item


def stable_key(obj: dict, shards: int) -> str | None:
    """The stable key for a parsed Responses body, or None if it lacks the run-invariant head."""
    inp = obj.get("input")
    if not isinstance(inp, list) or len(inp) < 2:
        return None
    head = _h(obj.get("model"), obj.get("instructions"), obj.get("tools"),
              _content(inp[0]), _content(inp[1])).hexdigest()[:20]
    task = _content(inp[2]) if len(inp) > 2 else None
    shard = int(_h(task).hexdigest(), 16) % max(1, shards)
    return f"{KEY_PREFIX}{head}:{shard}"


def rewrite(body: bytes, shards: int) -> tuple[bytes, dict | None]:
    """Return (body', info). info is None when the body was left untouched."""
    try:
        obj = json.loads(body)
    except Exception:  # noqa: BLE001 — fail-open: not JSON, forward as-is
        return body, None
    if not isinstance(obj, dict):
        return body, None
    old = obj.get("prompt_cache_key")
    if not isinstance(old, str) or not old or old.startswith(KEY_PREFIX):
        return body, None
    new = stable_key(obj, shards)
    if new is None:
        return body, None
    # Byte-level splice anchored on the `"prompt_cache_key": "<old>"` member (the same UUID also
    # appears in client_metadata, which must stay untouched). It must match exactly once; anything
    # else → leave the body untouched. A UUID needs no escaping, so json.dumps(old) is its encoding.
    pat = re.compile(rb'("prompt_cache_key"\s*:\s*)' + re.escape(json.dumps(old).encode()))
    if len(pat.findall(body)) != 1:
        return body, None
    out = pat.sub(lambda m: m.group(1) + json.dumps(new).encode(), body, count=1)
    return out, {"from": "client", "key": new}
