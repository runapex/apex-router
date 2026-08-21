#!/usr/bin/env python3
"""Project-memory compaction — hierarchical, measure-first, advisory.

A Claude Code project-memory dir (`.../<project>/memory/`) accumulates many small
files plus a `MEMORY.md` index that is re-read into every session's prefix. As the
index grows it becomes a per-session cache-read cost. This tool compacts the index
by clustering files, tiering them (frontmatter type + freshness), and rolling cold
files up into a single archived line per cluster.

Advisory by default: it MEASURES and PROPOSES. `--apply` is the only mutating path
(moves cold files to `archive/<cluster>/`, rewrites the index) — human-run and
git-guarded, so every apply is reversible. No LLM in the hot path: tiering is
deterministic from frontmatter that is already present.

Provider-neutral: nothing about any specific repo is hardcoded — everything is
derived from the memory dir passed in.

Run:
    python3 scripts/memory_compact.py --dir ~/.claude/projects/<slug>/memory
    python3 scripts/memory_compact.py --dir <memory> --json
    python3 scripts/memory_compact.py --dir <memory> --apply      # mutates (git-guarded)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

INDEX_NAME = "MEMORY.md"
# type -> keep-hot? feedback/project are durable working context; reference is the
# compactible bulk (facts the retriever/code can re-surface). Unknown -> hot (safe).
HOT_TYPES = {"feedback", "project", "user"}
COMPACTIBLE_TYPES = {"reference"}

def cluster_of(stem: str, *, multitoken: tuple[str, ...] = ()) -> str:
    """Cluster a memory file by the leading token of its name (before the first
    underscore). `multitoken` optionally names two-token prefixes that should read
    as one group (e.g. "foo_bar" so foo_bar_* clusters as "foo_bar", not "foo") —
    supplied by the caller, never hardcoded, so the engine stays project-agnostic."""
    for m in multitoken:
        if stem == m or stem.startswith(m + "_"):
            return m
    head = stem.split("_", 1)[0]
    return head or stem


def parse_frontmatter(text: str) -> dict:
    """Extract name/description/type/modified from a memory file's frontmatter.
    No YAML dependency. Hardening (Codex xval): the opening fence must be a line
    that is EXACTLY '---' (not just a '---' prefix), and `type` is read ONLY from
    a nested `metadata:` block at exactly 2-space indent — a top-level or
    deeper-nested `type:` is ignored, so a body/block-scalar line can't flip the
    tier by shadowing the real value (xval #7)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    # find the closing fence line (exactly '---')
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}

    out: dict = {}
    in_metadata = False
    for line in lines[1:end]:
        # top-level keys we allow (name/description live at column 0)
        m_top = re.match(r"(name|description)\s*:\s*(.*)$", line)
        if m_top:
            out[m_top.group(1)] = m_top.group(2).strip().strip('"').strip("'")
            in_metadata = False
            continue
        if re.match(r"metadata\s*:\s*$", line):
            in_metadata = True
            continue
        # only accept type/modified when nested exactly one level under metadata:
        m_meta = re.match(r"  (type|modified)\s*:\s*(.*)$", line)
        if m_meta and in_metadata:
            out[m_meta.group(1)] = m_meta.group(2).strip().strip('"').strip("'")
            continue
        # a non-indented, non-matching line ends the metadata block
        if line and not line.startswith(" "):
            in_metadata = False
    return out


def tier_of(meta: dict) -> str:
    """`hot` (keep in the live index) or `cold` (roll up / archive)."""
    t = (meta.get("type") or "").lower()
    if t in COMPACTIBLE_TYPES:
        return "cold"
    if t in HOT_TYPES or t == "":
        return "hot"
    return "hot"  # unknown type → keep hot (conservative)


def scan_memory(dir_path: Path, *, multitoken: tuple[str, ...] = ()) -> list[dict]:
    """One row per memory file (excluding the index itself)."""
    rows = []
    for p in sorted(dir_path.glob("*.md")):
        if p.name == INDEX_NAME:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        rows.append({
            "file": p.name,
            "stem": p.stem,
            "cluster": cluster_of(p.stem, multitoken=multitoken),
            "type": meta.get("type") or "",
            "modified": meta.get("modified") or "",
            "description": meta.get("description") or "",
            "tier": tier_of(meta),
            "bytes": len(text.encode("utf-8")),
        })
    return rows


def _is_cold(row: dict, *, min_age_days: int, now_date: str | None) -> bool:
    """A row is archivable only if it's cold-tier AND (no age guard, or old enough).
    `now_date`/`min_age_days` let a caller protect recently-touched files; when
    now_date is None the age guard is skipped (pure tier decision)."""
    if row["tier"] != "cold":
        return False
    if min_age_days <= 0 or not now_date or not row["modified"]:
        return True
    # dates are ISO 'YYYY-MM-DD...'; compare on the date prefix.
    mod = row["modified"][:10]
    # FAIL CLOSED (Codex xval #8): a malformed date/now_date must NOT archive the
    # file — an unparseable age can't prove the file is old enough, so keep it hot.
    try:
        from datetime import date
        y1, m1, d1 = (int(x) for x in now_date[:10].split("-"))
        y2, m2, d2 = (int(x) for x in mod.split("-"))
        return (date(y1, m1, d1) - date(y2, m2, d2)).days >= min_age_days
    except Exception:
        return False


def plan_compaction(rows: list[dict], *, min_age_days: int = 0,
                    now_date: str | None = None) -> dict:
    """Group rows into clusters; split each into hot (listed) vs archived (rolled
    up). Pure — no I/O — so it's unit-testable."""
    clusters: dict[str, dict] = {}
    for r in rows:
        c = clusters.setdefault(r["cluster"], {"hot": [], "archived": []})
        if _is_cold(r, min_age_days=min_age_days, now_date=now_date):
            c["archived"].append(r)
        else:
            c["hot"].append(r)
    return clusters


def render_index(clusters: dict, *, title: str = "Memory index") -> str:
    """Render the compacted MEMORY.md: one section per cluster, hot files listed,
    cold files collapsed to a single archived-count line."""
    lines = [f"# {title}", ""]
    for name in sorted(clusters):
        c = clusters[name]
        hot, archived = c["hot"], c["archived"]
        if not hot and not archived:
            continue
        lines.append(f"## {name}")
        for r in sorted(hot, key=lambda x: x["file"]):
            desc = r["description"] or "(no description)"
            lines.append(f"- [{r['file']}]({r['file']}) — {desc}")
        if archived:
            lines.append(f"- _(+{len(archived)} archived — see `archive/{name}/`)_")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_report(dir_path: Path, *, min_age_days: int = 0,
                 now_date: str | None = None, multitoken: tuple[str, ...] = ()) -> dict:
    rows = scan_memory(dir_path, multitoken=multitoken)
    clusters = plan_compaction(rows, min_age_days=min_age_days, now_date=now_date)
    proposed = render_index(clusters)
    index_path = dir_path / INDEX_NAME
    current_bytes = len(index_path.read_bytes()) if index_path.is_file() else 0
    proposed_bytes = len(proposed.encode("utf-8"))
    n_hot = sum(len(c["hot"]) for c in clusters.values())
    n_arch = sum(len(c["archived"]) for c in clusters.values())
    return {
        "schema": "memory-compact/1",
        "dir": str(dir_path),
        "files": len(rows),
        "clusters": len(clusters),
        "hot_files": n_hot,
        "archived_files": n_arch,
        "current_index_bytes": current_bytes,
        "proposed_index_bytes": proposed_bytes,
        "index_bytes_saved": current_bytes - proposed_bytes,
        "proposed_index": proposed,
        "manifest": [
            {k: r[k] for k in ("file", "cluster", "type", "modified", "tier", "bytes")}
            for r in rows
        ],
    }


def _git_clean(dir_path: Path) -> tuple[bool, str]:
    """(ok, reason). Apply is allowed only inside a clean, tracked git tree so every
    change is reversible. FAIL CLOSED on any uncertainty (Codex xval #4/#5/#6):
    - run status from the repo ROOT and match the dir's repo-relative prefix, so a
      relative --dir can't produce a bogus `memory/memory` pathspec that reports clean;
    - a nonzero git return code is treated as NOT clean, never as safe;
    - `--ignored` is included so gitignored memory files (no git recovery) also block."""
    d = dir_path.resolve()
    try:
        top = subprocess.run(["git", "-C", str(d), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
        if top.returncode != 0 or not top.stdout.strip():
            return False, "memory dir is not inside a git repo (apply needs git for reversibility)"
        root = Path(top.stdout.strip()).resolve()
        try:
            rel = d.relative_to(root)
        except ValueError:
            return False, "could not locate the memory dir within its git repo"
        st = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--ignored"],
                            capture_output=True, text=True)
        if st.returncode != 0:
            return False, f"git status failed (rc={st.returncode}) — refusing to apply"
        prefix = "" if str(rel) == "." else str(rel) + "/"
        for line in st.stdout.splitlines():
            # porcelain: 'XY <path>' (or '!! <path>' for ignored). Path starts at col 3.
            path = line[3:].split(" -> ")[-1].strip().strip('"')
            if prefix == "" or path.startswith(prefix):
                return False, ("git tree has uncommitted or ignored changes under the memory "
                               "dir — commit/stash and un-ignore first")
        return True, ""
    except FileNotFoundError:
        return False, "git not found"


def compact_existing_index(index_text: str, archived_files: set[str]) -> str:
    """Remove ONLY the lines that link to a now-archived file; preserve every other
    line — headings, prose, ordering, hot-file links — byte-for-byte (Codex xval #3).
    A line is dropped iff it markdown-links to `<file>` for a file in archived_files."""
    kept = []
    for line in index_text.splitlines():
        m = re.search(r"\]\(([^)]+\.md)\)", line)
        linked = m.group(1).rsplit("/", 1)[-1] if m else None
        if linked and linked in archived_files:
            continue  # drop only this cold-file entry
        kept.append(line)
    return "\n".join(kept).rstrip() + "\n"


def apply_compaction(dir_path: Path, *, min_age_days: int = 0,
                     now_date: str | None = None, multitoken: tuple[str, ...] = ()) -> dict:
    """MUTATING. Move archived files to archive/<cluster>/ and compact the index
    IN PLACE (drop cold-file lines, keep curated prose). Git-guarded + idempotent.

    Hardening (Codex xval): refuses to follow a symlinked index/archive/target (#2);
    never overwrites an existing archived file — a same-name re-archive is skipped
    with a warning rather than clobbering (#1); the index is edited, not regenerated
    from scratch, so hand-curated content survives (#3)."""
    ok, reason = _git_clean(dir_path)
    if not ok:
        raise RuntimeError(reason)

    index_path = dir_path / INDEX_NAME
    if index_path.is_symlink():
        raise RuntimeError(f"{INDEX_NAME} is a symlink — refusing to write through it")
    archive_root = dir_path / "archive"
    if archive_root.exists() and archive_root.is_symlink():
        raise RuntimeError("archive/ is a symlink — refusing to move files through it")

    rows = scan_memory(dir_path, multitoken=multitoken)
    clusters = plan_compaction(rows, min_age_days=min_age_days, now_date=now_date)

    moved, skipped = [], []
    for name, c in clusters.items():
        for r in c["archived"]:
            src = dir_path / r["file"]
            if not src.exists():
                continue
            if src.is_symlink():
                skipped.append(f"{r['file']} (symlink)")
                continue
            dst_dir = archive_root / name
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / r["file"]
            if dst.exists():
                # NEVER overwrite an existing archived version (data-loss guard #1)
                skipped.append(f"{r['file']} (already archived — not overwritten)")
                continue
            src.replace(dst)
            moved.append(str(dst.relative_to(dir_path)))

    # Compact the EXISTING index in place — preserve everything that isn't a
    # link to a file we just archived.
    archived_names = {Path(m).name for m in moved}
    if index_path.is_file():
        new_index = compact_existing_index(index_path.read_text(encoding="utf-8"), archived_names)
        index_path.write_text(new_index, encoding="utf-8")
    return {"moved": moved, "moved_count": len(moved), "skipped": skipped}


def _fmt_text(rep: dict) -> str:
    lines = [f"=== MEMORY COMPACTION — {rep['dir']} ==="]
    lines.append(f"  files={rep['files']}  clusters={rep['clusters']}  "
                 f"hot={rep['hot_files']}  archivable={rep['archived_files']}")
    lines.append(f"  index: {rep['current_index_bytes']:,} B → {rep['proposed_index_bytes']:,} B "
                 f"(saves {rep['index_bytes_saved']:,} B re-read every session)")
    if rep["archived_files"]:
        lines.append("  archivable by cluster:")
        from collections import Counter
        c = Counter(m["cluster"] for m in rep["manifest"] if m["tier"] == "cold")
        for name, n in sorted(c.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {name:16} {n} file(s)")
    lines.append("  (advisory — run with --apply to move cold files + rewrite the index)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="hierarchical project-memory compaction (advisory)")
    ap.add_argument("--dir", type=Path, required=True, help="the memory dir (contains MEMORY.md)")
    ap.add_argument("--multitoken", default="",
                    help="comma-separated two-token cluster prefixes (e.g. 'foo_bar,baz_qux') "
                         "so foo_bar_* groups as 'foo_bar' not 'foo'. Optional; none by default.")
    ap.add_argument("--min-age-days", type=int, default=0,
                    help="don't archive files modified within N days (needs --now-date)")
    ap.add_argument("--now-date", default=None, help="YYYY-MM-DD anchor for --min-age-days")
    ap.add_argument("--budget", type=int, default=None,
                    help="index byte budget; --check exits 2 if the CURRENT index exceeds it")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--apply", action="store_true", help="MUTATE: archive cold files + rewrite index")
    ap.add_argument("--check", action="store_true", help="exit 2 if current index over --budget")
    ap.add_argument("--write-proposed", type=Path, default=None,
                    help="write the proposed index to this path (does not touch the live index)")
    args = ap.parse_args(argv)

    if not args.dir.is_dir():
        print(f"memory_compact: no such dir: {args.dir}", file=sys.stderr)
        return 1

    multitoken = tuple(t.strip() for t in args.multitoken.split(",") if t.strip())

    if args.apply:
        try:
            res = apply_compaction(args.dir, min_age_days=args.min_age_days,
                                   now_date=args.now_date, multitoken=multitoken)
        except RuntimeError as e:
            print(f"memory_compact: refusing to apply — {e}", file=sys.stderr)
            return 3
        print(f"applied: moved {res['moved_count']} file(s) to archive/, rewrote {INDEX_NAME}")
        return 0

    rep = build_report(args.dir, min_age_days=args.min_age_days,
                       now_date=args.now_date, multitoken=multitoken)
    if args.write_proposed:
        args.write_proposed.write_text(rep["proposed_index"], encoding="utf-8")
    print(json.dumps({k: v for k, v in rep.items() if k != "proposed_index"}, indent=2)
          if args.json else _fmt_text(rep))

    if args.check and args.budget is not None and rep["current_index_bytes"] > args.budget:
        print(f"\nmemory_compact: index {rep['current_index_bytes']:,} B over budget {args.budget:,} B",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
