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


def _py() -> str:
    """The interpreter running this — unit files pin it so a venv install is self-contained."""
    return sys.executable


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _is_linux() -> bool:
    return platform.system() == "Linux"


# --------------------------------------------------------------------------- macOS (launchd)

def _launchd_plist(label: str, args: list[str], *, keepalive: bool,
                   calendar: tuple[int, int] | None) -> str:
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
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key><string>{home}</string>
    </dict>
    <key>StandardOutPath</key><string>{logdir}/{label}.log</string>
    <key>StandardErrorPath</key><string>{logdir}/{label}.err</string>
</dict>
</plist>
"""


def _launchd_install() -> list[str]:
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


def _systemd_install() -> list[str]:
    d = _systemd_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name, body in _systemd_units().items():
        (d / name).write_text(body)
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", "apex-router-drain.service")
    _systemctl("enable", "--now", "apex-router-daily.timer")
    return ["apex-router-drain.service", "apex-router-daily.timer"]


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


# --------------------------------------------------------------------------- public API

def install() -> list[str]:
    """Install both watchers for the current OS. Returns the unit labels installed."""
    if _is_macos():
        return _launchd_install()
    if _is_linux():
        return _systemd_install()
    raise SystemExit(f"unsupported OS for watchers: {platform.system()}")


def uninstall() -> list[str]:
    if _is_macos():
        return _launchd_uninstall()
    if _is_linux():
        return _systemd_uninstall()
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
    out = Path.home() / ".apex-router" / "offload_daily.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as f:
        f.write("\n## daily run\n```\n" + report + "\n```\n")
    print(report)
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="apex-router watch",
                                 description="Install/manage apex-router background watchers.")
    ap.add_argument("action", nargs="?", default="status",
                    choices=["install", "uninstall", "status"])
    ap.add_argument("--run-daily", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.run_daily:
        return run_daily()
    if a.action == "install":
        print("installed watchers:", ", ".join(install()))
    elif a.action == "uninstall":
        print("removed watchers:", ", ".join(uninstall()) or "(none)")
    else:
        print(status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
