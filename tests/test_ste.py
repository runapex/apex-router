"""ASD-STE100 default format: rules data, checker, CLAUDE.md install, CLI, and parity with the pi TS loader."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from apex_router import ste

ROOT = Path(__file__).resolve().parents[1]


def rules_of(findings):
    return sorted({f.rule for f in findings})


def test_rules_block_and_brief_present():
    r = ste.rules()
    assert (r["limits"]["procedure_words"], r["limits"]["description_words"]) == (20, 25)
    assert ste.rules_text().startswith("## Output format: ASD-STE100")
    for k in ("TASK", "CONTEXT", "STEPS", "LIMITS", "RESULT"):
        assert k in ste.brief_text()
    # the prompt block obeys its own rules; 'word' findings are the banned words it quotes as examples
    assert [f for f in ste.check(ste.rules_text()) if f.rule != "word"] == []
    assert ste.check(ste.brief_text()) == []


def test_rules_file_ships_in_the_package():
    assert (Path(ste.__file__).parent / "data" / "ste_rules.json").is_file()


@pytest.mark.parametrize("text,expected", [
    ("Run the test.", []),
    ("1. Utilize the tool to run it.", ["word"]),
    ("The file was deleted by the job.", ["passive"]),
    ("The proxy is running now.", ["ing"]),
    ("Start the proxy before the morning check.", []),          # 'morning' is an allowed noun
    ("The service is ready.", []),                               # 'is' + adjective: not passive
    ("1. " + " ".join(["word"] * 21), ["length"]),               # step: 20-word limit
    (" ".join(["word"] * 25) + ".", []),                          # description: 25 is the limit
    (" ".join(["word"] * 26) + ".", ["length"]),
])
def test_check_rules(text, expected):
    assert rules_of(ste.check(text)) == expected


def test_check_skips_code_tables_headings():
    text = "# Utilize heading\n```\nthe file was deleted by utilizing\n```\n| was deleted | utilize |\nRun it.\n"
    assert ste.check(text) == []


def test_check_kind_override():
    s = " ".join(["word"] * 22) + "."
    assert ste.check(s, kind="description") == []
    assert rules_of(ste.check(s, kind="procedure")) == ["length"]


def test_install_claude_idempotent_and_preserves_text(tmp_path):
    p = tmp_path / "CLAUDE.md"
    p.write_text("# Mine\nkeep this line\n")
    assert ste.install_claude(p) == "added"
    once = p.read_text()
    assert once.startswith("# Mine\nkeep this line\n")
    assert once.count(ste.CLAUDE_BEGIN) == 1
    assert ste.install_claude(p) == "unchanged"
    assert p.read_text() == once
    p.write_text(once.replace("ASD-STE100", "OLD") + "after\n")
    assert ste.install_claude(p) == "updated"
    assert p.read_text().endswith("after\n") and "ASD-STE100" in p.read_text()


def test_install_claude_new_file(tmp_path):
    p = tmp_path / "sub" / "CLAUDE.md"
    assert ste.install_claude(p) == "added"
    assert ste.CLAUDE_END in p.read_text()


def test_cli_dispatch(tmp_path, capsys):
    from apex_router.cli import main
    f = tmp_path / "a.md"
    f.write_text("Utilize it.\n")
    assert main(["ste", "check", str(f), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out[0]["rule"] == "word"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_ts_loader_reads_the_same_rules():
    js = ("import { rulesText, briefText } from './integrations/pi/lib/ste.ts';"
          "process.stdout.write(JSON.stringify([rulesText(), briefText()]));")
    r = subprocess.run(["node", "--input-type=module", "-e", js], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [ste.rules_text(), ste.brief_text()]
