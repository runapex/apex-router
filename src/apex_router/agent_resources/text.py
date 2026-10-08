"""Formatting for the menu and the graph: durations, sizes, rates, token and context text,
lifecycle words. One implementation per helper (``fmt_age`` and ``fmt_mb`` are aliases)."""
from __future__ import annotations

import math
import re

from .telemetry import RATE_WINDOW_S, cache_share


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean_text(s) -> str:
    """One line of display text: whitespace runs (newline, tab, …) -> one space, every other C0/C1
    control character (ESC, DEL, 0x80-0x9f) removed, so a description cannot drive a terminal or
    break a menu line."""
    s = _CTRL_RE.sub(lambda m: " " if m.group().isspace() else "", str(s))
    return " ".join(s.split())


# ---- formatting helpers (shared with snapshot) -----------------------------------------------

def fmt_dur(s) -> str:
    """``42s`` / ``5m`` / ``3h`` / ``2d`` (whole units, rounded down); ``?`` when not a number."""
    if not isinstance(s, (int, float)) or not math.isfinite(s):
        return "?"
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


fmt_age = fmt_dur                  # the menu's name for the same thing


def fmt_mb(mb, missing: str = "?") -> str:
    """``412MB`` / ``1.2GB``; ``missing`` when not a number (the menu: ``?``)."""
    if not isinstance(mb, (int, float)) or not math.isfinite(mb):
        return missing
    return f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb:.0f}MB"


def fmt_mem(mb) -> str:
    """``fmt_mb`` for the graph and the detail page, where a missing size reads ``?MB``."""
    return fmt_mb(mb, "?MB")


def _k(n) -> str:
    if not isinstance(n, (int, float)):
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


def display_state(a: dict) -> str:
    """Claude's own busy/idle when known, else the log-mtime state."""
    res = a.get("res") if isinstance(a.get("res"), dict) else {}
    return str(res.get("status") or a.get("state") or "?")


def io_rate(tree: dict):
    r, w = tree.get("read_mbs"), tree.get("write_mbs")
    if not isinstance(r, (int, float)) and not isinstance(w, (int, float)):
        return None
    return (r or 0.0) + (w or 0.0)


def fmt_rate(mbs) -> str:
    if not isinstance(mbs, (int, float)):
        return "?MB/s"
    return f"{mbs:.0f}MB/s" if mbs >= 10 else f"{mbs:.1f}MB/s"


def metrics_text(tree: dict) -> str:
    """``412MB · 14% · 0.4MB/s`` (footprint · cpu now · disk read+write now) — empty when the
    tree is unknown; the io rate only when two samples were taken."""
    if not tree or not tree.get("alive"):
        return ""
    bits = [fmt_mem(tree.get("footprint_mb")), f"{tree.get('cpu_pct') or 0:g}%"]
    io = io_rate(tree)
    if io is not None:
        bits.append(fmt_rate(io))
    return " · ".join(bits)


def fmt_pct(x) -> str:
    """A 0..1 share as a whole percent; a nonzero share below 1% is ``<1%``, never ``0%``."""
    if not isinstance(x, (int, float)) or not math.isfinite(x):
        return "?"
    return "<1%" if 0 < x < 0.01 else f"{100 * x:.0f}%"


def err_text(st: dict) -> str:
    n, req = st.get("errors") or 0, st.get("requests") or 0
    return f"{n} err {fmt_pct(n / req) if req else '?'}" if n else ""


def _kc(n) -> str:
    """Compact token count for context sizes: ``283k``, ``1M``, ``1.2M``, ``950``."""
    if not isinstance(n, (int, float)) or not math.isfinite(n):
        return "?"
    if n >= 999_500:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    return f"{n / 1000:.0f}k" if n >= 1000 else str(int(n))


def fmt_bytes(n) -> str:
    """``950B`` / ``12K`` / ``1.2M`` / ``3.4G`` (binary units, short for a menu column)."""
    if not isinstance(n, (int, float)) or not math.isfinite(n) or n < 0:
        return "?"
    for unit, div in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if n >= div:
            v = n / div
            return f"{v:.1f}{unit}" if v < 10 else f"{v:.0f}{unit}"
    return f"{int(n)}B"


def net_text(st: dict | None) -> str:
    """``↑12M ↓0.9M`` — proxy wire bytes up / down (empty when none recorded: pre-v10 rows)."""
    st = st or {}
    up, down = st.get("net_up") or 0, st.get("net_down") or 0
    if not (up or down):
        return ""
    return f"↑{fmt_bytes(up)} ↓{fmt_bytes(down)}"


def fmt_rate_min(n5) -> str:
    """``r5 3.2/min`` from a 5-min request count."""
    if not isinstance(n5, (int, float)):
        return ""
    x = n5 / (RATE_WINDOW_S / 60)
    v = f"{x:.0f}" if x >= 10 else f"{x:.1f}".rstrip("0").rstrip(".")
    return f"r5 {v or '0'}/min"


def tokens_text(st: dict | None) -> str:
    """``in 12k · cached 3.7M · write 40k · out 54k`` (input parts only when non-zero)."""
    st = st or {}
    bits = []
    if st.get("tokens_in") or st.get("cache_read") or st.get("cache_write"):
        bits += [f"in {_k(st.get('tokens_in') or 0)}", f"cached {_k(st.get('cache_read') or 0)}"]
    if st.get("cache_write"):
        bits.append(f"write {_k(st['cache_write'])}")
    bits.append(f"out {_k(st.get('tokens_out', 0))}")
    return " · ".join(bits)


def ctx_text(st: dict | None) -> str:
    """``ctx 283k/1M 28%`` with a known window, ``ctx 136k`` without (never a guessed window);
    '' when no request carried a prompt."""
    st = st or {}
    n = st.get("ctx_tokens")
    if not isinstance(n, (int, float)):
        return ""
    win, pct = st.get("ctx_window"), st.get("ctx_pct")
    if isinstance(win, (int, float)) and win > 0:
        pct = pct if isinstance(pct, (int, float)) else round(100 * n / win)
        return f"ctx {_kc(n)}/{_kc(win)} {pct:g}%"
    return f"ctx {_kc(n)}" + (f" {pct:g}%" if isinstance(pct, (int, float)) else "")


def tel_text(st: dict | None) -> str:
    """``12 req · in 2 · cached 115k · out 130 · cache 99%`` (+ ``5 err 42%`` only with errors,
    + ``ctx 245k`` when a context size is known)."""
    st = st or {}
    bits = [f"{st.get('requests', 0)} req", tokens_text(st)]
    share = cache_share(st)
    if share is not None:
        bits.append(f"cache {fmt_pct(share)}")
    e = err_text(st)
    if e:
        bits.append(e)
    c = ctx_text(st)
    if c:
        bits.append(c)
    n = net_text(st)
    if n:
        bits.append(n)
    return " · ".join(bits)


def lifecycle_text(s: dict) -> str:
    """``run 14m · last 2s`` / ``run 12m · quiet 3m`` / ``run 40m · done 20m`` /
    ``run 9m · waiting? 12m`` (``mark_waiting``)."""
    bits = []
    if isinstance(s.get("run_s"), (int, float)):
        bits.append(f"run {fmt_dur(s['run_s'])}")
    age, state = s.get("age_s"), s.get("state")
    if isinstance(age, (int, float)):
        word = {"running": "last", "quiet": "quiet", "done": "done"}.get(state, "last")
        if "errors" in (s.get("flags") or []) and state != "running":
            word = "last"
        elif s.get("waiting"):
            word = "waiting?"
        bits.append(f"{word} {fmt_dur(age)}")
    return " · ".join(bits) if bits else "no log"
