// /clear and resume (engine 2.1.288): session.end{reason:'clear'|'resume'} and the process goes on
// under a NEW session id with no session.start. Per-session state resets at that end and the new
// session is identified lazily on its first turn.start / prompt.compose (never on agent.spawn).
import { describe, expect, test } from 'claude-code/testing'

import type { Cell } from '../types/index.d.ts'
import { cellKey } from '../hooks/evidence.ts'
import { complete, MEASURE, SESSION, spawnInput, stepInput } from './fixtures/inputs.ts'
import { BACKEND, drain, worldOf, type World } from './fixtures/world.ts'

const READY: Cell = { n: 35, pass: 35, state: 'READY', winN: 5, winPass: 5, below: 0, above: 0, cusum: 0, p0: 1 }
const EVIDENCE_2 = 'sess-0004' // armOf → evidence
const HOLDOUT_ID = 'sess-0002' // armOf → holdout
const CLEAR = { reason: 'clear', sessionId: 'sess-0001' } as never
const RED_MEASURE = { ...MEASURE, rateLimits: [{ kind: 'five_hour' as const, percentUsed: 95, resetsAt: '2026-10-03T20:00:00Z' }] }
const PLAN = { skill: 'superpowers:writing-plans', text: 'PLAN' }

const routeRows = (world: World): Record<string, unknown>[] => (world.appended.get(`${BACKEND}/route_log.jsonl`) ?? []).map(l => JSON.parse(l))
const injectRows = (world: World): Record<string, unknown>[] =>
  (world.appended.get(`${BACKEND}/observe/2026-10-03.jsonl`) ?? []).map(l => JSON.parse(l)).filter(r => r.ev === 'inject')
const seedReady = (world: World): void => void world.store.set('datapce.cells', { [cellKey('explore', 'GREEN', 'sonnet')]: READY })

/** The engine's /clear: session.end{clear}, then the process answers a new id; no session.start. */
async function clear($: { session: { end(e: never): Promise<unknown> } }, world: World, next: string): Promise<void> {
  await $.session.end(CLEAR)
  world.sessionId = next
}

describe('session: /clear and resume re-identify', () => {
  test('route rows after /clear carry the new session id and its arm', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput({ tool_use_id: 'toolu_a' }))
    await $.turn.complete(complete({ agentId: 'agent-1' }))
    await clear($, world, HOLDOUT_ID)
    await $.turn.start({ text: 'next task', turnId: 'turn-n1' })
    await $.agent.spawn(spawnInput({ tool_use_id: 'toolu_b' }))
    await $.turn.complete(complete({ agentId: 'agent-2' }))
    await world.clock.advance(5000)
    expect(routeRows(world).map(r => [r.tool_use_id, r.session_id, r.inject_arm])).toEqual([
      ['toolu_a', 'sess-0001', 'evidence'],
      ['toolu_b', HOLDOUT_ID, 'holdout'],
    ])
    expect(world.published.get('inject')).toEqual({ arm: 'holdout', bytes: 0, sections: 0 })
  })

  test('prompt.compose also re-identifies', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await clear($, world, EVIDENCE_2)
    await $.prompt.compose({ model: 'claude-opus-5-5', promptModel: 'claude-opus-5-5', surfaces: ['terminal'], tools: ['Bash'], outputStyle: null, traits: [] } as never)
    await $.agent.spawn(spawnInput())
    await $.session.end({ reason: 'other', sessionId: EVIDENCE_2 } as never)
    expect(routeRows(world)).toEqual([expect.objectContaining({ session_id: EVIDENCE_2, inject_arm: 'evidence', outcome: 'async' })])
  })

  test('between /clear and the next turn nothing is injected and rows are stamped unknown (fails closed)', async ($, on) => {
    const world = worldOf(on)
    seedReady(world)
    await $.session.start(SESSION)
    await clear($, world, EVIDENCE_2)
    expect((await $.skill.prompt(PLAN)).text).toBe('PLAN')
    await $.agent.spawn(spawnInput())
    await $.session.end({ reason: 'other', sessionId: EVIDENCE_2 } as never)
    const row = routeRows(world)[0]!
    expect(row.inject_arm).toBe('unknown')
    expect(row).not.toHaveProperty('session_id')
  })

  test('the session byte counter is fresh for the new session (describe line; the skill table is off in v1)', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure(RED_MEASURE as never)
    await world.clock.advance(120_000)
    const describeOnce = () => $.tool.describe({ tool: 'Agent', description: 'D', provider: { plugin: 'engine', tier: 'core' } as never })
    expect((await describeOnce()).description).not.toBe('D')
    const first = world.published.get('inject') as { bytes: number; sections: number }
    expect(first).toMatchObject({ arm: 'evidence', sections: 1 })
    expect(first.bytes).toBeGreaterThan(0)
    await clear($, world, EVIDENCE_2)
    await $.turn.start({ text: '', turnId: 'turn-n1' })
    expect((await describeOnce()).description).not.toBe('D')
    expect(world.published.get('inject')).toEqual({ arm: 'evidence', bytes: first.bytes, sections: 1 })
  })

  test('the handoff toast does not repeat for the session the user started fresh', async ($, on) => {
    const world = worldOf(on, { [`${BACKEND}/pressure.json`]: '{"families": {}}', [`${BACKEND}/handoff_threshold.json`]: '{"threshold_tokens": 1000}' })
    await $.session.start(SESSION)
    await new Promise(r => setTimeout(r, 0)) // the first backend poll finishes in the background
    await drain($.turn.step(stepInput()))
    await $.session.measure(MEASURE)
    expect(world.toasts).toHaveLength(1)
    await clear($, world, EVIDENCE_2)
    await world.clock.advance(11 * 60_000)
    await $.turn.start({ text: '', turnId: 'turn-n1' })
    await $.session.measure(MEASURE)
    expect(world.toasts).toHaveLength(1)
    await drain($.turn.step(stepInput({ turnId: 'turn-n1' })))
    await $.session.measure(MEASURE)
    expect(world.toasts).toHaveLength(2) // the new session crossing its own threshold is told once
  })

  test('budget burn restarts with the new session', { options: { budgetUsd: 10 } }, async ($, on) => {
    const world = worldOf(on)
    const cost = (usd: number) => $.session.measure({ ...MEASURE, rateLimits: [], cost: { usd } } as never)
    await $.session.start(SESSION)
    await cost(0)
    await world.clock.advance(30 * 60_000)
    await cost(9)
    await clear($, world, EVIDENCE_2)
    await $.turn.start({ text: '', turnId: 'turn-n1' })
    await cost(9)
    await world.clock.advance(10 * 60_000)
    await cost(9.1)
    await world.clock.advance(60_000)
    const s = world.published.get('signals') as { budgetBurn: number | null }
    // $0.10 over ~11 min against $10/day: ~1.3×, not the $9.10 whole-process figure (~32×)
    expect(s.budgetBurn).not.toBeNull()
    expect(s.budgetBurn!).toBeLessThan(2)
  })

  test('a holdout first session still leaves the describe refresh for a later evidence session', async ($, on) => {
    const world = worldOf(on, {}, HOLDOUT_ID)
    await $.session.start(SESSION)
    await clear($, world, EVIDENCE_2)
    await $.turn.start({ text: '', turnId: 'turn-n1' })
    await $.session.measure(RED_MEASURE as never)
    await world.clock.advance(20 * 60_000)
    expect(world.invalidated).toEqual(['tool.describe'])
  })

  test('a resume resets like a /clear', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.session.end({ reason: 'resume', sessionId: 'sess-0001' } as never)
    world.sessionId = HOLDOUT_ID
    await $.turn.start({ text: '', turnId: 'turn-n1' })
    await $.agent.spawn(spawnInput())
    await $.session.end({ reason: 'other', sessionId: HOLDOUT_ID } as never)
    expect(routeRows(world)).toEqual([expect.objectContaining({ session_id: HOLDOUT_ID, inject_arm: 'holdout' })])
  })

  test('an exit does not reset: the last flush keeps the ending session id', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput())
    await $.session.end({ reason: 'prompt_input_exit', sessionId: 'sess-0001' } as never)
    expect(routeRows(world)).toEqual([expect.objectContaining({ session_id: 'sess-0001' })])
  })
})
