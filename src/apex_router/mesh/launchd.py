"""``apex-router mesh launchd install|uninstall|status``: run ``mesh up`` and ``mesh worker`` as
macOS LaunchAgents, so launchd starts them at login and again after a crash.

Two plists in ``~/Library/LaunchAgents``: ``com.apex-router.mesh-up`` and
``com.apex-router.mesh-worker``. KeepAlive and RunAtLoad are true, ThrottleInterval is 30 s,
stdout and stderr go to the existing ``up.log`` / ``worker.log`` in the mesh home, PATH is the PATH
of the installing shell, WorkingDirectory is the home directory.

``install`` never stops a process: it refuses while a ``mesh up`` or ``mesh worker`` runs outside
launchd (it prints the pids and the command to stop them), and while a label is loaded already.
It loads each plist with ``launchctl bootstrap gui/<uid>``. ``uninstall`` runs
``launchctl bootout`` (this stops the launchd-run processes) and deletes the plists.

Every function takes the LaunchAgents dir, the launchctl runner and the ps reader as arguments,
so tests never touch the real ones.
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from . import identity

ROLES = ("up", "worker")
LABELS = {"up": "com.apex-router.mesh-up", "worker": "com.apex-router.mesh-worker"}
LOGS = {"up": "up.log", "worker": "worker.log"}
THROTTLE_S = 30
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

Runner = Callable[[list[str]], "subprocess.CompletedProcess | None"]

_PID_RE = re.compile(r"^\s*pid = (\d+)\s*$", re.MULTILINE)
_EXIT_RE = re.compile(r"^\s*last exit code = (.+?)\s*$", re.MULTILINE)


def agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _launchctl(argv: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def _ps() -> str:
    try:
        r = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True,
                           timeout=30, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def exe_path() -> str | None:
    """Absolute path of the ``apex-router`` command (the uv tool shim), not a resolved symlink."""
    for c in (shutil.which("apex-router"), str(Path.home() / ".local/bin/apex-router")):
        if c and os.access(c, os.X_OK):
            return os.path.abspath(c)
    return None


def plist_path(agents: Path, role: str) -> Path:
    return agents / f"{LABELS[role]}.plist"


def plist_doc(role: str, *, exe: str, mesh_home: Path, path_env: str, home_dir: Path,
              mesh_home_env: str | None = None) -> dict:
    env = {"PATH": path_env}
    if mesh_home_env:
        env["APEX_MESH_HOME"] = mesh_home_env     # a second mesh home stays the same home
    log = str(mesh_home / LOGS[role])
    return {"Label": LABELS[role], "ProgramArguments": [exe, "mesh", role],
            "KeepAlive": True, "RunAtLoad": True, "ThrottleInterval": THROTTLE_S,
            "StandardOutPath": log, "StandardErrorPath": log,
            "EnvironmentVariables": env, "WorkingDirectory": str(home_dir)}


def render(doc: dict) -> bytes:
    return plistlib.dumps(doc, fmt=plistlib.FMT_XML, sort_keys=True)


def mesh_procs(ps_text: str, *, own_pid: int | None = None) -> list[tuple[int, str, str]]:
    """(pid, role, command) of every ``apex-router mesh up|worker`` process in ``ps`` text."""
    out = []
    for line in ps_text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, cmd = int(parts[0]), parts[1]
        toks = cmd.split()
        if pid == own_pid:
            continue
        # python -m apex_router.mesh.worker / python -m apex_router.mesh.cli up (xval 2026-10-26)
        mods = {toks[i + 1]: i + 1 for i, t in enumerate(toks[:-1]) if t == "-m"}
        if "apex_router.mesh.worker" in mods:
            out.append((pid, "worker", cmd))
            continue
        i = mods.get("apex_router.mesh.cli")
        if i is not None and i + 1 < len(toks) and toks[i + 1] in ROLES:
            out.append((pid, toks[i + 1], cmd))
            continue
        if not any(Path(t).name == "apex-router" or "apex_router" in t for t in toks):
            continue
        for i, t in enumerate(toks[:-1]):
            if t == "mesh" and toks[i + 1] in ROLES:
                out.append((pid, toks[i + 1], cmd))
                break
    return out


# The locks `mesh up` and `mesh worker` hold while they run (cli.LOCK_FILE, worker._take_lock).
LOCKS = {"up": "mesh.lock", "worker": "worker/lock"}


def held_locks(mesh_home: Path) -> list[str]:
    """Roles whose lock in this mesh home is held by a live process (any command line, any pid)."""
    held = []
    for role in ROLES:
        try:
            fd = os.open(mesh_home / LOCKS[role], os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            held.append(role)
        except OSError:
            pass
        finally:
            os.close(fd)
    return held


def label_state(label: str, *, uid: int, run: Runner) -> dict:
    r = run(["launchctl", "print", f"gui/{uid}/{label}"])
    if r is None or r.returncode != 0:
        return {"loaded": False, "pid": None, "last_exit": None}
    text = r.stdout or ""
    pid = _PID_RE.search(text)
    ex = _EXIT_RE.search(text)
    return {"loaded": True, "pid": int(pid.group(1)) if pid else None,
            "last_exit": ex.group(1) if ex else None}


def status(*, agents: Path, uid: int, run: Runner, ps: Callable[[], str]) -> dict:
    rows = []
    managed: set[int] = set()
    for role in ROLES:
        st = label_state(LABELS[role], uid=uid, run=run)
        if st["pid"]:
            managed.add(st["pid"])
        rows.append({"role": role, "label": LABELS[role], "plist": str(plist_path(agents, role)),
                     "installed": plist_path(agents, role).exists(), **st})
    outside = [{"pid": p, "role": r, "command": c} for p, r, c in mesh_procs(ps(), own_pid=os.getpid())
               if p not in managed]
    return {"agents": rows, "outside": outside}


def _stop_hint(pids: list[int]) -> str:
    return "kill " + " ".join(str(p) for p in sorted(pids))


def install(*, agents: Path, uid: int, run: Runner, ps: Callable[[], str], exe: str | None,
            mesh_home: Path, path_env: str, home_dir: Path, mesh_home_env: str | None = None,
            dry_run: bool = False, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    if exe is None:
        print("mesh launchd: no `apex-router` on PATH or in ~/.local/bin; run install.sh first",
              file=sys.stderr)
        return 2
    docs = {role: plist_doc(role, exe=exe, mesh_home=mesh_home, path_env=path_env,
                            home_dir=home_dir, mesh_home_env=mesh_home_env) for role in ROLES}
    if dry_run:
        for role in ROLES:
            print(f"# {plist_path(agents, role)}", file=out)
            out.write(render(docs[role]).decode("utf-8"))
        print(f"# load: launchctl bootstrap gui/{uid} <plist>  (dry run: nothing written)",
              file=out)
        return 0
    if sys.platform != "darwin":
        print("mesh launchd: macOS only (launchd)", file=sys.stderr)
        return 2
    procs = mesh_procs(ps(), own_pid=os.getpid())
    if procs:
        print("mesh launchd: mesh processes run outside launchd; install does not stop them:",
              file=out)
        for pid, role, cmd in procs:
            print(f"  pid {pid}  mesh {role}  {cmd}", file=out)
        print(f"stop them, then run install again: {_stop_hint([p for p, _, _ in procs])}",
              file=out)
        return 1
    held = held_locks(mesh_home)
    if held:
        print(f"mesh launchd: a process holds the lock of mesh {', '.join(held)} in {mesh_home}; "
              "install does not stop it. Stop it, then run install again.", file=out)
        return 1
    loaded = [LABELS[r] for r in ROLES if label_state(LABELS[r], uid=uid, run=run)["loaded"]]
    if loaded:
        print(f"mesh launchd: already loaded: {', '.join(loaded)}; run "
              "`apex-router mesh launchd uninstall` first", file=out)
        return 1
    agents.mkdir(parents=True, exist_ok=True)
    mesh_home.mkdir(parents=True, exist_ok=True)
    for role in ROLES:
        p = plist_path(agents, role)
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_bytes(render(docs[role]))
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
    rc = 0
    for role in ROLES:
        p = plist_path(agents, role)
        r = run(["launchctl", "bootstrap", f"gui/{uid}", str(p)])
        if r is None or r.returncode != 0:
            why = ((r.stderr or r.stdout or "").strip() if r is not None else "no result")[-300:]
            print(f"mesh launchd: bootstrap {LABELS[role]} failed: {why}", file=out)
            rc = 1
        else:
            print(f"mesh launchd: loaded {LABELS[role]} ({p})", file=out)
    return rc


def _abs_env(v: str | None) -> str | None:
    """APEX_MESH_HOME as an absolute path: launchd runs with WorkingDirectory = home, so a relative
    value would point to another mesh home (xval 2026-10-26)."""
    return str(Path(v).expanduser().resolve()) if v else None


def uninstall(*, agents: Path, uid: int, run: Runner, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    rc = 0
    for role in ROLES:
        label, p = LABELS[role], plist_path(agents, role)
        if label_state(label, uid=uid, run=run)["loaded"]:
            r = run(["launchctl", "bootout", f"gui/{uid}/{label}"])
            if r is None or r.returncode != 0:
                why = ((r.stderr or r.stdout or "").strip() if r is not None else "no result")[-300:]
                print(f"mesh launchd: bootout {label} failed: {why}", file=out)
                rc = 1
                continue
            print(f"mesh launchd: unloaded {label}", file=out)
        if p.exists():
            p.unlink()
            print(f"mesh launchd: removed {p}", file=out)
    return rc


def cmd(action: str, *, dry_run: bool = False, as_json: bool = False,
        agents: Path | None = None, run: Runner | None = None, ps: Callable[[], str] | None = None,
        exe: str | None = None, out: TextIO | None = None) -> int:
    """CLI entry. ``None`` arguments use the real LaunchAgents dir, launchctl and ps."""
    agents = agents if agents is not None else agents_dir()
    run = run or _launchctl
    ps = ps or _ps
    out = out or sys.stdout
    uid = os.getuid()
    if action == "install":
        return install(agents=agents, uid=uid, run=run, ps=ps,
                       exe=exe if exe is not None else exe_path(),
                       mesh_home=identity.mesh_home().resolve(),
                       path_env=os.environ.get("PATH") or DEFAULT_PATH, home_dir=Path.home(),
                       mesh_home_env=_abs_env(os.environ.get("APEX_MESH_HOME")),
                       dry_run=dry_run, out=out)
    if action == "uninstall":
        if dry_run:
            for role in ROLES:
                print(f"would bootout gui/{uid}/{LABELS[role]} and remove "
                      f"{plist_path(agents, role)}", file=out)
            return 0
        return uninstall(agents=agents, uid=uid, run=run, out=out)
    doc: dict[str, Any] = status(agents=agents, uid=uid, run=run, ps=ps)
    if as_json:
        print(json.dumps(doc, indent=2, sort_keys=True), file=out)
        return 0
    for r in doc["agents"]:
        print(f"{r['role']:<7} {r['label']:<28} plist={'yes' if r['installed'] else 'no':<3} "
              f"loaded={'yes' if r['loaded'] else 'no':<3} pid={r['pid'] or '-'} "
              f"last_exit={r['last_exit'] or '-'}", file=out)
    for o in doc["outside"]:
        print(f"outside launchd: pid {o['pid']}  mesh {o['role']}  {o['command']}", file=out)
    return 0
