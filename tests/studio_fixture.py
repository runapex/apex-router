"""Fixture home for apex flow studio tests and screenshots (no test functions here).

``make_home(root, now)`` writes a fake HOME with: ~/.apex-router (models.json with pi_families,
pi-routes.json, route log, conformance, pi-guard, pi-compact, pressure, widget history, xval
bandit), ~/.apex and ~/.apex-local telemetry, and two launchd plists in ~/Library/LaunchAgents.
The plist carries a fake secret (``APEX_FAKE_API_TOKEN``) that must never reach any API output.
"""
from __future__ import annotations

import json
import plistlib
import random
from pathlib import Path

FAKE_SECRET = "sk-fixture-SECRET-do-not-show-0123456789"


def _jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def env_for(root: Path) -> dict:
    """Env overrides that point every studio path into the fixture home."""
    return {"HOME": str(root), "APEX_ROUTER_HOME": str(root / ".apex-router"),
            "APEX_MODEL_REGISTRY": str(root / ".apex-router" / "models.json"),
            "APEX_STUDIO_LAUNCHAGENTS": str(root / "Library" / "LaunchAgents"),
            "APEX_STUDIO_MAIN_HOME": str(root / ".apex"),
            "APEX_STUDIO_LOCAL_HOME": str(root / ".apex-local")}


def make_home(root: Path, now: float, seed: int = 7, telemetry: bool = True) -> Path:
    rnd = random.Random(seed)
    root = Path(root)
    rh = root / ".apex-router"
    rh.mkdir(parents=True, exist_ok=True)
    (rh / "models.json").write_text(json.dumps({
        "//": "fixture overlay",
        "pi_families": {
            "deep": {"provider": "foundry", "tier": "opus", "effort": "high"},
            "frontier": {"provider": "foundry", "tier": "sonnet", "effort": "medium"},
            "kimi": {"provider": "moonshotai", "id": "kimi-k2.6"},
        },
    }, indent=2) + "\n")
    (rh / "pi-routes.json").write_text(json.dumps({
        "review": {"provider": "foundry-gpt", "id": "it-entra-gpt-6.1-sol"},
    }, indent=2) + "\n")
    la = root / "Library" / "LaunchAgents"
    la.mkdir(parents=True, exist_ok=True)
    with open(la / "com.apex-router.serve.plist", "wb") as f:
        plistlib.dump({"Label": "com.apex-router.serve", "ProgramArguments": ["/usr/bin/true", "serve"],
                       "KeepAlive": True, "EnvironmentVariables": {
                           "APEX_HOME": str(root / ".apex"), "APEX_PORT": "8788",
                           "APEX_CODEX_CACHE_KEY": "1", "APEX_CODEX_CACHE_KEY_SHARDS": "4",
                           "APEX_FAKE_API_TOKEN": FAKE_SECRET,
                           "APEX_OPENAI_UPSTREAM": "https://gateway.example.invalid/openai"}}, f)
    with open(la / "com.apex.local-qwen-proxy.plist", "wb") as f:
        plistlib.dump({"Label": "com.apex.local-qwen-proxy", "ProgramArguments": ["/usr/bin/true"],
                       "EnvironmentVariables": {"APEX_HOME": str(root / ".apex-local")}}, f)
    if telemetry:
        rows = []
        t = now - 24 * 3600
        while t < now - 5:
            busy = now - t < 3600
            t += rnd.expovariate(1 / (25 if busy else 140))
            if t >= now:
                break
            codex = rnd.random() < 0.22
            err = rnd.random() < 0.035
            cause = rnd.choice(["ConnectError", "http_429", "midstream_ReadError"]) if err else None
            rows.append({"ts": t, "client": "codex" if codex else "claude-code",
                         "endpoint_id": "openai" if codex else "anthropic", "schema_version": 12,
                         "is_error": err, "error_cause": cause,
                         "connect_retries": 1 if (err and cause == "ConnectError") or rnd.random() < 0.02 else 0,
                         "ttft_ms": rnd.lognormvariate(7.2, 0.5), "tokens_in": rnd.randint(2, 900),
                         "tokens_out": rnd.randint(50, 6000), "cache_read_tokens": rnd.randint(0, 150000),
                         "shadow": {"n_blocks": 10}, "agent_id": "a1b2c3d4" if rnd.random() < 0.15 else None,
                         "cache_key_rewrite": {"shard": 1} if codex else None})
        hb = [{"ev": "hb", "schema_version": 12, "ts": now - 24 * 3600 + 60 * i, "requests": i, "errors": 0}
              for i in range(24 * 60)]
        _jsonl(root / ".apex" / "telemetry.jsonl", sorted(rows + hb, key=lambda r: r["ts"]))
        _jsonl(root / ".apex-local" / "telemetry.jsonl",
               [{"ts": now - 600 * i, "client": "codex", "endpoint_id": "openai", "is_error": False,
                 "ttft_ms": 400.0} for i in range(1, 30)])
        _jsonl(rh / "route_log.jsonl",
               [{"ts": now - 300 * i, "task_type": rnd.choice(["debug", "generate", "explore"]),
                 "model": "it-entra-claude-opus-5-5", "passed": i % 5 != 0, "escalated": i % 5 == 0,
                 "family": rnd.choice(["deep", "frontier", "auto"])} for i in range(1, 200)])
        _jsonl(rh / "conformance.jsonl",
               [{"ts": now - 420 * i, "surface": "resolve", "task_type": "debug", "requested_tier": "opus",
                 "resolved_model": "claude-opus-5-5", "matched": True} for i in range(1, 150)])
        _jsonl(rh / "pi-guard" / "2026-10-27-111.jsonl",
               [{"ts": int((now - 700 * i) * 1000), "rule": "no-force-push", "tool": "bash"} for i in range(1, 40)])
        _jsonl(rh / "pi-compact" / "2026-10-27-111.jsonl",
               [{"ts": int((now - 3000 * i) * 1000), "v": 1, "reason": "auto"} for i in range(1, 12)])
        (rh / "pressure.json").write_text(json.dumps({"level": "GREEN", "generated_at": now - 40,
                                                      "insufficient_sample": False}))
        _jsonl(rh / "widget" / "history.jsonl", [{"v": 1, "ts": now - 60 * i} for i in range(1, 300)])
        (rh / "xval_bandit.json").write_text(json.dumps({"version": 1, "categories": {
            "diff": {"arms": {"2000/focused": {"n": 127}, "4000/focused": {"n": 31}}},
            "report": {"arms": {"2000/open": {"n": 12}}}}}))
    return root
