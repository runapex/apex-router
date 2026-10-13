#!/usr/bin/env python3
"""Follow-up: one full-context exchange round (rho1) vs call-matched self-consistency, with a self-only
control. Pre-declaration: docs/research/2026-10-13-debate-vs-sc-predeclaration.md. Reuses the cached client,
parser, pooling and verdict plumbing from scripts/panel.py.

  .venv/bin/python scripts/panel_debate.py prep [--n 360]
  .venv/bin/python scripts/panel_debate.py run  [--limit 12] [--workers 12]
"""
from __future__ import annotations

import argparse
import ast
import collections
import hashlib
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import panel as P  # noqa: E402
from panel import verdict  # noqa: E402

DATA = P.HOME / "musique_ans_dev.jsonl"
ITEMS = P.HOME / "items-musique.jsonl"
ARMS = ("single", "sc6", "rho1", "reflect")


def _lit(v):
    return ast.literal_eval(v) if isinstance(v, str) else v


def prep(n, seed=11):
    rows = [json.loads(line) for line in open(DATA)]
    rng = random.Random(seed)
    by_hop = collections.defaultdict(list)
    for r in rows:
        by_hop[r["id"].split("hop")[0]].append(r)
    out = []
    per_hop = {h: n * len(v) // len(rows) for h, v in by_hop.items()}
    per_hop["2"] += n - sum(per_hop.values())
    for h, quota in sorted(per_hop.items()):
        pool = by_hop[h][:]
        rng.shuffle(pool)
        pos = neg = 0
        for r in pool:
            if pos + neg >= quota:
                break
            paras = _lit(r["paragraphs"])
            docs = [{"title": p["title"], "text": p["paragraph_text"]} for p in paras]
            gold = sorted({p["title"] for p in paras if p["is_supporting"]})
            ans = r["answer"]
            bad = {a.lower() for a in [ans, *_lit(r["answer_aliases"])]}
            base = dict(id=r["id"], kind=f"{h}hop", q=r["question"], docs=docs, gold=gold)
            if pos < quota // 2 + quota % 2 and pos <= neg:
                out.append({**base, "cand": ans, "y": 1})
                pos += 1
                continue
            dec = _lit(r["question_decomposition"])
            cands = [d["answer"] for d in dec[:-1] if d["answer"].lower() not in bad]
            cand = cands[-1] if cands else None
            if cand is None:
                titles = [p["title"] for p in paras if not p["is_supporting"] and p["title"].lower() not in bad]
                cand = rng.choice(sorted(titles)) if titles else None
            if cand is not None:
                out.append({**base, "cand": cand, "y": 0, "true": ans})
                neg += 1
    for it in out:
        hsh = int(hashlib.sha256(it["id"].encode()).hexdigest(), 16)
        it["split"] = "promo" if hsh % 2 == 0 else "confirm"
        it["window"] = f"w{(hsh >> 8) % 4}"
    ITEMS.write_text("".join(json.dumps(it) + "\n" for it in out))
    c = collections.Counter((it["kind"], it["y"], it["split"]) for it in out)
    print(f"wrote {len(out)} items -> {ITEMS}: {dict(sorted(c.items()))}")


# ---------------------------------------------------------------- one neutral prompt for every arm

def perm(item, s):
    order = list(range(len(item["docs"])))
    if s:
        random.Random(int(hashlib.sha256(f"{item['id']}|{s}".encode()).hexdigest(), 16)).shuffle(order)
    return order


def task(item):
    return f"Question: {item['q']}\nProposed answer: {item['cand']}\nGive the probability that the proposed answer is correct."


def prompt(item, s, earlier=""):
    docs = "\n".join(f"[D{i + 1}] {item['docs'][i]['title']}: {item['docs'][i]['text']}" for i in perm(item, s))
    ex = (f"\n\nEarlier independent answers to this same question:\n{earlier}\n"
          "Using the documents and, if useful, the earlier answers, give your final probability.") if earlier else ""
    return (f"Judge the question using ONLY the documents below. If they do not settle it, give a probability "
            f"near 0.5.\n\nDocuments:\n{docs}\n\n{task(item)}{ex}\n{P.FMT}")


def _line(rec, label):
    return f"- {label}: p={P._p(rec):.2f}; rationale: {rec['rationale']}"


def run_item(item, call, arms, pool):
    iid = item["id"]
    tok = lambda recs: sum(r["tok_in"] + r["tok_out"] for r in recs)  # noqa: E731
    need = 6 if "sc6" in arms else 3 if {"rho1", "reflect"} & set(arms) else 1
    futs = [pool.submit(call, iid, f"s{s}", prompt(item, s)) for s in range(need)]
    s = [f.result() for f in futs]
    r2 = {}
    for a in range(3):
        if "rho1" in arms:
            hist = "\n".join(_line(s[k], "your earlier answer" if k == a else f"another reader ({k + 1})")
                             for k in range(3))
            r2[f"rho1:{a}"] = pool.submit(call, iid, f"rho1:{a}", prompt(item, a, hist))
        if "reflect" in arms:
            r2[f"reflect:{a}"] = pool.submit(call, iid, f"reflect:{a}", prompt(item, a, _line(s[a], "your earlier answer")))
    r2 = {k: f.result() for k, f in r2.items()}
    out = {}
    if "single" in arms:
        out["single"] = {"p": P.pool_uniform([P._p(s[0])]), "tok": tok(s[:1]), "recs": s[:1]}
    if "sc6" in arms:
        out["sc6"] = {"p": P.pool_uniform([P._p(x) for x in s]), "p_cw": P.pool_cw([P._p(x) for x in s]),
                      "tok": tok(s), "recs": s}
    for arm in ("rho1", "reflect"):
        if arm in arms:
            fin = [r2[f"{arm}:{a}"] for a in range(3)]
            ps = [P._p(x) for x in fin]
            out[arm] = {"p": P.pool_uniform(ps), "p_cw": P.pool_cw(ps), "tok": tok(s[:3] + fin),
                        "recs": s[:3] + fin, "r1": [P._p(x) for x in s[:3]], "final": ps}
    return out


def report(items, res, model, effort):
    n = len(items)
    tab = P.arm_table(items, res, ARMS, sorted({it["kind"] for it in items}))
    cons = verdict.apply_fdr([P.brier_contrast(nm, a, b, res, items) for nm, a, b in (
        ("C1 rho1 vs sc6 (primary)", "rho1", "sc6"),
        ("C2 rho1 vs reflect (peers vs second look)", "rho1", "reflect"),
        ("C3 reflect vs sc6", "reflect", "sc6"),
        ("C4 sc6 vs single", "sc6", "single"))])
    g = verdict.gate_rows([P.gate_cell(res, items, "sc6", ["rho1"], "debate/musique/vs-sc6", cost=False)],
                          k=30, m_windows=2)
    c2 = cons[1]
    promoted = bool(g and g[0]["promoted"])
    outcome = ("A: adopt a peer-exchange round" if promoted and c2.status == verdict.BETTER and c2.fdr_reject
               else "B: the gain is a second look (self-review, not peers)" if promoted
               else "C: close the thread (no promotion over call-matched self-consistency)")
    diag = {
        "sc_items_with_any_range": sum(len({P._p(r) for r in res[i]["sc6"]["recs"]}) > 1 for i in range(n)),
        "sc_items_with_range_gt_0.05": sum(max(P._p(r) for r in res[i]["sc6"]["recs"]) -
                                           min(P._p(r) for r in res[i]["sc6"]["recs"]) > 0.05 for i in range(n)),
        "round1_items_with_disagreement": sum(max(res[i]["rho1"]["r1"]) >= .5 > min(res[i]["rho1"]["r1"]) for i in range(n)),
        "rho1_flips_vs_round1": sum((res[i]["rho1"]["p"] >= .5) != (P.pool_uniform(res[i]["rho1"]["r1"]) >= .5) for i in range(n)),
        "reflect_flips_vs_round1": sum((res[i]["reflect"]["p"] >= .5) != (P.pool_uniform(res[i]["reflect"]["r1"]) >= .5) for i in range(n)),
    }
    return verdict.Verdict(
        question="does one full-context exchange round beat call-matched self-consistency, and is it the peers?",
        incumbent="sc6", decision=outcome + " | " + verdict.decide("sc6", gate=g), arms=tab, contrasts=cons, gate=g,
        diagnostics=diag, meta={"n": n, "model": model, "effort": effort, "benchmark": "MuSiQue-Ans dev, binarised",
                                "aggregator": "uniform logit pool, clipped [0.01, 0.99], all arms",
                                "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prep").add_argument("--n", type=int, default=360)
    r = sub.add_parser("run")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--model", default="it-entra-gpt-5.6-luna")
    r.add_argument("--effort", default="low")
    r.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()
    if a.cmd == "prep":
        return prep(a.n)
    items = [json.loads(line) for line in open(ITEMS)]
    if a.limit:
        items = items[::max(1, len(items) // a.limit)][:a.limit]
    call = P.Client(a.model, a.effort, a.workers)
    items, res, dropped = P.run_items(items, call, ARMS, run_item)
    card = report(items, res, a.model, a.effort)
    card.meta["dropped_items"] = dropped
    P.render(card)
    out = P.HOME / f"card-debate-musique-{len(items)}.json"
    out.write_text(card.to_json(indent=1))
    print(f"card -> {out}")


if __name__ == "__main__":
    main()
