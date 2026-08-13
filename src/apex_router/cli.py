"""apex-router CLI — status / self-check entry point.

Kept intentionally tiny: the library is the product; this is the command the installer
calls to verify a working install and to report what's live (routing always; embedding
and local Ornith only if their services are up).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path


def _service_up(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def _status() -> dict:
    # Routing is ALWAYS available (pure-stdlib core). The two optional services are
    # probed, not required.
    ollama = _service_up("http://127.0.0.1:11434/api/tags")
    ornith = _service_up("http://127.0.0.1:8080/v1/models")
    import apex_router  # noqa: F401 — import proves the package loads
    return {
        "package": "apex-router",
        "routing": "available",              # request-signal classifier + gate + table, 0 services
        "embedding_classifier": "available" if ollama else "unavailable (ollama/nomic not running)",
        "local_ornith_bench": "available" if ornith else "unavailable (MLX server not running)",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="apex-router", description="Adaptive model routing.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status", help="report which components are live")
    verify = sub.add_parser("verify", help="self-check: import + report; exit 0 if routing works")
    verify.add_argument("--json", action="store_true")
    watch = sub.add_parser("watch",
                           help="manage background services (watchers: drain+daily; proxy: serve)")
    watch.add_argument("action", nargs="?", default="status",
                       choices=["install", "uninstall", "status",
                                "install-serve", "uninstall-serve"])
    setup_proxy = sub.add_parser(
        "setup-proxy",
        help="merge proxy client env into ~/.claude/settings.json (from --config/env)")
    setup_proxy.add_argument("--config", type=Path)
    setup_proxy.add_argument("--dry-run", action="store_true")
    # Phase-1 escalation outcome log (fail-safe, write-only). Records whether a
    # cheap-started subtask succeeded or escalated — the measurement half of the
    # escalation on-ramp. NOT a router.
    route_log_p = sub.add_parser(
        "route-log", help="record a cheap-start subtask outcome (ok|escalated)")
    route_log_p.add_argument("--task-type", required=True)
    route_log_p.add_argument("--start-tier", required=True,
                             help="the model the cheap attempt ran on")
    route_log_p.add_argument("--outcome", required=True,
                             help="ok = cheap succeeded; escalated = re-dispatched heavy")
    route_log_p.add_argument("--note", default="")
    # Readout: aggregate the outcome log into per-task-type escalation rates — the
    # Phase-1 payoff ("when we start explore cheap, how often does it bounce to opus?").
    readout_p = sub.add_parser(
        "route-readout", help="show per-task-type escalation rate from the outcome log")
    readout_p.add_argument("--json", action="store_true")
    # The measuring proxy engine (optional `[proxy]` extra). `serve`/`doctor`/`compile`/… are
    # delegated to apex_router.proxy_engine.cli; all args after the subcommand are forwarded.
    sub.add_parser("serve", help="run the measuring proxy (needs the [proxy] extra)",
                   add_help=False)
    sub.add_parser("proxy", help="proxy engine CLI: serve/doctor/compile/readout (needs [proxy])",
                   add_help=False)
    args, extra = ap.parse_known_args(argv)

    if args.cmd == "watch":
        from . import watch as watch_mod
        return watch_mod.main([args.action])

    if args.cmd in ("serve", "proxy"):
        # `serve` is shorthand for `proxy serve`; `proxy <sub> …` forwards the rest verbatim.
        # The proxy engine's heavy deps (starlette/uvicorn/…) load lazily inside its subcommands, so
        # a missing dep surfaces here — catch it and point at the extra instead of dumping a traceback.
        forwarded = (["serve"] if args.cmd == "serve" else []) + extra
        try:
            from .proxy_engine import cli as proxy_cli
            return proxy_cli.main(forwarded)
        except ImportError as e:
            print("the proxy engine needs the [proxy] extra: "
                  f"pip install 'apex-router[proxy]'  (missing: {e.name or e})")
            return 1

    if args.cmd == "route-log":
        # Fail-safe by contract: logging must never break a dispatch, so this always
        # exits 0 — a rejected/failed write is reported on stderr, not via exit code.
        from . import route_log
        ok = route_log.log_outcome(args.task_type, args.start_tier, args.outcome,
                                   note=args.note)
        if not ok:
            try:
                print(f"route-log: not recorded (outcome={args.outcome!r} invalid or "
                      "log unwritable)", file=sys.stderr)
            except Exception:
                pass  # a broken stderr must not turn a swallowed failure into a raise
        return 0

    if args.cmd == "route-readout":
        # Read-only observability — must never break a caller. read_rates is fail-safe
        # (str-only keys, so sort/format can't choke); the prints are wrapped so a
        # broken/closed stdout can't turn a readout into a nonzero exit either.
        from . import route_log
        try:
            rates = route_log.read_rates()
            if args.json:
                print(json.dumps(rates, indent=2, sort_keys=True))
            elif not rates:
                print("route-readout: no outcomes logged yet "
                      "(start cheap-eligible subtasks and run route-log)")
            else:
                print(f"{'task_type':<12} {'n':>5} {'escalated':>10} {'rate':>7}")
                for tt in sorted(rates):
                    r = rates[tt]
                    print(f"{tt:<12} {r['n']:>5} {r['escalated']:>10} {r['rate']:>7.2f}")
        except Exception:
            pass
        return 0

    if args.cmd == "setup-proxy":
        from . import proxy_setup
        pa = []
        if args.config:
            pa += ["--config", str(args.config)]
        if args.dry_run:
            pa.append("--dry-run")
        return proxy_setup.main(pa)

    if args.cmd in (None, "status", "verify"):
        st = _status()
        if getattr(args, "json", False):
            print(json.dumps(st, indent=2))
        else:
            print(f"apex-router: routing={st['routing']}; "
                  f"embedding={st['embedding_classifier']}; "
                  f"ornith={st['local_ornith_bench']}")
        # routing being available is the only hard requirement for a green install.
        return 0 if st["routing"] == "available" else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
