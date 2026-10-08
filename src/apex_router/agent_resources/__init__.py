"""Per-agent resources and the call graph for the menu-bar widget — read-only, fail-open.

Ties each agent from ``agents.discover()`` to the OS processes it runs and the model calls it
makes. What is read, and nothing more:

  process table  ONE ``ps -axo pid=,ppid=,rss=,%cpu=,lstart=,comm=``, plus at most ONE
                 ``ps -o pid=,args= -p <pids>`` for interpreter processes (node, python, ruby, …)
                 inside agent trees (≤ 40 pids). From ``args`` only the basename of the first
                 script argument is kept (``pyright-langserver``); the rest of argv is discarded
                 and never stored or shown.
  libproc        ``proc_pid_rusage(pid, RUSAGE_INFO_V2)`` via ctypes (macOS, unprivileged for the
                 user's own processes): physical footprint, disk bytes read/written, cpu time.
                 ``user``/``system`` are mach absolute-time ticks, converted with
                 ``mach_timebase_info`` (125/3 on Apple Silicon), not nanoseconds. Sampled TWICE
                 ~250 ms apart for the busiest ≤ 40 agent-tree pids: the difference gives the true
                 cpu % and disk read/write MB/s now. Lifetime io and cpu seconds are tooltip detail.
  Claude Code    ``~/.claude/sessions/<pid>.json`` (``*.json`` only) — exact pid -> sessionId and
                 Claude's own busy/idle status. A file whose pid is dead, or whose ``procStart``
                 does not match the live process's start time, is ignored. Subagent sidecars
                 ``agent-<id>.meta.json`` (agentType, description, spawnDepth; its mtime is the
                 spawn time), the mtime of ``agent-<id>.jsonl`` (the last write) and, for a busy
                 session, the mtime of the session's own log (the ``waiting?`` heuristic).
  pi / Codex     ONE ``lsof -a -d cwd -Fpn -p <pids>`` (≤ 40 pids) maps the process to its cwd;
                 a process is credited to a session only when it is the sole process of its kind
                 in that cwd and the cwd has one listed session of that kind. For the menu
                 (``quiet=True``) the lookup also runs when no such session is listed: a live pi
                 / Codex process no listed session accounts for (no log written in the last hour)
                 is listed as quiet with its cwd's basename, uptime and memory.
  GPU            ``ioreg -r -d 1 -c IOAccelerator`` — SYSTEM-WIDE utilisation and in-use memory.
                 Per-process GPU needs root and is not attempted.
  ollama         ``GET http://127.0.0.1:11434/api/ps`` — loaded models, VRAM, unload time.
  telemetry      the last 60 min of ``~/.apex/telemetry.jsonl`` via ``pressure.tail_rows``:
                 requests, the token split (uncached input / cache read / cache write /
                 output), errors, ttft and the context size of the LATEST request, split by
                 session (main thread, agent_id null) and by subagent (agent_id). Only proxy
                 traffic is seen.

No dollar figures: tokens, cache share and context fill only. The context window comes from a
family table (``context_window``): Opus / Sonnet >= 4.6 and Fable -> 1M, Haiku 4.5 -> 200k, a
``[1m]`` suffix -> 1M (VERIFIED 2026-10-06 against the pi 0.99.1 model catalog and a live
283k-token request on claude-opus-5-5). Any other id has NO assumed window — its size is shown
absolute — unless a successful request of the same thread and model exceeded 200k in the window,
which proves 1M.

A request error flags an agent only when it is recent (last 5 min) or the 60-min error rate is
>= 5% (``err_flag``); one old transient error does not.

``waiting?`` is a HEURISTIC (``mark_waiting``): a hung subagent and a finished one both stop
writing, so a hung one otherwise looks done. While the session is ``busy``, the subagent whose log
went quiet (> 60 s) LAST — nothing in the main log or any other subagent written since — is the
one the session is probably waiting on. It is a ⚠ reason after 10 min. A subagent in a long tool
call (a test run, a build) looks the same; the question mark stays.

All subprocess and network calls share one deadline (``DEADLINE_S``, the rate-sample window
included), and so does the subagent filesystem scan; a source that would start after it is
skipped and recorded in ``system.errors``. Subagent logs are scanned only for the active sessions
the menu shows (``SCAN_SESSIONS_MAX``, ranked like the menu); every other session gets its
subagents from proxy traffic alone. The widget's own process, its descendants
and its ancestors below the session root are excluded from every tree, so a refresh run inside a
session does not count itself. Subagents run inside their Claude session's process, so OS
resources are per session, never per subagent; a subagent's "load" is its proxy traffic. No
prompt or transcript content is read.

Layout: ``base`` (deadline, command / HTTP runners), ``procs`` (ps, trees, rusage, rates),
``sessions`` (Claude / pi / Codex pid <-> session, quiet processes), ``telemetry`` (split, stats,
context), ``subagents``, ``system`` (GPU, ollama, network), ``graph``, ``text`` (formatting).
Every name is re-exported here; ``collect`` lives here so the injectable sources (``run_cmd``,
``rusage``, ``http_get``, ``_sleep``, ``_clock``) are looked up on this package at call time.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .. import pressure  # noqa: F401 — tests and callers reach it as ar.pressure
from .base import (  # noqa: F401 — re-exported
    PROCS_TOP, GRAPH_PROCS_MAX, GRAPH_MODELS_MAX, GRAPH_SESSIONS_MAX, DEADLINE_S,
    DEADLINE_MIN_S, _MB, _LSTART_FMT, _sleep, _clock, _err, _mb, _num, run_cmd, http_get,
    DeadlineSkip, Deadline,
)
from .text import (  # noqa: F401 — re-exported
    _CTRL_RE, clean_text, fmt_dur, fmt_age, fmt_mb, fmt_mem, _k, display_state, io_rate,
    fmt_rate, metrics_text, fmt_pct, err_text, _kc, fmt_bytes, net_text, fmt_rate_min,
    tokens_text, ctx_text, tel_text, lifecycle_text,
)
from .procs import (  # noqa: F401 — re-exported
    TREE_PIDS_MAX, RATE_PIDS_MAX, ARGS_PIDS_MAX, RATE_SAMPLE_S, _parse_lstart, parse_ps,
    ps_table, children_map, descendants, self_exclusion, _INTERPRETERS, _INTERP_RE,
    _ABORT_FLAGS, _VALUE_FLAGS, _SUBCOMMANDS, _SCRIPT_EXT, _LABEL_RE, _MODULE_RE,
    is_interpreter, script_label, parse_ps_args, script_labels, _RusageV2, _Timebase, _LIBS,
    mach_ticks_to_s, _libs, rusage, _safe_rusage, rates_from_samples, tree_metrics,
)
from .sessions import (  # noqa: F401 — re-exported
    LSOF_PIDS_MAX, QUIET_PROCS_MAX, SESSION_BYTES_MAX, START_TOLERANCE_S, _start_matches,
    claude_sessions, parse_lsof_cwd, lsof_cwds, match_by_cwd, quiet_procs,
)
from .telemetry import (  # noqa: F401 — re-exported
    TELEMETRY_WINDOW_S, CTX_WARN_PCT, CTX_200K_WARN, CTX_OBSERVED_1M, ERR_RECENT_S,
    ERR_RATE_WARN, RATE_WINDOW_S, _CTX_1M_RE, _FAMILY_RE, fresh_input, _cache_read,
    _cache_write, context_window, context_of, cache_share, _ts, _stats, _SUMMED, merge_stats,
    ctx_flag, err_rate, err_flag, telemetry_split, _buckets,
)
from .subagents import (  # noqa: F401 — re-exported
    SUBAGENT_RUNNING_S, SUBAGENT_QUIET_S, SUBAGENT_STUCK_S, SUBAGENT_WAITING_FLAG_S,
    SUBAGENTS_MAX, SUBAGENT_SCAN_MAX, SCAN_SESSIONS_MAX, META_BYTES_MAX, _AGENT_FILE_RE,
    _lifecycle, _sub_sort_key, _last_s, mark_waiting, subagents, session_totals, agent_flagged,
    agent_active, rank_key,
)
from .system import (  # noqa: F401 — re-exported
    OLLAMA_URL, _IOREG_UTIL_RE, _IOREG_MEM_RE, _IOREG_ALLOC_RE, OLLAMA_PINNED_S, _APEX_SUBCMDS,
    client_label, OLLAMA_PORT, ollama_clients, _NETSTAT_IF_RE, parse_netstat, parse_ioreg,
    _FRAC_RE, _iso_epoch, parse_ollama,
)
from .graph import (  # noqa: F401 — re-exported
    GRAPH_REPO_MAX, GRAPH_WIDTH, _session_label, _age_since, build_graph, graph_text,
)


# ---- collect --------------------------------------------------------------------------------

def collect(agents: list, *, home=None, telemetry=None, now: float | None = None,
            run=None, rusage_fn=None, fetch=None, loadavg=None, worker_pid=None,
            deadline: Deadline | None = None, self_pid: int | None = None,
            sample_s: float = RATE_SAMPLE_S, quiet: bool = False) -> dict:
    """Enrich ``agents`` (a new list; each matched agent gains ``res``) and return
    ``{"agents", "system", "graph", "worker"}``. The injectables default to the real sources,
    looked up at call time (tests patch the module attributes or pass fakes). Never raises.

    Order: ps -> (lsof) -> first rusage sample of every tree pid -> telemetry, subagent logs of
    the sessions the menu will show, ioreg, ollama, script names (these fill the gap) -> sleep
    the rest of ``sample_s`` (never past the deadline) -> second sample of the ≤ RATE_PIDS_MAX
    busiest pids -> rates -> trees. Subagent logs are read only for the ≤ SCAN_SESSIONS_MAX
    active Claude sessions ranked first by ``rank_key`` (re-checked once the cpu rates are in);
    every other session's subagents come from proxy traffic alone.

    ``quiet=True`` (the menu) also looks up the cwd of live pi / Codex processes when no such
    session is listed, and returns the ones no listed session accounts for
    (``system["quiet_procs"]``, ``quiet_procs``) with their tree; their memory is counted in
    ``agents_footprint_mb``."""
    now = time.time() if now is None else now
    run = run or run_cmd
    rusage_fn = rusage_fn or rusage
    loadavg = loadavg or os.getloadavg
    dl = deadline or Deadline()
    if fetch is None:
        def fetch(url):
            return http_get(url, timeout=dl.timeout(0.5))
    home = Path(home).expanduser() if home else Path.home()
    errors: dict = {}

    def guard(name, fn, default):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — each source fails open on its own
            errors[name] = _err(e)
            return default

    agents = [dict(a) if isinstance(a, dict) else a for a in agents]
    table = guard("ps", lambda: ps_table(run, dl.timeout(1.0)), {})
    kids = children_map(table)
    sessions = guard("claude_sessions", lambda: claude_sessions(home, table), {})
    kind_pids = sorted((p for p, r in table.items() if r["name"] in ("pi", "codex")),
                       key=lambda p: -(table[p].get("start") or 0))[:LSOF_PIDS_MAX]
    need_lsof = set(kind_pids) if quiet or any(
        isinstance(a, dict) and a.get("kind") in ("pi", "codex") for a in agents) else set()
    cwds = guard("lsof", lambda: lsof_cwds(need_lsof, run, dl.timeout(0.5)), {}) if need_lsof else {}
    by_cwd = match_by_cwd([a if isinstance(a, dict) else {} for a in agents], table, cwds)

    # roots: agent index -> pid
    roots: dict = {}
    for i, a in enumerate(agents):
        if not isinstance(a, dict) or a.get("error"):
            continue
        sid = a.get("session_id")
        if a.get("kind") == "claude" and sid in sessions:
            roots[i] = sessions[sid]["pid"]
        elif isinstance(by_cwd.get(i), int):
            roots[i] = by_cwd[i]
    ol = {p["pid"]: p for p in table.values() if p["name"] == "ollama"}
    ol_roots = [p for p in ol.values() if p["ppid"] not in ol]   # the server, not its runners
    ol_pid = min(ol_roots, key=lambda p: p["pid"])["pid"] if ol_roots else None
    extra_roots = [p for p in (worker_pid, ol_pid) if isinstance(p, int)]
    quiet_list = quiet_procs([a for a in agents if isinstance(a, dict)], table, cwds,
                             roots.values()) if quiet and kind_pids else []
    quiet_roots = [q["pid"] for q in quiet_list]
    all_roots = set(roots.values()) | set(extra_roots) | set(quiet_roots)
    excl = self_exclusion(table, all_roots, self_pid, kids) if table else set()

    tree_pids: dict = {}
    for r in all_roots:
        if r in table:
            tree_pids[r] = [r] + [p for p in descendants(table, r, kids) if p not in excl]
    every = {p for ps in tree_pids.values() for p in ps}
    s1 = {p: _safe_rusage(rusage_fn, p) for p in every}
    t1 = _clock()

    # gap work
    path = Path(telemetry) if telemetry else pressure.default_telemetry_path()
    rows = guard("telemetry", lambda: list(pressure.tail_rows(path, now - TELEMETRY_WINDOW_S)), [])
    tel = guard("telemetry_split", lambda: telemetry_split(rows, now), {})
    traffic = guard("telemetry_split", lambda: _stats(rows, now), None)
    series = guard("series", lambda: _buckets(rows, now), {})   # 12 x 5-min, per session
    # subagents: traffic-only for every Claude session; logs for the ones the menu will show
    empty_subs = {"list": [], "more": 0, "count": 0, "flagged": 0}
    subs_by: dict = {}
    for i, a in enumerate(agents):
        if isinstance(a, dict) and not a.get("error") and a.get("kind") == "claude" \
                and isinstance(a.get("session_id"), str):
            t = tel.get(a["session_id"]) or {}
            subs_by[i] = guard("subagents", lambda: subagents(
                home, a["session_id"], now, t.get("subagents"), scan=False), empty_subs)

    def provisional(i, cpu_of):
        a = agents[i]
        sid = a["session_id"]
        st = sessions.get(sid, {}).get("status") if sid in sessions else None
        pid = roots.get(i)
        res = {"status": st, "subagents": subs_by.get(i),
               "telemetry": (tel.get(sid) or {}).get("main"),
               "tree": {"cpu_pct": cpu_of(pid) if pid is not None else 0}}
        return dict(a, res=res)

    scanned: set = set()

    def scan_shown(cpu_of):
        cands = [provisional(i, cpu_of) for i in subs_by]
        idx = list(subs_by)
        order = sorted(range(len(cands)), key=lambda k: rank_key(cands[k]))
        shown = [idx[k] for k in order if agent_active(cands[k])][:SCAN_SESSIONS_MAX]
        for i in shown:
            if i in scanned:
                continue
            scanned.add(i)
            sid = agents[i]["session_id"]
            t = tel.get(sid) or {}
            st = sessions[sid].get("status") if sid in sessions else None
            r = guard("subagents", lambda: subagents(home, sid, now, t.get("subagents"),
                                                     deadline=dl, status=st), None)
            if isinstance(r, dict):
                subs_by[i] = r
                if r.get("partial"):
                    errors["subagents"] = "partial: deadline"

    def ps_tree_cpu(pid):
        return round(sum(table[p]["cpu"] for p in tree_pids.get(pid, []) if p in table), 1)
    scan_shown(ps_tree_cpu)

    system: dict = {"gpu_scope": "system-wide"}
    system.update(guard("ioreg", lambda: parse_ioreg(
        run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"], timeout=dl.timeout(0.5))), {}))
    system["ollama"] = guard("ollama", lambda: parse_ollama(fetch(OLLAMA_URL), now), None)
    if system["ollama"]:                          # a model is loaded: who is using it?
        cl = guard("ollama_clients", lambda: ollama_clients(run, dl.timeout(0.5)), None)
        if cl is not None:
            system["ollama_clients"] = [dict(c, worker=c["pid"] == worker_pid) for c in cl]
    net = guard("netstat", lambda: parse_netstat(run(["netstat", "-ibn"], timeout=dl.timeout(0.5))),
                None)
    if net:
        system["net"] = net
    agent_pids = {p for r in [*roots.values(), *quiet_roots] for p in tree_pids.get(r, [])}
    labels = guard("ps_args", lambda: script_labels(
        table, sorted(agent_pids, key=lambda p: -table[p]["cpu"]), run, dl.timeout(0.5)),
        {}) if agent_pids else {}

    # second sample of the busiest pids (roots first), inside the shared deadline
    have = [p for p in every if s1.get(p)]
    rates: dict = {}
    if have and sample_s > 0 and dl.remaining() > DEADLINE_MIN_S:
        pri = sorted(have, key=lambda p: (p not in all_roots, -table[p]["cpu"],
                                          -table[p]["rss_kb"]))[:RATE_PIDS_MAX]
        wait = min(sample_s - (_clock() - t1), dl.remaining() - DEADLINE_MIN_S)
        if wait > 0:
            _sleep(wait)
        s2 = {p: _safe_rusage(rusage_fn, p) for p in pri}
        dt = _clock() - t1
        rates = rates_from_samples({p: s1[p] for p in pri}, s2, dt)
        system["rate_window_s"] = round(dt, 3)
        system["rate_pids"] = len(pri)

    def tree_of(pid):
        return tree_metrics(table, pid, lambda p: s1.get(p), kids, excl, rates, labels, now)

    def sampled_tree_cpu(pid):
        ps = tree_pids.get(pid, [])
        if not any(p in rates for p in ps):
            return ps_tree_cpu(pid)
        return round(sum(rates[p]["cpu_pct"] for p in ps if p in rates), 1)
    if rates:
        scan_shown(sampled_tree_cpu)                    # the menu ranks by the sampled cpu

    for i, a in enumerate(agents):
        if not isinstance(a, dict) or a.get("error"):
            continue
        res: dict = {}
        sid = a.get("session_id")
        if a.get("kind") == "claude" and sid in sessions:
            s = sessions[sid]
            res.update(status=s["status"], name=s["name"], pid_source=s["verified"])
            res["tree"] = guard("rusage", lambda: tree_of(s["pid"]), {})
        elif isinstance(by_cwd.get(i), int):
            res["pid_source"] = "lsof-cwd"
            res["tree"] = guard("rusage", lambda: tree_of(by_cwd[i]), {})
        elif isinstance(by_cwd.get(i), dict):
            res["unattributed"] = by_cwd[i]
        t = tel.get(sid) if isinstance(sid, str) else None
        if t:
            res["telemetry"] = t["main"]
        if i in subs_by:
            res["subagents"] = subs_by[i]
        if isinstance(sid, str) and series.get(sid):
            res["series"] = series[sid]
        if res:
            a["res"] = res

    la = guard("loadavg", loadavg, None)
    system["loadavg"] = [round(x, 2) for x in la] if la else None
    system["agents_rss_mb"] = round(sum(table[p]["rss_kb"] for p in agent_pids if p in table)
                                    / 1024, 1)
    fps = [s1[p]["footprint_mb"] for p in agent_pids if s1.get(p)
           and isinstance(s1[p].get("footprint_mb"), (int, float))]
    system["agents_footprint_mb"] = round(sum(fps), 1) if fps else None
    system["agents_cpu_pct"] = round(sum(rates[p]["cpu_pct"] for p in agent_pids if p in rates),
                                     1) if rates else None
    system["agents_procs"] = len(agent_pids)
    if quiet:
        system["quiet_procs"] = []
        for q in quiet_list:
            tr = guard("rusage", lambda: tree_of(q["pid"]), {})
            system["quiet_procs"].append(dict(q, uptime_s=tr.get("uptime_s"),
                                              footprint_mb=tr.get("footprint_mb"),
                                              rss_mb=tr.get("rss_mb"), procs=tr.get("procs")))
    system["excluded_self_pids"] = len(excl)
    if traffic:
        system["traffic_60m"] = traffic
    worker = {}
    if isinstance(worker_pid, int):
        worker["tree"] = guard("rusage", lambda: tree_of(worker_pid), {})
    if ol_pid is not None:
        worker["ollama_tree"] = guard("rusage", lambda: tree_of(ol_pid), {})
    worker["ollama_models"] = system["ollama"]
    if errors:
        system["errors"] = errors
    system["subagent_scans"] = len(scanned)
    return {"agents": agents, "system": system, "graph": build_graph(agents, now),
            "worker": worker}
