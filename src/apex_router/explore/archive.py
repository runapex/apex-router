"""MAP-Elites style archive for `apex-router explore` (docs/research/2026-10-27-p99-discovery-harness.md, sec. 2).

Rules:
- A candidate is immutable (frozen dataclass, tuple fields). An evolution makes a NEW child with `parents`.
- Niche = (operator, mechanism fingerprint). The fingerprint comes from normalized mechanism tags, not from
  the wording of the text. Two texts with the same tags share a niche.
- One champion per niche. Only the oracle result can make a candidate a champion over another.
- An oracle result is recorded once per candidate. A second record for the same id is an error.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable

UNTAGGED = "untagged"


def normalize_tag(tag: str) -> str:
    """Lower case, keep [a-z0-9], join words with '-'. 'Binary Search' == 'binary_search' == 'binary-search '."""
    words = re.findall(r"[a-z0-9]+", str(tag).lower())
    return "-".join(words)


def normalize_tags(tags: Iterable[str]) -> tuple[str, ...]:
    """Sorted, unique, normalized tags. Empty tags are dropped."""
    return tuple(sorted({t for t in (normalize_tag(x) for x in tags) if t}))


def fingerprint(tags: Iterable[str]) -> str:
    """Mechanism fingerprint: the normalized tag set as one string. Word order and case do not matter."""
    norm = normalize_tags(tags)
    return "+".join(norm) if norm else UNTAGGED


def make_id(operator: str, text: str, parents: Iterable[str], salt: str = "") -> str:
    h = hashlib.sha256()
    for part in (operator, text, ",".join(parents), salt):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return "c" + h.hexdigest()[:12]


@dataclass(frozen=True)
class Candidate:
    id: str
    operator: str
    text: str
    tags: tuple[str, ...]
    parents: tuple[str, ...] = ()
    round: int = 0
    arm: str = "explore"

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.tags)

    @property
    def niche(self) -> tuple[str, str]:
        return (self.operator, self.fingerprint)

    @staticmethod
    def new(operator: str, text: str, tags: Iterable[str], parents: Iterable[str] = (),
            round: int = 0, arm: str = "explore", salt: str = "") -> "Candidate":
        parents = tuple(parents)
        return Candidate(id=make_id(operator, text, parents, salt), operator=operator, text=text,
                         tags=normalize_tags(tags), parents=parents, round=round, arm=arm)


@dataclass(frozen=True)
class OracleResult:
    valid: bool
    score: float
    detail: str = ""


def _rank(res: OracleResult | None) -> tuple:
    """Champion order: tested+valid by score > untested > tested+invalid."""
    if res is None:
        return (1, 0.0)
    if res.valid:
        return (2, float(res.score))
    return (0, float(res.score))


@dataclass
class Archive:
    candidates: dict[str, Candidate] = field(default_factory=dict)
    results: dict[str, OracleResult] = field(default_factory=dict)
    champions: dict[tuple[str, str], str] = field(default_factory=dict)

    # --- write -------------------------------------------------------------------------------
    def add(self, cand: Candidate) -> bool:
        """Add a candidate. Returns True if new. The same id twice is a no-op (same content, same id).
        Unknown parent ids are an error: lineage must stay closed."""
        if cand.id in self.candidates:
            if self.candidates[cand.id] != cand:
                raise ValueError(f"id collision with other content: {cand.id}")
            return False
        for p in cand.parents:
            if p not in self.candidates:
                raise KeyError(f"unknown parent id {p}")
        self.candidates[cand.id] = cand
        self._update_champion(cand.niche, cand.id)
        return True

    def record(self, cand_id: str, res: OracleResult) -> None:
        if cand_id not in self.candidates:
            raise KeyError(cand_id)
        if cand_id in self.results:
            raise ValueError(f"oracle result already recorded for {cand_id}")
        self.results[cand_id] = res
        self._update_champion(self.candidates[cand_id].niche, cand_id)

    def _update_champion(self, niche: tuple[str, str], cand_id: str) -> None:
        cur = self.champions.get(niche)
        if cur is None or _rank(self.results.get(cand_id)) > _rank(self.results.get(cur)):
            self.champions[niche] = cand_id

    # --- read --------------------------------------------------------------------------------
    def niches(self) -> list[tuple[str, str]]:
        return sorted(self.champions)

    def champion(self, niche: tuple[str, str]) -> Candidate | None:
        cid = self.champions.get(niche)
        return self.candidates.get(cid) if cid else None

    def is_tested(self, cand_id: str) -> bool:
        return cand_id in self.results

    def untested(self) -> list[Candidate]:
        return [c for c in self.candidates.values() if c.id not in self.results]

    def untested_niches(self) -> list[tuple[str, str]]:
        """Niches with no oracle result on any member."""
        tested = {self.candidates[c].niche for c in self.results}
        return [n for n in self.niches() if n not in tested]

    def valid_champions(self) -> list[Candidate]:
        out = []
        for n in self.niches():
            c = self.champion(n)
            r = self.results.get(c.id) if c else None
            if c and r and r.valid:
                out.append(c)
        return out

    def lineage(self, cand_id: str) -> list[str]:
        """All ancestor ids (breadth-first, no duplicates), not including cand_id."""
        seen: list[str] = []
        todo = list(self.candidates[cand_id].parents)
        while todo:
            p = todo.pop(0)
            if p in seen:
                continue
            seen.append(p)
            todo.extend(self.candidates[p].parents)
        return seen

    def best(self) -> tuple[Candidate, OracleResult] | None:
        best = None
        for cid, r in self.results.items():
            if r.valid and (best is None or r.score > best[1].score):
                best = (self.candidates[cid], r)
        return best
