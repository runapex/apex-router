"""Offline regression coverage for the config-driven classifier CLI."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "change_classifier.py"
spec = importlib.util.spec_from_file_location("change_classifier", SCRIPT)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)


@pytest.fixture(autouse=True)
def isolate_git_config(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def answer():
    return {
        "change_class": "docs", "change_summary": "Document a change",
        "requirement_fit": {"verdict": "neutral", "acs_touched": [],
                            "confidence": 0.8, "why": "Only documentation"},
        "blast_radius": {"level": "low", "surfaces_at_risk": [],
                         "regression_signal": "not-covered", "confidence": 0.8,
                         "why": "No tests supplied"},
        "top_concern": "none",
    }


def command(answer, exit_code=0, stderr=False):
    return [sys.executable, "-c", "import sys; sys.stdin.read(); "
            f"print({json.dumps(answer)!r}, file=sys.{'stderr' if stderr else 'stdout'}); "
            f"sys.exit({exit_code})"]


@pytest.mark.parametrize("summary", ['Adds a { example', 'Removes }', 'A \\"quoted\\" { value'])
def test_extract_handles_json_strings_and_decoys(answer, summary):
    answer["change_summary"] = summary
    text = 'prose } { broken\n{"confidence":0.8}\n```json\n' + json.dumps(answer) + '\n```'
    assert cc.extract_json(text) == answer


@pytest.mark.parametrize("bad", [{"error": "unauthorized"}, {},
                                  {"change_class": "docs", "requirement_fit": {}, "blast_radius": {}}])
def test_extract_rejects_non_classifications(bad):
    assert cc.extract_json(json.dumps(bad)) is None


@pytest.mark.parametrize("field,value", [("requirement_fit", None), ("change_class", []),
                                           ("blast_radius", {"level": "low"})])
def test_extract_rejects_malformed_contract(answer, field, value):
    answer[field] = value
    assert cc.extract_json(json.dumps(answer)) is None


def test_regression_signal_must_be_an_answer(answer):
    answer["blast_radius"]["regression_signal"] = "confirmed | refuted | not-covered"
    assert cc.extract_json(json.dumps(answer)) is None


def test_prompt_requires_numeric_confidence():
    assert "JSON numbers" in cc.contract_text()
    for axis in ("requirement_fit", "blast_radius"):
        assert isinstance(cc.OUTPUT_CONTRACT[axis]["confidence"], float)


@pytest.mark.parametrize("confidence", ["0.8", 1.5, -0.1, True, float("nan")])
def test_invalid_confidence_rejected(answer, confidence):
    answer["requirement_fit"]["confidence"] = confidence
    assert not cc.valid_classification(answer)


def test_valid_member(answer):
    result = cc.run_member("a", command(answer), "prompt")
    assert result["ok"] and result["json"] == answer


@pytest.mark.parametrize("exit_code,stderr", [(1, False), (0, True)])
def test_member_does_not_accept_failed_command_or_stderr(answer, exit_code, stderr):
    result = cc.run_member("a", command(answer, exit_code, stderr), "prompt")
    assert not result["ok"] and result["json"] is None
    assert result["raw_tail"]


def test_member_spawn_and_timeout_failures(tmp_path):
    assert not cc.run_member("a", [str(tmp_path / "missing")], "prompt")["ok"]
    assert not cc.run_member("a", [str(tmp_path)], "prompt")["ok"]
    assert not cc.run_member("a", [sys.executable, "-c", "import time; time.sleep(2)"],
                             "prompt", timeout=0.01)["ok"]


@pytest.mark.parametrize("panel", [[], {}, [{"name": "a", "cmd": []}],
                                    [{"name": "a", "cmd": "echo"}],
                                    [{"name": [], "cmd": ["echo"]}],
                                    [{"name": " ", "cmd": ["echo"]}],
                                    [{"name": "a", "cmd": [1]}],
                                    [{"name": "a", "cmd": ["echo"]}] * 2])
def test_load_panel_rejects_invalid_entries(tmp_path, panel):
    path = tmp_path / "panel.json"
    path.write_text(json.dumps(panel))
    with pytest.raises((SystemExit, ValueError)):
        cc.load_panel(str(path))


def test_load_panel_accepts_empty_argument(tmp_path):
    panel = [{"name": "a", "cmd": ["claude", "--tools", ""]}]
    path = tmp_path / "panel.json"
    path.write_text(json.dumps(panel))
    assert cc.load_panel(str(path)) == panel


def test_non_utf8_member_output_does_not_crash_panel():
    cmd = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"]
    assert not cc.run_member("a", cmd, "prompt")["ok"]


def test_untracked_symlinks_not_followed(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-m", "base")
    (tmp_path / "link").symlink_to("/etc/hosts")
    with pytest.raises(ValueError, match="symlink"):
        cc.git_diff(str(tmp_path), "HEAD", include_untracked=True)


def test_divergence_flags_class_and_small_blast_disagreement(answer):
    other = copy.deepcopy(answer)
    other["change_class"] = "compute-core"
    other["blast_radius"]["level"] = "medium"
    result = cc.divergence([{"name": "a", "ok": True, "json": answer},
                            {"name": "b", "ok": True, "json": other}])
    assert any("change-class" in flag for flag in result["flags"])
    assert any("blast-radius" in flag for flag in result["flags"])
    assert "panel broadly agrees" not in result["flags"]


def test_failed_members_cannot_agree():
    results = [cc.run_member(n, command({"error": "unauthorized"}, 1), "prompt")
               for n in ("a", "b")]
    summary = cc.divergence(results)
    assert summary["panel_n"] == 0
    assert summary["flags"]


def test_single_survivor_risk_is_visible(answer):
    answer["requirement_fit"]["verdict"] = "regresses-req"
    answer["blast_radius"]["level"] = "critical"
    summary = cc.divergence([{"name": "a", "ok": True, "json": answer}])
    assert any("REGRESSES" in flag for flag in summary["flags"])
    assert any("HIGH/CRITICAL" in flag for flag in summary["flags"])
    assert summary["change_class"]["agree"] is None
    assert summary["blast_level"]["spread"] is None
    assert set(summary) == set(cc.divergence([]))


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def test_repo_input_errors_and_untracked_opt_in(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-m", "base")
    (tmp_path / "private.env.bak").write_text("SECRET_SENTINEL")
    assert "SECRET_SENTINEL" not in cc.git_diff(str(tmp_path), "HEAD")
    assert "SECRET_SENTINEL" in cc.git_diff(str(tmp_path), "HEAD", include_untracked=True)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        cc.git_diff(str(tmp_path), "missing-ref")
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        cc.git_diff(str(tmp_path / "missing"), "HEAD")


def test_clipped_input_visible_in_report(answer):
    panel = [{"name": n, "cmd": command(answer)} for n in ("b", "a")]
    report = cc.classify("d" * 22001, "t" * 6002, "r" * 9003, panel)
    assert report["input_clipped_chars"] == {"diff": 1, "tests": 2, "reqs": 3}
    assert any("INCOMPLETE INPUT" in flag for flag in report["divergence"]["flags"])
    assert "panel broadly agrees" not in report["divergence"]["flags"]
    assert list(report["panel"]) == ["b", "a"]


def test_cli_all_failed_is_operational_failure(tmp_path):
    panel = tmp_path / "panel.json"
    panel.write_text(json.dumps([{"name": "bad", "cmd": command({"error": "auth"}, 1)}]))
    reqs = tmp_path / "reqs.md"
    reqs.write_text("Document the change")
    proc = subprocess.run([sys.executable, str(SCRIPT), "--diff", "-", "--reqs", str(reqs),
                           "--panel", str(panel)], input="+ docs", capture_output=True, text=True)
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["divergence"]["flags"]


def test_report_write_failure_preserves_results(tmp_path, answer):
    panel = tmp_path / "panel.json"
    panel.write_text(json.dumps([{"name": "a", "cmd": command(answer)}]))
    reqs = tmp_path / "reqs.md"
    reqs.write_text("Document the change")
    proc = subprocess.run([sys.executable, str(SCRIPT), "--diff", "-", "--reqs", str(reqs),
                           "--panel", str(panel), "--out", str(tmp_path / "missing" / "report.json")],
                          input="+ docs", capture_output=True, text=True)
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["panel"]["a"]["classification"] == answer
    assert "could not write report" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_cli_end_to_end(tmp_path, answer):
    panel = tmp_path / "panel.json"
    panel.write_text(json.dumps([{"name": "a", "cmd": command(answer)},
                                {"name": "b", "cmd": command(answer)},
                                {"name": "failed", "cmd": command({"error": "auth"}, 1)}]))
    reqs = tmp_path / "reqs.md"
    reqs.write_text("Document the change")
    out = tmp_path / "report.json"
    proc = subprocess.run([sys.executable, str(SCRIPT), "--diff", "-", "--reqs", str(reqs),
                           "--panel", str(panel), "--out", str(out)], input="+ documentation",
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(out.read_text())
    assert report["divergence"]["panel_n"] == 2
    assert report["panel"]["a"]["classification"] == answer
    assert not report["panel"]["failed"]["ok"]
    assert "flags" in proc.stdout
