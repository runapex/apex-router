#!/usr/bin/env python3
"""Turn a `tmux capture-pane -e` text into one HTML page (for README screenshots).

Usage: python3 scripts/ansi_to_html.py <in.ansi> <out.html> [title]
Handles SGR: reset, bold, dim, reverse, 24-bit and 256 colours (fg and bg). Other escapes are dropped.
"""
from __future__ import annotations

import html
import re
import sys

FG, BG = "#c0caf5", "#1a1b26"          # Tokyo Night
SGR_RE = re.compile(r"\x1b\[([0-9;:]*)m")
OTHER_RE = re.compile(r"\x1b(\[[0-9;?]*[A-Za-z]|\][^\x07\x1b]*(\x07|\x1b\\)|.)")
BASIC = ["#15161e", "#f7768e", "#9ece6a", "#e0af68", "#7aa2f7", "#bb9af7", "#7dcfff", "#a9b1d6",
         "#414868", "#f7768e", "#9ece6a", "#e0af68", "#7aa2f7", "#bb9af7", "#7dcfff", "#c0caf5"]


def c256(n: int) -> str:
    if n < 16:
        return BASIC[n]
    if n < 232:
        n -= 16
        v = [0, 95, 135, 175, 215, 255]
        return f"#{v[n // 36]:02x}{v[n // 6 % 6]:02x}{v[n % 6]:02x}"
    g = 8 + (n - 232) * 10
    return f"#{g:02x}{g:02x}{g:02x}"


def convert(text: str) -> str:
    st = {"fg": None, "bg": None, "b": False, "rev": False}
    out = []

    def span(s: str) -> None:
        if not s:
            return
        fg, bg = st["fg"], st["bg"]
        if st["rev"]:
            fg, bg = bg or BG, fg or FG
        css = []
        if fg:
            css.append(f"color:{fg}")
        if bg:
            css.append(f"background:{bg}")
        if st["b"]:
            css.append("font-weight:700")
        t = html.escape(s)
        out.append(f'<span style="{";".join(css)}">{t}</span>' if css else t)

    pos = 0
    for m in SGR_RE.finditer(text):
        span(OTHER_RE.sub("", text[pos:m.start()]))
        pos = m.end()
        codes = [int(x) if x.isdigit() else 0 for x in re.split("[;:]", m.group(1) or "0")]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                st.update(fg=None, bg=None, b=False, rev=False)
            elif c == 1:
                st["b"] = True
            elif c == 22:
                st["b"] = False
            elif c == 7:
                st["rev"] = True
            elif c == 27:
                st["rev"] = False
            elif c in (38, 48) and i + 1 < len(codes):
                key = "fg" if c == 38 else "bg"
                if codes[i + 1] == 2 and i + 4 < len(codes):
                    st[key] = "#%02x%02x%02x" % tuple(codes[i + 2:i + 5])
                    i += 4
                elif codes[i + 1] == 5 and i + 2 < len(codes):
                    st[key] = c256(codes[i + 2])
                    i += 2
            elif c == 39:
                st["fg"] = None
            elif c == 49:
                st["bg"] = None
            elif 30 <= c <= 37 or 90 <= c <= 97:
                st["fg"] = BASIC[(c - 30) if c < 90 else (c - 82)]
            elif 40 <= c <= 47 or 100 <= c <= 107:
                st["bg"] = BASIC[(c - 40) if c < 100 else (c - 92)]
            i += 1
    span(OTHER_RE.sub("", text[pos:]))
    return "".join(out)


def page(text: str, title: str = "") -> str:
    lines = text.rstrip("\n").split("\n")
    body = "\n".join(convert(ln) for ln in lines)
    bar = (f'<div class="bar"><i></i><i></i><i></i><span>{html.escape(title)}</span></div>' if title else "")
    return f"""<!doctype html><meta charset="utf-8"><style>
body{{margin:0;background:#0d0e14;padding:24px;display:inline-block}}
.win{{background:{BG};border-radius:10px;box-shadow:0 8px 30px #0008;overflow:hidden;display:inline-block}}
.bar{{background:#16161e;padding:8px 12px;font:12px -apple-system,sans-serif;color:#565f89}}
.bar i{{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:6px;background:#414868}}
.bar span{{margin-left:10px}}
pre{{margin:0;padding:12px 16px;color:{FG};font:13px/1.35 Menlo,"SF Mono",monospace;white-space:pre}}
</style><div class="win">{bar}<pre>{body}</pre></div>"""


if __name__ == "__main__":
    src = open(sys.argv[1], encoding="utf-8", errors="replace").read()
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        f.write(page(src, sys.argv[3] if len(sys.argv) > 3 else ""))
