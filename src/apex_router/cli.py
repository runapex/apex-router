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
    args = ap.parse_args(argv)

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
