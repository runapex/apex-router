"""Apply restart-class proxy settings: write the plist, then restart the proxy ONLY when it is idle.

The gate reads the proxy's own `/stats` `activity` block (proxy_engine/proxy/app.py ActivityCounter):
  - `inflight` == 0 (a streamed request counts until its last byte), and
  - `idle_s` >= the idle window (default 120 s, never below MIN_IDLE_S).
A proxy that does not report `activity` (older code) or does not answer fails the gate: no restart.

Flow (one proxy at a time; a lock file blocks a second run):
  1. gate                     -> refuse, or wait (a background waiter polls until ready / timeout)
  2. backup the plist         -> studio/backups/<stamp>/ (same manifest format as file applies)
  3. write the new plist      -> plistlib, `plutil -lint`, atomic replace, same mode
  4. gate again               -> the window between 1 and 5 is closed (race)
  5. launchctl bootout + bootstrap (bootstrap retried; kickstart -k would keep the OLD env)
  6. health                   -> /healthz ok AND /stats started_s_ago < time since step 5 (a new process)
  7. on any failure after 3   -> restore the old plist, bootout + bootstrap again, health check, report

Nothing here runs at import. Tests inject `run` (argv -> (rc, out)) and `fetch` (url -> dict|None);
they never call the real launchctl.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from .flow import PROXIES, Paths, plist_env, tilde

MIN_IDLE_S = 60
DEFAULT_IDLE_S = 120
DEFAULT_MAX_WAIT_S = 1800
HEALTH_TIMEOUT_S = 25.0


class RestartError(Exception):
    def __init__(self, msg: str, status: int = 409):
        super().__init__(msg)
        self.status = status


def _run(argv: list) -> tuple:
    p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    return p.returncode, (p.stdout + p.stderr)[:65536]      # launchctl print puts pid near the top


def _fetch(url: str):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=2.5) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - down or not JSON = no answer
        return None


def proxy_port(paths: Paths, proxy: str) -> int:
    env = plist_env(paths.plist(proxy))
    try:
        return int(env.get("APEX_PORT") or PROXIES[proxy]["port"])
    except ValueError:
        return PROXIES[proxy]["port"]


def gate(paths: Paths, proxy: str, idle_s: float = DEFAULT_IDLE_S, fetch=_fetch) -> dict:
    """-> {"ready": bool, "why": str, "inflight", "idle_s", "need_idle_s"}. Fails closed."""
    need = max(MIN_IDLE_S, float(idle_s))
    st = fetch(f"http://127.0.0.1:{proxy_port(paths, proxy)}/stats")
    base = {"ready": False, "inflight": None, "idle_s": None, "need_idle_s": need}
    if not isinstance(st, dict):
        return {**base, "why": "the proxy does not answer /stats"}
    a = st.get("activity")
    if not isinstance(a, dict) or not isinstance(a.get("inflight"), int) or not isinstance(a.get("idle_s"), (int, float)):
        return {**base, "why": "the running proxy does not report activity (older code): restart it once by hand "
                               "when no agent is busy; after that the studio can restart it safely"}
    base.update(inflight=a["inflight"], idle_s=float(a["idle_s"]))
    if a["inflight"] > 0:
        return {**base, "why": f"{a['inflight']} request(s) in progress"}
    if a["idle_s"] < need:
        return {**base, "why": f"idle {a['idle_s']:.0f} s of {need:.0f} s"}
    return {**base, "ready": True, "why": f"idle {a['idle_s']:.0f} s, nothing in progress"}


def _write_plist(path: Path, data: dict, run) -> None:
    raw = plistlib.dumps(data)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        rc, out = run(["/usr/bin/plutil", "-lint", tmp])
        if rc != 0:
            raise RestartError(f"plutil -lint failed: {out.strip()[:120]}", 500)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _pause(s: float) -> None:
    time.sleep(s)


def _reload(label: str, plist: Path, run) -> tuple:
    dom = f"gui/{os.getuid()}"
    run(["/bin/launchctl", "bootout", f"{dom}/{label}"])          # not loaded = fine
    rc, out = 1, ""
    for i in range(4):                                             # bootstrap right after bootout can fail with 5 (EIO)
        rc, out = run(["/bin/launchctl", "bootstrap", dom, str(plist)])
        if rc == 0:
            break
        _pause(1.0 + i)
    return rc, out


def _healthy(paths: Paths, proxy: str, since: float, fetch, timeout: float = HEALTH_TIMEOUT_S) -> bool:
    port = proxy_port(paths, proxy)
    end = time.time() + timeout
    while time.time() < end:
        h = fetch(f"http://127.0.0.1:{port}/healthz")
        st = fetch(f"http://127.0.0.1:{port}/stats")
        a = (st or {}).get("activity") if isinstance(st, dict) else None
        if isinstance(h, dict) and isinstance(a, dict) and isinstance(a.get("started_s_ago"), (int, float)) \
                and a["started_s_ago"] <= time.time() - since + 1.0:
            return True                                            # a NEW process answers
        _pause(0.5)
    return False


DRAIN_WAIT_S = 30.0       # requests that started just before the drain get this long to finish


def _launchd_pid(label: str, run) -> int | None:
    rc, out = run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"])
    m = re.search(r"^\s*pid = (\d+)\s*$", out or "", re.M) if rc == 0 else None
    return int(m.group(1)) if m else None


def _signal(pid: int, sig: str, run) -> bool:
    return run(["/bin/kill", f"-{sig}", str(pid)])[0] == 0


def _drain(paths: Paths, proxy: str, label: str, run, fetch, wait_s: float = DRAIN_WAIT_S) -> int:
    """Make the proxy refuse new requests (503 + Retry-After) and wait until the requests in
    progress end. -> the drained pid. Raises RestartError (and ends the drain) if it cannot."""
    port = proxy_port(paths, proxy)
    st = fetch(f"http://127.0.0.1:{port}/stats")
    a = (st or {}).get("activity") if isinstance(st, dict) else None
    pid = a.get("pid") if isinstance(a, dict) else None
    if not isinstance(pid, int) or pid <= 1 or "draining" not in a:
        raise RestartError("the running proxy cannot drain (older code): restart it once by hand", 409)
    if _launchd_pid(label, run) != pid:                     # the port answers, but not from our job
        raise RestartError(f"the process on :{port} is not {label}", 409)
    if not _signal(pid, "USR1", run):
        raise RestartError("could not signal the proxy", 500)
    end = time.time() + wait_s
    while time.time() < end:
        st = fetch(f"http://127.0.0.1:{port}/stats")
        a = (st or {}).get("activity") if isinstance(st, dict) else None
        if isinstance(a, dict) and a.get("pid") == pid and a.get("draining") is True and a.get("inflight") == 0:
            return pid
        _pause(0.2)
    _signal(pid, "USR2", run)
    raise RestartError(f"not restarted: requests did not finish within {wait_s:.0f} s (drain ended)", 409)


def _undrain(paths: Paths, proxy: str, run, fetch) -> None:
    st = fetch(f"http://127.0.0.1:{proxy_port(paths, proxy)}/stats")
    a = (st or {}).get("activity") if isinstance(st, dict) else None
    if isinstance(a, dict) and isinstance(a.get("pid"), int) and a.get("draining"):
        _signal(a["pid"], "USR2", run)


def _swap(paths: Paths, proxy: str, label: str, plist: Path, run, fetch, health_timeout: float) -> tuple:
    """Drain, then bootout + bootstrap. -> (healthy, t0). A drain failure raises before bootout."""
    _drain(paths, proxy, label, run, fetch)
    t0 = time.time()
    rc, _ = _reload(label, plist, run)
    return rc == 0 and _healthy(paths, proxy, t0, fetch, health_timeout), t0


def _save(bdir: Path, manifest: dict) -> None:
    (bdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    os.chmod(bdir / "manifest.json", 0o600)


def restart(paths: Paths, proxy: str, env_changes: dict, idle_s: float = DEFAULT_IDLE_S,
            run=_run, fetch=_fetch, health_timeout: float = HEALTH_TIMEOUT_S) -> dict:
    """Write `env_changes` ({NAME: value}; may be empty = reload only) into the proxy plist and
    restart it if the idle gate passes. Order: lock, gate, backup, write, DRAIN (new requests get
    503; wait for inflight 0), bootout + bootstrap, health (a NEW process). Any failure after the
    write restores the old plist; a restore that needs a reload drains first too."""
    if proxy not in PROXIES:
        raise RestartError("unknown proxy", 400)
    plist = paths.plist(proxy)
    label = PROXIES[proxy]["label"]
    if not plist.is_file():
        raise RestartError(f"{tilde(plist)} not found", 404)
    paths.studio.mkdir(parents=True, exist_ok=True)
    lock = open(paths.studio / f"restart-{proxy}.lock", "w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RestartError("a restart of this proxy is already running", 409) from None
        g = gate(paths, proxy, idle_s, fetch)
        if not g["ready"]:
            raise RestartError(f"not restarted: {g['why']}", 409)
        before = plist.read_bytes()
        data = plistlib.loads(before)
        env = data.setdefault("EnvironmentVariables", {})
        for k, v in env_changes.items():
            env[str(k)] = str(v)
        stamp = time.strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}-r"
        bdir = paths.backups / stamp
        bdir.mkdir(parents=True, exist_ok=False)
        os.chmod(bdir, 0o700)
        (bdir / plist.name).write_bytes(before)
        os.chmod(bdir / plist.name, 0o600)
        after_bytes = plistlib.dumps(data)
        manifest = {"schema": 1, "created_at": time.time(), "rolled_back": False, "complete": False,
                    "kind": "plist", "proxy": proxy, "label": label,
                    "files": [{"path": str(plist), "backup": plist.name,
                               "sha_before": hashlib.sha256(before).hexdigest(),
                               "sha_after": hashlib.sha256(after_bytes).hexdigest()}]}
        _save(bdir, manifest)
        stage = "write"
        try:
            if env_changes:
                _write_plist(plist, data, run)
            stage = "drain"
            ok, t0 = _swap(paths, proxy, label, plist, run, fetch, health_timeout)
            stage = "swapped"
        except RestartError as e:
            if stage in ("write", "drain"):   # nothing restarted: the old process still runs
                plist.write_bytes(before)
                _undrain(paths, proxy, run, fetch)
                manifest.update(rolled_back=True, aborted=str(e))
                _save(bdir, manifest)
                raise RestartError(f"{e} (plist restored)", e.status) from None
            raise
        except BaseException as e:                     # e.g. a launchctl timeout after bootout
            plist.write_bytes(before)
            manifest.update(rolled_back=True, aborted=f"{type(e).__name__} at {stage}")
            _save(bdir, manifest)
            _undrain(paths, proxy, run, fetch)
            raise RestartError(f"restart failed ({type(e).__name__}); the old plist is restored. Check: "
                               f"launchctl print gui/$(id -u)/{label}", 500) from None
        if ok:
            manifest["complete"] = True
            try:
                _save(bdir, manifest)                  # the restart worked: a failed note must not undo it
            except OSError:
                pass
            return {"ok": True, "proxy": proxy, "label": label, "changed": sorted(env_changes),
                    "backup": bdir.name, "restart_s": round(time.time() - t0, 1)}
        # automatic restore. A new process that answers is drained first (it may already serve).
        plist.write_bytes(before)
        manifest.update(rolled_back=True, aborted="health check failed")
        _save(bdir, manifest)
        back = False
        try:
            if _launchd_pid(label, run) is not None:    # a new process runs: it may serve, drain it first
                back, _ = _swap(paths, proxy, label, plist, run, fetch, health_timeout)
            else:                                       # no process (crash loop or not loaded): nothing to break
                t1 = time.time()
                rc2, _ = _reload(label, plist, run)
                back = rc2 == 0 and _healthy(paths, proxy, t1, fetch, health_timeout)
        except RestartError:
            back = False                                # fail closed: no reload without a drain
        raise RestartError(
            "the proxy did not come back healthy with the new settings; the old plist is restored and "
            + ("the proxy runs again" if back else f"the proxy is NOT confirmed healthy: check launchctl print "
               f"gui/$(id -u)/{label}, or run launchctl kickstart -k gui/$(id -u)/{label}"),
            500 if back else 503)
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()


class Waiter:
    """One background "restart when idle" job per proxy. Polls the gate every `poll_s` until it
    passes (then runs restart()) or until `max_wait_s`. Status is readable; cancel stops it."""

    def __init__(self):
        self.jobs: dict = {}
        self.lock = threading.Lock()

    def status(self, proxy: str) -> dict:
        with self.lock:
            j = self.jobs.get(proxy)
            return dict(j["state"]) if j else {"state": "none"}

    def busy(self) -> bool:
        with self.lock:
            return any(j["state"]["state"] == "waiting" for j in self.jobs.values())

    def start(self, paths: Paths, proxy: str, env_changes: dict, idle_s: float, max_wait_s: float,
              run=_run, fetch=_fetch, poll_s: float = 5.0, health_timeout: float = HEALTH_TIMEOUT_S,
              on_done=None) -> dict:
        with self.lock:
            j = self.jobs.get(proxy)
            if j and j["state"]["state"] == "waiting":
                raise RestartError("a restart of this proxy is already waiting", 409)
            stop = threading.Event()
            state = {"state": "waiting", "started": time.time(), "deadline": time.time() + max_wait_s,
                     "changed": sorted(env_changes), "gate": None, "result": None}
            self.jobs[proxy] = {"stop": stop, "state": state}

        def loop():
            while not stop.is_set():
                g = gate(paths, proxy, idle_s, fetch)
                with self.lock:
                    state["gate"] = g
                if g["ready"]:
                    try:
                        r = restart(paths, proxy, env_changes, idle_s, run, fetch, health_timeout)
                        final = {"state": "done", "result": r}
                        if on_done:
                            try:
                                on_done()
                            except Exception:  # noqa: BLE001 - the restart itself succeeded
                                pass
                    except RestartError as e:
                        if "not restarted" in str(e) and time.time() < state["deadline"]:
                            stop.wait(poll_s)               # the gate closed again: keep waiting
                            continue
                        final = {"state": "failed", "result": {"error": str(e)}}
                    with self.lock:
                        state.update(final)
                    return
                if time.time() >= state["deadline"]:
                    with self.lock:
                        state.update(state="timeout", result={"error": f"not idle within the wait limit: {g['why']}"})
                    return
                stop.wait(poll_s)
            with self.lock:
                state.update(state="cancelled")

        threading.Thread(target=loop, daemon=True, name=f"studio-restart-{proxy}").start()
        return self.status(proxy)

    def cancel(self, proxy: str) -> dict:
        with self.lock:
            j = self.jobs.get(proxy)
            if j and j["state"]["state"] == "waiting":
                j["stop"].set()
        time.sleep(0.05)
        return self.status(proxy)
