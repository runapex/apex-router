"""Owner decisions log (owner order 2026-10-27): apex-router decisions add | close | list.

conftest points HOME at a throwaway dir, so ~/.apex-router/decisions.jsonl here is never the real file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apex_router import decisions as dec
from apex_router.cli import main as cli_main


def _rows(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def test_path_is_under_temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert dec.path() == tmp_path / ".apex-router" / "decisions.jsonl"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / ".apex-router" / "decisions.jsonl"


def test_add_list_close(home, capsys):
    assert dec.main(["add", "Ship the gate today?", "--topic", "gates"]) == 0
    did = capsys.readouterr().out.strip()
    assert did
    rows = _rows(home)
    assert rows == [{"id": did, "ts": rows[0]["ts"], "topic": "gates", "text": "Ship the gate today?", "state": "open"}]
    assert isinstance(rows[0]["ts"], (int, float))
    assert dec.main(["list"]) == 0
    out = capsys.readouterr().out
    assert did in out and "Ship the gate today?" in out and "gates" in out
    assert dec.main(["close", did]) == 0
    capsys.readouterr()
    assert dec.main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    # append-only: two rows, the latest wins
    rows = _rows(home)
    assert [r["state"] for r in rows] == ["open", "closed"]
    assert rows[1]["id"] == did and rows[1]["text"] == "Ship the gate today?" and rows[1]["topic"] == "gates"


def test_list_json_shape_and_order(home, capsys):
    ids = []
    for t in ("one", "two", "three"):
        dec.main(["add", t])
        ids.append(capsys.readouterr().out.strip())
    assert len(set(ids)) == 3
    dec.main(["close", ids[1]])
    capsys.readouterr()
    dec.main(["list", "--json"])
    got = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in got] == [ids[0], ids[2]]
    assert set(got[0]) == {"id", "ts", "topic", "text", "state"}
    assert got[0]["topic"] == "general"


def test_latest_row_per_id_wins(home, capsys):
    home.parent.mkdir(parents=True)
    home.write_text("\n".join(json.dumps(r) for r in [
        {"id": "D1", "ts": 1, "topic": "a", "text": "q1", "state": "open"},
        {"id": "D1", "ts": 2, "topic": "a", "text": "q1", "state": "closed"},
        {"id": "D1", "ts": 3, "topic": "a", "text": "q1", "state": "open"},
        {"id": "D2", "ts": 1, "topic": "a", "text": "q2", "state": "open"},
        {"id": "D2", "ts": 2, "topic": "a", "text": "q2", "state": "closed"},
    ]) + "\n")
    assert [r["id"] for r in dec.open_rows()] == ["D1"]


def test_bad_rows_are_skipped_and_counted(home, capsys):
    home.parent.mkdir(parents=True)
    home.write_text("{not json\n" + json.dumps({"id": 5, "state": "open"}) + "\n"
                    + json.dumps({"id": "D3", "ts": 1, "topic": "a", "text": "q", "state": "maybe"}) + "\n"
                    + json.dumps({"id": "D4", "ts": 1, "topic": "a", "text": "q", "state": "open"}) + "\n")
    rows, bad = dec.read_rows()
    assert bad == 3
    assert [r["id"] for r in rows] == ["D4"]
    assert dec.main(["list"]) == 0  # never fatal
    cap = capsys.readouterr()
    assert "D4" in cap.out
    assert "3 bad row" in cap.err


def test_close_unknown_and_twice(home, capsys):
    assert dec.main(["close", "nope"]) == 2
    dec.main(["add", "q"])
    did = capsys.readouterr().out.strip()
    assert dec.main(["close", did]) == 0
    assert dec.main(["close", did]) == 0  # already closed: no new row
    assert len(_rows(home)) == 2


def test_add_rejects_empty_text_and_bad_topic(home, capsys):
    assert dec.main(["add", "   "]) == 2
    assert dec.main(["add", "q", "--topic", "bad topic/../x"]) == 2
    assert not home.exists()


def test_text_is_one_line_and_capped(home, capsys):
    dec.main(["add", "line1\nline2\r\n" + "x" * 5000])
    capsys.readouterr()
    r = _rows(home)[0]
    assert "\n" not in r["text"] and len(r["text"]) <= dec.TEXT_CAP
    assert len(home.read_text().splitlines()) == 1


def test_read_is_capped(home, monkeypatch):
    monkeypatch.setattr(dec, "READ_CAP", 200)
    home.parent.mkdir(parents=True)
    rows = [{"id": f"D{i}", "ts": i, "topic": "a", "text": "q" * 20, "state": "open"} for i in range(50)]
    home.write_text("".join(json.dumps(r) + "\n" for r in rows))
    got, bad = dec.read_rows()
    assert 0 < len(got) < 50
    assert got[-1]["id"] == "D49"
    assert bad == 1  # the cut first line is dropped and the file reported as incomplete (xval: no silent pass)


def test_cli_wiring(home, capsys):
    assert cli_main(["decisions", "add", "via cli", "--topic", "t1"]) == 0
    did = capsys.readouterr().out.strip()
    assert cli_main(["decisions", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == did
    assert cli_main(["decisions", "close", did]) == 0


# ---- xval P1: concurrent adds reused an id; a long history hid open decisions ---------------------

def test_concurrent_adds_get_distinct_ids(monkeypatch, tmp_path):
    import secrets as _s
    from concurrent.futures import ThreadPoolExecutor
    from apex_router import decisions as D
    monkeypatch.setattr(D, "path", lambda: tmp_path / "decisions.jsonl")
    seq = iter(["beefbeef", "beefbeef", "cafecafe"])
    monkeypatch.setattr(D.secrets, "token_hex", lambda n: next(seq))
    with ThreadPoolExecutor(2) as ex:
        ids = list(ex.map(lambda t: D.add(t), ["Ship spawn gates?", "Raise quota?"]))
    assert len(set(ids)) == 2
    assert len(D.open_rows()) == 2


def test_open_decisions_survive_a_long_history(monkeypatch, tmp_path):
    from apex_router import decisions as D
    monkeypatch.setattr(D, "path", lambda: tmp_path / "decisions.jsonl")
    keep = [D.add(f"open question {i}") for i in range(10)]
    for i in range(1000):
        d = D.add("x" * 500)
        D.close(d)
    assert (tmp_path / "decisions.jsonl").stat().st_size < D.READ_CAP
    assert {r["id"] for r in D.open_rows()} == set(keep)


def test_a_cut_file_is_reported_not_passed_in_silence(monkeypatch, tmp_path):
    import json
    from apex_router import decisions as D
    p = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(D, "path", lambda: p)
    monkeypatch.setattr(D, "READ_CAP", 200)
    p.write_text("".join(json.dumps({"id": f"D{i}", "state": "open", "text": "q" * 40}) + "\n" for i in range(10)))
    rows, bad = D.read_rows()
    assert bad >= 1


def test_compaction_never_drops_an_open_row_behind_a_large_recent_history(monkeypatch, tmp_path):
    # xval round 2: > 1 MiB of recent closed rows made the capped reader miss older open rows, and the
    # compaction deleted them
    import json
    import time
    from apex_router import decisions as D
    p = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(D, "path", lambda: p)
    now = time.time()
    rows = [{"id": f"OPEN{i}", "ts": now - 10, "topic": "a", "text": "q", "state": "open"} for i in range(5)]
    rows += [{"id": f"C{i}", "ts": now - 5, "topic": "a", "text": "x" * 480, "state": "closed"} for i in range(2500)]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert p.stat().st_size > D.READ_CAP
    D.add("trigger a compaction")
    ids = {r["id"] for r in D.open_rows()}
    assert {f"OPEN{i}" for i in range(5)} <= ids
    assert p.stat().st_size < D.READ_CAP


def test_no_compaction_when_the_file_has_a_bad_row(monkeypatch, tmp_path):
    import json
    from apex_router import decisions as D
    p = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(D, "path", lambda: p)
    monkeypatch.setattr(D, "COMPACT_AT", 100)
    p.write_text(json.dumps({"id": "OPEN1", "ts": 1, "state": "open"}) + "\n{bad\n" + "x" * 200 + "\n")
    before = p.read_text()
    D.add("q")
    assert p.read_text().startswith(before)
