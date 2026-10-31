# Cursor to apex-router

## How traffic reaches the proxy

| Client | Path |
| --- | --- |
| Claude Code | `ANTHROPIC_BASE_URL=http://127.0.0.1:8788` |
| pi | provider `baseUrl` to `:8788` |
| Cursor IDE BYOK | Override OpenAI Base URL = `http://127.0.0.1:8788/v1` and enable OpenAI API Key |

## Limit

Cursor subscription models, including catalog Grok, call `api2.cursor.sh`. They do not reach apex-router. An env var cannot redirect them.

## Grok through apex

1. Create an xAI API key at https://console.x.ai/.
2. In Cursor Settings, open Models.
3. Paste the key into OpenAI API Key and enable it.
4. Set Override OpenAI Base URL to `http://127.0.0.1:8788/v1`.
5. Add a custom model whose id starts with `grok-` (for example `grok-4`). Do not use the built-in Cursor Grok catalog entry.
6. Chat with that custom model.

The proxy sends a grok model id on the OpenAI wire to `https://api.x.ai`. Other OpenAI-wire models stay on `https://api.openai.com`. Codex is unchanged.

Set `APEX_XAI_UPSTREAM` only when the xAI host is not `https://api.x.ai`. Restart `serve` after that change.

Do not set `APEX_OPENAI_UPSTREAM` to `https://api.x.ai`. That sends Codex to xAI.

Studio shows client `cursor` and endpoint `xai`. The model family is Grok.

## Shell env

`~/.apex-router/cursor.env` plus `launchctl setenv` for `ANTHROPIC_BASE_URL` and `CURSOR_LOCAL_AGENT_BASE_URL` serve Claude children and `agent` agent-cli-local. They do not serve subscription IDE Grok.
