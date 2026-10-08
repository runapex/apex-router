"""System-wide sources: GPU (``ioreg``), ollama (``/api/ps`` and who is connected to it) and
the physical interfaces' byte counters (``netstat``)."""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time

from .base import _mb, run_cmd
from .procs import _LABEL_RE, is_interpreter, script_label


OLLAMA_URL = "http://127.0.0.1:11434/api/ps"
_IOREG_UTIL_RE = re.compile(r'"Device Utilization %"\s*=\s*(\d+)')
_IOREG_MEM_RE = re.compile(r'"In use system memory"\s*=\s*(\d+)')
_IOREG_ALLOC_RE = re.compile(r'"Alloc system memory"\s*=\s*(\d+)')
# keep_alive -1 is reported as now + max time.Duration (~292 years: "2319-01-17..."); any finite
# keep_alive a person sets is far below 100 years, so the gap is unambiguous.
OLLAMA_PINNED_S = 100 * 365 * 86400


# apex-router's own subcommands: a fixed vocabulary, so naming one never leaks an argument.
_APEX_SUBCMDS = {"labels", "xval", "zeno", "snapshot", "serve", "nightly", "rag-nightly",
                 "chain-bench", "chain-planner", "skill-bench", "review-preread", "route-advise",
                 "route-join", "pressure", "watch", "ornith-tier", "proxy"}


def client_label(args: str) -> str | None:
    """A process that talks to ollama, by name: ``apex-router labels`` for our own CLI (module
    or entrypoint form), else the script / program basename. Never other argv text."""
    toks = (args or "").split()
    if not toks:
        return None
    rest = None
    if "apex_router.cli" in toks:
        rest = toks[toks.index("apex_router.cli") + 1:]
    elif os.path.basename(toks[0]) == "apex-router":
        rest = toks[1:]
    elif len(toks) > 1 and os.path.basename(toks[1]) == "apex-router":
        rest = toks[2:]
    if rest is not None:
        sub = next((t for t in rest if not t.startswith("-")), "")
        return "apex-router" + (f" {sub}" if sub in _APEX_SUBCMDS else "")
    lab = script_label(args) if is_interpreter(os.path.basename(toks[0])) else None
    if lab:
        return lab
    base = os.path.basename(toks[0])
    return base if _LABEL_RE.match(base) else None


OLLAMA_PORT = 11434


def ollama_clients(run=run_cmd, timeout: float = 0.5, port: int = OLLAMA_PORT) -> list:
    """[{pid, name}] of processes with an open connection to ollama's API — who is driving the
    GPU when a model is busy. ollama's own processes are left out."""
    try:
        text = run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED", "-Fpc"], timeout=timeout)
    except RuntimeError as e:
        if str(e).endswith("exit 1"):   # lsof: exit 1 + no output = nothing matched, not a failure
            return []
        raise
    pids, cur, name = set(), None, {}
    for ln in (text or "").splitlines():
        if ln.startswith("p") and ln[1:].isdigit():
            cur = int(ln[1:])
        elif ln.startswith("c") and cur is not None:
            name[cur] = ln[1:]
            if not ln[1:].startswith("ollama"):
                pids.add(cur)
    if not pids:
        return []
    out = run(["ps", "-o", "pid=,args=", "-p", ",".join(str(p) for p in sorted(pids))],
              timeout=timeout)
    rows = []
    for line in (out or "").splitlines():
        t = line.strip().split(None, 1)
        if len(t) == 2 and t[0].isdigit():
            rows.append({"pid": int(t[0]), "name": client_label(t[1]) or name.get(int(t[0]), "?")})
    return rows


# ---- GPU / ollama ---------------------------------------------------------------------------

_NETSTAT_IF_RE = re.compile(r"^(en\d+)\s")


def parse_netstat(text: str) -> dict | None:
    """Physical interfaces' cumulative bytes from ``netstat -ibn`` (64-bit counters, unlike
    getifaddrs' 32-bit ones, which wrap every 4GB): ``{"rx": B, "tx": B, "ifs": "en0"}`` summed
    over the ``en*`` link rows. VPN tunnels (utun*) are left out — their traffic also crosses an
    ``en`` interface, so adding them would count it twice; loopback is local. None without rows."""
    lines = (text or "").splitlines()
    if not lines:
        return None
    head = lines[0].split()
    try:
        i_in, i_out = head.index("Ibytes"), head.index("Obytes")
    except ValueError:
        return None
    rx = tx = 0
    ifs = []
    for ln in lines[1:]:
        cols = ln.split()
        if not _NETSTAT_IF_RE.match(ln) or len(cols) < len(head) - 1 or "<Link#" not in ln:
            continue
        # a link row with no address has one column fewer: count from the right
        try:
            rxb, txb = int(cols[i_in - len(head)]), int(cols[i_out - len(head)])
        except (ValueError, IndexError):
            continue
        if rxb or txb:
            rx, tx = rx + rxb, tx + txb
            ifs.append(cols[0])
    return {"rx": rx, "tx": tx, "ifs": ",".join(sorted(set(ifs)))} if ifs else None


def parse_ioreg(text: str) -> dict:
    util = [int(m) for m in _IOREG_UTIL_RE.findall(text or "")]
    # "Alloc system memory" is what the GPU holds (ollama's resident models included);
    # "In use system memory" is only the current working set (1.4GB beside a 26GB model).
    # Per accelerator (ioreg starts each with "+-o"): Alloc, else that one's In use.
    mem = []
    for block in re.split(r"(?m)^\+-o ", text or ""):
        vals = [int(m) for m in _IOREG_ALLOC_RE.findall(block)] \
            or [int(m) for m in _IOREG_MEM_RE.findall(block)]
        mem += vals
    return {"gpu_util_pct": max(util) if util else None,
            "gpu_mem_mb": _mb(sum(mem)) if mem else None}


_FRAC_RE = re.compile(r"(\.\d{6})\d+")


def _iso_epoch(v) -> float | None:
    """ollama's ``expires_at`` (RFC 3339, nanosecond fraction, offset or Z) -> epoch seconds."""
    if not isinstance(v, str) or not v:
        return None
    s = _FRAC_RE.sub(r"\1", v.strip()).replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        return None
    return d.timestamp()


def parse_ollama(body, now: float | None = None) -> list:
    doc = json.loads(body)
    now = time.time() if now is None else now
    out = []
    for m in (doc.get("models") or []) if isinstance(doc, dict) else []:
        if not isinstance(m, dict) or not isinstance(m.get("name"), str):
            continue
        row = {"name": m["name"], "size_mb": _mb(m.get("size")), "vram_mb": _mb(m.get("size_vram"))}
        exp = _iso_epoch(m.get("expires_at"))
        if exp is not None and exp - now > OLLAMA_PINNED_S:
            row["pinned"] = True                     # keep_alive -1: never unloads
        elif exp is not None:
            row["unloads_in_s"] = round(max(0.0, exp - now))
        out.append(row)
    return out
