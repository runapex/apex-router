from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]


def append_row(path: PathLike, row: dict) -> None:
    """Append `row` as one JSON line to the JSONL file at `path` (str or Path).
    Create parent directories if missing. Use an OS-level append: open with
    os.open(flags=os.O_WRONLY|os.O_CREAT|os.O_APPEND) and guard the write with
    fcntl.flock(fd, LOCK_EX) so concurrent appends don't interleave. Write
    json.dumps(row) + newline. Release the lock / close the fd in a finally."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        str(p),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o644,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.write(fd, (json.dumps(row) + "\n").encode("utf-8"))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def read_rows(path: PathLike, venue: str | None = None) -> list[dict]:
    """Read all rows from the JSONL file. Return [] if the file does not exist.
    Parse each non-blank line with json.loads; SKIP (do not raise on) any line
    that fails to parse (corrupt-line tolerant, like codeqa metrics_report).
    If `venue` is given, return only rows whose row.get('venue') == venue,
    preserving file order."""
    p = Path(path)
    if not p.is_file():
        return []
    out: list[dict] = []
    with p.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(row, dict):
                continue
            if venue is not None and row.get("venue") != venue:
                continue
            out.append(row)
    return out


def append_reward(path: PathLike, row: dict) -> None:
    """Append a BENCH REWARD row. It MUST contain an 'outcome' key; raise
    ValueError('reward row requires outcome') if not. Then delegate to append_row."""
    if "outcome" not in row:
        raise ValueError("reward row requires outcome")
    append_row(path, row)


def append_outcome(path: PathLike, row: dict) -> None:
    """Append a CONSUMER OUTCOME-LOG row (choice + observed cost/latency, NO reward).
    It MUST NOT contain 'outcome' or 'reward' keys; raise
    ValueError('outcome-log row must not carry reward') if it does.
    Then delegate to append_row."""
    if "outcome" in row or "reward" in row:
        raise ValueError("outcome-log row must not carry reward")
    append_row(path, row)
