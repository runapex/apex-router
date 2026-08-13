#!/usr/bin/env python3
"""apex-router metrics export — pull-based, privacy-transparent.

Composes the EXISTING local aggregators into one redacted JSON a teammate hands back.
It reads only pre-aggregated summaries (counts, rates) — never raw prompts, file contents,
or paths — and self-checks the output for leaks before printing. No network, no daemon;
the teammate sees exactly what is shared.

Run (from the apex-router install venv so imports resolve):
    CODEQA_REPOS=~/.apex-router/codeqa/repos \
      ~/.apex-router/.venv/bin/python scripts/metrics_export.py > metrics-$(hostname -s).json

Then send that one file. It is safe to share: host is hashed, values are numeric aggregates.
Exit 2 if the built-in redaction self-check finds a leak (it prints what tripped it).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
from pathlib import Path


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:  # noqa: BLE001 — a missing/empty log must not break the export
        return {"_error": type(e).__name__}


def build_report() -> dict:
    # Import here so a partial install (e.g. no codeqa) still exports what it can.
    from apex_router.ornith import offload_report as orep
    from apex_router import route_log
    from apex_router.codeqa import doctor

    home = Path.home()
    repos_dir = Path(os.environ.get("CODEQA_REPOS") or home / ".apex-router" / "codeqa" / "repos")
    return {
        "schema": "apex-metrics/1",
        # A stable, NON-identifying host tag — a hash, never the hostname/user.
        "host": hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12],
        "codeqa_ask": _safe(orep.summarize_codeqa_impact),        # grounding + token counts
        "codeqa_validate": _safe(orep.summarize_codeqa_validate),  # local-vs-frontier routing
        "offload": _safe(orep.aggregate_offload, orep.DEFAULT_OFFLOAD_LOG),  # tokens saved by lane
        "escalation": _safe(route_log.read_rates),                 # per-task-type escalation rate
        # names + booleans + counts only — NO filesystem paths.
        "repos_health": [
            {"name": r.get("name"), "ok": r.get("ok"),
             "code_files": r.get("code_files"), "digest_ok": r.get("digest_ok")}
            for r in (_safe(doctor.repo_health, repos_dir=repos_dir) or [])
            if isinstance(r, dict)
        ],
    }


def redaction_leaks(out: str) -> list[str]:
    """Return anything that looks like a real leak in the serialized report: a filesystem
    path, an email, or any string VALUE longer than 40 chars (aggregates are short). Aggregate
    field NAMES like `prompt_tokens` are counts, not content, and are correctly not flagged."""
    leaks: list[str] = []
    leaks += re.findall(r"/(?:Users|home)/[A-Za-z0-9._-]+", out)
    leaks += re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", out)
    leaks += [v[:30] + "…" for v in re.findall(r'"([^"]{41,})"', out)]
    return leaks


def main() -> int:
    # sys.path shim so it runs from a source checkout too (installed venv doesn't need it).
    src = Path(__file__).resolve().parent.parent / "src"
    if src.is_dir():
        sys.path.insert(0, str(src))
    report = build_report()
    out = json.dumps(report, indent=2, default=str)
    leaks = redaction_leaks(out)
    if leaks:
        print(f"metrics_export: REFUSING — redaction self-check found possible leaks: {leaks}",
              file=sys.stderr)
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
