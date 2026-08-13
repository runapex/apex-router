"""codeqa `doctor` — post-install validation of registered repos.

`repo_health()` returns one status dict per config in the repos dir — a pure, testable
function with no Ornith/network dependency. A repo is `ok` only if its config parses, its
root exists, AND its search globs actually match at least one code file (a repo with no
reachable code can't answer anything). A missing digest is a non-fatal warning (codeqa
degrades cleanly without one). `all_healthy()` folds the rows for `--check` gating.

Root paths are whatever the config says — user-defined, never assumed. `doctor` only reports
whether each declared root exists on THIS machine; it never rewrites them.
"""
from __future__ import annotations

import json
from pathlib import Path


def _count_code_files(root: Path, globs: list[str], exts: list[str], *, cap: int = 1) -> int:
    """Count files under `root` matching any search glob and (if given) any code ext.
    Stops at `cap` — callers only need 'is there at least one', so we don't walk a huge tree."""
    n = 0
    seen = set()
    for g in (globs or ["**"]):
        # Normalize to a FILE-matching recursive pattern. A bare "**" (or a trailing "/**")
        # matches only directories on Python <3.13, so always end in "/*" to catch files
        # across versions. "app/**" -> "app/**/*"; "**" -> "**/*"; an explicit "*.py" is kept.
        gg = g.rstrip("/")
        if gg.endswith("**"):
            pattern = f"{gg}/*"
        elif any(c in gg for c in "*?["):
            pattern = gg
        else:
            pattern = f"{gg}/**/*"
        try:
            for p in root.glob(pattern):
                if not p.is_file() or p in seen:
                    continue
                if exts and p.suffix not in exts:
                    continue
                seen.add(p)
                n += 1
                if n >= cap:
                    return n
        except (OSError, ValueError):
            continue
    return n


def repo_health(*, repos_dir: Path) -> list[dict]:
    """One health row per <name>.json in `repos_dir`, sorted by name."""
    rows: list[dict] = []
    for cfg_path in sorted(Path(repos_dir).glob("*.json")):
        row = {"name": cfg_path.stem, "config_ok": False, "root_exists": False,
               "digest_ok": False, "code_files": 0, "ok": False, "error": ""}
        try:
            d = json.loads(cfg_path.read_text())
            row["name"] = d.get("name", cfg_path.stem)
            row["config_ok"] = True
            root = Path(str(d.get("root", ""))).expanduser()
            row["root"] = str(root)
            row["root_exists"] = root.is_dir()
            dig = d.get("digest")
            row["digest_ok"] = bool(dig) and Path(str(dig)).expanduser().is_file()
            if row["root_exists"]:
                row["code_files"] = _count_code_files(
                    root, d.get("search_globs", ["**"]), d.get("code_exts", []))
            # Healthy = parses + root present + at least one reachable code file.
            row["ok"] = row["config_ok"] and row["root_exists"] and row["code_files"] > 0
        except Exception as e:  # noqa: BLE001
            row["error"] = str(e)
        rows.append(row)
    return rows


def all_healthy(rows: list[dict]) -> bool:
    """True iff every repo is `ok` (used by `--check` to gate an install/CI)."""
    return bool(rows) and all(r.get("ok") for r in rows)
