"""Managed proxy-serve unit — the gateway as a supervised service.

The proxy is a LIVE data plane, so it is a SEPARATE opt-in unit (install_serve), not folded into
the default watcher install. These tests pin the unit generation (well-formed, KeepAlive daemon,
runs `apex-router serve`) without touching launchctl/systemctl.
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router import watch  # noqa: E402


class TestServeUnit(unittest.TestCase):
    def test_serve_label_distinct_from_watchers(self):
        self.assertNotIn(watch.LABEL_SERVE, (watch.LABEL_DRAIN, watch.LABEL_DAILY))

    def test_launchd_serve_plist_is_keepalive_daemon(self):
        p = watch._launchd_plist(watch.LABEL_SERVE, [watch._py(), "-m", "apex_router.cli", "serve"],
                                 keepalive=True, calendar=None)
        self.assertTrue(p.startswith("<?xml"))
        self.assertIn("<key>KeepAlive</key><true/>", p)      # always-on
        self.assertIn("apex_router.cli", p)
        self.assertIn("serve", p)

    def test_systemd_serve_unit_restarts_always(self):
        u = watch._systemd_serve_unit()
        self.assertIn("apex-router-serve.service", u)
        body = u["apex-router-serve.service"]
        self.assertIn("Restart=always", body)
        self.assertIn("serve", body)
        self.assertIn(sys.executable, body)   # pins the install interpreter

    def test_serve_env_is_baked_into_units(self):
        # env doesn't propagate to launchd/systemd, so the upstream/port MUST be in the unit itself.
        import os
        orig = dict(os.environ)
        os.environ["APEX_ANTHROPIC_UPSTREAM"] = "https://my-gateway.example/claude"
        os.environ["APEX_PORT"] = "8788"
        try:
            plist = watch._launchd_plist(
                watch.LABEL_SERVE, [watch._py(), "-m", "apex_router.cli", "serve"],
                keepalive=True, calendar=None, extra_env=watch._serve_env())
            self.assertIn("APEX_ANTHROPIC_UPSTREAM", plist)
            self.assertIn("my-gateway.example", plist)
            self.assertIn("<key>APEX_PORT</key><string>8788</string>", plist)
            unit = watch._systemd_serve_unit()["apex-router-serve.service"]
            self.assertIn("Environment=APEX_ANTHROPIC_UPSTREAM=https://my-gateway.example/claude", unit)
            self.assertIn("Environment=APEX_PORT=8788", unit)
        finally:
            os.environ.clear()
            os.environ.update(orig)

    def test_serve_env_derives_upstream_from_foundry_base_url(self):
        # Restart-survival regression: a box wired for Claude Code sets ANTHROPIC_FOUNDRY_BASE_URL,
        # not APEX_ANTHROPIC_UPSTREAM. _serve_env must derive the proxy upstream from it, or the
        # daemon silently falls back to api.anthropic.com after every reboot.
        import os
        orig = dict(os.environ)
        for k in ("APEX_ANTHROPIC_UPSTREAM", "ANTHROPIC_FOUNDRY_BASE_URL"):
            os.environ.pop(k, None)
        os.environ["ANTHROPIC_FOUNDRY_BASE_URL"] = "https://foundry.example/claude"
        try:
            env = watch._serve_env()
            self.assertEqual(env.get("APEX_ANTHROPIC_UPSTREAM"),
                             "https://foundry.example/claude")
        finally:
            os.environ.clear()
            os.environ.update(orig)

    def test_explicit_apex_upstream_wins_over_foundry(self):
        # An explicit APEX_ANTHROPIC_UPSTREAM must NOT be overridden by the Foundry fallback.
        import os
        orig = dict(os.environ)
        os.environ["APEX_ANTHROPIC_UPSTREAM"] = "https://explicit.example/claude"
        os.environ["ANTHROPIC_FOUNDRY_BASE_URL"] = "https://foundry.example/claude"
        try:
            env = watch._serve_env()
            self.assertEqual(env["APEX_ANTHROPIC_UPSTREAM"], "https://explicit.example/claude")
        finally:
            os.environ.clear()
            os.environ.update(orig)

    def test_serve_env_skips_loopback_foundry_url(self):
        # proxy-mode: ANTHROPIC_FOUNDRY_BASE_URL points at the LOCAL proxy itself; deriving the
        # upstream from it would make the proxy forward to itself (infinite loop) — so it must be
        # skipped, leaving no APEX_ANTHROPIC_UPSTREAM baked.
        import os
        orig = dict(os.environ)
        for k in ("APEX_ANTHROPIC_UPSTREAM", "ANTHROPIC_FOUNDRY_BASE_URL"):
            os.environ.pop(k, None)
        for loopback in ("http://localhost:8788", "http://127.0.0.1:8788"):
            os.environ["ANTHROPIC_FOUNDRY_BASE_URL"] = loopback
            try:
                self.assertNotIn("APEX_ANTHROPIC_UPSTREAM", watch._serve_env(), loopback)
            finally:
                os.environ.clear()
                os.environ.update(orig)

    def test_loopback_guard_covers_127_range_ipv6_and_trailing_dot(self):
        # the self-loop guard must catch more than literal 127.0.0.1: the whole 127/8 range, ::1,
        # and a trailing-dot FQDN all point back at this host.
        import os
        orig = dict(os.environ)
        for k in ("APEX_ANTHROPIC_UPSTREAM", "ANTHROPIC_FOUNDRY_BASE_URL"):
            os.environ.pop(k, None)
        for lb in ("http://127.0.0.2:8788", "http://[::1]:8788", "http://localhost.:8788"):
            os.environ["ANTHROPIC_FOUNDRY_BASE_URL"] = lb
            try:
                self.assertNotIn("APEX_ANTHROPIC_UPSTREAM", watch._serve_env(), lb)
            finally:
                os.environ.clear()
                os.environ.update(orig)

    def test_serve_env_drops_values_with_newlines(self):
        # A newline in a baked value could inject an extra plist key / systemd directive — drop it.
        import os
        orig = dict(os.environ)
        os.environ["APEX_ANTHROPIC_UPSTREAM"] = "https://gw.example/claude\nExecStart=/bin/evil"
        try:
            self.assertNotIn("APEX_ANTHROPIC_UPSTREAM", watch._serve_env())
        finally:
            os.environ.clear()
            os.environ.update(orig)

    def test_az_injection_keys_are_baked_when_set(self):
        # If auth injection is enabled, the daemon (no interactive shell) needs these in the unit.
        import os
        orig = dict(os.environ)
        os.environ["APEX_INJECT_AZURE_TOKEN"] = "1"
        os.environ["APEX_AZ_BIN"] = "/opt/homebrew/bin/az"
        try:
            env = watch._serve_env()
            self.assertEqual(env["APEX_INJECT_AZURE_TOKEN"], "1")
            self.assertEqual(env["APEX_AZ_BIN"], "/opt/homebrew/bin/az")
        finally:
            os.environ.clear()
            os.environ.update(orig)

    def test_xml_escaping_in_plist_env(self):
        # a value with & or < must not break the plist XML
        p = watch._launchd_plist("x", ["/py"], keepalive=True, calendar=None,
                                 extra_env={"K": "a&b<c"})
        self.assertIn("a&amp;b&lt;c", p)
        self.assertNotIn("a&b<c", p)

    def test_default_watcher_install_does_NOT_include_serve(self):
        # the proxy must not start silently with the ordinary watchers — it's a data plane.
        # verified structurally: the default launchd/systemd unit lists exclude LABEL_SERVE.
        src = Path(watch.__file__).read_text()
        # the default install unit tuple must not reference the serve label alongside drain/daily
        default_block = src.split("def _launchd_install")[1].split("def _launchd_uninstall")[0]
        self.assertNotIn("LABEL_SERVE", default_block)


if __name__ == "__main__":
    unittest.main()
