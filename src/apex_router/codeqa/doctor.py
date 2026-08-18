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
import shutil
import subprocess
from pathlib import Path


def _count_code_files(root: Path, globs: list[str], exts: list[str], excludes=None,
                      *, cap: int = 1) -> int:
    """Count code files codeqa would ACTUALLY reach, using the SAME mechanism as the real
    retriever: ripgrep with the config's --glob patterns, run with cwd=root (rg anchors
    relative globs to the CWD), then the extension filter applied in Python — a true
    path ∧ extension AND. This keeps `doctor` honest: a repo passes here iff retrieval can
    find files at ask-time (rg's .gitignore/glob semantics differ from pathlib's, so we must
    not reimplement the globbing). Falls back to a pathlib walk only if ripgrep is absent.

    Stops at `cap` — callers only need 'is there at least one reachable file'."""
    rg = shutil.which("rg")
    exts_t = tuple(exts or ())
    if rg:
        cmd = [rg, "--files", "--no-messages"]
        for g in (globs or ["**"]):
            cmd += ["--glob", g]
        for g in (excludes or []):
            cmd += ["--glob", f"!{g}"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(root),
                                  timeout=30)
        except (OSError, subprocess.SubprocessError):
            return _count_code_files_pathlib(root, globs, exts_t, cap=cap)
        n = 0
        for line in proc.stdout.splitlines():
            if exts_t and not line.endswith(exts_t):
                continue
            n += 1
            if n >= cap:
                return n
        return n
    return _count_code_files_pathlib(root, globs, exts_t, cap=cap)


def _count_code_files_pathlib(root: Path, globs, exts, *, cap: int = 1) -> int:
    """Fallback when ripgrep isn't installed. Best-effort; rg is the source of truth."""
    n = 0
    seen = set()
    for g in (globs or ["**"]):
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
                # Match the retriever's extension test: full-path str.endswith(exts), NOT
                # Path.suffix — so code_exts like ["py"] (no dot) or multi-part suffixes agree
                # with ask-time behavior (Codex xval #6).
                if exts and not str(p).endswith(tuple(exts)):
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
            # `name` and `root` are REQUIRED and must be non-empty strings — RepoConfig.load
            # indexes d["name"]/d["root"] directly (KeyError otherwise) and rejects a missing
            # root. A missing/empty/non-string root must NOT silently become "" -> cwd -> a
            # false OK (Codex xval #3/#8).
            name = d.get("name")
            if not isinstance(name, str) or not name.strip():
                row["error"] = f"config missing/invalid 'name' (got {name!r})"
                rows.append(row); continue
            row["name"] = name
            root_raw = d.get("root")
            if not isinstance(root_raw, str) or not root_raw.strip():
                row["config_ok"] = True
                row["error"] = f"config missing/invalid 'root' (got {root_raw!r})"
                rows.append(row); continue
            row["config_ok"] = True
            root = Path(root_raw).expanduser()
            row["root"] = str(root)
            row["root_exists"] = root.is_dir()
            dig = d.get("digest")
            row["digest_ok"] = bool(dig) and isinstance(dig, str) and Path(dig).expanduser().is_file()
            if row["root_exists"]:
                row["code_files"] = _count_code_files(
                    root, d.get("search_globs") or ["**"],
                    d.get("code_exts") or [], d.get("exclude_globs") or [])
            # Contract check: the config must also survive the PRODUCTION loader, so doctor can't OK
            # a config that every runtime command rejects (validator-matches-production; e.g. an
            # invalid max_tokens is rejected by RepoConfig.load but is invisible to the parsing above).
            if row["root_exists"]:
                from .retriever import REPOS_DIR as _RD, RepoConfig as _RC
                if Path(_RD).resolve() == Path(repos_dir).resolve():
                    try:
                        _RC.load(name)
                    except Exception as le:  # noqa: BLE001
                        row["config_ok"] = False
                        row["error"] = f"RepoConfig.load rejected: {le}"
                        rows.append(row); continue
            # Healthy = parses + required keys + root present + at least one reachable code file.
            row["ok"] = row["config_ok"] and row["root_exists"] and row["code_files"] > 0
        except Exception as e:  # noqa: BLE001
            row["error"] = str(e)
        rows.append(row)
    return rows


def all_healthy(rows: list[dict]) -> bool:
    """True iff every repo is `ok` (used by `--check` to gate an install/CI)."""
    return bool(rows) and all(r.get("ok") for r in rows)
