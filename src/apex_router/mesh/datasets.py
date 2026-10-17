"""Allowlisted metrics registry for the LAN mesh (see docs/DESIGN-lan-mesh.md, "Data plane").

Only aggregates are served. Every value passes through `scrub()`. A failing dataset gives
`{"error": "unavailable"}`; an unknown or not-shared name gives `{"error": "not_shared"}`.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterable

DROP_KEYS = frozenset({"error", "path", "telemetry", "session", "session_id", "repo", "cwd",
                       "description", "note", "url"})
NOT_SHARED = {"error": "not_shared"}
UNAVAILABLE = {"error": "unavailable"}


def _pressure() -> Any:
    from .. import snapshot
    return snapshot.pressure_block()


def _errors15m() -> Any:
    from .. import snapshot
    return snapshot.errors_block()


def _measure() -> Any:
    from .. import snapshot
    return snapshot.measure_block()


def _route_rates() -> Any:
    from .. import route_log
    return route_log.read_rates()


def _route_advice() -> Any:
    from .. import route_advise
    return route_advise.advise()


def _worker() -> Any:
    from .. import agents
    w = agents.worker()
    # counts only: drop error, label (scrub drops error) and the machine-specific pid
    return {k: w[k] for k in ("running", "inbox", "queue_running") if k in w}


ALLOWLIST: dict[str, Callable[[], Any]] = {
    "pressure": _pressure,
    "errors15m": _errors15m,
    "measure": _measure,
    "route_rates": _route_rates,
    "route_advice": _route_advice,
    "worker": _worker,
}


def _bad_str(s: str) -> bool:
    return s.startswith(("/", "~")) or "\\" in s


def scrub(value: Any) -> Any:
    """Drop sensitive keys and path-like strings; recurse into dicts and lists.

    Dict entries with a bad str value are removed; list items that are bad str are removed.
    Result is JSON-safe: non-JSON leaf types become str, tuples/sets become lists.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            k = k if isinstance(k, str) else str(k)
            if k in DROP_KEYS:
                continue
            if isinstance(v, str) and _bad_str(v):
                continue
            out[k] = scrub(v)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [scrub(v) for v in value if not (isinstance(v, str) and _bad_str(v))]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def read(names: Iterable[str], shared: Iterable[str]) -> dict:
    """Read datasets `names`. Names that are unknown or not in `shared` give not_shared."""
    shared_set = frozenset(shared)
    out: dict = {}
    for name in names:
        fn = ALLOWLIST.get(name) if isinstance(name, str) and name in shared_set else None
        if fn is None:
            out[name] = dict(NOT_SHARED)
            continue
        try:
            val = scrub(fn())
            json.dumps(val)
            out[name] = val
        except Exception:  # noqa: BLE001 - a dataset never raises
            out[name] = dict(UNAVAILABLE)
    return out


def parse_share(values: Iterable[str] | None) -> frozenset:
    """Parse repeated `--share` values. None or empty -> all. "none" -> empty set."""
    vals = list(values or [])
    if not vals:
        return frozenset(ALLOWLIST)
    result: set = set()
    for v in vals:
        if v == "all":
            result |= set(ALLOWLIST)
        elif v == "none":
            continue
        elif v in ALLOWLIST:
            result.add(v)
        else:
            raise ValueError(f"unknown dataset: {v!r}")
    return frozenset(result)
