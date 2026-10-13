#!/usr/bin/env python3
"""Panel step 1 — base rate of panel-candidate turns in local agent transcripts. Read-only.

A panel candidate is a model request that (a) answers in text with no tool call (a judgment, not an
action), (b) sits in a user turn whose prompt is decision-shaped (diagnose/review/choose/estimate) and
not edit-shaped, (c) has had no edit tool fire earlier in that turn, and (d) has >= k discrete evidence
blocks (tool results) in context since the user prompt. That is InfoDelphi's unit: a judgment over a
fixed evidence pool. Proxy telemetry carries no bodies, so shape comes from transcripts only.

Usage: python scripts/panel_base_rate.py [--root ~/.apex-router/transcripts] [--k 3 5 8] [--since 2026-09-30]
Kill criterion (pre-declared): strict candidate fraction of requests < 5% at k=3.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
from datetime import datetime, timezone
from pathlib import Path

DECISION = re.compile(
    r"\b(why|diagnos\w*|root[- ]cause|investigat\w*|review\w*|assess\w*|evaluat\w*|compar\w*|which|"
    r"should (we|i)|choose|pick|decid\w*|recommend\w*|estimat\w*|explain\w*|what'?s (wrong|causing)|"
    r"verify|validat\w*|cross-validat\w*|audit\w*|critique|second opinion|go/no-go|is (it|this|there) )", re.I)
EDIT = re.compile(
    r"\b(implement\w*|fix(es|ed)?|add|write|creat\w*|refactor\w*|renam\w*|updat\w*|chang\w*|edit|patch|"
    r"build|commit|push|install|migrat\w*|delet\w*|remov\w*)\b", re.I)
EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "apply_patch", "str_replace_based_edit_tool"}
SHELL_EDIT = re.compile(r"apply_patch|sed -i|\bcat\s*>|>\s*[\w./-]+\.(py|ts|js|md|rb|go|json|ya?ml)\b|tee ")


def shape(prompt: str) -> str:
    p = (prompt or "")[:3000]
    d, e = bool(DECISION.search(p)), bool(EDIT.search(p))
    return "decision" if d and not e else "edit" if e and not d else "mixed" if d else "other"


def _is_edit_call(name: str, args: str) -> bool:
    n = (name or "").lower()
    return n in EDIT_TOOLS or (n in {"exec_command", "shell", "bash", "local_shell"} and bool(SHELL_EDIT.search(args or "")))


# ---- normalizers: each yields ("user", text, ts) | ("result",) | ("req", model, tok_in, has_text, [(name,args)], ts)

def norm_claude(path):
    reqs, order = {}, []
    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message") if isinstance(d.get("message"), dict) else None
        ts = d.get("timestamp")
        if d.get("type") == "user" and m:
            c = m.get("content")
            if isinstance(c, str):
                if not d.get("isMeta") and not c.startswith("<"):
                    order.append(("user", c, ts))
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        order.append(("result",))
                    elif isinstance(b, dict) and b.get("type") == "text" and not d.get("isMeta"):
                        order.append(("user", b.get("text", ""), ts))
        elif d.get("type") == "assistant" and m:
            rid = d.get("requestId") or m.get("id")
            if not rid or m.get("model") == "<synthetic>":
                continue
            if rid not in reqs:
                u = m.get("usage") or {}
                tok = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                reqs[rid] = ["req", m.get("model"), tok, False, [], ts]
                order.append(reqs[rid])
            for b in m.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                    reqs[rid][3] = True
                elif isinstance(b, dict) and b.get("type") == "tool_use":
                    reqs[rid][4].append((b.get("name"), json.dumps(b.get("input"))[:4000]))
    return order, None


def norm_pi(path):
    out = []
    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message") if isinstance(d.get("message"), dict) else None
        if not m:
            continue
        ts, role = d.get("timestamp"), m.get("role")
        c = m.get("content") if isinstance(m.get("content"), list) else []
        if role == "user":
            out.append(("user", " ".join(b.get("text", "") for b in c if isinstance(b, dict)), ts))
        elif role == "toolResult":
            out.append(("result",))
        elif role == "assistant":
            u = m.get("usage") or {}
            tok = sum(int(u.get(k) or 0) for k in ("input", "cacheRead", "cacheWrite"))
            text = any(isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip() for b in c)
            calls = [(b.get("name"), json.dumps(b.get("arguments"))[:4000]) for b in c
                     if isinstance(b, dict) and b.get("type") == "toolCall"]
            out.append(["req", m.get("model"), tok, text, calls, ts])
    return out, None


def norm_codex(path):
    out, model, origin, cur = [], None, None, None
    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t, p, ts = d.get("type"), d.get("payload") or {}, d.get("timestamp")
        if t == "session_meta":
            origin = p.get("originator")
        elif t == "turn_context":
            model = p.get("model") or model
        elif t == "event_msg" and p.get("type") == "user_message":
            out.append(("user", p.get("message") or "", ts))
            cur = None
        elif t == "response_item":
            pt = p.get("type")
            if pt in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
                out.append(("result",))
                cur = None
            elif pt in ("function_call", "custom_tool_call", "local_shell_call", "message", "reasoning") and p.get("role", "assistant") == "assistant":
                if cur is None:
                    cur = ["req", model, 0, False, [], ts]
                    out.append(cur)
                if pt == "message":
                    cur[3] = cur[3] or any((b.get("text") or "").strip() for b in p.get("content") or [] if isinstance(b, dict))
                elif pt != "reasoning":
                    cur[4].append((p.get("name") or pt, str(p.get("arguments") or p.get("input") or p.get("action") or "")[:4000]))
        elif t == "event_msg" and p.get("type") == "token_count":
            info = p.get("info") or {}
            if cur is not None and info.get("last_token_usage"):
                cur[2] = int(info["last_token_usage"].get("input_tokens") or 0)
            cur = None
    return out, origin


def _ts(v):
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def walk(events, ks, since):
    """Yield one row per request: (model, tok, final, shape, evidence, edited_before)."""
    prompt, ev, edited = "", 0, False
    for e in events:
        if e[0] == "user":
            prompt, ev, edited = e[1], 0, False
        elif e[0] == "result":
            ev += 1
        else:
            _, model, tok, text, calls, ts = e  # noqa: F841 (model re-read below)
            t = _ts(ts)
            if since and t and t < since:
                edited = edited or any(_is_edit_call(n, a) for n, a in calls)
                continue
            yield model or "?", tok, text and not calls, shape(prompt), ev, edited, prompt
            edited = edited or any(_is_edit_call(n, a) for n, a in calls)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path.home() / ".apex-router" / "transcripts"))
    ap.add_argument("--k", type=int, nargs="+", default=[3, 5, 8])
    ap.add_argument("--since", default=None, help="YYYY-MM-DD; requests before are skipped")
    ap.add_argument("--examples", type=int, default=0)
    a = ap.parse_args()
    since = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc) if a.since else None
    root = Path(a.root).expanduser()
    norms = {"claude": norm_claude, "pi": norm_pi, "codex": norm_codex}

    tot = collections.Counter()           # (src) -> requests ; ("tok", src) -> tokens
    cand = collections.Counter()          # (variant, k, src) -> requests ; tokens under ("tok", ...)
    by_model = collections.Counter()      # (k, model) strict candidates
    shapes = collections.Counter()
    evhist = collections.Counter()
    examples = []
    for src, fn in norms.items():
        for f in sorted((root / src).rglob("*.jsonl")):
            events, origin = fn(f)
            s = f"{src}:{origin}" if origin else src
            for model, tok, final, shp, ev, edited, prompt in walk(events, a.k, since):
                tot[s] += 1
                tot["tok", s] += tok
                if final:
                    shapes[shp] += 1
                    evhist[min(ev, 20)] += 1
                for k in a.k:
                    base = final and not edited and ev >= k
                    for var, ok in (("strict", base and shp == "decision"),
                                    ("loose", base and shp in ("decision", "mixed"))):
                        if ok:
                            cand[var, k, s] += 1
                            cand["tok", var, k, s] += tok
                    if base and shp == "decision":
                        by_model[k, model] += 1
                        if k == a.k[0] and len(examples) < a.examples:
                            examples.append((s, model, ev, prompt[:160].replace("\n", " ")))

    srcs = sorted(s for s in tot if isinstance(s, str))
    N, T = sum(tot[s] for s in srcs), sum(tot["tok", s] for s in srcs)
    print(f"requests={N}  input_tokens={T:,}  since={a.since or 'all'}")
    print("per source:", {s: tot[s] for s in srcs})
    finals = sum(shapes.values())
    print(f"final (text-only) requests={finals} ({finals / max(N, 1):.1%}); prompt shape of finals: {dict(shapes)}")
    print("evidence blocks before final answer (capped 20):", dict(sorted(evhist.items())))
    for var in ("strict", "loose"):
        for k in a.k:
            n = sum(cand[var, k, s] for s in srcs)
            t = sum(cand["tok", var, k, s] for s in srcs)
            per = {s: f"{cand[var, k, s]}/{tot[s]}" for s in srcs if cand[var, k, s]}
            print(f"{var:6s} k>={k}: {n:5d} req = {n / max(N, 1):6.2%} of requests, {t / max(T, 1):6.2%} of tokens  {per}")
    k0 = a.k[0]
    print(f"strict k>={k0} by model:", {m: c for (k, m), c in by_model.most_common() if k == k0})
    for ex in examples:
        print("  ex", ex)
    strict = sum(cand["strict", k0, s] for s in srcs) / max(N, 1)
    print(f"KILL CHECK (strict, k>={k0}, threshold 5%): {strict:.2%} -> {'KILL' if strict < 0.05 else 'CONTINUE'}")


if __name__ == "__main__":
    main()
