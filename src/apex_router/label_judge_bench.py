"""``apex-router labels judge-bench``: score outcome-judge variants against gold.

A variant is (model, context, prompt) — see ``labels.JUDGE_CONTEXTS`` / ``labels.JUDGE_PROMPTS``.
Every variant runs on the SAME tasks: each gold-labelled task (the gold row in force per id;
orphaned gold and ids no longer in tasks.jsonl are skipped). Calls are deterministic (temperature
0, fixed seed) and cached per (variant, task id, hash of the rendered prompt) under
``OUT/cache/``, so a rerun is free and a changed prompt or task is re-judged.

Nothing written here holds transcript text: cache rows and results carry ids, outcomes,
confidences, an evidence enum, latencies and token counts only.

Metrics per variant (``score``):

- ``acc_vote``: accuracy of the judge's VOTES — gold in success/partial/fail, judge outcome in
  success/partial/fail with confidence >= min_conf. This is the number ``labels report`` shows as
  the judge's voter accuracy (0.46 on 2026-10-07), so the two are comparable.
- ``acc4``: exact 4-class accuracy over every gold task (unknown included; an error is wrong).
- ``abstain``: share of tasks with no vote (error, "unknown", or confidence < min_conf);
  ``unknown``: share answered "unknown"; ``error``: share that failed or returned bad JSON.
- ``per_class``: per outcome, recall / precision of the raw answer (any confidence) and of the
  vote (confidence >= min_conf); ``base_success``: accuracy of always answering "success" on the
  decided gold (the bar ``acc_vote`` must clear to mean anything).
- confusion gold x judge, mean latency, mean prompt / eval tokens.

``--gold-tag TAG`` restricts the tasks to gold rows in force tagged TAG (a holdout batch made
after the judge was chosen) and writes ``results-TAG.jsonl`` / ``table-TAG.md``; the cache is
shared.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request
from collections import Counter
from pathlib import Path

from . import labels as L

GOLD4 = L.OUTCOMES + ("unknown",)
JUDGE5 = GOLD4 + ("error",)
BAR_ACC = 0.70          # a winner needs acc_vote >= this ...
BAR_LO = 0.46           # ... and a Wilson lower bound above today's judge (0.46 on gold)
CAL_TARGET = 0.75       # abstain threshold: lowest confidence bin with accuracy >= this
CAL_EDGES = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0001)


def default_out() -> Path:
    return L.home() / "judge_bench"


def variant_name(model: str, context: str, prompt: str) -> str:
    return f"{model}|{context}|{prompt}"


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _append(p: Path, row: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def gold_tasks(log=print, tag: str | None = None) -> list:
    """[(task, gold outcome)] for every gold id in force (only rows tagged ``tag`` when given),
    sorted by id. The task text is read from its transcript now and never stored."""
    rows = {r["id"]: r for r in L._read(L.home() / "tasks.jsonl")}
    gold = {k: g["outcome"] for k, g in L.gold_latest().items()
            if not k.startswith("orphan:") and k in rows and g.get("outcome") in GOLD4
            and (tag is None or g.get("tag") == tag)}
    by_session = L._session_paths()
    want: dict = {}
    for k in gold:
        want.setdefault(rows[k]["session"], set()).add(k)
    out, missing = [], 0
    for sess, ids in want.items():
        p = by_session.get(sess)
        found = {t["id"]: t for t in L.extract(p) if t["id"] in ids} if p else {}
        missing += len(ids - set(found))
        out += [(found[i], gold[i]) for i in ids if i in found]
    if missing and log:
        log(f"  {missing} gold tasks not found in transcripts (skipped)")
    return sorted(out, key=lambda x: x[0]["id"])


# Errors that are a property of the model's answer (unparseable / out-of-schema output) are
# cached like any verdict; transport errors (model missing, HTTP 4xx/5xx, timeout) are retried.
_STABLE_ERRORS = (None, "bad_output", "JSONDecodeError")


def _load_cache(p: Path) -> dict:
    return {(r["id"], r["h"]): r for r in L._read(p)
            if "id" in r and "h" in r and r.get("error") in _STABLE_ERRORS}


def run_variant(tasks: list, model: str, context: str, prompt: str, cache_dir: Path,
                call=None, max_latency: float = 20.0, log=print) -> tuple:
    """Judge every task with one variant (cache first). -> (results, status) where status is
    "ok", "skipped: slow (..)" or "skipped: load failure (..)"; results are per-task dicts."""
    call = call or L.judge_call
    cache_p = cache_dir / f"{_slug(variant_name(model, context, prompt))}.jsonl"
    cache = _load_cache(cache_p)
    results, fresh = [], []
    t0 = time.time()
    for i, (t, g) in enumerate(tasks):
        text = L.judge_prompt(t, context, prompt)
        h = _hash(text)
        r = cache.get((t["id"], h))
        if r is None:
            v, meta = call(text, model)
            r = {"id": t["id"], "h": h, "outcome": v["outcome"] if v else "error",
                 "confidence": v["confidence"] if v else None,
                 "evidence": v["evidence"] if v else None, **meta}
            _append(cache_p, r)
            fresh.append(r)
            if len(fresh) == 3 and all(x["error"] and x["error"] != "bad_output" for x in fresh):
                return results + [{**r, "gold": g}], f"skipped: load failure ({fresh[0]['error']})"
            if len(fresh) == 5:
                lat = sum(x["latency_s"] or 0 for x in fresh) / 5
                if lat > max_latency:
                    return results + [{**r, "gold": g}], f"skipped: slow ({lat:.1f} s/task)"
        results.append({**r, "gold": g})
        if log and (i + 1) % 25 == 0:
            log(f"  {variant_name(model, context, prompt)}: {i + 1}/{len(tasks)} "
                f"({time.time() - t0:.0f}s, {len(fresh)} fresh)")
    return results, "ok"


BAD_SWEEP = (0.6, 0.7, 0.8, 0.9)


def bad_sweep(results: list, min_conf: float, thresholds=BAD_SWEEP) -> list:
    """Measure (never adopt) a split threshold: success votes at ``min_conf``, fail/partial votes
    at each threshold t. Per t: recall / precision of fail, of partial, of bad (fail or partial
    voted on a fail-or-partial gold task), and the overall vote accuracy."""
    bad = ("partial", "fail")
    out = []
    for t in thresholds:
        def votes(r):
            c = r.get("confidence") or 0
            o = r["outcome"]
            return o if (o == "success" and c >= min_conf) or (o in bad and c >= t) else None
        row = {"t": t}
        for name, cls in (("fail", ("fail",)), ("partial", ("partial",)), ("bad", bad)):
            ng = sum(1 for r in results if r["gold"] in cls)
            pv = [r for r in results if votes(r) in cls]
            hit = sum(1 for r in pv if (r["gold"] in cls if name == "bad" else r["gold"] == votes(r)))
            row[name] = {"recall": round(hit / ng, 4) if ng else None, "k": hit, "n_gold": ng,
                         "n_vote": len(pv),
                         "precision": round(hit / len(pv), 4) if pv else None}
        dec = [r for r in results if r["gold"] in L.OUTCOMES and votes(r)]
        row["acc_vote"] = round(sum(votes(r) == r["gold"] for r in dec) / len(dec), 4) if dec \
            else None
        row["vote_n"] = len(dec)
        out.append(row)
    return out


def score(results: list, min_conf: float | None = None) -> dict:
    min_conf = L.JUDGE_MIN_CONF if min_conf is None else min_conf
    n = len(results)
    vk = vn = k4 = 0
    conf = Counter()
    for r in results:
        o, g, c = r["outcome"], r["gold"], r.get("confidence") or 0.0
        conf[(g, o)] += 1
        k4 += o == g
        if g in L.OUTCOMES and o in L.OUTCOMES and c >= min_conf:
            vn += 1
            vk += o == g
    n_vote = sum(1 for r in results if r["outcome"] in L.OUTCOMES
                 and (r.get("confidence") or 0) >= min_conf)
    lat = [r["latency_s"] for r in results if r.get("latency_s") is not None]
    pt = [r["prompt_tokens"] for r in results if r.get("prompt_tokens") is not None]
    et = [r["eval_tokens"] for r in results if r.get("eval_tokens") is not None]
    lo, hi = L.wilson(vk, vn)
    lo4, hi4 = L.wilson(k4, n)
    mean = (lambda xs: round(sum(xs) / len(xs), 2) if xs else None)  # noqa: E731
    ratio = (lambda k, m: round(k / m, 4) if m else None)  # noqa: E731
    per_class = {}
    for o in GOLD4:
        ng = sum(1 for r in results if r["gold"] == o)
        pr = [r for r in results if r["outcome"] == o]
        pv = [r for r in pr if o in L.OUTCOMES and (r.get("confidence") or 0) >= min_conf]
        tv = sum(1 for r in pv if r["gold"] == o)
        per_class[o] = {"n_gold": ng,
                        "recall_raw": ratio(sum(r["gold"] == o for r in pr), ng),
                        "n_pred_raw": len(pr),
                        "precision_raw": ratio(sum(r["gold"] == o for r in pr), len(pr)),
                        "recall_vote": ratio(tv, ng) if o in L.OUTCOMES else None,
                        "n_vote": len(pv),
                        "precision_vote": ratio(tv, len(pv)) if o in L.OUTCOMES else None}
    decided = [r for r in results if r["gold"] in L.OUTCOMES]
    return {"n": n, "min_conf": min_conf,
            "acc_vote": round(vk / vn, 4) if vn else None, "vote_k": vk, "vote_n": vn,
            "acc_vote_ci": [round(lo, 3), round(hi, 3)],
            "acc4": round(k4 / n, 4) if n else None, "acc4_ci": [round(lo4, 3), round(hi4, 3)],
            "abstain": round(1 - n_vote / n, 4) if n else None,
            "unknown": round(sum(r["outcome"] == "unknown" for r in results) / n, 4) if n else None,
            "error": round(sum(r["outcome"] == "error" for r in results) / n, 4) if n else None,
            "confusion": {g: {o: conf[(g, o)] for o in JUDGE5} for g in GOLD4},
            "per_class": per_class,
            "base_success": ratio(sum(r["gold"] == "success" for r in decided), len(decided)),
            "bad_sweep": bad_sweep(results, min_conf),
            "latency_s": mean(lat), "prompt_tokens": mean(pt), "eval_tokens": mean(et)}


def calibrate(results: list, target: float = CAL_TARGET) -> dict:
    """Accuracy of the judge's decided outcomes (gold decided too) by confidence bin, and the
    abstain threshold: the lowest bin edge from which EVERY non-empty bin up has accuracy >=
    ``target`` (None when no bin qualifies)."""
    bins = []
    for lo, hi in zip(CAL_EDGES, CAL_EDGES[1:]):
        xs = [r for r in results if r["gold"] in L.OUTCOMES and r["outcome"] in L.OUTCOMES
              and lo <= (r.get("confidence") or 0) < hi]
        k = sum(r["outcome"] == r["gold"] for r in xs)
        bins.append({"lo": lo, "hi": min(hi, 1.0), "n": len(xs), "k": k,
                     "acc": round(k / len(xs), 3) if xs else None})
    threshold = None
    for i, b in enumerate(bins):
        if b["n"] and all(x["acc"] >= target for x in bins[i:] if x["n"]):
            threshold = b["lo"]
            break
    return {"target": target, "bins": bins, "threshold": threshold}


def pick_winner(rows: list, bar_acc: float = BAR_ACC, bar_lo: float = BAR_LO) -> dict | None:
    """Highest acc_vote (2 dp), tie-break fewer "unknown" answers; None unless it clears the bar
    (acc_vote >= bar_acc and Wilson lower bound > bar_lo)."""
    ok = [r for r in rows if r["status"] == "ok" and r["acc_vote"] is not None]
    if not ok:
        return None
    best = max(ok, key=lambda r: (round(r["acc_vote"], 2), -r["unknown"]))
    return best if best["acc_vote"] >= bar_acc and best["acc_vote_ci"][0] > bar_lo else None


def render(rows: list) -> str:
    head = ("| variant | status | n | min_conf | acc_vote (k/n) | 95% CI | acc4 | abstain | unknown "
            "| error | s/task | prompt tok | eval tok |\n" + "|---" * 13 + "|")
    out = [head]
    for r in rows:
        a = f"{r['acc_vote']:.2f} ({r['vote_k']}/{r['vote_n']})" if r["acc_vote"] is not None \
            else "–"
        ci = f"[{r['acc_vote_ci'][0]:.2f}, {r['acc_vote_ci'][1]:.2f}]"
        f = (lambda x: "–" if x is None else f"{x:.2f}")  # noqa: E731
        out.append(f"| {r['variant']} | {r['status']} | {r['n']} | {r['min_conf']} | {a} | {ci} "
                   f"| {f(r['acc4'])} "
                   f"| {f(r['abstain'])} | {f(r['unknown'])} | {f(r['error'])} "
                   f"| {f(r['latency_s'])} | {r['prompt_tokens'] or '–'} "
                   f"| {r['eval_tokens'] or '–'} |")
    return "\n".join(out)


def render_classes(r: dict) -> str:
    f = (lambda x: "–" if x is None else f"{x:.2f}")  # noqa: E731
    out = [f"per class {r['variant']} (raw = any confidence; vote = confidence >= {r['min_conf']})"
           f" · always-success on decided gold: {f(r.get('base_success'))}",
           "| class | gold n | recall raw | precision raw (n) | recall vote | precision vote (n) |",
           "|---" * 6 + "|"]
    for o, c in (r.get("per_class") or {}).items():
        out.append(f"| {o} | {c['n_gold']} | {f(c['recall_raw'])} | {f(c['precision_raw'])} "
                   f"({c['n_pred_raw']}) | {f(c['recall_vote'])} | {f(c['precision_vote'])} "
                   f"({c['n_vote']}) |")
    return "\n".join(out)


def render_sweep(r: dict) -> str:
    f = (lambda x: "–" if x is None else f"{x:.2f}")  # noqa: E731
    out = [f"fail/partial vote threshold sweep {r['variant']} (success stays at {r['min_conf']};"
           " measured, not adopted)",
           "| t | fail recall (k/n) | fail precision (votes) | partial recall | partial precision "
           "(votes) | bad recall | bad precision (votes) | acc_vote (n) |", "|---" * 8 + "|"]
    for s in r.get("bad_sweep") or []:
        fa, pa, ba = s["fail"], s["partial"], s["bad"]
        out.append(f"| {s['t']} | {f(fa['recall'])} ({fa['k']}/{fa['n_gold']}) | "
                   f"{f(fa['precision'])} ({fa['n_vote']}) | {f(pa['recall'])} | "
                   f"{f(pa['precision'])} ({pa['n_vote']}) | {f(ba['recall'])} | "
                   f"{f(ba['precision'])} ({ba['n_vote']}) | {f(s['acc_vote'])} ({s['vote_n']}) |")
    return "\n".join(out)


def render_calibration(r: dict) -> str:
    return (f"calibration {r['variant']}: " + ", ".join(
        f"[{b['lo']:.1f},{b['hi']:.1f}) {b['k']}/{b['n']}" for b in r["calibration"]["bins"])
        + f" -> threshold {r['calibration']['threshold']}")


def render_confusion(r: dict) -> str:
    out = [f"confusion {r['variant']} (rows gold, cols judge)",
           "| gold \\ judge | " + " | ".join(JUDGE5) + " |", "|---" * (len(JUDGE5) + 1) + "|"]
    for g in GOLD4:
        out.append(f"| {g} | " + " | ".join(str(r["confusion"][g][o]) for o in JUDGE5) + " |")
    return "\n".join(out)


def _unload(model: str) -> None:
    """Ask ollama to drop the model from memory (one model on the GPU at a time)."""
    url = L.JUDGE_URL.rsplit("/api/", 1)[0] + "/api/generate"
    body = json.dumps({"model": model, "keep_alive": 0}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            url, data=body, headers={"content-type": "application/json"}), timeout=60).read()
    except Exception:  # noqa: BLE001 — best effort
        pass


def bench(models: list, contexts: list, prompts: list, out: Path | None = None, call=None,
          min_conf: float | None = None, max_latency: float = 20.0, unload=None,
          tasks: list | None = None, log=print, gold_tag: str | None = None) -> list:
    """Run every variant (models outer, so one model is loaded at a time); write
    ``results.jsonl`` (one row per variant, merged with earlier runs) and ``table.md`` (all
    variants benched so far) — ``results-TAG.jsonl`` / ``table-TAG.md`` with ``gold_tag``, so a
    holdout never overwrites the in-sample rows. -> every result row."""
    out = Path(out) if out else default_out()
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    tasks = gold_tasks(log, tag=gold_tag) if tasks is None else tasks
    sfx = f"-{_slug(gold_tag)}" if gold_tag else ""
    if log:
        log(f"judge-bench: {len(tasks)} gold tasks" + (f" (tag {gold_tag})" if gold_tag else "")
            + f" · gold {dict(Counter(g for _, g in tasks))}")
    rows = []
    for m in models:
        slow = None
        for c in contexts:
            for p in prompts:
                name = variant_name(m, c, p)
                if slow:
                    res, status = [], slow
                else:
                    res, status = run_variant(tasks, m, c, p, out / "cache", call, max_latency,
                                              log)
                    if status != "ok":
                        slow = status
                row = {"variant": name, "model": m, "context": c, "prompt": p,
                       "status": status, "ts": time.time(), "gold_tag": gold_tag,
                       **score(res, min_conf),
                       "calibration": calibrate(res)}
                rows.append(row)
                if log:
                    log(f"  {name}: {status} acc_vote {row['acc_vote']} "
                        f"({row['vote_k']}/{row['vote_n']}) acc4 {row['acc4']} "
                        f"unknown {row['unknown']} {row['latency_s']} s/task")
        if unload:
            unload(m)
    # results.jsonl accumulates across runs: this run's variants replace their earlier rows
    mine = {r["variant"] for r in rows}
    rows = [r for r in L._read(out / f"results{sfx}.jsonl")
            if r.get("variant") not in mine] + rows
    L._write(out / f"results{sfx}.jsonl", rows)
    md = [render(rows), ""]
    md += [render_confusion(r) + "\n" for r in rows if r["status"] == "ok"]
    md += [render_classes(r) + "\n" for r in rows if r["status"] == "ok" and "per_class" in r]
    if gold_tag:
        # a held-out set measures the chosen judge; selecting on it would make it in-sample
        md += [render_calibration(r) + "\n" for r in rows if r["status"] == "ok"]
        md += [render_sweep(r) + "\n" for r in rows if r["status"] == "ok" and "bad_sweep" in r]
        md.append(f"held-out gold (tag {gold_tag}): scored only, no winner is picked here")
    else:
        w = pick_winner(rows)
        md.append(f"winner (acc_vote >= {BAR_ACC}, CI lo > {BAR_LO}): "
                  + (w["variant"] if w else "no variant clears the bar"))
        if w:
            md.append(render_calibration(w).replace(f"calibration {w['variant']}",
                                                    "calibration"))
    (out / f"table{sfx}.md").write_text("\n".join(md) + "\n")
    os.chmod(out / f"table{sfx}.md", 0o600)
    return rows


def main(a) -> int:
    models = [x.strip() for x in (a.models or L.JUDGE_MODEL).split(",") if x.strip()]
    contexts = [x.strip() for x in a.contexts.split(",") if x.strip()]
    prompts = [x.strip() for x in a.prompts.split(",") if x.strip()]
    bad = [c for c in contexts if c not in L.JUDGE_CONTEXTS] + \
          [p for p in prompts if p not in L.JUDGE_PROMPTS]
    if bad:
        print(f"judge-bench: unknown context/prompt: {', '.join(bad)}")
        return 2
    out = Path(a.out) if a.out else default_out()
    tag = getattr(a, "gold_tag", None)
    bench(models, contexts, prompts, out, min_conf=a.min_conf, max_latency=a.max_latency,
          unload=_unload, gold_tag=tag)
    print((out / (f"table-{_slug(tag)}.md" if tag else "table.md")).read_text())
    return 0
