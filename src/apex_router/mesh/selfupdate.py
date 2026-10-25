"""Mesh self-update: keep this node's apex-router checkout current (docs/DESIGN-lan-mesh.md).

A peer never sends code. A peer card only shows the build commit it runs. When a peer runs a
commit this node does not know (or every ``CHECK_EVERY_S``), this node fetches ITS OWN ``origin``
and fast-forwards a clean ``main`` to ``origin/main``. Every other state is a status, never a
change: a dirty tree, another branch, diverged history, an origin that is not an allowed upstream.

After a fast-forward: reinstall only if ``pyproject.toml`` or ``uv.lock`` changed (the same uv
commands as ``install.sh``), then a smoke import in a new process. A failed reinstall or smoke
resets the checkout to the old commit. The long-running ``mesh up`` and ``mesh worker`` processes
compare their start commit with the checkout HEAD at idle points and re-exec themselves.

Opt out: ``{"auto_update": false}`` in ``<mesh home>/update.json``, or ``APEX_MESH_NO_UPDATE=1``.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

STATUSES = ("ok", "updated", "off", "no_checkout", "dirty", "not_main", "diverged",
            "bad_origin", "fetch_failed", "busy", "error")
CAPS = ("bundle", "update")                 # what this build can do; peers read it from the card
DEFAULT_UPSTREAMS = ("github.com/runapex/apex-router",)
CONFIG_FILE = "update.json"                 # in the mesh home: {"auto_update": bool, "upstreams": [..]}
STATUS_FILE = "update-status.json"
LOCK_FILE = "update.lock"
CHECK_EVERY_S = 6 * 3600
MIN_GAP_S = 600                             # at most one fetch per 10 min, whatever peers say
GIT_TIMEOUT_S = 120
INSTALL_TIMEOUT_S = 900
SMOKE_TIMEOUT_S = 120
DEP_FILES = ("pyproject.toml", "uv.lock")
SMOKE_CODE = "import apex_router.mesh.cli, apex_router.mesh.worker, apex_router.mesh.selfupdate"

_GIT = ("git", "-c", "core.hooksPath=/dev/null")
_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_EXTRA_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")

Runner = Callable[..., "subprocess.CompletedProcess | None"]


def _run(argv: list[str], *, cwd: str | Path | None = None, timeout: float = GIT_TIMEOUT_S
         ) -> subprocess.CompletedProcess | None:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _git(repo: Path, *args: str, run: Runner = _run, timeout: float = GIT_TIMEOUT_S
         ) -> subprocess.CompletedProcess | None:
    return run([*_GIT, "-C", str(repo), *args], timeout=timeout)


def _out(r: subprocess.CompletedProcess | None) -> str | None:
    if r is None or r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", errors="replace").strip()


# ---- facts --------------------------------------------------------------------------------------

def code_checkout(run: Runner = _run) -> Path | None:
    """The git checkout of the RUNNING code (src layout: <repo>/src/apex_router), else None."""
    try:
        from .. import __file__ as pkg_file
        repo = Path(pkg_file).resolve().parents[2]
    except Exception:  # noqa: BLE001
        return None
    top = _out(_git(repo, "rev-parse", "--show-toplevel", run=run, timeout=30))
    if not top:
        return None
    try:
        return repo if Path(top).resolve() == repo.resolve() else None
    except OSError:
        return None


def head(repo: Path, run: Runner = _run) -> str | None:
    sha = _out(_git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", run=run, timeout=30))
    return sha if sha and _SHA_RE.match(sha) else None


def has_commit(repo: Path, sha: str, run: Runner = _run) -> bool:
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        return False
    r = _git(repo, "cat-file", "-e", f"{sha}^{{commit}}", run=run, timeout=30)
    return r is not None and r.returncode == 0


def is_ancestor(repo: Path, sha: str, run: Runner = _run) -> bool:
    """True if ``sha`` is HEAD or in HEAD's history. An object that only exists (a fetched
    FETCH_HEAD, a bundle, a rolled-back update) is NOT in our history.

    A git error or timeout gives False (never cached, so it is asked again). In a shallow clone
    an older commit can exist while the path to it is cut off: then a peer behind us looks
    ahead, and the node fetches once per MIN_GAP_S. That is bounded; the code checkout of a
    mesh node is a full clone."""
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        return False
    r = _git(repo, "merge-base", "--is-ancestor", sha, "HEAD", run=run, timeout=30)
    return r is not None and r.returncode == 0


def tool_receipt() -> dict | None:
    """The uv tool receipt of apex-router: {"editable": path|None, "extras": [..]} or None."""
    base = os.environ.get("UV_TOOL_DIR") or str(Path.home() / ".local/share/uv/tools")
    p = Path(base) / "apex-router" / "uv-receipt.toml"
    try:
        import tomllib
        doc = tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    for req in doc.get("tool", {}).get("requirements", []) or []:
        if isinstance(req, dict) and req.get("name") == "apex-router":
            extras = [e for e in req.get("extras", []) or [] if isinstance(e, str)
                      and _EXTRA_RE.match(e)]
            ed = req.get("editable")
            return {"editable": ed if isinstance(ed, str) else None, "extras": extras}
    return None


def norm_url(url: str) -> str:
    """``https://github.com/a/b.git`` and ``git@github.com:a/b`` -> ``github.com/a/b``."""
    u = url.strip()
    scheme = re.match(r"\A[a-z+]+://", u)
    u = u[scheme.end():] if scheme else u
    u = re.sub(r"\A[^@/]+@", "", u)
    host, sep, rest = u.partition("/")
    if scheme:
        u = host.split(":", 1)[0] + sep + rest               # drop a :port
    elif ":" in host:                                          # scp form host:path
        h, _, p = u.partition(":")
        u = f"{h}/{p}"
    u = u.rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    return u.lower()


def load_config(h: Path) -> dict:
    """``update.json`` with defaults. Bad or missing file = defaults (auto on)."""
    cfg: dict[str, Any] = {"auto_update": True, "upstreams": list(DEFAULT_UPSTREAMS)}
    with contextlib.suppress(OSError, ValueError):
        doc = json.loads((h / CONFIG_FILE).read_text(encoding="utf-8"))
        if isinstance(doc, dict):
            if isinstance(doc.get("auto_update"), bool):
                cfg["auto_update"] = doc["auto_update"]
            ups = doc.get("upstreams")
            if isinstance(ups, list) and all(isinstance(u, str) and u for u in ups):
                cfg["upstreams"] = [norm_url(u) for u in ups]
    if os.environ.get("APEX_MESH_NO_UPDATE"):
        cfg["auto_update"] = False
    return cfg


def read_status(h: Path) -> dict:
    with contextlib.suppress(OSError, ValueError):
        doc = json.loads((h / STATUS_FILE).read_text(encoding="utf-8"))
        if isinstance(doc, dict) and doc.get("status") in STATUSES:
            return doc
    return {}


def _write_status(h: Path, doc: dict) -> None:
    with contextlib.suppress(OSError):
        tmp = h / f".{STATUS_FILE}.tmp"
        tmp.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
        os.replace(tmp, h / STATUS_FILE)


# ---- one update ---------------------------------------------------------------------------------

def preflight(repo: Path, upstreams: Iterable[str], run: Runner = _run) -> str:
    """"" if the checkout may fast-forward, else a status code."""
    if _out(_git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", run=run, timeout=30)) != "main":
        return "not_main"
    st = _git(repo, "status", "--porcelain", "--untracked-files=no", run=run, timeout=60)
    if st is None or st.returncode != 0:
        return "error"
    if st.stdout.strip():
        return "dirty"
    url = _out(_git(repo, "remote", "get-url", "origin", run=run, timeout=30))
    if not url or norm_url(url) not in {norm_url(u) for u in upstreams}:
        return "bad_origin"
    return ""


def _uv() -> str | None:
    for c in (shutil.which("uv"), str(Path.home() / ".local/bin/uv"),
              str(Path.home() / ".cargo/bin/uv")):
        if c and os.access(c, os.X_OK):
            return c
    return None


def reinstall(repo: Path, run: Runner = _run) -> bool:
    """The uv steps of install.sh for an existing checkout. True on success."""
    uv = _uv()
    if uv is None:
        return False
    rec = tool_receipt() or {"editable": None, "extras": []}
    spec = f"{repo}[{','.join(rec['extras'])}]" if rec["extras"] else str(repo)
    venv_py = repo / ".venv" / "bin" / "python"
    if venv_py.exists():
        r = run([uv, "pip", "install", "--python", str(venv_py), "-e", spec],
                timeout=INSTALL_TIMEOUT_S)
        if r is None or r.returncode != 0:
            return False
    argv = [uv, "tool", "install", "--force"]
    if rec["editable"]:
        argv.append("--editable")
    r = run([*argv, spec], timeout=INSTALL_TIMEOUT_S)
    return r is not None and r.returncode == 0


def smoke(run: Runner = _run) -> bool:
    """Import the new code in a fresh interpreter (same python as this process)."""
    r = run([sys.executable, "-c", SMOKE_CODE], cwd="/", timeout=SMOKE_TIMEOUT_S)
    return r is not None and r.returncode == 0


def update(h: Path, *, repo: Path | None = None, dry_run: bool = False, run: Runner = _run,
           reinstall_fn: Callable[[Path], bool] | None = None,
           smoke_fn: Callable[[], bool] | None = None, now: float | None = None) -> dict:
    """Fetch origin and fast-forward a clean main. Returns and records a status dict. Never raises."""
    t = time.time() if now is None else float(now)
    out: dict[str, Any] = {"status": "error", "at": t}
    try:
        cfg = load_config(h)
        repo = repo or code_checkout(run)
        if repo is None:
            out["status"] = "no_checkout"
            return out
        out["repo"] = str(repo)
        old = head(repo, run)
        out["old"] = out["new"] = old
        why = preflight(repo, cfg["upstreams"], run)
        if why:
            out["status"] = why
            return out
        h.mkdir(parents=True, exist_ok=True)
        fd = os.open(h / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                out["status"] = "busy"
                return out
            out.update(_update_locked(repo, old, dry_run=dry_run, run=run,
                                      reinstall_fn=reinstall_fn or (lambda p: reinstall(p, run)),
                                      smoke_fn=smoke_fn or (lambda: smoke(run))))
        finally:
            os.close(fd)
        return out
    except Exception as e:  # noqa: BLE001 -- contract: never raises
        out.update(status="error", error=type(e).__name__)
        return out
    finally:
        if not dry_run:
            _write_status(h, out)


def _update_locked(repo: Path, old: str | None, *, dry_run: bool, run: Runner,
                   reinstall_fn: Callable[[Path], bool], smoke_fn: Callable[[], bool]) -> dict:
    r = _git(repo, "fetch", "--no-tags", "--quiet", "origin", "main", run=run)
    if r is None or r.returncode != 0:
        return {"status": "fetch_failed"}
    new = _out(_git(repo, "rev-parse", "--verify", "--quiet", "FETCH_HEAD^{commit}", run=run))
    if not old or not new or not _SHA_RE.match(new):
        return {"status": "error", "error": "rev_parse"}
    if new == old:
        return {"status": "ok", "new": old}
    anc = _git(repo, "merge-base", "--is-ancestor", old, new, run=run)
    if anc is None or anc.returncode != 0:
        return {"status": "diverged", "upstream": new}
    if dry_run:
        return {"status": "ok", "upstream": new, "behind": True}
    names = _out(_git(repo, "diff", "--name-only", old, new, run=run)) or ""
    deps = sorted(set(names.splitlines()) & set(DEP_FILES))
    m = _git(repo, "merge", "--ff-only", "--quiet", new, run=run)
    if m is None or m.returncode != 0:
        return {"status": "error", "error": "merge"}
    if (deps and not reinstall_fn(repo)) or not smoke_fn():
        _git(repo, "reset", "--hard", "--quiet", old, run=run)
        if deps:
            reinstall_fn(repo)
        return {"status": "error", "error": "reinstall" if deps else "smoke", "upstream": new,
                "new": old}
    return {"status": "updated", "new": new, "reinstalled": bool(deps)}


# ---- restart at idle ----------------------------------------------------------------------------

def restart_needed(start_commit: str | None, repo: Path | None, run: Runner = _run) -> bool:
    """True when the checkout HEAD moved since this process started (an update landed)."""
    if not start_commit or repo is None or os.environ.get("APEX_MESH_NO_UPDATE"):
        return False
    now = head(repo, run)
    return bool(now) and now != start_commit


def reexec() -> None:
    """Replace this process with the same command line (new code, same flags and env)."""
    sys.stdout.flush()
    sys.stderr.flush()
    # orig_argv keeps interpreter flags such as `-m pkg`; argv[0] alone would lose them.
    orig = getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv]
    os.execv(sys.executable, [sys.executable, *orig[1:]])


# ---- background driver for `mesh up` -----------------------------------------------------------

class AutoUpdater:
    """Decides when to run ``update`` (peer runs an unknown commit, or a periodic check) and runs
    it in a daemon thread. ``tick()`` is cheap; call it once per beacon cycle."""

    def __init__(self, h: Path, *, repo: Path | None = None, run: Runner = _run,
                 update_fn: Callable[..., dict] | None = None, now: Callable[[], float] = time.time,
                 every_s: float = CHECK_EVERY_S, gap_s: float = MIN_GAP_S) -> None:
        self.h, self.run, self.now = h, run, now
        self.repo = repo if repo is not None else code_checkout(run)
        self.start_commit = head(self.repo, run) if self.repo is not None else None
        self.update_fn = update_fn or (lambda: update(self.h, repo=self.repo, run=self.run))
        self.every_s, self.gap_s = float(every_s), float(gap_s)
        self.cfg = load_config(h)
        prev = read_status(h)
        self.status: dict = prev or {"status": "off" if not self.cfg["auto_update"] else "ok"}
        self.last = float(prev.get("at", 0.0)) if isinstance(prev.get("at"), (int, float)) else 0.0
        self._known: set[str] = set()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg["auto_update"]) and self.repo is not None

    def card_build(self) -> dict:
        st = self.status.get("status") if self.enabled else ("off" if self.repo else "no_checkout")
        doc: dict[str, Any] = {"update": st if st in STATUSES else "error", "caps": list(CAPS)}
        if self.start_commit:
            doc["commit"] = self.start_commit
        at = self.status.get("at")
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            doc["at"] = float(at)
        return doc

    def _peer_ahead(self, peers: Iterable[Any]) -> bool:
        for p in peers:
            c = (getattr(p, "build", None) or {}).get("commit")
            if not isinstance(c, str) or not _SHA_RE.match(c) or c == self.start_commit \
                    or c in self._known:
                continue
            if self.repo is not None and is_ancestor(self.repo, c, self.run):
                self._known.add(c)     # in HEAD history: the peer is behind us or equal
                continue
            return True
        return False

    def tick(self, peers: Iterable[Any] = ()) -> bool:
        """Start one background update if it is due. True if one started."""
        if not self.enabled or (self._thread is not None and self._thread.is_alive()):
            return False
        t = self.now()
        if t - self.last < self.gap_s:
            return False
        if t - self.last < self.every_s and not self._peer_ahead(peers):
            return False
        self.last = t

        def _go() -> None:
            res = self.update_fn()
            if isinstance(res, dict):
                self.status = res
        self._thread = threading.Thread(target=_go, name="apex-mesh-update", daemon=True)
        self._thread.start()
        return True

    def restart_needed(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        return restart_needed(self.start_commit, self.repo, self.run)
