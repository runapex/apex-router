# apex flow studio

A local web page that shows the apex-router request flow from source to sink, like a recipe canvas.
Start it from the menu bar ("Open flow studio ↗") or with:

```bash
apex-router studio            # opens the browser; --no-open prints the URL only
apex-router studio --idle-exit 1800 --port 8790
```

## What you see

| Lane | Steps |
|---|---|
| Sources | pi, claude, codex |
| Client steps | `>>family` cue, auto resolve, STE prompt, tool guards, compaction, sub-agents |
| Proxy | detect client, shadow, codex cache key, auth injection, upstream + retry, response + usage |
| Upstreams | foundry / anthropic, openai, local ollama |
| Sinks | telemetry, route log, pressure, widget history, xval bandit |

- Each card shows one live number for the window (15m / 1h / 24h) and a status dot.
- Edge width follows request volume. "Branches" (key `b`) shows fallback, retry, 502, offload and
  side sinks. Selecting a card shows the branches that touch it.
- Click a card (or arrow keys + Enter) to open the inspector: what the step does, its settings
  (now / default / effect), live numbers, the code file:line and the files it writes.

## Test a prompt

Type a prompt (`/` focuses the box), for example `>>auto fix the failing test`. The studio runs the
resolver as a dry run and lights the path: source → cue → family → model and effort → upstream,
with the reason (`source`, task type, confidence). It makes no model call, sends nothing to the
proxy and writes no conformance row.

## Edit, review, apply

| Effect class | Example | Apply does |
|---|---|---|
| live | `>>deep` effort in `~/.apex-router/models.json`, `pi-routes.json` | Writes the file: timestamped backup in `~/.apex-router/studio/backups/`, atomic write, JSON check. Run `/reload` in pi. "Rollback last apply" restores the backup. |
| restart | `APEX_CONNECT_RETRIES` and other proxy env | "Apply + restart when idle": the studio waits until the proxy reports 0 requests in progress and no request for the idle time (default 120 s, min 60 s, max wait 30 min). Then it backs up and writes the plist (`plutil -lint`) and drains the proxy: `kill -USR1 <pid>` (the pid must match `launchctl print`) makes it answer new requests with 503 + `Retry-After: 5`, and the studio waits until `inflight` is 0 (max 30 s, else `kill -USR2` ends the drain and the plist is restored). Then it runs `launchctl bootout` + `bootstrap`. If a new process does not answer `/healthz` within 25 s, it restores the old plist and restarts again. The commands stay visible for a manual run. |
| locked | policy path, upstream URLs, auth injection | Read-only. The server rejects a change, also from a hand-edited draft. |

Edits go to a draft (`~/.apex-router/studio/draft.json`) first. "Review" shows a diff per file.

## Restart gate

The proxy counts proxied requests in progress in a small ASGI wrapper (`ActivityCounter`): a
streamed answer counts until its last byte. `/stats` → `activity` = `inflight`, `idle_s`,
`started_s_ago`, `pid`, `draining`. A drain that nobody ends expires after 60 s. A proxy that does not report `activity` (older code) is never restarted by the
studio: restart it once by hand when it is quiet. A restart needs `confirm: "restart <label>"` in the
request body, and only one restart per proxy runs at a time (lock file).

## Safety rules

- The server binds 127.0.0.1 only and picks a free port unless `--port` is given.
- Every call needs the random token from the URL. Writes need it in the `X-Studio-Token` header.
- The Host and Origin headers must be the local address (no DNS rebinding).
- JSON bodies only, 64 KiB max. CSP `default-src 'none'` with a nonce per page; no external files.
- A setting whose name contains KEY, TOKEN, SECRET or PASSWORD shows only "set" / "not set".
- No request log: URLs carry the token.
