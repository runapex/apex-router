"""Replicate a proxy/Foundry client setup into ~/.claude/settings.json — config-driven, no secrets.

Some deployments front Claude Code with a local proxy (e.g. a measuring/routing proxy on
localhost) via Claude Code's env settings. This module merges the required `env` keys into
settings.json **from environment variables or a config file** — apex-router hardcodes NO URL,
NO key, NO value. It ships a documented template (`proxy.env.example`) with placeholders.

Safety properties (all tested):
  - MERGE, never overwrite: every unrelated setting (permissions, hooks, plugins, other env
    keys) is preserved. A blind write would wipe the user's config.
  - Backs up settings.json before editing.
  - Idempotent: re-running with the same config is a no-op.
  - No secrets: the proxy WIRING (base-url, feature flags, model-id mappings) is not sensitive;
    any actual auth the proxy itself needs lives in the PROXY's own env, never here.

Pure stdlib.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

# The client-side keys that point Claude Code at a proxy / Foundry and map model ids. These are
# NON-SECRET wiring. Only these keys are ever read from env/config and merged — nothing else.
PROXY_KEYS = (
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    # generic tuning some proxy setups rely on (safe, optional)
    "ENABLE_PROMPT_CACHING_1H",
)

DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"


def _parse_env_file(path: Path) -> dict:
    """Parse a KEY=VALUE file (# comments, blank lines ignored). Values are taken verbatim."""
    out: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def resolve_config(env: dict | None = None, config_file: Path | None = None) -> dict:
    """Collect the proxy keys to set, from a config file then env (env wins). Only known
    PROXY_KEYS are returned; anything else in the sources is ignored."""
    source = dict(os.environ) if env is None else dict(env)
    merged: dict[str, str] = {}
    if config_file is not None and Path(config_file).exists():
        merged.update(_parse_env_file(config_file))
    merged.update({k: source[k] for k in source if k in PROXY_KEYS})  # env overrides file
    return {k: v for k, v in merged.items() if k in PROXY_KEYS}


def merge_settings(settings: dict, proxy: dict) -> dict:
    """Return a copy of `settings` with `proxy` merged into its `env` block. Preserves every
    other key and every unrelated env key. Does not mutate the input."""
    out = copy.deepcopy(settings)
    if not proxy:
        return out
    env = dict(out.get("env") or {})
    env.update(proxy)
    out["env"] = env
    return out


def apply(settings_path: Path = DEFAULT_SETTINGS, proxy: dict | None = None,
          *, env: dict | None = None, config_file: Path | None = None) -> Path | None:
    """Merge the resolved proxy config into settings.json. Backs up first, writes valid JSON.

    Returns the backup Path if a change was made, or None if there was nothing to set (no-op).
    """
    if proxy is None:
        proxy = resolve_config(env=env, config_file=config_file)
    if not proxy:
        return None

    settings_path = Path(settings_path)
    if settings_path.exists():
        current = json.loads(settings_path.read_text())
    else:
        current = {}
        settings_path.parent.mkdir(parents=True, exist_ok=True)

    merged = merge_settings(current, proxy)
    if merged == current:
        return None  # already applied — idempotent no-op

    # back up before writing (timestamp-free name; caller may rotate)
    backup = settings_path.with_suffix(settings_path.suffix + ".apex-bak")
    if settings_path.exists():
        backup.write_text(settings_path.read_text())
    settings_path.write_text(json.dumps(merged, indent=2) + "\n")
    return backup if settings_path.exists() else None


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="apex-router setup-proxy",
        description="Merge proxy/Foundry client env into ~/.claude/settings.json "
                    "(values from --config file or environment; nothing hardcoded).")
    ap.add_argument("--config", type=Path, help="a KEY=VALUE file of proxy env keys")
    ap.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    ap.add_argument("--dry-run", action="store_true", help="print what would be set, write nothing")
    a = ap.parse_args(argv)

    cfg = resolve_config(config_file=a.config)
    if not cfg:
        print("no proxy keys found in env or --config; nothing to do.")
        print("set them in your environment or a --config file. Known keys:")
        for k in PROXY_KEYS:
            print(f"  {k}")
        return 0
    if a.dry_run:
        print("would set in", a.settings, "-> env:")
        for k, v in cfg.items():
            # base-url and flags are non-secret wiring; safe to echo
            print(f"  {k}={v}")
        return 0
    backup = apply(a.settings, cfg)
    if backup:
        print(f"merged proxy config into {a.settings} (backup: {backup})")
    else:
        print(f"proxy config already present in {a.settings}; no change")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
