"""Round scheduler for `apex-router explore`.

Each round has `n` model-call slots. An exploration floor (default 0.3) of each round goes to diverge
slots that target empty or untested niches. The rest evolves champions on the Pareto front
(mutate one, or combine two from different niches). With no valid champion, every slot diverges.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from . import select as sel
from .archive import Archive
from .operators import OPERATORS

DEFAULT_FLOOR = 0.3


@dataclass(frozen=True)
class Slot:
    kind: str                       # "diverge" | "mutate" | "combine"
    operator: str                   # diverge target operator, or the child's operator for evolve
    parents: tuple[str, ...] = ()


def explore_targets(archive: Archive, operators: Sequence[str]) -> list[str]:
    """Operators ordered for exploration: empty island first, then islands with an untested niche,
    then the island with the fewest candidates. Ties keep the operator order."""
    count = {op: 0 for op in operators}
    for c in archive.candidates.values():
        if c.operator in count:
            count[c.operator] += 1
    untested_ops = {n[0] for n in archive.untested_niches()}

    def key(i_op):
        i, op = i_op
        empty = count[op] == 0
        return (0 if empty else 1 if op in untested_ops else 2, count[op], i)
    return [op for _, op in sorted(enumerate(operators), key=key)]


def n_explore(n: int, floor: float) -> int:
    if not 0.0 <= floor <= 1.0:
        raise ValueError("floor must be in [0, 1]")
    return min(n, math.ceil(floor * n - 1e-9))


def plan_round(archive: Archive, n: int, floor: float = DEFAULT_FLOOR,
               operators: Sequence[str] | None = None, round_no: int = 0) -> list[Slot]:
    operators = list(operators or OPERATORS)
    if n <= 0:
        return []
    champs = archive.valid_champions()
    fr = sel.front(archive, champs)
    k_explore = n if not fr else n_explore(n, floor)
    targets = explore_targets(archive, operators)
    slots = [Slot("diverge", targets[i % len(targets)]) for i in range(k_explore)]
    for j in range(n - k_explore):
        p = fr[(j + round_no) % len(fr)].cand
        # every third exploit slot combines a front member with the best champion of another niche
        if j % 3 == 2:
            others = sorted((c for c in champs if c.niche != p.niche),
                            key=lambda c: (-archive.results[c.id].score, c.id))
            if others:
                slots.append(Slot("combine", p.operator, (p.id, others[0].id)))
                continue
        slots.append(Slot("mutate", p.operator, (p.id,)))
    return slots
