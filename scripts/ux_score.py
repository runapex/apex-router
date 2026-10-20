#!/usr/bin/env python3
"""Frozen readability score for the datapce band, status entry and /apex pane (UX RSI loop).

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


def render(plugin_dir: Path) -> list[dict]:
    out = subprocess.run(["claude", "plugin", "test", "."], cwd=plugin_dir, capture_output=True, text=True, timeout=600)
    text = out.stdout + out.stderr
    rows = [json.loads(line.split("UXJSON ", 1)[1]) for line in text.splitlines() if "UXJSON " in line]
    m = re.search(r"(\d+) fail", text)
    fails = int(m.group(1)) if m else -1
    return rows, fails


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
    rows, fails = render(plugin_dir)
    if not rows:
        print("no UXJSON rows: the scenario test did not run", file=sys.stderr)
        return 2
    res = score(rows)
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
