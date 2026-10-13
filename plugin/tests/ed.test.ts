import type { On } from 'claude-code'
import { describe, expect, test } from 'claude-code/testing'

import { completeEd, dropPrompt, ED_TEXT, edArmOf, edPath, mix32, noteCompose, noteEdSpawn, noteEdTokens, startTurn, submitPrompt } from '../hooks/ed.ts'
import { armOf, fnv1a } from '../hooks/inject.ts'
import type { Host } from '../hooks/host.ts'
import { endSession, newRuntime, optionsOf } from '../hooks/runtime.ts'
import { complete, SESSION, spawnInput, stepInput } from './fixtures/inputs.ts'
import { BACKEND, drain, SESSION_ID, T0, worldOf, type World } from './fixtures/world.ts'

const COMPOSE = { model: 'claude-opus-5-5', promptModel: 'claude-opus-5-5', surfaces: ['terminal'], outputStyle: null, traits: [] }
const edRows = (world: World): Record<string, unknown>[] => (world.appended.get(edPath(BACKEND)) ?? []).map(l => JSON.parse(l))
const NO_HOST = {} as Host

function rtOf(ed = true) {
  const rt = newRuntime(optionsOf({ ed }))
  rt.sessionId = SESSION_ID
  rt.backendDir = BACKEND
  return rt
}

describe('ed: arm assignment', () => {
  test('the arm is a fair coin per turn, serially independent, reproducible', () => {
    expect(edArmOf('s', 1)).toBe(edArmOf('s', 1))
    let w2 = 0, changes = 0, pairs = 0, n = 0
    const patterns = new Set<string>()
    for (let s = 0; s < 300; s++) {
      let pattern = ''
      let prev: string | null = null
      for (let t = 1; t <= 12; t++) {
        const a = edArmOf(`session-${s}-${fnv1a(String(s))}`, t)
        pattern += a === 'W2' ? '1' : '0'
        n += 1
        if (a === 'W2') w2 += 1
        if (prev !== null) {
          pairs += 1
          if (a !== prev) changes += 1
        }
        prev = a
      }
      patterns.add(pattern)
    }
    expect(w2 / n).toBeGreaterThan(0.45)
    expect(w2 / n).toBeLessThan(0.55)
    expect(changes / pairs).toBeGreaterThan(0.45) // FNV-1a's raw low bit alternates with the turn digit; mix32 fixes that
    expect(changes / pairs).toBeLessThan(0.55)
    expect(patterns.size).toBeGreaterThan(100)
    expect(mix32(0)).toBe(0)
    expect(mix32(1)).not.toBe(mix32(2))
  })

  test('prompt.submit: the user\'s own non-empty prompt gets a context block; nothing else does', () => {
    const rt = rtOf()
    expect(submitPrompt(rt, { text: '  ' }, T0)).toBeNull()
    expect(submitPrompt(rt, { text: 'x', origin: { kind: 'task-notification' } as never }, T0)).toBeNull()
    expect(submitPrompt(rt, { text: 'x', origin: { kind: 'plugin' } as never }, T0)).toBeNull()
    expect(submitPrompt(rt, { text: 'typed over a running turn', turnId: 'running' }, T0)).toBeNull()
    expect(rt.edQueue).toHaveLength(0)
    expect(submitPrompt(rt, { text: 'fix the parser', context: ['mine'] }, T0)).toEqual(['mine', ED_TEXT[edArmOf(SESSION_ID, 1)]])
    expect(submitPrompt(rt, { text: 'and the docs', origin: { kind: 'composer' } as never }, T0 + 1)).toEqual([ED_TEXT[edArmOf(SESSION_ID, 2)]])
    expect(rt.edQueue.map(e => [e.turn, e.arm, e.ts, e.applied, e.turnId])).toEqual([
      [1, edArmOf(SESSION_ID, 1), T0, true, null],
      [2, edArmOf(SESSION_ID, 2), T0 + 1, true, null],
    ])
    expect(submitPrompt(rtOf(false), { text: 'x' }, T0)).toBeNull()
    const anon = rtOf()
    anon.sessionId = ''
    expect(submitPrompt(anon, { text: 'x' }, T0)).toBeNull()
    // the evidence experiment's holdout never injects: no arm there either
    const holdout = rtOf()
    holdout.sessionId = Array.from({ length: 200 }, (_, i) => `hold-${i}`).find(id => armOf(id) === 'holdout')!
    expect(submitPrompt(holdout, { text: 'x' }, T0)).toBeNull()
    // a prompt a hook beneath dropped is forgotten
    const d = rtOf()
    submitPrompt(d, { text: 'keep' }, T0)
    submitPrompt(d, { text: 'gone' }, T0)
    dropPrompt(d, 'gone')
    dropPrompt(d, 'never queued')
    expect(d.edQueue.map(e => e.turn)).toEqual([1])
  })

  test('turn.start binds the queued arm whose text it proceeds with; never by queue order', () => {
    const rt = rtOf()
    startTurn(rt, { text: 'typed with no submit', turnId: 't0' }, T0)
    expect(rt.ed).toBeNull()
    submitPrompt(rt, { text: 'a' }, T0)
    submitPrompt(rt, { text: 'b' }, T0 + 1)
    startTurn(rt, { text: '', turnId: 'cont' }, T0 + 2)
    expect(rt.ed).toBeNull()
    expect(rt.edQueue).toHaveLength(2)
    startTurn(rt, { text: 'a task notification arrived', turnId: 'notif' }, T0 + 2) // no match: binds nothing, drops nothing
    expect(rt.ed).toBeNull()
    expect(rt.edQueue).toHaveLength(2)
    startTurn(rt, { text: 'b', turnId: 't2' }, T0 + 3) // 'a' never got a turn: stale, dropped
    expect(rt.ed).toMatchObject({ turn: 2, arm: edArmOf(SESSION_ID, 2), turnId: 't2', tsStart: T0 + 3, fanout: false })
    expect(rt.edQueue).toHaveLength(0)
  })

  test('a render offering Agent or Workflow makes the turn a unit; tokens go to the turn that owns the agent', async () => {
    const rt = rtOf()
    submitPrompt(rt, { text: 'a' }, T0)
    startTurn(rt, { text: 'a', turnId: 't1' }, T0)
    noteCompose(rt, { tools: ['Bash'] })
    expect(rt.ed?.fanout).toBe(false)
    noteCompose(rt, { tools: ['Bash', 'Workflow'] })
    expect(rt.ed?.fanout).toBe(true)
    noteEdSpawn(rt, 'agent-1')
    noteEdSpawn(rt, '')
    noteEdSpawn(rt, null)
    noteEdTokens(rt, undefined, 100)
    noteEdTokens(rt, 'agent-1', 40)
    noteEdTokens(rt, 'wf-agent', 9) // a Workflow agent: no agent.spawn, bound by its first step
    expect(rt.ed).toMatchObject({ tokMain: 100, tokSub: 49, open: 2 })
    expect([...rt.ed!.agents]).toEqual(['agent-1', 'wf-agent'])
    const ed = rt.ed!
    await completeEd(NO_HOST, rt, { turnId: 'wf-sub', agentId: 'wf-agent', isAborted: false, durationMs: 1 }, T0 + 1)
    await completeEd(NO_HOST, rt, { turnId: 't1', agentId: undefined, isAborted: false, durationMs: 5 }, T0 + 5)
    expect(rt.ed).toBeNull()
    expect(ed.done).toBe(true)
    noteEdTokens(rt, undefined, 7) // a continuation step after the turn: nobody's
    noteEdTokens(rt, 'agent-1', 3) // the background subagent's step: still the turn's
    noteEdTokens(rt, 'late-unknown', 5) // an unknown agent after the turn: nobody's
    expect(ed).toMatchObject({ tokMain: 100, tokSub: 52, open: 1 })
    expect(rt.edClosing).toEqual([ed])
  })

  test('endSession forgets the open turn, the queue, the agents and the count', () => {
    const rt = rtOf()
    submitPrompt(rt, { text: 'a' }, T0)
    noteEdSpawn(rt, 'x')
    endSession(rt)
    expect([rt.ed, rt.edQueue, rt.edClosing, rt.edAgents.size, rt.edTurns]).toEqual([null, [], [], 0, 0])
  })
})

describe('ed: rows through the hooks', () => {
  const submitBottom = (on: On) => on('prompt.submit', (_$, e) => ({ text: e.text, context: e.context }))

  test('one row per typed turn: context attached, tokens from steps only, subagents counted once', { options: { ed: true } }, async ($, on) => {
    const world = worldOf(on)
    submitBottom(on)
    await $.session.start(SESSION)
    const r = await $.prompt.submit({ text: 'investigate the parser SECRET-TEXT' } as never)
    expect((r as { context?: string[] }).context).toEqual([ED_TEXT[edArmOf(SESSION_ID, 1)]])
    await $.turn.start({ text: 'investigate the parser SECRET-TEXT', turnId: 'turn-1' })
    const sections = (await $.prompt.compose({ ...COMPOSE, tools: ['Agent', 'Bash'] } as never)).sections
    expect(sections.some(s => s.text.includes('Workflow arm'))).toBe(false) // never a system-prompt section
    expect(await $.agent.spawn(spawnInput())).toEqual({ model: 'claude-opus-5-5', agentId: 'agent-1' })
    await drain($.turn.step(stepInput({ agentId: 'agent-1' }))) // 1150 tokens
    await $.turn.complete(complete({ turnId: 'sub-1', agentId: 'agent-1' })) // usage here is NOT added again
    await drain($.turn.step(stepInput()))
    await drain($.turn.step(stepInput()))
    expect(edRows(world)).toEqual([])
    await world.clock.advance(3000)
    await $.turn.complete(complete({ turnId: 'turn-1', agentId: undefined, durationMs: 3000 }))
    const rows = edRows(world)
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({
      ev: 'ed_arm', ts: T0 / 1000, ts_start: T0 / 1000, ts_end: (T0 + 3000) / 1000, session_id: SESSION_ID, turn: 1, turn_id: 'turn-1',
      arm: edArmOf(SESSION_ID, 1), applied: true, fanout: true, aborted: false, duration_ms: 3000,
      tok_main: 2300, tok_sub: 1150, subs: 1, subs_open: 0, inject_arm: 'evidence',
    })
    expect(JSON.stringify(rows[0])).not.toContain('SECRET')
    await $.turn.complete(complete({ turnId: 'turn-1', agentId: undefined }))
    expect(edRows(world)).toHaveLength(1)
  })

  test('a background subagent keeps its turn open: the row waits for its completion and carries its tokens', { options: { ed: true } }, async ($, on) => {
    const world = worldOf(on)
    submitBottom(on)
    await $.session.start(SESSION)
    await $.prompt.submit({ text: 'go' } as never)
    await $.turn.start({ text: 'go', turnId: 'turn-1' })
    await $.agent.spawn(spawnInput({ background: true }))
    await $.turn.complete(complete({ turnId: 'turn-1', agentId: undefined }))
    expect(edRows(world)).toEqual([])
    await drain($.turn.step(stepInput({ agentId: 'agent-1' })))
    await $.turn.complete(complete({ turnId: 'sub-1', agentId: 'agent-1' }))
    expect(edRows(world)).toEqual([expect.objectContaining({ turn: 1, tok_main: 0, tok_sub: 1150, subs: 1, subs_open: 0 })])
  })

  test('a turn still waiting on a background subagent keeps collecting across later turns and is flushed at session end with subs_open', { options: { ed: true } }, async ($, on) => {
    const world = worldOf(on)
    submitBottom(on)
    await $.session.start(SESSION)
    await $.prompt.submit({ text: 'one' } as never)
    await $.turn.start({ text: 'one', turnId: 'turn-1' })
    await $.agent.spawn(spawnInput({ background: true }))
    await $.turn.complete(complete({ turnId: 'turn-1', agentId: undefined }))
    await $.turn.start({ text: '', turnId: 'cont' }) // a continuation gets no arm
    await $.prompt.submit({ text: 'two' } as never)
    await $.turn.start({ text: 'two', turnId: 'turn-2' })
    await drain($.turn.step(stepInput({ agentId: 'agent-1' }))) // the background agent's step still counts for turn 1
    await $.turn.complete(complete({ turnId: 'turn-2', agentId: undefined }))
    expect(edRows(world).map(r => [r.turn, r.fanout])).toEqual([[2, false]])
    await $.session.end({ reason: 'prompt_input_exit', sessionId: SESSION_ID } as never)
    expect(edRows(world).map(r => [r.turn, r.tok_sub, r.subs, r.subs_open])).toEqual([[2, 0, 0, 0], [1, 1150, 1, 1]])
  })

  test('a prompt a hook beneath drops leaves no arm behind; a queued prompt a notification turn overtakes keeps its own arm', { options: { ed: true } }, async ($, on) => {
    const world = worldOf(on)
    let dropNext = false
    on('prompt.submit', (_$, e) => (dropNext ? ({ drop: 'test' } as never) : { text: e.text, context: e.context }))
    await $.session.start(SESSION)
    dropNext = true
    await $.prompt.submit({ text: 'dropped' } as never)
    dropNext = false
    await $.prompt.submit({ text: 'kept' } as never)
    await $.turn.start({ text: 'background task finished', turnId: 'notif' })
    await $.turn.complete(complete({ turnId: 'notif', agentId: undefined }))
    expect(edRows(world)).toEqual([])
    await $.turn.start({ text: 'kept', turnId: 'turn-2' })
    await $.turn.complete(complete({ turnId: 'turn-2', agentId: undefined }))
    expect(edRows(world)).toEqual([expect.objectContaining({ turn: 2, turn_id: 'turn-2', arm: edArmOf(SESSION_ID, 2) })])
  })

  test('a notification prompt gets no arm and no row', { options: { ed: true } }, async ($, on) => {
    const world = worldOf(on)
    submitBottom(on)
    await $.session.start(SESSION)
    const r = await $.prompt.submit({ text: 'task done', origin: { kind: 'task-notification' } } as never)
    expect((r as { context?: string[] }).context).toBeUndefined()
    await $.turn.start({ text: 'task done', turnId: 'turn-1' })
    await $.turn.complete(complete({ turnId: 'turn-1', agentId: undefined }))
    expect(edRows(world)).toEqual([])
  })

  test('off by default: no context, no rows', async ($, on) => {
    const world = worldOf(on)
    submitBottom(on)
    await $.session.start(SESSION)
    const r = await $.prompt.submit({ text: 'go' } as never)
    expect((r as { context?: string[] }).context).toBeUndefined()
    await $.turn.start({ text: 'go', turnId: 'turn-1' })
    await $.turn.complete(complete({ turnId: 'turn-1', agentId: undefined }))
    expect(edRows(world)).toEqual([])
  })
})
