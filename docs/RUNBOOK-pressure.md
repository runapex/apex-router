# Runbook: `apex-router pressure`

A pre-dispatch signal: "is upstream pushing back right now?" Consult it before fanning out
parallel heavy subagents. Module: `src/apex_router/pressure.py`.

## What it reads

The last `--window` minutes (default 15) of the measuring proxy's telemetry, read backwards from
the end of the file. The path is resolved by `apex_router.telemetry_path`: `$APEX_TELEMETRY`
(explicit file) wins, then `$APEX_HOME/telemetry.jsonl`, then `~/.apex/telemetry.jsonl`;
`--telemetry PATH` overrides all three. It does not parse the whole file, and a half-written
trailing line is ignored. Heartbeat rows (`ev: hb`) are skipped.

### Stop rule (why one old row cannot hide a 429 storm)

Rows are appended when a request **finishes**, but `ts` is the request **start**. A 26-minute
opus stream that finished a second ago is the newest line in the file and carries a timestamp
26 minutes old. Let `stop_ts = window_start - 10 min` (the ordering slack). The backward scan:

- skips (does not count) any row older than `stop_ts`, and keeps reading;
- stops after **200 consecutive** rows older than `stop_ts` (any newer row resets the run);
- stops at once on a row older than `stop_ts` by **more than 6 hours** (no request lasts that long).

So a single long request at the tail never ends the scan, and the read stays bounded on a large file.

It reports It reports
per model family (haiku/sonnet/opus/fable/other, from `model_requested`) and overall:

- **429s**: `error_cause == "http_429"`.
- **transport errors** (`xport`): non-HTTP causes (`SSLError`, `ReadError`, ...) plus any
  `http_5xx`, **plus** any row with `connect_retries > 0`. A row counts once even if it is both.
  4xx client errors (400/401/404/413) are not counted as pressure.
- **retried**: rows where the proxy retried the connect before the first byte
  (`connect_retries > 0`). The proxy retries transient `SSLError`/`ReadError`/`RemoteProtocolError`/
  `ConnectError`, so a retried request that then succeeded has `error_cause: null`. Without this
  column the flakiness would not show up. These rows are already included in `xport`.
- **local**: `PoolTimeout` rows. The proxy's own connection pool was exhausted, which is local
  back-pressure and not an upstream fault. They are shown separately and are **not** counted in
  the transport rate.
- **p50 ttft_ms**: successful rows only.
- **agents2m**: distinct `agent_id`s seen in the last 2 minutes, a rough measure of in-flight concurrency.
- the newest `retry-after` / `anthropic-ratelimit-*` values from `error_detail.headers`, if any, and
  a count of `upstream_rejected: true` rows when the proxy writes that field.

The latest readout is also written atomically to `~/.apex-router/pressure.json` (`--state PATH`
to relocate, `--no-write` to skip), so a hook can read it without scanning the telemetry again.

## Levels

| Level | Condition (overall) |
|---|---|
| GREEN | 429 rate < 2% **and** transport < 3% |
| AMBER | 429 rate 2–10% **or** transport 3–10% |
| RED | either rate > 10%, **or** a `retry-after` header seen in the last 2 minutes |
| UNKNOWN | the telemetry file is missing or unreadable, or the readout itself raised |

**Sample floor.** With fewer than 10 requests in the window a rate is noise, so the level is
reported as `GREEN (insufficient sample: n<10)` and `--check` exits 0. The rates are still
printed, and JSON carries `"insufficient_sample": true`. The one exception is a fresh
`retry-after` (last 2 minutes): that still forces RED at any sample size, because it is the
provider telling us to back off, not a rate. The floor also applies per family.

Env overrides (fractions): `APEX_PRESSURE_AMBER` sets both amber floors, and `APEX_PRESSURE_RED`
sets the red ceiling. A value that does not parse falls back to the default. Each family also
gets its own level in the table, but only the overall level drives `--check`.

## Recommendations (quoted verbatim by the model-routing skill)

| Level | Recommendation |
|---|---|
| GREEN | dispatch as planned |
| AMBER | shed mechanical and exploration subagents one tier down (opus→sonnet, sonnet→haiku); cap parallel heavy agents at 2 |
| RED | no new heavy fan-out; serialize; mechanical work to haiku or the local tier; wait `<N>` s before retrying heavy |
| UNKNOWN | no pressure signal (telemetry unreadable); dispatch conservatively, as for AMBER |

`<N>` is the parsed `retry-after` if one arrived in the last 2 minutes, otherwise 60. A
`retry-after` that is not finite (`inf`, `nan`), overflows, or exceeds one day is treated as
unparseable.

## The `--check` contract

| Exit | Meaning |
|---|---|
| 0 | GREEN (including `GREEN (insufficient sample: n<10)`) |
| 1 | AMBER |
| 2 | RED |
| 3 | UNKNOWN: telemetry missing or unreadable, or an unexpected exception (prints `pressure: UNKNOWN (<ExcName>: msg)`) |
| 4 | usage error (bad flag or value). argparse's usual 2 would collide with RED. |

```sh
apex-router pressure --check >/dev/null; case $? in
  0) echo dispatch ;;   # GREEN
  1) echo shed ;;       # AMBER
  2) echo hold ;;       # RED
  3) echo shed ;;       # UNKNOWN: no signal, so treat it like AMBER and do not assume GREEN
  *) echo "pressure: bad invocation" >&2 ;;
esac
```

Without `--check`, a readout exits 0 even when it is UNKNOWN. An unexpected exception or a usage
error still exits 3 or 4. Use `--json` for machine output; it has the same fields as
`pressure.json`. An empty but readable telemetry file gives `GREEN (insufficient sample)` with
0 requests. A missing file gives UNKNOWN.

## Local tier: is thinking really off?

Every local lane sends `reasoning_effort: "none"`. If the serving stack ignores it, every call is
thinking-ON (measured: 0/3 complete, budget burned in `<think>`) and scripts hang up to 900 s.
Check once per box, and after any ollama upgrade:

    .venv/bin/python -m apex_router.ornith.ornith_client --probe-thinking

Exit codes: 0 = thinking off (verified); 1 = evidence thinking is ON (reasoning present, an
unterminated inline `<think>`, or an empty answer because reasoning ate the budget); 2 =
inconclusive (server busy, down, in maintenance, or another error — re-run, don't conclude).
The probe waits at most 10 s for the inference lock.

Related knobs: `ORNITH_LOCK_TIMEOUT_SECS` (default 120) bounds the wait on the inference lock;
`ORNITH_SOCKET_TIMEOUT_SECS` (default 900) bounds one inference.
