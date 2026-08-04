"""Cross-platform watcher install — unit-file generation and CLI wiring.

Tests the pure generators (no actual install: we don't touch launchctl/systemctl in CI). The
generated launchd plist and systemd units must be well-formed and pin the installing interpreter.
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router import watch  # noqa: E402


class TestLaunchdPlist(unittest.TestCase):
    def test_daemon_plist_has_keepalive_no_calendar(self):
        p = watch._launchd_plist("com.test", ["/py", "-m", "x"], keepalive=True, calendar=None)
        self.assertTrue(p.startswith("<?xml"))
        self.assertIn("<key>KeepAlive</key><true/>", p)
        self.assertNotIn("StartCalendarInterval", p)
        self.assertIn("<string>/py</string>", p)

    def test_calendar_plist_has_schedule_no_keepalive(self):
        p = watch._launchd_plist("com.test", ["/py"], keepalive=False, calendar=(9, 0))
        self.assertIn("StartCalendarInterval", p)
        self.assertIn("<key>Hour</key><integer>9</integer>", p)
        self.assertNotIn("<key>KeepAlive</key><true/>", p)


class TestSystemdUnits(unittest.TestCase):
    def test_three_units_present(self):
        u = watch._systemd_units()
        self.assertEqual(set(u), {"apex-router-drain.service",
                                  "apex-router-daily.service", "apex-router-daily.timer"})

    def test_drain_restarts_always(self):
        self.assertIn("Restart=always", watch._systemd_units()["apex-router-drain.service"])

    def test_timer_is_daily(self):
        self.assertIn("OnCalendar=*-*-* 09:00:00", watch._systemd_units()["apex-router-daily.timer"])

    def test_units_pin_the_interpreter(self):
        # a venv install must invoke ITS OWN python, not a bare `python`
        drain = watch._systemd_units()["apex-router-drain.service"]
        self.assertIn(sys.executable, drain)


class TestRunDailyFailOpen(unittest.TestCase):
    def test_run_daily_never_raises(self):
        # even if the report can't be built, the scheduled run must exit 0 (never break the timer)
        rc = watch.run_daily()
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
