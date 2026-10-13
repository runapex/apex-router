#!/usr/bin/env python3
"""Panel step 2 — InfoDelphi-style panel vs controls on a PUBLIC fixed-evidence benchmark, judged by
the gate engine. This is an engine demo, NOT a reproduction of InfoDelphi (PolyGym's evidence was never
released). Stdlib + apex_router.{gate,core.stats}; run with the repo venv.

Benchmark: HotpotQA distractor dev (10 fixed paragraphs per question, 2 gold). Binary items:
  cmp  — native yes/no comparison questions ("Were X and Y of the same nationality?")
  brg  — bridge questions turned into claim verification: (question, proposed answer) -> correct?
         Negatives are a same-shape gold answer of ANOTHER question that occurs verbatim in THIS
         item's paragraphs (a hard, in-context distractor).

Arms (all on the same items, same model, same reasoning effort):
  single    one call, full context
  sc6       six full-context calls, logit-pooled (call-matched to the 2-round J=3 panels)
  rho1      J=3, R=2, everyone sees everything, rationale exchange (isolates asymmetry)
  asym_r1   J=3, rho=0.5 BM25 split, round 1 only (independent; free from asym round 1)
  asym_rat  J=3, R=2, rho=0.5, rationale exchange (InfoDelphi)
  asym_ev   J=3, R=2, rho=0.5, round 2 passes each peer's private docs, capped to the char length of
            that peer's round-1 rationale (the evidence-exchange arm; shares round 1 with asym_rat)
Primary aggregator: uniform logit pool. Extremity (CW) weighting is reported as secondary only.

Usage:
  .venv/bin/python scripts/panel.py prep  [--n 240]
  .venv/bin/python scripts/panel.py run   [--arms single] [--limit 20] [--model it-entra-gpt-5.6-luna]
  .venv/bin/python scripts/panel.py report
"""
from __future__ import annotations

import argparse
import ast
import collections
import hashlib
import json
import math
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from apex_router import gate  # noqa: E402
from apex_router.core import verdict  # noqa: E402

HOME = Path.home() / ".apex-router" / "panel"
DATA = HOME / "hotpot_dev_distractor.jsonl"
ITEMS = HOME / "items.jsonl"
CACHE = HOME / "calls.jsonl"
URL = "http://localhost:8788/gpt5/openai/responses?api-version=2025-04-01-preview"
J, RHO, EQUIV_MARGIN, GAIN_MIN = 3, 0.5, 0.01, 0.01
ALL_ARMS = ("single", "sc6", "rho1", "asym_r1", "asym_rat", "rand_r1", "rand_rat", "rand_ev")
STOP = set("the a an of in on at to for and or is was were are be by with as from that this which who what "
           "did does do how when where whose same both".split())


# ---------------------------------------------------------------- data prep

def _ctx(r):
    c = r["context"]
    c = ast.literal_eval(c) if isinstance(c, str) else c
    return [{"title": t, "text": "".join(s).strip()} for t, s in zip(c["title"], c["sentences"])]


def _gold(r):
    s = r["supporting_facts"]
    s = ast.literal_eval(s) if isinstance(s, str) else s
    return sorted(set(s["title"]))


def _toks(s):
    return {t for t in re.findall(r"\w+", s.lower()) if t not in STOP}


def prep(n, seed=7):
    rows = [json.loads(line) for line in open(DATA)]
    rng = random.Random(seed)
    cmp_y = [r for r in rows if r["type"] == "comparison" and r["answer"].lower() == "yes"]
    cmp_n = [r for r in rows if r["type"] == "comparison" and r["answer"].lower() == "no"]
    brg = [r for r in rows if r["type"] == "bridge" and r["answer"].lower() not in ("yes", "no")]
    q = n // 4
    out = []
    for r in rng.sample(cmp_y, q) + rng.sample(cmp_n, q):
        out.append(dict(id=r["id"], kind="cmp", q=r["question"], cand=None, y=int(r["answer"].lower() == "yes"),
                        docs=_ctx(r), gold=_gold(r)))
    rng.shuffle(brg)
    pos = neg = 0
    for r in brg:
        docs, gold = _ctx(r), r["answer"]
        if pos < q and (pos <= neg):
            out.append(dict(id=r["id"], kind="brg", q=r["question"], cand=gold, y=1, docs=docs, gold=_gold(r)))
            pos += 1
            continue
        if neg >= q:
            if pos >= q:
                break
            continue
        gt, gold_titles = _toks(gold), set(_gold(r))
        if re.fullmatch(r"\d{4}", gold):   # a year: another year that occurs in the paragraphs
            text = " ".join(d["text"] for d in docs)
            cands = sorted(set(re.findall(r"\b(1[5-9]\d\d|20[0-2]\d)\b", text)) - {gold})
        else:                                # an entity: the title of a non-gold (BM25 distractor) paragraph
            cands = sorted(d["title"] for d in docs if d["title"] not in gold_titles and not (_toks(d["title"]) & gt)
                           and gold.lower() not in d["title"].lower() and d["title"].lower() not in gold.lower())
        if cands:
            out.append(dict(id=r["id"], kind="brg", q=r["question"], cand=rng.choice(cands), y=0,
                            docs=docs, gold=_gold(r), true=gold))
            neg += 1
    for it in out:
        h = int(hashlib.sha256(it["id"].encode()).hexdigest(), 16)
        it["split"] = "promo" if h % 2 == 0 else "confirm"
        it["window"] = f"w{(h >> 8) % 4}"
    ITEMS.write_text("".join(json.dumps(it) + "\n" for it in out))
    c = collections.Counter((it["kind"], it["y"], it["split"]) for it in out)
    print(f"wrote {len(out)} items -> {ITEMS}: {dict(sorted(c.items()))}")


# ---------------------------------------------------------------- evidence routing

def bm25(query, docs, k1=1.5, b=0.75):
    tok = lambda s: [t for t in re.findall(r"\w+", s.lower())]  # noqa: E731
    q = [t for t in tok(query) if t not in STOP]
    D = [tok(d["title"] + " " + d["text"]) for d in docs]
    avg = sum(map(len, D)) / max(len(D), 1)
    df = collections.Counter(t for d in D for t in set(d))
    out = []
    for d in D:
        tf, s = collections.Counter(d), 0.0
        for t in q:
            if t in tf:
                idf = math.log(1 + (len(D) - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avg))
        out.append(s)
    return out


def partition(item, route="bm25", rho=RHO, j=J):
    """Deterministic. bm25: top floor(rho*n) by BM25(question) are public, the rest round-robin by rank
    (InfoDelphi). random: a per-item seeded shuffle, then the same split (the paper's random-allocation ablation).
    all: everyone sees everything (rho=1)."""
    n = len(item["docs"])
    if route == "all":
        return list(range(n)), [[] for _ in range(j)]
    if route == "random":
        order = list(range(n))
        random.Random(int(hashlib.sha256(item["id"].encode()).hexdigest(), 16)).shuffle(order)
    else:
        sc = bm25(item["q"], item["docs"])
        order = sorted(range(n), key=lambda i: (-sc[i], i))
    n_pub = int(rho * len(order))
    pub, rest = order[:n_pub], order[n_pub:]
    priv = [[i for k, i in enumerate(rest) if k % j == a] for a in range(j)]
    return pub, priv


def gold_diag(item, pub, priv):
    gi = [i for i, d in enumerate(item["docs"]) if d["title"] in item["gold"]]
    private = [i for i in gi if i not in pub]
    holders = {a for a, p in enumerate(priv) for i in gi if i in p}
    return {"gold_private": len(private), "gold_n": len(gi), "gold_split_across_agents": len(holders) >= 2}


# ---------------------------------------------------------------- prompts

def _doc(i, d):
    return f"[D{i + 1}] {d['title']}: {d['text']}"


def task(item):
    if item["kind"] == "cmp":
        return f"Question: {item['q']}\nGive the probability that the answer is YES."
    return f"Question: {item['q']}\nProposed answer: {item['cand']}\nGive the probability that the proposed answer is correct."


FMT = ('Respond with JSON only: {"p": <probability between 0 and 1>, "rationale": "<at most 60 words>", '
       '"cites": ["D<n>", ...]}')


def prompt_full(item):
    docs = "\n".join(_doc(i, d) for i, d in enumerate(item["docs"]))
    return (f"Judge the question using ONLY the documents below. If they do not settle it, give a probability "
            f"near 0.5.\n\nDocuments:\n{docs}\n\n{task(item)}\n{FMT}")


def prompt_agent(item, pub, mine, history=""):
    """Public pool first, then the private slice: the shared prefix is cache-stable across the panel."""
    head = ("You are one member of a 3-person panel. Judge the question using ONLY the documents you were "
            "given. If they do not settle it, give a probability near 0.5.\n\n")
    shared = "Shared documents (all panel members see these):\n" + "\n".join(_doc(i, item["docs"][i]) for i in pub)
    own = ("\n\nYour private documents (only you see these):\n" + "\n".join(_doc(i, item["docs"][i]) for i in mine)
           if mine else "")
    hist = f"\n\nRound 1 from the other panel members:\n{history}\nReconsider your estimate." if history else ""
    return f"{head}{shared}{own}\n\n{task(item)}{hist}\n{FMT}"


# ---------------------------------------------------------------- model calls (cached, resumable)

class Client:
    def __init__(self, model, effort, workers):
        self.model, self.effort = model, effort
        self.sem = threading.Semaphore(workers)
        self.lock = threading.Lock()
        self.tok, self.tok_t = None, 0.0
        self.cache = {}
        self.n_live = 0
        if CACHE.exists():
            for line in open(CACHE):
                try:
                    r = json.loads(line)
                    self.cache[r["key"]] = r
                except ValueError:
                    pass

    def _token(self):
        with self.lock:
            if not self.tok or time.time() - self.tok_t > 1800:
                self.tok = subprocess.check_output(
                    ["az", "account", "get-access-token", "--scope", "https://cognitiveservices.azure.com/.default",
                     "--query", "accessToken", "-o", "tsv"], text=True).strip()
                self.tok_t = time.time()
            return self.tok

    def __call__(self, item_id, slot, prompt):
        key = hashlib.sha256(f"{item_id}|{slot}|{self.model}|{self.effort}|{prompt}".encode()).hexdigest()[:24]
        if key in self.cache:
            return self.cache[key]
        body = json.dumps({"model": self.model, "input": prompt, "max_output_tokens": 1200,
                           "reasoning": {"effort": self.effort}}).encode()
        text, usage, err = "", {}, None
        with self.sem:
            for attempt in range(6):
                req = urllib.request.Request(URL, body, {"Authorization": f"Bearer {self._token()}",
                                                         "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=180) as r:
                        d = json.load(r)
                    text = "".join(c.get("text", "") for o in d.get("output", []) for c in (o.get("content") or [])
                                   if isinstance(c, dict))
                    usage, err = d.get("usage") or {}, None
                    break
                except urllib.error.HTTPError as e:
                    err = f"http {e.code}"
                    if e.code in (429, 500, 502, 503, 504):
                        ra = e.headers.get("retry-after")
                        time.sleep(min(float(ra) if ra and ra.replace(".", "").isdigit() else 2 ** attempt, 60))
                        continue
                    break
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    err = f"net {type(e).__name__}"
                    time.sleep(2 ** attempt)
        p, rat, cites = parse(text)
        rec = {"key": key, "item": item_id, "slot": slot, "model": self.model, "effort": self.effort, "p": p,
               "rationale": rat, "cites": cites, "parse_ok": p is not None, "err": err,
               "tok_in": int(usage.get("input_tokens") or 0), "tok_out": int(usage.get("output_tokens") or 0)}
        if err is not None:
            print(f"  call failed: {item_id} {slot}: {err}", file=sys.stderr)
        if err is None and p is None:
            print(f"  unparseable answer (not cached): {item_id} {slot}", file=sys.stderr)
        if err is None and p is not None:   # never cache transport OR parse failures: a rerun retries them
            with self.lock:
                self.cache[key] = rec
                self.n_live += 1
                with open(CACHE, "a") as f:
                    f.write(json.dumps(rec) + "\n")
        return rec


def parse(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None, "", []
    try:
        d = json.loads(m.group(0))
        p = float(d.get("p"))
        p = p / 100 if 1 < p <= 100 else p
        if not 0 <= p <= 1:
            return None, "", []
        return p, str(d.get("rationale") or "")[:1200], [str(c) for c in d.get("cites") or []]
    except (ValueError, TypeError):
        return None, "", []


# ---------------------------------------------------------------- arms

def _p(rec):
    return 0.5 if rec["p"] is None else rec["p"]


def logit(p):
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))


def pool_uniform(ps):
    return 1 / (1 + math.exp(-sum(map(logit, ps)) / len(ps)))


def pool_cw(ps):
    ls = [logit(p) for p in ps]
    w = [abs(x) for x in ls]
    return pool_uniform(ps) if sum(w) == 0 else 1 / (1 + math.exp(-sum(a * b for a, b in zip(w, ls)) / sum(w)))


def _hist_rationale(r1, me):
    return "\n".join(f"- Agent {k + 1}: p={_p(r):.2f}; rationale: {r['rationale']}; cites: {', '.join(r['cites'])}"
                     for k, r in enumerate(r1) if k != me)


def _hist_evidence(item, r1, priv, me):
    lines = []
    for k, r in enumerate(r1):
        if k == me:
            continue
        budget = max(len(r["rationale"]), 1)
        ev = " ".join(_doc(i, item["docs"][i]) for i in priv[k])[:budget]
        lines.append(f"- Agent {k + 1}: p={_p(r):.2f}; excerpt of its private documents: {ev}")
    return "\n".join(lines)


PANELS = {"rho1": "all", "asym": "bm25", "rand": "random"}   # panel -> evidence route


def _arm_parts(arm):
    """'rho1' -> ('rho1', 'rat'); 'asym_r1' -> ('asym', 'r1'); 'rand_ev' -> ('rand', 'ev')."""
    return ("rho1", "rat") if arm == "rho1" else tuple(arm.split("_", 1))


def run_item(item, call, arms, pool):
    """Returns {arm: {"p": aggregate, "tok": int, "recs": [...], ...}} for the requested arms. Round 1 of a panel is
    shared by all of its round-2 variants (rat/ev), so each contrast between them is paired on identical round 1."""
    iid, out = item["id"], {}
    tok = lambda recs: sum(r["tok_in"] + r["tok_out"] for r in recs)  # noqa: E731
    panels = {}
    for arm in arms:
        if arm not in ("single", "sc6"):
            pn, var = _arm_parts(arm)
            panels.setdefault(pn, set()).add(var)
    jobs = {}
    if "single" in arms:
        jobs["single"] = pool.submit(call, iid, "single", prompt_full(item))
    if "sc6" in arms:
        for s in range(6):
            jobs[f"sc{s}"] = pool.submit(call, iid, f"sc{s}", prompt_full(item))
    split = {pn: partition(item, PANELS[pn]) for pn in panels}
    for pn in panels:
        pub, priv = split[pn]
        for a in range(J):
            jobs[f"{pn}:r1:{a}"] = pool.submit(call, iid, f"{pn}:r1:{a}", prompt_agent(item, pub, priv[a]))
    r = {k: f.result() for k, f in jobs.items()}
    r2 = {}
    for pn, vars_ in panels.items():
        pub, priv = split[pn]
        r1 = [r[f"{pn}:r1:{a}"] for a in range(J)]
        for var in vars_ - {"r1"}:
            for a in range(J):
                h = _hist_rationale(r1, a) if var == "rat" else _hist_evidence(item, r1, priv, a)
                r2[f"{pn}:{var}:{a}"] = pool.submit(call, iid, f"{pn}:{var}:{a}", prompt_agent(item, pub, priv[a], h))
    r2 = {k: f.result() for k, f in r2.items()}
    if "single" in arms:
        out["single"] = {"p": pool_uniform([_p(r["single"])]), "tok": tok([r["single"]]), "recs": [r["single"]]}
    if "sc6" in arms:
        recs = [r[f"sc{s}"] for s in range(6)]
        out["sc6"] = {"p": pool_uniform([_p(x) for x in recs]), "p_cw": pool_cw([_p(x) for x in recs]),
                      "tok": tok(recs), "recs": recs}
    for arm in arms:
        if arm in ("single", "sc6"):
            continue
        pn, var = _arm_parts(arm)
        recs1 = [r[f"{pn}:r1:{a}"] for a in range(J)]
        final = recs1 if var == "r1" else [r2[f"{pn}:{var}:{a}"] for a in range(J)]
        ps = [_p(x) for x in final]
        out[arm] = {"p": pool_uniform(ps), "p_cw": pool_cw(ps), "tok": tok(recs1 + ([] if var == "r1" else final)),
                    "recs": recs1 + ([] if var == "r1" else final), "r1": [_p(x) for x in recs1], "final": ps}
    return out


# ---------------------------------------------------------------- verdict card (core.verdict)

def brier(p, y):
    return (p - y) ** 2


def brier_contrast(name, a, b, res, items, margin=None):
    """Paired per-item Brier(b) - Brier(a): positive means arm a is better than arm b."""
    d = [brier(res[i][b]["p"], it["y"]) - brier(res[i][a]["p"], it["y"]) for i, it in enumerate(items)]
    return verdict.paired_contrast(name, a, b, d, metric="brier_gain", min_n=30, margin=margin)


def gate_cell(res, items, incumbent, cands, cell_id, cost=True):
    def deltas(cand, split):
        return [brier(res[i][incumbent]["p"], it["y"]) - brier(res[i][cand]["p"], it["y"])
                for i, it in enumerate(items) if it["split"] == split]
    wins = {a: {it["window"] for it in items if it["split"] == "confirm"} for a in cands}
    costs = ({a: sum(res[i][a]["tok"] for i in range(len(items))) / len(items) for a in [incumbent, *cands]}
             if cost else {})
    return gate.CellEvidence(cell_id=cell_id, parent_task_type="judgment-over-evidence", incumbent_model=incumbent,
                             promo_deltas={a: deltas(a, "promo") for a in cands},
                             confirm_deltas={a: deltas(a, "confirm") for a in cands}, confirm_windows=wins,
                             provenance="objective", cost=costs)


def arm_table(items, res, arms, kinds):
    n, tab = len(items), {}
    for a in arms:
        ps = [res[i][a]["p"] for i in range(n)]
        recs = [r for i in range(n) for r in res[i][a]["recs"]]
        tab[a] = {"brier": sum(brier(p, it["y"]) for p, it in zip(ps, items)) / n,
                  "acc": sum((p >= 0.5) == bool(it["y"]) for p, it in zip(ps, items)) / n,
                  "tok_per_item": sum(res[i][a]["tok"] for i in range(n)) / n,
                  "calls_per_item": len(recs) / n, "parse_fail": sum(not r["parse_ok"] for r in recs)}
        if "p_cw" in res[0][a]:
            tab[a]["brier_cw"] = sum(brier(res[i][a]["p_cw"], it["y"]) for i, it in enumerate(items)) / n
        for kind in kinds:
            idx = [i for i, it in enumerate(items) if it["kind"] == kind]
            tab[a][f"brier_{kind}"] = sum(brier(res[i][a]["p"], items[i]["y"]) for i in idx) / max(len(idx), 1)
    return tab


def report(items, res, arms, model, effort):
    n = len(items)
    arm_tab = arm_table(items, res, arms, ("cmp", "brg"))
    pre = [("bm25 panel beats self-consistency", "asym_rat", "sc6", None),
           ("random panel beats self-consistency", "rand_rat", "sc6", None),
           ("asymmetry effect, bm25 (vs rho=1)", "asym_rat", "rho1", None),
           ("asymmetry effect, random (vs rho=1)", "rand_rat", "rho1", None),
           ("evidence vs rationale exchange", "rand_ev", "rand_rat", EQUIV_MARGIN),
           ("bm25 panel beats single call", "asym_rat", "single", None)]
    cons = verdict.apply_fdr([brier_contrast(nm, a, b, res, items, m) for nm, a, b, m in pre
                              if a in arms and b in arms])
    # Pre-declared: cost = tokens/item, which enables the gate's §5.4 cost tiebreak (a cheaper candidate that
    # independently passes on its own confirmation evidence can replace the promo winner; FDR still applies).
    cells = [gate_cell(res, items, inc, [a for a in arms if a != inc], f"panel/hotpot/vs-{inc}")
             for inc in ("sc6", "single") if inc in arms]
    g = verdict.gate_rows(cells, k=30, m_windows=2) if cells else []
    by = {c.name: c for c in cons}
    kills = {}
    g_sc6 = [x for x in g if x["cell_id"].endswith("vs-sc6")]
    if g_sc6:   # K2 via the gate
        kills["K2_panel_not_better_than_sc6"] = not (g_sc6[0]["promoted"]
                                                    and g_sc6[0]["chosen_model"] not in ("single", "sc6"))
    if "evidence vs rationale exchange" in by:
        st = by["evidence vs rationale exchange"].status
        kills["K3_ev_vs_rat"] = {verdict.EQUIVALENT: "indistinguishable (build evidence arm)",
                                 verdict.BETTER: "evidence better", verdict.WORSE: "rationale better"}.get(
                                     st, f"undecided (CI wider than +/-{EQUIV_MARGIN})")
    if "bm25 panel beats single call" in by and "single" in arm_tab:
        ratio = arm_tab["asym_rat"]["tok_per_item"] / arm_tab["single"]["tok_per_item"]
        kills["K4_token_ratio_vs_single"] = round(ratio, 2)
        kills["K4_fires"] = ratio > 2 and by["bm25 panel beats single call"].estimate < GAIN_MIN
    diag = collections.Counter()
    for it in items:
        for route in ("bm25", "random"):
            gd = gold_diag(it, *partition(it, route))
            diag[f"{route}:gold_private"] += gd["gold_private"]
            diag[f"{route}:items_gold_split_across_agents"] += gd["gold_split_across_agents"]
        diag["gold_docs"] += len([d for d in it["docs"] if d["title"] in it["gold"]])
    if "sc6" in arms:
        diag["sc6_items_with_range_gt_0.05"] = sum(max(_p(r) for r in res[i]["sc6"]["recs"]) -
                                                   min(_p(r) for r in res[i]["sc6"]["recs"]) > 0.05 for i in range(n))
        diag["sc6_items_with_any_range"] = sum(len({_p(r) for r in res[i]["sc6"]["recs"]}) > 1 for i in range(n))
    for pn in ("rho1", "asym", "rand"):
        arm = pn if pn == "rho1" else f"{pn}_r1"
        if arm in arms:
            diag[f"{pn}_r1_items_with_disagreement"] = sum(
                max(res[i][arm]["r1"]) >= .5 > min(res[i][arm]["r1"]) for i in range(n))
    return verdict.Verdict(
        question="does a partitioned-evidence panel beat its controls on judgment-over-fixed-evidence?",
        incumbent="sc6", decision=verdict.decide("sc6", gate=g, kills={k: v for k, v in kills.items() if k == "K2_panel_not_better_than_sc6"}),
        arms=arm_tab, contrasts=cons, gate=g, kills=kills, diagnostics=dict(diag),
        meta={"n": n, "model": model, "effort": effort, "J": J, "rho": RHO, "R": 2,
              "aggregator": "uniform logit pool, p clipped to [0.01, 0.99] for every arm incl. single",
              "equiv_margin": EQUIV_MARGIN, "gain_min": GAIN_MIN, "benchmark": "HotpotQA distractor dev",
              "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})


def render(card):
    m = card.meta
    print(f"n={m['n']} model={m['model']} effort={m['effort']}")
    keys = [k for k in next(iter(card.arms.values())) if k.startswith("brier_") and k != "brier_cw"]
    print(f"{'arm':10s} {'brier':>7s} {'(cw)':>7s} {'acc':>6s} " + " ".join(f"{k[6:]:>7s}" for k in keys)
          + f" {'tok/it':>8s} {'calls':>5s} {'pfail':>5s}")
    for a, t in card.arms.items():
        print(f"{a:10s} {t['brier']:7.4f} {t.get('brier_cw', float('nan')):7.4f} {t['acc']:6.1%} "
              + " ".join(f"{t[k]:7.4f}" for k in keys)
              + f" {t['tok_per_item']:8.0f} {t['calls_per_item']:5.1f} {t['parse_fail']:5d}")
    for c in card.contrasts:
        print(f"  {c.name:38s} gain={c.estimate:+.4f} CI95=[{c.ci[0]:+.4f},{c.ci[1]:+.4f}] "
              f"p={c.p:.3f} BH={'reject' if c.fdr_reject else '-'} -> {c.status}")
    for x in card.gate:
        print(f"  gate {x['cell_id']}: promoted={x['promoted']} chosen={x['chosen_model']} p={x['pvalue']:.3f} ({x['reason']})")
    print("  kills:", card.kills)
    print("  diagnostics:", card.diagnostics)
    print("  decision:", card.decision)


def run_items(items, call, arms, run_item_fn):
    t0, res = time.time(), [None] * len(items)
    with ThreadPoolExecutor(max_workers=64) as pool, ThreadPoolExecutor(max_workers=6) as outer:
        futs = {outer.submit(run_item_fn, it, call, arms, pool): i for i, it in enumerate(items)}
        for k, f in enumerate(futs):
            res[futs[f]] = f.result()
            if (k + 1) % 20 == 0:
                print(f"  {k + 1}/{len(items)} items, {call.n_live} live calls, {time.time() - t0:.0f}s", file=sys.stderr)
    # An item with any failed or unparseable call (e.g. an Azure content-filter 400) is dropped from EVERY arm, so
    # the comparison stays paired; a failed call is never scored as p=0.5. The retained population is conditional
    # on every requested arm completing (reported as dropped_items).
    bad = {i for i, r in enumerate(res) for arm in r.values() for rec in arm["recs"] if rec.get("err") or not rec["parse_ok"]}
    return [it for i, it in enumerate(items) if i not in bad], [r for i, r in enumerate(res) if i not in bad], \
        [it["id"] for i, it in enumerate(items) if i in bad]


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("--n", type=int, default=240)
    for name in ("run", "report"):
        r = sub.add_parser(name)
        r.add_argument("--arms", nargs="+", default=list(ALL_ARMS), choices=ALL_ARMS)
        r.add_argument("--limit", type=int, default=0)
        r.add_argument("--model", default="it-entra-gpt-5.6-luna")
        r.add_argument("--effort", default="low")
        r.add_argument("--workers", type=int, default=10)
    a = ap.parse_args()
    if a.cmd == "prep":
        return prep(a.n)
    items = [json.loads(line) for line in open(ITEMS)]
    if a.limit:
        items = items[::max(1, len(items) // a.limit)][:a.limit]
    call = Client(a.model, a.effort, a.workers)
    if a.cmd == "report" and not CACHE.exists():
        sys.exit("no cache")
    items, res, dropped = run_items(items, call, a.arms, run_item)
    card = report(items, res, a.arms, a.model, a.effort)
    card.meta["dropped_items"] = dropped
    render(card)
    out = HOME / f"card-{'-'.join(a.arms)}-{len(items)}.json"
    out.write_text(card.to_json(indent=1))
    print(f"card -> {out}")


if __name__ == "__main__":
    main()
