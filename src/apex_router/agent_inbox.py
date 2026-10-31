"""Agent inbox in the terminal (contract: ``docs/design/AGENTS-INBOX.md`` sections 2-6; owner decisions D24, D25).

One list of all sub-agents: pi jobs, Claude Code sub-agents and Claude Code background agents. Each item is
``needs`` (you must act), ``running`` or ``done``. The blockers of a done agent stay until the owner resolves them
(``agents resolve``).

Sources (read-only, every read capped, every listing capped):
  pi-job      ``<router home>/pi-jobs/<id>/meta.json`` (<= 256 KiB) + the last 8 KiB of ``result.md``.
              The 2000 newest job dir names.
  claude-sub  ``~/.claude/projects/<slug>/<session>/subagents/agent-<id>.jsonl``: the first 64 KiB (TASK line)
              and the last 256 KiB (last assistant text, cwd, branch). The 200 newest sessions.
  claude-bg   ``~/.claude/jobs/<id>/state.json`` (<= 256 KiB): state, needs, detail, output. The 200 newest.

Output text: a label is ``commands.mask_short`` of the first ``TASK:`` line (bash rules: plain words only), or
"repo · branch". A blocker is ``commands.mask_secrets`` of one line, at most 160 characters. No prompt text, no
argv, no token, no ``last_tool`` or ``final_preview`` goes out.

Time: the whole collect has a deadline (3 s; 1 s for ``--line``). A worker thread reads; the caller waits at most
the deadline plus a short grace and then returns what is there with ``complete: false``. A stuck read can never
hang the caller.

Writes: ``agent-acks.json`` and ``agent-inbox-seen.json`` use a lock file (flock, bounded wait) and an atomic
replace (temp file in the same dir, fsync, ``os.replace``). A corrupt acks file is never overwritten.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import threading
import time
from pathlib import Path

from . import commands

DEADLINE_S = 2.4               # whole call <= 3 s: the collect deadline + join grace + start-up
LINE_DEADLINE_S = 1.0
LINE_CACHE_S = 5.0
JOIN_GRACE_S = 0.2
JOBS_MAX = 2000
SESSIONS_MAX = 200
BG_MAX = 200
SUB_FILES_MAX = 400
ITEMS_MAX = 200
NAMES_MAX = 20000
STATS_MAX = 20000
SLUG_DIRS_MAX = 5000
META_MAX = 256 * 1024
RESULT_TAIL = 8 * 1024
SUB_HEAD = 64 * 1024
SUB_TAIL = 256 * 1024
ACKS_MAX_IDS = 2000
ACK_KEYS_MAX = 200
ACKS_FILE_MAX = 4 * 1024 * 1024
SEEN_MAX = 5000
CACHE_MAX = 3000
CACHE_FILE_MAX = 8 * 1024 * 1024
OPEN_MAX = 20
LABEL_MAX = 60
TEXT_MAX = 160
NEW_LINES_MAX = 5
SUB_RUNNING_S = 120.0
LOCK_WAIT_S = 2.0
CACHE_LOCK_WAIT_S = 0.2        # the parse cache is optional: skip the save when another run holds it
CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")

ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
KEY_RE = re.compile(r"[0-9a-f]{16}")
SUB_NAME_RE = re.compile(r"agent-([A-Za-z0-9_-]{1,64})\.jsonl")
BG_SHORT_RE = re.compile(r"[0-9a-f]{8}")     # the short id that `claude attach` takes (= the ~/.claude/jobs dir)
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ROLES = ("xval", "worker", "reviewer", "scout")
NEEDS_STATES = ("failed", "capped", "blocked", "cancelled", "timed_out")
NEEDS_MAX_AGE_S = 48 * 3600   # D27 (open): an ended item older than this is "done" with stale=true, not "needs"
BAD_VERDICTS = ("NO-GO", "BLOCKED")


# ---- capped reads -----------------------------------------------------------------------------------------

def _open_regular(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, ValueError):
        return None, None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None, None
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        return None, None
    return fd, st


def _read_small(path, cap: int):
    """The whole regular file, or None (missing, not regular, larger than ``cap``)."""
    fd, st = _open_regular(path)
    if fd is None:
        return None
    try:
        if st.st_size > cap:
            return None
        return os.pread(fd, st.st_size, 0)
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_head(path, cap: int):
    fd, st = _open_regular(path)
    if fd is None:
        return None
    try:
        return os.pread(fd, min(cap, st.st_size), 0)
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_tail(path, cap: int):
    """(bytes, offset, mtime): the last ``cap`` bytes of a regular file, or None."""
    fd, st = _open_regular(path)
    if fd is None:
        return None
    try:
        off = max(0, st.st_size - cap)
        return os.pread(fd, min(cap, st.st_size), off), off, st.st_mtime
    except OSError:
        return None
    finally:
        os.close(fd)


def _tail_text(got) -> str:
    """The text of a tail read, without the first (cut) line when the read did not start at 0."""
    if not got:
        return ""
    data, off, _ = got
    if off > 0:
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else b""
    return data.decode("utf-8", "replace")


def _scan(d, cap: int = NAMES_MAX):
    """(entries, cut): at most ``cap`` directory entries, no stat."""
    out = []
    try:
        with os.scandir(d) as it:
            for e in it:
                if len(out) >= cap:
                    return out, True
                out.append(e)
    except OSError:
        return [], False
    return out, False


def _real_dir(p) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(p).st_mode)
    except OSError:
        return False


def _is_dir(e) -> bool:
    try:
        return e.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _json(raw):
    if raw is None:
        return None
    try:
        v = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return v


# ---- parse cache ------------------------------------------------------------------------------------------
# An open() costs ~0.2-0.4 ms on a Mac with endpoint security; a stat ~2 us. 2000 jobs x 2 opens did not fit in
# 3 s. The cache keeps the OUTPUT fields of an ended item (already masked: the same text the CLI prints), keyed by
# (inode, size, mtime_ns) of its files and a hash of this module + commands.py (a new mask rule drops the cache).
# A running item is never cached. The file is disposable: corrupt or old -> empty.

_CODE_TAG = None


def _code_tag() -> str:
    global _CODE_TAG
    if _CODE_TAG is None:
        h = hashlib.sha256()
        for f in (__file__, commands.__file__):
            try:
                h.update(Path(f).read_bytes())
            except OSError:
                h.update(b"?")
        _CODE_TAG = h.hexdigest()[:16]
    return _CODE_TAG


def _sig(*paths):
    out = []
    for p in paths:
        try:
            st = os.lstat(p)
            out.append([st.st_ino, st.st_size, st.st_mtime_ns] if stat.S_ISREG(st.st_mode) else None)
        except OSError:
            out.append(None)
    return out


class _Cache:
    def __init__(self, path: Path):
        self.path = path
        self.old: dict = {}
        self.new: dict = {}
        self.dirty = False
        self.lock = threading.Lock()
        d = _json(_read_small(path, CACHE_FILE_MAX))
        if isinstance(d, dict) and d.get("v") == _code_tag() and isinstance(d.get("e"), dict):
            self.old = d["e"]

    def get(self, key: str, sig):
        e = self.old.get(key)
        if isinstance(e, dict) and e.get("s") == sig and isinstance(e.get("i"), dict):
            with self.lock:
                self.new[key] = e
            return json.loads(json.dumps(e["i"]))
        return None

    def put(self, key: str, sig, item: dict) -> None:
        with self.lock:
            self.new[key] = {"s": sig, "i": json.loads(json.dumps(item))}
            self.dirty = True

    def save(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            # entries not looked at in this run stay (a deadline stop must not drop them); the ones looked at go
            # last, so the cut at CACHE_MAX drops the oldest unseen first
            merged = {k: v for k, v in self.old.items() if k not in self.new}
            merged.update(self.new)
            ent = dict(list(merged.items())[-CACHE_MAX:])
        with contextlib.suppress(OSError, TimeoutError, ValueError):
            with _locked(self.path, CACHE_LOCK_WAIT_S):     # xval x2e6: a 2 s wait broke the 3 s deadline
                _atomic_write(self.path, {"v": _code_tag(), "e": ent})


# ---- text: masking and cutting ----------------------------------------------------------------------------

_MARKERS = ("[hidden]", "<script>", "<text>")


def _cut(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    head = s[:limit - 1]
    for mk in _MARKERS:                        # never split a marker
        for k in range(len(mk) - 1, 0, -1):
            if head.endswith(mk[:k]):
                head = head[:-k]
                break
    return head.rstrip() + "…"


def _one_line(s: str) -> str:
    return " ".join(CTRL_RE.sub(" ", s).split())


def mask_line(s, limit: int = TEXT_MAX) -> str:
    """One line, ``commands.mask_secrets``, at most ``limit`` characters. The cut to 4096 characters is at a
    space before the mask, so a secret is never cut into a short piece that the mask does not know."""
    if not isinstance(s, str):
        return ""
    s = _one_line(s)
    if len(s) > 4096:
        k = s.rfind(" ", 0, 4096)
        s = s[:k] if k > 0 else ""
    s, _ = commands.mask_secrets(s)
    return _cut(s, limit)


_TASK_LINE_RE = re.compile(r"^[ \t>*#_-]*TASK:[ \t*_]*(.{0,4000})", re.M)


def _task_label(task) -> str:
    """``commands.mask_short`` (bash rules: the plain words up to the first word that is not plain) of the
    first TASK: line. "" when there is none."""
    if not isinstance(task, str):
        return ""
    m = _TASK_LINE_RE.search(task[:64 * 1024])
    if not m:
        return ""
    short, _ = commands.mask_short("bash", {"command": m.group(1)}, LABEL_MAX)
    return short.strip()


def _repo_branch(cwd_short, branch) -> str:
    parts = [p for p in (cwd_short, branch) if p]
    return _cut(" · ".join(parts), LABEL_MAX) if parts else "?"


def _cwd_short(cwd):
    if not isinstance(cwd, str) or not cwd:
        return None
    base = os.path.basename(cwd.rstrip("/")) or cwd
    return mask_line(base, 40) or None


def _branch(b):
    if not isinstance(b, str) or not b or len(b) > 200:
        return None
    return mask_line(b, 60) or None


def _resume(cwd, cmd: str, sid) -> str | None:
    """``cd <cwd> && <cmd> <sid>``. None when a part is not safe to show or run."""
    if not isinstance(cwd, str) or not cwd.startswith("/") or len(cwd) > 1024 or CTRL_RE.search(cwd):
        return None
    if not isinstance(sid, str) or not ID_RE.fullmatch(sid):
        return None
    if not UUID_RE.fullmatch(sid) and commands.mask_secrets(sid)[1]:
        return None
    if commands.mask_secrets(cwd)[1]:
        return None
    return f"cd {shlex.quote(cwd)} && {cmd} {sid}"


# ---- blockers and verdict (section 3) ---------------------------------------------------------------------

_STDERR_RE = re.compile(r"^## stderr \(tail\)[ \t]*$", re.M)
_VERDICT_WORD = r"(GO-WITH-CHANGES|NO-GO|BLOCKED|DONE|GO)(?![A-Za-z0-9-])"
_VERDICT_LABEL_RE = re.compile(r"^[ \t>#*_-]*(?i:verdict)[ \t*_]*[:\-][ \t*_]*" + _VERDICT_WORD, re.M)
_VERDICT_ONLY_RE = re.compile(_VERDICT_WORD)
_HEADER_RE = re.compile(r"(#+ job \S+ — |agent \S+ · |resume: |resumed \d+x )")


def _body(text: str) -> str:
    m = _STDERR_RE.search(text)
    return text[:m.start()] if m else text


def _norm_verdict(v: str) -> str:
    return "GO" if v == "GO-WITH-CHANGES" else v


def parse_verdict(text) -> str | None:
    """``GO | NO-GO | BLOCKED | DONE`` or None. The last ``VERDICT:`` line counts; else a line among the last 3
    non-empty lines that is only the verdict. ``GO-WITH-CHANGES`` is GO (its changes are in the blockers)."""
    if not isinstance(text, str):
        return None
    body = _body(text)
    marked = _VERDICT_LABEL_RE.findall(body)
    if marked:
        return _norm_verdict(marked[-1])
    lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not _HEADER_RE.match(ln.strip())]
    for ln in reversed(lines[-3:]):
        w = ln.strip("*_#>`- \t").rstrip(".!").strip("*_ ")
        if _VERDICT_ONLY_RE.fullmatch(w):
            return _norm_verdict(w)
    return None


_LABELS = r"(OPEN(?:[ \t]+(?:ITEMS|QUESTIONS|POINTS|ISSUES|BLOCKERS))?|BLOCKED|BLOCKERS|WARNING|WARNINGS|CAUTION)"
_FOOTER_RE = re.compile(r"^#{1,6}[ \t]+OPEN[ \t]*$")
_SEC_HEAD_RE = re.compile(r"^#{1,6}[ \t]+[*_]*(?i:" + _LABELS + r")[*_]*[ \t]*:?[ \t]*$")
_SEC_BOLD_RE = re.compile(r"^(?:\*\*|__)(?i:" + _LABELS + r")[ \t]*:?[ \t]*(?:\*\*|__)[ \t]*:?[ \t]*(.*)$")
_SEC_CAPS_RE = re.compile(r"^" + _LABELS + r"[ \t]*:[ \t]*(.*)$")
_HEADING_RE = re.compile(r"^#{1,6}[ \t]")
_BOLD_LABEL_RE = re.compile(r"^(?:\*\*|__)[^*_\n]{1,60}?(?:\*\*|__)[ \t]*(?::|$)|^(?:\*\*|__)[^*_\n]{1,60}?:[ \t]*(?:\*\*|__)")
_CAPS_LABEL_RE = re.compile(r"^[A-Z][A-Z0-9 _/-]{1,30}(?:\([^)]{0,40}\))?[ \t]*:")
_HR_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})[ \t]*$")
_LIST_RE = re.compile(r"^[ ]?(?:[-*+]|\d{1,3}[.)])[ \t]+(.*)$")        # indent >= 2: a sub-item (part of the item)
_PRI_RE = re.compile(r"^\[?(P[0-3])\]?(?:[ \t]*[:\-–—)][ \t]*|[ \t]+|$)")
_NONE_RE = re.compile(r"^(?:none|n/?a|nothing|-+|no(?:ne)?[ \t]+(?:open[ \t]+)?(?:items?|blockers?|issues?|"
                      r"questions?|open))(?:[ \t]*(?:open|left|remain(?:s|ing)?))?[ \t.!]*$", re.I)
_SEP_ROW_RE = re.compile(r"^\|?[ \t:|-]*-[ \t:|-]*$")


def _is_end(line: str) -> bool:
    s = line.strip()
    return bool(_HEADING_RE.match(s) or _HR_RE.match(s) or _BOLD_LABEL_RE.match(s) or _CAPS_LABEL_RE.match(s))


def _item(raw: str):
    """(pri, text) for one item, or None for an empty or "none" item."""
    t = raw.replace("**", "").replace("__", "").strip()
    pri = None
    m = _PRI_RE.match(t)
    if m:
        pri = "P1" if m.group(1) == "P0" else m.group(1)
        t = t[m.end():].strip()
    if not t or _NONE_RE.match(t):
        return None
    return pri, t


def _row(line: str):
    cells = [c.strip().replace("**", "").replace("__", "").strip() for c in line.strip().strip("|").split("|")]
    pri, keep = None, []
    for c in cells:
        if not c:
            continue
        m = re.fullmatch(r"\[?(P[0-3])\]?", c)
        if m and pri is None:
            pri = "P1" if m.group(1) == "P0" else m.group(1)
            continue
        keep.append(c)
    if not keep:
        return None
    got = _item(" · ".join(keep))
    return (pri or got[0], got[1]) if got else None


def _section(lines, i: int, inline: str):
    """Items of the section that starts at line ``i`` (its label line). Returns (items, next index)."""
    items, started, blank = [], False, False
    if inline.strip():
        got = _item(inline)
        if got:
            items.append(got)
        started = True
    j = i + 1
    n = len(lines)
    while j < n:
        raw = lines[j]
        s = raw.strip()
        if not s:
            blank = True
            j += 1
            continue
        if _is_end(s):
            break
        lm = _LIST_RE.match(raw)
        if lm:
            got = _item(lm.group(1))
            if got:
                items.append(got)
            started, blank = True, False
        elif s.startswith("|"):
            nxt = lines[j + 1].strip() if j + 1 < n else ""
            if not _SEP_ROW_RE.match(s) and not _SEP_ROW_RE.match(nxt or "x"):
                got = _row(s)
                if got:
                    items.append(got)
            elif _SEP_ROW_RE.match(nxt or "x") and not _SEP_ROW_RE.match(s):
                pass                                   # a header row
            started, blank = True, False
        elif raw[:1] in (" ", "\t"):                   # an indented line: part of the item above
            pass
        elif not started:
            got = _item(s)
            if got:
                items.append(got)
            started, blank = True, False
        elif blank:
            break                                      # a new paragraph after a blank line
        j += 1
    return items, j


_SEC_BARE_RE = re.compile(r"^" + _LABELS + r"[ \t]*$")


def _start(line: str, nxt: str = ""):
    """The inline text when ``line`` starts an OPEN, BLOCKED, WARNING or CAUTION section, else None. A bare
    upper-case label line ("OPEN") counts only when a list item or a table row follows it: a lone "BLOCKED"
    line is a verdict, not a section."""
    s = line.strip()
    if _SEC_HEAD_RE.match(s):
        return ""
    if _SEC_BARE_RE.match(s) and (_LIST_RE.match(nxt) or nxt.strip().startswith("|")):
        return ""
    m = _SEC_BOLD_RE.match(s) or _SEC_CAPS_RE.match(s)
    if m:
        return m.group(2) or ""
    return None


def parse_blockers(text, labels: str = "all") -> list[dict]:
    """Blockers ``[{"pri", "text"}]`` from the tail of a result. The last ``## OPEN`` footer first; else every
    OPEN, BLOCKED, WARNING or CAUTION section (``labels="open"``: OPEN sections only). Text: one line,
    ``mask_secrets``, at most 160 characters. ``## OPEN`` with no items, or ``- none``, is no blockers."""
    if not isinstance(text, str):
        return []
    lines = _body(text).splitlines()
    found = []
    foot = [k for k, ln in enumerate(lines) if _FOOTER_RE.match(ln.strip())]
    if foot:
        found, _ = _section(lines, foot[-1], "")
    else:
        k = 0
        while k < len(lines) and len(found) < OPEN_MAX:
            inline = _start(lines[k], lines[k + 1] if k + 1 < len(lines) else "")
            if inline is None or (labels == "open" and not lines[k].strip().strip("#*_ ").upper().startswith("OPEN")):
                k += 1
                continue
            got, nxt = _section(lines, k, inline)
            found.extend(got)
            k = max(nxt, k + 1)
    out, seen = [], set()
    for pri, t in found[:OPEN_MAX]:
        t = mask_line(t)
        if not t or (pri, t) in seen:
            continue
        seen.add((pri, t))
        out.append({"pri": pri, "text": t})
    return out


def _keyed(iid: str, blockers) -> list[dict]:
    out = []
    for b in blockers:
        k = hashlib.sha256(f"{iid}\0{b['pri']}\0{b['text']}".encode()).hexdigest()[:16]
        out.append({"key": k, "pri": b["pri"], "text": b["text"], "resolved": False})
    return out


# ---- sources ----------------------------------------------------------------------------------------------

def _ms(v):
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
        return None
    return round(v / 1000.0 if v > 1e11 else float(v), 3)


def _iso(s):
    if not isinstance(s, str) or not 10 <= len(s) <= 40:
        return None
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return round(d.timestamp(), 3)


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


_PI_STATES = {"done": "done", "failed": "failed", "killed(cost)": "capped", "killed": "cancelled",
              "cancelled": "cancelled", "timed_out(idle)": "timed_out", "timed_out(wall)": "timed_out",
              "timed_out": "timed_out"}


def _pi_state(meta) -> str:
    st = meta.get("state")
    if st in ("running", "queued"):
        pid = meta.get("pid")
        if st == "queued" or pid is None or _pid_alive(pid):
            return "running"
        return "failed"                                # the pid is gone: a crash left "running" behind
    out = _PI_STATES.get(st, "failed")
    if out == "done" and (meta.get("capped") or meta.get("soft_cap_at") is not None):
        return "capped"
    return out


def _pi_item(d: Path, name: str, dl) -> dict | None:
    meta = _json(_read_small(d / "meta.json", META_MAX))
    if not isinstance(meta, dict):
        return None
    state = _pi_state(meta)
    agent = meta.get("agent")
    cwd = meta.get("cwd")
    cs, br = _cwd_short(cwd), _branch(meta.get("branch"))
    usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
    cost = usage.get("cost")
    ahead = meta.get("ahead")
    it = {"id": name, "kind": "pi-job", "role": agent if agent in ROLES else "other", "group": None,
          "state": state, "verdict": None, "label": _task_label(meta.get("task")) or _repo_branch(cs, br),
          "cwd_short": cs, "branch": br,
          "ahead": ahead if isinstance(ahead, int) and not isinstance(ahead, bool) and ahead >= 0 else None,
          "start": _ms(meta.get("started")) or _ms(meta.get("created")),
          "end": None if state == "running" else _ms(meta.get("ended")),
          "cost": round(cost, 4) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
          "open": [], "resume": _resume(cwd, "pi --session-id", meta.get("session_id"))}
    if state != "running":
        if not dl.ok():
            return None
        text = _tail_text(_read_tail(d / "result.md", RESULT_TAIL))
        it["verdict"] = parse_verdict(text)
        it["open"] = _keyed(name, parse_blockers(text))
    return it


def _pi_items(rh: Path, dl, box) -> None:
    ents, cut = _scan(rh / "pi-jobs")
    if cut:
        box.incomplete()
    names = sorted((e.name for e in ents if commands.JOB_NAME_RE.fullmatch(e.name)), reverse=True)[:JOBS_MAX]
    ents = None
    for name in names:
        if not dl.ok() or box.closed:
            box.incomplete()
            return
        d = rh / "pi-jobs" / name
        if not _real_dir(d):                                # xval x2e6: a symlinked job dir left the tree
            continue
        sig = _sig(d / "meta.json", d / "result.md")
        if sig[0] is None:
            continue
        it = box.cache.get("p:" + name, sig) if box.cache else None
        if it is None:
            it = _pi_item(d, name, dl)
            if it is not None and it["state"] != "running" and box.cache:
                box.cache.put("p:" + name, sig, it)
        elif it["state"] == "running":
            it = _pi_item(d, name, dl)
        if it is None and not dl.ok():
            box.incomplete()
            return
        if it is not None:
            _add_now(it, "pi-job", dl, box)
            box.add(it)


_BG_STATES = {"running": "running", "blocked": "blocked", "done": "done", "failed": "failed",
              "error": "failed", "cancelled": "cancelled", "canceled": "cancelled", "killed": "cancelled",
              "stopped": "cancelled", "timed_out": "timed_out"}
_NEEDS_SPLIT_RE = re.compile(r"(?:^|\s)(?:\d{1,2}[.)]|[-*•])\s+")


def _split_needs(s) -> list[str]:
    if not isinstance(s, str) or not s.strip():
        return []
    s = s[:16 * 1024]
    parts = [p.strip() for p in _NEEDS_SPLIT_RE.split(s)]
    return [p for p in parts if p][:OPEN_MAX]


def _bg_item(d: Path, name: str, dl) -> dict | None:
    st = _json(_read_small(d / "state.json", META_MAX))
    if not isinstance(st, dict):
        return None
    raw = st.get("state")
    state = _BG_STATES.get(raw, "blocked") if isinstance(raw, str) else "blocked"
    needs = _split_needs(st.get("needs"))
    detail = st.get("detail")
    if state == "blocked" and isinstance(detail, str) and detail.strip():
        d1 = _split_needs(detail) or [detail]
        if not set(d1) <= set(needs):
            needs.append(detail)
    blockers, seen = [], set()
    for t in needs:
        got = _item(t)
        if got:
            m = mask_line(got[1])
            if m and m not in seen:
                seen.add(m)
                blockers.append({"pri": got[0], "text": m})
    out = st.get("output")
    if isinstance(out, dict):
        out = out.get("result")
    verdict = parse_verdict(out[-RESULT_TAIL:]) if isinstance(out, str) else None
    cwd = st.get("cwd")
    cs = _cwd_short(cwd)
    sid = st.get("resumeSessionId") or st.get("sessionId")
    return {"id": name, "kind": "claude-bg", "role": "claude", "group": None, "state": state, "verdict": verdict,
            "label": _repo_branch(cs, None), "cwd_short": cs, "branch": None, "ahead": None,
            "start": _iso(st.get("createdAt")), "end": None if state == "running" else _iso(st.get("updatedAt")),
            "cost": None, "open": _keyed(name, blockers),
            "resume": _resume(cwd, "claude attach", name) if BG_SHORT_RE.fullmatch(name) else None,
            "_sid": sid if isinstance(sid, str) and UUID_RE.fullmatch(sid) else None}


def _bg_items(home: Path, dl, box) -> None:
    root = home / ".claude" / "jobs"
    ents, cut = _scan(root)
    if cut:
        box.incomplete()
    cand = []
    for e in ents:
        if len(cand) >= STATS_MAX:
            box.incomplete()
            break
        if not ID_RE.fullmatch(e.name) or not _is_dir(e):
            continue
        try:
            mt = os.lstat(os.path.join(e.path, "state.json")).st_mtime
        except OSError:
            continue
        cand.append((mt, e.name))
    ents = None
    cand.sort(reverse=True)
    for _, name in cand[:BG_MAX]:
        if not dl.ok() or box.closed:
            box.incomplete()
            return
        sig = _sig(root / name / "state.json")
        it = box.cache.get("b:" + name, sig) if box.cache else None
        if it is None:
            it = _bg_item(root / name, name, dl)
            if it is not None and it["state"] != "running" and box.cache:
                box.cache.put("b:" + name, sig, it)
        if it is not None:
            if it["state"] == "running" and it.get("_sid"):
                _add_now(it, "claude", dl, box, agent=it["_sid"])
            box.add(it)


_SUB_TASK_RE = re.compile(rb'(?:^|\\n|")[ \t>*#_-]*TASK:[ \t*_]*((?:[^"\\\n]|\\[^n]){0,2000})')


def _sub_head(path: Path):
    """(label, start) from the first 64 KiB: the TASK: line of the first user message."""
    data = _read_head(path, SUB_HEAD)
    if not data:
        return "", None
    first = data.split(b"\n", 1)[0]
    r = _json(first)
    if isinstance(r, dict):
        m = r.get("message")
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and isinstance(b.get("text"), str))
        return (_task_label(c) if isinstance(c, str) else ""), _iso(r.get("timestamp"))
    m = _SUB_TASK_RE.search(first)
    if not m:
        return "", None
    try:
        txt = json.loads(b'"' + m.group(1).rstrip(b"\\") + b'"')
    except (ValueError, UnicodeDecodeError):
        txt = m.group(1).decode("utf-8", "replace")
    return _task_label("TASK: " + txt), None


def _sub_tail(path: Path):
    """(last assistant text, cwd, branch, session id, mtime) from the last 256 KiB."""
    got = _read_tail(path, SUB_TAIL)
    if not got:
        return "", None, None, None, None
    data, off, mtime = got
    lines = data.split(b"\n")
    if off > 0:
        lines = lines[1:]
    text, cwd, br, sid = None, None, None, None
    for ln in reversed(lines):
        if not ln.strip():
            continue
        r = _json(ln)
        if not isinstance(r, dict):
            continue
        if cwd is None and isinstance(r.get("cwd"), str):
            cwd, br, sid = r.get("cwd"), r.get("gitBranch"), r.get("sessionId")
        if text is None and r.get("type") == "assistant":
            m = r.get("message")
            c = m.get("content") if isinstance(m, dict) else None
            parts = []
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and isinstance(b.get("text"), str):
                        parts.append(b["text"])
                    elif b.get("type") == "tool_use" and b.get("name") == "SubagentHandback" \
                            and isinstance(b.get("input"), dict) and isinstance(b["input"].get("message"), str):
                        parts.append(b["input"]["message"])
            if parts:
                text = "\n".join(parts)
        if text is not None and cwd is not None:
            break
    return (text or "")[-RESULT_TAIL:], cwd, br, sid, mtime


def _sub_item(path: Path, aid: str, now: float) -> dict:
    label, start = _sub_head(path)
    text, cwd, br, sid, mtime = _sub_tail(path)
    running = isinstance(mtime, (int, float)) and now - mtime < SUB_RUNNING_S
    cs, b = _cwd_short(cwd), _branch(br)
    if not isinstance(sid, str) or not UUID_RE.fullmatch(sid):
        sid = path.parent.parent.name if UUID_RE.fullmatch(path.parent.parent.name) else None
    return {"id": aid, "kind": "claude-sub", "role": "claude", "group": None,
            "state": "running" if running else "done", "verdict": parse_verdict(text),
            "label": label or _repo_branch(cs, b), "cwd_short": cs, "branch": b, "ahead": None,
            "start": start, "end": None if running else (round(mtime, 3) if mtime else None), "cost": None,
            "open": _keyed(aid, parse_blockers(text, labels="open")),
            "resume": _resume(cwd, "claude --resume", sid)}


def _sub_items(home: Path, now: float, dl, box) -> None:
    root = home / ".claude" / "projects"
    slugs, cut = _scan(root, SLUG_DIRS_MAX)
    if cut:
        box.incomplete()
    sess, stats = [], 0
    for e in slugs:
        if not dl.ok() or box.closed:
            box.incomplete()
            return
        if not _is_dir(e):
            continue
        kids, cut = _scan(e.path)
        if cut:
            box.incomplete()
        for k in kids:
            if stats >= STATS_MAX:
                box.incomplete()
                break
            if k.name.endswith(".jsonl") or k.name.startswith(".") or not _is_dir(k):
                continue
            stats += 1
            try:
                st_sub = os.lstat(os.path.join(k.path, "subagents"))
            except OSError:
                continue
            if not stat.S_ISDIR(st_sub.st_mode):              # xval x2e6: a symlinked subagents dir
                continue
            mt = st_sub.st_mtime
            sess.append((mt, os.path.join(k.path, "subagents")))
    slugs = None
    sess.sort(reverse=True)
    files = []
    for _, sd in sess[:SESSIONS_MAX]:
        if not dl.ok() or box.closed:
            box.incomplete()
            return
        ents, cut = _scan(sd)
        if cut:
            box.incomplete()
        for f in ents:
            m = SUB_NAME_RE.fullmatch(f.name)
            if not m:
                continue
            try:
                st = f.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                files.append((st.st_mtime, m.group(1), Path(f.path), [st.st_ino, st.st_size, st.st_mtime_ns]))
    files.sort(reverse=True)
    if len(files) > SUB_FILES_MAX:
        box.incomplete()
    seen = set()
    for mt, aid, p, sig in files[:SUB_FILES_MAX]:
        if aid in seen:
            continue
        seen.add(aid)
        if not dl.ok() or box.closed:
            box.incomplete()
            return
        it = None
        if now - mt >= SUB_RUNNING_S and box.cache:
            it = box.cache.get("s:" + aid, sig)
        if it is None:
            it = _sub_item(p, aid, now)
            if it["state"] != "running" and box.cache:
                box.cache.put("s:" + aid, sig, it)
        if it["state"] == "running":
            _add_now(it, "claude-sub", dl, box)
        box.add(it)


def _add_now(it: dict, kind: str, dl, box, agent: str | None = None) -> None:
    """Rule 4: only a running item has ``now``: the newest running call (``commands.running_now``)."""
    if it["state"] != "running":
        return
    it["now"] = None
    if not dl.ok():
        box.incomplete()
        return
    try:
        r = commands.running_now(agent or it["id"], home=box.home, router_home=box.rh, now=box.now, kind=kind)
    except Exception:  # noqa: BLE001 — no call data is "no call", never an error
        r = None
    if isinstance(r, dict) and isinstance(r.get("short"), str):
        it["now"] = {"tool": r.get("tool") if isinstance(r.get("tool"), str) else "other",
                     "short": _cut(_one_line(r["short"]), LABEL_MAX)}


# ---- collect ----------------------------------------------------------------------------------------------

class _Deadline:
    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    def remaining(self) -> float:
        return self.end - time.monotonic()

    def ok(self) -> bool:
        return self.remaining() > 0


class _Box:
    def __init__(self, home, rh, now):
        self.home, self.rh, self.now = home, rh, now
        self.items: list = []
        self.complete = True
        self.closed = False
        self.cache: _Cache | None = None
        self.lock = threading.Lock()

    def add(self, it):
        with self.lock:
            if not self.closed:
                self.items.append(it)

    def incomplete(self):
        self.complete = False


def _homes(home=None, router_home=None):
    home = Path(home) if home else Path.home()
    if router_home:
        rh = Path(router_home)
    elif os.environ.get("APEX_HOME"):
        rh = Path(os.environ["APEX_HOME"])
    else:
        rh = home / ".apex-router"
    return home, rh


def _work(box: _Box, dl: _Deadline) -> None:
    try:
        box.cache = _Cache(Path(box.rh) / "agent-inbox-cache.json")      # read here: inside the deadline
    except Exception:  # noqa: BLE001
        box.cache = None
    for fn in (lambda: _bg_items(box.home, dl, box), lambda: _sub_items(box.home, box.now, dl, box),
               lambda: _pi_items(box.rh, dl, box)):
        if box.closed:
            return
        try:
            fn()
        except Exception:  # noqa: BLE001 — a broken source is "not complete", never a crash
            box.incomplete()


def _apply_acks(it: dict, acks: dict | None, now: float | None = None) -> None:
    e = (acks or {}).get(it["id"])
    keys = set(e.get("keys", ())) if isinstance(e, dict) else set()
    all_ = isinstance(e, dict) and e.get("all") is True
    for o in it["open"]:
        o["resolved"] = o["key"] in keys
    v_ok = all_ and e.get("verdict") == it["verdict"]
    s_ok = all_ and e.get("state") == it["state"]
    it["_bad_state"] = it["state"] in NEEDS_STATES and not s_ok
    it["_bad_verdict"] = it["verdict"] in BAD_VERDICTS and not v_ok
    it["stale"] = False
    if it["_bad_state"] or it["_bad_verdict"] or any(not o["resolved"] for o in it["open"]):
        t = it.get("end") or it.get("start")
        if it["state"] != "running" and now is not None and isinstance(t, (int, float)) and now - t > NEEDS_MAX_AGE_S:
            it["group"], it["stale"] = "done", True     # 522 old items on the real home hid the new ones
        else:
            it["group"] = "needs"
    elif it["state"] == "running":
        it["group"] = "running"
    else:
        it["group"] = "done"


def _order(items: list) -> list:
    def newest(i):
        t = i.get("end") or i.get("start") or 0
        return (-t, _neg(i["id"]))
    needs = sorted((i for i in items if i["group"] == "needs"), key=newest)
    run = sorted((i for i in items if i["group"] == "running"), key=lambda i: (i.get("start") or 0, i["id"]))
    done = sorted((i for i in items if i["group"] == "done"), key=newest)
    return needs + run + done


def _neg(s: str):
    return tuple(-ord(c) for c in s)


def _public(it: dict) -> dict:
    return {k: v for k, v in it.items() if not k.startswith("_")}


def _gather(home, rh, now, deadline_s):
    box = _Box(home, rh, now)
    dl = _Deadline(deadline_s)
    t = threading.Thread(target=_work, args=(box, dl), name="agent-inbox", daemon=True)
    t.start()
    t.join(max(0.0, deadline_s) + JOIN_GRACE_S)
    with box.lock:
        box.closed = True
        items = list(box.items)
    complete = box.complete and not t.is_alive()
    if box.cache is not None:
        box.cache.save()
    acks = ack_read(rh)
    for it in items:
        _apply_acks(it, acks if isinstance(acks, dict) else None, now)
    return _order(items), complete


def collect(*, home=None, router_home=None, now: float | None = None, deadline_s: float = DEADLINE_S) -> dict:
    """The ``apex-router agents --json`` document (contract section 4)."""
    now = time.time() if now is None else now
    home, rh = _homes(home, router_home)
    items, complete = _gather(home, rh, now, deadline_s)
    counts = {"running": 0, "needs": 0, "done": 0}
    for it in items:
        counts[it["group"]] += 1
    return {"now": round(now, 3), "complete": complete, "counts": counts,
            "items": [_public(i) for i in _cap(items)]}


def _cap(items: list) -> list:
    """At most ITEMS_MAX items in the section 4 order. Running items are kept first (482 old needs items on the
    real home pushed all running items out of the list); needs, then done fill the rest."""
    run = [i for i in items if i["group"] == "running"][:ITEMS_MAX]
    rest = ITEMS_MAX - len(run)
    needs = [i for i in items if i["group"] == "needs"][:rest]
    done = [i for i in items if i["group"] == "done"][:rest - len(needs)]
    return needs + run + done


def _find(iid: str, home, rh, now, deadline_s=DEADLINE_S):
    items, complete = _gather(home, rh, now, deadline_s)
    for it in items:
        if it["id"] == iid:
            return it, complete
    return None, complete


def clear_caches() -> None:
    commands.clear_caches()


# ---- writes: lock + atomic replace ------------------------------------------------------------------------

@contextlib.contextmanager
def _locked(path: Path, wait: float = LOCK_WAIT_S):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        end = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > end:
                    raise TimeoutError(f"lock busy: {path.name}")
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)


def _atomic_write(path: Path, doc) -> None:
    data = json.dumps(doc, separators=(",", ":"), sort_keys=True).encode()
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _acks_path(rh: Path) -> Path:
    return Path(rh) / "agent-acks.json"


def ack_read(rh):
    """The acks dict; {} when there is no file; None when the file is corrupt or cannot be read."""
    p = _acks_path(rh)
    if not os.path.lexists(p):
        return {}
    d = _json(_read_small(p, ACKS_FILE_MAX))
    if not isinstance(d, dict):
        return None
    out = {}
    for k, v in d.items():
        if isinstance(k, str) and ID_RE.fullmatch(k) and isinstance(v, dict):
            keys = [x for x in v.get("keys", []) if isinstance(x, str) and KEY_RE.fullmatch(x)] \
                if isinstance(v.get("keys"), list) else []
            at = v.get("at") if isinstance(v.get("at"), (int, float)) and not isinstance(v.get("at"), bool) else 0
            e = {"keys": keys[-ACK_KEYS_MAX:], "all": v.get("all") is True, "at": at}
            for f in ("verdict", "state"):
                if isinstance(v.get(f), str) and len(v[f]) <= 20:
                    e[f] = v[f]
            out[k] = e
    return out


def ack_write(rh, iid: str, *, keys=(), all_: bool = False, verdict=None, state=None, undo: bool = False,
              now: float | None = None) -> bool:
    """Add (or with ``undo`` remove) the ack of one item under a lock, with an atomic replace. At most 2000
    ids: the oldest go first. A corrupt file is not overwritten (returns False)."""
    if not isinstance(iid, str) or not ID_RE.fullmatch(iid):
        return False
    now = time.time() if now is None else now
    p = _acks_path(rh)
    with _locked(p):
        cur = ack_read(rh)
        if cur is None:
            return False
        if undo:
            if iid not in cur:
                return True
            cur.pop(iid)
        else:
            e = cur.get(iid) or {"keys": [], "all": False}
            ks = list(e.get("keys", []))
            for k in keys:
                if KEY_RE.fullmatch(k) and k not in ks:
                    ks.append(k)
            e = {"keys": ks[-ACK_KEYS_MAX:], "all": bool(e.get("all")) or all_, "at": now}
            if all_:
                e["verdict"], e["state"] = verdict, state
            else:
                old = cur.get(iid) or {}
                for f in ("verdict", "state"):
                    if f in old:
                        e[f] = old[f]
            cur.pop(iid, None)
            cur[iid] = e
            if len(cur) > ACKS_MAX_IDS:
                keep = sorted(cur, key=lambda k: (cur[k].get("at") or 0, k == iid))[-ACKS_MAX_IDS:]
                if iid not in keep:
                    keep = keep[1:] + [iid]
                cur = {k: cur[k] for k in keep}
            # xval x2e6: 1092 ids x 200 keys passed ACKS_FILE_MAX, then every read failed. Drop the oldest ids
            # (never the one just written) until the file fits in 3/4 of the read cap.
            size = len(json.dumps(cur, separators=(",", ":"), sort_keys=True))
            if size > ACKS_FILE_MAX * 3 // 4:
                for k in sorted(cur, key=lambda k: (cur[k].get("at") or 0, k)):
                    if size <= ACKS_FILE_MAX * 3 // 4:
                        break
                    if k == iid:
                        continue
                    size -= len(json.dumps({k: cur[k]}, separators=(",", ":"), sort_keys=True)) - 1
                    del cur[k]
        _atomic_write(p, cur)
    return True


# ---- surfaces ---------------------------------------------------------------------------------------------

_GROUP_MARK = {"needs": "⚠", "running": "●", "done": "✓"}


def _age(now, t):
    if not isinstance(t, (int, float)):
        return "-"
    s = max(0, int(now - t))
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def _table(doc) -> str:
    now = doc["now"]
    out = []
    c = doc["counts"]
    out.append(f"agents: ● {c['running']} running  ⚠ {c['needs']} need you  ✓ {c['done']} done"
               + ("" if doc["complete"] else "  (not complete: deadline)"))
    last = None
    for it in doc["items"]:
        if it["group"] != last:
            last = it["group"]
            out.append("")
            out.append({"needs": "NEEDS YOU", "running": "RUNNING", "done": "DONE"}[last])
        n_open = sum(1 for o in it["open"] if not o["resolved"])
        extra = []
        if it["verdict"]:
            extra.append(it["verdict"])
        if n_open:
            extra.append(f"{n_open} open")
        if it.get("now"):
            extra.append(f"{it['now']['tool']}: {it['now']['short']}")
        cost = f"${it['cost']:.2f}" if isinstance(it["cost"], (int, float)) else ""
        t = it["start"] if it["group"] == "running" else (it["end"] or it["start"])
        out.append(f"  {_GROUP_MARK[it['group']]} {it['id']:<22} {it['kind']:<10} {it['role']:<8} "
                   f"{it['state']:<9} {_age(now, t):>4} {cost:>7}  {it['label']}"
                   + (f"  [{'; '.join(extra)}]" if extra else ""))
    if len(doc["items"]) < sum(c.values()):
        out.append(f"\n({sum(c.values()) - len(doc['items'])} more not shown)")
    return "\n".join(out)


def _show(it: dict, now: float) -> str:
    out = [f"{it['id']}  ({it['kind']}, {it['role']})",
           f"  state:   {it['state']} ({it['group']})",
           f"  verdict: {it['verdict'] or '-'}",
           f"  label:   {it['label']}",
           f"  repo:    {it['cwd_short'] or '-'}   branch: {it['branch'] or '-'}"
           + (f"   ahead: {it['ahead']}" if it.get("ahead") is not None else ""),
           f"  cost:    {('$%.4f' % it['cost']) if isinstance(it['cost'], (int, float)) else '-'}"
           f"   start: {_age(now, it['start'])} ago   end: {_age(now, it['end']) + ' ago' if it['end'] else '-'}"]
    if it.get("now"):
        out.append(f"  now:     {it['now']['tool']}: {it['now']['short']}")
    out.append("  blockers:" + ("" if it["open"] else " none"))
    for o in it["open"]:
        out.append(f"    {'✓' if o['resolved'] else '⚠'} {o['key']} {o['pri'] or '--'} {o['text']}")
    out.append(f"  resume:  {it['resume'] or '-'}")
    return "\n".join(out)


def _cache_path(rh: Path) -> Path:
    return Path(rh) / "agent-inbox-line.json"


def status_line(*, home=None, router_home=None, now: float | None = None) -> str:
    """``● 2 ⚠ 3`` for the Claude Code status line; "" when nothing runs or needs you. 1 s deadline, 5 s
    cache (``agent-inbox-line.json``)."""
    now = time.time() if now is None else now
    home, rh = _homes(home, router_home)
    c = _json(_read_small(_cache_path(rh), 4096))
    if isinstance(c, dict) and isinstance(c.get("at"), (int, float)) and isinstance(c.get("line"), str) \
            and 0 <= now - c["at"] < LINE_CACHE_S:
        return c["line"]
    doc = collect(home=home, router_home=rh, now=now, deadline_s=LINE_DEADLINE_S)
    r, n = doc["counts"]["running"], doc["counts"]["needs"]
    line = f"● {r} ⚠ {n}" if (r or n) else ""
    with contextlib.suppress(OSError, TimeoutError):
        with _locked(_cache_path(rh)):
            _atomic_write(_cache_path(rh), {"at": now, "line": line})
    return line


def _seen_path(rh: Path) -> Path:
    return Path(rh) / "agent-inbox-seen.json"


def _blocker_tokens(it: dict):
    """(token, text) of each unresolved blocker of a needs item."""
    for o in it["open"]:
        if not o["resolved"]:
            yield f"{it['id']}:{o['key']}", o["text"]
    if it.get("_bad_verdict"):
        yield f"{it['id']}:verdict:{it['verdict']}", f"verdict {it['verdict']}"
    if it.get("_bad_state"):
        yield f"{it['id']}:state:{it['state']}", f"state {it['state']}"


def new_blockers(*, home=None, router_home=None, now: float | None = None) -> list[str]:
    """New unresolved blockers since the last call, at most 5 lines (Claude Code ``Stop`` hook). State file:
    ``agent-inbox-seen.json``."""
    now = time.time() if now is None else now
    home, rh = _homes(home, router_home)
    items, complete = _gather(home, rh, now, DEADLINE_S)
    cur = []
    for it in items:
        if it["group"] == "needs":
            for tok, text in _blocker_tokens(it):
                cur.append((tok, it, text))
    p = _seen_path(rh)
    try:
        with _locked(p):
            old = _json(_read_small(p, 4 * 1024 * 1024))
            seen = old.get("seen") if isinstance(old, dict) and isinstance(old.get("seen"), list) else []
            seen_set = set(x for x in seen if isinstance(x, str))
            new = [(tok, it, text) for tok, it, text in cur if tok not in seen_set]
            if complete:
                keep = [tok for tok, _, _ in cur]
            else:
                keep = [x for x in seen if isinstance(x, str)] + [tok for tok, _, _ in new]
            _atomic_write(p, {"at": now, "seen": keep[-SEEN_MAX:]})
    except (OSError, TimeoutError):
        return []
    by_item: dict = {}                                  # one line per item: its first new blocker + a count
    for tok, it, text in new:
        by_item.setdefault(it["id"], (it, text, []))[2].append(tok)
    rows = list(by_item.values())
    lines = []
    for k, (it, text, toks) in enumerate(rows):
        if k == NEW_LINES_MAX - 1 and len(rows) > NEW_LINES_MAX:
            lines.append(f"⚠ {len(rows) - k} more agents need you: apex-router agents")
            break
        more = f" (+{len(toks) - 1})" if len(toks) > 1 else ""
        lines.append(_cut(f"⚠ {it['id']} {it['label']}: {text}{more}", 200))
    return lines


# ---- CLI --------------------------------------------------------------------------------------------------

GIT_TIMEOUT_S = 1.5
_GIT_BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]{1,200}")


def _git(cwd: str, *args) -> str | None:
    import subprocess
    from .safe_proc import run_capped
    try:
        rc, out = run_capped(["git", "-C", cwd, *args], GIT_TIMEOUT_S, 4096)
    except (OSError, ValueError, subprocess.SubprocessError):    # xval 7vxt: a slow git raised TimeoutExpired
        return None
    return out.decode("utf-8", "replace").strip() if rc == 0 else None


def _git_place(it: dict, cwd) -> None:
    """Branch and commits ahead of main for one pi job worktree (``show`` only; the list stays git-free).
    Fails open: any error leaves the fields as they are."""
    if it.get("kind") != "pi-job" or not isinstance(cwd, str) or not cwd.startswith("/") or not _real_dir(cwd):
        return
    br = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if br and br != "HEAD" and _GIT_BRANCH_RE.fullmatch(br):
        it["branch"] = it.get("branch") or _branch(br)
        if br not in ("main", "master"):
            for base in ("main", "master"):
                n = _git(cwd, "rev-list", "--count", f"{base}..HEAD")
                if n is not None and n.isdigit():
                    it["ahead"] = int(n)
                    break


_AGE_RE = re.compile(r"([0-9]{1,5})([mhd])")


def _age_s(text) -> float | None:
    m = _AGE_RE.fullmatch(text or "")
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)] if m else None


def _resolve_needs_cli(a, home, rh, now) -> int:
    """Bulk resolve (polish: 43 needs items took 43 presses). Running items never match. Lists by default."""
    age = None
    if a.older_than is not None:
        age = _age_s(a.older_than)
        if age is None:
            print("agents resolve-needs: AGE is a number and m, h or d (for example 6h)", file=sys.stderr)
            return 2
    items, complete = _gather(home, rh, now, DEADLINE_S)
    hit = []
    for it in items:
        if it["group"] != "needs" or it["state"] == "running":
            continue
        if a.role and it["role"] != a.role:
            continue
        if a.verdict and it["verdict"] != a.verdict:
            continue
        t = it.get("end") or it.get("start")
        if age is not None and not (isinstance(t, (int, float)) and now - t >= age):
            continue
        hit.append(it)
    for it in hit[:20]:
        print(f"  {it['id']:<24} {it['role']:<8} {it['verdict'] or it['state']:<8} {_cut(it['label'], 60)}")
    if len(hit) > 20:
        print(f"  … {len(hit) - 20} more")
    if not a.yes:
        print(f"{len(hit)} item(s) match" + ("" if complete else " (not complete: deadline)")
              + ". Add --yes to resolve them.")
        return 0
    done = 0
    for it in hit:
        try:
            ok = ack_write(rh, it["id"], keys=[o["key"] for o in it["open"]], all_=True, verdict=it["verdict"],
                           state=it["state"], now=now)
        except (OSError, TimeoutError) as e:
            print(f"agents resolve-needs: stopped after {done} ({type(e).__name__})", file=sys.stderr)
            return 1
        if not ok:
            print(f"agents resolve-needs: stopped after {done} (agent-acks.json is corrupt)", file=sys.stderr)
            return 1
        done += 1
    print(f"resolved: {done} item(s). Undo one with: apex-router agents resolve <id> --undo")
    return 0


def _resolve_cli(a, home, rh, now) -> int:
    iid = a.id
    if not ID_RE.fullmatch(iid or ""):
        print("agents resolve: bad id", file=sys.stderr)
        return 2
    try:
        if a.undo:
            ok = ack_write(rh, iid, undo=True, now=now)
        else:
            it, complete = _find(iid, home, rh, now)
            if it is None:
                print("agents resolve: id not found" + ("" if complete else " (deadline)"), file=sys.stderr)
                return 1
            keys = [o["key"] for o in it["open"]]
            if a.key and not a.all:
                if a.key not in keys:
                    print("agents resolve: key not found", file=sys.stderr)
                    return 1
                ok = ack_write(rh, iid, keys=[a.key], now=now)
            else:
                ok = ack_write(rh, iid, keys=keys, all_=True, verdict=it["verdict"], state=it["state"], now=now)
    except (OSError, TimeoutError) as e:
        print(f"agents resolve: not written ({type(e).__name__})", file=sys.stderr)
        return 1
    if not ok:
        print("agents resolve: not written (agent-acks.json is corrupt; fix or move it)", file=sys.stderr)
        return 1
    print(f"{'undone' if a.undo else 'resolved'}: {iid}")
    return 0


RESULT_LINES = 200
RESULT_BYTES = 64 * 1024
RESULT_LINE_MAX = 400
# xval x2e6 / e2tw: a block list (brief headings, ``` fences) missed multi-line prompts, ~~~ fences and a fence
# opened before the tail. Allow-list instead: only report STRUCTURE is shown, one line each, masked: headings,
# list items, table rows, the VERDICT line. Every other line (prose, code, indented text) is dropped, never
# shown. Inline `code` spans show only when they are one plain word (a file name), else <code>.
_RESULT_KEEP_RE = re.compile(r"^(?:#{1,6} |[-*+] |[0-9]{1,3}[.)] |\|)|^\**VERDICT\b", re.I)
_PLAIN_SPAN_RE = re.compile(r"`(" + commands._PLAIN_PART + r"+)`")
_PLAIN_SPANS_LINE_RE = re.compile(r"[^`]*(?:`" + commands._PLAIN_PART + r"+`[^`]*)*")
_PROMPT_LINE_RE = re.compile(r"^[ \t>*#_|-]*(?:TASK|PROMPT|BRIEF|CONTEXT|STEPS|LIMITS)\b", re.I)


def _result_spans(ln: str) -> str:
    """Inline code, fail closed without parsing (xval yyi4, 68mv: span parsers showed commands through
    mixed backtick runs). The line keeps its backticks only when every backtick is part of a `word` pair
    with one plain word of at most 80 characters inside. Any other line is cut at its first backtick."""
    if "`" not in ln:
        return ln
    if _PLAIN_SPANS_LINE_RE.fullmatch(ln) and all(len(w) <= 80 for w in _PLAIN_SPAN_RE.findall(ln)):
        return ln
    return ln[:ln.index("`")] + "<code>"


def result_lines(rh, iid: str) -> list[str] | None:
    """The ``o`` view: the structure of a pi job result.md (last RESULT_BYTES), at most RESULT_LINES lines.
    A line shows only when it starts (with no indent) as a heading, a list item, a table row or VERDICT;
    brief lines (TASK:, PROMPT: ...) show as <text>. None when there is no such job or file."""
    if not ID_RE.fullmatch(iid) or not commands.JOB_NAME_RE.fullmatch(iid):
        return None
    d = Path(rh) / "pi-jobs" / iid
    if not _real_dir(d):
        return None
    got = _read_tail(d / "result.md", RESULT_BYTES)
    if got is None:
        return None
    out, dropped = [], 0
    for ln in _tail_text(got).split("\n"):
        if not _RESULT_KEEP_RE.match(ln):
            dropped += bool(ln.strip())
            continue
        if dropped:
            out.append(f"… {dropped} line(s) not shown")
            dropped = 0
        if _PROMPT_LINE_RE.match(ln):
            out.append("<text>")
            continue
        out.append(mask_line(_result_spans(ln), RESULT_LINE_MAX))
    if dropped:
        out.append(f"… {dropped} line(s) not shown")
    return out[-RESULT_LINES:]


def main(argv=None, *, home=None, router_home=None, now: float | None = None) -> int:
    ap = argparse.ArgumentParser(prog="apex-router agents", description="agent inbox: pi jobs, Claude Code "
                                 "sub-agents and background agents (needs you, running, done)")
    ap.add_argument("--json", action="store_true", help="the contract JSON document")
    ap.add_argument("--line", action="store_true", help="one status line: ● running ⚠ need you")
    ap.add_argument("--new-blockers", action="store_true", help="new unresolved blockers since the last call")
    sub = ap.add_subparsers(dest="cmd")
    sp = sub.add_parser("show", help="blockers, verdict, label, branch, cost and resume command of one item")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true", dest="show_json")
    xp = sub.add_parser("result", help="the last 200 lines of a pi job result.md, masked")
    xp.add_argument("id")
    rp = sub.add_parser("resolve", help="resolve one blocker key, or all (--all), or undo (--undo)")
    rp.add_argument("id")
    rp.add_argument("key", nargs="?")
    g = rp.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true")
    g.add_argument("--undo", action="store_true")
    bp = sub.add_parser("resolve-needs", help="resolve every needs item that matches the filters; without --yes "
                        "it only lists them")
    bp.add_argument("--older-than", metavar="AGE", help="ended at least AGE ago: 30m, 6h, 2d")
    bp.add_argument("--role", choices=sorted((*ROLES, "other", "claude")), help="only this role (for example xval)")
    bp.add_argument("--verdict", choices=("NO-GO", "BLOCKED"), help="only this verdict")
    bp.add_argument("--yes", action="store_true", help="write the acks (default: list only)")
    a = ap.parse_args(argv)
    home, rh = _homes(home, router_home)
    now = time.time() if now is None else now
    if a.cmd == "resolve":
        if a.key is not None and not KEY_RE.fullmatch(a.key):
            print("agents resolve: a key is 16 hex characters", file=sys.stderr)
            return 2
        return _resolve_cli(a, home, rh, now)
    if a.cmd == "resolve-needs":
        return _resolve_needs_cli(a, home, rh, now)
    if a.cmd == "result":
        lines = result_lines(rh, a.id)
        if lines is None:
            print("agents result: no result.md for this id", file=sys.stderr)
            return 1
        for ln in lines:
            print(ln)
        return 0
    if a.cmd == "show":
        if not ID_RE.fullmatch(a.id):
            print("agents show: bad id", file=sys.stderr)
            return 2
        it, complete = _find(a.id, home, rh, now)
        if it is None:
            print("agents show: id not found" + ("" if complete else " (deadline)"), file=sys.stderr)
            return 1
        if it.get("kind") == "pi-job":
            meta = _json(_read_small(Path(rh) / "pi-jobs" / it["id"] / "meta.json", META_MAX))
            _git_place(it, meta.get("cwd") if isinstance(meta, dict) else None)
        print(json.dumps(_public(it), ensure_ascii=False) if a.show_json else _show(_public(it), now))
        return 0
    if a.line:
        print(status_line(home=home, router_home=rh, now=now))
        return 0
    if a.new_blockers:
        for ln in new_blockers(home=home, router_home=rh, now=now):
            print(ln)
        return 0
    doc = collect(home=home, router_home=rh, now=now)
    print(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) if a.json else _table(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
