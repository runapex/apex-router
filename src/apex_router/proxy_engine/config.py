"""Apex configuration — one place for endpoints, ports, paths, locked defaults.

Values resolve from env (APEX_* overrides) with public defaults. Point the upstreams at your
own gateway via APEX_ANTHROPIC_UPSTREAM / APEX_OPENAI_UPSTREAM if you front the providers with
a proxy; the defaults target the providers directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Upstream endpoints — default to the providers directly; override via env for a gateway/proxy.
DEFAULT_ANTHROPIC_UPSTREAM = os.environ.get("APEX_ANTHROPIC_UPSTREAM", "https://api.anthropic.com")
DEFAULT_OPENAI_UPSTREAM = "https://api.openai.com"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    # Wire
    host: str = _env("APEX_HOST", "127.0.0.1")
    port: int = int(_env("APEX_PORT", "8788"))
    anthropic_upstream: str = _env("APEX_ANTHROPIC_UPSTREAM", DEFAULT_ANTHROPIC_UPSTREAM)
    openai_upstream: str = _env("APEX_OPENAI_UPSTREAM", DEFAULT_OPENAI_UPSTREAM)
    upstream_connect_timeout_s: float = float(_env("APEX_CONNECT_TIMEOUT", "10"))
    upstream_read_timeout_s: float = float(_env("APEX_READ_TIMEOUT", "600"))

    # Paths
    home: Path = field(default_factory=lambda: Path(_env("APEX_HOME", str(Path.home() / ".apex"))))

    # TTFT budget — apex's added-latency ceiling (an invariant wall). Set low enough that a real
    # regression trips it: measured apex_added_ms p99 is well under 1ms, and the inline transforms
    # add ~1ms p99 on 128KB blocks, so 20ms = measured overhead + inline margin.
    ttft_budget_ms: int = int(_env("APEX_TTFT_BUDGET_MS", "20"))

    # Retention (§3.1): GC rows older than N days on startup
    retention_days: int = int(_env("APEX_RETENTION_DAYS", "14"))

    # Shadow mode (M6b Stage A / wire-switch rung A). When on, the proxy runs the full pipeline
    # (decide() over the frontier) and captures provider usage, logging both to telemetry — but
    # STILL forwards bytes verbatim (passthrough emission). Zero live risk; it just starts the
    # evidence clock (R1 wire-usage regression, predicted-Δ per cell). `policy_path` points at a
    # signed bundle the compiler emitted offline (the hot path can't compile — plane separation);
    # absent → shadow still logs raw bytes_by_class + usage (R1's X,y from request one).
    shadow_mode: bool = _env("APEX_SHADOW", "0") not in ("0", "", "false", "False")
    policy_path_env: str = _env("APEX_POLICY_PATH", "")

    # Telemetry heartbeat interval (§Step 2): a heartbeat line every N s so a consumer (the TUI)
    # can tell an idle proxy from a dead one. Off the request path.
    heartbeat_s: float = float(_env("APEX_HEARTBEAT_S", "15"))

    @property
    def db_path(self) -> Path:
        return self.home / "state.db"

    @property
    def telemetry_path(self) -> Path:
        return self.home / "telemetry.jsonl"

    @property
    def policy_path(self) -> Path:
        """The signed evidence bundle the shadow/live path loads.

        The JSON contains the sealed PolicyVersion plus the manifest that binds source, corpus,
        tokenizer, models, validators, and verified gate transcripts. Env override, else
        `~/.apex/policy.json`. `apex compile` writes it atomically; the proxy load-verifies it.
        """
        return Path(self.policy_path_env) if self.policy_path_env else self.home / "policy.json"

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        return self.home


# Module-level singleton; tests construct their own Config(home=tmp).
CONFIG = Config()

# apex code version — part of the epoch identity (§3.1: restart with new code == new epoch).
APEX_VERSION = "0.0.1"
