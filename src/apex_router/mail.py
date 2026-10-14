"""Mail bus: short updates from running agents, and the owner's messages back to them.

``apex-router mail`` is the one channel between a fan-out (a Workflow run, a nightly loop, any
set of subagents) and the person who started it. Agents post one-line status, questions and
replies; the owner reads a run's inbox, sends a message to one agent or to all, and the agents
pick it up at their next checkpoint (they are told, in their prompt, to run ``mail read`` before
every major decision). Nothing here is instant: delivery is checkpoint-based, which is what a
batch of agents can honour.

Layout under ``$APEX_MAIL_DIR`` (default ``$APEX_ROUTER_HOME/mail``, i.e. ``~/.apex-router/mail``)::

    <run>/out.jsonl                 agent -> owner, append-only: {id, ts, agent, kind, text, reply_to}
    <run>/in/<agent>.jsonl          owner -> one agent, append-only: {id, ts, to, text}
                                    (``/`` in the agent name is written as ``__``)
    <run>/in/_all.jsonl             owner -> every agent in the run
    <run>/in/<agent>.cursor         JSON {"own": n, "all": n}: how many lines the agent consumed

A run name is ``[A-Za-z0-9._-]`` (no ``/``); an agent name is the same with ``/`` allowed inside
(``world/role``), never starting with ``_`` or ``.`` and never containing ``__`` (so ``_all`` and
the ``__`` file encoding cannot collide with an agent). Text is one line: whitespace controls
become a space, other control, line-separator and bidi characters are dropped, at most
``TEXT_MAX`` characters. Kinds: ``start``, ``status``, ``question``, ``reply``, ``done``. Appends
take an exclusive ``flock`` on the file: the id is the count of parsed rows inside the lock (unique
per file), and a torn tail line left by a crashed writer is terminated before the next row, so it
is skipped, never counted and never glues onto the next row. ``reply_to`` names the id of an owner
line in the agent's own inbox. A question counts as open until the owner's next line to that agent
(or to ``*``) after it.

Every post or send also rewrites the menu-bar adapter ``$APEX_ADAPTERS_DIR/mail.json``
(default ``~/.apex-router/adapters``; read by ``apex-router snapshot``, which shows at most five
adapter files in name order) for the run last written to, so the widget shows open questions,
unread owner mail and the last few lines without a widget change; dollar amounts in message text
are masked there. Stdlib only; all reads fail open (a missing run is an empty run).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

TEXT_MAX = 400
KINDS = ("start", "status", "question", "reply", "done")
_RUN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
_WS = re.compile(r"[\t\n\r\x0b\x0c\u2028\u2029]+")
_DROP = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_MONEY = re.compile(r"\$\s?\d[\d,.]*\s?[kKmMbB]?")
ADAPTER_ROWS = 10
ADAPTER_WIDTH = 120
EXIT_USAGE = 2


# ---------------------------------------------------------------- paths and names

def mail_dir() -> Path:
    d = os.environ.get("APEX_MAIL_DIR")
    if d:
        return Path(d).expanduser()
    home = os.environ.get("APEX_ROUTER_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".apex-router") / "mail"


def adapters_dir() -> Path:
    d = os.environ.get("APEX_ADAPTERS_DIR")
    if d:
        return Path(d).expanduser()
    home = os.environ.get("APEX_ROUTER_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".apex-router") / "adapters"


def _name(s: str, what: str) -> str:
    rx = _RUN if what == "run" else _AGENT
    if not isinstance(s, str) or not rx.match(s) or ".." in s.split("/") or "__" in s:
        raise ValueError(f"{what} must match [A-Za-z0-9._-]"
                         f"{' (with / inside, no leading _, no __)' if what == 'agent' else ''}: {s!r}")
    return s


def _valid(s: str, what: str) -> bool:
    try:
        _name(s, what)
        return True
    except ValueError:
        return False


def _text(s: str) -> str:
    t = _DROP.sub("", _WS.sub(" ", str(s)))
    t = re.sub(r" {2,}", " ", t).strip()
    if not t:
        raise ValueError("text is empty")
    return t[:TEXT_MAX]


def _run_dir(run: str) -> Path:
    return mail_dir() / _name(run, "run")


def _in_path(run: str, agent: str) -> Path:
    return _run_dir(run) / "in" / (("_all" if agent == "*" else agent.replace("/", "__")) + ".jsonl")


def _cursor_path(run: str, agent: str) -> Path:
    return _run_dir(run) / "in" / (agent.replace("/", "__") + ".cursor")


def _rows(path: Path) -> list:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in lines:
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def _append(path: Path, row: dict) -> dict:
    """Append ``row`` with the next id under an exclusive lock; returns the row written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            data = f.read()
            n = 0
            for line in data.splitlines():
                try:
                    if isinstance(json.loads(line), dict):
                        n += 1
                except ValueError:
                    continue
            row = {"id": n + 1, **row}
            lead = b"" if not data or data.endswith(b"\n") else b"\n"
            f.write(lead + json.dumps(row, sort_keys=True).encode("utf-8") + b"\n")
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return row


# ---------------------------------------------------------------- agent side

def post(run: str, agent: str, text: str, *, kind: str = "status", reply_to=None,
         now: float | None = None) -> dict:
    """An agent's one-line message to the owner. Returns the row written."""
    run, agent = _name(run, "run"), _name(agent, "agent")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}: {kind!r}")
    row = _append(_run_dir(run) / "out.jsonl",
                  {"ts": time.time() if now is None else float(now), "agent": agent, "kind": kind,
                   "text": _text(text), "reply_to": int(reply_to) if reply_to is not None else None})
    _write_adapter(run, row["ts"])
    return row


def read(run: str, agent: str, *, peek: bool = False) -> list:
    """The owner's unread messages to ``agent`` (its own, then broadcast), oldest first.
    Advances the agent's cursor unless ``peek`` (atomically, under the agent's lock, so two
    concurrent reads never deliver a line twice). An unknown run creates nothing."""
    try:
        run, agent = _name(run, "run"), _name(agent, "agent")
    except ValueError:
        return []
    if not _run_dir(run).is_dir():
        return []
    cpath = _cursor_path(run, agent)
    cpath.parent.mkdir(parents=True, exist_ok=True)
    with open(cpath.with_suffix(".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            cur = _cursor(run, agent)
            own, alls = _rows(_in_path(run, agent)), _rows(_in_path(run, "*"))
            got = own[cur["own"]:] + alls[cur["all"]:]
            got.sort(key=lambda r: (r.get("ts") or 0, r.get("id") or 0))
            if got and not peek:
                tmp = cpath.with_name(f"{cpath.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps({"own": len(own), "all": len(alls)}), encoding="utf-8")
                os.replace(tmp, cpath)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return got


def _cursor(run: str, agent: str) -> dict:
    try:
        v = json.loads(_cursor_path(run, agent).read_text(encoding="utf-8"))
        return {"own": int(v.get("own", 0)), "all": int(v.get("all", 0))}
    except (OSError, ValueError, AttributeError, TypeError):
        return {"own": 0, "all": 0}


# ---------------------------------------------------------------- owner side

def send(run: str, agent: str, text: str, *, now: float | None = None) -> dict:
    """The owner's message to one agent, or to every agent in the run with ``agent='*'``."""
    run = _name(run, "run")
    to = "*" if agent == "*" else _name(agent, "agent")
    row = _append(_in_path(run, to), {"ts": time.time() if now is None else float(now), "to": to,
                                      "text": _text(text)})
    _write_adapter(run, row["ts"])
    return row


def latest_run() -> str | None:
    best, best_t = None, -1.0
    try:
        for d in mail_dir().iterdir():
            if not d.is_dir() or not _valid(d.name, "run"):
                continue
            try:
                t = max([p.stat().st_mtime for p in d.rglob("*.jsonl")] or [d.stat().st_mtime])
            except OSError:
                continue
            if t > best_t:
                best, best_t = d.name, t
    except OSError:
        return None
    return best


def inbox(run: str | None, *, agent: str | None = None, n: int | None = None,
          owner: bool = True) -> list:
    """The conversation of a run (the latest run when ``run`` is None), oldest first: agent rows
    and, with ``owner``, the owner's sends as ``agent="owner"`` rows with ``to``; ``agent``
    filters by exact name or prefix (``w-dist/`` = that world, owner lines to it or to ``*``
    included); ``n`` keeps the newest n."""
    run = run or latest_run()
    if run is None:
        return []
    try:
        rows = _rows(_run_dir(run) / "out.jsonl")
        if owner:
            d = _run_dir(run) / "in"
            for p in (sorted(d.glob("*.jsonl")) if d.is_dir() else []):
                for r in _rows(p):
                    rows.append({"id": r.get("id"), "ts": r.get("ts"), "agent": "owner",
                                 "kind": "owner", "to": r.get("to"), "text": r.get("text"),
                                 "reply_to": None})
            rows.sort(key=lambda r: (r.get("ts") or 0, 0 if r.get("agent") == "owner" else 1))
    except ValueError:
        return []
    if agent:
        hit = lambda a: a == agent or (agent.endswith("/") and str(a).startswith(agent))
        rows = [r for r in rows if hit(r.get("agent")) or
                (r.get("agent") == "owner" and (r.get("to") == "*" or hit(r.get("to"))))]
    return rows[-n:] if n else rows


def runs() -> list:
    out = []
    try:
        names = [d.name for d in mail_dir().iterdir() if d.is_dir() and _valid(d.name, "run")]
    except OSError:
        return []
    for name in names:
        rows = _rows(_run_dir(name) / "out.jsonl")
        ag = agents(name)
        out.append({"run": name, "posts": len(rows), "agents": len(ag),
                    "questions": sum(a["questions"] for a in ag),
                    "unread": sum(a["unread"] for a in ag),
                    "last_ts": max([r.get("ts") or 0 for r in rows] or [0])})
    out.sort(key=lambda r: -r["last_ts"])
    return out


def agents(run: str) -> list:
    """Per agent: state (running/done), last line, open questions (asked and not followed by an
    owner line to that agent or to ``*``), unread owner mail (own + broadcast past its cursor)."""
    try:
        rows = _rows(_run_dir(run) / "out.jsonl")
    except ValueError:
        return []
    by: dict = {}
    for r in rows:
        a = str(r.get("agent", ""))
        if not a:
            continue
        s = by.setdefault(a, {"agent": a, "state": "running", "last": "", "last_kind": "",
                              "last_ts": 0, "questions": 0, "unread": 0, "posts": 0})
        s["posts"] += 1
        s["last"], s["last_kind"], s["last_ts"] = r.get("text", ""), r.get("kind", ""), r.get("ts", 0)
        if r.get("kind") == "question":
            s.setdefault("_q", []).append(r.get("ts") or 0)
        if r.get("kind") == "done":
            s["state"] = "done"
    alls = _rows(_in_path(run, "*"))
    for a, s in by.items():
        own = _rows(_in_path(run, a))
        last_owner = max([r.get("ts") or 0 for r in own + alls] or [-1.0])
        s["questions"] = sum(1 for t in s.pop("_q", []) if t > last_owner)
        cur = _cursor(run, a)
        s["unread"] = max(0, len(own) - cur["own"]) + max(0, len(alls) - cur["all"])
    return sorted(by.values(), key=lambda s: s["agent"])


# ---------------------------------------------------------------- widget adapter

def _clip(s: str, n: int = ADAPTER_WIDTH) -> str:
    s = _MONEY.sub("•", _DROP.sub("", _WS.sub(" ", s)))
    return s if len(s) <= n else s[:n - 1] + "…"


def _write_adapter(run: str, ts: float) -> None:
    try:
        ag = agents(run)
        q, u = sum(a["questions"] for a in ag), sum(a["unread"] for a in ag)
        head = (f"{run}: {len(ag)} agents, {q} question{'s' if q != 1 else ''} open, "
                f"{u} owner line{'s' if u != 1 else ''} unread")
        rows = [head]
        for r in inbox(run, n=ADAPTER_ROWS - 1)[::-1]:
            tag = {"question": "?", "done": "✓", "owner": "→"}.get(str(r.get("kind")), "·")
            who = f"owner→{r.get('to')}" if r.get("kind") == "owner" else r.get("agent")
            rows.append(_clip(f"{tag} {who}: {r.get('text')}"))
        d = adapters_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".mail.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"title": "Mail", "rows": rows[:ADAPTER_ROWS], "ts": ts}),
                       encoding="utf-8")
        os.replace(tmp, d / "mail.json")
    except (OSError, ValueError):
        pass


def write_widget(run: str | None = None) -> bool:
    run = run or latest_run()
    if run is None:
        return False
    _write_adapter(run, time.time())
    return True


# ---------------------------------------------------------------- e-mail digest

DIGEST_MAX = 60


def digest(run: str | None = None, *, since_cursor: bool = True, advance: bool = False) -> dict:
    """A plain-text digest of a run: per-agent state, open questions, and the conversation lines
    not yet digested (``<run>/digest.cursor`` = count of ``inbox`` rows already sent; ``advance``
    moves it). Returns {run, subject, body, new, questions, unread, agents}."""
    run = run or latest_run()
    if run is None or not _valid(run, "run") or not _run_dir(run).is_dir():
        return {"run": None, "subject": "", "body": "", "new": 0, "questions": 0, "unread": 0, "agents": 0}
    rows = inbox(run)
    cpath = _run_dir(run) / "digest.cursor"
    try:
        sent = int(cpath.read_text(encoding="utf-8")) if since_cursor else 0
    except (OSError, ValueError):
        sent = 0
    new = rows[sent:] if sent <= len(rows) else rows
    ag = agents(run)
    q, u = sum(a["questions"] for a in ag), sum(a["unread"] for a in ag)
    head = (f"run {run}: {len(ag)} agents, {q} question{'s' if q != 1 else ''} open, "
            f"{u} owner line{'s' if u != 1 else ''} unread, {len(new)} new line{'s' if len(new) != 1 else ''}")
    lines = [head, ""]
    if q:
        lines.append("OPEN QUESTIONS")
        for r in rows:
            if r.get("kind") == "question":
                lines.append(f"  {r.get('agent')}: {r.get('text')}")
        lines.append("")
    lines.append("AGENTS")
    for a in ag:
        lines.append(f"  {a['agent']} [{a['state']}] {a['last_kind']}: {a['last']}")
    lines.append("")
    lines.append("NEW LINES" if new else "no new lines")
    shown = new[-DIGEST_MAX:]
    if len(new) > len(shown):
        lines.append(f"  … {len(new) - len(shown)} earlier lines omitted")
    for r in shown:
        lines.append("  " + _fmt(r))
    lines += ["", f"reply: apex-router mail send {run} <agent|*> \"...\""]
    if advance:
        try:
            cpath.write_text(str(len(rows)), encoding="utf-8")
        except OSError:
            pass
    subject = f"[apex-router mail] {run}: {len(new)} new, {q} question{'s' if q != 1 else ''} open"
    return {"run": run, "subject": subject, "body": "\n".join(lines), "new": len(new),
            "questions": q, "unread": u, "agents": len(ag)}


def send_email(to: str, subject: str, body: str, *, runner=None) -> bool:
    """Send through the Mail.app (osascript) on macOS; falls back to /usr/sbin/sendmail. The
    address is validated; the subject and body reach AppleScript as string literals only."""
    if not re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", to):
        raise ValueError(f"not an e-mail address: {to!r}")
    run = runner or (lambda argv, **kw: subprocess.run(argv, capture_output=True, text=True,
                                                        timeout=30, check=False, **kw))
    esc = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Mail"\n'
        f'  set m to make new outgoing message with properties {{subject:"{esc(subject)}", '
        f'content:"{esc(body)}", visible:false}}\n'
        f'  tell m to make new to recipient at end of to recipients with properties {{address:"{to}"}}\n'
        '  send m\n'
        'end tell'
    )
    try:
        r = run(["/usr/bin/osascript", "-"], input=script)
        if r.returncode == 0:
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        msg = f"To: {to}\nSubject: {subject}\nContent-Type: text/plain; charset=utf-8\n\n{body}\n"
        r = run(["/usr/sbin/sendmail", "-t"], input=msg)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------- prompt snippet

def brief(run: str, agent: str) -> str:
    """The paragraph to put in an agent's prompt so it uses the bus."""
    run, agent = _name(run, "run"), _name(agent, "agent")
    return (
        f"MAIL: the owner follows this run and may answer you. Post one line (≤ {TEXT_MAX} chars, no "
        f"secrets, no prompt text) when you start, at each decision that changes a candidate's fate, "
        f"when you need a ruling, and when you finish:\n"
        f"  apex-router mail post {run} {agent} \"<one line>\" --kind start|status|question|done\n"
        f"Before every major decision, read the owner's messages and follow them (a message to '*' is "
        f"for everyone):\n"
        f"  apex-router mail read {run} {agent}\n"
        f"When you act on an owner line, post --kind reply --reply-to <its id>. Mail never replaces your "
        f"deliverable: the structured output is still the result."
    )


# ---------------------------------------------------------------- CLI

def _key(r: dict) -> tuple:
    return (r.get("agent"), r.get("to") if r.get("kind") == "owner" else None, r.get("id"))


def _fmt(r: dict) -> str:
    t = time.strftime("%H:%M", time.localtime(r.get("ts") or 0))
    if r.get("kind") == "owner":
        return f"{t} → {r.get('to')}: {r.get('text')}"
    tag = {"question": "?", "done": "✓", "start": "▶", "reply": "↩"}.get(r.get("kind"), "·")
    return f"{t} {tag} {r.get('agent')}: {r.get('text')}"


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="apex-router mail",
                                 description="mail bus between a fan-out's agents and the owner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("post", help="agent -> owner: one line")
    p.add_argument("run"); p.add_argument("agent"); p.add_argument("text")
    p.add_argument("--kind", choices=KINDS, default="status")
    p.add_argument("--reply-to", type=int, default=None)
    p = sub.add_parser("read", help="agent reads the owner's unread messages (advances its cursor)")
    p.add_argument("run"); p.add_argument("agent"); p.add_argument("--peek", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("send", help="owner -> agent (or * for every agent in the run)")
    p.add_argument("run"); p.add_argument("agent"); p.add_argument("text")
    p = sub.add_parser("inbox", help="owner view of a run's agent lines (latest run by default)")
    p.add_argument("run", nargs="?"); p.add_argument("--agent", default=None)
    p.add_argument("-n", type=int, default=None); p.add_argument("--follow", action="store_true")
    p.add_argument("--json", action="store_true")
    sub.add_parser("runs", help="runs with counts")
    p = sub.add_parser("agents", help="agents of a run: state, last line, open questions, unread")
    p.add_argument("run", nargs="?")
    p = sub.add_parser("brief", help="the prompt paragraph that tells an agent to use the bus")
    p.add_argument("run"); p.add_argument("agent")
    p = sub.add_parser("widget", help="rewrite the menu-bar adapter for a run (latest by default)")
    p.add_argument("run", nargs="?")
    p = sub.add_parser("digest", help="plain-text digest of a run's new lines; --to e-mails it via "
                                      "Mail.app (or sendmail) and advances the digest cursor")
    p.add_argument("run", nargs="?"); p.add_argument("--to", default=None)
    p.add_argument("--all", action="store_true", help="every line, not only those since the last digest")
    p.add_argument("--even-if-empty", action="store_true")
    return ap


def main(argv=None) -> int:
    try:
        a = _parser().parse_args(argv)
    except SystemExit as e:
        return 0 if e.code in (0, None) else EXIT_USAGE
    try:
        if a.cmd == "post":
            r = post(a.run, a.agent, a.text, kind=a.kind, reply_to=a.reply_to)
            print(r["id"])
        elif a.cmd == "read":
            for r in read(a.run, a.agent, peek=a.peek):
                print(json.dumps(r, sort_keys=True) if a.json
                      else f"[{'*' if r.get('to') == '*' else 'id ' + str(r.get('id'))}] {r.get('text')}")
        elif a.cmd == "send":
            r = send(a.run, a.agent, a.text)
            print(r["id"])
        elif a.cmd == "inbox":
            seen: set = set()
            first = True
            while True:
                rows = inbox(a.run, agent=a.agent)
                show = rows[-a.n:] if (a.n and first) else rows
                for r in show:
                    k = _key(r)
                    if k in seen:
                        continue
                    seen.add(k)
                    print(json.dumps(r, sort_keys=True) if a.json else _fmt(r), flush=True)
                if not a.follow:
                    break
                if first:
                    seen.update(_key(r) for r in rows)      # older rows hidden by -n stay hidden
                first = False
                time.sleep(1.0)
        elif a.cmd == "runs":
            for r in runs():
                print(f"{r['run']}: {r['agents']} agents, {r['posts']} lines, "
                      f"{r['questions']} questions open, {r['unread']} unread")
        elif a.cmd == "agents":
            run = a.run or latest_run()
            for s in agents(run) if run else []:
                print(f"{s['agent']} [{s['state']}] q={s['questions']} unread={s['unread']} "
                      f"last={s['last_kind']}: {s['last']}")
        elif a.cmd == "brief":
            print(brief(a.run, a.agent))
        elif a.cmd == "widget":
            print("written" if write_widget(a.run) else "no run")
        elif a.cmd == "digest":
            d = digest(a.run, since_cursor=not a.all, advance=bool(a.to))
            if d["run"] is None:
                print("no run")
                return 1
            if a.to:
                if d["new"] == 0 and not a.even_if_empty:
                    print("nothing new; not sent")
                    return 0
                ok = send_email(a.to, d["subject"], d["body"])
                print(f"{'sent' if ok else 'NOT sent'}: {d['subject']} -> {a.to}")
                return 0 if ok else 1
            print(d["subject"]); print(); print(d["body"])
        return 0
    except ValueError as e:
        print(f"mail: {e}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 0
