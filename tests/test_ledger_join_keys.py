import json

from apex_router import route_join


def write(path, rows):
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))


def test_tool_identity_precedes_nearest_session(tmp_path):
    log, conf, labels = [tmp_path / name for name in ('route.jsonl', 'conf.jsonl', 'labels.jsonl')]
    write(log, [dict(ts=100, task_type='debug', model='sonnet', escalated=False,
                     session_id='s', tool_use_id='t', agent_id='a')])
    write(conf, [dict(ts=101, task_type='debug', surface='pi', requested_tier='opus',
                      matched=True, session_id='s'),
                 dict(ts=900, task_type='debug', surface='pi', requested_tier='sonnet',
                      matched=True, session_id='s', tool_use_id='t')])
    write(labels, [dict(id='nearest', ts=101, session_id='s', label='fail'),
                   dict(id='exact', ts=900, session_id='s', tool_use_id='t', label='success')])
    result = route_join.join_labels(log, conf, label_path=labels)
    row = result['table'][0]
    assert row['requested_tier'] == 'sonnet'
    assert row['quality_label'] == 'success'
    assert row['agent_id'] == 'a' and row['tool_use_id'] == 't'
    assert result['stats']['join_rate'] == 1


def test_quality_join_rate_counts_raw_rows_and_legacy_tasks(tmp_path):
    log, conf, labels = [tmp_path / name for name in ('route.jsonl', 'conf.jsonl', 'labels.jsonl')]
    write(log, [dict(ts=100, task_type='debug', model='sonnet', escalated=False, session_id='s'),
                dict(ts=100, task_type='debug', model='sonnet', escalated=False, session_id='other'),
                dict(ts=100, task_type='debug', model='sonnet', escalated=False)])
    write(conf, [])
    write(labels, [dict(id='legacy', session='s', label='success'),
                   dict(id='unknown', session='other', label='unknown')])
    write(tmp_path / 'tasks.jsonl', [dict(id='legacy', session='s', ts='1970-01-01T00:01:41Z')])
    result = route_join.join_labels(log, conf, label_path=labels)
    assert result['stats']['label_matches'] == 1
    assert result['stats']['join_rate'] == 1 / 3


def test_session_fallback_never_matches_another_known_session(tmp_path):
    log, conf, labels = [tmp_path / name for name in ('route.jsonl', 'conf.jsonl', 'labels.jsonl')]
    write(log, [dict(ts=100, task_type='debug', model='sonnet', escalated=False, session_id='s')])
    write(conf, [dict(ts=101, task_type='debug', surface='pi', requested_tier='sonnet', matched=True, session_id='wrong')])
    write(labels, [dict(ts=101, session_id='wrong', label='fail')])
    result = route_join.join_labels(log, conf, label_path=labels)
    assert result['table'] == []
    assert result['stats']['join_rate'] == 0


def test_conflicting_tool_and_agent_labels_do_not_match():
    route = dict(ts=100, session_id='s', tool_use_id='a', agent_id='worker-a')
    labels = [dict(ts=101, session_id='s', tool_use_id='b', label='success'),
              dict(ts=101, session_id='s', agent_id='worker-b', label='success')]
    assert route_join._match_quality(route, labels) is None


def test_quality_label_has_one_nearest_dispatch_claim(tmp_path):
    log, conf, labels = [tmp_path / name for name in ('route.jsonl', 'conf.jsonl', 'labels.jsonl')]
    base = dict(task_type='debug', model='sonnet', escalated=False, label_pending=True,
                outcome='ok', session_id='s')
    write(log, [dict(base, ts=100, tool_use_id='far'),
                dict(base, ts=110, tool_use_id='near')])
    write(conf, [])
    write(labels, [dict(id='one', ts=111, session_id='s', label='success')])
    result = route_join.join_labels(log, conf, label_path=labels)
    claimed = [r for r in result['table'] if 'quality_label' in r]
    assert [r['tool_use_id'] for r in claimed] == ['near']
    assert result['stats']['label_matches'] == 1
    assert result['stats']['join_rate'] == 0.5


def test_quality_label_tool_requires_same_route_tool(tmp_path):
    log, conf, labels = [tmp_path / name for name in ('route.jsonl', 'conf.jsonl', 'labels.jsonl')]
    write(log, [dict(ts=100, task_type='debug', model='sonnet', escalated=False, session_id='s')])
    write(conf, [])
    write(labels, [dict(id='other', ts=101, session_id='s', tool_use_id='other', label='success')])
    result = route_join.join_labels(log, conf, label_path=labels)
    assert result['stats']['label_matches'] == 0
    assert result['stats']['join_rate'] == 0


def test_quality_exact_claim_precedes_closer_legacy_label():
    routes = [dict(ts=100, session_id='s'), dict(ts=110, session_id='s', tool_use_id='t')]
    labels = [dict(ts=100, session_id='s', tool_use_id='t', label='success'),
              dict(ts=110, session_id='s', label='fail')]
    claims = route_join._claim_quality(routes, labels)
    assert claims[1] is labels[0]
    assert claims[0] is labels[1]


def test_exact_partner_is_reserved_before_weak_routes(tmp_path):
    log, conf = tmp_path / 'route.jsonl', tmp_path / 'conf.jsonl'
    base = dict(ts=100, task_type='debug', model='sonnet', escalated=False, session_id='s')
    write(log, [base, dict(base, tool_use_id='t')])
    write(conf, [dict(ts=101, task_type='debug', surface='pi', requested_tier='sonnet',
                      matched=True, session_id='s', tool_use_id='t')])
    table = route_join.join_labels(log, conf)['table']
    assert len(table) == 1 and table[0]['tool_use_id'] == 't'
