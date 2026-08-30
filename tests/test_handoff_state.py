"""Structured handoff state — module contract + hook embed drift guard.

The hook (hooks/cache-handoff-nudge.sh) embeds the template in a heredoc because it runs
under the system python with no package-import guarantee; apex_router.handoff_state is the
source of truth. The drift guard extracts the heredoc and compares byte-for-byte so the two
can never silently diverge.
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from apex_router.handoff_state import FIELDS, PLACEHOLDER, render_template, validate  # noqa: E402

HOOK = ROOT / "hooks" / "cache-handoff-nudge.sh"


def _extract_hook_template() -> str:
    text = HOOK.read_text()
    m = re.search(r"cat <<'HANDOFF_STATE_TEMPLATE'\n(.*?)\nHANDOFF_STATE_TEMPLATE", text, re.S)
    assert m, "hook is missing the HANDOFF_STATE_TEMPLATE heredoc"
    return m.group(1) + "\n"


class TestTemplate(unittest.TestCase):
    def test_all_fields_present_in_order(self):
        t = render_template()
        pos = -1
        for name, _ in FIELDS:
            nxt = t.index(f"- **{name}**:", pos + 1)
            self.assertGreater(nxt, pos, name)
            pos = nxt

    def test_hook_embed_matches_module(self):
        self.assertEqual(_extract_hook_template(), render_template(),
                         "hook heredoc drifted from handoff_state.render_template() — "
                         "edit the module, re-embed")


class TestValidate(unittest.TestCase):
    def _filled(self, **overrides):
        vals = {name: f"value for {name}" for name, _ in FIELDS}
        vals.update(overrides)
        return "\n".join(f"- **{k}**: {v}" for k, v in vals.items())

    def test_complete_handoff_valid(self):
        self.assertEqual(validate(self._filled()), [])

    def test_missing_field_reported(self):
        text = self._filled()
        text = "\n".join(ln for ln in text.splitlines() if "**decisions**" not in ln)
        self.assertIn("missing field: decisions", validate(text))

    def test_placeholder_reported(self):
        problems = validate(self._filled(goal=f"{PLACEHOLDER} one sentence"))
        self.assertIn("unfilled placeholder: goal", problems)

    def test_template_itself_reports_all_placeholders(self):
        # the blank template is by definition unfilled — validate must say so, not crash
        problems = validate(render_template())
        self.assertEqual(len([p for p in problems if "placeholder" in p]), len(FIELDS))

    def test_prose_mention_is_not_a_field_line(self):
        # `note: **goal** unavailable` mentions the marker but is not `- **goal**: value`
        text = "\n".join(f"note: **{n}** unavailable" for n, _ in FIELDS)
        problems = validate(text)
        self.assertEqual(len([p for p in problems if p.startswith("missing field")]),
                         len(FIELDS))

    def test_duplicate_placeholder_cannot_hide_behind_filled_line(self):
        text = self._filled() + "\n- **goal**: " + PLACEHOLDER + " later edit\n"
        self.assertIn("unfilled placeholder: goal", validate(text))


class TestCli(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run([sys.executable, "-m", "apex_router.handoff_state", *args],
                              capture_output=True, text=True, cwd=ROOT,
                              env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"})

    def test_template_cli(self):
        r = self._run("template")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, render_template())

    def test_validate_cli_exit_codes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            good = Path(d) / "good.md"
            good.write_text("\n".join(f"- **{n}**: x" for n, _ in FIELDS))
            bad = Path(d) / "bad.md"
            bad.write_text("- **goal**: x\n")
            self.assertEqual(self._run("validate", str(good)).returncode, 0)
            self.assertEqual(self._run("validate", str(bad)).returncode, 2)


if __name__ == "__main__":
    unittest.main()
