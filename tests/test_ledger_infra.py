import json

import pytest

from apex_router import quality, route_join, route_log


def write_rows(path, rows):
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))


@pytest.mark.parametrize('note', ['turn: provider error', 'auto: provider error/empty'])
def test_legacy_infra_is_excluded_without_rewriting(tmp_path, note):
    p = tmp_path / 'route_log.jsonl'
    rows = [dict(ts=1, task_type='debug', model='sonnet', escalated=True, note=note),
            dict(ts=2, task_type='debug', model='sonnet', escalated=False)]
    write_rows(p, rows)
    before = p.read_bytes()
    cell = route_log.read_rates(log_path=p)['debug']
    assert (cell['n'], cell['escalated'], cell['infra']) == (1, 0, 1)
    assert cell['by_tier']['sonnet']['infra'] == 1
    joined = route_join.cell_rates(rows)['debug']
    assert (joined['n'], joined['escalated'], joined['infra']) == (1, 0, 1)
    assert quality.classification(0, tmp_path)['infra'] == 1
    assert quality.classification(0, tmp_path)['escalated'] == 0
    assert p.read_bytes() == before


def test_cause_validation_and_explicit_capability(tmp_path):
    p = tmp_path / 'route_log.jsonl'
    assert not route_log.log_outcome('debug', 'sonnet', 'escalated', cause='bad', log_path=p)
    assert not p.exists()
    assert route_log.log_outcome('debug', 'sonnet', 'escalated', cause='capability',
                                 note='turn: provider error', log_path=p)
    assert route_log.read_rates(log_path=p)['debug']['escalated'] == 1
    assert json.loads(p.read_text())['cause'] == 'capability'


def test_all_infra_and_gpt_counts(tmp_path):
    p = tmp_path / 'route_log.jsonl'
    for model in ('sonnet', 'gpt-5.6-terra'):
        assert route_log.log_outcome('debug', model, 'escalated', cause='infra', log_path=p)
    cell = route_log.read_rates(log_path=p)['debug']
    assert (cell['n'], cell['escalated'], cell['rate'], cell['infra']) == (0, 0, 0, 2)
    assert cell['by_tier']['gpt-terra']['infra'] == 1


def test_join_preserves_infra(tmp_path):
    log = tmp_path / 'route_log.jsonl'
    conf = tmp_path / 'conformance.jsonl'
    write_rows(log, [dict(ts=1, task_type='debug', model='sonnet', escalated=True, note='turn: provider error')])
    write_rows(conf, [dict(ts=1, task_type='debug', surface='pi', requested_tier='sonnet', matched=True)])
    table = route_join.join_labels(log, conf)['table']
    assert table[0]['cause'] == 'infra'
    assert route_join.cell_rates(table)['debug']['infra'] == 1
