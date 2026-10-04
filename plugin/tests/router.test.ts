import { describe, expect, test } from 'claude-code/testing'

import type { Cell } from '../types/index.d.ts'
import { cellKey } from '../hooks/evidence.ts'
import { newRuntime, optionsOf } from '../hooks/runtime.ts'
import { decideSpawn, escalationTierOf, finishOf, normalizeDescription, routeRow, type SpawnContext } from '../hooks/router.ts'
import { complete, SESSION, spawnInput } from './fixtures/inputs.ts'
import { BACKEND, worldOf, type World } from './fixtures/world.ts'

const READY: Cell = { n: 35, pass: 35, state: 'READY', winN: 5, winPass: 5, below: 0, above: 0, cusum: 0, p0: 1 }
const ALMOST: Cell = { n: 34, pass: 34, state: 'WARMING', winN: 4, winPass: 4, below: 0, above: 0, cusum: 0, p0: null }
const ctx = (over: Partial<SpawnContext> = {}): SpawnContext => ({
  level: 'GREEN', cells: { [cellKey('explore', 'GREEN', 'sonnet')]: READY }, stats: {}, admitted: true, ...over,
})
const routeRows = (world: World): Record<string, unknown>[] => (world.appended.get(`${BACKEND}/route_log.jsonl`) ?? []).map(l => JSON.parse(l))
const END = { reason: 'prompt_input_exit', sessionId: 'sess-0001' } as never
const workflowCall = (id = 'toolu_wf') => ({ tool: 'Workflow', tool_use_id: id, script: "agent('x', { model: 'haiku' })" }) as never

describe('router: decisions', () => {
  test('advice is text only: a READY cell is advised, nothing is applied', () => {
    const d = decideSpawn(spawnInput(), ctx())
    expect(d.taskType).toBe('explore')
    expect(d.requested).toBe('inherit')
    expect(d.requestedTier).toBe('opus')
    expect(d.advice?.tier).toBe('sonnet')
    expect(d.hardLimit).toBeNull()
    expect('apply' in d).toBe(false)
  })

  test('hard limits are checked before advice: explicit model keeps text advice; fork, Fable, unknown tier get none', () => {
    const cells = { [cellKey('explore', 'GREEN', 'sonnet')]: READY, [cellKey('explore', 'GREEN', 'opus')]: READY }
    expect(decideSpawn(spawnInput({ model: 'opus' }), ctx({ cells }))).toMatchObject({ hardLimit: 'explicit model', requested: 'opus', advice: { tier: 'sonnet' } })
    expect(decideSpawn(spawnInput({ fork: true }), ctx({ cells }))).toMatchObject({ hardLimit: 'a fork inherits the parent model', advice: null })
    expect(decideSpawn(spawnInput({ parentModel: 'claude-fable-5' }), ctx({ cells }))).toMatchObject({ hardLimit: 'Fable is never downshifted silently', advice: null })
    expect(decideSpawn(spawnInput({ model: 'fable' }), ctx({ cells }))).toMatchObject({ hardLimit: 'explicit model', advice: null })
    expect(decideSpawn(spawnInput({ parentModel: 'gpt-5' }), ctx({ cells }))).toMatchObject({ hardLimit: 'unknown tier', advice: null })
  })

  test('RED or a refused admission advises serializing heavy fan-out', () => {
    expect(decideSpawn(spawnInput(), ctx({ level: 'RED' })).serialize).toBe(true)
    expect(decideSpawn(spawnInput(), ctx({ admitted: false })).serialize).toBe(true)
    expect(decideSpawn(spawnInput({ parentModel: 'claude-sonnet-5-5' }), ctx({ level: 'RED' })).serialize).toBe(false)
  })

  test('route rows keep the route_join schema: optional fields are strings, never null', () => {
    const rt = newRuntime(optionsOf({}))
    rt.sessionId = 'sess-1'
    const e = spawnInput()
    const row = routeRow(e, decideSpawn(e, ctx()), 'claude-opus-5-5', 'agent-1', rt, 1791028800000)
    expect(row).toEqual({
      ts: 1791028800, task_type: 'explore', model: 'inherit', passed: null, escalated: false, note: 'agent:Explore',
      label_pending: true, outcome: 'async', surface: 'claude-code', tool_use_id: 'toolu_1', start_tier: 'inherit',
      description: 'Find pressure gate state file', resolved_model: 'claude-opus-5-5', applied: 'no',
      inject_arm: 'evidence', injected: 'no', session_id: 'sess-1', agent_id: 'agent-1', advice_tier: 'sonnet', advice_state: 'READY',
    })
  })

  test('normalizeDescription mirrors route_join.normalize_description', () => {
    expect(normalizeDescription('  Fix the FLAKY test!! (v2) ')).toBe('fix the flaky test v2')
    expect(normalizeDescription('naïve—café')).toBe('na ve caf')
    expect(normalizeDescription('!!!')).toBe('')
    expect(normalizeDescription(undefined)).toBe('')
  })

  test('the later tier of an escalation follows route_join: explicit tier, else resolved tier', () => {
    expect(escalationTierOf('opus', 'claude-sonnet-5-5')).toBe('opus')
    expect(escalationTierOf('inherit', 'claude-opus-5-5')).toBe('opus')
    expect(escalationTierOf('best-model', 'claude-opus-5-5')).toBe('opus')
    expect(escalationTierOf('best-model', 'gpt-5')).toBeNull()
  })

  test('outcome from structured fields only: answer ok; no-usage API error is unavailable; anything else other', () => {
    expect(finishOf(complete())).toEqual({ outcome: 'ok', errorKind: null })
    expect(finishOf(complete({ reason: 'error', usage: undefined }))).toEqual({ outcome: 'error', errorKind: 'unavailable' })
    expect(finishOf(complete({ reason: 'error' }))).toEqual({ outcome: 'error', errorKind: 'other' })
    expect(finishOf(complete({ reason: 'aborted', isAborted: true, usage: undefined }))).toEqual({ outcome: 'error', errorKind: 'other' })
    expect(finishOf(complete({ isAborted: true }))).toEqual({ outcome: 'error', errorKind: 'other' })
    expect(finishOf(complete({ answer: 'opus is temporarily unavailable' }))).toEqual({ outcome: 'ok', errorKind: null })
  })
})

describe('router: hooks', () => {
  test('advise-only: a READY cell never rewrites the spawn; the row is written at turn.complete as ok', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', { [cellKey('explore', 'GREEN', 'sonnet')]: READY })
    await $.session.start(SESSION)
    expect(await $.agent.spawn(spawnInput())).toEqual({ model: 'claude-opus-5-5', agentId: 'agent-1' })
    expect(world.spawned).toEqual([{ model: undefined, subagentType: 'Explore' }])
    await world.clock.advance(5000)
    expect(routeRows(world)).toEqual([])
    await $.turn.complete(complete())
    await world.clock.advance(5000)
    expect(routeRows(world)).toEqual([
      expect.objectContaining({
        model: 'inherit', start_tier: 'inherit', resolved_model: 'claude-opus-5-5', agent_id: 'agent-1', applied: 'no',
        advice_tier: 'sonnet', outcome: 'ok', end_reason: 'answer', label_pending: true, escalated: false,
      }),
    ])
    expect(routeRows(world)[0]).not.toHaveProperty('error_kind')
  })

  test('explicit model, fork and Fable spawns reach the engine exactly as requested', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', { [cellKey('explore', 'GREEN', 'sonnet')]: READY, [cellKey('explore', 'GREEN', 'haiku')]: READY })
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput({ model: 'opus' }))
    await $.agent.spawn(spawnInput({ fork: true }))
    await $.agent.spawn(spawnInput({ parentModel: 'claude-fable-5' }))
    expect(world.spawned).toEqual([
      { model: 'opus', subagentType: 'Explore' },
      { model: undefined, subagentType: 'Explore' },
      { model: undefined, subagentType: 'Explore' },
    ])
    expect(world.toasts).toEqual([])
  })

  test('an API error with no usage finalizes the row as error/unavailable and feeds the ledger', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput({ model: 'opus' }))
    await $.turn.complete(complete({ reason: 'error', usage: undefined, durationMs: 900 }))
    await world.clock.advance(5000)
    expect(routeRows(world)).toEqual([expect.objectContaining({ outcome: 'error', error_kind: 'unavailable', end_reason: 'error', model: 'opus' })])
    const key = cellKey('explore', 'GREEN', 'opus')
    expect((world.store.get('datapce.cells') as Record<string, Cell>)[key]).toMatchObject({ n: 1, pass: 0 })
    const stats = world.store.get('datapce.stats') as Record<string, Record<string, { n: number; mean: number }>>
    expect(stats[key]?.unavailable).toMatchObject({ n: 1, mean: 1 })
    expect(stats[key]?.tokens).toBeUndefined()
  })

  test('a denied spawn was never made: no route row, no cell, no stat', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    world.denySpawn = true
    expect(await $.agent.spawn(spawnInput())).toEqual({ deny: 'denied by policy' })
    await world.clock.advance(5000)
    expect(world.store.get('datapce.cells')).toBeUndefined()
    expect(Object.keys((world.store.get('datapce.stats') as Record<string, unknown>) ?? {}).filter(k => !k.startsWith('step|'))).toEqual([])
    await $.session.end(END)
    expect(routeRows(world)).toEqual([])
  })

  test('a completed subagent labels the cell of the tier that ran', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput())
    await $.turn.complete(complete())
    await world.clock.advance(5000)
    const cells = world.store.get('datapce.cells') as Record<string, Cell>
    expect(cells[cellKey('explore', 'GREEN', 'opus')]).toMatchObject({ n: 1, pass: 1, state: 'WARMING' })
    const stats = world.store.get('datapce.stats') as Record<string, Record<string, { mean: number }>>
    expect(stats[cellKey('explore', 'GREEN', 'opus')]?.tokens?.mean).toBe(41000)
    expect(stats[cellKey('explore', 'GREEN', 'opus')]?.unavailable?.mean).toBe(0)
  })

  test('a promotion to READY never toasts (advise-only: no enforce eligibility)', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', { [cellKey('explore', 'GREEN', 'opus')]: ALMOST })
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput())
    await $.turn.complete(complete())
    await world.clock.advance(5000)
    expect((world.store.get('datapce.cells') as Record<string, Cell>)[cellKey('explore', 'GREEN', 'opus')]?.state).toBe('READY')
    expect(world.toasts).toEqual([])
  })

  test('a second turn.complete of the same agent (resumed) adds no row', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput())
    await $.turn.complete(complete())
    await $.turn.complete(complete())
    await world.clock.advance(5000)
    expect(routeRows(world)).toHaveLength(1)
    expect((world.store.get('datapce.cells') as Record<string, Cell>)[cellKey('explore', 'GREEN', 'opus')]).toMatchObject({ n: 1 })
  })

  test('unknown agentId after a reload is ignored when no Workflow run is active', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.turn.complete(complete({ agentId: 'agent-from-before-reload' }))
    await $.turn.complete(complete({ agentId: undefined }))
    await world.clock.advance(5000)
    expect(world.store.get('datapce.cells')).toBeUndefined()
    expect(routeRows(world)).toEqual([])
  })

  test('agents still running at session.end are written as async', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput({ background: true }))
    await $.session.end(END)
    expect(routeRows(world)).toEqual([expect.objectContaining({ agent_id: 'agent-1', outcome: 'async', label_pending: true })])
    expect(routeRows(world)[0]).not.toHaveProperty('end_reason')
  })

  test('Workflow agents: an unknown agentId during an active run becomes one workflow row', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.tool.call(workflowCall())
    await world.clock.advance(3000)
    const usage = { input_tokens: 900, output_tokens: 100, cache_read_input_tokens: 0, cache_creation_input_tokens: 0, model: 'claude-haiku-4-5' }
    await $.turn.complete(complete({ agentId: 'wf-agent-1', usage, durationMs: 2500 }))
    await $.turn.complete(complete({ agentId: 'wf-agent-1', usage, durationMs: 2500 }))
    await world.clock.advance(5000)
    expect(routeRows(world)).toEqual([
      expect.objectContaining({
        source: 'workflow', task_type: 'other', model: 'haiku', start_tier: 'unknown', resolved_model: 'claude-haiku-4-5',
        tool_use_id: 'toolu_wf', agent_id: 'wf-agent-1', outcome: 'ok', label_pending: true, note: 'workflow',
      }),
    ])
    expect(routeRows(world)[0]).not.toHaveProperty('description')
    expect((world.store.get('datapce.cells') as Record<string, Cell>)[cellKey('other', 'GREEN', 'haiku')]).toMatchObject({ n: 1, pass: 1 })
  })

  test('a stranger that started before the Workflow launch (survived a reload) is ignored', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await world.clock.advance(60_000)
    await $.tool.call(workflowCall())
    await world.clock.advance(1000)
    await $.turn.complete(complete({ agentId: 'pre-reload-agent', durationMs: 30_000 }))
    await world.clock.advance(5000)
    expect(routeRows(world)).toEqual([])
  })

  test('a Workflow agent running longer than an hour, started after the launch, is attributed', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.tool.call(workflowCall())
    await world.clock.advance(90 * 60_000)
    await $.turn.complete(complete({ agentId: 'long-wf-agent', durationMs: 89 * 60_000 }))
    await world.clock.advance(5000)
    expect(routeRows(world)).toEqual([expect.objectContaining({ source: 'workflow', agent_id: 'long-wf-agent', tool_use_id: 'toolu_wf' })])
  })

  test('session.end (/clear) forgets active Workflow runs', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.tool.call(workflowCall())
    await $.session.end(END)
    await world.clock.advance(3000)
    await $.turn.complete(complete({ agentId: 'next-session-stranger', durationMs: 1000 }))
    await world.clock.advance(5000)
    expect(routeRows(world)).toEqual([])
  })

  test('a turn.complete before session.start (hot reload) never overwrites persisted cells or stats', async ($, on) => {
    const world = worldOf(on)
    const persisted = { [cellKey('review', 'GREEN', 'sonnet')]: READY }
    world.store.set('datapce.cells', persisted)
    world.store.set('datapce.stats', { [cellKey('review', 'GREEN', 'sonnet')]: { tokens: { n: 3, mean: 10, m2: 1 } } })
    await $.agent.spawn(spawnInput())
    await $.turn.complete(complete())
    await $.session.end(END)
    expect(world.store.get('datapce.cells')).toEqual(persisted)
    expect(world.store.get('datapce.stats')).toEqual({ [cellKey('review', 'GREEN', 'sonnet')]: { tokens: { n: 3, mean: 10, m2: 1 } } })
  })

  test('escalation: a same-description re-dispatch to a higher tier marks the earlier (still running) row', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput({ model: 'sonnet', description: 'Fix the flaky test', background: true }))
    await world.clock.advance(60_000)
    await $.agent.spawn(spawnInput({ tool_use_id: 'toolu_2', model: 'opus', description: 'fix the flaky TEST!' }))
    await $.turn.complete(complete({ agentId: 'agent-2' }))
    await $.turn.complete(complete({ agentId: 'agent-1', reason: 'error', usage: undefined }))
    await world.clock.advance(5000)
    const rows = routeRows(world)
    expect(rows.find(r => r.tool_use_id === 'toolu_1')).toMatchObject({ escalated: true, escalated_by: 'toolu_2', outcome: 'error' })
    expect(rows.find(r => r.tool_use_id === 'toolu_2')).toMatchObject({ escalated: false })
  })

  test('escalation patches a finished row still waiting for the flush; inherit, same-tier and other descriptions never escalate', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput({ model: 'haiku', description: 'Summarize the log' }))
    await $.turn.complete(complete({ agentId: 'agent-1' }))
    await $.agent.spawn(spawnInput({ tool_use_id: 'toolu_inh', description: 'Read the config' }))
    await $.turn.complete(complete({ agentId: 'agent-2' }))
    await $.agent.spawn(spawnInput({ tool_use_id: 'toolu_same', model: 'haiku', description: 'Count lines' }))
    await $.turn.complete(complete({ agentId: 'agent-3' }))
    await world.clock.advance(1000)
    await $.agent.spawn(spawnInput({ tool_use_id: 'toolu_up', model: 'sonnet', description: 'summarize the log.' }))
    await $.agent.spawn(spawnInput({ tool_use_id: 'toolu_inh2', model: 'opus', description: 'Read the config' }))
    await $.agent.spawn(spawnInput({ tool_use_id: 'toolu_same2', model: 'haiku', description: 'Count lines' }))
    await $.agent.spawn(spawnInput({ tool_use_id: 'toolu_other', model: 'opus', description: 'Something else' }))
    await world.clock.advance(5000)
    const byId = new Map(routeRows(world).map(r => [r.tool_use_id, r]))
    expect(byId.get('toolu_1')).toMatchObject({ escalated: true, escalated_by: 'toolu_up', outcome: 'ok' })
    expect(byId.get('toolu_inh')).toMatchObject({ escalated: false })
    expect(byId.get('toolu_same')).toMatchObject({ escalated: false })
    expect(byId.get('toolu_inh')).not.toHaveProperty('escalated_by')
  })

  test('malformed persisted cells do not break dispatch', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', 'garbage')
    await $.session.start(SESSION)
    expect(await $.agent.spawn(spawnInput())).toEqual({ model: 'claude-opus-5-5', agentId: 'agent-1' })
  })
})

describe('router: dispatch view publishing', () => {
  const published = (world: World): { toolUseId: string }[] => (world.published.get('dispatches') as { toolUseId: string }[] | undefined) ?? []

  test('a burst publishes once at once; the rest wait for the timer; session.end flushes the latest', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    for (let i = 0; i < 3; i++) await $.agent.spawn(spawnInput({ tool_use_id: `toolu_${i}` }))
    expect(published(world).map(d => d.toolUseId)).toEqual(['toolu_0'])
    await world.clock.advance(1000)
    expect(published(world).map(d => d.toolUseId)).toEqual(['toolu_0', 'toolu_1', 'toolu_2'])
    await $.agent.spawn(spawnInput({ tool_use_id: 'toolu_3' }))
    expect(published(world)).toHaveLength(3)
    await $.session.end(END)
    expect(published(world).map(d => d.toolUseId)).toEqual(['toolu_0', 'toolu_1', 'toolu_2', 'toolu_3'])
  })

  test('the published view stays at the newest 200 (running dispatches are never trimmed)', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    for (let i = 0; i < 520; i++) await $.agent.spawn(spawnInput({ tool_use_id: `toolu_${i}` }))
    await $.session.end(END)
    const view = published(world)
    expect(view).toHaveLength(200)
    expect(view[199]!.toolUseId).toBe('toolu_519')
  })
})
