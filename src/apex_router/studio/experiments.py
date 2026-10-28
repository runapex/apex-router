"""Experiment cards for studio v2 (``GET /api/experiments``). Read-only, fail-open.

Each reader is fixed in code (no path comes from a request), reads at most ``CAP`` bytes of each
file, and returns one item in the contract shape (``docs/design/studio-v2/API.md``). Any error
gives ``state: "unknown"`` and a short fixed ``error`` ("missing", "unreadable", "bad format",
"too large"); never exception text. Headlines are built from numbers and fixed words; a string
taken from a file (an arm, a loop or a cell name) passes ``_safe`` (a short allow-listed token).

Roots (overridable, so tests use a fixture home):
  repo        $APEX_STUDIO_REPO, else the checkout this package is in (docs/research, RESEARCH-MAP)
  router home Paths.router_home (labels/gold, explore, xval_*, panel, pi-compact)
  ml          $APEX_STUDIO_ML_ROOT, else ~/dev/ml (rsi/outside, logmine, sec/swarm)
"""
from __future__ import annotations

import contextvars
import csv
import datetime as _dt
import fcntl
import io
import json
import os
import re
import time
from pathlib import Path

from .flow import Paths, tilde

CAP = 1024 * 1024
STALE_S = 7 * 86400
DIRS_MAX = 500
FILES_MAX = 50
_SAFE_RE = re.compile(r"[^A-Za-z0-9 ._:/()+-]")
_DATE_RE = re.compile(r"(20\d\d)-(\d\d)-(\d\d)")


class ReadError(Exception):
    pass


# ---- roots and bounded reads -----------------------------------------------------------------

def repo_root(paths: Paths) -> Path:
    v = paths.env.get("APEX_STUDIO_REPO")
    return Path(v).expanduser() if v else Path(__file__).resolve().parents[3]


def ml_root(paths: Paths) -> Path:
    v = paths.env.get("APEX_STUDIO_ML_ROOT")
    return Path(v).expanduser() if v else Path(paths.env.get("HOME") or Path.home()) / "dev" / "ml"


_ROOTS: contextvars.ContextVar = contextvars.ContextVar("experiment_roots", default=())


def _under_roots(real: str) -> bool:
    roots = _ROOTS.get()
    if not roots:
        return False                                     # fail closed: no roots set, no read
    return any(real == r or real.startswith(r + os.sep) for r in roots)


def _inside_roots(path: Path) -> bool:
    """Early check by name (every link resolved). The binding check is _fd_inside_roots."""
    try:
        return _under_roots(os.path.realpath(path))
    except OSError:
        return False


def _fd_inside_roots(fd: int) -> bool:
    """The path of the file we HAVE OPEN (F_GETPATH): a swap of a parent directory after the
    name check cannot move the read outside. Without F_GETPATH: fail closed."""
    getpath = getattr(fcntl, "F_GETPATH", None)
    if getpath is None:
        return False
    try:
        real = fcntl.fcntl(fd, getpath, b"\0" * 1024).split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
        return _under_roots(real)                        # F_GETPATH is already the real path: no
                                                         # second name lookup (that would race again)
    except OSError:
        return False


def _read(path: Path, cap: int = CAP, tail: bool = False, whole: bool = False) -> bytes:
    """At most ``cap`` bytes (head, or tail). ``whole``: a file larger than cap is an error.
    Only files whose real path is under the collect roots (a linked directory cannot lead out)."""
    if not _inside_roots(path):
        raise ReadError("outside its root" if os.path.lexists(path) else "missing")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)   # a link can point outside the root: refuse
    except FileNotFoundError:
        raise ReadError("missing") from None
    except OSError:
        raise ReadError("unreadable") from None
    if not _fd_inside_roots(fd):                         # check the open file, not the name
        os.close(fd)
        raise ReadError("outside its root")
    try:
        with os.fdopen(fd, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if whole and size > cap:
                raise ReadError("too large")
            if tail and size > cap:
                f.seek(size - cap)
            return f.read(cap)
    except FileNotFoundError:
        raise ReadError("missing") from None
    except IsADirectoryError:
        raise ReadError("unreadable") from None
    except OSError:
        raise ReadError("unreadable") from None


def _text(path: Path) -> str:
    return _read(path).decode("utf-8", "replace")


def _json(path: Path):
    try:
        return json.loads(_read(path, whole=True))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ReadError("bad format") from None


def _jsonl(path: Path, tail: bool = True) -> list:
    data = _read(path, tail=tail)
    lines = data.split(b"\n")
    if tail and len(data) >= CAP and lines:
        lines = lines[1:]
    out = []
    for ln in lines:
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def _mtime(p: Path):
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _date_ts(name: str):
    m = _DATE_RE.search(name)
    if not m:
        return None
    try:
        return _dt.datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return None


def _iso_ts(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) / 1000.0 if v > 1e11 else float(v)
    if not isinstance(v, str):
        return None
    try:
        d = _dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp()


def _safe(v, n: int = 40) -> str:
    return _SAFE_RE.sub("", str(v))[:n].strip() or "?"


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _newest(paths_iter, cap: int = DIRS_MAX):
    best, best_m = None, None
    for i, p in enumerate(paths_iter):
        if i >= cap:
            break
        m = _mtime(p)
        if m is not None and (best_m is None or m > best_m):
            best, best_m = p, m
    return best


def _f(v, nd: int = 1) -> str:
    return f"{v:.{nd}f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


# ---- item shape ------------------------------------------------------------------------------

HEADLINE_MAX = 160
_SECRETISH = re.compile(r"(?:sk-|xox[abp]-|ghp_|gho_|eyJ|AKIA)[A-Za-z0-9._-]{6,}|[A-Za-z0-9+/_=-]{32,}")


def _clean(v, n: int = HEADLINE_MAX):
    """One output text: secret-shaped words masked, one line, at most n chars."""
    if v is None:
        return None
    t = _SECRETISH.sub("[hidden]", " ".join(str(v).split()))
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def _clean_metric(m):
    if not isinstance(m, dict):
        return None
    v = m.get("value")
    return {"name": _clean(m.get("name"), 40), "value": v if _num(v) is not None else None,
            "unit": _clean(m.get("unit"), 8) or ""}


def _clean_gate(g):
    if not isinstance(g, dict):
        return None
    p = g.get("passed")
    return {"name": _clean(g.get("name"), 40), "passed": p if isinstance(p, bool) else None}


def _item(id_, name, source, state="unknown", headline="", metric=None, gate=None, n=None,
          updated_ts=None, error=None, now=None):
    now = time.time() if now is None else now
    return {"id": id_, "name": name, "state": state, "headline": _clean(headline) or "",
            "metric": _clean_metric(metric), "gate": _clean_gate(gate),
            "n": n if _num(n) is not None else None, "updated_ts": round(updated_ts) if updated_ts else None,
            "stale": bool(updated_ts) and now - updated_ts > STALE_S, "source": tilde(source),
            "error": _clean(error, 80)}


def _metric(name, value, unit=""):
    return {"name": name, "value": value, "unit": unit} if value is not None else None


# ---- readers ---------------------------------------------------------------------------------

JEPA_DOC = "docs/research/2026-10-08-p6-g1-attempt-3.md"


def read_jepa(paths: Paths, now: float) -> dict:
    root = repo_root(paths)
    doc = root / JEPA_DOC
    txt = _text(doc)
    title = txt.split("\n", 1)[0]
    g = re.search(r"\b(G\d)\s+(PASS|FAIL)\b", title)
    if not g:
        raise ReadError("bad format")
    passed = g[2] == "PASS"
    m = re.search(r"C1\s+(?:PASS|FAIL)\**\s+[-—–]+\s+rel\s+([−-]?\d+(?:\.\d+)?)\s*%", txt)
    rel = float(m[1].replace("−", "-")) if m else None
    nm = re.search(r"today\s+(\d+(?:\.\d+)?)k\s+main", txt)
    n = int(round(float(nm[1]) * 1000)) if nm else None
    # status row of the research map (fixed row "| JEPA |")
    state = "done" if passed else "failed"
    try:
        for ln in _text(root / "docs" / "RESEARCH-MAP.md").splitlines():
            if ln.startswith("| JEPA |"):
                low = ln.lower()
                state = ("paused" if "paused" in low else "running" if "running" in low
                         else "failed" if "fail" in low else state)
                break
    except ReadError:
        pass
    stop = "stop rule fires" in title.lower() or state == "paused"
    head = f"Gate {g[1]} {'passed' if passed else 'failed'}." + (" Paused by its stop rule." if stop else "")
    return _item("jepa", "JEPA world model", doc, state, head, _metric("C1 rel", rel, "%"),
                 {"name": g[1], "passed": passed}, n, _date_ts(doc.name), now=now)


def read_gold(paths: Paths, now: float) -> dict:
    d = paths.router_home / "labels" / "gold"
    res = d / "results_for_mac_home.txt"
    txt = _text(res)
    st = re.search(r"STATUS:\s*([A-Z]+)", txt)
    if not st:
        raise ReadError("bad format")
    state = {"PROVISIONAL": "provisional", "FINAL": "done", "DONE": "done"}.get(st[1], "unknown")
    ag = re.search(r"outcome agreement\s+(\d+(?:\.\d+)?)%,\s*kappa\s+(\d+(?:\.\d+)?)", txt)
    rows = re.search(r"compare\.py:\s*(\d+)\s+rows", txt)
    adj = d / "adjudicate.csv"
    total = done = None
    try:
        raw = _read(adj, whole=True).decode("utf-8", "replace")
        rdr = csv.DictReader(io.StringIO(raw))
        total = done = 0
        for r in rdr:
            total += 1
            done += 1 if (r.get("owner_outcome") or "").strip() else 0
    except (ReadError, csv.Error):
        total = done = None
    finalized = not re.search(r"finalize\.py NOT run", txt)
    parts = [f"Gold labels {st[1].lower()}."]
    if total is not None:
        parts.append(f"Owner adjudicated {done} of {total} disputed rows.")
    if ag:
        parts.append(f"Kappa {ag[2]}.")
    upd = max([t for t in (_mtime(res), _mtime(adj)) if t is not None], default=None)
    return _item("gold", "Gold labels", res, state, " ".join(parts),
                 _metric("outcome agreement", float(ag[1]) if ag else None, "%"),
                 {"name": "finalized", "passed": finalized}, int(rows[1]) if rows else None, upd, now=now)


XVAL_DOC = "docs/research/2026-10-08-gold-xval.md"


def read_label_judge(paths: Paths, now: float) -> dict:
    doc = repo_root(paths) / XVAL_DOC
    title = _text(doc).split("\n", 1)[0]
    m = re.search(r"(\d+)\s+labels,\s*(\d+)\s+corrections,\s*judge\s+(\d?\.\d+)\s+held out", title)
    if not m:
        raise ReadError("bad format")
    acc = float(m[3])
    head = f"Judge {m[3]} held out. {m[1]} owner gold labels, {m[2]} corrections."
    return _item("label_judge", "Label judge", doc, "done", head, _metric("judge accuracy", acc),
                 None, int(m[1]), _date_ts(doc.name), now=now)


def read_explore(paths: Paths, now: float) -> dict:
    base = paths.router_home / "explore"
    try:
        dirs = (p for p in base.iterdir() if p.is_dir() and not p.is_symlink())
        d = _newest(dirs)
    except OSError:
        d = None
    if d is None:
        raise ReadError("missing")
    res = d / "results.jsonl"
    rows = _jsonl(res, tail=True)
    arms = {}
    for r in rows:
        arms[str(r.get("arm"))] = r
    if not arms:
        raise ReadError("bad format")
    ex, bl = arms.get("explore"), arms.get("baseline")
    best = _num((ex or {}).get("best_score"))
    hit = any(r.get("hit") is True for r in arms.values())
    n = sum(int(r["calls"]) for r in arms.values() if _num(r.get("calls")) is not None) or None
    upd = _mtime(res)
    state = "done" if (d / "table.md").is_file() else ("running" if upd and now - upd < 3600 else "paused")
    task = _safe(next(iter(arms.values())).get("task"))
    if ex and bl and _num(ex.get("best_score")) is not None and _num(bl.get("best_score")) is not None:
        cmp_ = ("ties" if ex["best_score"] == bl["best_score"]
                else "beats" if ex["best_score"] > bl["best_score"] else "loses to")
        head = f"{task}: explore {cmp_} baseline ({_f(float(ex['best_score']), 3)})."
    else:
        head = f"{task}: {len(arms)} arm(s) scored."
    head += " Target reached." if hit else " Target not reached."
    tau = _num((ex or bl or {}).get("tau"))
    return _item("explore", "Explore search", res, state, head,
                 _metric("best score", round(float(best), 4) if best is not None else None),
                 {"name": f"tau {tau}" if tau is not None else "tau", "passed": hit}, n, upd, now=now)


def read_xval(paths: Paths, now: float) -> dict:
    bf = paths.router_home / "xval_bandit.json"
    d = _json(bf)
    cats = d.get("categories") if isinstance(d, dict) else None
    if not isinstance(cats, dict):
        raise ReadError("bad format")
    pulls = arms = 0
    for c in cats.values():
        for a in ((c or {}).get("arms") or {}).values() if isinstance(c, dict) else ():
            if isinstance(a, dict) and _num(a.get("n")) is not None:
                pulls += int(a["n"])
                arms += 1
    ok_pct = last = None
    runs = paths.router_home / "xval_runs.jsonl"
    try:
        rows = _jsonl(runs, tail=True)[-50:]
        if rows:
            ok_pct = round(100.0 * sum(1 for r in rows if r.get("ok") is True) / len(rows), 1)
            last = max((t for t in (_iso_ts(r.get("ts")) for r in rows) if t), default=None)
    except ReadError:
        pass
    upd = last or _mtime(bf)
    state = "running" if upd and now - upd < 86400 else "paused"
    head = f"{pulls} pulls over {arms} arms in {len(cats)} categories."
    if ok_pct is not None:
        head += f" {_f(ok_pct)}% of the last runs ok."
    return _item("xval_bandit", "xval bandit", bf, state, head, _metric("runs ok (last 50)", ok_pct, "%"),
                 None, pulls, upd, now=now)


def read_panel(paths: Paths, now: float) -> dict:
    base = paths.router_home / "panel"
    try:
        f = _newest(p for p in base.glob("card-*.json") if not p.is_symlink())
    except OSError:
        f = None
    if f is None:
        raise ReadError("missing")
    d = _json(f)
    if not isinstance(d, dict):
        raise ReadError("bad format")
    gates = d.get("gate") if isinstance(d.get("gate"), list) else []
    g0 = gates[0] if gates and isinstance(gates[0], dict) else None
    con = [c for c in (d.get("contrasts") or []) if isinstance(c, dict)]
    c0 = next((c for c in con if "primary" in str(c.get("name", ""))), con[0] if con else None)
    meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
    inc = _safe(d.get("incumbent") or (g0 or {}).get("chosen_model") or "incumbent")
    if g0 is None:
        raise ReadError("bad format")
    promoted = g0.get("promoted") is True
    head = (f"Promoted over {inc}." if promoted else f"No promotion over {inc}. Thread closed.")
    if c0 and isinstance(c0.get("status"), str):
        head += f" Primary: {_safe(c0['status'], 40)}."
    est = _num((c0 or {}).get("estimate"))
    return _item("panel", "Panel debate", f, "done", head,
                 _metric(_safe((c0 or {}).get("metric") or "estimate"), round(est, 4) if est is not None else None),
                 {"name": _safe(g0.get("cell_id") or "gate"), "passed": promoted},
                 int(meta["n"]) if _num(meta.get("n")) is not None else None,
                 _iso_ts(meta.get("ts")) or _mtime(f), now=now)


def read_rsi(paths: Paths, now: float) -> dict:
    f = ml_root(paths) / "rsi" / "outside" / "judge_runs.jsonl"
    rows = [r for r in _jsonl(f, tail=True) if _num(r.get("total")) is not None and _num(r.get("max"))]
    if not rows:
        raise ReadError("bad format")
    last = rows[-1]
    loop = last.get("loop")
    same = [r for r in rows if r.get("loop") == loop]
    pct = round(100.0 * last["total"] / last["max"], 1)
    upd = _iso_ts(last.get("ts")) or _mtime(f)
    state = "running" if upd and now - upd < 2 * 86400 else "paused"
    head = (f"Loop {_safe(loop)}: last outside score {_f(float(last['total']))}/{_f(last['max'])} "
            f"({len(same)} judged runs).")
    return _item("rsi_outside", "RSI outside score", f, state, head, _metric("outside score", pct, "%"),
                 None, len(same), upd, now=now)


def read_logmine(paths: Paths, now: float) -> dict:
    f = ml_root(paths) / "logmine" / "data" / "oracle_coverage.json"
    d = _json(f)
    a = d.get("all") if isinstance(d, dict) else None
    if not isinstance(a, dict) or _num(a.get("recall")) is None:
        raise ReadError("bad format")
    rec = round(100.0 * a["recall"], 1)
    head = f"Oracle recall {_f(rec)}% over {a.get('signals', '?')} signals."
    gd = _num(d.get("goldens_recall_ml_drift"))
    if gd is not None:
        head += f" Goldens {_f(round(100.0 * gd, 1))}% with drift."
    return _item("logmine", "logmine", f, "done", head, _metric("recall", rec, "%"), None,
                 int(a["signals"]) if _num(a.get("signals")) is not None else None,
                 _iso_ts(d.get("t_end")) or _mtime(f), now=now)


def read_swarm(paths: Paths, now: float) -> dict:
    f = ml_root(paths) / "sec" / "swarm" / "data" / "STATUS.json"
    d = _json(f)
    if not isinstance(d, dict) or not isinstance(d.get("state"), str):
        raise ReadError("bad format")
    state = {"DONE": "done", "RUNNING": "running", "FAILED": "failed", "ERROR": "failed",
             "PAUSED": "paused"}.get(d["state"].upper(), "unknown")
    if state == "done" and d.get("ok") is False:
        state = "failed"
    ev = (d.get("evaluate") or {}).get("summary") if isinstance(d.get("evaluate"), dict) else None
    ev = ev if isinstance(ev, dict) else {}
    q = ev.get("queue") if isinstance(ev.get("queue"), dict) else {}
    qlen = _num(q.get("length"))
    gate_open = ev.get("gate_open")
    anomalies = d.get("anomalies") if isinstance(d.get("anomalies"), list) else []
    head = f"Last run {'ok' if d.get('ok') is True else 'not ok'}."
    if qlen is not None:
        head += f" {int(qlen)} units queued."
    if isinstance(gate_open, bool):
        head += f" Gate {'open' if gate_open else 'closed'}."
    if anomalies:
        head += f" {len(anomalies)} anomalies."
    return _item("sec_swarm", "sec swarm", f, state, head, _metric("queue", int(qlen) if qlen is not None else None),
                 {"name": "evaluate", "passed": gate_open} if isinstance(gate_open, bool) else None,
                 _num(ev.get("units")), _num(d.get("last_complete")) or _num(d.get("finished")) or _mtime(f),
                 now=now)


COMPACT_DOC = "docs/research/2026-10-26-pi-compaction-precision-cost.md"


def read_compaction(paths: Paths, now: float) -> dict:
    d = paths.router_home / "pi-compact"
    doc = repo_root(paths) / COMPACT_DOC
    rows, seen = [], False
    try:
        files = sorted((p for p in d.glob("*.jsonl") if p.is_file() and not p.is_symlink()),
                       key=lambda p: _mtime(p) or 0)[-FILES_MAX:]
    except OSError:
        files = []
    for p in files:
        try:
            rows += _jsonl(p, tail=True)
            seen = True
        except ReadError:
            pass
    study, doc_seen = None, False
    try:
        m = re.search(r"Results \((\d+) compactions, (\d+) sessions", _text(doc))
        doc_seen = True
        study = (int(m[1]), int(m[2])) if m else None
    except ReadError:
        pass
    if not rows and study is None:
        raise ReadError("bad format" if seen or doc_seen else "missing")
    week = [r for r in rows if (_iso_ts(r.get("ts")) or 0) >= now - STALE_S]
    tb = sorted(r["tokens_before"] for r in week if _num(r.get("tokens_before")) is not None)
    med = tb[len(tb) // 2] if tb else None
    last = max((t for t in (_iso_ts(r.get("ts")) for r in rows) if t), default=None)
    head = f"Compactions in the last 7 days: {len(week)}."
    if study:
        head += f" Study: {study[0]} compactions over {study[1]} sessions."
    state = "running" if last and now - last < STALE_S else "paused"
    return _item("compaction", "pi compaction", d if seen else doc, state, head,
                 _metric("median tokens before", med), None, len(week), last or _date_ts(doc.name), now=now)


READERS = (("jepa", "JEPA world model", read_jepa), ("gold", "Gold labels", read_gold),
           ("label_judge", "Label judge", read_label_judge), ("explore", "Explore search", read_explore),
           ("xval_bandit", "xval bandit", read_xval), ("panel", "Panel debate", read_panel),
           ("rsi_outside", "RSI outside score", read_rsi), ("logmine", "logmine", read_logmine),
           ("sec_swarm", "sec swarm", read_swarm), ("compaction", "pi compaction", read_compaction))

_SOURCES = {"jepa": lambda p: repo_root(p) / JEPA_DOC,
            "gold": lambda p: p.router_home / "labels" / "gold" / "results_for_mac_home.txt",
            "label_judge": lambda p: repo_root(p) / XVAL_DOC,
            "explore": lambda p: p.router_home / "explore",
            "xval_bandit": lambda p: p.router_home / "xval_bandit.json",
            "panel": lambda p: p.router_home / "panel",
            "rsi_outside": lambda p: ml_root(p) / "rsi" / "outside" / "judge_runs.jsonl",
            "logmine": lambda p: ml_root(p) / "logmine" / "data" / "oracle_coverage.json",
            "sec_swarm": lambda p: ml_root(p) / "sec" / "swarm" / "data" / "STATUS.json",
            "compaction": lambda p: p.router_home / "pi-compact"}


def roots(paths: Paths) -> tuple:
    """The real paths of the 3 roots, resolved once (the roots themselves may be links)."""
    out = []
    for r in (repo_root(paths), ml_root(paths), paths.router_home):
        try:
            out.append(os.path.realpath(r))
        except OSError:
            pass
    return tuple(out)


def collect(paths: Paths, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    token = _ROOTS.set(roots(paths))
    try:
        return _collect(paths, now)
    finally:
        _ROOTS.reset(token)


def _collect(paths: Paths, now: float) -> dict:
    items = []
    for id_, name, fn in READERS:
        try:
            items.append(fn(paths, now))
        except ReadError as e:
            items.append(_item(id_, name, _SOURCES[id_](paths), error=str(e), now=now))
        except Exception:  # noqa: BLE001 — one broken source never hides the others
            items.append(_item(id_, name, _SOURCES[id_](paths), error="bad format", now=now))
    return {"items": items}
