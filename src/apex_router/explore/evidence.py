"""Append-only evidence store for `apex-router explore`.

One JSON object per line. The store never rewrites or truncates the file. It keeps rejections (cheap check
and oracle) and counterexamples, so a disproven mechanism goes into later prompts as negative evidence.

Negative evidence holds mechanism tags, operator and the oracle/check detail. It never holds candidate text:
a diverge prompt must not see another candidate.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

NEGATIVE_KINDS = ("check_reject", "oracle_reject", "counterexample")
DETAIL_MAX = 200


class EvidenceStore:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, kind: str, **fields) -> dict:
        row = {"ts": round(time.time(), 3), "kind": kind, **fields}
        line = json.dumps(row, sort_keys=True, default=str) + "\n"
        # O_APPEND: each write lands at the end; existing bytes are never touched.
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return row

    def rows(self, kind: str | None = None) -> list[dict]:
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if kind is None or r.get("kind") == kind:
                    out.append(r)
        return out

    def disproven_fingerprints(self) -> set[str]:
        """Fingerprints with an oracle rejection or a counterexample."""
        return {r.get("fingerprint", "") for r in self.rows()
                if r.get("kind") in ("oracle_reject", "counterexample") and r.get("fingerprint")}

    def negative(self, limit: int = 8) -> list[str]:
        """Short lines for the prompt 'negative evidence' block. Newest first, unique, no candidate text."""
        lines: list[str] = []
        for r in reversed(self.rows()):
            if r.get("kind") not in NEGATIVE_KINDS:
                continue
            tags = ", ".join(r.get("tags") or []) or "untagged"
            detail = str(r.get("detail", "")).replace("\n", " ")[:DETAIL_MAX]
            line = f"[{r['kind']}] mechanism ({tags}): {detail}"
            if line not in lines:
                lines.append(line)
            if len(lines) >= limit:
                break
        return lines
