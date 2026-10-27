"""Open owner decisions (owner order 2026-10-27). The pi agent_spawn gate counts the open rows.

File: ``~/.apex-router/decisions.jsonl``. Append-only. One row: ``{id, ts, topic, text, state}``.
``state`` is ``open`` or ``closed``. The latest row per id wins. A bad row is skipped and counted, never fatal.
The text is the owner question only. Do not put task text or secrets in it.

Usage:
  apex-router decisions add TEXT [--topic T]     prints the new id
  apex-router decisions close ID
  apex-router decisions list [--all] [--json]   open rows (--all: also closed)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

STATES = ("open", "closed")
TEXT_CAP = 500
READ_CAP = 1 << 20  # bytes read from the end of the file
_TOPIC = re.compile(r"[A-Za-z0-9._-]{1,40}")
_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def path() -> Path:
    return Path.home() / ".apex-router" / "decisions.jsonl"


def _valid(r: object) -> bool:
    return (isinstance(r, dict) and isinstance(r.get("id"), str) and bool(r["id"])
            and r.get("state") in STATES)


COMPACT_AT = 256 * 1024          # writers rewrite the file (latest row per id) above this size
CLOSED_KEEP_S = 30 * 86400       # closed decisions older than this are dropped at a compaction


def read_rows() -> tuple[list[dict], int]:
    """Latest row per id (first-seen order) and the count of bad rows. Reads at most READ_CAP bytes.
    Writers keep the file far below READ_CAP (compaction under the lock), so the cap is a guard only;
    a cut file counts as one bad row, so the caller warns instead of passing in silence."""
    p = path()
    try:
        with p.open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            start = max(0, size - READ_CAP)
            f.seek(start)
            data = f.read(READ_CAP)
    except FileNotFoundError:
        return [], 0
    lines = data.decode("utf-8", "replace").split("\n")
    latest: dict[str, dict] = {}
    bad = 0
    if start > 0:
        lines = lines[1:]  # the first line is cut: drop it, and report the file as incomplete
        bad += 1
    for line in lines:
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not _valid(r):
            bad += 1
            continue
        latest.pop(r["id"], None)
        latest[r["id"]] = r
    return list(latest.values()), bad


def open_rows() -> list[dict]:
    return [r for r in read_rows()[0] if r["state"] == "open"]


class _Locked:
    """An exclusive flock on <file>.lock for one read-modify-append (ids, compaction)."""
    def __enter__(self):
        import fcntl
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(p) + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)          # closing the fd drops the lock


def _append(row: dict) -> None:
    """Append one row; the caller holds _Locked. Above COMPACT_AT the file is rewritten first with the
    latest row per id (open rows always kept; closed rows older than CLOSED_KEEP_S dropped)."""
    p = path()
    try:
        big = p.stat().st_size > COMPACT_AT
    except FileNotFoundError:
        big = False
    if big:
        rows, _bad = read_rows()
        now = time.time()
        keep = [r for r in rows if r["state"] == "open" or now - float(r.get("ts") or 0) < CLOSED_KEEP_S]
        tmp = p.with_name(p.name + f".tmp.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in keep)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)         # atomic: a reader sees the old or the new file, never a part
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode()
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)  # one write per row: concurrent writers do not interleave
    finally:
        os.close(fd)


def _clean(text: str) -> str:
    return " ".join(text.split())[:TEXT_CAP]


def add(text: str, topic: str = "general") -> str:
    t = _clean(text)
    if not t:
        raise ValueError("text is empty")
    if not _TOPIC.fullmatch(topic):
        raise ValueError("topic must match [A-Za-z0-9._-]{1,40}")
    with _Locked():
        used = {r["id"] for r in read_rows()[0]}
        did = f"D{time.strftime('%m%d')}-{secrets.token_hex(4)}"
        while did in used:
            did = f"D{time.strftime('%m%d')}-{secrets.token_hex(4)}"
        _append({"id": did, "ts": round(time.time(), 3), "topic": topic, "text": t, "state": "open"})
    return did


def close(did: str) -> str:
    """Returns 'closed', 'already' or 'unknown'."""
    if not _ID.fullmatch(did):
        return "unknown"
    with _Locked():
        cur = next((r for r in read_rows()[0] if r["id"] == did), None)
        if cur is None:
            return "unknown"
        if cur["state"] == "closed":
            return "already"
        _append({"id": did, "ts": round(time.time(), 3), "topic": cur.get("topic", "general"),
                 "text": cur.get("text", ""), "state": "closed"})
    return "closed"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="apex-router decisions", description="Open owner decisions (append-only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add an open decision; prints its id")
    a.add_argument("text")
    a.add_argument("--topic", default="general")
    c = sub.add_parser("close", help="close a decision")
    c.add_argument("id")
    ls = sub.add_parser("list", help="list open decisions")
    ls.add_argument("--all", action="store_true", help="also closed rows")
    ls.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv)
    if ns.cmd == "add":
        try:
            print(add(ns.text, ns.topic))
        except ValueError as e:
            print(f"decisions add: {e}", file=sys.stderr)
            return 2
        return 0
    if ns.cmd == "close":
        r = close(ns.id)
        if r == "unknown":
            print(f"decisions close: no decision {ns.id}", file=sys.stderr)
            return 2
        print(f"{ns.id} {'already closed' if r == 'already' else 'closed'}")
        return 0
    rows, bad = read_rows()
    if not ns.all:
        rows = [r for r in rows if r["state"] == "open"]
    if bad:
        print(f"decisions: {bad} bad row(s) skipped in {path()}", file=sys.stderr)
    if ns.json:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        for r in rows:
            print(f"{r['id']}  {r['state']:<6}  {r.get('topic', '')}  {r.get('text', '')}")
        if not rows:
            print("No open decisions." if not ns.all else "No decisions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
