"""Selection for `apex-router explore`: hard gate, then a Pareto front over (impact, novelty, evidence).

No vote or count removes a candidate. A candidate leaves the pool only by the hard gate (oracle `valid` is
False). Ten copies of one mechanism do not outvote one different mechanism: novelty is per candidate, and
a Pareto front keeps every candidate that no other candidate beats on all three objectives.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .archive import Archive, Candidate


@dataclass(frozen=True)
class Scored:
    cand: Candidate
    impact: float     # oracle score if tested+valid; else the best valid parent score; else 0
    novelty: float    # 1 - max Jaccard(tags) vs. every other candidate in the archive
    evidence: float   # oracle-valid results in the lineage (self + ancestors)

    def objectives(self) -> tuple[float, float, float]:
        return (self.impact, self.novelty, self.evidence)


def jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def gate(archive: Archive, cands: Sequence[Candidate]) -> list[Candidate]:
    """Hard gate: drop a candidate only if the oracle tested it and said valid=False."""
    out = []
    for c in cands:
        r = archive.results.get(c.id)
        if r is not None and not r.valid:
            continue
        out.append(c)
    return out


def score(archive: Archive, cand: Candidate) -> Scored:
    r = archive.results.get(cand.id)
    if r is not None and r.valid:
        impact = float(r.score)
    else:
        ps = [archive.results[p].score for p in cand.parents
              if p in archive.results and archive.results[p].valid]
        impact = float(max(ps)) if ps else 0.0
    others = [o for o in archive.candidates.values() if o.id != cand.id]
    novelty = 1.0 - max((jaccard(cand.tags, o.tags) for o in others), default=0.0)
    ev = 0
    for cid in [cand.id] + archive.lineage(cand.id):
        rr = archive.results.get(cid)
        if rr is not None and rr.valid:
            ev += 1
    return Scored(cand, impact, novelty, float(ev))


def dominates(a: tuple, b: tuple) -> bool:
    return all(x >= y for x, y in zip(a, b)) and any(x > y for x, y in zip(a, b))


def pareto_front(items: Sequence[Scored]) -> list[Scored]:
    return [a for a in items
            if not any(dominates(b.objectives(), a.objectives()) for b in items if b is not a)]


def pareto_layers(items: Sequence[Scored]) -> list[list[Scored]]:
    rest = list(items)
    layers = []
    while rest:
        front = pareto_front(rest)
        layers.append(front)
        ids = {id(x) for x in front}
        rest = [x for x in rest if id(x) not in ids]
    return layers


def select(archive: Archive, cands: Sequence[Candidate], k: int | None = None) -> list[Scored]:
    """Gate, then Pareto layers. Inside a layer: by impact, then novelty, then id (deterministic).
    k=None returns every gated candidate (ordered); the front itself is never cut by a vote."""
    scored = [score(archive, c) for c in gate(archive, cands)]
    order: list[Scored] = []
    for layer in pareto_layers(scored):
        order.extend(sorted(layer, key=lambda s: (-s.impact, -s.novelty, s.cand.id)))
    if k is None:
        return order
    first = len(pareto_front(scored))
    return order[:max(k, first)]  # the whole first front always survives a cut


def front(archive: Archive, cands: Sequence[Candidate]) -> list[Scored]:
    scored = [score(archive, c) for c in gate(archive, cands)]
    return sorted(pareto_front(scored), key=lambda s: (-s.impact, -s.novelty, s.cand.id))
