"""preflight: fault types from earlier review findings, bounded scan, no finding text in the output."""
from __future__ import annotations

import json
import subprocess
from collections import Counter

from apex_router import preflight as P


def _job(root, jid, agent, state, task="", result="", cost=None):
    d = root / jid
    d.mkdir(parents=True)
    m = {"id": jid, "agent": agent, "state": state, "task": task}
    if cost is not None:
        m["usage"] = {"cost": cost}
    (d / "meta.json").write_text(json.dumps(m))
    if result:
        (d / "result.md").write_text(result)


def test_finding_lines_skip_status_lines():
    txt = ("- **P1: Unbounded read.** [x.py:3](/w/src/apex_router/x.py:3) reads the whole file\n"
           "- P1 (1): CLOSED. `records.py:131` reads only cap+1\n"
           "- New P0/P1 findings: none.\nGO\n")
    assert P.finding_lines(txt) == [txt.splitlines()[0]]


def test_classify_names_the_known_types():
    assert "deadline or hang" in P.classify("P1: a slow launchctl can hang the route")
    assert "unbounded read or listing" in P.classify("P1: reads the entire reservoir without a size limit")
    assert "fail counted as pass" in P.classify("P1: a crashed oracle can still pass")
    assert P.classify("P1: the button is blue") == []


def test_analyse_counts_per_file_and_related_jobs(tmp_path):
    _job(tmp_path, "20261001000001-aaaa", "worker", "killed(cost)", task="edit src/apex_router/studio/records.py", cost=25.5)
    _job(tmp_path, "20261001000002-bbbb", "xval", "done",
         result="- **P1: unbounded listing.** [r.py](/wt/x/src/apex_router/studio/records.py:126) enumerates every entry\n"
                "- **P1: route can hang.** [s.py](/wt/x/src/apex_router/studio/server.py:9) waits with no timeout\n")
    _job(tmp_path, "20261001000003-cccc", "xval", "done",
         result="- **P1: secret leak.** [e.py](/wt/src/apex_router/other.py:1) shows a raw token\n")
    jobs = P.scan_jobs(tmp_path)
    a = P.analyse(["src/apex_router/studio/records.py"], jobs=jobs)
    assert a["faults_here"] == {"unbounded read or listing": 1}
    assert a["top"][0] == "unbounded read or listing"
    assert a["jobs_here"]["n"] == 1 and a["jobs_here"]["usd_lost_at_cost_limit"] == 25.5
    assert "an oversized file" in a["tests_first"]["unbounded read or listing"]


def test_rank_blends_file_and_global_and_drops_other():
    here = Counter({"race or check-then-use gap": 1})
    allf = Counter({"deadline or hang": 30, "other": 99, "race or check-then-use gap": 1})
    assert P.rank(here, allf, k=2) == ["race or check-then-use gap", "deadline or hang"]


def test_brief_block_has_no_finding_text(tmp_path):
    _job(tmp_path, "20261001000002-bbbb", "xval", "done",
         result="- **P1: secret leak.** [k.py](/w/src/apex_router/k.py:1) prints sk-ABCDEFGH12345678 to the log\n")
    out = P.brief_block(P.analyse(["src/apex_router/k.py"], jobs=P.scan_jobs(tmp_path)))
    assert "sk-ABCD" not in out and "prints" not in out
    assert "raw text or secret in output (1 / 1)" in out and "THREAT SCOPE" in out


def test_scan_is_bounded_and_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "JOBS_MAX", 3)
    for i in range(6):
        _job(tmp_path, f"2026100100000{i}-x", "worker", "done")
    (tmp_path / "20261001000009-bad").mkdir()
    (tmp_path / "20261001000009-bad" / "meta.json").write_text("{bad")
    big = tmp_path / "20261001000008-big"
    big.mkdir()
    (big / "meta.json").write_text('{"agent": "worker", "task": "' + "a" * (P.META_MAX + 10) + '"}')
    assert [j["id"] for j in P.scan_jobs(tmp_path)] == ["20261001000005-x"]   # 3 newest: bad, big (no dict), 5
    assert P.scan_jobs(tmp_path / "missing") == []


def test_review_rounds_counts_xval_fix_commits(tmp_path):
    g = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(g[:3] + ["init", "-q"], check=True)
    (tmp_path / "a.py").write_text("1")
    subprocess.run(g + ["add", "a.py"], check=True)
    subprocess.run(g + ["commit", "-qm", "fix: bound the read (xval P1 x2)"], check=True)
    (tmp_path / "a.py").write_text("2")
    subprocess.run(g + ["commit", "-qam", "feature"], check=True)
    r = P.review_rounds(tmp_path, ["a.py"])
    assert r == {"fix_commits": 1, "by_file": {"a.py": 1}}


def test_cli_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    assert P.main(["--files", "src/x.py", "--model", "", "--repo", str(tmp_path)]) == 0
    assert "SUCCESS" in capsys.readouterr().out
