"""codeqa Phase 0 — citation verification + impact log (measure-first).

Two jobs, one mechanism:
  1. STALENESS GUARD — before an answer is trusted, check every cited file:line against the LIVE
     working tree. A `stale` cite is surfaced loudly so a delivery never presents a confidently-
     cited answer whose source has moved.
  2. IMPACT SIGNAL — the same check is the honest measure of whether grounded semantic search helps:
     an answer is 'impactful' only if its citations point at code that is actually there NOW. The
     per-query verdicts feed a grounding-accuracy metric that gates the larger dynamic-index build.

The verifier reads the LIVE file, never the chunk's stored text — verifying a chunk against its own
text is a tautology that always passes and measures nothing.

The impact log records paths / line numbers / verdicts / token counts ONLY — never source text or
the question string (only its length). Consistent with the measure-only, no-content doctrine.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .retriever import Chunk

Verdict = str  # "grounded" | "stale" | "hallucinated"

# A file:line(-span) citation as EMITTED in the model's answer text, e.g. "src/policy.py:50-66" or
# "server.py:378". The file must end in an ALPHABETIC-initial extension (so "127.0.0.1:9000" — a
# numeric final segment — is NOT a citation), and the span is `N` or `N-M` / `N–M` (en-dash too).
# Deliberately conservative to keep prose ("line 5", "3:1 ratio", host:port) out.
_CITE_RE = re.compile(
    r"(?P<file>[\w./-]+\.[A-Za-z]\w*):(?P<start>\d+)(?:[-–](?P<end>\d+))?")


@dataclass(frozen=True)
class EmittedCite:
    file: str
    start: int
    end: int

    def cite(self) -> str:
        span = f"{self.start}" if self.start == self.end else f"{self.start}-{self.end}"
        return f"{self.file}:{span}"


def parse_emitted_citations(answer_text: str) -> list[EmittedCite]:
    """Pull the file:line citations the MODEL actually emitted in its answer (Codex xval: we must
    verify what the model CITED, not what retrieval supplied). De-duplicated, order-preserving."""
    out: list[EmittedCite] = []
    seen: set[tuple[str, int, int]] = set()
    for m in _CITE_RE.finditer(answer_text):
        start = int(m.group("start"))
        end = int(m.group("end")) if m.group("end") else start
        key = (m.group("file"), start, end)
        if key in seen:
            continue
        seen.add(key)
        out.append(EmittedCite(m.group("file"), start, end))
    return out


def _same_file(cite_file: str, chunk_file: str) -> bool:
    """Whether an emitted cite path and a chunk path denote the same file. Requires a full path-
    SEGMENT suffix match, NOT a bare basename (Codex xval F2: bare-basename matching marks a same-
    named file in another dir as supplied). 'a/b/x.py' matches 'b/x.py' and 'x.py' by trailing
    segments, but 'other/x.py' does NOT match 'src/x.py'."""
    if cite_file == chunk_file:
        return True
    a, b = cite_file.split("/"), chunk_file.split("/")
    n = min(len(a), len(b))
    return a[-n:] == b[-n:]  # one path is a trailing-segment suffix of the other


def _chunk_covers(ch: Chunk, cite: EmittedCite) -> bool:
    """True iff a retrieved chunk actually SUPPLIED this emitted cite: same file AND the cite's span
    is FULLY CONTAINED in the chunk's span (Codex xval F2: mere overlap let 'x.py:1-999999' pass over
    a 5-line chunk). The cite must be a valid forward range."""
    if cite.start > cite.end:  # reversed range is malformed → not a supplied cite
        return False
    if not _same_file(cite.file, ch.file):
        return False
    return ch.start <= cite.start and cite.end <= ch.end  # full containment, not overlap


def verify_emitted_citation(repo_root: Path, cite: EmittedCite, chunks: list[Chunk]) -> Verdict:
    """Classify one MODEL-EMITTED citation (Codex xval rev 2):

      - hallucinated : the cite was NOT supplied by any retrieved chunk — the model invented a
                       file:line it was never given. (The failure grounding-of-retrieval can't catch.)
      - stale        : the cite was supplied by retrieval BUT its file:line no longer exists in the
                       live tree (file gone, or the cited span is now past end-of-file).
      - grounded     : the cite was supplied by retrieval AND the file:line exists in the live tree.

    Note the deliberate honesty limit (Codex #3): 'grounded' means the cited LOCATION is real and was
    given to the model — NOT that the code there still means what the answer says. Semantic staleness
    (same lines, changed behaviour) is beyond a file:line check and is out of scope."""
    covering = [ch for ch in chunks if _chunk_covers(ch, cite)]
    if not covering:
        return "hallucinated"  # no retrieved chunk fully supplied this cite → invented (or bad span)
    # Resolve the live file: prefer the model's path, else the covering chunk's path.
    path = Path(repo_root) / cite.file
    if not path.exists():
        path = Path(repo_root) / covering[0].file
    try:
        n_lines = len(path.read_text(errors="replace").splitlines())
    except OSError:
        return "stale"  # file gone
    # grounded iff the WHOLE cited span is within the live file (Codex xval F2: check end, not start).
    return "grounded" if 1 <= cite.start <= cite.end <= n_lines else "stale"


@dataclass
class ImpactRecord:
    """One `apex ask` outcome. NO source text and NO question string — only question_len, paths,
    line numbers (in the cites), verdicts, and token/timing counts."""
    ts: float
    repo: str
    git_head: str | None
    question_len: int
    n_chunks: int
    citations: list[dict]              # [{"cite": "file:line", "verdict": "current|moved|stale"}]
    cached_tokens: int | None
    prompt_tokens: int | None
    latency_ms: int
    digest_commits_behind: int | None
    ts_iso: str = ""                   # optional human ts; caller may fill (no clock dep here)
    _extra: dict = field(default_factory=dict)

    def grounding(self) -> dict:
        """Tally the EMITTED-citation verdicts — the citation-validity numerator/denominator."""
        g = {"grounded": 0, "stale": 0, "hallucinated": 0}
        for c in self.citations:
            v = c.get("verdict")
            if v in g:
                g[v] += 1
        return g

    def to_json_obj(self) -> dict:
        d = asdict(self)
        d.pop("_extra", None)
        d["grounding"] = self.grounding()  # derived, so a consumer needn't re-tally
        return d


def write_impact(log_path: Path, rec: ImpactRecord) -> None:
    """Append one impact record as JSONL. Fail-open (an instrument must never break the tool)."""
    try:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec.to_json_obj(), separators=(",", ":")) + "\n")
    except OSError:
        pass


def aggregate_grounding(log_path: Path) -> dict:
    """Aggregate an impact log into a CITATION-COMPLIANCE diagnostic (Codex xval: NOT an
    answer-correctness metric and NOT a causal Phase-1 gate — see the honesty limits below):

      - citation_validity = grounded / total EMITTED citations (did the model cite real code it was
        given). A wrong answer can still cite valid lines, so this is compliance, not correctness.
      - grounded / stale / hallucinated totals (hallucinated = invented cites, the key failure a
        verify-of-retrieval design cannot catch).
      - per_record: a (digest_commits_behind, validity) pair per record — a DIAGNOSTIC signal to
        *inform* the dynamic-index question, NOT to decide it. `digest_commits_behind` is a coarse,
        noisy drift proxy (counts unrelated commits, ignores a dirty tree, is None for an untracked
        digest), and records span different questions/repos — so a real go/no-go needs a controlled
        same-query fresh-vs-stale-digest A/B, which this aggregate does not perform.
    """
    grounded = stale = hallucinated = 0
    per_record: list[dict] = []
    try:
        text = Path(log_path).read_text()
    except OSError:
        return {"total_citations": 0, "grounded": 0, "stale": 0, "hallucinated": 0,
                "citation_validity": None, "per_record": []}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        g = d.get("grounding") or {}
        rg, rs, rh = g.get("grounded", 0), g.get("stale", 0), g.get("hallucinated", 0)
        grounded += rg
        stale += rs
        hallucinated += rh
        rtot = rg + rs + rh
        per_record.append({
            "digest_commits_behind": d.get("digest_commits_behind"),
            "validity": (rg / rtot) if rtot else None,
            "n_citations": rtot,
        })
    total = grounded + stale + hallucinated
    return {
        "total_citations": total,
        "grounded": grounded, "stale": stale, "hallucinated": hallucinated,
        "citation_validity": (grounded / total) if total else None,
        "per_record": per_record,
    }
