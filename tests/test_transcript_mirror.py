"""Transcript mirror: `worldmodel snapshot` copies, readers union live + mirror without double
counting, the launchd/systemd snapshot unit."""
from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from apex_router import labels as L
from apex_router import transcript_mirror as TM
from apex_router import watch
from apex_router.worldmodel import cli as WCLI
from apex_router.worldmodel import steps as S
from test_worldmodel_steps import SID_CL, SID_PI, _fixture, _read


@pytest.fixture(autouse=True)
def _no_ollama(monkeypatch):
    monkeypatch.setattr(S, "_auto_embed", lambda: None)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _homes(tmp_path):
    uh, tel = _fixture(tmp_path)
    return uh, tel, tmp_path / "ar"


def _extras(uh: Path) -> None:
    """Codex session, a workflow journal, a memory file (not a transcript) and a symlink."""
    c = uh / ".codex" / "sessions" / "2026" / "10" / "07" / "rollout-x.jsonl"
    c.parent.mkdir(parents=True)
    c.write_text('{"type":"session_meta"}\n')
    wf = uh / ".claude" / "projects" / "-w-proj" / SID_CL / "subagents" / "workflows" / "wf_1"
    wf.mkdir(parents=True)
    (wf / "journal.jsonl").write_text("{}\n")
    mem = uh / ".claude" / "projects" / "-w-proj" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("not a transcript")
    os.symlink(c, uh / ".claude" / "projects" / "-w-proj" / "link.jsonl")


# ---- snapshot -------------------------------------------------------------------------------

def test_snapshot_copies_counts_and_permissions(tmp_path):
    uh, _, home = _homes(tmp_path)
    _extras(uh)
    r = TM.snapshot(home=home, user_home=uh)
    m = home / "transcripts"
    assert r["errors"] == 0 and r["updated"] == 0 and r["unchanged"] == 0
    live = [p for p in uh.rglob("*") if p.is_file() and not p.is_symlink()
            and "memory" not in p.parts]
    assert r["new"] == len(live)
    assert r["bytes"] == sum(p.stat().st_size for p in live)
    assert (m / "codex" / "2026" / "10" / "07" / "rollout-x.jsonl").exists()
    assert (m / "claude" / "-w-proj" / f"{SID_CL}.jsonl").exists()
    assert (m / "claude" / "-w-proj" / SID_CL / "subagents" / "workflows" / "wf_1"
            / "journal.jsonl").exists()
    assert not (m / "claude" / "-w-proj" / "memory").exists()        # not a transcript
    assert not (m / "claude" / "-w-proj" / "link.jsonl").exists()    # symlinks not followed
    pis = list((m / "pi").rglob("*.jsonl"))
    assert len(pis) == 1 and pis[0].name.endswith(SID_PI + ".jsonl")
    for p in m.rglob("*"):
        assert _mode(p) == (0o700 if p.is_dir() else 0o600), p
    assert _mode(m) == 0o700
    assert not [p for p in m.rglob(".*tmp-*")]                       # no tmp left behind
    # idempotent
    r2 = TM.snapshot(home=home, user_home=uh)
    assert (r2["new"], r2["updated"], r2["bytes"]) == (0, 0, 0)
    assert r2["unchanged"] == r["new"]


def test_snapshot_grows_never_truncates_never_deletes(tmp_path):
    uh, _, home = _homes(tmp_path)
    TM.snapshot(home=home, user_home=uh)
    live = uh / ".claude" / "projects" / "-w-proj" / f"{SID_CL}.jsonl"
    mirror = home / "transcripts" / "claude" / "-w-proj" / f"{SID_CL}.jsonl"
    with open(live, "a") as f:
        f.write('{"more": 1}\n')
    r = TM.snapshot(home=home, user_home=uh)
    assert r["updated"] == 1 and mirror.read_bytes() == live.read_bytes()
    full = mirror.read_bytes()
    live.write_text('{"rewritten": 1}\n')                            # live shrank
    r = TM.snapshot(home=home, user_home=uh)
    assert r["kept_mirror_larger"] == 1 and mirror.read_bytes() == full
    shutil.rmtree(uh / ".pi")                                        # pruned
    r = TM.snapshot(home=home, user_home=uh)
    assert len(list((home / "transcripts" / "pi").rglob("*.jsonl"))) == 1


def test_snapshot_dry_run_large_and_fail_open(tmp_path, monkeypatch):
    uh, _, home = _homes(tmp_path)
    r = TM.snapshot(home=home, user_home=uh, dry_run=True)
    assert r["new"] > 0 and not (home / "transcripts").exists()
    r = TM.snapshot(home=home, user_home=uh, max_bytes=1000)
    assert r["skipped_large"] >= 1 and any("skipped" in n for n in r["notes"])
    # one unreadable file: counted as an error, the rest still copied
    shutil.rmtree(home / "transcripts")
    real = TM._copy

    def flaky(src, dst, mtime):
        if src.name.startswith("agent-"):
            raise PermissionError("nope")
        return real(src, dst, mtime)
    monkeypatch.setattr(TM, "_copy", flaky)
    r = TM.snapshot(home=home, user_home=uh)
    assert r["errors"] >= 1 and r["new"] >= 2
    assert (home / "transcripts" / "claude" / "-w-proj" / f"{SID_CL}.jsonl").exists()


def test_snapshot_cli(tmp_path, monkeypatch, capsys):
    uh, _, home = _homes(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: uh))
    assert WCLI.main(["snapshot", "--home", str(home), "--json"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["new"] > 0 and r["errors"] == 0
    assert WCLI.main(["snapshot", "--home", str(home), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "new 0" in out and "unchanged" in out


# ---- readers: union, no double count --------------------------------------------------------

def _ids(uh, home):
    return sorted(t["id"] for p in L.transcripts(uh, home) for t in L.extract(p))


def test_readers_no_mirror_matches_live(tmp_path):
    uh, _, home = _homes(tmp_path)
    paths = L.transcripts(uh, home)
    assert len(paths) == 2 and all(str(p).startswith(str(uh)) for p in paths)


def test_readers_both_present_counted_once_prefer_larger(tmp_path):
    uh, _, home = _homes(tmp_path)
    before = _ids(uh, home)
    TM.snapshot(home=home, user_home=uh)
    assert _ids(uh, home) == before                                  # no double count
    assert all(str(p).startswith(str(uh)) for p in L.transcripts(uh, home))   # tie -> live
    subs = S.sub_transcripts(uh, home)
    assert len(subs) == len({(s[0], s[1], s[3]) for s in subs})
    assert all(str(s[2]).startswith(str(uh)) for s in subs)
    # live shrank (rewritten) -> the larger mirror copy wins
    live = uh / ".claude" / "projects" / "-w-proj" / f"{SID_CL}.jsonl"
    live.write_text("")
    picked = [p for p in L.transcripts(uh, home) if p.stem == SID_CL]
    assert len(picked) == 1 and str(picked[0]).startswith(str(home))
    assert _ids(uh, home) == before
    # live grew past the mirror -> live wins again
    live.write_text(open(home / "transcripts" / "claude" / "-w-proj" / f"{SID_CL}.jsonl").read()
                    + '{"type":"user"}\n')
    picked = [p for p in L.transcripts(uh, home) if p.stem == SID_CL]
    assert str(picked[0]).startswith(str(uh))


def test_readers_pruned_live_kept_in_mirror_and_mirror_only(tmp_path):
    uh, _, home = _homes(tmp_path)
    before = _ids(uh, home)
    srcs = sorted({t["source"] for p in L.transcripts(uh, home) for t in L.extract(p)})
    TM.snapshot(home=home, user_home=uh)
    shutil.rmtree(uh / ".pi")                                        # pi pruned one session
    assert _ids(uh, home) == before
    shutil.rmtree(uh / ".claude")                                    # mirror only
    assert _ids(uh, home) == before
    assert sorted({t["source"] for p in L.transcripts(uh, home) for t in L.extract(p)}) == srcs
    assert all(str(p).startswith(str(home)) for p in L.transcripts(uh, home))


def test_build_union_same_dataset(tmp_path):
    uh, tel, home = _homes(tmp_path)
    kw = dict(user_home=uh, telemetry=tel, embed_fn=None)
    m0 = S.build(home=home, **kw)
    steps0 = _read(home / "worldmodel" / "steps.jsonl")
    TM.snapshot(home=home, user_home=uh)
    m1 = S.build(home=home, **kw)                                    # both present
    assert m1["counts"] == m0["counts"]
    shutil.rmtree(uh / ".pi")
    shutil.rmtree(uh / ".claude" / "projects" / "-w-proj" / SID_CL)  # subagents pruned too
    (uh / ".claude" / "projects" / "-w-proj" / f"{SID_CL}.jsonl").unlink()
    m2 = S.build(home=home, **kw)                                    # mirror only
    assert m2["counts"] == m0["counts"]
    assert m2["per_source"] == m0["per_source"]
    key = lambda r: json.dumps([r.get("task"), r.get("agent"), r.get("i")])  # noqa: E731
    assert sorted(map(key, _read(home / "worldmodel" / "steps.jsonl"))) == \
        sorted(map(key, steps0))


# ---- launchd / systemd unit -----------------------------------------------------------------

def test_snapshot_plist_and_systemd_units():
    p = watch._launchd_plist(watch.LABEL_SNAPSHOT, watch._snapshot_args(), keepalive=False,
                             calendar=watch.SNAPSHOT_AT)
    assert "<key>RunAtLoad</key><false/>" in p and "KeepAlive" not in p
    assert "<integer>3</integer>" in p and "<integer>17</integer>" in p
    assert "<string>worldmodel</string>" in p and "<string>snapshot</string>" in p
    assert "/.apex-router/logs/com.apex-router.snapshot.log" in p
    u = watch._systemd_snapshot_units()
    assert "OnCalendar=*-*-* 03:17:00" in u["apex-router-snapshot.timer"]
    assert "worldmodel snapshot" in u["apex-router-snapshot.service"]


def test_install_snapshot_launchd(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(watch.subprocess, "run", lambda a, **k: calls.append(a))
    assert watch._launchd_install_snapshot() == [watch.LABEL_SNAPSHOT]
    plist = tmp_path / "Library" / "LaunchAgents" / "com.apex-router.snapshot.plist"
    assert plist.exists() and (tmp_path / ".apex-router" / "logs").is_dir()
    uid = os.getuid()
    assert ["launchctl", "bootstrap", f"gui/{uid}", str(plist)] in calls
    assert calls[0] == ["launchctl", "bootout", f"gui/{uid}/com.apex-router.snapshot"]
    assert watch._launchd_uninstall_snapshot() == [watch.LABEL_SNAPSHOT]
    assert not plist.exists()
