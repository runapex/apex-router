"""E1 step dataset (DESIGN-worldmodel-P6.md §1): one record per tool call, one per task.

Reads pi sessions, Claude Code main sessions and Claude Code subagent logs (read-only), and writes
``steps.jsonl`` / ``tasks.jsonl`` / ``manifest.json`` under ``~/.apex-router/worldmodel/``
(``APEX_ROUTER_HOME`` honoured; dir 0700, files 0600). A rebuild rewrites all three from the
transcripts, so it is idempotent.

- **Tasks** are cut exactly as ``labels.extract`` cuts them (shared ``request_text`` /
  ``is_tool_result``): a task is a user turn that ran >= 1 tool; its id is
  ``<session id>:<turn ordinal>`` with the same ordinal ``labels`` hashes into its own task id, so
  outcomes join 1:1.
- **Subagent** steps carry the parent session id and ``agent``; they belong to the task whose
  ``Agent`` tool call spawned them (``meta.json`` ``toolUseId``, resolved through nested
  subagents), else (workflow agents have no ``toolUseId``) to the latest task of the parent
  session that started before the subagent's first step. Their ``i`` / ``dt`` / ``phase`` are
  computed within their own stream (task, agent).
- **model** comes from proxy telemetry (same session_id / agent_id, the latest request at or
  before the step, within 15 min), else from the transcript's own assistant record, else null.
- **outcome** from ``labels/labels.jsonl`` + ``labels/gold.jsonl``: gold > weak > none.
- **split** by session start time: first 70% of sessions train, next 10% val, last 20% test.
- **task_type** (tasks.jsonl): ``classify.classify`` on the task's request text (embedding
  refinement via local ollama; the text is never stored), null when it cannot classify;
  **workflow**: ``W2`` when the task spawned a subagent (Agent / Task / Workflow), else ``W0``.

Never stores text: classes, counts, buckets, flags, ids. Every reader fails open per line.
"""
from __future__ import annotations

import bisect
import json
import os
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .. import labels as L
from ..classify import TASK_TYPES
from ..telemetry_path import telemetry_path
from . import actions as A

SCHEMA = 1
MODEL_WINDOW_S = 900.0
SPLITS = (("train", 0.70), ("val", 0.80), ("test", 1.00))
TEST_TAIL = 8000                   # chars of a test step's output kept (in memory) to parse counts
TEXT_CLIP = 2000                   # chars of the request the task-type classifier embeds


def base_home(home=None) -> Path:
    return Path(home) if home else Path(os.environ.get("APEX_ROUTER_HOME") or
                                        Path.home() / ".apex-router")


def data_home(home=None) -> Path:
    return base_home(home) / "worldmodel"


# ---- small helpers --------------------------------------------------------------------------

def _ts(v) -> float | None:
    """ISO-8601 string or epoch (s or ms) -> epoch seconds."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) / 1000.0 if v > 1e11 else float(v)
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _rec_ts(d: dict, m: dict) -> float | None:
    t = _ts(d.get("timestamp"))
    return t if t is not None else _ts(m.get("timestamp"))


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    return L._text(content)


def _size(obj) -> int:
    if obj is None:
        return 0
    if isinstance(obj, str):
        return len(obj)
    try:
        return len(json.dumps(obj, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


class _Stats:
    def __init__(self):
        self.malformed = 0
        self.unreadable = 0


def _lines(path: Path, st: _Stats):
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    st.malformed += 1
                    continue
                if isinstance(d, dict):
                    yield d
                else:
                    st.malformed += 1
    except OSError:
        st.unreadable += 1


def _new_call(b: dict, ts, model) -> dict:
    tool = b.get("name") if isinstance(b.get("name"), str) else ""
    inp = b.get("arguments") if "arguments" in b else b.get("input")
    act = A.classify(tool, inp if isinstance(inp, (dict, str)) else {})
    return {"id": b.get("id"), "tool": tool, "act": act, "ts": ts, "in": _size(inp),
            "out": 0, "err": 0, "text": None, "model": model}


def _set_result(call: dict, text: str, err) -> None:
    call["out"] = len(text)
    call["err"] = int(err in (True, "True", "true"))
    if call["act"] == "test":
        call["text"] = text[-TEST_TAIL:]


def _model_of(m: dict):
    v = m.get("model")
    return v if isinstance(v, str) and v and not v.startswith("<") else None


# ---- transcript walkers ---------------------------------------------------------------------

def walk_main(path: Path, st: _Stats) -> dict:
    """One main transcript -> {start, turns: [{i, ts, calls}]} with every request turn (same
    boundaries and ordinals as ``labels.extract``)."""
    start = None
    turns: list = []
    cur = None
    by_call: dict = {}
    for d in _lines(path, st):
        try:
            m = d.get("message") if isinstance(d.get("message"), dict) else {}
            ts = _rec_ts(d, m)
            if start is None and ts is not None:
                start = ts
            role = m.get("role") or d.get("type")
            c = m.get("content")
            if role == "user":
                if L.is_tool_result(c):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            call = by_call.get(b.get("tool_use_id"))
                            if call is not None:
                                _set_result(call, _result_text(b.get("content")), b.get("is_error"))
                    continue
                req = L.request_text(c)
                if req is None:
                    continue
                # the request text lives only in memory, for the task-type classifier
                cur = {"i": len(turns), "ts": ts, "calls": [], "text": req}
                turns.append(cur)
            elif role == "assistant" and cur is not None and isinstance(c, list):
                model = _model_of(m)
                for b in c:
                    if isinstance(b, dict) and b.get("type") in ("toolCall", "tool_use"):
                        call = _new_call(b, ts, model)
                        cur["calls"].append(call)
                        by_call[b.get("id")] = call
            elif role == "toolResult" and cur is not None:              # pi
                call = by_call.get(m.get("toolCallId"))
                if call is not None:
                    _set_result(call, L._text(c), m.get("isError"))
        except Exception:  # noqa: BLE001 — one odd record never sinks the file
            st.malformed += 1
    return {"start": start, "turns": turns}


def walk_sub(path: Path, st: _Stats) -> list:
    """One subagent log -> its tool calls in order (the whole log is one stream)."""
    calls: list = []
    by_call: dict = {}
    for d in _lines(path, st):
        try:
            m = d.get("message") if isinstance(d.get("message"), dict) else {}
            ts = _rec_ts(d, m)
            role = m.get("role") or d.get("type")
            c = m.get("content")
            if role == "user" and L.is_tool_result(c):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        call = by_call.get(b.get("tool_use_id"))
                        if call is not None:
                            _set_result(call, _result_text(b.get("content")), b.get("is_error"))
            elif role == "assistant" and isinstance(c, list):
                model = _model_of(m)
                for b in c:
                    if isinstance(b, dict) and b.get("type") in ("toolCall", "tool_use"):
                        call = _new_call(b, ts, model)
                        calls.append(call)
                        by_call[b.get("id")] = call
        except Exception:  # noqa: BLE001
            st.malformed += 1
    return calls


def _meta(path: Path) -> dict:
    mp = path.with_name(path.name[:-len(".jsonl")] + ".meta.json")
    try:
        d = json.loads(mp.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


# ---- discovery ------------------------------------------------------------------------------

def main_transcripts(user_home: Path) -> list:
    """Same globs as ``labels.transcripts`` (pi sessions + Claude Code main sessions)."""
    return sorted(list((user_home / ".pi" / "agent" / "sessions").glob("**/*.jsonl"))
                  + list((user_home / ".claude" / "projects").glob("*/*.jsonl")))


def sub_transcripts(user_home: Path) -> list:
    """Claude Code subagent logs: ``<slug>/<session>/subagents/[workflows/<wf>/]agent-<id>.jsonl``."""
    root = user_home / ".claude" / "projects"
    out = []
    for p in root.glob("*/*/subagents/**/agent-*.jsonl"):
        try:
            sid = p.relative_to(root).parts[1]
        except (ValueError, IndexError):
            continue
        out.append((sid, p.name[len("agent-"):-len(".jsonl")], p))
    return sorted(out, key=lambda x: (x[0], str(x[2])))


# ---- telemetry + outcomes -------------------------------------------------------------------

def load_telemetry(path: Path, sessions: set, st: _Stats | None = None) -> dict:
    """(session_id, agent_id|None) -> (sorted ts list, models list)."""
    idx: dict = {}
    try:
        fh = open(path, errors="replace")
    except OSError:
        return {}
    with fh:
        for line in fh:
            if '"session_id"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            sid = d.get("session_id") if isinstance(d, dict) else None
            if sid not in sessions:
                continue
            ts = _ts(d.get("ts"))
            model = d.get("model_resolved") or d.get("model_requested")
            if ts is None or not isinstance(model, str) or not model:
                continue
            aid = d.get("agent_id") if isinstance(d.get("agent_id"), str) and d.get("agent_id") else None
            idx.setdefault((sid, aid), []).append((ts, model))
    out = {}
    for k, rows in idx.items():
        rows.sort()
        out[k] = ([r[0] for r in rows], [r[1] for r in rows])
    return out


def tel_model(tel: dict, sid: str, agent, ts) -> str | None:
    if ts is None:
        return None
    rows = tel.get((sid, agent))
    if not rows:
        return None
    tss, models = rows
    k = bisect.bisect_right(tss, ts + 1.0) - 1        # the request that produced this tool call
    if k >= 0 and ts + 1.0 - tss[k] <= MODEL_WINDOW_S:
        return models[k]
    return None


def load_outcomes(base: Path) -> dict:
    """labels task id -> (outcome, outcome_src): gold > weak > none."""
    out: dict = {}
    d = base / "labels"
    for r in L._read(d / "labels.jsonl"):
        if not isinstance(r, dict) or not isinstance(r.get("id"), str):
            continue
        lab = r.get("label")
        if r.get("from") == "gold" and lab in L.OUTCOMES + ("unknown",):
            out[r["id"]] = (lab, "gold")
        elif lab in L.OUTCOMES and out.get(r["id"], (None, None))[1] != "gold":
            out[r["id"]] = (lab, "weak")
    for g in L._read(d / "gold.jsonl"):
        if isinstance(g, dict) and isinstance(g.get("id"), str) and \
                g.get("outcome") in L.OUTCOMES + ("unknown",):
            out[g["id"]] = (g["outcome"], "gold")
    return out


# ---- build ----------------------------------------------------------------------------------

def _git_sha() -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _iso(t) -> str | None:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat().replace("+00:00", "Z") \
        if t is not None else None


def _stream(calls: list, base: dict, tel: dict, sid: str, agent, mstat: Counter) -> list:
    acts = [c["act"] for c in calls]
    ph = A.phases(acts)
    out, prev = [], None
    for i, c in enumerate(calls):
        model = tel_model(tel, sid, agent, c["ts"])
        if model:
            mstat["telemetry"] += 1
        elif c["model"]:
            model = c["model"]
            mstat["transcript"] += 1
        else:
            mstat["none"] += 1
        dt = round(c["ts"] - prev, 3) if (c["ts"] is not None and prev is not None) else None
        prev = c["ts"] if c["ts"] is not None else prev
        out.append({**base, "agent": agent, "i": i,
                    "ts": round(c["ts"], 3) if c["ts"] is not None else None,
                    "act": c["act"], "tool": c["tool"], "err": c["err"],
                    "tests": A.tests_field(c["act"], c["text"]),
                    "out_b": A.size_bucket(c["out"]), "in_b": A.size_bucket(c["in"]),
                    "dt": dt, "phase": ph[i], "model": model, "spawn": A.is_spawn(c["tool"])})
    return out


def _auto_embed():
    """ollama nomic-embed (local), or None when unreachable — then every task_type is null."""
    from ..route_resolve import _embed_fn
    return _embed_fn()


def task_typer(embed_fn):
    """-> f(request text) -> (task_type | None, source). ``classify.classify`` on the text alone:
    with no tool-set the request prior is the safe default, so only an embedding refinement
    (``embedding``) names a type; the default / an unreachable embedder gives None."""
    from .. import classify as C
    from ..route_resolve import EXEMPLARS
    cache: dict = {}

    def ef(x):
        if x not in cache:
            cache[x] = embed_fn(x)
        return cache[x]

    def f(text):
        if embed_fn is None or not text or not text.strip():
            return None, "none"
        try:
            c = C.classify(text[:TEXT_CLIP], embed_fn=ef, exemplars=EXEMPLARS)
        except Exception:  # noqa: BLE001 — an embedder failure leaves the task untyped
            return None, "error"
        if c.source == "default" or c.task_type not in C.TASK_TYPES:
            return None, c.source
        return c.task_type, c.source
    return f


def build(home=None, user_home=None, telemetry=None, log=None, embed_fn="auto") -> dict:
    """Rebuild steps/tasks/manifest from the transcripts. Returns the manifest.
    ``embed_fn``: "auto" (local ollama, None if down), None (no task types) or a callable."""
    t_start = time.time()
    base = base_home(home)
    out_dir = data_home(home)
    uh = Path(user_home) if user_home else Path.home()
    st = _Stats()

    # 1. main sessions -> tasks
    sessions: dict = {}                 # sid -> {src, start, path, tasks: [task]}
    for p in main_transcripts(uh):
        w = walk_main(p, st)
        sid = p.stem[-36:]
        src = "pi" if "/.pi/" in str(p) else "claude"
        tasks = []
        for t in w["turns"]:
            if not t["calls"]:
                continue
            tasks.append({"task": f"{sid}:{t['i']}", "lid": L._tid(p, t["i"]), "i": t["i"],
                          "ts": t["ts"], "calls": t["calls"], "subs": [], "text": t["text"]})
        if not tasks:
            continue
        if sid in sessions:             # same id twice (a copied file): keep the larger one
            if len(tasks) <= len(sessions[sid]["tasks"]):
                continue
        sessions[sid] = {"src": src, "start": w["start"] if w["start"] is not None
                         else tasks[0]["ts"], "tasks": tasks}

    # 2. subagents -> parent task
    by_use: dict = {}                   # tool_use id -> task dict
    for s in sessions.values():
        for t in s["tasks"]:
            for c in t["calls"]:
                if c["id"]:
                    by_use[c["id"]] = t
    pending = []
    orphans = 0
    for sid, aid, p in sub_transcripts(uh):
        if sid not in sessions:         # parent main log gone or ran no tool
            orphans += 1
            continue
        calls = walk_sub(p, st)
        if not calls:
            continue
        pending.append({"sid": sid, "agent": aid, "calls": calls,
                        "use": _meta(p).get("toolUseId")})
    linked = []
    progress = True
    while pending and progress:         # nested subagents resolve once their parent has
        progress = False
        rest = []
        for a in pending:
            t = by_use.get(a["use"]) if a["use"] else None
            if t is None:
                rest.append(a)
                continue
            progress = True
            linked.append((t, a))
            for c in a["calls"]:
                if c["id"]:
                    by_use[c["id"]] = t
        pending = rest
    unlinked = 0
    for a in pending:                   # no toolUseId (workflow agents) or parent unseen: by time
        first = next((c["ts"] for c in a["calls"] if c["ts"] is not None), None)
        cands = [t for t in sessions[a["sid"]]["tasks"]
                 if t["ts"] is not None and first is not None and t["ts"] <= first]
        if not cands:
            unlinked += 1
            continue
        linked.append((max(cands, key=lambda t: t["ts"]), a))
    for t, a in linked:
        t["subs"].append(a)

    # task type from the request text, then the text is dropped
    typer = task_typer(_auto_embed() if embed_fn == "auto" else embed_fn)
    ttsrc: Counter = Counter()
    for s in sessions.values():
        for t in s["tasks"]:
            t["task_type"], src = typer(t.pop("text", None))
            ttsrc[src] += 1

    # 3. telemetry, outcomes, splits
    tel = load_telemetry(Path(telemetry) if telemetry else telemetry_path(), set(sessions), st)
    outcomes = load_outcomes(base)
    order = sorted(sessions, key=lambda s: (sessions[s]["start"] or 0.0, s))
    split_of = {}
    n = len(order)
    for r, sid in enumerate(order):
        for name, hi in SPLITS:
            if r < round(hi * n):
                split_of[sid] = name
                break

    # 4. emit
    steps, task_rows = [], []
    mstat: Counter = Counter()
    for sid in order:
        s = sessions[sid]
        for t in s["tasks"]:
            base_rec = {"sid": sid, "src": s["src"], "task": t["task"]}
            main = _stream(t["calls"], base_rec, tel, sid, None, mstat)
            subs = []
            for a in sorted(t["subs"], key=lambda a: (next((c["ts"] for c in a["calls"]
                                                          if c["ts"] is not None), 0.0), a["agent"])):
                subs.extend(_stream(a["calls"], base_rec, tel, sid, a["agent"], mstat))
            steps.extend(main)
            steps.extend(subs)
            tss = [x["ts"] for x in main + subs if x["ts"] is not None]
            oc, osrc = outcomes.get(t["lid"], ("unknown", "none"))
            spawned = bool(t["subs"]) or any(x["spawn"] for x in main)
            task_rows.append({"task": t["task"], "sid": sid, "src": s["src"],
                              "t0": round(t["ts"], 3) if t["ts"] is not None else
                              (min(tss) if tss else None),
                              "t1": max(tss) if tss else None, "steps": len(main),
                              "sub_steps": len(subs), "agents": len(t["subs"]),
                              "outcome": oc, "outcome_src": osrc, "split": split_of[sid],
                              "task_type": t["task_type"], "workflow": "W2" if spawned else "W0"})

    L._write(out_dir / "steps.jsonl", steps)
    L._write(out_dir / "tasks.jsonl", task_rows)
    man = manifest(steps, task_rows, sessions, linked, unlinked, mstat, st, split_of)
    man["counts"]["subagents_orphan"] = orphans
    man["task_type_source"] = dict(sorted(ttsrc.items()))
    man["build_s"] = round(time.time() - t_start, 1)
    _write_json(out_dir / "manifest.json", man)
    if log:
        log(f"wrote {len(steps)} steps, {len(task_rows)} tasks -> {out_dir}")
    return man


def manifest(steps, task_rows, sessions, linked, unlinked, mstat, st, split_of) -> dict:
    cls = Counter(s["act"] for s in steps)
    phases = Counter(s["phase"] for s in steps)
    tests = [s for s in steps if s["tests"]["ran"]]
    parsed = sum(1 for s in tests if s["tests"]["failed"] is not None)
    per_src: dict = {}
    for src in ("pi", "claude"):
        per_src[src] = {"sessions": sum(1 for x in sessions.values() if x["src"] == src),
                        "tasks": sum(1 for t in task_rows if t["src"] == src),
                        "steps_main": sum(1 for s in steps if s["src"] == src and s["agent"] is None),
                        "steps_sub": sum(1 for s in steps if s["src"] == src and s["agent"])}
    per_src["claude"]["subagents"] = len(linked)
    splits = {}
    for name, _ in SPLITS:
        sids = {s for s, v in split_of.items() if v == name}
        splits[name] = {"sessions": len(sids),
                        "tasks": sum(1 for t in task_rows if t["split"] == name),
                        "steps": sum(1 for s in steps if s["sid"] in sids)}
    tss = [s["ts"] for s in steps if s["ts"] is not None]
    oc = Counter(t["outcome_src"] for t in task_rows)
    tt = Counter(t["task_type"] for t in task_rows)
    wf = Counter(t["workflow"] for t in task_rows)
    return {"schema": SCHEMA, "built_at": _iso(time.time()), "git_sha": _git_sha(),
            "counts": {"steps": len(steps), "tasks": len(task_rows), "sessions": len(sessions),
                       "subagents": len(linked), "subagents_unlinked": unlinked,
                       "steps_main": sum(1 for s in steps if s["agent"] is None),
                       "steps_sub": sum(1 for s in steps if s["agent"])},
            "per_source": per_src,
            "classes": {c: cls.get(c, 0) for c in A.CLASSES},
            "phases": {p: phases.get(p, 0) for p in A.PHASES},
            "errors_share": round(sum(s["err"] for s in steps) / max(len(steps), 1), 4),
            "tests": {"test_steps": len(tests), "parsed": parsed,
                      "coverage": round(parsed / len(tests), 4) if tests else None},
            "splits": splits,
            "time_range": {"first": _iso(min(tss)) if tss else None,
                           "last": _iso(max(tss)) if tss else None},
            "outcomes": {"gold": oc.get("gold", 0), "weak": oc.get("weak", 0),
                         "none": oc.get("none", 0)},
            "task_types": {**{k: tt.get(k, 0) for k in TASK_TYPES}, "null": tt.get(None, 0)},
            "workflows": {"W0": wf.get("W0", 0), "W2": wf.get("W2", 0)},
            "model_source": {k: mstat.get(k, 0) for k in ("telemetry", "transcript", "none")},
            "read_errors": {"malformed_lines": st.malformed, "unreadable_files": st.unreadable}}


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=False) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def read_manifest(home=None) -> dict | None:
    try:
        return json.loads((data_home(home) / "manifest.json").read_text())
    except (OSError, ValueError):
        return None
