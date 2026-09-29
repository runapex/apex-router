#!/usr/bin/env python3
"""Multi-model change classifier — measure where a change is risky, per commit/loop.

Given a code CHANGE (a diff) + a REGRESSION signal (test/CI output) + the
REQUIREMENTS it is meant to satisfy, ask a PANEL of independent models to classify
the change on three axes — change class, requirement fit, blast-radius damage —
using one fixed JSON schema, then report each model's classification AND their
divergence.

The product is the DYNAMICS: where independent models AGREE you can trust the read;
where they DISAGREE is exactly where to look. This is a measurement tool, not a
gate — it never blocks a change, it tells you where the risk is. Pairs with a
develop → test → cross-validate loop: run it on each change, read the divergence,
inspect the axis the panel splits on.

## The panel is config-driven (no vendor lock-in)

A panel member is just a NAME and an argv command that reads the classification
prompt on STDIN and prints the model's answer (containing one JSON object) on
STDOUT. Any CLI that satisfies that is a valid member: a hosted-model CLI, a local
OpenAI-compatible server behind a tiny wrapper, or a shell function. Point the tool
at a panel file (or set CLASSIFIER_PANEL):

    [
      {"name": "reviewer-a", "cmd": ["your-model-cli", "--model", "<id-a>", "--stdin"]},
      {"name": "reviewer-b", "cmd": ["your-model-cli", "--model", "<id-b>", "--stdin"]}
    ]

For genuine signal, span DIFFERENT model families/vendors — two members of the same
family share blind spots, so their agreement is weaker evidence. Two-to-three
members is the sweet spot: enough to see divergence, cheap enough to run every
change. With no panel configured the tool prints an example and exits — it never
hardcodes a model id.

  change_classifier.py --repo . --tests t.log --reqs spec.md --panel panel.json
  change_classifier.py --diff change.diff --reqs spec.md --panel panel.json --out r.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────────────────────
# Classification schema — one fixed contract so every model's answer is
# directly comparable and divergence is measurable.
# ─────────────────────────────────────────────────────────────────────────────

# Advisory enums. A model may add a value (that's itself signal — "one model used
# a class the others didn't"); the harness normalizes and still compares.
CHANGE_CLASSES = [
    "schema-migration",   # DB migration / schema / table
    "data-model",         # model/class, associations, (de)serialization, copy/equality
    "compute-core",       # the load-bearing logic / engine / hot path
    "validation",         # input validation, guards, invariants
    "api-surface",        # API entities, request/response schemas, input mapping
    "search-index",       # search/query/index/analysis code, deletion-safety
    "config-gating",      # feature flags, version/capability gates
    "test",               # test files only
    "docs",               # docs / comments only
    "mixed",              # spans several of the above
]

REQ_FIT_VERDICTS = [
    "advances",      # moves a specific requirement forward, correctly
    "partial",       # advances a requirement but incomplete / leaves a gap
    "off-target",    # touches the area but not tied to any stated requirement
    "regresses-req", # breaks/contradicts a requirement or shipped behavior
    "neutral",       # scaffolding/test/docs with no direct requirement effect
]

BLAST_LEVELS = ["none", "low", "medium", "high", "critical"]

OUTPUT_CONTRACT = {
    "change_class": "one of CHANGE_CLASSES (or your own; say why in notes)",
    "change_summary": "<=25 words: what this diff actually does",
    "requirement_fit": {
        "verdict": "one of REQ_FIT_VERDICTS",
        "acs_touched": ["e.g. 'R1', 'AC#4', 'invariant-X'"],
        "confidence": "0.0-1.0",
        "why": "<=30 words citing the diff",
    },
    "blast_radius": {
        "level": "one of BLAST_LEVELS",
        "surfaces_at_risk": ["e.g. 'existing callers', 'persistence', 'shared path'"],
        "regression_signal": "confirmed | refuted | not-covered  (per the test output)",
        "confidence": "0.0-1.0",
        "why": "<=30 words; cite a test line if regression_signal != not-covered",
    },
    "top_concern": "<=20 words: the single most important worry, or 'none'",
    "notes": "<=25 words optional",
}

REQUIRED_TOP_KEYS = ["change_class", "change_summary", "requirement_fit",
                     "blast_radius", "top_concern"]

EXAMPLE_PANEL = [
    {"name": "reviewer-a", "cmd": ["your-model-cli", "--model", "<model-a>", "--stdin"]},
    {"name": "reviewer-b", "cmd": ["your-model-cli", "--model", "<model-b>", "--stdin"]},
]


def contract_text() -> str:
    return (
        "CHANGE_CLASSES = " + ", ".join(CHANGE_CLASSES) + "\n"
        "REQ_FIT_VERDICTS = " + ", ".join(REQ_FIT_VERDICTS) + "\n"
        "BLAST_LEVELS = " + ", ".join(BLAST_LEVELS) + "\n\n"
        "Return ONLY one JSON object, no prose, matching exactly:\n"
        + json.dumps(OUTPUT_CONTRACT, indent=2)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Input assembly
# ─────────────────────────────────────────────────────────────────────────────

def read_maybe_stdin(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path) as f:
        return f.read()


def git_diff(repo: str, base: str) -> str:
    """Uncommitted change vs `base`, plus untracked files rendered as additions."""
    out = subprocess.run(["git", "-C", repo, "diff", base],
                         capture_output=True, text=True)
    extra = subprocess.run(["git", "-C", repo, "ls-files", "--others",
                            "--exclude-standard"], capture_output=True, text=True)
    body = out.stdout
    for fn in filter(None, extra.stdout.splitlines()):
        p = os.path.join(repo, fn)
        try:
            with open(p) as f:
                c = f.read()
            body += f"\n--- /dev/null\n+++ b/{fn} (untracked)\n" + \
                    "".join("+" + ln + "\n" for ln in c.splitlines())
        except Exception:
            pass
    return body


def build_prompt(diff: str, tests: str, reqs: str, cap: int = 22000) -> str:
    def clip(s, n):
        return s if len(s) <= n else s[:n] + f"\n...[clipped {len(s)-n} chars]"
    return (
        "You classify ONE code change from a development loop. Be a skeptical\n"
        "reviewer: judge REQUIREMENT FIT against the stated requirements and BLAST\n"
        "RADIUS (regression risk) grounded in the TEST/CI OUTPUT. Do not assume a\n"
        "test passed unless the output says so.\n\n"
        "=== REQUIREMENTS (the yardstick) ===\n" + clip(reqs, 9000) + "\n\n"
        "=== CODE CHANGE (diff) ===\n" + clip(diff, cap) + "\n\n"
        "=== REGRESSION / TEST OUTPUT ===\n"
        + clip(tests or "(no test output provided)", 6000)
        + "\n\n=== YOUR TASK ===\n" + contract_text()
    )


# ─────────────────────────────────────────────────────────────────────────────
# JSON extraction — pick the object matching the contract, not a trailing
# sub-object. Models often emit prose, a nested field, then the real answer.
# ─────────────────────────────────────────────────────────────────────────────

def extract_json(text: str) -> dict | None:
    depth, start, cands = 0, None, []
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    cands.append(json.loads(text[start:i + 1]))
                except Exception:
                    pass
    if not cands:
        return None
    full = [c for c in cands if isinstance(c, dict)
            and all(k in c for k in REQUIRED_TOP_KEYS)]
    if full:
        return max(full, key=lambda c: len(json.dumps(c)))
    partial = [c for c in cands if isinstance(c, dict)
               and any(k in c for k in ("change_class", "requirement_fit", "blast_radius"))
               and len(c) >= 3]
    if partial:
        return max(partial, key=lambda c: len(json.dumps(c)))
    return max(cands, key=lambda c: len(json.dumps(c)))


# ─────────────────────────────────────────────────────────────────────────────
# Panel member runner
# ─────────────────────────────────────────────────────────────────────────────

def run_member(name: str, cmd: list[str], prompt: str, timeout: int = 300) -> dict:
    """Feed the prompt on stdin to one panel member; parse the JSON it prints."""
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True,
                              text=True, timeout=timeout)
        raw = (proc.stdout or "") + "\n" + (proc.stderr or "")
        js = extract_json(raw)
        return {"name": name, "ok": js is not None, "json": js,
                "raw_tail": None if js else raw[-500:],
                "secs": round(time.time() - t0, 1)}
    except FileNotFoundError:
        return {"name": name, "ok": False, "json": None,
                "raw_tail": f"command not found: {cmd[0]}", "secs": 0.0}
    except subprocess.TimeoutExpired:
        return {"name": name, "ok": False, "json": None,
                "raw_tail": "TIMEOUT", "secs": float(timeout)}


# ─────────────────────────────────────────────────────────────────────────────
# Divergence — the signal
# ─────────────────────────────────────────────────────────────────────────────

def _g(d, *ks):
    for k in ks:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def divergence(results: list[dict]) -> dict:
    ok = {r["name"]: r["json"] for r in results if r.get("ok") and r.get("json")}
    if len(ok) < 2:
        return {"panel_n": len(ok),
                "note": "need >=2 valid classifications for divergence"}

    def collect(path):
        return {m: _g(j, *path) for m, j in ok.items()}

    def agree(d):
        return len(set(str(v).lower() for v in d.values())) == 1

    cls = collect(["change_class"])
    fit = collect(["requirement_fit", "verdict"])
    blast = collect(["blast_radius", "level"])
    reg = collect(["blast_radius", "regression_signal"])

    order = {b: i for i, b in enumerate(BLAST_LEVELS)}
    lv = [order.get(str(v).lower(), -1) for v in blast.values()]
    spread = (max(lv) - min(lv)) if lv and min(lv) >= 0 else None

    flags = []
    if not agree(fit):
        flags.append(f"requirement-fit DISAGREEMENT: {fit}")
    if spread and spread >= 2:
        flags.append(f"blast-radius SPREAD>=2: {blast}")
    if not agree(reg):
        flags.append(f"regression-signal DISAGREEMENT: {reg}")
    if any(str(v).lower() == "regresses-req" for v in fit.values()):
        flags.append("at least one model says the change REGRESSES a requirement")
    if any(str(v).lower() in ("high", "critical") for v in blast.values()):
        flags.append("at least one model rates blast HIGH/CRITICAL")

    return {
        "panel_n": len(ok),
        "change_class": {"agree": agree(cls), "values": cls},
        "requirement_fit": {"agree": agree(fit), "values": fit},
        "blast_level": {"values": blast, "spread": spread},
        "regression_signal": {"agree": agree(reg), "values": reg},
        "flags": flags or ["panel broadly agrees"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

def load_panel(panel_arg: str | None) -> list[dict]:
    path = panel_arg or os.environ.get("CLASSIFIER_PANEL")
    if not path:
        sys.stderr.write(
            "No panel configured. Pass --panel <file> or set CLASSIFIER_PANEL.\n"
            "Each member is {\"name\": ..., \"cmd\": [argv...]}; the cmd reads the\n"
            "prompt on stdin and prints one JSON object. Example:\n"
            + json.dumps(EXAMPLE_PANEL, indent=2) + "\n")
        sys.exit(2)
    with open(path) as f:
        panel = json.load(f)
    if not isinstance(panel, list) or not all(
            isinstance(m, dict) and "name" in m and "cmd" in m for m in panel):
        sys.exit("panel file must be a JSON list of {name, cmd} objects")
    return panel


def classify(diff: str, tests: str, reqs: str, panel: list[dict]) -> dict:
    prompt = build_prompt(diff, tests, reqs)
    with ThreadPoolExecutor(max_workers=max(1, len(panel))) as ex:
        futs = [ex.submit(run_member, m["name"], m["cmd"], prompt) for m in panel]
        results = [f.result() for f in as_completed(futs)]
    return {
        "panel": {r["name"]: {"ok": r["ok"], "secs": r["secs"],
                              "classification": r["json"], "raw_tail": r["raw_tail"]}
                  for r in results},
        "divergence": divergence(results),
        "prompt_chars": len(prompt),
    }


def main():
    ap = argparse.ArgumentParser(description="Multi-model change classifier.")
    ap.add_argument("--diff", help="diff file, or '-' for stdin")
    ap.add_argument("--repo", help="git repo to diff instead of --diff")
    ap.add_argument("--base", default="HEAD", help="base ref for --repo (default HEAD)")
    ap.add_argument("--tests", default="", help="test/CI output file (regression signal)")
    ap.add_argument("--reqs", required=True, help="comma-separated requirement/spec files")
    ap.add_argument("--panel", help="panel JSON file (or set CLASSIFIER_PANEL)")
    ap.add_argument("--out", help="write the full report JSON here")
    a = ap.parse_args()

    if a.repo:
        diff = git_diff(a.repo, a.base)
    elif a.diff:
        diff = read_maybe_stdin(a.diff)
    else:
        sys.exit("need --diff or --repo")
    if not diff.strip():
        sys.exit("empty diff — nothing to classify")

    tests = read_maybe_stdin(a.tests) if a.tests else ""
    reqs = "\n\n".join(read_maybe_stdin(p) for p in a.reqs.split(","))
    panel = load_panel(a.panel)

    rep = classify(diff, tests, reqs, panel)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(rep, f, indent=2)
        print(f"wrote {a.out}")
    print(json.dumps({"divergence": rep["divergence"],
                      "panel_ok": {m: v["ok"] for m, v in rep["panel"].items()}},
                     indent=2))


if __name__ == "__main__":
    main()
