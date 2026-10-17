# RUNBOOK: remote ask (DeepSeek) and the $ offload view

`apex-router ask` sends one advisory question to a cheaper backend and books the call in
`~/.apex/offload_telemetry.jsonl`. The calling Claude role checks the answer, then books one
verdict. `apex-router offload-report` shows what the backend cost and what Claude would have
charged to write the answers that the role used.

## Commands

```bash
apex-router ask --backend deepseek --role marketing-sig --baseline sonnet "Draft three titles for ..."
echo "question" | apex-router ask --backend local --lane draft --context notes.md
#   stdout: the answer     stderr: call-id: <id>
#   exit 0 answer | 3 no answer (do the work yourself) | 4 refused, nothing sent | 2 usage error
apex-router offload-verdict <call-id> --used        # the role kept the answer
apex-router offload-verdict <call-id> --discarded   # the role did the work itself
apex-router offload-report [--log PATH] [--verdicts PATH] [--json]
```

| Option | Meaning |
|---|---|
| `--backend` | `deepseek` (remote, paid) or `local` (the Ornith tier, $0) |
| `--baseline` | the Claude tier the answer replaces: `haiku`, `sonnet`, `opus`, `fable` (default `sonnet`) |
| `--model` | `deepseek-v4-pro` (default) or `deepseek-flash`; for `local`, an ollama model id |
| `--lane` | the offload-log lane (default `ask`) |
| `--context FILE` | appended as data, not instructions (200 000 characters per file at most) |

## Key and hygiene

- WARNING: the DeepSeek API is outside the owner's control. Do not send secrets, personal data or
  private names. The client refuses the request before any network call (exit 4) if the text holds
  a secret, an e-mail address or a `remote.deepseek.deny_regex` pattern.
- Put private names in the overlay only (`~/.apex-router/models.json`), never in this repository:

  ```json
  {"remote": {"deepseek": {"deny_regex": ["(?i)private-project-name"]}}}
  ```

- If the overlay exists but cannot be read or parsed, a `deepseek` call stops with exit 2: the
  private deny patterns would be missing. A deny rule that is not a valid regex refuses every call.
- `--model` must be in `remote.deepseek.models` and have a price; else exit 2 (an unpriced call
  would book as free).
- The key comes from `DEEPSEEK_API_KEY`, else the macOS Keychain service `deepseek-api`. The tool
  never prints or logs it. To store or rotate it:
  `security add-generic-password -U -a "$USER" -s deepseek-api -w`

## What the numbers mean

| Field | Meaning |
|---|---|
| `spent` | DeepSeek cost of every call, failures that billed included |
| `claude_equiv(used)` | completion tokens of USED answers × the baseline tier's output price |
| `gross_saving(used)` | `claude_equiv(used)` − the cost of those calls |
| `waste(discarded)` | cost of answers that the role discarded |
| `pending` | cost of answered calls with no verdict yet |
| `after_remote` | `claude_equiv(used)` − `spent` |

- A timeout or a dropped connection after the request was sent may have billed with no usage
  returned. The row has `cost_known: false` and the report says "spend incomplete": `spent` is then a
  lower bound and `after_remote` an upper bound. An HTTP error response is not billed.
- Each `call_id` counts once; duplicate rows are ignored and reported.
- All $ savings are GROSS. The Claude tokens spent to read and check each answer are not measured,
  so the report says "net unknown". Do not quote `after_remote` as a net saving.
- In the token table every `ask` row has `gated=false`, so it stays MEASURE-ONLY. Verdicts live in
  a separate file (`~/.apex/offload_verdicts.jsonl`) and never enter the token aggregate.
- Prices are in `model_registry.DEFAULTS["prices"]` (USD per 1M tokens, with the date read). DeepSeek
  prices are peak rates; off-peak bills `offpeak_factor` × the price. Update them in the overlay when
  the vendor changes them.
