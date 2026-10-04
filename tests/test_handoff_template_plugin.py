"""The plugin's /apex handoff block is byte-for-byte apex_router.handoff_state.render_template()."""
from pathlib import Path

from apex_router.handoff_state import render_template

ROOT = Path(__file__).resolve().parents[1]
START = "export const HANDOFF_TEMPLATE = `"
END = "`\n// END HANDOFF_STATE_TEMPLATE"


def test_plugin_embed_matches_module():
    text = (ROOT / "plugin" / "hooks" / "handoff.ts").read_text()
    embedded = text[text.index(START) + len(START):text.index(END)]
    assert embedded == render_template(), "re-embed: python -m apex_router.handoff_state template"
