#!/usr/bin/env python3
"""Audit pi auto-compactions in ~/.pi/agent/sessions: cost, cache use, survival of user rules and ids.

Read only. Prints one JSON object. Heuristics (state them when you cite a number):
- a "user rule" is a user message < 800 chars with do not/don't/never/always/must/stop/only/without;
  it is "matched" if >= 50 % of its first 6 words of >= 6 chars occur in the next summary OR in the
  kept raw messages (from firstKeptEntryId). No match is NOT proof of loss: a paraphrase does not match.
- an "id" is a git sha (7-40 hex), EYE-<n>, a pi job id (14 digits-xxxx) or #<4-6 digits> in a user message.
- cached_prefix_cost_est: the summary call reads the last assistant prefix at 10 % of the input
  price, plus 2000 new input tokens, plus the same output. A model, not a measurement.
Usage: compaction_audit.py [--sessions DIR] [--last N]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics

RULE = re.compile(r"\b(do not|don't|never|always|must|stop|only|without)\b", re.I)
IDS = re.compile(r"\b(?:[0-9a-f]{7,40}|EYE-\d+|\d{14}-[a-z0-9]{4}|\d{8}-\d{6}-[a-z0-9]{4})\b|(?<![\w#])#\d{4,6}\b")


def text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    return " ".join(x.get("text", "") for x in (c or []) if isinstance(x, dict) and x.get("type") == "text")


def audit(files: list[str]) -> dict:
    out = {"sessions": 0, "compactions": 0, "cost": 0.0, "cache_read_tokens": 0, "input_tokens": 0,
           "first": None, "last": None, "input_min": None, "input_max": None, "by_model": {},
           "cached_prefix_cost_est": 0.0, "rules": [0, 0], "ids": [0, 0], "growth": [], "lost_rules": []}
    for f in files:
        try:
            rows = [json.loads(line) for line in open(f, errors="ignore") if line.strip()]
        except (OSError, ValueError):
            continue
        if not any(r.get("type") == "compaction" for r in rows):
            continue
        out["sessions"] += 1
        prev, rules, ids, last, model = None, [], set(), None, None
        by_id = {r.get("id"): i for i, r in enumerate(rows)}
        for idx, r in enumerate(rows):
            if r.get("type") == "model_change":
                model = r.get("modelId") or r.get("model") or model
            m = r.get("message") or {}
            if m.get("role") == "assistant":
                last = m
            if m.get("role") == "user":
                t = text(m)
                if t and len(t) < 800 and RULE.search(t):
                    rules.append(t)
                ids |= set(IDS.findall(t or ""))
            if r.get("type") != "compaction":
                continue
            out["compactions"] += 1
            u = r.get("usage") or {}
            cost = u.get("cost") or {}
            out["cost"] += cost.get("total", 0) or 0
            out["cache_read_tokens"] += u.get("cacheRead", 0) or 0
            out["input_tokens"] += u.get("input", 0) or 0
            if u.get("input") and last:
                rate = (cost.get("input", 0) or 0) / u["input"]
                lu = last.get("usage") or {}
                prefix = sum(lu.get(k, 0) or 0 for k in ("input", "cacheRead", "cacheWrite"))
                out["cached_prefix_cost_est"] += prefix * rate * 0.1 + 2000 * rate + (cost.get("output", 0) or 0)
            ts = r.get("timestamp")
            out["first"] = min(filter(None, [out["first"], ts])) if ts else out["first"]
            out["last"] = max(filter(None, [out["last"], ts])) if ts else out["last"]
            if u.get("input"):
                out["input_min"] = min(filter(None, [out["input_min"], u["input"]]))
                out["input_max"] = max(filter(None, [out["input_max"], u["input"]]))
            out["by_model"][str(model)] = out["by_model"].get(str(model), 0) + 1
            k = by_id.get(r.get("firstKeptEntryId"))
            kept = " ".join(text(x.get("message") or {}) for x in rows[k:idx]) if k is not None else ""
            s = ((r.get("summary") or "") + "\n" + kept).lower()
            for t in rules:
                w = re.findall(r"[a-z0-9_.-]{6,}", t.lower())[:6]
                if not w:
                    continue
                out["rules"][1] += 1
                if sum(x in s for x in w) / len(w) >= 0.5:
                    out["rules"][0] += 1
                elif len(out["lost_rules"]) < 10:
                    out["lost_rules"].append(re.sub(r"\s+", " ", t)[:100])
            out["ids"][1] += len(ids)
            out["ids"][0] += sum(x.lower() in s for x in ids)
            if prev:
                out["growth"].append(round(len(r.get("summary") or "") / len(prev), 2))
            prev, rules, ids = r.get("summary") or "", [], set()
    g = out.pop("growth")
    out["growth_median"] = statistics.median(g) if g else None
    out["growth_max"] = max(g) if g else None
    out["cost"] = round(out["cost"], 2)
    out["cached_prefix_cost_est"] = round(out["cached_prefix_cost_est"], 2)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", default=os.path.expanduser("~/.pi/agent/sessions"))
    ap.add_argument("--last", type=int, default=0, help="only the N newest session files (0 = all)")
    a = ap.parse_args()
    files = sorted(glob.glob(os.path.join(a.sessions, "**", "*.jsonl"), recursive=True), key=os.path.getmtime)
    if a.last:
        files = files[-a.last:]
    print(json.dumps(audit(files), indent=1))


if __name__ == "__main__":
    main()
