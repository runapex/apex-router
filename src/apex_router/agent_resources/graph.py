"""The call graph (session -> subagents / processes / models) and its indented text form."""
from __future__ import annotations

import math
import time

from .base import GRAPH_MODELS_MAX, GRAPH_PROCS_MAX, GRAPH_SESSIONS_MAX, _num
from .subagents import SUBAGENTS_MAX, agent_flagged, session_totals
from .telemetry import cache_share, merge_stats
from .text import (
    _k, clean_text, ctx_text, display_state, fmt_dur, fmt_mem, fmt_pct, fmt_rate_min,
    lifecycle_text, metrics_text, tel_text,
)


# ---- graph ----------------------------------------------------------------------------------

GRAPH_REPO_MAX = 32
GRAPH_WIDTH = 120


def _session_label(a: dict) -> str:
    bits = [str(a.get("kind", "?"))]
    if a.get("repo"):
        repo = clean_text(a["repo"])
        bits.append(repo if len(repo) <= GRAPH_REPO_MAX else repo[:GRAPH_REPO_MAX - 1] + "…")
    bits.append(str(a.get("session", "")))             # the 8-char id is never cut
    return " · ".join(bits)


def _age_since(ts, now):
    t = _num(ts)
    if t is None or not math.isfinite(t):
        return None
    return round(max(0.0, (time.time() if now is None else now) - t), 1)


def build_graph(agents: list, now: float | None = None) -> dict:
    """{nodes, edges} from enriched agents (each may carry ``res``). Bounded by the caps; the
    subagents past the cap are one ``more`` node carrying their summed traffic and model calls."""
    nodes, edges, models = [], [], {}

    def model_edge(src, counts):
        for name, n in list((counts or {}).items())[:GRAPH_MODELS_MAX]:
            mid = f"m:{name}"
            if mid not in models:
                models[mid] = {"id": mid, "kind": "model", "label": name, "requests": 0}
            models[mid]["requests"] += n
            edges.append({"from": src, "to": mid, "kind": "calls", "requests": n})

    for a in [a for a in agents if isinstance(a, dict) and not a.get("error")][:GRAPH_SESSIONS_MAX]:
        res = a.get("res") or {}
        sid = f"s:{a.get('kind')}:{a.get('session_id') or a.get('session')}"
        tree = res.get("tree") or {}
        main = res.get("telemetry") or {}
        tot = session_totals(res)
        nodes.append({"id": sid, "kind": "session", "label": _session_label(a),
                      "state": display_state(a), "status": res.get("status"),
                      "pid": tree.get("pid"), "rss_mb": tree.get("rss_mb"),
                      "footprint_mb": tree.get("footprint_mb"), "cpu_pct": tree.get("cpu_pct"),
                      "read_mbs": tree.get("read_mbs"), "write_mbs": tree.get("write_mbs"),
                      "read_mb": tree.get("read_mb"), "write_mb": tree.get("write_mb"),
                      "requests": main.get("requests"), "requests_total": tot["requests"],
                      "tokens_out_total": tot["tokens_out"], "cache_share": cache_share(tot),
                      "ctx_tokens": main.get("ctx_tokens"), "ctx_pct": main.get("ctx_pct"),
                      "ctx_window": main.get("ctx_window"),
                      "req_5m": tot.get("req_5m") if main or tot["requests"] else None,
                      "last_s": _age_since(tot.get("last_ts"), now),
                      "flagged": agent_flagged(a)})
        subs = res.get("subagents") or {}
        lst = [s for s in subs.get("list") or [] if isinstance(s, dict)]
        cut = lst[SUBAGENTS_MAX:]                      # an over-long list: sum, never drop
        for s in lst[:SUBAGENTS_MAX]:
            aid = f"a:{s['id']}"
            st = s.get("telemetry") or {}
            nodes.append({"id": aid, "kind": "subagent", "label": f"{s['type']} · {s['description']}",
                          "state": s.get("state"), "depth": s.get("depth"),
                          "run_s": s.get("run_s"), "age_s": s.get("age_s"),
                          "flags": s.get("flags") or [],
                          "requests": st.get("requests", 0), "tokens_out": st.get("tokens_out", 0),
                          "errors": st.get("errors", 0), "tokens_in": st.get("tokens_in", 0),
                          "cache_read": st.get("cache_read", 0),
                          "cache_write": st.get("cache_write", 0),
                          "ctx_tokens": st.get("ctx_tokens"), "ctx_pct": st.get("ctx_pct"),
                          "ctx_window": st.get("ctx_window"), "errors_5m": st.get("errors_5m", 0)})
            if s.get("waiting"):
                nodes[-1]["waiting"] = True
            edges.append({"from": sid, "to": aid, "kind": "spawned"})
            model_edge(aid, st.get("models"))
        more = (subs.get("more") or 0) + len(cut)
        if more:
            hid = merge_stats(subs.get("hidden"), *[c.get("telemetry") for c in cut])
            mid = f"{sid}:more"
            nodes.append({"id": mid, "kind": "more", "label": f"{more} more subagents",
                          "count": more, "requests": hid.get("requests", 0),
                          "tokens_out": hid.get("tokens_out", 0), "errors": hid.get("errors", 0)})
            edges.append({"from": sid, "to": mid, "kind": "spawned"})
            model_edge(mid, hid.get("models"))
        for p in tree.get("top", [])[:GRAPH_PROCS_MAX]:
            pid = f"p:{p['pid']}"
            nodes.append({"id": pid, "kind": "process", "label": p["name"], "pid": p["pid"],
                          "footprint_mb": p.get("footprint_mb"), "rss_mb": p.get("rss_mb"),
                          "cpu_pct": p.get("cpu_pct")})
            edges.append({"from": sid, "to": pid, "kind": "runs"})
        model_edge(sid, main.get("models"))
    return {"nodes": nodes + list(models.values()), "edges": edges}


def graph_text(graph: dict) -> str:
    """Indented text tree: session -> spawned/runs/calls, subagent -> calls."""
    by_id = {n["id"]: n for n in graph.get("nodes", [])}
    out_edges: dict = {}
    for e in graph.get("edges", []):
        out_edges.setdefault(e["from"], []).append(e)
    lines = []

    def node_bits(n, e=None) -> list:
        """The node's text as ' · '-joined parts (wrapped at GRAPH_WIDTH by emit)."""
        k = n["kind"]
        if k == "session":
            m = metrics_text({"alive": n.get("pid") is not None, **n})
            bits = [("⚠ " if n.get("flagged") else "") + clean_text(n["label"]),
                    clean_text(n.get("state") or "?")]
            if n.get("pid"):
                bits.append(f"pid {n['pid']}")
            if m:
                bits.append(m)
            if n.get("requests_total"):
                bits += [f"{n['requests_total']} req/h", f"out {_k(n.get('tokens_out_total'))}/h"]
                if n.get("cache_share") is not None:
                    bits.append(f"cache {fmt_pct(n['cache_share'])}")
            c = ctx_text(n)
            if c:
                bits.append(c)
            if n.get("req_5m"):
                bits.append(fmt_rate_min(n["req_5m"]))
            if isinstance(n.get("last_s"), (int, float)) and n.get("requests_total"):
                bits.append(f"last {fmt_dur(n['last_s'])}")
            return bits
        if k == "subagent":
            flag = "⚠ " if n.get("flags") else ""
            life = lifecycle_text(n)
            st = n.get("state")                        # quiet/done already name themselves
            label = clean_text(n["label"])[:60]
            bits = [f"spawned {flag}{label}", tel_text(n)]
            if f"{st} " not in life and not n.get("waiting"):
                bits.append(str(st))
            bits.append(life)
            if n.get("depth"):
                bits.append(f"depth {n['depth']}")
            return bits
        if k == "more":
            return [f"… {n.get('count', 0)} more subagents ({n.get('requests', 0)} req, "
                    f"out {_k(n.get('tokens_out', 0))})"]
        if k == "process":
            return [f"runs {clean_text(n['label'])} (pid {n['pid']})",
                    fmt_mem(n.get("footprint_mb")), f"{n.get('cpu_pct') or 0:g}%"]
        return [f"calls {clean_text(n['label'])} "
                f"×{e.get('requests', 0) if e else n.get('requests', 0)}"]

    def emit(depth, bits):
        """Pack the parts into lines of at most GRAPH_WIDTH; continuation lines indent 4 more.
        A single part longer than a line is cut with '…'."""
        indent, cont = "  " * depth, "  " * depth + "    "
        cur = indent
        for b in (x for x in " · ".join(bits).split(" · ") if x != ""):
            sep = "" if cur in (indent, cont) else " · "
            if len(cur) + len(sep) + len(b) <= GRAPH_WIDTH:
                cur += sep + b
                continue
            if cur not in (indent, cont):
                lines.append(cur)
            cur = cont
            room = GRAPH_WIDTH - len(cur)
            cur += b if len(b) <= room else b[:room - 1] + "…"
        lines.append(cur)

    def walk(nid, depth):
        for e in out_edges.get(nid, []):
            child = by_id.get(e["to"])
            if child is None:
                continue
            emit(depth, node_bits(child, e))
            if child["kind"] in ("subagent", "more"):
                walk(child["id"], depth + 1)

    for n in graph.get("nodes", []):
        if n["kind"] == "session":
            emit(0, node_bits(n))
            walk(n["id"], 1)
    return "\n".join(lines) if lines else "no agents in the last hour"
