"""Transcript mirror: keep the owner's agent transcripts after the tools prune them.

pi, Claude Code and Codex all delete old session logs, so every reader that walks the live dirs
(``labels.transcripts``, the P6 step builder) saw its dataset shrink with retention.
``snapshot`` copies every transcript file into ``<apex-router home>/transcripts/<source>/<path
relative to the source root>``; the readers walk the UNION of the live dirs and the mirror,
deduplicated per session (larger file wins), so a pruned session keeps counting and a session
present in both is counted once.

Sources (root relative to the user's home -> mirror subdir):

- ``pi``     ``.pi/agent/sessions/**/*.jsonl``
- ``claude`` ``.claude/projects/<slug>/*.jsonl`` + ``<slug>/<session>/subagents/**`` (agent
  logs, ``*.meta.json``, workflow dirs)
- ``codex``  ``.codex/sessions/**/*.jsonl`` (mirrored; no reader walks it yet)

Rules: files only grow, so a mirror copy is replaced only when it is missing, smaller, or the
same size and older; it is never deleted and never truncated (a live file SMALLER than its
mirror copy leaves the mirror alone). Copies go through a tmp file + ``os.replace``; dirs are
0700, files 0600. Fail-open per file; files over 512 MB are skipped with a note. Symlinks are
not followed.

Privacy: the mirror holds the owner's own transcripts (prompt text) in their home only. The
proxy and the widget never read it; the P6 builder reads it like the live dirs and stores no
text.
"""
from __future__ import annotations

import os
import shutil
import stat as _stat
from pathlib import Path

MAX_BYTES = 512 * 1024 * 1024
SOURCES = (("pi", (".pi", "agent", "sessions")),
           ("claude", (".claude", "projects")),
           ("codex", (".codex", "sessions")))


def base_home(home=None) -> Path:
    return Path(home) if home else Path(os.environ.get("APEX_ROUTER_HOME") or
                                        Path.home() / ".apex-router")


def mirror_root(home=None) -> Path:
    return base_home(home) / "transcripts"


def live_root(source: str, user_home=None) -> Path:
    uh = Path(user_home) if user_home else Path.home()
    return uh.joinpath(*dict(SOURCES)[source])


def source_of(path) -> str:
    """``pi`` for a live pi session or its mirror copy, else ``claude``."""
    s = str(path)
    return "pi" if ("/.pi/" in s or "/transcripts/pi/" in s) else "claude"


def _files(source: str, root: Path):
    """Files of one source root that the mirror keeps (regular files only, no symlinks)."""
    if source == "claude":
        it = [*root.glob("*/*.jsonl"), *root.glob("*/*/subagents/**/*")]
    else:
        it = root.glob("**/*.jsonl")
    for p in it:
        try:
            if p.is_symlink() or not p.is_file():
                continue
        except OSError:
            continue
        yield p


def _mkdirs(d: Path, stop: Path) -> None:
    """mkdir -p ``d`` with every created dir (and ``stop`` itself) at 0700."""
    todo = []
    cur = d
    while not cur.exists():
        todo.append(cur)
        if cur == stop or cur == cur.parent:
            break
        cur = cur.parent
    for x in reversed(todo):
        x.mkdir(mode=0o700, exist_ok=True)
        os.chmod(x, 0o700)


def _copy(src: Path, dst: Path, mtime: float) -> int:
    tmp = dst.with_name(f".{dst.name}.tmp-{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out, 1 << 20)
            n = out.tell()
        os.chmod(tmp, 0o600)
        os.utime(tmp, (mtime, mtime))
        os.replace(tmp, dst)
        return n
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def snapshot(home=None, user_home=None, dry_run: bool = False, max_bytes: int = MAX_BYTES) -> dict:
    """Copy new/grown transcript files into the mirror. Returns the counts."""
    mroot = mirror_root(home)
    res = {"mirror": str(mroot), "dry_run": dry_run, "new": 0, "updated": 0, "unchanged": 0,
           "kept_mirror_larger": 0, "skipped_large": 0, "errors": 0, "bytes": 0,
           "per_source": {}, "notes": []}
    if not dry_run:
        mroot.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(mroot, 0o700)
    for source, _ in SOURCES:
        root = live_root(source, user_home)
        ps = res["per_source"].setdefault(source, {"files": 0, "new": 0, "updated": 0})
        if not root.is_dir():
            continue
        for p in _files(source, root):
            try:
                rel = p.relative_to(root)
                st = p.stat()
                if not _stat.S_ISREG(st.st_mode):
                    continue
                ps["files"] += 1
                if st.st_size > max_bytes:
                    res["skipped_large"] += 1
                    res["notes"].append(f"skipped {source}/{rel}: {st.st_size} bytes > {max_bytes}")
                    continue
                dst = mroot / source / rel
                try:
                    mst = dst.stat()
                except FileNotFoundError:
                    mst = None
                if mst is None:
                    kind = "new"
                elif st.st_size < mst.st_size:
                    res["kept_mirror_larger"] += 1     # never truncate the mirror
                    continue
                elif st.st_size > mst.st_size or st.st_mtime > mst.st_mtime + 1e-3:
                    kind = "updated"
                else:
                    res["unchanged"] += 1
                    continue
                if dry_run:
                    n = st.st_size
                else:
                    _mkdirs(dst.parent, mroot)
                    n = _copy(p, dst, st.st_mtime)
                res[kind] += 1
                ps[kind] += 1
                res["bytes"] += n
            except Exception as e:  # noqa: BLE001 — fail open per file
                res["errors"] += 1
                if len(res["notes"]) < 50:
                    res["notes"].append(f"error {source}/{p.name}: {type(e).__name__}")
    return res


# ---- readers: union of live dirs and the mirror ---------------------------------------------

def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return -1


def _pick(cands: dict, key, p: Path) -> None:
    """Keep the larger file per key; on a tie the first seen (live) wins."""
    old = cands.get(key)
    if old is None or _size(p) > _size(old):
        cands[key] = p


def main_transcripts(user_home=None, home=None) -> list:
    """pi sessions + Claude Code main sessions from the live dirs and the mirror, one path per
    (source, session id) — the larger file wins, live on a tie."""
    mroot = mirror_root(home)
    cands: dict = {}
    for source, pattern in (("pi", "**/*.jsonl"), ("claude", "*/*.jsonl")):
        for root in (live_root(source, user_home), mroot / source):
            for p in sorted(root.glob(pattern)):
                if p.name.startswith("."):          # an in-flight tmp copy
                    continue
                _pick(cands, (source, p.stem[-36:]), p)
    return sorted(cands.values())


def sub_transcripts(user_home=None, home=None) -> list:
    """Claude Code subagent logs from the live dir and the mirror:
    [(session id, agent id, path, workflow run id | None)], one per (session, agent, workflow),
    the larger file wins (live on a tie)."""
    cands: dict = {}
    for root in (live_root("claude", user_home), mirror_root(home) / "claude"):
        for p in sorted(root.glob("*/*/subagents/**/agent-*.jsonl")):
            try:
                parts = p.relative_to(root).parts
                sid = parts[1]
            except (ValueError, IndexError):
                continue
            wf = parts[-2] if len(parts) >= 6 and parts[3] == "workflows" else None
            aid = p.name[len("agent-"):-len(".jsonl")]
            key = (sid, aid, wf)
            _pick(cands, key, p)
    out = [(k[0], k[1], cands[k], k[2]) for k in cands]
    return sorted(out, key=lambda x: (x[0], str(x[2])))
