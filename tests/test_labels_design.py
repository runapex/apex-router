"""Milestone 5: probability sample for gold (floor 1), inclusion probability pi on every new gold
row, weighted voter accuracy (report only). No transcripts: strata are injected."""
from __future__ import annotations

import json
import os

import pytest

from apex_router import labels as L


def _home(tmp_path, monkeypatch, n=60):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    h = tmp_path / "labels"
    (h / "gold").mkdir(parents=True)
    rows = [{"id": f"t{i:03d}", "source": "pi", "session": "s", "index": i, "n_calls": 1,
             "n_errors": 0, "has_next": True, "votes": {"tests": "success"}} for i in range(n)]
    (h / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    # 3 strata: A big, B middle, C one task (floor 1 must still sample it)
    strata = {r["id"]: (("p", "A") if i < 45 else ("p", "B") if i < 59 else ("q", "C"))
              for i, r in enumerate(rows)}
    return h, rows, strata


def test_draw_design_floor_one_and_pi(tmp_path, monkeypatch):
    _h, _rows, strata = _home(tmp_path, monkeypatch)
    d = L.draw_design(strata, budget=12, floor=1, seed=3)
    take = {(s["project"], s["task_type"]): s["take"] for s in d["strata"]}
    assert take[("q", "C")] == 1 and sum(take.values()) == 12           # floor 1 reaches C
    assert take[("p", "A")] > take[("p", "B")]                          # the rest is proportional
    assert len(d["drawn"]) == 12 and d["kind"] == "probability" and d["floor"] == 1
    for i, s in d["drawn"].items():
        pop = next(x for x in d["strata"] if (x["project"], x["task_type"]) == tuple(s))
        assert d["pi"][i] == pop["take"] / pop["population"]
    assert d["pi"][next(i for i, s in d["drawn"].items() if tuple(s) == ("q", "C"))] == 1.0
    assert 1.0 <= d["deff"] < 1.5
    assert L.draw_design(strata, budget=12, floor=1, seed=3)["drawn"] == d["drawn"]   # seeded
    with pytest.raises(L.GoldImportError):
        L.draw_design(strata, budget=2, floor=1, seed=3)               # floors exceed the budget


def test_design_file_is_private_and_holds_no_text(tmp_path, monkeypatch):
    h, _rows, strata = _home(tmp_path, monkeypatch)
    d = L.draw_design(strata, budget=12, floor=1, seed=3)
    p = L.save_design(d)
    assert p.parent == h / "gold" / "designs" and oct(p.stat().st_mode & 0o777) == "0o600"
    doc = json.loads(p.read_text())
    assert set(doc) <= {"id", "kind", "created", "seed", "floor", "budget", "frame_size", "strata",
                        "drawn", "pi", "deff", "pi_kind", "pools"}
    assert L.load_designs()[0]["id"] == d["id"]


def test_import_gold_attaches_design_pi_and_ignores_reviewer_values(tmp_path, monkeypatch):
    _h, _rows, strata = _home(tmp_path, monkeypatch)
    d = L.draw_design(strata, budget=12, floor=1, seed=3)
    L.save_design(d)
    tid = next(iter(d["drawn"]))
    other = next(i for i in strata if i not in d["drawn"])
    f = tmp_path / "in.jsonl"
    f.write_text(json.dumps({"id": tid, "outcome": "success", "pi": 0.999, "design": "fake"}) + "\n"
                 + json.dumps({"id": other, "outcome": "fail"}) + "\n")
    L.import_gold(f, by="tester")
    g = L.gold_latest()
    assert g[tid]["design"] == d["id"] and g[tid]["pi"] == d["pi"][tid]
    assert g[tid]["stratum"] == list(d["drawn"][tid])
    assert "pi" not in g[other] and "design" not in g[other]            # hand-picked: no pi


def test_gold_design_uses_newest_covered_probability_design(tmp_path, monkeypatch):
    _h, _rows, strata = _home(tmp_path, monkeypatch)
    d = L.draw_design(strata, budget=12, floor=1, seed=3)
    L.save_design(d)
    ids = list(d["drawn"])
    gold = {i: "success" for i in ids[:-1]}
    gold["extra"] = "fail"                                              # not in the design: no weight
    lastc = ids[-1]
    if strata[lastc] == ("q", "C"):                                     # stratum C uncovered -> None
        assert L.gold_design(gold) is None
    gold = {i: "success" for i in ids} | {"extra": "fail"}
    w = L.gold_design(gold)
    assert "extra" not in w and set(w) == set(ids)
    pop = {(s["project"], s["task_type"]): s["population"] for s in d["strata"]}
    for i in ids:
        h_ = tuple(d["drawn"][i])
        r_h = sum(1 for j in ids if tuple(d["drawn"][j]) == h_)
        assert w[i] == pop[h_] / r_h
    assert L.gold_design_id(gold) == d["id"]


def test_purposive_export_records_conditional_pi(tmp_path, monkeypatch):
    _h, rows, _strata = _home(tmp_path, monkeypatch)
    for r in rows[:10]:
        r["votes"] = {"tests": "fail"}                                  # suspected-fail pool of 10
    pis: dict = {}
    got = L.sample_strata(rows, [], {}, [("suspected-fail", 4), ("random", 5)], set(), seed=1, pis=pis)
    sf = [r["id"] for r, s in got if s == "suspected-fail"]
    rnd = [r["id"] for r, s in got if s == "random"]
    assert all(pis[i] == 4 / 10 for i in sf)
    assert all(pis[i] == 5 / 56 for i in rnd)                           # 60 - 4 taken
    # test-split draws by priority: a fully taken rank has pi 1.0, the boundary rank n/N
    keys = {L.wm_task_key(r) for r in rows[20:30]}
    pis2: dict = {}
    got = L.sample_strata(rows, [], {}, [("test-split", 3)], keys, seed=1, pis=pis2)
    assert len(got) == 3 and all(abs(pis2[r["id"]] - 3 / 10) < 1e-12 for r, _ in got)


def test_weighted_voter_accuracy_report_only(tmp_path, monkeypatch):
    rows = [{"id": f"t{i}", "votes": {"tests": "success" if i % 2 else "fail"}} for i in range(4)]
    gold = {f"t{i}": "success" for i in range(4)}
    w = {"t0": 9.0, "t1": 1.0, "t2": 9.0, "t3": 1.0}                    # wrong rows weigh 9x
    out = L.voter_accuracy_weighted(rows, gold, w)
    assert abs(out["tests"]["accuracy"] - 0.1) < 1e-12
    assert out["tests"]["n"] == 4 and out["tests"]["n_eff"] < 4
    assert L.voter_accuracy(rows, gold)["tests"] == (2, 4)             # unweighted path unchanged


def test_export_review_design_writes_design_and_rows(tmp_path, monkeypatch):
    h, _rows, strata = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(L, "task_strata", lambda rows, by_session=None: strata)
    monkeypatch.setattr(L, "_session_paths", dict)
    monkeypatch.setattr(L, "_load_task", lambda r, by: {"calls": [], "request": "x", "last": "y",
                                                        "next": None})
    monkeypatch.setattr(L, "votes", lambda t: {})
    monkeypatch.setattr(L, "signals", lambda t: {})
    monkeypatch.setattr(L, "_tool_summary", lambda t: {})
    (h / "gold.jsonl").write_text("")
    out = tmp_path / "rev.jsonl"
    n = L.export_review(0, out, seed=3, design=(12, 1))
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    (d,) = L.load_designs()
    assert n == len(lines) == 12
    for x in lines:
        assert x["design"] == d["id"] and x["pi"] == d["pi"][x["id"]]
        assert x["stratum"] == list(d["drawn"][x["id"]])
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"


def test_newest_design_gives_pi_and_purposive_never_weights(tmp_path, monkeypatch):
    _h, _rows, strata = _home(tmp_path, monkeypatch)
    old = L.draw_design(strata, budget=60, floor=1, seed=3)             # every task drawn, pi 1.0
    old["created"] = 1.0
    old["id"] = "old"
    L.save_design(old)
    new = L.draw_design(strata, budget=12, floor=1, seed=4)
    new["created"] = 2.0
    new["id"] = "new"
    L.save_design(new)
    rows = L._design_rows()
    i = next(iter(new["drawn"]))
    assert rows[i]["design"] == "new" and rows[i]["pi"] == new["pi"][i] < 1.0
    gold = {i: "success" for i in new["drawn"]}
    assert L.gold_design_id(gold) == "new"
    # a purposive design (newer still) never gives weights, even when it covers every id
    pur = {**new, "id": "pur", "kind": "purposive", "created": 3.0}    # complete strata, but purposive
    L.save_design(pur)
    assert L.gold_design_id(gold) == "new"
    assert L._design_rows()[i]["design"] == "pur"                       # but pi is recorded


def test_test_split_pi_is_per_priority_rank(tmp_path, monkeypatch):
    _h, rows, _strata = _home(tmp_path, monkeypatch)
    for r in rows[20:24]:
        r["votes"] = {"tests": "fail"}                                  # rank 1 (rule fail): 4 rows
    labels = [{"id": r["id"], "label": "success"} for r in rows]       # rank 3 for the rest
    keys = {L.wm_task_key(r) for r in rows[20:30]}                      # 4 at rank 1, 6 at rank 3
    pis: dict = {}
    got = L.sample_strata(rows, labels, {}, [("test-split", 6)], keys, seed=1, pis=pis)
    p1 = [pis[r["id"]] for r, _ in got if r["votes"].get("tests") == "fail"]
    p3 = [pis[r["id"]] for r, _ in got if r["votes"].get("tests") != "fail"]
    assert p1 == [1.0] * 4 and p3 == [2 / 6] * 2                        # rank taken whole / boundary


def test_design_export_skips_tasks_that_already_have_gold(tmp_path, monkeypatch):
    h, _rows, strata = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(L, "task_strata", lambda rows, by_session=None: strata)
    monkeypatch.setattr(L, "_session_paths", dict)
    monkeypatch.setattr(L, "_load_task", lambda r, by: {"calls": [], "request": "x", "last": "y",
                                                        "next": None})
    monkeypatch.setattr(L, "votes", lambda t: {})
    monkeypatch.setattr(L, "signals", lambda t: {})
    monkeypatch.setattr(L, "_tool_summary", lambda t: {})
    d0 = L.draw_design(strata, budget=12, floor=1, seed=3)              # same seed as the export
    gold_ids = list(d0["drawn"])[:5]
    (h / "gold.jsonl").write_text("".join(json.dumps({"id": i, "outcome": "success", "ts": 1.0,
                                                      "by": "t"}) + "\n" for i in gold_ids))
    out = tmp_path / "rev.jsonl"
    n = L.export_review(0, out, seed=3, design=(12, 1))
    got = {json.loads(x)["id"] for x in out.read_text().splitlines()}
    assert n == 7 and not (got & set(gold_ids))                         # drawn, reused, not exported
    assert set(gold_ids) <= set(L.load_designs()[0]["drawn"])           # but still in the design


def test_design_ids_never_collide_and_a_design_is_never_replaced(tmp_path, monkeypatch):
    _h, _rows, strata = _home(tmp_path, monkeypatch)
    a = L.draw_design(strata, budget=12, floor=1, seed=3)
    b = L.draw_design(strata, budget=12, floor=1, seed=3)              # same second, same seed
    assert a["id"] != b["id"]
    L.save_design(a)
    with pytest.raises(L.GoldImportError):
        L.save_design({**b, "id": a["id"]})                             # O_EXCL: no overwrite
    assert L.load_designs()[0]["drawn"] == a["drawn"]
    assert not list((tmp_path / "labels" / "gold" / "designs").glob("*.tmp"))


def test_failed_export_stores_no_design(tmp_path, monkeypatch):
    _h, _rows, strata = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(L, "task_strata", lambda rows, by_session=None: strata)
    monkeypatch.setattr(L, "_session_paths", dict)
    monkeypatch.setattr(L, "_load_task", lambda r, by: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        L.export_review(0, tmp_path / "rev.jsonl", seed=3, design=(12, 1))
    assert [d["id"] for d in L.load_designs()] == []
    # a save failure removes the review file: no review rows without their design
    monkeypatch.setattr(L, "_load_task", lambda r, by: {"calls": [], "request": "x", "last": "y",
                                                        "next": None})
    for f in ("votes", "signals", "_tool_summary"):
        monkeypatch.setattr(L, f, lambda t: {})
    monkeypatch.setattr(L, "save_design", lambda d: (_ for _ in ()).throw(L.GoldImportError("x")))
    with pytest.raises(L.GoldImportError):
        L.export_review(0, tmp_path / "rev2.jsonl", seed=3, design=(12, 1))
    assert not (tmp_path / "rev2.jsonl").exists()


@pytest.mark.parametrize("bad", [
    {"created": "bad"}, {"created": float("inf")}, {"created": 10 ** 400}, {"id": "../x"}, {"kind": "other"},
    {"pi": {"t000": 2.0}}, {"drawn": {"t000": "p|A"}}, {"strata": "x"}])
def test_malformed_design_file_is_skipped_not_fatal(tmp_path, monkeypatch, bad):
    _h, _rows, strata = _home(tmp_path, monkeypatch)
    good = L.draw_design(strata, budget=12, floor=1, seed=3)
    L.save_design(good)
    dd = tmp_path / "labels" / "gold" / "designs"
    (dd / "bad.json").write_text(json.dumps({**good, "id": "bad", **bad}, allow_nan=True))
    assert [d["id"] for d in L.load_designs()] == [good["id"]]
    assert L.gold_design({i: "success" for i in good["drawn"]})        # still works


def test_save_design_without_hard_links(tmp_path, monkeypatch):
    _h, _rows, strata = _home(tmp_path, monkeypatch)
    d = L.draw_design(strata, budget=12, floor=1, seed=3)

    def no_link(a, b):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(L.os, "link", no_link)
    p = L.save_design(d)
    assert oct(p.stat().st_mode & 0o777) == "0o600" and L.load_designs()[0]["id"] == d["id"]
    with pytest.raises(L.GoldImportError):
        L.save_design(d)                                                # still never replaced
    assert not list(p.parent.glob("*.tmp")) and not list(p.parent.glob(".*.tmp"))


def test_design_export_reuses_consensus_gold_too(tmp_path, monkeypatch):
    # live bug: gold in gold/consensus.csv (not gold.jsonl) was exported again for review
    h, _rows, strata = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(L, "task_strata", lambda rows, by_session=None: strata)
    monkeypatch.setattr(L, "_session_paths", dict)
    monkeypatch.setattr(L, "_load_task", lambda r, by: {"calls": [], "request": "x", "last": "y",
                                                        "next": None})
    for f in ("votes", "signals", "_tool_summary"):
        monkeypatch.setattr(L, f, lambda t: {})
    d0 = L.draw_design(strata, budget=12, floor=1, seed=3)
    cons = list(d0["drawn"])[:4]
    (h / "gold" / "consensus.csv").write_text(
        "task_id,outcome\n" + "".join(f"{i},success\n" for i in cons))
    out = tmp_path / "rev.jsonl"
    n = L.export_review(0, out, seed=3, design=(12, 1))
    got = {json.loads(x)["id"] for x in out.read_text().splitlines()}
    assert n == 8 and not (got & set(cons))


def test_strata_and_default_export_skip_consensus_gold(tmp_path, monkeypatch):
    h, rows, _strata = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(L, "_session_paths", dict)
    monkeypatch.setattr(L, "_load_task", lambda r, by: {"calls": [], "request": "x", "last": "y",
                                                        "next": None})
    for f in ("votes", "signals", "_tool_summary"):
        monkeypatch.setattr(L, f, lambda t: {})
    cons = [r["id"] for r in rows[:50]]                                 # 50 of 60 have consensus gold
    (h / "gold" / "consensus.csv").write_text(
        "task_id,outcome\n" + "".join(f"{i},success\n" for i in cons))
    out = tmp_path / "a.jsonl"
    L.export_review(0, out, seed=3, strata=[("random", 30)], test_keys=set())
    got = {json.loads(x)["id"] for x in out.read_text().splitlines()}
    assert len(got) == 10 and not (got & set(cons))
    L.export_review(30, out, seed=3)
    got = {json.loads(x)["id"] for x in out.read_text().splitlines()}
    assert got and not (got & set(cons))


def test_reviewed_unknown_and_consensus_are_never_sampled_again(tmp_path, monkeypatch):
    h, rows, strata = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(L, "task_strata", lambda rows, by_session=None: strata)
    monkeypatch.setattr(L, "_session_paths", dict)
    monkeypatch.setattr(L, "_load_task", lambda r, by: {"calls": [], "request": "x", "last": "y",
                                                        "next": None})
    for f in ("votes", "signals", "_tool_summary"):
        monkeypatch.setattr(L, f, lambda t: {})
    unk = [r["id"] for r in rows[:40]]                                  # reviewed: "unknown"
    (h / "gold.jsonl").write_text("".join(json.dumps({"id": i, "outcome": "unknown", "ts": 1.0,
                                                      "by": "t"}) + "\n" for i in unk))
    (h / "gold" / "consensus.csv").write_text(
        "task_id,outcome\n" + "".join(f"{r['id']},success\n" for r in rows[40:50]))
    seen = set(unk) | {r["id"] for r in rows[40:50]}
    assert set(L.gold_reviewed(rows)) == seen
    assert set(L.gold_outcomes(rows)) == {r["id"] for r in rows[40:50]}   # measuring: unchanged
    out = tmp_path / "a.jsonl"
    for kw in ({"strata": [("random", 30)], "test_keys": set()}, {"design": (12, 1)}, {}):
        L.export_review(30, out, seed=3, **kw)
        got = {json.loads(x)["id"] for x in out.read_text().splitlines()}
        assert not (got & seen), kw
    # interactive review(): the sampler it calls must get every reviewed id as "gold"
    real = L.sample_for_review
    got_gold = {}

    def spy(rows_, labels_, gold_, k, seed=7):
        got_gold.update(gold_)
        return real(rows_, labels_, gold_, k, seed=seed)

    monkeypatch.setattr(L, "sample_for_review", spy)
    L.review(5, inp=lambda prompt: "q", out=lambda *a: None)
    assert seen <= set(got_gold)


def test_import_gold_refuses_to_replace_consensus_without_override(tmp_path, monkeypatch):
    h, rows, _strata = _home(tmp_path, monkeypatch)
    cid, other = rows[0]["id"], rows[1]["id"]
    (h / "gold" / "consensus.csv").write_text(f"task_id,outcome\n{cid},success\n")
    f = tmp_path / "in.jsonl"
    f.write_text(json.dumps({"id": cid, "outcome": "fail"}) + "\n"
                 + json.dumps({"id": other, "outcome": "fail"}) + "\n")
    with pytest.raises(L.GoldImportError, match="consensus"):
        L.import_gold(f, by="tester")
    assert not (h / "gold.jsonl").exists()                              # all-or-nothing
    assert L.gold_outcomes(rows)[cid] == "success"
    L.import_gold(f, by="owner", override_consensus=True)
    g = L.gold_latest()
    assert g[cid]["outcome"] == "fail" and g[cid]["supersedes_consensus"] == "success"
    assert "supersedes_consensus" not in g[other]
    assert L.gold_outcomes(rows)[cid] == "fail"
    # a row that only repeats the consensus outcome is refused too (nothing to import)
    f.write_text(json.dumps({"id": rows[2]["id"], "outcome": "partial"}) + "\n")
    (h / "gold" / "consensus.csv").write_text(f"task_id,outcome\n{rows[2]['id']},partial\n")
    with pytest.raises(L.GoldImportError, match="consensus"):
        L.import_gold(f, by="tester")


def test_cli_override_consensus_flag(tmp_path, monkeypatch, capsys):
    h, rows, _strata = _home(tmp_path, monkeypatch)
    cid = rows[0]["id"]
    (h / "gold" / "consensus.csv").write_text(f"task_id,outcome\n{cid},success\n")
    f = tmp_path / "in.jsonl"
    f.write_text(json.dumps({"id": cid, "outcome": "fail"}) + "\n")
    assert L.main(["import-gold", str(f), "--by", "t"]) == 2
    assert "--override-consensus" in capsys.readouterr().err
    assert L.main(["import-gold", str(f), "--by", "owner", "--override-consensus"]) == 0
