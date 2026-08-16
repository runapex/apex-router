"""Cross-platform watcher install for apex-router's two background jobs.

Two jobs, two lifecycles (do NOT merge them — a daemon and a scheduled task are different things):
  - drain : an always-on worker draining the local job queue (KeepAlive / Restart=always)
  - daily : a once-a-day report + codeqa refresh (calendar-triggered)

One command manages both on either OS:
  - macOS  -> launchd user agents in ~/Library/LaunchAgents (launchctl bootstrap)
  - Linux  -> systemd --user units in ~/.config/systemd/user (systemctl --user)

Everything is idempotent and reversible (`apex-router watch uninstall`). Pure stdlib; no deps.
The unit files invoke `python -m apex_router.ornith.ornith_worker` and `-m apex_router.watch --run-daily`
via the SAME interpreter that installed them, so a venv install stays self-contained.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

LABEL_DRAIN = "com.apex-router.drain"
LABEL_DAILY = "com.apex-router.daily"
LABEL_SERVE = "com.apex-router.serve"   # the measuring proxy — a LIVE data plane, opt-in only

# Server-side proxy config the serve unit must carry (env doesn't propagate to launchd/systemd).
# Read from the environment at install time and baked into the unit so the gateway is reproducible.
_SERVE_ENV_KEYS = (
    "APEX_ANTHROPIC_UPSTREAM",  # the upstream/gateway the proxy forwards to (may be internal)
    "APEX_OPENAI_UPSTREAM",
    "APEX_PORT",
    "APEX_HOST",
    "APEX_HOME",
    # Azure-AD auth injection (opt-in) — the daemon has no interactive shell to carry a token, so if
    # injection is enabled these MUST ride in the unit. APEX_AZ_BIN pins az's absolute path because a
    # launchd/systemd unit runs with a minimal PATH. See proxy_engine.proxy.az_auth.
    "APEX_INJECT_AZURE_TOKEN",
    "APEX_AZURE_TOKEN_RESOURCE",
    "APEX_AZ_BIN",
    # Shadow posture, if the operator runs the managed proxy in shadow mode.
    "APEX_SHADOW",
    "APEX_POLICY_PATH",
)


def _is_loopback_url(url: str) -> bool:
    """True if the URL's host is loopback. Deriving the proxy upstream from a loopback URL would
    point the proxy at itself — the client-facing proxy URL (ANTHROPIC_FOUNDRY_BASE_URL) is loopback
    in 'proxy mode', so it must NOT be reused as the upstream. Uses `ipaddress` so the whole 127.0.0.0/8
    range and ::1 are caught (not just the literal 127.0.0.1), and strips a trailing FQDN dot."""
    import ipaddress
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _serve_env() -> dict:
    """Collect the serve-unit env: the known proxy keys from the environment, plus PYTHONPATH if the
    package isn't importable by the unit's interpreter (a source/dev checkout — a pip install needs
    none). Baked into the unit because env from the installing shell does not reach launchd/systemd."""
    env = {k: os.environ[k] for k in _SERVE_ENV_KEYS if os.environ.get(k)}
    # Restart survival: the managed proxy reads APEX_ANTHROPIC_UPSTREAM, but a machine wired for
    # Claude Code sets Claude Code's own ANTHROPIC_FOUNDRY_BASE_URL instead. Derive the proxy upstream
    # from it when the apex key isn't set explicitly — BUT only when it names a real gateway, not the
    # proxy itself. In "proxy mode" ANTHROPIC_FOUNDRY_BASE_URL points at http://localhost:8788 (THIS
    # proxy); baking that as the upstream makes the proxy forward to itself (an infinite loop). A
    # loopback foundry URL means "can't derive the upstream" — skip it and leave the config default.
    if "APEX_ANTHROPIC_UPSTREAM" not in env:
        foundry = os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL", "").strip()
        if foundry and not _is_loopback_url(foundry):
            env["APEX_ANTHROPIC_UPSTREAM"] = foundry
    import importlib.util
    if importlib.util.find_spec("apex_router") is None:
        # not pip-visible here; but if WE can import it, our sys.path[0] locates the src root
        pass
    else:
        # pinned so a dev/source run (not `pip install`ed into the venv) still resolves the package.
        import apex_router
        pkg_parent = str(Path(apex_router.__file__).resolve().parents[1])  # .../src
        env.setdefault("PYTHONPATH",
                       pkg_parent + (os.pathsep + os.environ["PYTHONPATH"]
                                     if os.environ.get("PYTHONPATH") else ""))
    # A value baked into a unit must never carry a newline/CR: it would inject extra plist keys or
    # systemd directives (these values are URLs/ports/paths, so a control char means a malformed
    # value anyway). Drop any offender rather than emit a corrupt/injectable unit.
    return {k: v for k, v in env.items() if "\n" not in v and "\r" not in v}


def _py() -> str:
    """The interpreter running this — unit files pin it so a venv install is self-contained."""
    return sys.executable


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _is_linux() -> bool:
    return platform.system() == "Linux"


# --------------------------------------------------------------------------- macOS (launchd)

def _launchd_plist(label: str, args: list[str], *, keepalive: bool,
                   calendar: tuple[int, int] | None, extra_env: dict | None = None) -> str:
    prog = "".join(f"\n        <string>{a}</string>" for a in args)
    if calendar is not None:
        hour, minute = calendar
        sched = (f"    <key>StartCalendarInterval</key>\n    <dict>\n"
                 f"        <key>Hour</key><integer>{hour}</integer>\n"
                 f"        <key>Minute</key><integer>{minute}</integer>\n    </dict>\n")
    else:
        sched = ""
    ka = "    <key>KeepAlive</key><true/>\n" if keepalive else ""
    home = str(Path.home())
    logdir = f"{home}/.apex-router/logs"
    # Baked env: PATH + HOME always, plus any caller-supplied keys (e.g. the proxy's upstream) so a
    # managed service carries its config — env from the installing shell does NOT propagate to launchd.
    env_lines = [
        '        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>',
        f'        <key>HOME</key><string>{home}</string>',
    ]
    for k, v in (extra_env or {}).items():
        env_lines.append(f"        <key>{_xml(k)}</key><string>{_xml(v)}</string>")
    env_block = "\n".join(env_lines)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>{prog}
    </array>
    <key>RunAtLoad</key><{'true' if keepalive else 'false'}/>
{ka}{sched}    <key>ThrottleInterval</key><integer>30</integer>
    <key>ProcessType</key><string>Background</string>
    <key>EnvironmentVariables</key>
    <dict>
{env_block}
    </dict>
    <key>StandardOutPath</key><string>{logdir}/{label}.log</string>
    <key>StandardErrorPath</key><string>{logdir}/{label}.err</string>
</dict>
</plist>
"""


def _xml(s: str) -> str:
    """Minimal XML-escape for values baked into a plist."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _launchd_install(no_drain: bool = False) -> list[str]:
    agents = Path.home() / "Library/LaunchAgents"
    logs = Path.home() / ".apex-router/logs"
    agents.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    done = []
    units = [
        (LABEL_DRAIN, [_py(), "-m", "apex_router.ornith.ornith_worker"], True, None),
        (LABEL_DAILY, [_py(), "-m", "apex_router.watch", "--run-daily"], False, (9, 0)),
    ]
    if no_drain:
        # The Ornith launchd stack (--ornith-serve) already installs com.ornith.worker draining the
        # SAME queue; running the drain watcher too puts two daemons on one single-GPU inbox. Skip
        # drain here and remove any drain unit a prior (non--ornith-serve) install left behind.
        units = [u for u in units if u[0] != LABEL_DRAIN]
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL_DRAIN}"], capture_output=True)
        (agents / f"{LABEL_DRAIN}.plist").unlink(missing_ok=True)
    for label, args, keepalive, cal in units:
        plist = agents / f"{label}.plist"
        plist.write_text(_launchd_plist(label, args, keepalive=keepalive, calendar=cal))
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                       capture_output=True)  # ignore if not loaded
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                       capture_output=True)
        done.append(label)
    return done


def _launchd_uninstall() -> list[str]:
    agents = Path.home() / "Library/LaunchAgents"
    uid = os.getuid()
    done = []
    for label in (LABEL_DRAIN, LABEL_DAILY):
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
        plist = agents / f"{label}.plist"
        if plist.exists():
            plist.unlink()
            done.append(label)
    return done


def _launchd_install_serve() -> list[str]:
    """Install ONLY the proxy-serve daemon (opt-in — it routes live traffic)."""
    agents = Path.home() / "Library/LaunchAgents"
    logs = Path.home() / ".apex-router/logs"
    agents.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    plist = agents / f"{LABEL_SERVE}.plist"
    plist.write_text(_launchd_plist(
        LABEL_SERVE, [_py(), "-m", "apex_router.cli", "serve"], keepalive=True, calendar=None,
        extra_env=_serve_env()))   # bake the upstream/port so the gateway is reproducible
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL_SERVE}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True)
    return [LABEL_SERVE]


def _launchd_uninstall_serve() -> list[str]:
    agents = Path.home() / "Library/LaunchAgents"
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL_SERVE}"], capture_output=True)
    plist = agents / f"{LABEL_SERVE}.plist"
    if plist.exists():
        plist.unlink()
        return [LABEL_SERVE]
    return []


# --------------------------------------------------------------------------- Linux (systemd --user)

def _systemd_dir() -> Path:
    return Path.home() / ".config/systemd/user"


def _systemd_units() -> dict[str, str]:
    py = _py()
    drain_service = f"""[Unit]
Description=apex-router local job drain worker
[Service]
Type=simple
ExecStart={py} -m apex_router.ornith.ornith_worker
Restart=always
RestartSec=30
[Install]
WantedBy=default.target
"""
    daily_service = f"""[Unit]
Description=apex-router daily report + codeqa refresh
[Service]
Type=oneshot
ExecStart={py} -m apex_router.watch --run-daily
"""
    daily_timer = """[Unit]
Description=apex-router daily report timer
[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true
[Install]
WantedBy=timers.target
"""
    return {
        "apex-router-drain.service": drain_service,
        "apex-router-daily.service": daily_service,
        "apex-router-daily.timer": daily_timer,
    }


def _systemctl(*args) -> None:
    subprocess.run(["systemctl", "--user", *args], capture_output=True)


def _systemd_install(no_drain: bool = False) -> list[str]:
    d = _systemd_dir()
    d.mkdir(parents=True, exist_ok=True)
    units = _systemd_units()
    if no_drain:
        # Ornith stack owns queue draining — don't also run the drain service on the same inbox.
        _systemctl("disable", "--now", "apex-router-drain.service")
        units.pop("apex-router-drain.service", None)
        (d / "apex-router-drain.service").unlink(missing_ok=True)
    for name, body in units.items():
        (d / name).write_text(body)
    _systemctl("daemon-reload")
    installed = []
    if not no_drain:
        _systemctl("enable", "--now", "apex-router-drain.service")
        installed.append("apex-router-drain.service")
    _systemctl("enable", "--now", "apex-router-daily.timer")
    installed.append("apex-router-daily.timer")
    return installed


def _systemd_uninstall() -> list[str]:
    d = _systemd_dir()
    _systemctl("disable", "--now", "apex-router-drain.service")
    _systemctl("disable", "--now", "apex-router-daily.timer")
    done = []
    for name in _systemd_units():
        f = d / name
        if f.exists():
            f.unlink()
            done.append(name)
    _systemctl("daemon-reload")
    return done


def _systemd_serve_unit() -> dict[str, str]:
    py = _py()
    # bake the proxy env into the unit (Environment= lines) so the gateway config is carried.
    env_lines = "".join(f"Environment={k}={v}\n" for k, v in _serve_env().items())
    return {"apex-router-serve.service": f"""[Unit]
Description=apex-router measuring proxy (serve)
[Service]
Type=simple
{env_lines}ExecStart={py} -m apex_router.cli serve
Restart=always
RestartSec=10
[Install]
WantedBy=default.target
"""}


def _systemd_install_serve() -> list[str]:
    d = _systemd_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name, body in _systemd_serve_unit().items():
        (d / name).write_text(body)
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", "apex-router-serve.service")
    return ["apex-router-serve.service"]


def _systemd_uninstall_serve() -> list[str]:
    d = _systemd_dir()
    _systemctl("disable", "--now", "apex-router-serve.service")
    done = []
    for name in _systemd_serve_unit():
        f = d / name
        if f.exists():
            f.unlink()
            done.append(name)
    _systemctl("daemon-reload")
    return done


# --------------------------------------------------------------------------- public API

def install(no_drain: bool = False) -> list[str]:
    """Install the watchers for the current OS. Returns the unit labels installed.
    With no_drain=True the always-on drain worker is skipped (and any existing one removed) —
    used when the Ornith launchd stack (--ornith-serve) already provides the queue drainer, so
    two daemons don't poll the same single-GPU inbox."""
    if _is_macos():
        return _launchd_install(no_drain=no_drain)
    if _is_linux():
        return _systemd_install(no_drain=no_drain)
    raise SystemExit(f"unsupported OS for watchers: {platform.system()}")


def uninstall() -> list[str]:
    if _is_macos():
        return _launchd_uninstall()
    if _is_linux():
        return _systemd_uninstall()
    raise SystemExit(f"unsupported OS: {platform.system()}")


def install_serve() -> list[str]:
    """Install the measuring proxy as an always-on managed service (opt-in — it routes live
    traffic). The proxy's upstream/port come from the environment (APEX_ANTHROPIC_UPSTREAM,
    APEX_PORT, …) at launch, so point it at your gateway via those before/at install."""
    if _is_macos():
        return _launchd_install_serve()
    if _is_linux():
        return _systemd_install_serve()
    raise SystemExit(f"unsupported OS for serve: {platform.system()}")


def uninstall_serve() -> list[str]:
    if _is_macos():
        return _launchd_uninstall_serve()
    if _is_linux():
        return _systemd_uninstall_serve()
    raise SystemExit(f"unsupported OS: {platform.system()}")


def status() -> str:
    if _is_macos():
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        lines = [ln for ln in out.splitlines() if "apex-router" in ln]
        return "\n".join(lines) or "(no apex-router launchd agents loaded)"
    if _is_linux():
        out = subprocess.run(["systemctl", "--user", "list-units", "--all", "apex-router*"],
                             capture_output=True, text=True).stdout
        return out or "(no apex-router systemd units)"
    return f"unsupported OS: {platform.system()}"


def run_daily() -> int:
    """Invoked by the daily unit. Runs the codeqa refresh + writes the offload report digest.
    Kept dependency-light and fail-open so a scheduled run never errors out the timer."""
    try:
        from .ornith import offload_report
        agg = offload_report.aggregate_offload(offload_report.DEFAULT_OFFLOAD_LOG)
        report = offload_report.format_report(agg)
    except Exception as e:  # noqa: BLE001
        report = f"(offload report unavailable: {e!r})"
    # Fold the per-task-type escalation rate into the daily digest so it surfaces without
    # anyone running `route-readout` by hand (and so the collected digest carries it).
    # Build the escalation section SEPARATELY so its Markdown heading lands OUTSIDE the code
    # fence the offload `report` is wrapped in (Codex #7 — a heading inside ``` renders as
    # literal text). Fail-open like the rest of run_daily — a bad/empty log never breaks the timer.
    def _clean(tt) -> str:
        # task_type is arbitrary user text; strip anything that could break the fence or a line.
        s = str(tt).replace("`", "").replace("\n", " ").replace("\r", " ").strip()
        return (s[:40] or "(blank)")
    esc = "\n## escalation (per task-type: n | escalated | rate)\n"
    try:
        from . import route_log
        rates = route_log.read_rates()
        if rates:
            esc += "```\n"
            for tt in sorted(rates, key=lambda k: str(k)):
                r = rates[tt]
                esc += f"  {_clean(tt):<12} n={r['n']:<4} escalated={r['escalated']:<4} rate={r['rate']:.2f}\n"
            esc += "```\n"
        else:
            esc += "  (no cheap-start outcomes logged yet)\n"
    except Exception as e:  # noqa: BLE001
        esc += f"  (unavailable: {type(e).__name__})\n"

    out = Path.home() / ".apex-router" / "offload_daily.md"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8") as f:
            # offload report inside its own fence; escalation section OUTSIDE it.
            f.write("\n## daily run\n```\n" + report + "\n```\n" + esc)
    except Exception as e:  # noqa: BLE001 — a write failure must not error out the scheduled timer
        print(f"(daily digest write failed: {type(e).__name__})")
    print(report)
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="apex-router watch",
                                 description="Install/manage apex-router background watchers.")
    ap.add_argument("action", nargs="?", default="status",
                    choices=["install", "uninstall", "status", "install-serve", "uninstall-serve"])
    ap.add_argument("--run-daily", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-drain", action="store_true",
                    help="skip the always-on drain worker (the Ornith stack drains the queue instead)")
    a = ap.parse_args(argv)
    if a.run_daily:
        return run_daily()
    if a.action == "install":
        print("installed watchers:", ", ".join(install(no_drain=a.no_drain)))
    elif a.action == "uninstall":
        print("removed watchers:", ", ".join(uninstall()) or "(none)")
    elif a.action == "install-serve":
        print("installed proxy service:", ", ".join(install_serve()))
        print("  the proxy is now always-on; its upstream/port come from the environment "
              "(APEX_ANTHROPIC_UPSTREAM / APEX_PORT). Point Claude Code at it with 'apex-router setup-proxy'.")
    elif a.action == "uninstall-serve":
        print("removed proxy service:", ", ".join(uninstall_serve()) or "(none)")
    else:
        print(status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
