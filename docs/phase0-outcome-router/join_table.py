#!/usr/bin/env python3
"""Phase-0 training-table join for LLM routing."""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.expanduser('~/src/apex-router/src'))
from apex_router.stats import wilson_ci

ROUTE_LOG = Path(os.path.expanduser('~/.apex-router/route_log.jsonl'))
CONFORMANCE = Path(os.path.expanduser('~/.apex-router/conformance.jsonl'))
OUT_DIR = Path('.')
LABELED_TABLE = OUT_DIR / 'labeled_table.jsonl'
REPORT = OUT_DIR / 'join_report.md'

JOIN_WINDOW_S = 300.0
SUSPICIOUS_RESOLVED = {'S', 'O'}
BATCH_CLUSTER_S = 1.0  # flag groups of rows with timestamps this close as a batch/cluster defect


def parse_jsonl(path: Path, required_keys: Tuple[str, ...]) -> Tuple[List[Dict[str, Any]], int]:
    """Stream a JSONL file, returning valid rows and a skipped-line count."""
    rows: List[Dict[str, Any]] = []
    skipped = 0
    with path.open('r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(obj, dict):
                skipped += 1
                continue
            if any(k not in obj for k in required_keys):
                skipped += 1
                continue
            rows.append(obj)
    return rows, skipped


def safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f'not a bool: {value!r}')


def build_report(
    route_rows: List[Dict[str, Any]],
    route_skipped: int,
    conf_rows: List[Dict[str, Any]],
    conf_skipped: int,
    joined_rows: List[Dict[str, Any]],
    unjoinable_ts_null: int,
    no_partner: int,
) -> str:
    lines: List[str] = []
    lines.append('# Phase-0 Join Report')
    lines.append('')

    # Row/skip counts
    lines.append('## Row/skip counts')
    lines.append('')
    lines.append(f'- route_log.jsonl: parsed {len(route_rows)} rows, skipped {route_skipped}')
    lines.append(f'- conformance.jsonl: parsed {len(conf_rows)} rows, skipped {conf_skipped}')
    lines.append('')

    # Per-task-type escalation stats helpers
    def task_stats(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        by_task: Dict[str, List[bool]] = defaultdict(list)
        for r in rows:
            by_task[r['task_type']].append(safe_bool(r['escalated']))
        out: Dict[str, Dict[str, Any]] = {}
        for task in sorted(by_task):
            escs = by_task[task]
            n = len(escs)
            k = sum(escs)
            rate = k / n if n else 0.0
            lo, hi = wilson_ci(k, n) if n > 0 else (0.0, 0.0)
            out[task] = {'n': n, 'escalated': k, 'rate': rate, 'lo': lo, 'hi': hi}
        return out

    def fmt_rate(r: float) -> str:
        return f'{r:.1%}'

    def stats_table(stats: Dict[str, Dict[str, Any]]) -> List[str]:
        out = ['| task_type | n | escalated | rate | Wilson 95% CI |',
               '|-----------|---|-----------|------|---------------|']
        for task in sorted(stats):
            s = stats[task]
            out.append(
                f"| {task} | {s['n']} | {s['escalated']} | {fmt_rate(s['rate'])} | "
                f"[{fmt_rate(s['lo'])}, {fmt_rate(s['hi'])}] |"
            )
        return out

    # All route_log rows
    lines.append('## Per-task-type escalation rates (all route_log rows)')
    lines.append('')
    lines.extend(stats_table(task_stats(route_rows)))
    lines.append('')

    # Excluding ts=null
    ts_ok_route = [r for r in route_rows if r.get('ts') is not None]
    lines.append('## Per-task-type escalation rates (excluding ts=null rows)')
    lines.append('')
    lines.extend(stats_table(task_stats(ts_ok_route)))
    lines.append('')

    # Join match stats
    lines.append('## Join match statistics')
    lines.append('')
    lines.append(f'- route_log rows joined to conformance: {len(joined_rows)}')
    lines.append(f'- route_log rows with ts=null (unjoinable): {unjoinable_ts_null}')
    lines.append(f'- route_log rows with no conformance partner within 300s: {no_partner}')
    lines.append('')

    # Data quality
    lines.append('## Data quality defects')
    lines.append('')

    # null ts
    null_ts_count = sum(1 for r in route_rows if r.get('ts') is None)
    if null_ts_count:
        lines.append(f'- **Null timestamps**: {null_ts_count} route_log rows have ts=null and cannot be time-joined.')
    else:
        lines.append('- No null timestamps found.')

    # suspicious resolved_model
    suspicious = [
        (i, r) for i, r in enumerate(conf_rows)
        if r.get('resolved_model') in SUSPICIOUS_RESOLVED
    ]
    if suspicious:
        lines.append(f'- **Suspicious resolved_model values**: {len(suspicious)} conformance rows have resolved_model in {sorted(SUSPICIOUS_RESOLVED)}:')
        for i, r in suspicious:
            lines.append(
                f"  - row {i}: ts={r.get('ts')}, task_type={r.get('task_type')!r}, "
                f"resolved_model={r.get('resolved_model')!r}, surface={r.get('surface')!r}"
            )
    else:
        lines.append('- No suspicious resolved_model values found.')

    # batch-identical / tight timestamp clusters
    ts_groups: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for r in route_rows:
        if r.get('ts') is not None:
            ts_groups[r['ts']].append(r)
    for r in conf_rows:
        if r.get('ts') is not None:
            ts_groups[r['ts']].append(r)
    exact_dups = {ts: rows for ts, rows in ts_groups.items() if len(rows) > 1}
    if exact_dups:
        lines.append(f'- **Batch-identical timestamps (exact)**: {len(exact_dups)} distinct timestamp(s) appear on multiple rows:')
        for ts in sorted(exact_dups):
            rows = exact_dups[ts]
            tasks = [r.get('task_type') for r in rows]
            surfaces = [r.get('surface', 'n/a') for r in rows]
            lines.append(
                f"  - ts={ts}: {len(rows)} rows, task_types={tasks}, surfaces={surfaces}"
            )
    else:
        lines.append('- No exactly batch-identical timestamps found.')

    # tight clusters (rows within BATCH_CLUSTER_S of each other)
    timed_rows = [
        (r.get('ts'), r)
        for r in route_rows + conf_rows
        if isinstance(r.get('ts'), (int, float))
    ]
    timed_rows.sort(key=lambda x: x[0])
    clusters: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for ts, r in timed_rows:
        if not current:
            current = [r]
        else:
            last_ts = current[-1].get('ts')
            if last_ts is not None and abs(ts - last_ts) <= BATCH_CLUSTER_S:
                current.append(r)
            else:
                if len(current) > 1:
                    clusters.append(current)
                current = [r]
    if len(current) > 1:
        clusters.append(current)
    if clusters:
        total_clustered = sum(len(c) for c in clusters)
        lines.append(
            f'- **Tight timestamp clusters (<= {BATCH_CLUSTER_S}s)**: {len(clusters)} cluster(s) '
            f'containing {total_clustered} rows:')
        for c in clusters:
            start = min(r.get('ts') for r in c)
            end = max(r.get('ts') for r in c)
            tasks = [r.get('task_type') for r in c]
            surfaces = [r.get('surface', 'n/a') for r in c]
            lines.append(
                f"  - {len(c)} rows spanning {end - start:.6f}s around ts={start}: "
                f"task_types={tasks}, surfaces={surfaces}"
            )
    else:
        lines.append('- No tight timestamp clusters found.')

    # Honesty-invariant exclusions
    excluded = [
        r for r in conf_rows
        if r.get('surface') == 'agent' and r.get('matched') is None
    ]
    if excluded:
        lines.append(f'- **Intent-only conformance rows excluded**: {len(excluded)} rows with surface=agent and matched=null were excluded from denominators and the labeled table.')

    lines.append('')
    return '\n'.join(lines)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    route_rows, route_skipped = parse_jsonl(
        ROUTE_LOG,
        ('ts', 'task_type', 'model', 'passed', 'escalated', 'note'),
    )
    conf_rows, conf_skipped = parse_jsonl(
        CONFORMANCE,
        ('ts', 'surface', 'task_type', 'requested_tier', 'resolved_model', 'matched', 'note'),
    )

    # Apply honesty invariant: exclude agent+matched=null from any rate denominator and labeled table.
    usable_conf = [
        r for r in conf_rows
        if not (r.get('surface') == 'agent' and r.get('matched') is None)
    ]

    # Build index of usable conformance rows by task_type
    conf_by_task: Dict[str, List[Tuple[int, Dict[str, Any]]]] = defaultdict(list)
    for idx, r in enumerate(usable_conf):
        conf_by_task[r['task_type']].append((idx, r))

    used_conf_indices = set()
    joined_rows: List[Dict[str, Any]] = []
    unjoinable_ts_null = 0
    no_partner = 0

    for r in route_rows:
        ts = r.get('ts')
        if ts is None:
            unjoinable_ts_null += 1
            continue

        candidates = conf_by_task.get(r['task_type'], [])
        best_idx = -1
        best_row: Dict[str, Any] = {}
        best_diff = float('inf')
        for idx, c in candidates:
            if idx in used_conf_indices:
                continue
            c_ts = c.get('ts')
            if c_ts is None:
                continue
            diff = abs(ts - c_ts)
            if diff <= JOIN_WINDOW_S and diff < best_diff:
                best_diff = diff
                best_idx = idx
                best_row = c

        if best_idx < 0:
            no_partner += 1
            continue

        used_conf_indices.add(best_idx)
        label = 'hard' if safe_bool(r['escalated']) else 'easy'
        joined = {
            'ts': ts,
            'task_type': r['task_type'],
            'model': r['model'],
            'escalated': safe_bool(r['escalated']),
            'label': label,
            'surface': best_row.get('surface'),
            'requested_tier': best_row.get('requested_tier'),
            'resolved_model': best_row.get('resolved_model'),
            'matched': best_row.get('matched'),
        }
        joined_rows.append(joined)

    # Write labeled table
    with LABELED_TABLE.open('w', encoding='utf-8') as fh:
        for row in joined_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + '\n')

    # Build and write report
    report = build_report(
        route_rows, route_skipped,
        conf_rows, conf_skipped,
        joined_rows, unjoinable_ts_null, no_partner,
    )
    REPORT.write_text(report, encoding='utf-8')
    print(report)


if __name__ == '__main__':
    main()
