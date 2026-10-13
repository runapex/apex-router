# Compaction counterfactual

Claude Code rows (client contains 'claude'), input = tokens_in+cache_read+cache_write > 0. Generated from telemetry.jsonl; total qualifying rows 29567.


## Window: last 7 days (n=7125)

### Per model

| model | n | mean | median | p90 | max | share req >=400k | share tokens from >=400k |
|---|---|---|---|---|---|---|---|
| claude-opus-5-5 | 5215 | 197,781 | 150,964 | 409,471 | 841,451 | 10.6% | 30.6% |
| claude-sonnet-5-5 | 1272 | 142,605 | 122,748 | 278,574 | 422,565 | 1.5% | 4.3% |
| claude-fable-5-1 | 595 | 244,340 | 156,346 | 599,656 | 765,790 | 22.9% | 54.3% |
| claude-haiku-4-5-20251001 | 43 | 53,640 | 54,792 | 69,958 | 70,193 | 0.0% | 0.0% |
| ALL | 7125 | 190,949 | 143,593 | 398,737 | 841,451 | 9.9% | 29.5% |

### Counterfactual cap (share of input tokens removed)

| scope | 250k | 400k | 500k | 600k |
|---|---|---|---|---|
| overall | 20.1% | 8.7% | 4.6% | 2.0% |
| claude-opus-5-5 | 20.5% | 9.1% | 4.9% | 2.2% |

### Sessions

- sessions: 8
- sessions with any request >=400k: 4 (50.0%)
- session max input p50: 415,548, p90: 841,451

## Window: last 30 days (n=28837)

### Per model

| model | n | mean | median | p90 | max | share req >=400k | share tokens from >=400k |
|---|---|---|---|---|---|---|---|
| claude-opus-5-5 | 20207 | 172,486 | 131,815 | 351,229 | 841,451 | 7.7% | 24.5% |
| claude-sonnet-5-5 | 4098 | 91,215 | 68,755 | 166,513 | 422,565 | 0.6% | 2.5% |
| claude-fable-5-1 | 1972 | 241,452 | 158,880 | 613,131 | 964,781 | 18.8% | 49.8% |
| claude-opus-5 | 1178 | 198,705 | 141,148 | 439,045 | 682,074 | 13.4% | 32.8% |
| claude-sonnet-5 | 1076 | 82,566 | 77,817 | 120,033 | 168,688 | 0.0% | 0.0% |
| claude-haiku-4-5-20251001 | 304 | 45,438 | 46,094 | 66,077 | 77,135 | 0.0% | 0.0% |
| claude-opus-4-8 | 2 | 115,148 | 115,148 | 115,519 | 115,519 | 0.0% | 0.0% |
| ALL | 28837 | 162,025 | 117,521 | 335,647 | 964,781 | 7.3% | 25.2% |

### Counterfactual cap (share of input tokens removed)

| scope | 250k | 400k | 500k | 600k |
|---|---|---|---|---|
| overall | 17.4% | 7.1% | 3.6% | 1.6% |
| claude-opus-5-5 | 16.9% | 6.6% | 3.2% | 1.2% |
| claude-opus-5 | 22.3% | 5.8% | 1.4% | 0.3% |
| claude-opus-4-8 | 0.0% | 0.0% | 0.0% | 0.0% |

### Sessions

- sessions: 59
- sessions with any request >=400k: 15 (25.4%)
- session max input p50: 87,014, p90: 725,740

## CLAUDE_AUTOCOMPACT_PCT_OVERRIDE trigger size

| pct | 1,000,000 window | 200,000 window |
|---|---|---|
| 40 | 400,000 | 80,000 |
| 50 | 500,000 | 100,000 |
| 60 | 600,000 | 120,000 |
