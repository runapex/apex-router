"""Per-user path containment — everything serve writes lives under `home` (WP1b, multi-tenant).

apex is a per-user tool: all persistent state (state.db, telemetry.jsonl, policy.json, key) roots
at `~/.apex/` (or `$APEX_HOME`), so two tenants on one box never share or clobber each other's data.
This is likely already true — the test makes it a CONTRACT, so a future path escape outside the
home fails CI. Same discipline as the authority-defaults/no-embedded-key pins.
"""
from __future__ import annotations

from pathlib import Path

from apex_router.proxy_engine.config import Config


def test_all_config_paths_are_under_home(tmp_path):
    home = tmp_path / "tenant_home"
    cfg = Config(home=home)
    # every path-producing property + the seal key file must be inside `home`
    from apex_router.proxy_engine.policy import _KEY_FILENAME
    paths = {
        "db_path": cfg.db_path,
        "telemetry_path": cfg.telemetry_path,
        "policy_path": cfg.policy_path,
        "key_path": home / _KEY_FILENAME,
    }
    for name, p in paths.items():
        p = Path(p).resolve()
        assert home.resolve() in p.parents or p == home.resolve(), (
            f"{name} = {p} is NOT under home {home} — a per-user path escaped the tenant home"
        )


def test_two_tenants_get_disjoint_paths(tmp_path):
    a = Config(home=tmp_path / "tenant_a")
    b = Config(home=tmp_path / "tenant_b")
    for attr in ("db_path", "telemetry_path", "policy_path"):
        pa, pb = getattr(a, attr), getattr(b, attr)
        assert pa != pb, f"{attr} collides across tenants: {pa}"


def test_the_containment_check_is_not_vacuous(tmp_path):
    """Meta: a path OUTSIDE home must be caught by the containment predicate — else the pin passes
    for a real escape."""
    home = (tmp_path / "h").resolve()
    escaped = Path("/etc/apex/leaked.db").resolve()
    assert not (home in escaped.parents or escaped == home), (
        "the containment predicate accepts a path outside home — vacuous"
    )
