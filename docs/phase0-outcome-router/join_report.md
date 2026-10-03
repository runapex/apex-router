# Phase-0 Join Report

## Row/skip counts

- route_log.jsonl: parsed 38 rows, skipped 0
- conformance.jsonl: parsed 7 rows, skipped 0

## Per-task-type escalation rates (all route_log rows)

| task_type | n | escalated | rate | Wilson 95% CI |
|-----------|---|-----------|------|---------------|
| adhoc | 1 | 1 | 100.0% | [20.7%, 100.0%] |
| debug | 5 | 1 | 20.0% | [3.6%, 62.4%] |
| e2e_probe | 1 | 0 | 0.0% | [0.0%, 79.3%] |
| extract | 30 | 6 | 20.0% | [9.5%, 37.3%] |
| generate | 1 | 0 | 0.0% | [0.0%, 79.3%] |

## Per-task-type escalation rates (excluding ts=null rows)

| task_type | n | escalated | rate | Wilson 95% CI |
|-----------|---|-----------|------|---------------|
| debug | 5 | 1 | 20.0% | [3.6%, 62.4%] |
| generate | 1 | 0 | 0.0% | [0.0%, 79.3%] |

## Join match statistics

- route_log rows joined to conformance: 0
- route_log rows with ts=null (unjoinable): 32
- route_log rows with no conformance partner within 300s: 6

## Data quality defects

- **Null timestamps**: 32 route_log rows have ts=null and cannot be time-joined.
- **Suspicious resolved_model values**: 7 conformance rows have resolved_model in ['O', 'S']:
  - row 0: ts=1787621882.008242, task_type='explore', resolved_model='S', surface='resolve'
  - row 1: ts=1787621882.008835, task_type='refactor', resolved_model='S', surface='resolve'
  - row 2: ts=1787621882.009312, task_type='debug', resolved_model='O', surface='resolve'
  - row 3: ts=1787621882.009888, task_type='debug', resolved_model='O', surface='resolve'
  - row 4: ts=1787621882.010985, task_type='refactor', resolved_model='S', surface='resolve'
  - row 5: ts=1787621882.013029, task_type='debug', resolved_model='O', surface='resolve'
  - row 6: ts=1787621882.013561, task_type='debug', resolved_model='O', surface='resolve'
- No exactly batch-identical timestamps found.
- **Tight timestamp clusters (<= 1.0s)**: 1 cluster(s) containing 7 rows:
  - 7 rows spanning 0.005319s around ts=1787621882.008242: task_types=['explore', 'refactor', 'debug', 'debug', 'refactor', 'debug', 'debug'], surfaces=['resolve', 'resolve', 'resolve', 'resolve', 'resolve', 'resolve', 'resolve']
