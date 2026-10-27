# Studio v2 (Metro) API contract

Status: fixed for the v2 build. Backend and UI work against this file. Change it only by owner decision.
All routes: 127.0.0.1 only, header `X-Studio-Token` (no token in URLs for new routes), Host/Origin check,
JSON bodies only. Existing routes (`/api/flow`, `/api/live`, `/api/test`, `/api/draft`, `/api/apply`,
`/api/rollback`, `/api/restart`, `/api/ping`) keep their shapes.

Plain names (UI and API `label` fields): agents `pi`, `Claude Code`, `Codex`, `Other`;
providers `Claude models` (anthropic/foundry), `GPT models` (openai), `Models on this Mac` (ollama/local).
No prompt text, header, key or token value ever appears in any response.

## Agent attribution

`agent` is one of `pi`, `claude`, `codex`, `other`. pi sends Claude-style headers, so the telemetry
`client` field alone is wrong: map `session_id` to the agent kind the same way the menu snapshot does
(pi session files, codex sessions). Unknown = `other`.

## GET /api/recent?n=50   (n 1..200)

```json
{"requests": [{
  "id": "telemetry row id or ts-hash", "ts": 1793120000.1,
  "agent": "pi", "provider": "anthropic", "model_family": "Claude Opus",
  "status": "ok | error | retried", "cause": "Provider not reached (only when not ok)",
  "ms_total": 5400, "ttft_ms": 1200,
  "tokens": {"in": 2, "out": 689, "cache_read": 340993, "cache_write": 242}
}]}
```
Newest first. `cause` is a plain sentence from a fixed map of error causes (no raw exception text).

## GET /api/tokens?window=15m|1h|24h

```json
{"window": "15m",
 "total": {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0},
 "by_agent":    {"pi": {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}},
 "by_provider": {"anthropic": {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}},
 "by_model":    {"Claude Opus": {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}},
 "cache_share": 0.75}
```
`cache_share` = cache_read / (in + cache_read + cache_write), the share of input tokens served from
cache. The backend must check provider semantics (Anthropic `input_tokens` excludes cache; OpenAI
cached tokens are inside input) and normalise so `in` never double counts.

## GET /api/experiments

```json
{"items": [{
  "id": "jepa", "name": "JEPA world model",
  "state": "running | paused | done | failed | provisional | unknown",
  "headline": "Gate G1 failed. Paused by its stop rule.",
  "metric": {"name": "C1 rel", "value": -11.1, "unit": "%"},
  "gate": {"name": "G1", "passed": false},
  "n": 7100, "updated_ts": 1791500000, "stale": true,
  "source": "~/dev/apex-router/docs/research/2026-10-08-p6-g1-attempt-3.md",
  "error": null
}]}
```
Readers are fixed in code (no path from the request). Each reader caps bytes read and fails open:
on any error the item has `state: "unknown"` and a short `error`. `stale` = older than 7 days.

## GET /api/stream   (Server-Sent Events, `text/event-stream`)

The UI reads it with `fetch()` + a stream reader (so the token stays in the header; no EventSource).

| event | when | data |
|---|---|---|
| `snapshot` | at connect, then every 2 s | `{"status": {"level": "ok|slow|problem", "sentence": "All good. Last request 4 s ago."}, "lines": [{"agent": "pi", "provider": "anthropic", "rpm": 3.2, "err_pct": 0.0, "live": true}], "proxy": {"up": true, "inflight": 0, "idle_s": 12.0, "draining": false}}` |
| `request` | each new telemetry row | one item, same shape as `/api/recent` |
| comment `: hb` | every 15 s | keep-alive |

Limits: at most 4 open streams (5th gets 429). A stream ends when the client goes away. Open streams
do not keep the studio alive past `--idle-exit`. The tail follows the telemetry file by offset and
handles rotation.

## Decision flow (owner order 2026-10-27: "a dynamic flow with decisions in real time"; no "Try a prompt")

The apex panel shows no static on/off list and no prompt box. It shows each live request moving
through its decision stations, with the decision made at each one.

`request` events (SSE) and `/api/recent` items get one more field, `decisions`: an ordered list.

```json
"decisions": [
  {"step": "task",   "label": "Task",          "verdict": "debug",                 "state": "ok"},
  {"step": "model",  "label": "Model choice",  "verdict": "deep → Claude Opus",    "state": "ok"},
  {"step": "guard",  "label": "Safety blocks", "verdict": "passed",                "state": "ok"},
  {"step": "cache",  "label": "Cache",         "verdict": "93 % from cache",       "state": "ok"},
  {"step": "send",   "label": "Send",          "verdict": "1 retry, then sent",    "state": "warn"},
  {"step": "answer", "label": "Answer",        "verdict": "ended normally · 4.1 s","state": "ok"}
]
```

| step | source | verdict examples | state rule |
|---|---|---|---|
| task | route_log row of the same session, nearest earlier ts (<= 120 s) | debug / generate / review / — | ok; "—" when no row |
| model | route_log family + telemetry model_requested -> model_resolved | "deep → Claude Opus", "escalated → Opus" | warn when escalated or requested != resolved |
| guard | telemetry guard_fired, guard_action | passed / blocked: <plain action> | error when blocked |
| cache | cache_read / (in + cache_read + cache_write); bust, bust_cause; cache_key_rewrite | "93 % from cache", "cache missed: <plain cause>" | warn when bust |
| send | connect_retries, upstream_rejected, error_cause, provider | "sent", "2 retries, then sent", "not reached" | warn on retry, error on failure |
| answer | stop_reason, refusal, ttft_ms, total time | "ended normally · 4.1 s", "stopped: length", "refused" | warn on length/refusal |

Rules: verdict strings come from fixed maps (no raw text from rows); at most 6 steps; a missing
source gives verdict "—" and state "none". The UI draws the stations as a short line inside apex:
a train moves station to station (about 150 ms each), each station shows its verdict as it
passes, a warn/error station turns amber/red and stays lit for the last 10 requests. Recent and
the lit journey reuse the same list.
