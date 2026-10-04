import { describe, expect, test } from 'claude-code/testing'

import {
  bashEventOf, expiredFiles, measureRow, observePath, record, spawnRow, ROW_MAX_BYTES,
} from '../hooks/observe.ts'
import { newRuntime, optionsOf } from '../hooks/runtime.ts'
import { MEASURE, SESSION, spawnInput, stepInput } from './fixtures/inputs.ts'
import { BACKEND, T0, drain, worldOf, type World } from './fixtures/world.ts'

const DAY = observePath(BACKEND, T0)
const rowsOf = (world: World, path = DAY): Record<string, unknown>[] => (world.appended.get(path) ?? []).map(l => JSON.parse(l))

describe('observe: pure rows', () => {
  test('spawn rows keep the dispatch label (≤ 120 chars), never the prompt', () => {
    const row = spawnRow(spawnInput({ description: 'd'.repeat(300), prompt: 'SECRET PROMPT' }), 'explore', 'claude-sonnet-5-5', 1)
    expect(String(row.description)).toHaveLength(120)
    expect(JSON.stringify(row)).not.toContain('SECRET')
    expect(row).toMatchObject({ ev: 'spawn', tool_use_id: 'toolu_1', task_type: 'explore', model_resolved: 'claude-sonnet-5-5' })
  })

  test('measure rows', () => {
    expect(measureRow({ ctxPercent: 61, limitKind: 'five_hour', limitPercent: 62, resetsAt: null, costUsd: 4.12 }, 7)).toEqual({
      ev: 'measure', ts: 7, ctx_pct: 61, limit_kind: 'five_hour', limit_pct: 62, resets_at: null, cost_usd: 4.12,
    })
  })

  test('backend commands and signal patterns are classified; anything else is ignored', () => {
    expect(bashEventOf('apex-router pressure --check', 'GREEN', false, 1)).toEqual({ cmd: 'pressure', signal: null, isError: false, t: 1 })
    expect(bashEventOf('apex-ornith review x', 'OrnithBusy: queue full', true, 2)).toEqual({ cmd: 'ornith', signal: 'OrnithBusy', isError: true, t: 2 })
    expect(bashEventOf('apex-router review-preread', 'timeout after 30s', false, 3)?.signal).toBe('timeout')
    expect(bashEventOf('apex-ornith gen', 'finish_reason=length', false, 4)?.signal).toBe('truncation')
    expect(bashEventOf('ls -la', 'OrnithBusy', false, 5)).toBeNull()
  })

  test('retention keeps 30 UTC days and ignores other files', () => {
    expect(expiredFiles(['2026-09-01.jsonl', '2026-09-03.jsonl', '2026-10-03.jsonl', '.keep', 'notes.txt'], T0)).toEqual(['2026-09-01.jsonl'])
  })

  test('the pending buffer is bounded at 5000 rows, newest kept', () => {
    const rt = newRuntime(optionsOf({}))
    for (let i = 0; i < 5100; i++) record(rt, { ev: 'step', ts: i })
    expect(rt.rows).toHaveLength(5000)
    expect(rt.rows[0]).toBe('{"ev":"step","ts":100}')
  })

  test('an oversized row is replaced by a stub, never split', () => {
    const rt = newRuntime(optionsOf({}))
    record(rt, { ev: 'spawn', ts: 1, description: 'x'.repeat(5000) })
    expect(rt.rows[0]).toBe('{"ev":"spawn","ts":1,"truncated":true}')
    expect(new TextEncoder().encode(rt.rows[0]!).length).toBeLessThanOrEqual(ROW_MAX_BYTES)
  })
})

describe('observe: hooks', () => {
  test('session.start removes expired day files', async ($, on) => {
    const old = `${BACKEND}/observe/2026-09-01.jsonl`
    const kept = `${BACKEND}/observe/2026-09-20.jsonl`
    const world = worldOf(on, { [old]: '{}\n', [kept]: '{}\n' })
    await $.session.start(SESSION)
    expect(world.files.has(old)).toBe(false)
    expect(world.files.has(kept)).toBe(true)
  })

  test('session.start creates the observe directory when it is missing', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    expect(world.files.has(`${BACKEND}/observe/.keep`)).toBe(true)
  })

  test('a step and a measurement land as rows after the 5 s flush — counts only', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    world.step = { toolUses: [{ name: 'Agent', input: { prompt: 'SECRET PROMPT' } }] }
    await drain($.turn.step(stepInput()))
    await $.session.measure(MEASURE)
    expect(rowsOf(world)).toEqual([])
    await world.clock.advance(5000)
    const rows = rowsOf(world)
    expect(rows.map(r => r.ev)).toEqual(['step', 'measure'])
    expect(rows[0]).toMatchObject({ model: 'claude-opus-5-5', output: 50, cache_read: 1000, stop: 'end_turn', tools: ['Agent'] })
    expect(JSON.stringify(rows)).not.toContain('SECRET')
  })

  test('Bash backend commands become signal rows; results pass through untouched', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    world.bashOutput = 'error: OrnithBusy (queue full)'
    const r = await $.tool.call({ tool: 'Bash', command: 'apex-router pressure --check --token=SECRET' } as never)
    expect((r as { result?: unknown }).result).toEqual({ stdout: 'error: OrnithBusy (queue full)', stderr: '' })
    await $.tool.call({ tool: 'Bash', command: 'ls -la' } as never)
    await world.clock.advance(5000)
    expect(rowsOf(world)).toEqual([{ ev: 'bash', ts: T0, cmd: 'pressure', signal: 'OrnithBusy', error: false }])
    expect(JSON.stringify(rowsOf(world))).not.toContain('SECRET')
  })

  test('skills in use are counted in the profile', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.skill.prompt({ skill: 'superpowers:writing-plans', text: 'PLAN' })
    await world.clock.advance(5000)
    const p = world.store.get('datapce.profile') as { skills: Record<string, number>; repos: string[] }
    expect(p.skills['superpowers:writing-plans']).toBe(1)
    expect(p.repos).toHaveLength(1)
    expect(p.repos[0]).toMatch(/^[0-9a-f]{16}$/)
  })

  test('a failed append keeps the rows and retries on the next flush', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    world.answer = argv => (argv[0] === '/usr/bin/tee' ? { exitCode: 1, stdout: '' } : null)
    await drain($.turn.step(stepInput()))
    await world.clock.advance(5000)
    expect(rowsOf(world)).toEqual([])
    world.answer = () => null
    await world.clock.advance(5000)
    expect(rowsOf(world).map(r => r.ev)).toEqual(['step'])
  })

  test('malformed persisted values are dropped, not fatal', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.stats', 'garbage')
    world.store.set('datapce.profile', { repos: 5, hours: 'x', skills: { a: 'b' } })
    await $.session.start(SESSION)
    await drain($.turn.step(stepInput()))
    await world.clock.advance(5000)
    const p = world.store.get('datapce.profile') as { repos: string[]; hours: number[]; skills: Record<string, number> }
    expect(p.repos).toHaveLength(1)
    expect(p.hours).toHaveLength(24)
    expect(p.skills).toEqual({})
    expect(Object.keys(world.store.get('datapce.stats') as object)).toEqual(['step|opus'])
  })
})
