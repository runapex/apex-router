"""Corpus builder frontier-PHASE contract (shadow day-1 finding).

The builder recorded a request boundary on each assistant turn AFTER folding that assistant
message into the accumulating prefix — so the newest message of every produced Request was the
ASSISTANT's `tool_use` JSON args. But on the live wire the assistant turn is the COMPLETION being
generated; the request that produces it ends on the newest USER turn (`messages[-1]` is a
user/tool_result). The mismatch made the offline composition see JSON at the frontier (assistant
tool_use) where live shadow correctly sees prose/file_read (user tool_result) and JSON at 0% —
which is why the one admitted cell (`json/xl`) fired on 0 live blocks: it was priced on a phantom
population the runtime never meets. The builder's own docstring already states the correct rule
("up to and including the preceding user turn"); this pins the code to it.
"""
from __future__ import annotations

import json

from fixtures.build_replay_corpus import transcript_to_requests


def _write_transcript(path, records: list[dict]) -> None:
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_request_content_excludes_the_assistant_completion(tmp_path):
    """A request is the bytes the client sent to PRODUCE an assistant turn — so it must carry the
    triggering user turn and must NOT contain the assistant completion that answers it."""
    path = tmp_path / "s.jsonl"
    _write_transcript(path, [
        {"type": "user", "timestamp": "2026-07-13T10:00:00Z",
         "message": {"content": [{"type": "tool_result", "content": "USER_FRONTIER_PROSE"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T10:00:01Z",
         "message": {"model": "opus", "usage": {"input_tokens": 100},
                     "content": [{"type": "tool_use", "input": {"marker": "ASSISTANT_COMPLETION"}}]}},
    ])
    reqs = transcript_to_requests(str(path))
    assert len(reqs) == 1
    content = reqs[0].content.decode("utf-8")
    assert "USER_FRONTIER_PROSE" in content          # the triggering user turn IS in the request
    assert "ASSISTANT_COMPLETION" not in content     # the completion it produced is NOT


def test_newest_message_of_each_request_is_a_user_turn(tmp_path):
    """Across a multi-turn session the frontier (newest whole message, via message_boundaries) of
    every request is the user turn — the assistant completion of the prior request has folded into
    history, never sits at the frontier of the request that follows it."""
    path = tmp_path / "s.jsonl"
    _write_transcript(path, [
        {"type": "user", "timestamp": "2026-07-13T10:00:00Z",
         "message": {"content": [{"type": "tool_result", "content": "USER_ONE"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T10:00:01Z",
         "message": {"model": "opus", "usage": {"input_tokens": 100},
                     "content": [{"type": "tool_use", "input": {"marker": "ASSIST_ONE_JSON"}}]}},
        {"type": "user", "timestamp": "2026-07-13T10:00:02Z",
         "message": {"content": [{"type": "tool_result", "content": "USER_TWO"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T10:00:03Z",
         "message": {"model": "opus", "usage": {"input_tokens": 200},
                     "content": [{"type": "tool_use", "input": {"marker": "ASSIST_TWO_JSON"}}]}},
    ])
    reqs = transcript_to_requests(str(path))
    assert len(reqs) == 2
    for req, expected_user in zip(reqs, ["USER_ONE", "USER_TWO"]):
        b = req.message_boundaries
        assert b is not None
        lo = b[-2] if len(b) >= 2 else 0
        newest = req.content[lo:b[-1]].decode("utf-8")
        assert expected_user in newest           # the newest message is the user turn
        assert "_JSON" not in newest             # not the assistant tool_use args
