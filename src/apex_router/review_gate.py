"""Review acceptance gate (B1) and reconciled review-loop records (B3). Advisory: nothing blocks a command.

Rows go to ``~/.apex-router/labels/review_loops.jsonl`` (``APEX_HOME`` respected). Append-only.
A row is written once: a record that equals the current state of its slot is skipped.
Privacy: only ids, enums and counts. No brief, answer, finding text or path is stored.

Row kinds:
  review   loop, rev, reviewer, vendor, verdict, code_access, confirmed, rejected, author (+ vendor)
  tests    loop, rev, passed
  regress  loop, finding, test            (a regression test for a confirmed finding)
  post_go  loop, defects                  (defects found after a GO; the latest row is the total)

ACCEPT for the latest revision needs all of:
  1. a GO from a reviewer of a different vendor than the author, not cheap, with code access recorded;
  2. no open NO-GO from any reviewer on that revision;
  3. tests recorded as passed for that revision;
  4. a regression test for every confirmed finding of the earlier revisions.
Else HOLD, with reason codes.

Usage:
  apex-router review-gate record  --loop ID --rev N --reviewer MODEL --verdict GO|NO-GO|GO-WITH-CHANGES
                                  [--author MODEL] [--code-access] [--confirmed K] [--rejected K]
  apex-router review-gate tests   --loop ID --rev N --passed|--failed
  apex-router review-gate regress --loop ID --finding F --test NAME
  apex-router review-gate post-go --loop ID --defects K
  apex-router review-gate check   --loop ID [--json]
  apex-router review-gate report  [--json]
K is a count (3) or a comma list of finding ids (F1,F2).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

SCHEMA = 1
VERDICTS = ("GO", "NO-GO", "GO-WITH-CHANGES")
CHEAP_RE = re.compile(r"haiku|mini|nano|flash|lite|small", re.I)
_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_MODEL = re.compile(r"[A-Za-z0-9._:/-]{1,96}")
_TEST = re.compile(r"[A-Za-z0-9._:/\[\]-]{1,200}")


def out_path() -> Path:
    return Path(os.environ.get("APEX_HOME", Path.home() / ".apex-router")) / "labels" / "review_loops.jsonl"


def vendor_of(model: str | None) -> str:
    """Vendor enum from the model name: anthropic, openai, local, google, deepseek, unknown."""
    m = (model or "").lower()
    if "claude" in m or re.search(r"\b(opus|sonnet|haiku)\b", m):
        return "anthropic"
    if "gpt" in m or "codex" in m or re.search(r"\bo[134](-|$)", m):
        return "openai"
    if "qwen" in m or "ornith" in m or "ollama" in m:
        return "local"
    if "gemini" in m:
        return "google"
    if "deepseek" in m:
        return "deepseek"
    return "unknown"


def is_cheap(model: str | None) -> bool:
    return vendor_of(model) == "local" or bool(CHEAP_RE.search(model or ""))


# ---------- storage ----------

def _read() -> list[dict]:
    rows = []
    try:
        with out_path().open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("kind") in ("review", "tests", "regress", "post_go"):
                    rows.append(r)
    except OSError:
        pass
    return rows


def _slot(r: dict) -> tuple:
    k = r["kind"]
    if k == "review":
        return (k, r.get("loop"), r.get("rev"), r.get("reviewer"))
    if k == "tests":
        return (k, r.get("loop"), r.get("rev"))
    if k == "regress":
        return (k, r.get("loop"), r.get("finding"), r.get("test"))
    return (k, r.get("loop"))


def _same(a: dict, b: dict) -> bool:
    return {k: v for k, v in a.items() if k != "ts"} == {k: v for k, v in b.items() if k != "ts"}


def write_row(row: dict, now: float | None = None) -> bool:
    """Append `row` unless it equals the latest row of its slot. True if written."""
    row = {"schema": SCHEMA, **row}
    last = None
    for r in _read():
        if _slot(r) == _slot(row):
            last = r
    if last is not None and _same(last, row):
        return False
    row["ts"] = int((time.time() if now is None else now) * 1000)
    p = out_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
    return True


def latest(rows: list[dict]) -> list[dict]:
    """The latest row of every slot, in file order."""
    cur: dict[tuple, dict] = {}
    for r in rows:
        cur[_slot(r)] = r
    return list(cur.values())


# ---------- input checks ----------

def _check(pat: re.Pattern, v: str, what: str) -> str:
    if not isinstance(v, str) or not pat.fullmatch(v):
        raise ValueError(f"bad {what}: use only letters, digits and . _ : / - (max length applies)")
    return v


def parse_findings(v: str | None) -> tuple[int, list[str]]:
    """K -> (count, ids). K is an int or a comma list of ids."""
    if v is None:
        return 0, []
    if re.fullmatch(r"\d{1,6}", v):
        return int(v), []
    ids = sorted({_check(_ID, p, "finding id") for p in v.split(",")})
    return len(ids), ids


def _rev(v) -> int:
    n = int(v)
    if n < 1:
        raise ValueError("rev must be 1 or more")
    return n


# ---------- record API ----------

def record_review(loop, rev, reviewer, verdict, *, author=None, code_access=False, confirmed=None, rejected=None) -> bool:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    row = {"kind": "review", "loop": _check(_ID, loop, "loop id"), "rev": _rev(rev),
           "reviewer": _check(_MODEL, reviewer, "reviewer"), "verdict": verdict, "code_access": bool(code_access)}
    row["vendor"] = vendor_of(reviewer)
    if author:
        row["author"] = _check(_MODEL, author, "author")
        row["author_vendor"] = vendor_of(author)
    for name, val in (("confirmed", confirmed), ("rejected", rejected)):
        n, ids = parse_findings(None if val is None else str(val))
        row[name] = n
        if ids:
            row[name + "_ids"] = ids
    return write_row(row)


def record_tests(loop, rev, passed: bool) -> bool:
    return write_row({"kind": "tests", "loop": _check(_ID, loop, "loop id"), "rev": _rev(rev), "passed": bool(passed)})


def record_regress(loop, finding, test) -> bool:
    return write_row({"kind": "regress", "loop": _check(_ID, loop, "loop id"), "finding": _check(_ID, finding, "finding id"),
                      "test": _check(_TEST, test, "test name")})


def record_post_go(loop, defects) -> bool:
    n = int(defects)
    if n < 0:
        raise ValueError("defects must be 0 or more")
    return write_row({"kind": "post_go", "loop": _check(_ID, loop, "loop id"), "defects": n})


# ---------- B1: the gate ----------

def _count(reviews: list[dict], field: str) -> tuple[int, list[str]]:
    """Distinct findings of one revision: the union of ids, plus the largest anonymous count of one reviewer."""
    ids = sorted({i for r in reviews for i in r.get(field + "_ids", [])})
    anon = max([max(0, int(r.get(field) or 0) - len(r.get(field + "_ids", []))) for r in reviews] or [0])
    return len(ids) + anon, ids


def _loop_state(loop: str, rows: list[dict]) -> dict:
    cur = [r for r in latest(rows) if r.get("loop") == loop]
    by = lambda k: [r for r in cur if r["kind"] == k]  # noqa: E731
    return {"reviews": by("review"), "tests": {r["rev"]: r for r in by("tests")}, "regress": by("regress")}


def revisions(st: dict) -> list[int]:
    return sorted({r["rev"] for r in st["reviews"]} | set(st["tests"]))


def evaluate(st: dict, rev: int) -> dict:
    """Gate decision for revision `rev` of one loop state. Pure: no file access."""
    reviews = [r for r in st["reviews"] if r["rev"] == rev]
    reasons: list[str] = []
    if not reviews:
        reasons.append("no_review")
    author_v = next((r["author_vendor"] for r in sorted(reviews, key=lambda r: r["ts"], reverse=True) if r.get("author_vendor")), None)
    if author_v == "unknown":   # an unparsed author vendor cannot prove reviewer independence
        author_v = None
    if reviews and author_v is None:
        reasons.append("author_unknown")
    gos = [r for r in reviews if r["verdict"] == "GO"]
    qualified = []
    skipped = set()
    for r in gos:
        if author_v is None or r["vendor"] == "unknown":
            skipped.add("author_unknown" if author_v is None else "reviewer_vendor_unknown")
        elif r["vendor"] == author_v:
            skipped.add("same_vendor_go_only")
        elif is_cheap(r["reviewer"]):
            skipped.add("cheap_go_only")
        elif not r.get("code_access"):
            skipped.add("no_code_access")
        else:
            qualified.append(r)
    if reviews and not qualified:
        reasons.append("no_go" if not gos else "no_qualified_go")
        reasons.extend(sorted(skipped - set(reasons)))
    if any(r["verdict"] == "NO-GO" for r in reviews):
        reasons.append("open_no_go")
    t = st["tests"].get(rev)
    if t is None:
        reasons.append("tests_missing")
    elif not t["passed"]:
        reasons.append("tests_failed")
    # regression tests for confirmed findings of this and the earlier revisions (a finding confirmed
    # on the latest revision needs its regression test too, or a GO could ship it untested)
    all_ids: set[str] = set()
    anon_need = 0
    for e in sorted({r["rev"] for r in st["reviews"] if r["rev"] <= rev}):
        n, ids = _count([r for r in st["reviews"] if r["rev"] == e], "confirmed")
        all_ids.update(ids)
        anon_need += n - len(ids)
    have = {r["finding"] for r in st["regress"]}
    missing = sorted(all_ids - have)
    anon_short = max(0, anon_need - len(have - all_ids))
    if missing or anon_short:
        reasons.append("regression_missing")
    return {"rev": rev, "decision": "HOLD" if reasons else "ACCEPT", "reasons": reasons,
            "regression_missing_ids": missing, "regression_missing_anon": anon_short}


def check(loop: str) -> dict:
    st = _loop_state(loop, _read())
    revs = revisions(st)
    if not revs:
        return {"loop": loop, "rev": None, "decision": "HOLD", "reasons": ["no_records"],
                "regression_missing_ids": [], "regression_missing_anon": 0}
    return {"loop": loop, **evaluate(st, revs[-1])}


# ---------- B3: report ----------

def report() -> dict:
    rows = latest(_read())
    loops = sorted({r["loop"] for r in rows if r.get("loop")})
    out_loops = []
    rv: dict[str, dict] = {}
    for loop in loops:
        st = _loop_state(loop, rows)
        revs = revisions(st)
        first = next((r for r in revs if evaluate(st, r)["decision"] == "ACCEPT"), None)
        conf = rej = 0
        for r in revs:
            rr = [x for x in st["reviews"] if x["rev"] == r]
            conf += _count(rr, "confirmed")[0]
            rej += _count(rr, "rejected")[0]
        pg = next((r["defects"] for r in rows if r["kind"] == "post_go" and r["loop"] == loop), None)
        out_loops.append({"loop": loop, "revisions": len(revs), "revisions_to_accept": first, "accepted": first is not None,
                          "latest_decision": evaluate(st, revs[-1])["decision"] if revs else "HOLD",
                          "confirmed": conf, "rejected": rej, "post_go_defects": pg})
        for r in st["reviews"]:
            s = rv.setdefault(r["reviewer"], {"reviews": 0, "go": 0, "confirmed": 0, "rejected": 0})
            s["reviews"] += 1
            s["go"] += r["verdict"] == "GO"
            s["confirmed"] += int(r.get("confirmed") or 0)
            s["rejected"] += int(r.get("rejected") or 0)
    reviewers = []
    for m, s in sorted(rv.items()):
        judged = s["confirmed"] + s["rejected"]
        reviewers.append({"reviewer": m, "vendor": vendor_of(m), **s, "go_rate": round(s["go"] / s["reviews"], 3),
                          "precision": round(s["confirmed"] / judged, 3) if judged else None})
    return {"loops": out_loops, "reviewers": reviewers,
            "note": "precision = confirmed / (confirmed + rejected) findings of that reviewer; None if no finding was judged."}


def _print_report(rep: dict) -> None:
    print(f"{'loop':<24} {'revs':>4} {'to ACCEPT':>9} {'now':>6} {'confirmed':>9} {'rejected':>8} {'post-GO':>7}")
    for l in rep["loops"]:
        pg = "-" if l["post_go_defects"] is None else l["post_go_defects"]
        acc = "-" if l["revisions_to_accept"] is None else l["revisions_to_accept"]
        print(f"{l['loop'][:24]:<24} {l['revisions']:>4} {acc:>9} {l['latest_decision']:>6} {l['confirmed']:>9} {l['rejected']:>8} {pg:>7}")
    print(f"\n{'reviewer':<30} {'vendor':<9} {'n':>3} {'GO rate':>8} {'precision':>9}")
    for r in rep["reviewers"]:
        p = "-" if r["precision"] is None else f"{r['precision']:.2f}"
        print(f"{r['reviewer'][:30]:<30} {r['vendor']:<9} {r['reviews']:>3} {r['go_rate']:>8.2f} {p:>9}")
    print(rep["note"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="apex-router review-gate", description="advisory review acceptance gate + review-loop records (no text)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--loop", required=True)
    r.add_argument("--rev", type=int, required=True)
    r.add_argument("--reviewer", required=True)
    r.add_argument("--verdict", required=True, choices=VERDICTS)
    r.add_argument("--author", default=None, help="model that wrote this revision (sets the author vendor)")
    r.add_argument("--code-access", action="store_true", help="the reviewer read the code")
    r.add_argument("--confirmed", default=None, help="count or comma list of finding ids")
    r.add_argument("--rejected", default=None, help="count or comma list of finding ids")
    t = sub.add_parser("tests")
    t.add_argument("--loop", required=True)
    t.add_argument("--rev", type=int, required=True)
    g = t.add_mutually_exclusive_group(required=True)
    g.add_argument("--passed", action="store_true")
    g.add_argument("--failed", action="store_true")
    x = sub.add_parser("regress")
    x.add_argument("--loop", required=True)
    x.add_argument("--finding", required=True)
    x.add_argument("--test", required=True)
    p = sub.add_parser("post-go")
    p.add_argument("--loop", required=True)
    p.add_argument("--defects", type=int, required=True)
    c = sub.add_parser("check")
    c.add_argument("--loop", required=True)
    c.add_argument("--json", action="store_true")
    q = sub.add_parser("report")
    q.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "check":
            res = check(a.loop)
            if a.json:
                print(json.dumps(res))
            else:
                why = ("; ".join(res["reasons"])) or "all checks pass"
                print(f"{res['decision']} loop={res['loop']} rev={res['rev']}: {why}")
                if res["regression_missing_ids"]:
                    print("regression test missing for: " + ", ".join(res["regression_missing_ids"]))
                if res["regression_missing_anon"]:
                    print(f"regression tests missing for {res['regression_missing_anon']} unnamed finding(s)")
            return 0
        if a.cmd == "report":
            rep = report()
            print(json.dumps(rep)) if a.json else _print_report(rep)
            return 0
        if a.cmd == "record":
            new = record_review(a.loop, a.rev, a.reviewer, a.verdict, author=a.author, code_access=a.code_access,
                                confirmed=a.confirmed, rejected=a.rejected)
        elif a.cmd == "tests":
            new = record_tests(a.loop, a.rev, a.passed)
        elif a.cmd == "regress":
            new = record_regress(a.loop, a.finding, a.test)
        else:
            new = record_post_go(a.loop, a.defects)
    except ValueError as e:
        print(f"review-gate: {e}", file=sys.stderr)
        return 2
    print("recorded" if new else "unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
