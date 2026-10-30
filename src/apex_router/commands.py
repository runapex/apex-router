"""Commands of the agents (contract: ``docs/design/AGENTS-INBOX.md`` and the "Commands" rules; owner
decision D19): the tool calls of a pi job, a pi session, a Claude Code session or a Claude Code
sub-agent, as masked short text, plus the live process tree. Used by the terminal agent inbox
(``agent_inbox.py``, the pi overlay).

Sources (read-only, every read capped, no write):
  pi job        ``<router home>/pi-jobs/<id>/events.jsonl``: ``tool_execution_start`` (toolCallId,
                toolName, args) and ``tool_execution_end`` (isError). These events have no time; the
                time of the nearest ``message_start`` / ``message_end`` before them is used.
                ``meta.json`` (<= 256 KiB): state, pid, pid_start. Only the newest 2000 job dir
                names count (the same set as /api/workflows).
  pi session    ``~/.pi/agent/sessions/<slug>/<ts>_<id>.jsonl``: assistant ``toolCall`` blocks and
                ``toolResult`` messages.
  Claude Code   ``~/.claude/projects/<slug>/<session>.jsonl`` and
                ``<session>/subagents/agent-<id>.jsonl``: ``tool_use`` and ``tool_result`` blocks.
  processes     one ``ps -axo``; ``ps -o pid=,args=`` for interpreters only (``procs.script_label``:
                one script name, never argv); one ``lsof`` cwd call for a pi session. All through
                ``safe_proc.run_capped`` (time and output caps).

Masking (rule 2): ``mask_short`` makes the one-line text (<= 80 characters; 40 in the menu bar):
heredoc bodies and ``-c`` / ``-e`` scripts become ``<script>``, quoted text over 40 characters becomes
``<text>``, secret-like text becomes ``[hidden]``. ``mask_full`` applies the secret rule to the full
arguments (<= 64 KiB). A leading ``cd <dir> &&`` is not shown in the short text (the first command
words are the ones after it).

Call ids (rule 3): ``c-`` + 16 hex of HMAC-SHA256(studio token, agent id, event file, byte offset,
block index). ``full_call`` reads the tail again and finds the call among the newest 20 by its id;
an id of another agent, another studio or an older call finds nothing. The full text is made per
request and returned; this module keeps no command text (the only cache holds file paths).
"""
from __future__ import annotations

import calendar
import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import stat
import threading
import time
from pathlib import Path

SHORT_MAX = 80
MENU_MAX = 40
FULL_MAX = 64 * 1024
MASK_IN_MAX = FULL_MAX + 4096          # characters masked at most (cut at a space before masking)
TAIL_MAX = 256 * 1024
# pi job events.jsonl holds many message_update lines: 256 KiB often had only 6 tool calls. The read grows
# x4 from TAIL_MAX up to TAIL_GROW_MAX until it holds CALLS_MAX + 1 calls or reaches the file start.
TAIL_GROW_MAX = 4 * 1024 * 1024
CALLS_MAX = 20
JOBS_MAX = 2000
NAMES_MAX = 20000                      # names looked at per directory listing
SLUG_DIRS_MAX = 5000
SUB_STATS_MAX = 20000                  # stat calls to find a Claude Code sub-agent file
META_MAX = 256 * 1024
HEAD_MAX = 64 * 1024
PROCS_MAX = 40
PROCS_DEPTH = 3
PI_PIDS_MAX = 40
PS_OUT_MAX = 4 * 1024 * 1024
PS_TIMEOUT_S = 1.0
RUNNING_S = 3600.0                     # a session call with no result is running while the file is this fresh
START_TOLERANCE_S = 2.0
RESOLVE_CACHE_MAX = 512

ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
JOB_NAME_RE = re.compile(r"\d{14}-[a-z0-9]{1,16}")
CALL_RE = re.compile(r"c-[0-9a-f]{16}")
TOOLS = ("bash", "read", "edit", "write", "Bash", "Read", "Edit", "Write")
_FILE_TOOLS = ("read", "edit", "write")
_PATH_KEYS = ("path", "file_path", "notebook_path", "filePath")
_MARKERS = ("[hidden]", "<script>", "<text>")
_LSTART_FMT = "%a %b %d %H:%M:%S %Y"

# ---- rule 2: masking ---------------------------------------------------------------------------------

_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")         # keeps \t and \n
_SECRET_NAME_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|AUTH", re.I)
# NAME=value with a secret-like NAME: a plain value hides as one word; any other value hides to the line end.
_JSON_PAIR_RE = re.compile(r"\"([A-Za-z0-9_.-]{1,128})\"(\s{0,3}:\s{0,3})\"((?:[^\"\\\n]|\\.){0,4096})\"")
_BEARER_RE = re.compile(r"\b(Bearer)\s+[^\s'\"]{1,4096}", re.I)
_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}"
    r"|(?<![A-Za-z0-9_])(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{8,}"
    r"|(?<![A-Za-z0-9])xox[a-z]-[A-Za-z0-9-]{4,}"
    r"|(?<![A-Za-z0-9])AKIA[A-Z0-9]{8,}"
    r"|(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]*){0,2}")
# base64 (url-safe, or std without "/") or hex. "/" is NOT in the class: with it, 57 % of 875 real bash
# commands had a long PATH hidden (owner review 2026-10-29). Tokens with "/" are still caught by the
# NAME=value, Bearer and prefix rules; a long run between two "/" is still caught here.
_RUN_RE = re.compile(r"[A-Za-z0-9+_-]{32,}={0,2}")
# Allow-list (xval dt3i, 45zb, lm3c found blocklist bypasses in 3 rounds): a short text shows only PLAIN words,
# from the start of the first line to the first word that is not plain. A plain word has no quote, backslash,
# "#", "(", ")", backtick or "$" (except a $NAME or ${NAME} reference: it holds no value).
_PLAIN_PART = r"(?:[A-Za-z0-9_./:@%+,=~^*?\[\]{}!<>|&;-]|\$[A-Za-z_][A-Za-z0-9_]{0,63}(?![A-Za-z0-9_])|\$\{[A-Za-z_][A-Za-z0-9_]{0,63}\})"
_PLAIN_WORD_RE = re.compile(_PLAIN_PART + "+")
# a script flag: a single-dash word with a lowercase c or e (-c, -e, -lc, -czf, -name: over-masking is
# accepted), --eval, --command (with or without =value). Everything after it is <script>.
# Line breaks for the first line: every break that str.splitlines knows (xval 7p38: only "\n" was used).
_BREAK_RE = re.compile(r"[\n\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")
# NAME=value in any text: a plain value is one word; any other value hides to the end of the line
_PLAIN_VALUE_RE = re.compile(_PLAIN_PART + "+")
_CD_RE = re.compile(r"\s*cd[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s;&|]+)[ \t]*(?:&&|;)\s*")
_WS_RE = re.compile(r"\s+")


def _clean(s: str) -> str:
    """A control character becomes a space, never nothing: removing it joined X=1\rTOKEN=x into one word
    (xval 7khq)."""
    return _CTRL_RE.sub(" ", s)


def _cut_input(s: str, cap: int = MASK_IN_MAX) -> str:
    """At most ``cap`` characters, cut at the last space (a secret is not cut into a short piece)."""
    if len(s) <= cap:
        return s
    head = s[:cap]
    k = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t"))
    return head[:k] if k > 0 else ""


# A word ends at a blank or at a bash control character (xval o41s: X=1;TOKEN=x was one word). A quote or
# a backslash stays inside the word: a non-plain value hides the rest of the text anyway.
_TOKEN_RE = re.compile(r"[^ \t\n;&|()<>`]+")
_NEXT_EQ_RE = re.compile(r"[ \t]+=")


def _name_values(s: str) -> tuple[str, int]:
    """NAME=value with a secret-like NAME, as a check per word (xval 8ll6: a NAME regex missed TOKEN==x and a
    129-character name). A word with "=": when the text before its first "=" holds a secret-like name, the
    rest of the word is the value. "NAME =", "NAME= value" and "NAME = value" hide the next word. A value
    that is not plain (a quote, $(..), a backslash) hides the rest of the text. One pass, O(len)."""
    out, pos, n, nxt = [], 0, 0, False
    for m in _TOKEN_RE.finditer(s):
        w, a = m.group(0), m.start()
        if a < pos:
            continue
        if nxt:                                       # the word after "NAME", "NAME =" or "NAME="
            nxt = False
            if w == "=":
                nxt = True
                continue
            v0 = a + 1 if w.startswith("=") else a
            if v0 >= m.end():
                continue
        else:
            k = w.find("=")
            if k < 0:
                if _SECRET_NAME_RE.search(w):         # "NAME = value" or "NAME =value": "=" starts the next word
                    nm = _NEXT_EQ_RE.match(s, m.end())
                    if nm:
                        nxt = True                    # the "=" word is handled as the value word below
                continue
            if not _SECRET_NAME_RE.search(w[:k]):
                continue
            v0 = a + k + 1
        e = m.end()
        if v0 >= e:                                   # "NAME=" alone: the next word is the value
            nxt = True
            continue
        out.append(s[pos:v0]); out.append("[hidden]"); n += 1
        pos = e if _PLAIN_VALUE_RE.fullmatch(s, v0, e) else len(s)
    out.append(s[pos:])
    return "".join(out), n


def mask_secrets(s: str) -> tuple[str, bool]:
    """Rule 2c: secret-like text becomes ``[hidden]``. Returns (text, changed)."""
    n_all = 0

    def json_pair(m):
        nonlocal n_all
        if not _SECRET_NAME_RE.search(m.group(1)):
            return m.group(0)
        n_all += 1
        return f'"{m.group(1)}"{m.group(2)}"[hidden]"'
    s = _JSON_PAIR_RE.sub(json_pair, s)
    s, n = _name_values(s)
    n_all += n
    s, n = _BEARER_RE.subn(lambda m: m.group(1) + " [hidden]", s)
    n_all += n
    s, n = _PREFIX_RE.subn("[hidden]", s)
    n_all += n
    s, n = _RUN_RE.subn("[hidden]", s)
    n_all += n
    return s, n_all > 0


def _script_flag(w: str):
    """The text to show for a script flag word, or None. Linear (xval 7p38: a regex went quadratic).
    A single-dash word with a c, e, C or E anywhere (-c, -e, -E, -0e, -lc, -czf, -name): show up to that
    letter (xval 7p38: -cpass showed "pass"; 8ll6: -0eprint). Over-masking is accepted. --eval and --command, with or without =value: show the flag."""
    for f in ("--eval", "--command"):
        if w == f or w.startswith(f + "="):
            return f
    if len(w) < 2 or w[0] != "-" or w[1] == "-":
        return None
    for k in range(1, len(w)):                        # xval 8ll6: -0eprint stopped at "0"
        if w[k] in "ceCE":
            return w[:k + 1]
    return None


def _bash_short(raw: str) -> tuple[str, bool]:
    """Rule 2a/2b as an allow-list: the plain words of the first line, up to the first word that is not plain
    (<text>), a script flag (<script>) or the end of the line (<script> when more lines follow)."""
    m = _BREAK_RE.search(raw)
    line, more = (raw, False) if m is None else (raw[:m.start()], bool(raw[m.start():].strip()))
    out, mark = [], " <script>" if more else ""
    for w in re.split(r"[ \t]+", line):
        if not w:
            continue
        if "IFS" in w:                                # ${IFS} and IFS=.. split words at run time (xval o41s)
            mark = " <text>"
            break
        flag = _script_flag(w)
        if flag is not None:
            if _PLAIN_WORD_RE.fullmatch(flag):            # the shown part is plain too (fuzz test)
                out.append(flag)
            mark = " <script>"
            break
        if not _PLAIN_WORD_RE.fullmatch(w):
            script = more or "<<" in w or any("<<" in x for x in out)
            mark = " <script>" if script else " <text>"
            break
        out.append(w)
    return " ".join(out) + mark, bool(mark)


def _one_line(s: str) -> str:
    return _WS_RE.sub(" ", _clean(s.replace("\\\n", " "))).strip()


def _cut(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    head = s[:limit - 1]
    for mk in _MARKERS:                    # never split a marker
        for k in range(len(mk) - 1, 0, -1):
            if head.endswith(mk[:k]):
                head = head[:-k]
                break
    return head.rstrip() + "…"


def _tool_name(tool) -> str:
    name = _WS_RE.sub("", _clean(tool)) if isinstance(tool, str) else ""
    return name[:40] or "?"


def tool_out(tool) -> str:
    """The ``tool`` field: one of TOOLS, else ``other``."""
    return tool if isinstance(tool, str) and tool in TOOLS else "other"


def _kind(tool) -> str:
    t = tool.lower() if isinstance(tool, str) else ""
    return "bash" if t == "bash" else "file" if t in _FILE_TOOLS else "other"


def _path_arg(args) -> str:
    if isinstance(args, dict):
        for k in _PATH_KEYS:
            if isinstance(args.get(k), str):
                return args[k]
    return ""


def _command_arg(args) -> str:
    if isinstance(args, dict):
        c = args.get("command")
        return c if isinstance(c, str) else ""
    return args if isinstance(args, str) else ""


def mask_short(tool, args, limit: int = SHORT_MAX) -> tuple[str, bool]:
    """Rule 2: (one line of at most ``limit`` characters, masked)."""
    name, kind = _tool_name(tool), _kind(tool)
    if kind == "file":
        p = _cut_input(_clean(_path_arg(args)).replace("\n", " "), 4096).rstrip("/")
        base, masked = mask_secrets(os.path.basename(p) or p)
        return _cut(_one_line(f"{name} {base}"), limit), masked
    if kind == "other":
        text, masked = mask_secrets(name)
        return _cut(text, limit), masked
    s = _cut_input(_command_arg(args))
    s, cut = _bash_short(s)
    s, sec = mask_secrets(s)
    s = _one_line(s)
    m = _CD_RE.match(s)
    if m and m.end() < len(s):
        s = s[m.end():]
    return _cut(s, limit), bool(cut or sec)


def mask_full(tool, args) -> dict:
    """Rule 2c on the full arguments: bash ``command``; read/edit/write ``path``; other tools the
    arguments as JSON. ``{"text", "cut"}``, text <= 64 KiB (UTF-8)."""
    kind = _kind(tool)
    if kind == "bash":
        raw = _command_arg(args)
    elif kind == "file":
        raw = _path_arg(args)
    else:
        try:
            raw = json.dumps(args, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError, RecursionError):
            raw = ""
    raw = _clean(raw)
    cut = len(raw) > MASK_IN_MAX
    text, _ = mask_secrets(_cut_input(raw))
    b = text.encode("utf-8")
    if len(b) > FULL_MAX:
        text = b[:FULL_MAX].decode("utf-8", "ignore")
        cut = True
    return {"text": text, "cut": cut}


# ---- capped reads -------------------------------------------------------------------------------------

def _open_regular(path: Path):
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


def _read_tail(path: Path, cap: int):
    """(bytes, offset of the first byte, mtime) — the last ``cap`` bytes of a regular file, or None."""
    fd, st = _open_regular(path)
    if fd is None:
        return None
    try:
        off = max(0, st.st_size - cap)
        data = os.pread(fd, min(cap, st.st_size), off)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data, off, st.st_mtime


def _read_small(path: Path, cap: int):
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


def _scan(d: Path, cap: int = NAMES_MAX):
    """(entries, cut): at most ``cap`` directory entries."""
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


def _is_file(p: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(p).st_mode)
    except (OSError, ValueError):
        return False


def _is_dir(e) -> bool:
    try:
        return e.is_dir(follow_symlinks=False)
    except OSError:
        return False


# ---- finding the agent ----------------------------------------------------------------------------------

_cache: dict = {}
_cache_lock = threading.Lock()


def clear_caches() -> None:
    with _cache_lock:
        _cache.clear()


def _job_names(root: Path) -> set:
    """The newest JOBS_MAX job dir names (names only, no stat)."""
    ents, _ = _scan(root)
    names = sorted((e.name for e in ents if JOB_NAME_RE.fullmatch(e.name)), reverse=True)
    return set(names[:JOBS_MAX])


def _find_claude(home: Path, sid: str):
    ents, _ = _scan(home / ".claude" / "projects", SLUG_DIRS_MAX)
    for e in ents:
        if _is_dir(e):
            p = Path(e.path) / f"{sid}.jsonl"
            if _is_file(p):
                return p
    return None


def _find_pi(home: Path, sid: str):
    ents, _ = _scan(home / ".pi" / "agent" / "sessions", SLUG_DIRS_MAX)
    tail = f"_{sid}.jsonl"
    for e in ents:
        if not _is_dir(e):
            continue
        files, _ = _scan(Path(e.path))
        for f in files:
            if (f.name.endswith(tail) or f.name == f"{sid}.jsonl") and _is_file(Path(f.path)):
                return Path(f.path)
    return None


def _find_sub(home: Path, aid: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", aid):
        return None
    name = f"agent-{aid}.jsonl"
    ents, _ = _scan(home / ".claude" / "projects", SLUG_DIRS_MAX)
    stats = 0
    for e in ents:
        if not _is_dir(e):
            continue
        p = Path(e.path) / "subagents" / name
        stats += 1
        if _is_file(p):
            return p
        sess, _ = _scan(Path(e.path))
        for s in sess:
            if stats >= SUB_STATS_MAX:
                return None
            if not s.name.endswith(".jsonl") or s.name == name:
                continue
            p = Path(e.path) / s.name[:-6] / "subagents" / name
            stats += 1
            if _is_file(p):
                return p
    return None


def resolve(home: Path, jobs_root: Path, agent: str, kind: str | None = None, listed: bool = False):
    """``{"kind", "path"}`` (+ ``meta`` for a job) for an agent id, or None. Order: pi job, Claude
    Code session, pi session, Claude Code sub-agent. ``listed``: the caller found the job id in its
    own bounded listing (the menu bar), so the newest-2000 listing is not done again."""
    if not isinstance(agent, str) or not ID_RE.fullmatch(agent):
        return None
    home, jobs_root = Path(home), Path(jobs_root)
    if (kind in (None, "pi-job")) and JOB_NAME_RE.fullmatch(agent) \
            and (listed or agent in _job_names(jobs_root)):
        d = jobs_root / agent
        raw = _read_small(d / "meta.json", META_MAX)
        try:
            meta = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError, RecursionError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if raw is not None or _is_file(d / "events.jsonl"):
            return {"kind": "pi-job", "path": d / "events.jsonl", "meta": meta}
    if kind == "pi-job":
        return None
    key = (str(home), agent)
    with _cache_lock:
        hit = _cache.get(key)
    if hit and (kind is None or hit[0] == kind) and _is_file(hit[1]):
        return {"kind": hit[0], "path": hit[1]}
    found = None
    for k, fn in (("claude", _find_claude), ("pi", _find_pi), ("claude-sub", _find_sub)):
        if kind not in (None, k):
            continue
        p = fn(home, agent)
        if p is not None:
            found = (k, p)
            break
    if found is None:
        return None
    with _cache_lock:
        _cache[key] = found
        while len(_cache) > RESOLVE_CACHE_MAX:
            _cache.pop(next(iter(_cache)))
    return {"kind": found[0], "path": found[1]}


# ---- rule 1: the readers --------------------------------------------------------------------------------

def _ms_ts(v):
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
        return None
    return v / 1000.0 if v > 1e11 else float(v)


def _iso_ts(s):
    if not isinstance(s, str) or not 10 <= len(s) <= 40:
        return None
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp()


_PI_JOB_WANT = (b"tool_execution_start", b"tool_execution_end", b"message_start", b"message_end")
_PI_WANT = (b'"toolCall"', b'"toolResult"')
_CLAUDE_WANT = (b'"tool_use"', b'"tool_result"')


def _lines(data: bytes, base: int, want):
    """(offset, parsed row) of the complete lines that hold one of ``want``."""
    start = 0
    if base > 0:
        nl = data.find(b"\n")
        start = nl + 1 if nl >= 0 else len(data)
    n = len(data)
    while start < n:
        nl = data.find(b"\n", start)
        end = nl if nl >= 0 else n
        ln = data[start:end]
        if ln and any(w in ln for w in want):
            try:
                r = json.loads(ln)
            except (ValueError, UnicodeDecodeError, RecursionError):
                r = None
            if isinstance(r, dict):
                yield base + start, r
        start = end + 1


def _parse_pi_job(data, base):
    calls, by_id, pending, last_ts = [], {}, [], None
    for off, r in _lines(data, base, _PI_JOB_WANT):
        t = r.get("type")
        if t in ("message_start", "message_end"):
            m = r.get("message")
            ts = _ms_ts(m.get("timestamp")) if isinstance(m, dict) else None
            if ts is not None:
                last_ts = ts
                for c in pending:
                    c["end"] = ts
                pending = []
        elif t == "tool_execution_start":
            c = {"off": off, "i": 0, "tool": r.get("toolName"), "args": r.get("args"), "start": last_ts,
                 "end": None, "state": None}
            calls.append(c)
            cid = r.get("toolCallId")
            if isinstance(cid, str):
                by_id[cid] = c
        elif t == "tool_execution_end":
            c = by_id.get(r.get("toolCallId"))
            if c is not None and c["state"] is None:
                c["state"] = "error" if r.get("isError") else "ok"
                pending.append(c)
    return calls, pending


def _parse_pi(data, base):
    calls, by_id = [], {}
    for off, r in _lines(data, base, _PI_WANT):
        m = r.get("message")
        if r.get("type") != "message" or not isinstance(m, dict):
            continue
        ts = _iso_ts(r.get("timestamp"))
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            for i, b in enumerate(m["content"]):
                if isinstance(b, dict) and b.get("type") == "toolCall":
                    c = {"off": off, "i": i, "tool": b.get("name"), "args": b.get("arguments"), "start": ts,
                         "end": None, "state": None}
                    calls.append(c)
                    if isinstance(b.get("id"), str):
                        by_id[b["id"]] = c
        elif m.get("role") == "toolResult":
            c = by_id.get(m.get("toolCallId"))
            if c is not None and c["state"] is None:
                c["state"] = "error" if m.get("isError") else "ok"
                c["end"] = _ms_ts(m.get("timestamp")) or ts
    return calls, []


def _parse_claude(data, base):
    calls, by_id = [], {}
    for off, r in _lines(data, base, _CLAUDE_WANT):
        m = r.get("message")
        if not isinstance(m, dict) or not isinstance(m.get("content"), list):
            continue
        ts = _iso_ts(r.get("timestamp"))
        for i, b in enumerate(m["content"]):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and r.get("type") == "assistant":
                c = {"off": off, "i": i, "tool": b.get("name"), "args": b.get("input"), "start": ts,
                     "end": None, "state": None}
                calls.append(c)
                if isinstance(b.get("id"), str):
                    by_id[b["id"]] = c
            elif b.get("type") == "tool_result":
                c = by_id.get(b.get("tool_use_id"))
                if c is not None and c["state"] is None:
                    c["state"] = "error" if b.get("is_error") else "ok"
                    c["end"] = ts
    return calls, []


_PARSERS = {"pi-job": _parse_pi_job, "pi": _parse_pi, "claude": _parse_claude, "claude-sub": _parse_claude}


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _live(target: dict, mtime: float, now: float) -> bool:
    if target["kind"] == "pi-job":
        meta = target.get("meta") or {}
        if meta.get("state") not in ("running", "queued"):
            return False
        pid = meta.get("pid")
        return pid is None or _pid_alive(pid)
    return now - mtime < RUNNING_S


def _calls(target: dict, now: float, grow: bool = True):
    """(newest-first raw calls, total, cut) from the tail of the agent's file. The read grows x4 from
    TAIL_MAX to TAIL_GROW_MAX (``grow``) until it holds more than CALLS_MAX calls or the whole file."""
    cap = TAIL_MAX
    while True:
        got = _read_tail(target["path"], cap)
        if got is None:
            return [], 0, False
        data, base, mtime = got
        calls, pending = _PARSERS[target["kind"]](data, base)
        if not grow or base == 0 or len(calls) > CALLS_MAX or cap >= TAIL_GROW_MAX:
            break
        cap = min(cap * 4, TAIL_GROW_MAX)
        data = calls = pending = None
    for c in pending:                       # ended after the last timed event: the file time
        c["end"] = mtime
    live = _live(target, mtime, now)
    for c in calls:
        if c["state"] is None:
            c["state"] = "running" if live else "error"
    calls.sort(key=lambda c: (c["off"], c["i"]), reverse=True)
    return calls, len(calls), base > 0


def call_id(key: bytes, agent: str, path, off: int, i: int) -> str:
    msg = f"{agent}\0{path}\0{int(off)}\0{int(i)}".encode("utf-8", "replace")
    return "c-" + hmac.new(key, msg, hashlib.sha256).hexdigest()[:16]


def _round(t):
    return round(t, 3) if isinstance(t, (int, float)) else None


def list_calls(home, router_home, agent: str, key: bytes, now: float | None = None, run=None) -> dict | None:
    """The /api/commands document, or None when the agent is not known."""
    now = time.time() if now is None else now
    target = resolve(Path(home), Path(router_home) / "pi-jobs", agent)
    if target is None:
        return None
    calls, total, cut = _calls(target, now)
    out = []
    for c in calls[:CALLS_MAX]:
        short, masked = mask_short(c["tool"], c["args"])
        out.append({"call": call_id(key, agent, target["path"], c["off"], c["i"]), "tool": tool_out(c["tool"]),
                    "short": short, "masked": masked, "start": _round(c["start"]), "end": _round(c["end"]),
                    "state": c["state"]})
    calls = None                            # the raw arguments go now
    procs, pcomplete = procs_of(target, Path(home), now, run)
    return {"agent": agent, "kind": target["kind"], "calls": out, "calls_more": max(0, total - CALLS_MAX),
            "procs": procs, "complete": not cut and pcomplete}


def full_call(home, router_home, agent: str, call: str, key: bytes) -> dict | None:
    """The /api/commands/full document, or None (404) for a wrong, old or other-agent call id."""
    if not isinstance(call, str) or not CALL_RE.fullmatch(call):
        return None
    target = resolve(Path(home), Path(router_home) / "pi-jobs", agent)
    if target is None:
        return None
    calls, _, _ = _calls(target, time.time())
    for c in calls[:CALLS_MAX]:
        if hmac.compare_digest(call_id(key, agent, target["path"], c["off"], c["i"]), call):
            full = mask_full(c["tool"], c["args"])
            return {"call": call, "tool": tool_out(c["tool"]), "text": full["text"], "cut": full["cut"]}
    return None


def running_now(agent: str, *, home=None, router_home=None, jobs_dir=None, now: float | None = None,
                kind: str | None = None) -> dict | None:
    """The newest running call of an agent for the menu bar (short text at MENU_MAX), or None.
    ``kind`` "pi-job": the caller found the job in its own bounded listing."""
    now = time.time() if now is None else now
    home = Path(home) if home else Path.home()
    jroot = Path(jobs_dir) if jobs_dir else (Path(router_home) if router_home else home / ".apex-router") / "pi-jobs"
    target = resolve(home, jroot, agent, kind=kind, listed=kind == "pi-job")
    if target is None:
        return None
    calls, _, _ = _calls(target, now, grow=False)    # the menu bar: the newest call only, the 256 KiB tail
    for c in calls[:CALLS_MAX]:
        if c["state"] == "running":
            short, masked = mask_short(c["tool"], c["args"], MENU_MAX)
            age = round(max(0.0, now - c["start"])) if isinstance(c["start"], (int, float)) else None
            return {"agent": agent, "kind": target["kind"], "tool": tool_out(c["tool"]), "short": short,
                    "masked": masked, "start": _round(c["start"]), "age_s": age}
    return None


# ---- rule 4: procs --------------------------------------------------------------------------------------

def _run(argv, timeout: float = PS_TIMEOUT_S) -> str:
    from . import safe_proc
    _, out = safe_proc.run_capped(argv, timeout, PS_OUT_MAX, stop_at_cap=True)
    return out.decode("utf-8", "replace")


def _lstart(s):
    if not isinstance(s, str):
        return None
    try:
        st = time.strptime(" ".join(s.split()), _LSTART_FMT)
    except ValueError:
        return None
    return st


def _start_ok(pid_start, actual) -> bool:
    st = _lstart(pid_start)
    if st is None or actual is None:
        return True                         # nothing to compare: the pid is trusted
    return any(abs(c - actual) <= START_TOLERANCE_S for c in (time.mktime(st), calendar.timegm(st)))


def _root_pid(target: dict, home: Path, table: dict, run) -> int | None:
    from .agent_resources import sessions as sess_mod
    kind = target["kind"]
    if kind == "pi-job":
        meta = target.get("meta") or {}
        pid = meta.get("pid")
        if isinstance(pid, int) and pid in table and _start_ok(meta.get("pid_start"), table[pid].get("start")):
            return pid
        return None
    if kind == "claude":
        sid = target["path"].stem
        hit = sess_mod.claude_sessions(home, table).get(sid)
        return hit["pid"] if hit else None
    if kind == "pi":
        head = _read_small_head(target["path"])
        cwd = head.get("cwd") if isinstance(head, dict) else None
        if not isinstance(cwd, str) or not cwd.startswith("/"):
            return None
        if not _newest_in_dir(target["path"]):
            return None
        pids = sorted((p for p, r in table.items() if r["name"] == "pi"),
                      key=lambda p: -(table[p].get("start") or 0))[:PI_PIDS_MAX]
        if not pids:
            return None
        cwds = sess_mod.lsof_cwds(pids, run, PS_TIMEOUT_S)
        real = os.path.realpath(cwd)
        cand = [p for p, c in cwds.items() if p in table and os.path.realpath(c) == real]
        return cand[0] if len(cand) == 1 else None
    return None


def _read_small_head(path: Path):
    fd, st = _open_regular(path)
    if fd is None:
        return None
    try:
        data = os.pread(fd, min(HEAD_MAX, st.st_size), 0)
    except OSError:
        return None
    finally:
        os.close(fd)
    line = data.split(b"\n", 1)[0]
    try:
        r = json.loads(line)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return r if isinstance(r, dict) else None


def _newest_in_dir(path: Path) -> bool:
    """The pi session file is the newest one of its cwd (slug dir): the live process of that cwd runs it."""
    try:
        mine = os.lstat(path).st_mtime
    except OSError:
        return False
    ents, _ = _scan(path.parent)
    for e in ents:
        if e.name.endswith(".jsonl") and e.path != str(path):
            try:
                if e.stat(follow_symlinks=False).st_mtime > mine:
                    return False
            except OSError:
                continue
    return True


def _pname(comm) -> str:
    name = os.path.basename(str(comm or "")) or "?"
    name, _ = mask_secrets(_WS_RE.sub(" ", _clean(name)).strip())
    return name[:40] or "?"


def procs_of(target: dict, home: Path, now: float, run=None):
    """(tree or None, complete). No process lookup for an ended job or a sub-agent (it runs inside its
    session's process)."""
    kind = target["kind"]
    if kind == "claude-sub":
        return None, True
    if kind == "pi-job":
        meta = target.get("meta") or {}
        if meta.get("state") not in ("running", "queued") or not isinstance(meta.get("pid"), int):
            return None, True
    from .agent_resources import procs as procs_mod
    run = run or _run
    try:
        table = procs_mod.parse_ps(run(["ps", "-axo", "pid=,ppid=,rss=,%cpu=,lstart=,comm="], timeout=PS_TIMEOUT_S))
        root = _root_pid(target, home, table, run) if table else None
    except Exception:  # noqa: BLE001 — no process data is "no live process", never an error
        return None, True
    if root is None:
        return None, True
    kids = procs_mod.children_map(table)
    shown: list = []
    complete = [True]

    def walk(pid, depth):
        shown.append(pid)
        for c in sorted(kids.get(pid, ())):
            if c == pid or c in shown:
                continue
            if depth >= PROCS_DEPTH or len(shown) >= PROCS_MAX:
                complete[0] = False
                return
            walk(c, depth + 1)
    walk(root, 0)
    try:
        labels = procs_mod.script_labels(table, shown[1:], run, PS_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        labels = {}

    def node(pid, depth):
        p = table[pid]
        n = {"pid": pid, "name": _pname(p.get("comm")), "label": labels.get(pid),
             "cpu": p.get("cpu"), "rss_mb": round((p.get("rss_kb") or 0) / 1024, 1),
             "age_s": round(max(0.0, now - p["start"])) if isinstance(p.get("start"), (int, float)) else None,
             "children": []}
        for c in sorted(kids.get(pid, ())):
            if c in want and depth < PROCS_DEPTH:
                n["children"].append(node(c, depth + 1))
        return n
    want = set(shown)
    tree = node(root, 0)
    tree.pop("label", None)
    tree.pop("age_s", None)
    return tree, complete[0]
