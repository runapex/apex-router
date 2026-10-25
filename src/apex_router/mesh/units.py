"""systemd --user unit files for a Linux mesh node: ``mesh up`` and, opt-in, ``mesh worker``.

Writes files only. It never runs ``systemctl`` or ``loginctl``: the CLI prints those commands for
the owner of the host. The units pin the interpreter that wrote them (a venv install stays
self-contained) and carry only ``APEX_MESH_HOME`` and ``PATH`` from the installing shell (a user
unit starts with a minimal environment; the worker needs PATH to find its runner CLI).

Every value that goes into a unit must be a plain token: no whitespace, quotes, backslash, ``$``,
``;``, backtick, ``%`` or control characters. That keeps systemd quoting and specifiers out of the
picture, so an argument can never become a second directive or a shell fragment.
"""
from __future__ import annotations

import re

UP_UNIT = "apex-router-mesh.service"
WORKER_UNIT = "apex-router-mesh-worker.service"
_CARRIED_ENV = ("APEX_MESH_HOME", "PATH")
_SAFE_ARG = re.compile(r"\A[A-Za-z0-9._:/@+=,-]+\Z")
# systemd splits an unquoted Environment= value on whitespace: "PATH=/a B=1" sets two variables.
_UNSAFE_ENV = re.compile(r"[\s\x00-\x1f\x7f\"'\\$`]")


def _check_arg(a: str) -> str:
    if not _SAFE_ARG.match(a):
        raise ValueError(f"unsafe argument for a unit file: {a!r}")
    return a


def check_up_args(up_args: list[str]) -> None:
    """Parse ``up_args`` with the ``mesh up`` parser. Raises ValueError on a bad flag or value."""
    import contextlib
    import io

    from . import cli
    for a in up_args:
        _check_arg(a)
    ap = cli.build_parser()
    up = next(a for a in ap._subparsers._group_actions if a.choices and "up" in a.choices).choices["up"]
    for a in up_args:
        if a in ("-h", "--help"):
            raise ValueError("--help is not a `mesh up` argument for a unit")
        # argparse takes "--pee" for "--peer"; a later flag could make it ambiguous at boot.
        if a.startswith("-") and a.split("=", 1)[0] not in up._option_string_actions:
            raise ValueError(f"unknown `mesh up` flag {a.split('=', 1)[0]!r} (give the full name)")
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            ap.parse_args(["up", *up_args])
    except SystemExit:
        msg = err.getvalue().strip().splitlines()
        raise ValueError(msg[-1] if msg else "bad `mesh up` arguments") from None


def _env_lines(env: dict[str, str]) -> str:
    out = []
    for k in _CARRIED_ENV:
        v = env.get(k)
        if not v:
            continue
        if _UNSAFE_ENV.search(v):
            raise ValueError(f"unsafe {k} value for a unit file")
        out.append(f"Environment={k}={v.replace('%', '%%')}\n")
    return "".join(out)


def _service(desc: str, exec_args: list[str], py: str, env: dict[str, str]) -> str:
    if not _SAFE_ARG.match(py):
        raise ValueError(f"interpreter path {py!r} has unsafe characters; use a venv path "
                         "without spaces")
    cmd = " ".join(_check_arg(a) for a in [py, "-m", "apex_router.mesh.cli", *exec_args])
    return (f"[Unit]\nDescription={desc}\n"
            f"[Service]\nType=simple\n{_env_lines(env)}ExecStart={cmd}\n"
            "Restart=always\nRestartSec=30\n"
            "[Install]\nWantedBy=default.target\n")


def render_up(up_args: list[str], *, py: str, env: dict[str, str]) -> str:
    return _service("apex-router LAN mesh node (mesh up)", ["up", *up_args], py, env)


def render_worker(*, py: str, env: dict[str, str]) -> str:
    return _service("apex-router mesh job worker (mesh worker)", ["worker"], py, env)
