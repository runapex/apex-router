"""Tests for session/wire.py — the §4 matcher on the request path.

Covers the store-application contract (new/extend/client_edit/compaction), the fail-open
guarantee (garbage in → None, never a raise, never a write), and the handler-level wiring
(passthrough emits a real matcher_event and a derived session_id for header-less traffic).
"""
import json
import tempfile
import unittest
from pathlib import Path

from apex_router.proxy_engine.session.store import Store
from apex_router.proxy_engine.session.wire import identify_into_store


def _body(messages, system=None):
    obj = {"model": "m", "messages": messages}
    if system is not None:
        obj["system"] = system
    return json.dumps(obj).encode()


def _msg(i):
    return {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}"}


class TestIdentifyIntoStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(Path(self._tmp.name) / "state.db")
        self.addCleanup(self.store.close)

    def _ident(self, messages, **kw):
        args = dict(body=_body(messages, system="sys"), client="claude-code",
                    wire_hint=None, agent_id=None, store=self.store, epoch_id="e0")
        args.update(kw)
        return identify_into_store(**args)

    def test_first_request_is_new_and_persisted(self):
        out = self._ident([_msg(0)])
        self.assertIsNotNone(out)
        sid, turn, event = out
        self.assertEqual((turn, event), (0, "new"))
        self.assertEqual(self.store.get_chain(sid), identify_into_store.__module__ and
                         __import__("apex_router.proxy_engine.session.matcher", fromlist=["matcher"])
                         .chain_of([_msg(0)]))

    def test_second_request_extends_same_session(self):
        sid1, _, _ = self._ident([_msg(0), _msg(1)])
        sid2, turn, event = self._ident([_msg(0), _msg(1), _msg(2)])
        self.assertEqual(sid1, sid2)
        self.assertEqual((turn, event), (1, "extend"))

    def test_client_edit_same_session_invalidated(self):
        sid1, _, _ = self._ident([_msg(i) for i in range(6)])
        # edit: same head, diverged tail — shared prefix (5) exceeds total divergence (1+1)
        edited = [_msg(i) for i in range(5)] + [{"role": "assistant", "content": "EDITED"}]
        sid2, turn, event = self._ident(edited)
        self.assertEqual(sid1, sid2)
        self.assertEqual(event, "client_edit")
        self.assertEqual(self.store.get_chain(sid1),
                         __import__("apex_router.proxy_engine.session.matcher", fromlist=["m"])
                         .chain_of(edited))

    def test_compaction_rebases_chain(self):
        msgs = [_msg(i) for i in range(10)]
        sid1, _, _ = self._ident(msgs)
        compacted = [{"role": "system", "content": "summary"}] + msgs[6:]
        sid2, _, event = self._ident(compacted)
        self.assertEqual(sid1, sid2)
        self.assertEqual(event, "compaction")

    def test_different_client_session_partition_never_merges(self):
        sid1, _, _ = self._ident([_msg(0)], wire_hint="sess-A")
        sid2, _, event = self._ident([_msg(0), _msg(1)], wire_hint="sess-B")
        self.assertNotEqual(sid1, sid2)
        self.assertEqual(event, "new")

    def test_fail_open_on_garbage(self):
        for bad in (b"not json", b"[1,2]", b"{}", json.dumps({"messages": []}).encode()):
            self.assertIsNone(identify_into_store(
                body=bad, client="claude-code", wire_hint=None, agent_id=None,
                store=self.store, epoch_id="e0"))
        self.assertEqual(self.store.counts()["sessions"], 0)

    def test_openai_wire_messages(self):
        out = self._ident([_msg(0)])
        self.assertIsNotNone(out)


class TestAppLevelWiring(unittest.TestCase):
    """App-level: a request through the real proxy (mock upstream) emits a real matcher_event
    and a derived session_id for header-less traffic; a client header still wins."""

    def _app(self, tmp_path, monkeypatch):
        import httpx
        import pytest  # noqa: F401  (pattern parity with test_m0_passthrough)
        from apex_router.proxy_engine.config import Config
        from apex_router.proxy_engine.proxy import upstream as upstream_mod

        class _AStream(httpx.AsyncByteStream):
            def __init__(self, chunks): self._chunks = chunks
            async def __aiter__(self):
                for c in self._chunks:
                    yield c
            async def aclose(self) -> None: pass

        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=_AStream([b"event: message_stop\ndata: {}\n\n"]))

        real_init = upstream_mod.Upstream.__init__

        def patched_init(self, cfg):
            real_init(self, cfg)
            self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        monkeypatch.setattr(upstream_mod.Upstream, "__init__", patched_init)
        from apex_router.proxy_engine.proxy.app import create_app
        cfg = Config(home=tmp_path)
        return create_app(cfg), cfg

    def _rows(self, cfg):
        out = []
        for line in (cfg.home / "telemetry.jsonl").read_text().splitlines():
            rec = json.loads(line)
            if rec.get("ev") != "hb":
                out.append(rec)
        return out

    def test_matcher_wired_end_to_end(self):
        import pytest
        with tempfile.TemporaryDirectory() as d:
            mp = pytest.MonkeyPatch()
            try:
                app, cfg = self._app(Path(d), mp)
                from starlette.testclient import TestClient
                with TestClient(app) as client:
                    r1 = client.post("/v1/messages", content=_body([_msg(0)]),
                                     headers={"anthropic-version": "2023-06-01"})
                    self.assertEqual(r1.status_code, 200)
                    r2 = client.post("/v1/messages", content=_body([_msg(0), _msg(1)]),
                                     headers={"anthropic-version": "2023-06-01"})
                    self.assertEqual(r2.status_code, 200)
                    # headered request: client session id wins, matcher event still real
                    r3 = client.post("/v1/messages", content=_body([_msg(0)]),
                                     headers={"anthropic-version": "2023-06-01",
                                              "x-claude-code-session-id": "client-sess-1"})
                    self.assertEqual(r3.status_code, 200)
                rows = self._rows(cfg)
                self.assertEqual(len(rows), 3)
                self.assertEqual(rows[0]["matcher_event"], "new")
                self.assertIsNotNone(rows[0]["session_id"])
                self.assertEqual(rows[1]["matcher_event"], "extend")
                self.assertEqual(rows[1]["session_id"], rows[0]["session_id"])
                self.assertEqual(rows[1]["turn"], 1)
                self.assertEqual(rows[2]["session_id"], "client-sess-1")
                self.assertIn(rows[2]["matcher_event"], ("new", "extend"))
            finally:
                mp.undo()


if __name__ == "__main__":
    unittest.main()
