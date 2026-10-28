"""Studio open counter (studio/opens.py): a count per day only, bounded, never stops the page."""
from __future__ import annotations

import datetime as dt
import json
import os

from apex_router.studio import opens

D = dt.date(2026, 10, 28)


def test_record_counts_per_day_and_summary(tmp_path):
    for _ in range(3):
        assert opens.record(tmp_path, D)
    assert opens.record(tmp_path, D - dt.timedelta(days=5))
    assert opens.record(tmp_path, D - dt.timedelta(days=20))
    assert opens.summary(tmp_path, D) == {"today": 3, "d7": 4, "d30": 5, "days_seen": 3}


def test_file_holds_only_day_counts(tmp_path):
    opens.record(tmp_path, D)
    doc = json.loads((tmp_path / "opens.json").read_text())
    assert doc == {"days": {"2026-10-28": 1}}


def test_days_are_capped(tmp_path):
    for i in range(opens.DAYS_KEEP + 30):
        opens.record(tmp_path, D - dt.timedelta(days=i))
    doc = json.loads((tmp_path / "opens.json").read_text())
    assert len(doc["days"]) == opens.DAYS_KEEP
    assert "2026-10-28" in doc["days"]          # the newest days stay


def test_oversized_or_bad_file_starts_again(tmp_path):
    (tmp_path / "opens.json").write_text("x" * (opens.READ_MAX + 10))
    assert opens.record(tmp_path, D)
    assert opens.summary(tmp_path, D)["today"] == 1
    (tmp_path / "opens.json").write_text('{"days": {"2026-10-28": "7", "bad": 3, "2026-10-27": -1, "2026-10-26": true}}')
    assert opens.summary(tmp_path, D)["d30"] == 0
    (tmp_path / "opens.json").write_text("[" * 100000)
    assert opens.summary(tmp_path, D)["today"] == 0


def test_a_read_only_dir_never_raises(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        assert opens.record(ro, D) is False
    finally:
        os.chmod(ro, 0o700)


def test_no_temp_file_is_left(tmp_path):
    opens.record(tmp_path, D)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["opens.json", "opens.json.lock"]


def test_a_page_open_adds_one_and_an_api_call_does_not(tmp_path, monkeypatch):
    import http.client
    import threading
    import time

    import studio_fixture as F
    from apex_router.studio import flow, server

    root = tmp_path / "home"
    F.make_home(root, time.time(), telemetry=False)
    for k, v in F.env_for(root).items():
        monkeypatch.setenv(k, v)
    srv = server.make_server(port=0, token="t" * 32, paths=flow.resolve_paths())
    srv.stats_fetch = srv.restart_fetch = lambda url: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(path, hdr=None):
            c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
            c.request("GET", path, headers={"Host": f"127.0.0.1:{srv.port}", **(hdr or {})})
            r = c.getresponse()
            r.read()
            c.close()
            return r.status
        assert get("/?token=" + "t" * 32) == 200
        assert get("/api/ping", {"X-Studio-Token": "t" * 32}) == 200
        sdir = flow.resolve_paths().studio
        assert opens.summary(sdir)["today"] == 1
        raw = (sdir / "opens.json").read_text()
        assert "t" * 8 not in raw and "token" not in raw
    finally:
        srv.shutdown()
        srv.server_close()
