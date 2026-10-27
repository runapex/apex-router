"""External oracle for `apex-router explore`.

The oracle is a command. It gets the candidate on stdin and prints one JSON line on stdout:
{"valid": bool, "score": float, "detail": str}. Only the oracle sets `valid` and `score`.
An oracle that fails, times out, exits non-zero or prints bad JSON gives valid=False, score=0 (fail closed).
A non-zero exit wins over any JSON the oracle printed before it.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
from pathlib import Path
from typing import Sequence

from .archive import OracleResult

DETAIL_MAX = 300


def run_oracle(cmd: Sequence[str], candidate: str, cwd: str | os.PathLike | None = None,
               timeout: float = 300.0) -> OracleResult:
    try:
        p = subprocess.run(list(cmd), input=candidate, capture_output=True, text=True, timeout=timeout,
                           cwd=cwd)
    except subprocess.TimeoutExpired:
        return OracleResult(False, 0.0, "oracle timeout")
    except OSError as e:
        return OracleResult(False, 0.0, f"oracle not runnable: {type(e).__name__}")
    if p.returncode != 0:
        return OracleResult(False, 0.0, f"oracle exit {p.returncode}")
    lines = [x for x in p.stdout.splitlines() if x.strip()]
    if not lines:
        return OracleResult(False, 0.0, f"oracle printed nothing (exit {p.returncode})")
    try:
        d = json.loads(lines[-1])
        valid = d["valid"] is True
        score = float(d.get("score", 0.0))
        if not math.isfinite(score):
            raise ValueError("score not finite")
        detail = str(d.get("detail", ""))[:DETAIL_MAX]
    except (ValueError, KeyError, TypeError) as e:
        return OracleResult(False, 0.0, f"oracle bad output: {type(e).__name__}")
    return OracleResult(valid, score if valid else 0.0, detail)


def is_inside(child: Path, parent: Path) -> bool:
    child, parent = Path(child).resolve(), Path(parent).resolve()
    return child == parent or parent in child.parents


def assert_isolated(oracle_dir: Path, generator_dir: Path) -> None:
    """The generator must not see the oracle files: neither directory may contain the other."""
    if is_inside(oracle_dir, generator_dir) or is_inside(generator_dir, oracle_dir):
        raise ValueError(f"oracle dir {oracle_dir} and generator dir {generator_dir} overlap")
