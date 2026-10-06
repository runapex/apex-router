"""Opt-in stable Codex prompt_cache_key (proxy.cache_key, APEX_CODEX_CACHE_KEY).

Pins: off by default (byte-identical forward); when on, ONLY the prompt_cache_key member changes
(client_metadata keeps the original UUID); runs sharing the run-invariant head share a key; head
differences (repo/date via environment_context, model, tools) never collide; a fork that copies the
first task prompt stays on its parent's shard; anything unexpected fails open (untouched).
"""
from __future__ import annotations

import dataclasses
import json

from apex_router.proxy_engine.config import Config
from apex_router.proxy_engine.proxy import cache_key
from apex_router.proxy_engine.proxy.upstream import Upstream

DEV = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "<permissions>"}]}
ENV = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context><cwd>/r/a</cwd></environment_context>"}]}


def _body(uuid: str, task: str = "review X", env: dict = ENV, model: str = "m", extra: list | None = None) -> bytes:
    task_item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": task}]}
    obj = {
        "model": model, "instructions": "be terse", "tools": [{"type": "function", "name": "exec"}],
        "input": [DEV, env, task_item, *(extra or [])],
        "prompt_cache_key": uuid,
        "client_metadata": {"session_id": uuid, "thread_id": uuid},
    }
    return json.dumps(obj).encode()


def _key(body: bytes) -> str:
    return json.loads(body)["prompt_cache_key"]


def test_only_the_key_member_changes():
    b = _body("01a1-uuid-A")
    out, info = cache_key.rewrite(b, 4)
    assert info is not None and _key(out).startswith("apex:")
    o, n = json.loads(b), json.loads(out)
    assert n["client_metadata"] == o["client_metadata"]  # same UUID elsewhere is untouched
    n["prompt_cache_key"] = o["prompt_cache_key"]
    assert json.dumps(n) == json.dumps(o)  # nothing else differs
    assert out.replace(_key(out).encode(), b"01a1-uuid-A") == b  # a pure in-place splice


def test_runs_with_the_same_head_share_a_key_and_the_key_is_stable_mid_run():
    a = _key(cache_key.rewrite(_body("uuid-run-1"), 4)[0])
    b = _key(cache_key.rewrite(_body("uuid-run-2"), 4)[0])
    assert a == b  # different thread UUIDs, same task + head
    later = _body("uuid-run-1", extra=[{"type": "function_call_output", "output": "x" * 500}])
    assert _key(cache_key.rewrite(later, 4)[0]) == a  # growing history keeps the run's key


def test_per_run_item_ids_do_not_split_the_key():
    def with_ids(uuid: str, suffix: str) -> bytes:
        obj = json.loads(_body(uuid))
        for n, it in enumerate(obj["input"]):
            it["id"] = f"msg_{suffix}_{n}"  # Codex mints fresh msg ids every run
        return json.dumps(obj).encode()
    assert _key(cache_key.rewrite(with_ids("u1", "a"), 4)[0]) == _key(cache_key.rewrite(with_ids("u2", "b"), 4)[0])


def test_head_differences_never_collide():
    base = _key(cache_key.rewrite(_body("u"), 1)[0])
    other_repo = dict(ENV, content=[{"type": "input_text", "text": "<environment_context><cwd>/r/b</cwd></environment_context>"}])
    assert _key(cache_key.rewrite(_body("u", env=other_repo), 1)[0]) != base
    assert _key(cache_key.rewrite(_body("u", model="m2"), 1)[0]) != base


def test_tasks_spread_over_shards_and_a_fork_keeps_its_parents_shard():
    keys = {_key(cache_key.rewrite(_body("u", task=f"task {i}"), 4)[0]) for i in range(40)}
    assert 2 <= len(keys) <= 4
    assert len({k.rsplit(":", 1)[0] for k in keys}) == 1  # one head, several shards
    parent = _key(cache_key.rewrite(_body("parent", task="audit Y"), 4)[0])
    fork = _key(cache_key.rewrite(_body("child", task="audit Y", extra=[{"type": "message", "role": "user", "content": []}]), 4)[0])
    assert fork == parent


def test_fail_open_cases_leave_the_body_untouched():
    for b in (b"not json", b"[1,2]", json.dumps({"input": [DEV, ENV]}).encode(),  # no key
              json.dumps({"prompt_cache_key": "k", "input": [DEV]}).encode(),     # no head
              _body("apex:already:0")):                                              # idempotent
        assert cache_key.rewrite(b, 4) == (b, None)


def test_upstream_prepare_body_is_identity_unless_enabled_on_the_responses_wire():
    b = _body("u-1")
    off = Upstream(Config())
    assert off.prepare_body("codex", "/gpt5/openai/responses", b) == (b, None)
    on = Upstream(dataclasses.replace(Config(), codex_cache_key=True))
    assert on.prepare_body("claude-code", "/v1/messages", b) == (b, None)
    assert on.prepare_body("codex", "/gpt5/openai/chat/completions", b) == (b, None)
    out, info = on.prepare_body("codex", "/gpt5/openai/responses", b)
    assert info and _key(out).startswith("apex:")
