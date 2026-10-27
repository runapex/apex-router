#!/usr/bin/env python3
"""Backtest of preflight.rank on this machine's review history (the claim in preflight.rank).

For each xval job with at least one classified P0/P1 finding, in id (= time) order, rank the fault types
with ONLY the earlier jobs, take the top 6, and count how many of the job's real fault types are in it.
Prints the recall of the used ranking, of 2 fixed other rankings and of a seeded random 6.
Usage: PYTHONPATH=src python3 scripts/preflight_backtest.py
"""
from __future__ import annotations

import random
from collections import Counter

from apex_router import preflight as P


def main() -> None:
    jobs = sorted(P.scan_jobs(), key=lambda j: j["id"])
    names = [f[0] for f in P.FAULTS]
    tot = lambda c: sum(c.values()) or 1                       # noqa: E731
    ranks = {
        "used (2 x file share + global share)": lambda h, a: P.rank(h, a),
        "global share only": lambda h, a: [n for n, _ in a.most_common() if n != "other"][:6],
        "these files, else global": lambda h, a: [n for n, _ in (h or a).most_common() if n != "other"][:6],
    }
    hits = Counter()
    total = n = 0
    for i, j in enumerate(jobs):
        real = {k for k in j["faults"] if k != "other"}
        if j["agent"] != "xval" or not real:
            continue
        files = [P._norm(x) for x in (list(j["per_file"]) or j["files"])]
        here, allf = Counter(), Counter()
        for p in jobs[:i]:
            allf.update(p["faults"])
            for f, c in p["per_file"].items():
                if P._touches([f], files):
                    here.update(c)
        n += 1
        total += len(real)
        for name, fn in ranks.items():
            hits[name] += len(real & set(fn(here, allf)))
        random.seed(i)
        hits["random 6 of %d" % len(names)] += len(real & set(random.sample(names, 6)))
    print(f"reviews scored: {n}; real fault types: {total}")
    for name, h in hits.items():
        print(f"{h / max(total, 1):6.0%}  {name}")


if __name__ == "__main__":
    main()
