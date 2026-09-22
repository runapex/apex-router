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


def _env_bool(name: str, default: str = "0") -> bool:
    """Parse a boolean env var robustly: ONLY affirmative tokens enable it, so 'FALSE'/'no'/'off'/a
    typo all read as False. An opt-in flag must fail SAFE (disabled) when mis-set — a naive
    `value not in ('0','false')` would treat 'FALSE' as truthy and silently enable the feature."""
    return _env(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    # Wire
    host: str = _env("APEX_HOST", "127.0.0.1")
    port: int = int(_env("APEX_PORT", "8788"))
    anthropic_upstream: str = _env("APEX_ANTHROPIC_UPSTREAM", DEFAULT_ANTHROPIC_UPSTREAM)
    openai_upstream: str = _env("APEX_OPENAI_UPSTREAM", DEFAULT_OPENAI_UPSTREAM)
    upstream_connect_timeout_s: float = float(_env("APEX_CONNECT_TIMEOUT", "10"))
    upstream_read_timeout_s: float = float(_env("APEX_READ_TIMEOUT", "600"))
    # Connect-only retry: a ConnectError/ConnectTimeout means the TCP connection was NEVER
    # established, so the upstream never received the request — retry is provably safe (no
    # double-submission of a non-idempotent POST). A ReadError is deliberately NOT retried: the
    # request may already be in flight upstream, so a retry risks a duplicate completion. Measured
    # need: on a 3.7-day live window the cross-validation reviewer model saw 20 ConnectError +
    # 7 ReadError on ~1.2k calls (3.3%), the highest of any model. Bounded attempts + backoff.
    upstream_connect_retries: int = int(_env("APEX_CONNECT_RETRIES", "2"))
    upstream_connect_backoff_s: float = float(_env("APEX_CONNECT_BACKOFF", "0.25"))

    # Paths
    home: Path = field(default_factory=lambda: Path(_env("APEX_HOME", str(Path.home() / ".apex"))))

    # TTFT budget — apex's added-latency ceiling (an invariant wall). NOTE (2026-09, measured on
    # 12.3k live shadow requests): apex_added_ms is p50=5.8ms / p90=16ms / p99=35.8ms / max=245ms,
    # and 5.0% (623 reqs) EXCEED this 20ms budget — the wall is currently breached, not comfortably
    # clear. Cause is NOT wire latency: `apex_added_ms` = `pre_forward_ms`, which includes the
    # SYNCHRONOUS shadow-compute (block decomposition) done before forwarding. Breaches correlate
    # with body size — median context_bytes 355KB vs 44KB overall, n_blocks 123 vs 14 — i.e. large
    # requests pay O(blocks) decomposition on the critical path. The lone max (245ms, context_bytes=0)
    # is the az-auth token mint (`az` subprocess), a separate cold-path cost. The earlier "<1ms p99"
    # claim was a small-block microbench, not live traffic. NOTE: moving the compute off the request
    # path is NOT a valid fix — it only LOOKS observation-only today because no policy is admitted; by
    # design (decide.py/freeze.py) the per-block decision PRODUCES the bytes to forward, so once a
    # transform is admitted the compute is load-bearing BEFORE the forward and cannot be deferred.
    # The honest levers are therefore: (a) make decompose faster (cost is ~3.3ms/100KB, linear in
    # body size), or (b) raise this budget to a measured p99+margin. Left at 20ms so the breach stays
    # VISIBLE rather than hidden; lowering OVERSIZE_FRONTIER_BYTES is rejected — it blinds R1's X on
    # exactly the largest blocks it most needs.
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

    # Azure-AD auth injection (opt-in). When an Azure API Management gateway fronts the provider, it
    # authenticates with a short-lived Azure AD token. Off by default → the proxy stays pure
    # passthrough. When on, the proxy mints a token via `az` and injects it ONLY when the client sent
    # no auth of its own (strict superset); a fresh mint after `az login` needs no restart. See
    # proxy.az_auth. `az_bin` lets a managed unit pin an absolute path (its PATH is minimal).
    inject_azure_token: bool = _env_bool("APEX_INJECT_AZURE_TOKEN")
    azure_token_resource: str = _env("APEX_AZURE_TOKEN_RESOURCE", "https://cognitiveservices.azure.com")
    az_bin: str = _env("APEX_AZ_BIN", "az")

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
