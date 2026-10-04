import { describe, expect, test } from 'claude-code/testing'

import type { CellView, Dispatch } from '../types/index.d.ts'
import { cellKey, cellViews } from '../hooks/evidence.ts'
import { HANDOFF_TEMPLATE } from '../hooks/handoff.ts'
import { ledgerRows, NO_LABEL, paneSections, type PaneInput } from '../hooks/pane.tsx'
import { EMPTY_BACKEND, EMPTY_INJECT, EMPTY_PROFILE_STORE, EMPTY_SIGNALS, profileView } from '../hooks/state.ts'
import { command, PANE, SESSION, spawnInput } from './fixtures/inputs.ts'
import { worldOf } from './fixtures/world.ts'

const view = (over: Partial<CellView>): CellView => ({
  key: 'explore|GREEN|sonnet', taskType: 'explore', level: 'GREEN', tier: 'sonnet', state: 'WARMING', n: 12, pass: 12, wilsonLo: 0.76, tokMean: 41000, tokN: 12, durationMean: 41000, durationN: 12, unavailable: 0, ...over,
})
const empty = (over: Partial<PaneInput> = {}): PaneInput => ({
  ds: [], s: EMPTY_SIGNALS, b: EMPTY_BACKEND, cells: [], profile: profileView(EMPTY_PROFILE_STORE), inject: EMPTY_INJECT, ...over,
})
const d = (over: Partial<Dispatch>): Dispatch => ({
  toolUseId: 't', agentId: 'a', description: 'Find X', subagentType: 'Explore', taskType: 'explore', requested: 'inherit',
  resolved: 'claude-opus-5-5', advised: null, advisedState: null, applied: false, basis: '', outcome: 'ok', startedAt: 0,
  durationMs: 41000, tokens: 12000, level: 'GREEN', ...over,
})

describe('pane: sections', () => {
  test('empty: honest placeholders; no Lanes without a backend; never an anomaly or enforce section', () => {
    const s = paneSections(empty())
    expect(s.map(x => x.title)).toEqual(['Dispatches', 'Signals', 'Evidence', 'Profile'])
    expect(s[0]!.rows).toEqual(['no dispatches yet'])
    expect(s[2]!.rows).toEqual(['no finished dispatches yet', NO_LABEL])
    expect(JSON.stringify(s)).not.toMatch(/anomaly|enforce/i)
  })

  test('dispatch rows, the ▲ basis, agreement and pass rate', () => {
    const s = paneSections(empty({
      ds: [
        d({ advised: 'sonnet', advisedState: 'READY', basis: 'opus→sonnet: 35/35 pass; GREEN now' }),
        d({ description: 'Review diff', taskType: 'review', outcome: 'failed', resolved: 'claude-sonnet-5-5', requested: 'sonnet' }),
      ],
    }))
    expect(s[0]!.rows).toEqual([
      'agreement 0/1 with advice · pass 1/2',
      'Review diff · review · sonnet→sonnet · failed 41s 12k',
      'Find X · explore · inherit→opus · ok 41s 12k ▲ sonnet: opus→sonnet: 35/35 pass; GREEN now',
    ])
  })

  test('ledger: n, ok% with error kinds, tok μ per task type × tier, levels pooled, dur μ shown', () => {
    const rows = ledgerRows([
      view({ n: 10, pass: 9, unavailable: 1, tokMean: 40000, tokN: 10, durationMean: 41000, durationN: 10 }),
      view({ key: 'explore|AMBER|sonnet', level: 'AMBER', n: 10, pass: 7, unavailable: 1, tokMean: 20000, tokN: 10, durationMean: 30000, durationN: 10 }),
      view({ key: 'review|GREEN|opus', taskType: 'review', tier: 'opus', n: 4, pass: 4, tokMean: null, tokN: 0, durationMean: null, durationN: 0 }),
    ])
    expect(rows).toEqual([
      'explore · sonnet · n 20 · ok 80% (2 unavailable, 2 other) · tok μ 30k · dur μ 35.5s',
      'review · opus · n 4 · ok 100% · tok μ — · dur μ —',
    ])
  })

  test('a cell restarted below its lifetime stats renders consistent counts', () => {
    const rows = ledgerRows([view({ n: 5, pass: 3, unavailable: 4, tokN: 5, durationN: 5, durationMean: 2000 })])
    expect(rows[0]).toContain('n 5 · ok 60% (2 unavailable)')
  })

  test('cellViews clamps lifetime stats to the cell', () => {
    const key = cellKey('explore', 'GREEN', 'sonnet')
    const cell = { n: 5, pass: 3, state: 'WARMING' as const, winN: 0, winPass: 0, below: 0, above: 0, cusum: 0, p0: null }
    const stats = { [key]: { tokens: { n: 50, mean: 1000, m2: 0 }, unavailable: { n: 50, mean: 0.5, m2: 0 } } }
    const v = cellViews({ [key]: cell }, stats)[0]!
    expect([v.unavailable, v.tokN]).toEqual([2, 5])
  })

  test('a workflow dispatch is labelled, not blank', () => {
    const rows = paneSections(empty({ ds: [d({ description: '', requested: 'unknown', taskType: 'other', subagentType: 'workflow' })] }))[0]!.rows
    expect(rows[1]).toMatch(/^workflow · other · unknown→opus/)
  })

  test('the ledger never states a label count that would suffice', () => {
    const text = JSON.stringify(paneSections(empty({ cells: [view({ n: 35, pass: 35, state: 'READY' })] })))
    expect(text).toContain('no quality label yet')
    expect(text).not.toMatch(/n\/30|\/30|needs \d+ more|eligible|READY/)
  })

  test('shedding is shown as advisory', () => {
    const rows = paneSections(empty())[1]!.rows
    expect(rows).toContain('heavy spawns ≤ 10.0/min (advisory)')
  })

  test('lanes appear with a backend', () => {
    const s = paneSections(empty({ b: { ...EMPTY_BACKEND, present: true, lanes: { codegen: { n: 4, ok: 3, escalated: 1 } }, drainEtaS: 386 } }))
    const lanes = s.find(x => x.title === 'Lanes 24h')!
    expect(lanes.rows).toEqual(['codegen ▇▇▇▇▇▇░░ 3/4 ok · 1 escalated', 'queue drains in 386 s'])
  })
})

describe('pane: hooks', () => {
  test('session.start registers /apex and opens nothing by default', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    expect(world.commands).toEqual(['apex'])
    expect(world.opened).toEqual([])
  })

  test('pane = auto opens it at start on a terminal', { options: { pane: 'auto' } }, async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    expect(world.opened).toEqual(['datapce'])
  })

  test('/apex opens the pane, which draws its sections on terminal and desktop', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput())
    const r = await $.command.run(command('apex'))
    expect(world.opened).toEqual(['datapce'])
    expect(r.text).toBe('datapce: pane open (/apex close closes it)')
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ plugin: 'datapce', surface, ...PANE })
      expect(await ui.find({ type: 'Text', text: 'Dispatches' })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: /Find pressure gate state file · explore · inherit→opus · running/ })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: /no quality label yet/ })).toBeDefined()
      await ui.unmount()
    }
  })

  test('/apex close closes the pane', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    const r = await $.command.run(command('apex', 'close'))
    expect(world.closed).toEqual(['datapce'])
    expect(r.text).toBe('datapce: pane closed')
  })

  test('/apex json answers the sections', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    const r = await $.command.run(command('apex', 'json'))
    const parsed = JSON.parse(r.text ?? '{}') as { sections: { title: string }[] }
    expect(parsed.sections.map(x => x.title)).toEqual(['Dispatches', 'Signals', 'Evidence', 'Profile'])
  })

  test('there is no enforce subcommand: it persists nothing and rewrites nothing', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', { [cellKey('explore', 'GREEN', 'sonnet')]: { n: 35, pass: 35, state: 'READY', winN: 5, winPass: 5, below: 0, above: 0, cusum: 0, p0: 1 } })
    await $.session.start(SESSION)
    const r = await $.command.run(command('apex', 'enforce on'))
    expect(r.text).toBe('datapce: unknown argument "enforce on" — usage: /apex [open|close|handoff|json]')
    expect(world.store.has('datapce.enforce')).toBe(false)
    await $.agent.spawn(spawnInput())
    expect(world.spawned[0]?.model).toBeUndefined()
  })

  test('the ledger fills from a finished dispatch', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput())
    await $.turn.complete({
      answer: 'done', durationMs: 41000, isAborted: false, turnId: 'sub-turn-1', agentId: 'agent-1', reason: 'answer',
      usage: { input_tokens: 30000, output_tokens: 11000, cache_read_input_tokens: 0, cache_creation_input_tokens: 0, model: 'claude-opus-5-5' },
    } as never)
    const r = await $.command.run(command('apex', 'json'))
    const ev = (JSON.parse(r.text ?? '{}') as { sections: { id: string; rows: string[] }[] }).sections.find(x => x.id === 'evidence')!
    expect(ev.rows[0]).toBe('explore · opus · n 1 · ok 100% · tok μ 41k · dur μ 41.0s')
  })

  test('only a known /apex subcommand is logged', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.command.run(command('apex', 'json'))
    await $.command.run(command('apex', 'my secret text'))
    await $.session.end({ sessionId: 's', reason: 'other' } as never)
    const logged = [...world.appended.values()].flat().join('\n')
    expect(logged).toContain('"args":"json"')
    expect(logged).toContain('"args":"unknown"')
    expect(logged).not.toContain('secret')
  })

  test('/apex handoff hands the model the structured block', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    const r = await $.command.run(command('apex', 'handoff'))
    expect(r.context).toEqual([HANDOFF_TEMPLATE])
  })

  test('pane = off answers as text and never opens', { options: { pane: 'off' } }, async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    const r = await $.command.run(command('apex'))
    expect(world.opened).toEqual([])
    expect(r.text?.startsWith('Dispatches\n  no dispatches yet')).toBe(true)
  })
})
