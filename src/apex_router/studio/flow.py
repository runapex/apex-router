"""The flow graph and the settings registry of apex flow studio.

The graph is static data: 5 lanes (sources, client steps, proxy, upstreams, sinks), one node per
stage, edges with a volume metric key (``live.metrics``) and a kind (main / error / fallback /
offload). Every node lists its code refs (file, line, a symbol that must be on that line; a test
keeps them true), the sinks it writes, and its settings.

A setting has an effect class:
  live     file-backed; Apply writes the file (backup first). Only pi family routes and efforts.
  restart  an env var of a long-running process (the proxy plist, or pi's shell). Apply only
           writes a plan (the plist diff + commands); the user runs it.
  locked   read-only here AND rejected by the server (auth, upstream URLs, signed policy, code
           constants).
A setting whose name contains KEY, TOKEN, SECRET or PASSWORD is secret: its value never leaves the
server ("set" / "not set"), and it is never editable.

Path resolution (all overridable, so tests and screenshots never touch the real home):
  router home  $APEX_ROUTER_HOME, else ~/.apex-router  (models.json: $APEX_MODEL_REGISTRY first)
  main proxy   $APEX_STUDIO_MAIN_HOME, else the plist's APEX_HOME, else ~/.apex
  local proxy  $APEX_STUDIO_LOCAL_HOME, else the plist's APEX_HOME, else ~/.apex-local
  plists       $APEX_STUDIO_LAUNCHAGENTS, else ~/Library/LaunchAgents
"""
from __future__ import annotations

import json
import os
import plistlib
import re
from dataclasses import dataclass
from pathlib import Path

SECRET_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.I)
# Token COUNTS, not credentials: the names say TOKENS but the values are sizes.
_NOT_SECRET = frozenset({"APEX_COMPACT_SUMMARY_TOKENS", "APEX_COMPACT_LEDGER_TOKENS"})

THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}")
PROVIDER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}")
FAMILY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}")
PATH_VALUE_RE = re.compile(r"[A-Za-z0-9._/~+-]{1,400}")


def is_secret(name: str) -> bool:
    return bool(name) and name not in _NOT_SECRET and bool(SECRET_RE.search(name))


# ---- paths ----------------------------------------------------------------------------------

PROXIES = {
    "main": {"port": 8788, "label": "com.apex-router.serve", "default_home": ".apex"},
    "local": {"port": 8789, "label": "com.apex.local-qwen-proxy", "default_home": ".apex-local"},
}


@dataclass
class Paths:
    router_home: Path
    models: Path
    launch_agents: Path
    env: dict

    @property
    def pi_routes(self) -> Path:
        return self.router_home / "pi-routes.json"

    @property
    def studio(self) -> Path:
        return self.router_home / "studio"

    @property
    def draft(self) -> Path:
        return self.studio / "draft.json"

    @property
    def backups(self) -> Path:
        return self.studio / "backups"

    def plist(self, proxy: str) -> Path:
        return self.launch_agents / f"{PROXIES[proxy]['label']}.plist"

    def proxy_home(self, proxy: str) -> Path:
        override = self.env.get("APEX_STUDIO_MAIN_HOME" if proxy == "main" else "APEX_STUDIO_LOCAL_HOME")
        if override:
            return Path(override).expanduser()
        env = plist_env(self.plist(proxy))
        if env.get("APEX_HOME"):
            return Path(env["APEX_HOME"]).expanduser()
        return Path(self.env.get("HOME") or Path.home()) / PROXIES[proxy]["default_home"]

    def telemetry(self, proxy: str) -> Path:
        return self.proxy_home(proxy) / "telemetry.jsonl"


def resolve_paths(env=None) -> Paths:
    e = dict(os.environ if env is None else env)
    home = Path(e.get("HOME") or Path.home())
    rh = Path(e["APEX_ROUTER_HOME"]).expanduser() if e.get("APEX_ROUTER_HOME") else home / ".apex-router"
    models = Path(e["APEX_MODEL_REGISTRY"]).expanduser() if e.get("APEX_MODEL_REGISTRY") else rh / "models.json"
    la = (Path(e["APEX_STUDIO_LAUNCHAGENTS"]).expanduser() if e.get("APEX_STUDIO_LAUNCHAGENTS")
          else home / "Library" / "LaunchAgents")
    return Paths(router_home=rh, models=models, launch_agents=la, env=e)


def plist_env(path: Path) -> dict:
    """EnvironmentVariables of a launchd plist ({} when missing or malformed). Values stay on the
    server; callers mask secrets before they show anything."""
    try:
        with open(path, "rb") as f:
            d = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
        return {}
    env = d.get("EnvironmentVariables") if isinstance(d, dict) else None
    return {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def tilde(p) -> str:
    """A path for display: the home prefix becomes ~ (the page shows no user name)."""
    s, h = str(p), str(Path.home())
    return "~" + s[len(h):] if h and h != "/" and (s == h or s.startswith(h + "/")) else s


# ---- settings registry ----------------------------------------------------------------------

def _s(sid, name, scope, cls, default, kind, help_, **kw):
    d = {"id": sid, "name": name, "scope": scope, "class": cls, "default": default, "kind": kind,
         "help": help_}
    d.update(kw)
    d["secret"] = is_secret(name)
    return d


# scope: pi_env (the shell that starts pi) | proxy_env (the selected proxy's launchd plist) |
# code (a constant) | file (generated per family, see family_settings)
SETTINGS = [
    _s("APEX_ROUTE_TABLE", "APEX_ROUTE_TABLE", "pi_env", "restart", "~/.apex-router/tables/route_table.skill.json",
       "path", "Route table file that `apex-router resolve` reads."),
    _s("min_confidence", "min_confidence", "code", "locked", "0.7", "float",
       "Below this classifier confidence the static map wins (consumer.resolve).",
       ref=["src/apex_router/consumer.py", 69, "min_confidence"]),
    _s("APEX_STE", "APEX_STE", "pi_env", "restart", "1", "bool", "STE system prompt on (1) or off (0)."),
    _s("APEX_GUARD", "APEX_GUARD", "pi_env", "restart", "1", "bool", "Tool-call deny rules on (1) or off (0)."),
    _s("APEX_HANGGUARD", "APEX_HANGGUARD", "pi_env", "restart", "1", "bool",
       "Bash hang guard on (1) or off (0)."),
    _s("APEX_PI_BASH_TIMEOUT", "APEX_PI_BASH_TIMEOUT", "pi_env", "restart", "600", "int",
       "Default bash timeout in seconds when a call sets none.", min=10, max=7200),
    _s("APEX_GUARDRULES_FILE", "APEX_GUARDRULES_FILE", "pi_env", "restart", "~/.apex-router/pi-guardrules.json",
       "path", "Deny-rule file of the tool guard."),
    _s("APEX_COMPACT", "APEX_COMPACT", "pi_env", "restart", "1", "bool", "Compaction ledger on (1) or off (0)."),
    _s("APEX_COMPACT_CACHE", "APEX_COMPACT_CACHE", "pi_env", "restart", "0", "bool",
       "Reuse the cached prefix for the compaction call (1) or not (0)."),
    _s("APEX_COMPACT_SUMMARY_TOKENS", "APEX_COMPACT_SUMMARY_TOKENS", "pi_env", "restart", "6000", "int",
       "Summary token budget.", min=500, max=64000),
    _s("APEX_AGENT_CAP", "APEX_AGENT_CAP", "pi_env", "restart", "4", "int",
       "Most sub-agent jobs that run at the same time.", min=1, max=16),
    _s("APEX_AGENT_MAX_DEPTH", "APEX_AGENT_MAX_DEPTH", "pi_env", "restart", "1", "int",
       "Most sub-agent levels (the code clamps to 3).", min=0, max=3),
    _s("APEX_SHADOW", "APEX_SHADOW", "proxy_env", "restart", "0", "bool",
       "Shadow compute on (1) or off (0); bytes are forwarded unchanged."),
    _s("APEX_POLICY_PATH", "APEX_POLICY_PATH", "proxy_env", "locked", "", "path",
       "Signed policy bundle. Locked: only the offline compiler sets it."),
    _s("APEX_CODEX_CACHE_KEY", "APEX_CODEX_CACHE_KEY", "proxy_env", "restart", "0", "bool",
       "Stable Codex prompt_cache_key (secret-like name: value hidden)."),
    _s("APEX_CODEX_CACHE_KEY_SHARDS", "APEX_CODEX_CACHE_KEY_SHARDS", "proxy_env", "restart", "4", "int",
       "Shard count of the cache key (secret-like name: value hidden).", min=1, max=64),
    _s("APEX_INJECT_AZURE_TOKEN", "APEX_INJECT_AZURE_TOKEN", "proxy_env", "locked", "0", "bool",
       "Azure AD token injection. Locked."),
    _s("APEX_AZURE_TOKEN_RESOURCE", "APEX_AZURE_TOKEN_RESOURCE", "proxy_env", "locked",
       "https://cognitiveservices.azure.com", "str", "Token audience. Locked."),
    _s("APEX_CONNECT_TIMEOUT", "APEX_CONNECT_TIMEOUT", "proxy_env", "restart", "10", "float",
       "Upstream connect timeout in seconds.", min=1, max=60),
    _s("APEX_READ_TIMEOUT", "APEX_READ_TIMEOUT", "proxy_env", "restart", "600", "float",
       "Upstream read timeout in seconds.", min=30, max=3600),
    _s("APEX_CONNECT_RETRIES", "APEX_CONNECT_RETRIES", "proxy_env", "restart", "2", "int",
       "Connect-only retries (the request never left).", min=0, max=10),
    _s("APEX_CONNECT_BACKOFF", "APEX_CONNECT_BACKOFF", "proxy_env", "restart", "0.25", "float",
       "Base backoff between connect retries in seconds.", min=0, max=5),
    _s("APEX_RETRY_POLICY", "APEX_RETRY_POLICY", "proxy_env", "restart", "immediate", "enum",
       "Retry action: immediate, wait or switch.", choices=["immediate", "wait", "switch"]),
    _s("APEX_RETRY_WAIT_MS", "APEX_RETRY_WAIT_MS", "proxy_env", "restart", "1000", "int",
       "Least wait before a retry for policy wait (clamped to 2000).", min=0, max=2000),
    _s("APEX_ANTHROPIC_UPSTREAM", "APEX_ANTHROPIC_UPSTREAM", "proxy_env", "locked", "(built-in)", "str",
       "Anthropic-wire upstream URL. Locked."),
    _s("APEX_OPENAI_UPSTREAM", "APEX_OPENAI_UPSTREAM", "proxy_env", "locked", "(built-in)", "str",
       "OpenAI-wire upstream URL. Locked."),
    _s("APEX_TTFT_BUDGET_MS", "APEX_TTFT_BUDGET_MS", "proxy_env", "restart", "20", "int",
       "Budget for apex-added latency (measure only).", min=1, max=1000),
    _s("APEX_TELEMETRY_KEEP", "APEX_TELEMETRY_KEEP", "proxy_env", "restart", "20", "int",
       "Rotated telemetry generations to keep.", min=1, max=200),
    _s("APEX_HEARTBEAT_S", "APEX_HEARTBEAT_S", "proxy_env", "restart", "15", "float",
       "Heartbeat interval in seconds.", min=5, max=300),
]
SETTINGS_BY_ID = {s["id"]: s for s in SETTINGS}

# ---- graph ----------------------------------------------------------------------------------

LANES = [
    {"id": "sources", "name": "Sources"},
    {"id": "client", "name": "Client steps"},
    {"id": "proxy", "name": "Proxy"},
    {"id": "upstreams", "name": "Upstreams"},
    {"id": "sinks", "name": "Sinks"},
]

R = "~/.apex-router/"


def _n(nid, lane, row, name, icon, does, metric, refs, sinks=(), settings=(), switch=None,
       locked=False, branches=(), unit=""):
    return {"id": nid, "lane": lane, "row": row, "name": name, "icon": icon, "does": does,
            "metric": metric, "unit": unit, "refs": [list(r) for r in refs], "sinks": list(sinks),
            "settings": list(settings), "switch": switch, "locked": locked, "branches": list(branches)}


NODES = [
    _n("src.pi", "sources", 0, "pi", "π", "pi coding agent sessions; a >>cue picks a model family per turn.",
       "pi.turns", [("integrations/pi/apex-route.ts", 375, 'pi.on("input"')], unit="turns"),
    _n("src.claude", "sources", 1, "claude", "✳", "Claude Code and pi foundry traffic on the Anthropic wire.",
       "tel.client.claude-code", [("src/apex_router/proxy_engine/proxy/handlers/passthrough.py", 25, "def detect_client")],
       unit="req"),
    _n("src.codex", "sources", 2, "codex", "◎", "Codex CLI traffic on the OpenAI wire; xval runs codex exec.",
       "tel.client.codex", [("src/apex_router/proxy_engine/proxy/handlers/passthrough.py", 25, "def detect_client")],
       unit="req"),
    _n("cue", "client", 0, "cue >>family", "»", "Maps a >>family cue to a provider and model id.",
       "pi.turns", [("integrations/pi/apex-route.ts", 375, 'pi.on("input"'),
                    ("integrations/pi/apex-route.ts", 128, "function loadRoutes")],
       sinks=[R + "route_log.jsonl"], unit="turns",
       branches=["local offload: >>local goes direct to ollama :11434 (no proxy hop)",
                 "escalation: the turn outcome (ok / escalated) goes to the route log"]),
    _n("resolve", "client", 1, "auto resolve", "⚖", "Classifies the task, reads the route table, else uses the static map.",
       "conf.resolve", [("src/apex_router/route_resolve.py", 136, "def resolve_text"),
                        ("src/apex_router/consumer.py", 68, "def resolve("),
                        ("src/apex_router/classify.py", 115, "def classify(")],
       sinks=[R + "conformance.jsonl"], settings=["APEX_ROUTE_TABLE", "min_confidence"], unit="resolves",
       branches=["fallback: low confidence or no promoted cell gives the static map (source field)"]),
    _n("ste", "client", 2, "STE prompt", "✎", "Adds the ASD-STE100 rules to the system prompt.",
       None, [("integrations/pi/lib/ste.ts", 27, "APEX_STE")], settings=["APEX_STE"], switch="APEX_STE"),
    _n("guards", "client", 3, "tool guards", "⛨", "Blocks denied tool calls and adds bash timeouts.",
       "pi.guard", [("integrations/pi/apex-compact.ts", 105, "APEX_GUARD"),
                    ("integrations/pi/apex-hangguard.ts", 29, "APEX_HANGGUARD"),
                    ("integrations/pi/lib/hangguard.ts", 23, "DEFAULT_TIMEOUT_S")],
       sinks=[R + "pi-guard/", R + "pi-hangguard/"],
       settings=["APEX_GUARD", "APEX_HANGGUARD", "APEX_PI_BASH_TIMEOUT", "APEX_GUARDRULES_FILE"],
       switch="APEX_GUARD", unit="events"),
    _n("compact", "client", 4, "compaction", "⇲", "Keeps a ledger of rules and ids when pi compacts.",
       "pi.compact", [("integrations/pi/apex-compact.ts", 144, "APEX_COMPACT")],
       sinks=[R + "pi-compact/"], settings=["APEX_COMPACT", "APEX_COMPACT_CACHE", "APEX_COMPACT_SUMMARY_TOKENS"],
       switch="APEX_COMPACT", unit="runs"),
    _n("agents", "client", 5, "sub-agents", "⑂", "Starts sub-agent jobs; the pressure gate queues them on RED.",
       "tel.agents", [("integrations/pi/apex-agents.ts", 131, "APEX_AGENT_CAP"),
                      ("integrations/pi/apex-agents.ts", 39, "APEX_AGENT_MAX_DEPTH"),
                      ("integrations/pi/lib/agents.ts", 224, "readPressure")],
       settings=["APEX_AGENT_CAP", "APEX_AGENT_MAX_DEPTH"], unit="req",
       branches=["pressure queue: a job waits as queued(pressure) while pressure is RED"]),
    _n("detect", "proxy", 0, "detect client", "⌖", "Sets client and session id from the path and headers.",
       "tel.requests", [("src/apex_router/proxy_engine/proxy/handlers/passthrough.py", 25, "def detect_client")],
       unit="req"),
    _n("shadow", "proxy", 1, "shadow", "◐", "Splits the body into blocks and predicts savings; bytes stay unchanged.",
       "tel.shadow", [("src/apex_router/proxy_engine/pipeline/shadow.py", 281, "def run_shadow"),
                      ("src/apex_router/proxy_engine/pipeline/shadow.py", 225, "OVERSIZE_FRONTIER_BYTES")],
       settings=["APEX_SHADOW", "APEX_POLICY_PATH"], switch="APEX_SHADOW", unit="req",
       branches=["oversize skip: a body over OVERSIZE_FRONTIER_BYTES is labeled, not split"]),
    _n("cachekey", "proxy", 2, "codex cache key", "⚿", "Rewrites the Codex prompt_cache_key to a stable value.",
       "tel.cachekey", [("src/apex_router/proxy_engine/proxy/upstream.py", 285, "def prepare_body"),
                        ("src/apex_router/proxy_engine/proxy/cache_key.py", 60, "def rewrite")],
       settings=["APEX_CODEX_CACHE_KEY", "APEX_CODEX_CACHE_KEY_SHARDS"], unit="req"),
    _n("auth", "proxy", 3, "auth injection", "⚷", "Adds an Azure AD token only when the client sent no auth.",
       None, [("src/apex_router/proxy_engine/proxy/az_auth.py", 79, "class AzureTokenProvider")],
       settings=["APEX_INJECT_AZURE_TOKEN", "APEX_AZURE_TOKEN_RESOURCE"], locked=True),
    _n("upstream", "proxy", 4, "upstream + retry", "⇄", "Sends upstream; retries only a connect that never reached it.",
       "tel.error_pct", [("src/apex_router/proxy_engine/proxy/upstream.py", 303, "async def send_stream"),
                         ("src/apex_router/proxy_engine/proxy/upstream.py", 87, "RETRY_POLICIES")],
       settings=["APEX_CONNECT_TIMEOUT", "APEX_READ_TIMEOUT", "APEX_CONNECT_RETRIES", "APEX_CONNECT_BACKOFF",
                 "APEX_RETRY_POLICY", "APEX_RETRY_WAIT_MS", "APEX_ANTHROPIC_UPSTREAM", "APEX_OPENAI_UPSTREAM"],
       unit="% err", branches=["retry: connect error, then backoff and send again",
                               "502 unreachable: all connects failed, the client gets 502"]),
    _n("response", "proxy", 5, "response + usage", "↩", "Streams the bytes back and reads ttft, tokens and cache use.",
       "tel.ttft_p50", [("src/apex_router/proxy_engine/proxy/handlers/passthrough.py", 204, "async def body_stream")],
       settings=["APEX_TTFT_BUDGET_MS"], unit="ms p50"),
    _n("up.anthropic", "upstreams", 0, "foundry / anthropic", "☁", "Anthropic wire (Azure foundry or Anthropic).",
       "tel.endpoint.anthropic", [("src/apex_router/proxy_engine/proxy/upstream.py", 303, "async def send_stream")],
       unit="req"),
    _n("up.openai", "upstreams", 1, "openai", "☁", "OpenAI wire (Codex, foundry-gpt, Kimi).",
       "tel.endpoint.openai", [("src/apex_router/proxy_engine/proxy/upstream.py", 303, "async def send_stream")],
       unit="req"),
    _n("up.ollama", "upstreams", 2, "local ollama", "▣", "Local models: ollama :11434 direct, or the local proxy :8789.",
       "local.requests", [("integrations/pi/apex-route.ts", 128, "function loadRoutes")], unit="req"),
    _n("sink.telemetry", "sinks", 0, "telemetry", "▤", "One JSON row per request plus heartbeat rows.",
       "tel.rows", [("src/apex_router/proxy_engine/telemetry/events.py", 261, "class TelemetryWriter"),
                    ("src/apex_router/proxy_engine/telemetry/events.py", 310, "def heartbeat")],
       sinks=["~/.apex/telemetry.jsonl", "~/.apex-local/telemetry.jsonl"],
       settings=["APEX_TELEMETRY_KEEP", "APEX_HEARTBEAT_S"], unit="rows"),
    _n("sink.routelog", "sinks", 1, "route log", "☰", "Turn outcomes: task type, start tier, ok or escalated.",
       "routelog.rows", [("src/apex_router/route_log.py", 310, "def log_outcome")],
       sinks=[R + "route_log.jsonl"], unit="rows"),
    _n("sink.pressure", "sinks", 2, "pressure", "◔", "GREEN / AMBER / RED from recent telemetry errors.",
       "pressure.level", [("src/apex_router/pressure.py", 306, "def compute"),
                          ("src/apex_router/pressure.py", 413, "def write_state")],
       sinks=[R + "pressure.json"]),
    _n("sink.history", "sinks", 3, "widget history", "▥", "Menu-bar samples for the sparklines.",
       "history.rows", [("src/apex_router/widget_history.py", 174, "def append")],
       sinks=[R + "widget/history.jsonl"], unit="rows"),
    _n("sink.xval", "sinks", 4, "xval bandit", "⚄", "Codex cross-validation arm stats (per category).",
       "xval.pulls", [("src/apex_router/xval_router.py", 289, "xval_bandit.json")],
       sinks=[R + "xval_bandit.json"], unit="pulls"),
]
NODES_BY_ID = {n["id"]: n for n in NODES}


def _e(src, dst, vol=None, kind="main", label=""):
    return {"id": f"{src}>{dst}" + (f":{kind}" if kind != "main" else ""), "from": src, "to": dst,
            "vol": vol, "kind": kind, "label": label}


EDGES = [
    _e("src.pi", "cue", "pi.turns"),
    _e("cue", "resolve", "conf.resolve", label=">>auto"),
    _e("resolve", "ste", "pi.turns"),
    _e("cue", "ste", None, kind="skip"),   # explicit >>family skips resolve (the title says it)
    _e("ste", "guards", "pi.turns"),
    _e("guards", "compact", "pi.turns"),
    _e("compact", "agents", "pi.turns"),
    _e("agents", "detect", "tel.client.claude-code"),
    _e("src.claude", "detect", "tel.client.claude-code", label="claude, codex: direct to proxy"),
    _e("src.codex", "detect", "tel.client.codex"),
    _e("detect", "shadow", "tel.requests"),
    _e("shadow", "cachekey", "tel.requests"),
    _e("cachekey", "auth", "tel.requests"),
    _e("auth", "upstream", "tel.requests"),
    _e("upstream", "response", "tel.requests"),
    _e("upstream", "up.anthropic", "tel.endpoint.anthropic"),
    _e("upstream", "up.openai", "tel.endpoint.openai"),
    _e("response", "sink.telemetry", "tel.rows"),
    _e("sink.telemetry", "sink.pressure", "tel.rows", label="derived"),
    _e("sink.pressure", "sink.history", "history.rows"),
    _e("src.codex", "sink.xval", "xval.pulls", label="xval"),
    _e("cue", "sink.routelog", "routelog.rows", kind="fallback", label="escalation label"),
    _e("cue", "up.ollama", "local.requests", kind="offload", label=">>local"),
    _e("resolve", "ste", "conf.fallback", kind="fallback", label="static fallback"),
    _e("upstream", "upstream", "tel.retries", kind="error", label="retry"),
    _e("upstream", "response", "tel.unreachable", kind="error", label="502 unreachable"),
]

BRANCHES = [
    {"name": "resolve fallback", "edge": "resolve>ste:fallback", "note": "the source field says why"},
    {"name": "retry", "edge": "upstream>upstream:error", "note": "connect retries, APEX_CONNECT_RETRIES"},
    {"name": "502 unreachable", "edge": "upstream>response:error", "note": "every connect failed"},
    {"name": "escalation label", "edge": "cue>sink.routelog:fallback", "note": "outcome ok / escalated"},
    {"name": "local offload", "edge": "cue>up.ollama:offload", "note": ">>local: ollama :11434 and :8789"},
    {"name": "pressure queue", "edge": None, "note": "sub-agent jobs wait on RED"},
    {"name": "oversize skip", "edge": None, "note": "shadow labels a body over the size cap"},
]


# ---- families (the live-class settings) -----------------------------------------------------

DEFAULT_ROUTES = {  # mirrors integrations/pi/apex-route.ts DEFAULT_ROUTES (built-in last resort)
    "local": {"provider": "ollama", "id": "(active ornith tier)"},
}


def _models_overlay(paths: Paths):
    raw = read_json(paths.models)
    return raw if isinstance(raw, dict) else None


def merged_families(paths: Paths) -> dict:
    """Family -> {provider, id, effort, source, editable{field: target}} with apex-route.ts order:
    registry (DEFAULTS + models.json overlay), then the pi-routes.json overlay wins per family."""
    from .. import model_registry
    reg = model_registry.load(paths.models)
    overlay = _models_overlay(paths) or {}
    ofams = overlay.get("pi_families") if isinstance(overlay.get("pi_families"), dict) else {}
    out: dict = {}
    for name, spec in (reg.get("pi_families") or {}).items():
        if not isinstance(spec, dict) or not isinstance(spec.get("provider"), str):
            continue
        if spec.get("source") == "ornith.env":
            mid = "(active ornith tier)"
        elif isinstance(spec.get("id"), str) and spec["id"]:
            mid = spec["id"]
        elif isinstance(spec.get("tier"), str):
            t = model_registry.tier_model(spec["tier"], registry=reg)
            mid = (model_registry._provider_prefix(spec["provider"], reg) + t) if t else None
        else:
            mid = None
        if not mid:
            continue
        own = ofams.get(name) if isinstance(ofams.get(name), dict) else None
        editable = {}
        if own is not None:
            editable["effort"] = "models.json"
            if isinstance(own.get("id"), str):
                editable["id"] = "models.json"
        out[name] = {"provider": spec["provider"], "id": mid,
                     "effort": spec.get("effort") if isinstance(spec.get("effort"), str) else None,
                     "tier": spec.get("tier"), "source": "models.json" if own is not None else "built-in",
                     "editable": editable}
    routes = read_json(paths.pi_routes)
    if isinstance(routes, dict):
        for name, r in routes.items():
            if isinstance(r, dict) and isinstance(r.get("provider"), str) and isinstance(r.get("id"), str):
                cur = out.get(name, {"effort": None, "tier": None})
                out[name] = {**cur, "provider": r["provider"], "id": r["id"], "source": "pi-routes.json",
                             "editable": {"route_id": "pi-routes.json", "route_provider": "pi-routes.json",
                                          **({"effort": "models.json"} if "effort" in cur.get("editable", {})
                                             else {})}}
    return out


def family_settings(paths: Paths) -> list:
    """One live-class setting per editable family field (only where the file and the family exist)."""
    out = []
    for name, f in sorted(merged_families(paths).items()):
        ed = f.get("editable") or {}
        if "effort" in ed:
            out.append(_s(f"family.{name}.effort", f"{name}.effort", "file", "live", "", "enum",
                          f"Reasoning effort of >>{name} (models.json pi_families).",
                          choices=list(THINKING_LEVELS), file="models.json", family=name, field="effort"))
        if "id" in ed:
            out.append(_s(f"family.{name}.id", f"{name}.id", "file", "live", "", "model",
                          f"Pinned model id of >>{name} (models.json pi_families).",
                          file="models.json", family=name, field="id"))
        if "route_id" in ed:
            out.append(_s(f"route.{name}.id", f"{name} route id", "file", "live", "", "model",
                          f"Model id of >>{name} in the pi-routes.json overlay (wins over models.json).",
                          file="pi-routes.json", family=name, field="id"))
            out.append(_s(f"route.{name}.provider", f"{name} route provider", "file", "live", "", "provider",
                          f"Provider of >>{name} in the pi-routes.json overlay.",
                          file="pi-routes.json", family=name, field="provider"))
    return out


def all_settings(paths: Paths) -> dict:
    d = dict(SETTINGS_BY_ID)
    for s in family_settings(paths):
        d[s["id"]] = s
    return d


def current_value(setting: dict, paths: Paths, proxy: str = "main") -> dict:
    """{"value": str|None, "state": "set"|"default"|"unknown"|"secret-set"|"secret-unset"} — masked."""
    scope = setting["scope"]
    if scope == "code":
        return {"value": setting["default"], "state": "code"}
    if scope == "pi_env":
        return {"value": None, "state": "unknown"}   # pi's shell env is not visible from here
    if scope == "proxy_env":
        env = plist_env(paths.plist(proxy))
        present = setting["name"] in env
        if setting["secret"]:
            return {"value": None, "state": "secret-set" if present else "secret-unset"}
        return {"value": env.get(setting["name"]), "state": "set" if present else "default"}
    if scope == "file":
        fams = merged_families(paths)
        f = fams.get(setting["family"]) or {}
        field = setting["field"]
        if setting["file"] == "pi-routes.json":
            r = read_json(paths.pi_routes) or {}
            v = (r.get(setting["family"]) or {}).get(field)
        else:
            v = f.get(field)
        return {"value": v if isinstance(v, str) else None, "state": "set" if v else "default"}
    return {"value": None, "state": "unknown"}


def public_setting(setting: dict, paths: Paths, proxy: str = "main") -> dict:
    out = {k: v for k, v in setting.items() if k not in ("ref",)}
    if setting.get("ref"):
        out["ref"] = list(setting["ref"])
    out["current"] = current_value(setting, paths, proxy)
    if setting["secret"]:
        out["default"] = "(hidden)"
    out["editable"] = setting["class"] != "locked" and not setting["secret"]
    return out


def build_flow(paths: Paths | None = None, proxy: str = "main") -> dict:
    paths = paths or resolve_paths()
    proxy = proxy if proxy in PROXIES else "main"
    sets = all_settings(paths)
    fam_ids = [s["id"] for s in family_settings(paths)]
    nodes = []
    for n in NODES:
        nn = dict(n)
        ids = list(n["settings"]) + (fam_ids if n["id"] == "cue" else [])
        nn["settings"] = [public_setting(sets[i], paths, proxy) for i in ids if i in sets]
        nodes.append(nn)
    fams = merged_families(paths)
    return {
        "schema": 1,
        "proxy": proxy,
        "proxies": {k: {"port": v["port"], "label": v["label"], "plist": tilde(paths.plist(k)),
                        "plist_present": paths.plist(k).is_file()} for k, v in PROXIES.items()},
        "lanes": LANES, "nodes": nodes, "edges": EDGES, "branches": BRANCHES,
        "families": {k: {kk: vv for kk, vv in v.items() if kk != "editable"} | {"editable": sorted(v["editable"])}
                     for k, v in sorted(fams.items())},
        "files": {"models": tilde(paths.models), "models_present": paths.models.is_file(),
                  "pi_routes": tilde(paths.pi_routes), "pi_routes_present": paths.pi_routes.is_file()},
    }


# ---- test a prompt (dry run) ----------------------------------------------------------------

CUE_RE = re.compile(r"^\s*>>([a-z0-9][a-z0-9._-]{0,47})(?:\s+(.*))?$", re.S)
_PROXIED = {"foundry", "foundry-gpt", "moonshotai", "anthropic-proxy"}
_UPSTREAM_OF = {"foundry": "up.anthropic", "anthropic": "up.anthropic", "foundry-gpt": "up.openai",
                "openai-codex": "up.openai", "openai": "up.openai", "moonshotai": "up.openai",
                "ollama": "up.ollama"}
CLIENT_CHAIN = ["cue", "resolve", "ste", "guards", "compact"]
PROXY_CHAIN = ["detect", "shadow", "cachekey", "auth", "upstream", "response"]


def _default_resolver(text: str, paths: Paths) -> dict:
    from .. import model_registry, route_resolve
    reg = model_registry.load(paths.models)
    table = route_resolve.default_table_path() if os.environ.get("APEX_ROUTE_TABLE") \
        else paths.router_home / "tables" / "route_table.skill.json"
    # embed_fn=None: no ollama call; record=False: no conformance row. A pure dry run.
    return route_resolve.resolve_text(text, registry=reg, table_path=table, embed_fn=None, record=False)


def _family_for_model(model: str, fams: dict) -> str | None:
    if not model:
        return None
    for name, f in sorted(fams.items()):
        fid = f.get("id") or ""
        if fid == model or fid.endswith("-" + model):
            return name
    return None


def test_path(prompt: str, paths: Paths | None = None, resolver=None) -> dict:
    """Which path a prompt takes. No model call, no proxy request, no log row."""
    paths = paths or resolve_paths()
    if not isinstance(prompt, str) or not prompt.strip():
        return {"ok": False, "error": "empty prompt"}
    if len(prompt) > 20000:
        return {"ok": False, "error": "prompt too long (max 20000 chars)"}
    fams = merged_families(paths)
    m = CUE_RE.match(prompt)
    cue = m.group(1) if m else None
    task = (m.group(2) or "").strip() if m else prompt.strip()
    skipped = []
    if cue and cue != "auto":
        if cue not in fams:
            return {"ok": True, "cue": cue, "family": None, "path": ["src.pi", "cue"], "edges": ["src.pi>cue"],
                    "skipped": [], "reason": {"source": "unknown_family", "text": f">>{cue} is not a known family"},
                    "families": sorted(fams), "dry_run": True}
        family = cue
        reason = {"source": "cue", "text": f"explicit >>{cue}: the resolver is not used"}
        skipped.append("resolve")
    else:
        try:
            out = (resolver or _default_resolver)(task or prompt, paths)
        except Exception as e:  # noqa: BLE001 — the dry run must answer, never crash the page
            return {"ok": False, "error": f"resolver failed: {type(e).__name__}"}
        model = out.get("model")
        try:
            from .. import model_registry
            reg = model_registry.load(paths.models)
            tier = next((t for t, mid in (reg.get("tiers") or {}).items() if mid == model), None)
        except Exception:  # noqa: BLE001
            tier = None
        family = tier if tier in fams else _family_for_model(model or "", fams)
        try:
            conf = float(out.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        reason = {"source": out.get("source"), "confidence": conf, "task_type": out.get("task_type"),
                  "cell": out.get("cell"), "model": model, "tier": tier, "embedding": out.get("embedding"),
                  "text": f"{out.get('source')} · {out.get('task_type') or 'unclassified'} (confidence {conf:.2f})"}
    f = fams.get(family) if family else None
    provider = (f or {}).get("provider")
    upstream = _UPSTREAM_OF.get(provider or "", "up.anthropic")
    nodes = ["src.pi"] + CLIENT_CHAIN
    edges = [f"{a}>{b}" for a, b in zip(["src.pi"] + CLIENT_CHAIN, CLIENT_CHAIN)]
    if provider == "ollama":
        nodes.append(upstream)
        edges.append("cue>up.ollama:offload")
        via = "direct to ollama :11434 (no proxy hop)"
    elif provider in _PROXIED:
        nodes += ["agents"] + PROXY_CHAIN + [upstream, "sink.telemetry"]
        edges += ["compact>agents", "agents>detect"] + [f"{a}>{b}" for a, b in zip(PROXY_CHAIN, PROXY_CHAIN[1:])]
        edges += [f"upstream>{upstream}", "response>sink.telemetry"]
        via = "apex proxy :8788"
    else:
        nodes.append(upstream)
        via = "direct to the provider (no apex proxy hop)"
    nodes = [x for x in nodes if x not in skipped]
    if skipped:
        edges = [e for e in edges if "resolve" not in e.split(">")] + ["cue>ste:skip"]
    return {"ok": True, "cue": cue or "auto", "family": family, "provider": provider,
            "model": (f or {}).get("id") or reason.get("model"), "effort": (f or {}).get("effort"),
            "upstream": upstream, "via": via, "path": nodes, "skipped": skipped, "edges": edges,
            "reason": reason, "dry_run": True}
