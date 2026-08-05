"""Control-plane / data-plane separation — the enforceable `policy_provenance` invariant.

Apex v2.1 (Fable): the runtime (enforcement plane) must take NO estimated / economic / dollar
input. All economics — the cachesim, replay optimizer, pricing, survival/retrieval estimates —
live OFFLINE in `apex/tuner/` (the policy compiler) and emit only a static, versioned policy
table the runtime looks up. The runtime cannot compute or write policy; it routes, looks up a
rule, applies, checks structural floors, freezes.

There was never a runtime dollar-decision system to remove — this test PROVES the separation is
structural and keeps it that way: it fails the moment a hot-path module imports the economics, so
a future contributor cannot quietly put a Δ$ gate back on the request path.

The hot path (may NOT import apex_router.proxy_engine.tuner):   proxy/ · pipeline/ · session/ · telemetry/
The offline plane (economics live here):    tuner/  (+ cli.py, the offline entrypoint)
"""
from __future__ import annotations

import ast
import os

APEX = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src", "apex_router", "proxy_engine")

# Modules on the request hot path. None may reach the economics.
HOT_PATH_DIRS = ("proxy", "pipeline", "session", "telemetry")
# The forbidden dependency: the offline economics package.
FORBIDDEN_PREFIX = "apex_router.proxy_engine.tuner"


def _imports_of(py_path: str) -> list[str]:
    tree = ast.parse(open(py_path).read())
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
    return mods


def _hot_path_files() -> list[str]:
    out = []
    for d in HOT_PATH_DIRS:
        for root, _, files in os.walk(os.path.join(APEX, d)):
            out += [os.path.join(root, f) for f in files if f.endswith(".py")]
    return out


def test_hot_path_does_not_import_economics():
    """No enforcement-plane module may import apex_router.proxy_engine.tuner. This IS policy_provenance: the runtime
    cannot compute/price policy, only look it up. A violation = a Δ$ gate creeping onto the hot
    path — the exact thing v2.1 forbids."""
    violations = []
    for fp in _hot_path_files():
        for mod in _imports_of(fp):
            if mod.startswith(FORBIDDEN_PREFIX):
                violations.append(f"{os.path.relpath(fp, APEX)} imports {mod}")
    assert not violations, (
        "control/data-plane leak — hot path imports economics:\n  " + "\n  ".join(violations)
    )


def test_cachegate_is_purely_structural():
    """The one runtime per-block gate takes only STRUCTURAL signals (cache_control marker,
    ship_count, in_frozen_prefix) — no cost/dollar/estimated input. Pin its signature so an
    economic argument can't be added without this test noticing."""
    import inspect

    from apex_router.proxy_engine.pipeline import cachegate

    params = set(inspect.signature(cachegate.check).parameters)
    assert params == {"block_meta", "ship_count", "in_frozen_prefix"}
    src = inspect.getsource(cachegate.check).lower()
    for economic in ("cost", "dollar", "p_read", "p_write", "survival", "retriev", "break_even"):
        assert economic not in src, f"cachegate.check references economic signal '{economic}'"
