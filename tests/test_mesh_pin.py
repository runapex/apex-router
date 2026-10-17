"""Interface pinning: a VPN that routes the LAN subnet must not swallow mesh traffic (macOS)."""
import socket
import subprocess
import sys

import pytest

from apex_router.mesh import collect, netif

IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::1%en0 prefixlen 64 scopeid 0xe
\tinet 10.0.0.84 netmask 0xffffff00 broadcast 10.0.0.255
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1400
\tinet 192.168.132.4 --> 192.168.132.4 netmask 0xffffffff
"""


@pytest.fixture
def fake_ifconfig(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, IFCONFIG, ""))
    monkeypatch.setattr(socket, "if_nametoindex", {"en0": 14, "utun4": 22, "lo0": 1}.__getitem__)


def test_index_for_lan_ip_is_en0_not_vpn(fake_ifconfig):
    assert netif.iface_index_for_ip("10.0.0.84") == 14
    assert netif.iface_index_for_ip("192.168.132.4") == 22
    assert netif.iface_index_for_ip("10.0.0.99") is None
    assert netif.iface_index_for_ip("not-an-ip") is None


def test_index_none_off_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert netif.iface_index_for_ip("10.0.0.84") is None


class _Sock:
    family = socket.AF_INET

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def setsockopt(self, *a):
        if self.fail:
            raise OSError("nope")
        self.calls.append(a)


def test_pin_sets_ip_bound_if(fake_ifconfig):
    s = _Sock()
    assert netif.pin_socket(s, "10.0.0.84") is True
    assert s.calls == [(socket.IPPROTO_IP, 25, 14)]


def test_pin_noop_cases(fake_ifconfig):
    for ip in (None, "", "127.0.0.1", "10.0.0.99", "junk"):
        s = _Sock()
        assert netif.pin_socket(s, ip) is False and s.calls == []
    assert netif.pin_socket(_Sock(fail=True), "10.0.0.84") is False   # never raises


def test_collect_uses_pinned_opener_from_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_MESH_HOME", str(tmp_path))
    assert collect._local_ip() is None
    assert collect._pinned_opener(None) is collect._OPENER
    (tmp_path / "mesh.pid").write_text('{"host": "10.0.0.84"}')
    assert collect._local_ip() == "10.0.0.84"
    assert collect._pinned_opener("10.0.0.84") is not collect._OPENER
    (tmp_path / "mesh.pid").write_text("{bad")
    assert collect._local_ip() is None


def test_pinned_opener_reaches_loopback_server():
    """The custom connection class still does real HTTP (loopback: pin is a no-op)."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        op = collect._pinned_opener("127.0.0.1")
        with op.open(f"http://127.0.0.1:{srv.server_port}/x", timeout=3) as r:
            assert r.read() == b"ok"
    finally:
        srv.shutdown()
