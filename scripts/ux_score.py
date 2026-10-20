#!/usr/bin/env python3
"""Frozen readability score for the datapce band, status entry and /apex pane (UX RSI loop).

v2 (2026-10-20): adds invariant checks over plugin/tests/ux_invariants.test.tsx (fixed edge cases +
a seeded fuzz set). Each check is one defect type the round-1..3 judges found by hand. A critical
failure (cut action, width break, contradicting advice) caps the composite at 60.

Runs `claude plugin test` in a plugin folder, reads the `UXJSON` lines that
plugin/tests/ux_scenarios.test.tsx prints, and scores them. The score is deterministic: the same
rendered text always gives the same number, so rounds of the loop can be compared. It is a proxy.
It does not replace a reader; the loop pairs it with judge votes.

Usage:  python3 scripts/ux_score.py [plugin_dir] [--json]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

#: Terminal width the band must fit without truncation (a common narrow default).
BAND_COLS = 80
#: Width of the docked pane body in the fixture (plugin/tests/fixtures/inputs.ts PANE.bodyColumns).
PANE_COLS = 72
#: Opaque glyphs and terms: a first-time reader cannot decode them without docs.
OPAQUE = ["▲", "↑", "μ", "●", "conf ", "routes ", "tok(all)", "inherit", " arm", "advisory", "breakers",
          "heavy spawns", "P(any slow)", "settling", "burn 5m", "Wilson", "injection", " n "]
#: Words that tell the user to do something (an action hint).
ACTION = re.compile(r"\b(run|start|wait|stop|pause|use|open|switch|compact|handoff|close|slow down|resume|retry)\b|/apex|/compact", re.I)
JARGON_CAP = 40  # opaque hits at which the jargon term reaches 0


def cols(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


SCORER_VERSION = 2
PAUSE, COMPACT = "pause new subagents", "run /compact"
JUNK = ["undefined", "NaN", "null", "unknownx", "Infinity", "[object", ">1e"]
#: Exponent notation in any figure (`1e+21`, `$4.12e3`, `1e-7`).
EXPONENT = re.compile(r"\d(?:\.\d+)?e[+-]?\d")
CONTRA = re.compile(r"at most .*subagents|heavy spawns", re.I)
#: Critical invariants: one failure caps the composite (the judges marked these MUST-FIX).
CRITICAL = ("renders", "action_whole", "now_actions", "status_action", "band_width", "no_contradiction")
#: The invariant fixture's case count (12 edge + 400 fuzz). Fewer rows = the scorer refuses to score.
INV_EXPECTED = 412
INV_CAP = 60.0


def run_tests(plugin_dir: Path) -> str:
    out = subprocess.run(["claude", "plugin", "test", "."], cwd=plugin_dir, capture_output=True, text=True, timeout=900)
    return out.stdout + out.stderr


def render(plugin_dir: Path, text: str | None = None):
    text = run_tests(plugin_dir) if text is None else text
    rows = [json.loads(line.split("UXJSON ", 1)[1]) for line in text.splitlines() if "UXJSON " in line]
    m = re.search(r"(\d+) fail", text)
    fails = int(m.group(1)) if m else -1
    return rows, fails


def inv_cases(text: str) -> list[dict]:
    return [json.loads(line.split("UXINV ", 1)[1]) for line in text.splitlines() if "UXINV " in line]


def inv_expected(text: str) -> int | None:
    for line in text.splitlines():
        if "UXINV_META " in line:
            return json.loads(line.split("UXINV_META ", 1)[1]).get("n")
    return None


def check_case(c: dict) -> dict[str, bool]:
    """One case -> {invariant: passed}. Only the invariants that apply to the case are present."""
    if "error" in c:
        return {"renders": False}
    o, band, status = c["oracle"], c["band"], c["status"]
    secs = c["sections"]
    now = next((s["rows"] for s in secs if s.get("id") == "now"), [])
    allrows = [r for s in secs for r in s["rows"]]
    out: dict[str, bool] = {"renders": True}
    out["band_width"] = cols(band) <= 74
    out["pane_width"] = all(cols(r) <= PANE_COLS for r in allrows)
    texts = [band, status, *allrows]
    # Exponent check: datapce figures only. The Subagents section shows user descriptions (`commit 1e3abc`).
    figures = [band, status, *[r for s in secs if s.get("id") != "dispatches" for r in s["rows"]]]
    out["no_junk"] = not any(j in t for t in texts for j in JUNK) and not any(EXPONENT.search(t) for t in figures)
    if o["first"] is not None:
        # I1: the first action is in the band, whole (not cut by a width guard).
        out["action_whole"] = o["first"] in band
        # I8: surfaces without a band carry it too.
        out["status_action"] = o["first"] in status
        # I11: the pane Now section lists every action the user must take.
        out["now_actions"] = all(any(a in r for r in now) for a in o["actions"])
        # I5: GREEN with an action must not read as all-clear.
        if c["level"] == "GREEN":
            out["green_not_all_clear"] = not band.startswith("apex GREEN ·") and "●GREEN" not in band
        # I4: RED names the quota only when it is >= 80 %.
        if o["firstKind"] == "limit":
            out["cause_matches"] = ("limit" in band.split(PAUSE)[0]) == o["quotaCause"] or (not o["quotaCause"] and "pressure RED" in band)
    else:
        # I2: no action words when the user need not act.
        out["calm_no_action"] = ACTION.search(band) is None and ACTION.search(status) is None
    if PAUSE in o["actions"]:
        # I6: no standing "go ahead" advice next to a pause.
        out["no_contradiction"] = not any(CONTRA.search(r) for r in allrows)
    # I7: every figure is in the band or named in the pane Now section, with its label.
    out["no_hidden_fact"] = all(fact_shown(k, v, [band, *now]) for k, v in c["facts"].items())
    return out


def _unwrap(texts: list[str]) -> list[str]:
    """Join wrapped pane rows back: a continuation row starts with more indent than the row before."""
    out: list[str] = []
    for t in texts:
        if out and len(t) - len(t.lstrip(" ")) > len(out[-1]) - len(out[-1].lstrip(" ")):
            out[-1] = out[-1] + " " + t.strip()
        else:
            out.append(t)
    return out


def _cells(texts: list[str]) -> list[str]:
    # whitespace runs collapse to one space (a label may hold a double space; old bands used two as a separator)
    # Cells split only on the band separator " · ". The one known pane lead-in is removed; any other
    # text before a figure stays in its cell, so a foreign label ("budget: $4.12") never passes.
    out = []
    for t in _unwrap(texts):
        t = re.sub(r"^\s*not in the band \(no room\): ", "", t)
        out += [re.sub(r"\s+", " ", cell).strip() for cell in t.split(" · ") if cell.strip()]
    return out


def fact_shown(kind: str, val, texts: list[str]) -> bool:
    """A figure counts only as a whole cell with its label: `context 61%`, `5h 62%` / `5h limit 95%`,
    `$4.12` (not `$4.12M`), `3 subagents` / `2 of 7 differ from advice`."""
    cells = _cells(texts)
    if kind == "ctx":
        rx = re.compile(rf"context {val}%( full)?")
        return any(rx.fullmatch(c) for c in cells)
    if kind == "limit":
        labels = "|".join(re.escape(re.sub(r"\s+", " ", x)) for x in val["labels"])
        rx = re.compile(rf"(?:{labels})(?: limit)?[ :]{val['pct']}%")
        return any(rx.fullmatch(c) for c in cells)
    if kind == "cost":
        # the spend is the cell's first figure: `$4.12`, `$4.12/$10`; never a denominator or a labelled other sum
        # closed grammar: `$4.12`, `$4.12/$10`, `$4.12/$10, empty ≈18 min`. Any other form fails.
        return any(re.fullmatch(re.escape(tok) + r"(?:/\$(?:999T\+|[0-9]+(?:\.[0-9]+)?[kMBT]?)(?:, empty (?:≈|in about )[0-9]+ min)?)?", c)
                   for tok in val for c in cells)
    if kind == "subagents":
        rx = re.compile(rf"{val} subagents?|\d+ of {val} differ from advice")
        return any(rx.fullmatch(c) for c in cells)
    return False


def score_invariants(cases: list[dict]) -> dict:
    per: dict[str, list[int]] = {}
    fails: dict[str, list[str]] = {}
    for c in cases:
        for k, ok in check_case(c).items():
            per.setdefault(k, [0, 0])
            per[k][0] += ok
            per[k][1] += 1
            if not ok:
                fails.setdefault(k, []).append(c["id"])
    rates = {k: v[0] / v[1] for k, v in per.items()}
    inv_rate = sum(rates.values()) / max(len(rates), 1)
    critical = sorted(k for k in CRITICAL if rates.get(k, 1.0) < 1.0)
    return {
        "inv_rate": round(inv_rate, 4),
        "inv_cases": len(cases),
        "critical_fail": critical,
        "inv": {k: f"{v[0]}/{v[1]}" for k, v in sorted(per.items())},
        "inv_fail_examples": {k: v[:5] for k, v in sorted(fails.items())},
    }


def has_fact(text: str, key: str, val) -> bool:
    low = text.lower()
    if key == "level":
        return str(val).lower() in low
    v = str(val)
    # a number must appear as a whole token (10 must not match 100)
    return re.search(rf"(?<![\d.]){re.escape(v)}(?![\d])", text) is not None


def score(rows: list[dict]) -> dict:
    facts_total = facts_hit = 0
    action_ok = 0
    band_over = 0
    jargon = 0
    pane_rows = pane_trunc = 0
    words = []
    per = {}
    for r in rows:
        band = r["band"]
        fh = [k for k, v in r["facts"].items() if has_fact(band, k, v)]
        facts_total += len(r["facts"])
        facts_hit += len(fh)
        hint = ACTION.search(band) is not None
        ok = hint == r["actionNeeded"]
        action_ok += ok
        over = cols(band) > BAND_COLS
        band_over += over
        texts = [band, r["status"]] + [x for s in r["sections"] for x in [s["title"], *s["rows"]]]
        j = sum(t.count(o) for t in texts for o in OPAQUE)
        jargon += j
        for s in r["sections"]:
            for x in s["rows"]:
                pane_rows += 1
                pane_trunc += cols(x) > PANE_COLS
                words.append(len(x.split()))
        per[r["id"]] = {"band_cols": cols(band), "facts_missed": sorted(set(r["facts"]) - set(fh)), "action_ok": ok, "jargon": j}
    n = max(len(rows), 1)
    fact_recall = facts_hit / max(facts_total, 1)
    action_acc = action_ok / n
    band_fit = 1 - band_over / n
    jargon_term = 1 - min(jargon / JARGON_CAP, 1.0)
    pane_fit = 1 - pane_trunc / max(pane_rows, 1)
    composite = 100 * (0.30 * fact_recall + 0.25 * action_acc + 0.15 * band_fit + 0.15 * jargon_term + 0.15 * pane_fit)
    return {
        "composite": round(composite, 1),
        "fact_recall": round(fact_recall, 3),
        "action_acc": round(action_acc, 3),
        "band_fit": round(band_fit, 3),
        "jargon_hits": jargon,
        "pane_trunc": f"{pane_trunc}/{pane_rows}",
        "pane_words_mean": round(sum(words) / max(len(words), 1), 1),
        "scenarios": per,
    }


def main(argv: list[str]) -> int:
    plugin_dir = Path(next((a for a in argv if not a.startswith("--")), "plugin")).resolve()
    text = run_tests(plugin_dir)
    rows, fails = render(plugin_dir, text)
    if not rows:
        print("no UXJSON rows: the scenario test did not run", file=sys.stderr)
        return 2
    res = score(rows)
    res["composite_v1"] = res.pop("composite")
    cases = inv_cases(text)
    meta_n = inv_expected(text)
    if meta_n != INV_EXPECTED or len(cases) != INV_EXPECTED:
        print(f"invariant fixture incomplete: meta {meta_n}, rows {len(cases)}, expected {INV_EXPECTED}", file=sys.stderr)
        print(json.dumps({"scorer": SCORER_VERSION, "composite": 0.0, "error": "invariant fixture incomplete"}))
        return 2
    inv = score_invariants(cases)
    res.update(inv)
    comp = 0.4 * res["composite_v1"] + 60 * inv["inv_rate"]
    if inv["critical_fail"]:
        comp = min(comp, INV_CAP)
    res = {"scorer": SCORER_VERSION, "composite": round(comp, 1), **res}
    res["test_fails"] = fails
    if "--json" in argv:
        print(json.dumps(res))
    else:
        print(json.dumps(res, indent=2))
        for r in rows:
            print(f"{r['id']:>15} | {r['band']}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
