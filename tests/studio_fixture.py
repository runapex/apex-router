"""Fixture home for apex flow studio tests and screenshots (no test functions here).

``make_home(root, now)`` writes a fake HOME with: ~/.apex-router (models.json with pi_families,
pi-routes.json, route log, conformance, pi-guard, pi-compact, pressure, widget history, xval
bandit), ~/.apex and ~/.apex-local telemetry, and two launchd plists in ~/Library/LaunchAgents.
The plist carries a fake secret (``APEX_FAKE_API_TOKEN``) that must never reach any API output.
"""
from __future__ import annotations

import datetime
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
    """Env overrides that point every studio path into the fixture home (the v2 experiment roots
    too: the repo docs and ~/dev/ml come from the fixture, never the real checkout or home)."""
    return {"HOME": str(root), "APEX_ROUTER_HOME": str(root / ".apex-router"),
            "APEX_MODEL_REGISTRY": str(root / ".apex-router" / "models.json"),
            "APEX_STUDIO_LAUNCHAGENTS": str(root / "Library" / "LaunchAgents"),
            "APEX_STUDIO_MAIN_HOME": str(root / ".apex"),
            "APEX_STUDIO_LOCAL_HOME": str(root / ".apex-local"),
            "APEX_STUDIO_REPO": str(root / "repo"),
            "APEX_STUDIO_ML_ROOT": str(root / "dev" / "ml")}


# session ids of the fixture agents (file names follow the real layouts; see agents.session_kinds)
PI_SID = "apexjob-20261027120000-ab12"
PI_UUID_SID = "01a15a4c-d406-767d-ab67-134bbb838dad"
CLAUDE_SID = "5b0c1d2e-3f40-4a5b-8c6d-7e8f90a1b2c3"
CODEX_SID = "01a17e81-c547-7703-93d2-89a576057e5b"
UNKNOWN_SID = "d95da93b-1ad7-4c85-a994-7e90c5cd19d9"


def make_sessions(root: Path) -> None:
    """Session files of one pi (job id), one pi (uuid), one Claude Code and one Codex session.
    Contents carry a fake secret: attribution reads file NAMES only."""
    root = Path(root)
    pi = root / ".pi" / "agent" / "sessions" / "--Users-x-dev-repo--"
    pi.mkdir(parents=True, exist_ok=True)
    (pi / f"2026-10-27T12-00-00-000Z_{PI_SID}.jsonl").write_text(json.dumps({"cwd": "/x", "k": FAKE_SECRET}) + "\n")
    (pi / f"2026-10-20T17-12-26-887Z_{PI_UUID_SID}.jsonl").write_text("{}\n")
    cl = root / ".claude" / "projects" / "-Users-x-dev-repo"
    cl.mkdir(parents=True, exist_ok=True)
    (cl / f"{CLAUDE_SID}.jsonl").write_text(json.dumps({"prompt": FAKE_SECRET}) + "\n")
    cx = root / ".codex" / "sessions" / "2026" / "10" / "26"
    cx.mkdir(parents=True, exist_ok=True)
    (cx / f"rollout-2026-10-26T23-00-00-{CODEX_SID}.jsonl").write_text("{}\n")


def make_experiments(root: Path, now: float) -> None:
    """One good source file per experiment reader (shapes copied from the real files)."""
    root = Path(root)
    rh, repo, ml = root / ".apex-router", root / "repo", root / "dev" / "ml"
    res = repo / "docs" / "research"
    res.mkdir(parents=True, exist_ok=True)
    (res / "2026-10-08-p6-g1-attempt-3.md").write_text(
        "# P6 world model — attempt 3 (2026-10-08): G1 FAIL; the stop rule fires\n\n"
        "- **C1 FAIL** — rel −11.1% [−11.7%, −7.2%] (seeds)\n"
        "kill criterion (G1 at 50k main-session steps; today 7.1k main + 18k subagent steps)\n")
    (repo / "docs" / "RESEARCH-MAP.md").write_text(
        "| Experiment | Layer |\n|---|---|\n| JEPA | L4 | E2 (G1 attempts 1–3 FAIL; **paused** — stop rule fired) |\n")
    (res / "2026-10-08-gold-xval.md").write_text(
        "# Owner gold by cross-validation (2026-10-08): 222 labels, 15 corrections, judge 0.92 held out\n")
    (res / "2026-10-26-pi-compaction-precision-cost.md").write_text(
        "# pi auto-compaction\n\n## 3. Results (39 compactions, 13 sessions, 2026-10-13 .. 2026-10-27 UTC)\n")
    gold = rh / "labels" / "gold"
    gold.mkdir(parents=True, exist_ok=True)
    (gold / "results_for_mac_home.txt").write_text(
        "GOLD-LABEL RESULTS\nSTATUS: PROVISIONAL. Owner adjudication: 1/16 rows. finalize.py NOT run.\n\n"
        "compare.py: 100 rows. outcome agreement 91%, kappa 0.75. rework agreement 93%, kappa 0.82.\n")
    with open(gold / "adjudicate.csv", "w", newline="", encoding="utf-8") as f:
        f.write("task_id,project,task_type,request,passA,passB,owner_outcome,owner_rework\n")
        f.write(f'a1,ml,review,"{FAKE_SECRET} please fix",success,fail,success,no\n')
        f.write('a2,ml,debug,"multi\nline request",fail,partial,,\n')
    ex = rh / "explore" / "pilot-smoke-20261026-224006"
    ex.mkdir(parents=True, exist_ok=True)
    _jsonl(ex / "results.jsonl", [
        {"task": "binpack", "arm": "explore", "calls": 6, "tau": 0.985, "best_score": 0.97089, "hit": False},
        {"task": "binpack", "arm": "baseline", "calls": 6, "tau": 0.985, "best_score": 0.97089, "hit": False}])
    (ex / "table.md").write_text("| arm |\n")
    (rh / "xval_bandit.json").write_text(json.dumps({"version": 1, "categories": {
        "diff": {"arms": {"2000/focused": {"n": 127}, "4000/focused": {"n": 31}}},
        "report": {"arms": {"2000/open": {"n": 12}}}}}))
    _jsonl(rh / "xval_runs.jsonl", [{"ts": now - 600 * i, "ok": i % 5 != 0, "session_id": CODEX_SID,
                                     "model": "it-entra-gpt-6-astra"} for i in range(10, 0, -1)])
    panel = rh / "panel"
    panel.mkdir(parents=True, exist_ok=True)
    (panel / "card-debate-musique-356.json").write_text(json.dumps({
        "question": f"does a round beat sc? {FAKE_SECRET}", "incumbent": "sc6",
        "decision": "C: close the thread", "gate": [{"cell_id": "debate/musique/vs-sc6", "promoted": False,
                                                     "chosen_model": "sc6", "reason": FAKE_SECRET}],
        "contrasts": [{"name": "C1 rho1 vs sc6 (primary)", "metric": "brier_gain", "estimate": -0.000126,
                       "status": "no detectable difference"}],
        "meta": {"n": 356, "ts": "2026-10-13T12:15:56"}, "schema": 1}))
    rsi = ml / "rsi" / "outside"
    _jsonl(rsi / "judge_runs.jsonl", [
        {"loop": "pd_po", "artifact": FAKE_SECRET, "source": "judge:codex-xval", "total": t, "max": 20,
         "run": i, "ts": "2026-10-26T21:30:30Z"} for i, t in enumerate((14.0, 18.0, 16.0))])
    lm = ml / "logmine" / "data"
    lm.mkdir(parents=True, exist_ok=True)
    (lm / "oracle_coverage.json").write_text(json.dumps({
        "t_end": datetime.datetime.fromtimestamp(now - 30 * 86400, datetime.timezone.utc).isoformat(), "all": {"signals": 887, "caught": 807, "recall": 0.91},
        "goldens_recall_ml_drift": 0.818}))
    sw = ml / "sec" / "swarm" / "data"
    sw.mkdir(parents=True, exist_ok=True)
    (sw / "STATUS.json").write_text(json.dumps({
        "state": "DONE", "ok": True, "anomalies": [], "last_complete": now - 3600,
        "evaluate": {"ok": True, "summary": {"gate_open": False, "units": 4916, "queue": {"length": 4916}}}}))
    _jsonl(rh / "pi-compact" / "2026-10-26-20587.jsonl",
           [{"ts": int((now - 3600) * 1000), "v": 1, "reason": "manual", "tokens_before": 105478}])


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
