"""Claim-grounding oracle for an independent cross-validation loop.

A cross-validation loop pits two independent model reviewers against each other. This module adds a
THIRD, different KIND of check — not a third vote, a ground-truth oracle: given a finding/report's
text, it pulls every `file:line` citation, resolves each to a registered codeqa repo, and checks
DETERMINISTICALLY (file exists + cited span is within the live file) whether the citation is real.

Why deterministic, not a model call: the authoritative verdict here is a fact ("pkg/mod.py:50 exists
/ doesn't"), which is exactly the "go to ground truth on disagreement" step such a loop mandates. It
reuses codeqa's citation regex and repo registry but NEEDS no retrieval and NO model — so it can't
hallucinate, and it works even when the local answerer model is weak.

SELF-SKIP: when the text cites no code, or cites only repos codeqa doesn't know, the oracle is "not
applicable" (`applicable=False`) — an honest skip, never a fake pass.

Verdicts per citation:
  grounded      — file exists AND 1 <= start <= end <= n_lines in the live tree
  stale         — file exists but the cited span is past end-of-file (moved/shrunk) — a REAL defect
  unverified    — advisory: the path is name-owned by a repo but can't be positively located there
                  (may be a real file in an unregistered sibling repo, an unreadable file, or an
                  invented path — the filesystem alone can't tell). NEVER a rejectable problem.

Scope honesty (hardened by two independent cross-validation passes): a deterministic filesystem
oracle CANNOT reliably distinguish an invented citation from a real file that lives in an
unregistered sibling repo sharing the same name prefix (e.g. a 'foo/x.py' citation name-owned by the
registered `foo` repo but real in an unregistered `foo-ext` sibling). Asserting 'hallucinated' from
the filesystem would false-accuse such paths and sink a VALID finding — worse than a missed catch. So
this oracle does NOT emit 'hallucinated'; the invented-vs-real judgment is left to the model-side
answer-verification gate (which sees the retrieved chunks). What this oracle contributes is the two
verdicts it CAN prove: grounded and stale.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .retriever import REPOS_DIR, RepoConfig

# Same shape as codeqa's answer-citation regex: a path ending in an ALPHABETIC-initial extension,
# then :start(-end). The alpha-initial extension guard stops a "host:port" token parsing as a cite.
_CITE_RE = re.compile(r"(?P<file>[\w./-]+\.[A-Za-z]\w*):(?P<start>\d+)(?:[-–](?P<end>\d+))?")


@dataclass(frozen=True)
class GroundedCite:
    file: str
    start: int
    end: int
    repo: str
    verdict: str  # grounded | stale | unverified


@dataclass
class GroundVerdict:
    """The oracle's read of one artifact. `applicable` is False when nothing could be grounded."""
    applicable: bool
    citations: list[GroundedCite] = field(default_factory=list)

    @property
    def has_problem(self) -> bool:
        """A citation the oracle can PROVE is defective: 'stale' (file exists, cited span past EOF).
        'unverified' is deliberately EXCLUDED — it's a low-confidence advisory (possibly a real file
        in an unregistered sibling repo), never grounds to reject a finding. Not-applicable is never
        a problem either."""
        return any(c.verdict == "stale" for c in self.citations)

    @property
    def has_grounded(self) -> bool:
        return any(c.verdict == "grounded" for c in self.citations)

    def summary(self) -> str:
        if not self.applicable:
            return "codeqa grounding: not applicable (no registered-repo file:line citations)"
        g = sum(1 for c in self.citations if c.verdict == "grounded")
        s = sum(1 for c in self.citations if c.verdict == "stale")
        u = sum(1 for c in self.citations if c.verdict == "unverified")
        parts = [f"{len(self.citations)} citation(s)", f"{g} grounded"]
        if s:
            parts.append(f"{s} STALE")
        if u:
            parts.append(f"{u} unverified (advisory)")
        return "codeqa grounding: " + ", ".join(parts)


def _repo_roots() -> list[tuple[str, Path]]:
    """(name, resolved_root) for every registered repo whose root exists. Never raises on one bad
    config — a broken entry must not blind the oracle to the others."""
    out: list[tuple[str, Path]] = []
    for p in sorted(REPOS_DIR.glob("*.json")):
        try:
            cfg = RepoConfig.load(p.stem)
        except Exception:  # noqa: BLE001 — skip a broken/missing-root config, keep the rest
            continue
        out.append((cfg.name, cfg.root.resolve()))
    return out


def _within(root: Path, p: Path) -> bool:
    """True iff resolved `p` is `root` or a descendant of it — containment check that defeats '..',
    absolute paths, and symlink escape. Both sides are already .resolve()'d."""
    return p == root or root in p.parents


def _candidates_under(cite_file: str, name: str, root: Path) -> list[Path]:
    """Concrete CONTAINED candidate paths a cite denotes under `root`. Tries the path as-is
    (root/cite → for a repo nested like <root>/<pkg>/mod.py cited as 'pkg/mod.py') AND, when the
    leading segment is THIS repo's name, the stripped form (root/rest → for a repo whose root IS the
    name dir, cited as 'name/sub/file'). Every candidate is .resolve()'d and required to stay inside
    root, so '..'/absolute/symlink paths are dropped. A bare suffix that leads with ANOTHER repo's
    name is not stripped here — attribution is decided by the caller (name-prefix owner first), so a
    same-named file in a foreign repo can't sneak a match."""
    cands: list[Path] = []

    def _add(rel: str) -> None:
        try:
            p = (root / rel).resolve()  # .resolve() can raise on a symlink loop
        except (OSError, RuntimeError):  # RuntimeError: symlink loop on Python 3.11/3.12
            return
        if _within(root, p) and p not in cands:
            cands.append(p)

    _add(cite_file)
    parts = cite_file.split("/", 1)
    if parts[0] == name and len(parts) == 2:
        _add(parts[1])
    return cands


def _lead_owner(cite_file: str, repos: list[tuple[str, Path]]) -> str | None:
    """The repo whose NAME is the citation's leading segment, if any (authoritative attribution)."""
    lead = cite_file.split("/", 1)[0]
    names = [name for name, _ in repos]
    return lead if lead in names else None


def _first_existing(cands: list[Path]) -> Path | None:
    return next((c for c in cands if c.exists()), None)


def resolve_repo(cite_file: str) -> str | None:
    """Which registered repo this cited path belongs to. Name-prefix attribution wins; otherwise the
    first repo where a contained candidate exists. None when unattributable."""
    repos = _repo_roots()
    owner = _lead_owner(cite_file, repos)
    if owner is not None:
        return owner
    for name, root in repos:
        if _first_existing(_candidates_under(cite_file, name, root)):
            return name
    return None


def _grade_one(cite_file: str) -> tuple[str, Path | None, str] | None:
    """Grade one cited path → (repo, resolved_path_or_None, verdict) or None (unattributable → drop).

    Attribution is name-prefix-first, then contained-existence. Bare suffixes can't match a foreign
    repo, and '..'/absolute paths can't escape a repo root (via _within).

      grounded   : a contained candidate exists (grounded/stale refined by line count in ground_text).
      unverified : name-owned by a repo but no contained candidate exists — advisory, NEVER an
                   accusation (may be a real file in an unregistered sibling repo; the filesystem
                   can't tell it from an invented path).
      None       : no name owner and no contained existing candidate anywhere → dropped.
    """
    # Citations are repo-relative by contract. An absolute path is not attributable to a repo's
    # namespace (and could point anywhere on disk), so drop it outright rather than let it ground via
    # a coincidental in-repo suffix.
    if cite_file.startswith("/"):
        return None
    repos = _repo_roots()
    owner = _lead_owner(cite_file, repos)
    if owner is not None:
        root = dict(repos)[owner]
        cands = _candidates_under(cite_file, owner, root)
        hit = _first_existing(cands)
        if hit is not None:
            return owner, hit, "grounded"
        # Name-owned but absent. We DELIBERATELY do NOT assert 'hallucinated' from the filesystem
        # alone: a 'foo/x.py' citation is name-owned by the registered `foo` repo yet the real file
        # may live in an unregistered `foo-ext` sibling that shares the 'foo' prefix — any
        # parent-dir heuristic false-accuses it. A false 'hallucinated' would sink a VALID finding,
        # which is worse than a missed catch, so a name-owned miss is 'unverified' (advisory) and
        # the loop never rejects on it. The invented-file catch is recovered by the model-side
        # answer-verification gate, which sees the retrieved chunks and can distinguish invented
        # from merely-elsewhere.
        return owner, None, "unverified"
    for name, root in repos:  # no name owner: only speak if a CONTAINED path positively exists
        hit = _first_existing(_candidates_under(cite_file, name, root))
        if hit is not None:
            return name, hit, "grounded"
    return None


def ground_text(text: str) -> GroundVerdict:
    """Ground every file:line citation in `text` against the live registered repos. Deterministic;
    no model call, so it cannot itself hallucinate. Verdicts:

      grounded   — a candidate file exists AND 1 <= start <= end <= its line count.
      stale      — the file exists but the cited span runs past end-of-file (moved/shrunk).
      unverified — name-owned but not positively locatable (sibling repo / unreadable / invented);
                   advisory only, never a rejectable problem.

    When no citation can be graded, the verdict is not-applicable — an honest self-skip."""
    seen: set[tuple[str, int, int]] = set()
    cites: list[GroundedCite] = []
    for m in _CITE_RE.finditer(text):
        file = m.group("file")
        start = int(m.group("start"))
        end = int(m.group("end")) if m.group("end") else start
        key = (file, start, end)
        if key in seen:
            continue
        seen.add(key)
        graded = _grade_one(file)
        if graded is None:
            continue  # unattributable → drop (self-skip contributor)
        repo, path, verdict = graded
        if verdict == "grounded":
            # refine grounded vs stale by line count. A directory or an unreadable file is NOT a
            # grounded citation but also not an invented one → 'unverified' (advisory), not 'stale'
            # (an I/O error must not sink a finding by faking staleness).
            if path is None or not path.is_file():
                verdict = "unverified"
            else:
                try:
                    n_lines = sum(1 for _ in path.open("rb"))
                except OSError:
                    verdict = "unverified"
                else:
                    verdict = "grounded" if 1 <= start <= end <= n_lines else "stale"
        cites.append(GroundedCite(file, start, end, repo, verdict))
    return GroundVerdict(applicable=bool(cites), citations=cites)
