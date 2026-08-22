"""apex-router CLI — status / self-check entry point.

Kept intentionally tiny: the library is the product; this is the command the installer
calls to verify a working install and to report what's live (routing always; embedding
and local Ornith only if their services are up).
"""
from __future__ import annotations

import argparse
import json
import os
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
    # Both the embedding classifier and the local Ornith tiers are now served by the SAME ollama
    # instance (the MLX server on :8080 is retired), so one probe answers for both — but they stay
    # separate lines because a live ollama with no Ornith tier pulled is a real, distinct state.
    from .ornith import local_tier
    base = os.environ.get("ORNITH_URL") or local_tier.DEFAULT_URL
    ollama = _service_up("http://127.0.0.1:11434/api/tags")
    ornith = _service_up(f"{base.rstrip('/')}/v1/models")
    import apex_router  # noqa: F401 — import proves the package loads
    return {
        "package": "apex-router",
        "routing": "available",              # request-signal classifier + gate + table, 0 services
        "embedding_classifier": "available" if ollama else "unavailable (ollama/nomic not running)",
        "local_ornith_bench": (f"available (tier={local_tier.resolve().name})" if ornith
                               else "unavailable (ollama not running)"),
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
    watch.add_argument("--no-drain", action="store_true",
                       help="on 'install', skip the drain worker (Ornith stack drains instead)")
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
    # Advise: turn the escalation rates into an evidence-backed routing recommendation per
    # task-type, gated on statistical significance (Wilson CI + a sample floor). Recommends only;
    # it never mutates a config or a skill — the caller reads the advice and decides.
    advise_p = sub.add_parser(
        "route-advise",
        help="cost-efficiency verdict per task-type (cheap-start vs heavy-start) when significant")
    advise_p.add_argument("--json", action="store_true")
    advise_p.add_argument("--min-n", type=int, default=None,
                          help="minimum samples before a verdict (default 30)")
    advise_p.add_argument("--cost-ratio", type=float, default=None,
                          help="C_heavy / C_cheap; sets the cost break-even escalation rate (default 5.0)")
    # The measuring proxy engine (optional `[proxy]` extra). `serve`/`doctor`/`compile`/… are
    # delegated to apex_router.proxy_engine.cli; all args after the subcommand are forwarded.
    sub.add_parser("serve", help="run the measuring proxy (needs the [proxy] extra)",
                   add_help=False)
    sub.add_parser("proxy", help="proxy engine CLI: serve/doctor/compile/readout (needs [proxy])",
                   add_help=False)
    # Local-model tier: which Ornith size is resident. Bare `ornith-tier` reports; a positional
    # name switches. Switching is never implicit — it costs a multi-GB model load.
    tier_p = sub.add_parser("ornith-tier",
                            help="show or switch the resident local Ornith tier (small|large)")
    tier_p.add_argument("tier", nargs="?", help="tier to switch to (omit to report status)")
    tier_p.add_argument("--json", action="store_true", help="machine-readable status")
    tier_p.add_argument("--unload", action="store_true",
                        help="unload every resident tier and exit (frees RAM; changes no config)")
    tier_p.add_argument("--no-warm", action="store_true",
                        help="write the tier without waiting for it to load")
    tier_p.add_argument("--no-reload", action="store_true",
                        help="skip restarting the launchd consumers")
    args, extra = ap.parse_known_args(argv)

    if args.cmd == "watch":
        from . import watch as watch_mod
        return watch_mod.main([args.action] + (["--no-drain"] if args.no_drain else []))

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

    if args.cmd == "route-advise":
        # Read-only, fail-safe (same contract as route-readout): never break a caller. Emits a
        # per-task-type recommendation gated on significance; HOLD (keep the static default) is the
        # safe common case. Exit is always 0 — this is advice, not a gate.
        from . import route_advise
        try:
            kw = {}
            if getattr(args, "min_n", None) is not None:
                kw["min_n"] = args.min_n
            if getattr(args, "cost_ratio", None) is not None:
                kw["cost_ratio"] = args.cost_ratio
            recs = route_advise.advise(**kw)
            if args.json:
                print(json.dumps(recs, indent=2, sort_keys=True))
            elif not recs:
                print("route-advise: no outcomes logged yet "
                      "(start cheap-eligible subtasks and run route-log)")
            else:
                be = next(iter(recs.values()))["break_even"]
                print(f"cost break-even escalation rate = {be:.2f} "
                      f"(cheap-first cheaper below it). COST-ONLY — assumes kept cheap output is acceptable.")
                print(f"{'task_type':<12} {'n':>5} {'rate':>6} {'95% CI':>13}  verdict")
                for tt in sorted(recs):
                    r = recs[tt]
                    ci = f"[{r['ci_low']:.2f},{r['ci_high']:.2f}]"
                    tag = r["verdict"].upper() + ("" if r["significant"] else " (keep default)")
                    print(f"{tt:<12} {r['n']:>5} {r['rate']:>6.2f} {ci:>13}  {tag}")
                    print(f"{'':<12} {'':>5} {'':>6} {'':>13}  ↳ {r['reason']}")
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

    if args.cmd == "ornith-tier":
        from .ornith import tier_switch
        if args.unload:
            freed = tier_switch.unload_all_tiers()
            print(f"unloaded: {', '.join(freed) if freed else 'nothing resident'}")
            return 0
        if args.tier:
            return tier_switch.switch(args.tier, warm_after=not args.no_warm,
                                      reload_units=not args.no_reload)
        st = tier_switch.status()
        if args.json:
            print(json.dumps(st, indent=2))
            return 0
        print(f"configured: {st['configured_tier']} -> {st['configured_model']}")
        print(f"  endpoint:  {st['url']}   ({st['ram_gb']:.0f} GB RAM)")
        print(f"  pulled:    {st['configured_is_pulled']}   resident: {st['configured_is_resident']}")
        print(f"  loaded now: {', '.join(st['resident_models']) or 'nothing'}")
        for n, t in st["tiers"].items():
            mark = "*" if n == st["configured_tier"] else " "
            fit = "fits" if t["fits"] else "TOO BIG"
            print(f"  {mark} {n:<6} {t['weights_gb']:>5.1f} GB  {fit:<7} {t['note']}")
        return 0

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
