import { describe, expect, test } from 'claude-code/testing'

import type { CellView, Dispatch } from '../types/index.d.ts'
import { cellKey, cellViews } from '../hooks/evidence.ts'
import { HANDOFF_TEMPLATE } from '../hooks/handoff.ts'
import { ledgerRows, NO_LABEL, paneSections, wrapRow, type PaneInput } from '../hooks/pane.tsx'
import { EMPTY_BACKEND, EMPTY_INJECT, EMPTY_PROFILE_STORE, EMPTY_SIGNALS, profileView } from '../hooks/state.ts'
import { command, PANE, SESSION, spawnInput } from './fixtures/inputs.ts'
import { worldOf } from './fixtures/world.ts'

const view = (over: Partial<CellView>): CellView => ({
  key: 'explore|GREEN|sonnet', taskType: 'explore', level: 'GREEN', tier: 'sonnet', state: 'WARMING', n: 12, pass: 12, wilsonLo: 0.76, tokMean: 41000, tokN: 12, durationMean: 41000, durationN: 12, unavailable: 0, ...over,
})
const sec = (s: { id: string; rows: string[] }[], id: string): string[] => s.find(x => x.id === id)!.rows
const empty = (over: Partial<PaneInput> = {}): PaneInput => ({
  ds: [], s: EMPTY_SIGNALS, b: EMPTY_BACKEND, cells: [], profile: profileView(EMPTY_PROFILE_STORE), inject: EMPTY_INJECT, ...over,
})
const d = (over: Partial<Dispatch>): Dispatch => ({
  toolUseId: 't', agentId: 'a', description: 'Find X', subagentType: 'Explore', taskType: 'explore', requested: 'inherit',
  resolved: 'claude-opus-5-5', advised: null, advisedState: null, applied: false, basis: '', outcome: 'ok', startedAt: 0,
  durationMs: 41000, tokens: 12000, level: 'GREEN', ...over,
})

describe('pane: sections', () => {
  test('pressure row explains a level that is still settling', () => {
    const rows = (over: object) => paneSections(empty({ s: { ...EMPTY_SIGNALS, level: 'AMBER', ...over } })).find(x => x.id === 'now')!.rows
    expect(rows({ observed: 'GREEN', exitIn: 2 }).slice(0, 2).join(' ').replace(/ +/g, ' ')).toBe('pressure AMBER; last check GREEN; returns to GREEN after 2 more calm checks')
    expect(rows({ observed: 'AMBER', exitIn: null }).join()).not.toMatch(/last check|returns/)
    expect(rows({ observed: 'RED', exitIn: null }).join()).not.toMatch(/returns/)
  })

  test('empty: honest placeholders; no Lanes without a backend; never an anomaly or enforce section', () => {
    const s = paneSections(empty())
    expect(s.map(x => x.title)).toEqual(['Now', 'Limits and budget', 'Subagents', 'Track record', 'Session'])
    expect(sec(s, 'now')).toEqual(['GREEN · no action needed', 'advice only: datapce never changes the model; you decide'])
    expect(sec(s, 'dispatches')).toEqual(['no subagents yet'])
    expect(sec(s, 'evidence')).toEqual(['no track record yet', NO_LABEL])
    expect(JSON.stringify(s)).not.toMatch(/anomaly|enforce/i)
  })

  test('dispatch rows, the advice basis, agreement and ok count', () => {
    const s = paneSections(empty({
      ds: [
        d({ advised: 'sonnet', advisedState: 'READY', basis: 'opus→sonnet: 35/35 ok; GREEN now' }),
        d({ description: 'Review diff', taskType: 'review', outcome: 'failed', resolved: 'claude-sonnet-5-5', requested: 'sonnet' }),
      ],
    }))
    expect(sec(s, 'dispatches')).toEqual([
      '1 of 2 finished ok · 1 of 2 differ from advice (1 had advice)',
      'Review diff · review',
      '  asked sonnet, ran sonnet · failed in 41s · 12k tokens',
      'Find X · explore',
      '  asked session model, ran opus · ok in 41s · 12k tokens',
      '  advice: sonnet (opus→sonnet: 35/35 ok; GREEN now) (all pressure',
      '    levels)',
    ])
  })

  test('M5: conformance names the backend scope within 72 columns', () => {
    const rows = sec(paneSections(empty({ b: { ...EMPTY_BACKEND, conformance: 0.86 } })), 'signals')
    expect(rows).toContain('ran the tier asked for: 86% (backend log, all sessions)')
    expect(rows.every(r => r.length <= 72)).toBe(true)
  })

  test('M7: advice run counts name all pressure levels; Track record names the cell list', () => {
    const sections = paneSections(empty({ ds: [d({ advised: 'sonnet', basis: 'explore·sonnet ok 92% n 25' })], cells: [view({})] }))
    expect(sec(sections, 'dispatches')).toContain('  advice: sonnet (explore·sonnet ok 92% 25 runs) (all pressure levels)')
    expect(sec(sections, 'evidence')).toContain('scope: cell list, all pressure levels')
  })

  test('ledger: runs, ok% with error kinds, avg tokens per task type × tier, levels pooled, avg time shown', () => {
    const rows = ledgerRows([
      view({ n: 10, pass: 9, unavailable: 1, tokMean: 40000, tokN: 10, durationMean: 41000, durationN: 10 }),
      view({ key: 'explore|AMBER|sonnet', level: 'AMBER', n: 10, pass: 7, unavailable: 1, tokMean: 20000, tokN: 10, durationMean: 30000, durationN: 10 }),
      view({ key: 'review|GREEN|opus', taskType: 'review', tier: 'opus', n: 4, pass: 4, tokMean: null, tokN: 0, durationMean: null, durationN: 0 }),
    ])
    expect(rows).toEqual([
      'explore · sonnet · 20 runs · ok 80% (2 unavailable, 2 other)',
      '  avg 30k tokens · avg 35.5s',
      'review · opus · 4 runs · ok 100%',
      '  avg tokens unknown · avg time unknown',
    ])
  })

  test('a cell restarted below its lifetime stats renders consistent counts', () => {
    const rows = ledgerRows([view({ n: 5, pass: 3, unavailable: 4, tokN: 5, durationN: 5, durationMean: 2000 })])
    expect(rows[0]).toContain('5 runs · ok 60% (2 unavailable)')
  })

  test('cellViews clamps lifetime stats to the cell', () => {
    const key = cellKey('explore', 'GREEN', 'sonnet')
    const cell = { n: 5, pass: 3, state: 'WARMING' as const, winN: 0, winPass: 0, below: 0, above: 0, cusum: 0, p0: null }
    const stats = { [key]: { tokens: { n: 50, mean: 1000, m2: 0 }, unavailable: { n: 50, mean: 0.5, m2: 0 } } }
    const v = cellViews({ [key]: cell }, stats)[0]!
    expect([v.unavailable, v.tokN]).toEqual([2, 5])
  })

  test('a workflow dispatch is labelled, not blank', () => {
    const rows = sec(paneSections(empty({ ds: [d({ description: '', requested: 'unknown', taskType: 'other', subagentType: 'workflow' })] })), 'dispatches')
    expect(rows.slice(1, 3)).toEqual(['workflow · other', '  asked unknown, ran opus · ok in 41s · 12k tokens'])
  })

  test('the ledger never states a label count that would suffice', () => {
    const text = JSON.stringify(paneSections(empty({ cells: [view({ n: 35, pass: 35, state: 'READY' })] })))
    expect(text).toContain('no quality label yet')
    expect(text).not.toMatch(/n\/30|\/30|needs \d+ more|eligible|READY/)
  })

  test('shedding is shown as advisory', () => {
    const rows = sec(paneSections(empty()), 'signals')
    expect(rows).toContain('suggested: at most 10 new large subagents a minute')
  })

  test('M1: any pause alert hides standing admission advice; compact alone keeps it', () => {
    for (const level of ['GREEN', 'AMBER', 'RED'] as const) {
      const sections = paneSections(empty({ s: { ...EMPTY_SIGNALS, level }, m: { ctxPercent: 20, limitKind: 'five_hour', limitPercent: 40, resetsAt: null, costUsd: 9.1 }, budgetUsd: 10 }))
      expect(sec(sections, 'now').join(' ')).toContain('pause new subagents')
      expect(sec(sections, 'signals').join(' ')).not.toContain('suggested:')
    }
    const sections = paneSections(empty({ m: { ctxPercent: 88, limitKind: null, limitPercent: null, resetsAt: null, costUsd: null } }))
    expect(sec(sections, 'signals')).toContain('suggested: at most 10 new large subagents a minute')
  })

  test('Now: same cause and action as the band; limits row carries the measure and the budget', () => {
    const m = { ctxPercent: 61, limitKind: 'five_hour', limitPercent: 95, resetsAt: '2026-10-03T20:00:00Z', costUsd: 4.12 }
    const s = paneSections(empty({ s: { ...EMPTY_SIGNALS, level: 'RED', observed: 'RED' }, m, budgetUsd: 10 }))
    expect(sec(s, 'now')[0]).toBe('RED · 5h limit 95% · pause new subagents')
    expect(sec(s, 'signals')).toContain('5-hour limit 95% used · resets 20:00 UTC')
    expect(sec(s, 'signals')).toContain('spent $4.12 of $10 budget')
    expect(sec(s, 'signals')).toContain('failure rate vs target: last 5 min unknown, last hour unknown')
  })

  test('a long row wraps at a word to 72 columns; nothing is cut', () => {
    const long = 'a'.repeat(30) + ' ' + 'b'.repeat(30) + ' ' + 'c'.repeat(30)
    const rows = sec(paneSections(empty({ ds: [d({ description: long })] })), 'dispatches')
    expect(rows.every(r => r.length <= 72)).toBe(true)
    expect(rows.slice(1, 3)).toEqual([`${'a'.repeat(30)} ${'b'.repeat(30)}`, `  ${'c'.repeat(30)} · explore`])
  })

  test('M4: hard-split long words without lost text or rows over width', () => {
    for (const prefix of ['', '  ', 'start ']) {
      const row = `${prefix}${'x'.repeat(200)} end`
      const rows = wrapRow(row)
      expect(rows.every(r => r.length <= 72)).toBe(true)
      expect(rows.join('').replace(/ /g, '')).toBe(row.replace(/ /g, ''))
    }
    const indented = wrapRow(`${' '.repeat(69)}😀😀`)
    expect(indented.every(r => r.length <= 72)).toBe(true)
    expect(indented.join('').replace(/ /g, '')).toBe('😀😀')
    const unicode = `${'a'.repeat(71)}😀tail`
    const unicodeRows = wrapRow(unicode)
    expect(unicodeRows.every(r => r.length <= 72)).toBe(true)
    expect(unicodeRows.join('').replace(/ /g, '')).toBe(unicode)
    expect(unicodeRows.every(r => !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(r))).toBe(true)
    const sections = paneSections(empty({ ds: [d({ description: 'x'.repeat(120) })] }))
    expect(sections.flatMap(s => s.rows).every(r => r.length <= 72)).toBe(true)
  })

  test('M3: Now names a rank-99 figure dropped by the final width guard', () => {
    const sections = paneSections(empty({ m: { ctxPercent: 88, limitKind: 'weekly_opus_x', limitPercent: 95, resetsAt: null, costUsd: 123_456_789_012 } }))
    expect(sec(sections, 'now')).toContain('not in the band (no room): $123.5B')
  })

  test('Now names the figures the band dropped for room', () => {
    const m = { ctxPercent: 61, limitKind: 'five_hour', limitPercent: 62, resetsAt: null, costUsd: 4.12 }
    const ds = Array.from({ length: 7 }, (_, k) => d({ advised: k < 2 ? 'haiku' : null }))
    const s = paneSections(empty({ ds, m, budgetUsd: 10, s: { ...EMPTY_SIGNALS, level: 'AMBER' }, b: { ...EMPTY_BACKEND, present: true, conformance: 0.86 } }))
    expect(sec(s, 'now').slice(0, 2)).toEqual(['AMBER · no action needed · 2 of 7 subagents differ from advice', 'not in the band (no room): ran tier asked 86%'])
  })

  test('lanes appear with a backend', () => {
    const s = paneSections(empty({ b: { ...EMPTY_BACKEND, present: true, lanes: { codegen: { n: 4, ok: 3, escalated: 1 } }, drainEtaS: 386 } }))
    const lanes = s.find(x => x.title === 'Local models (24 h)')!
    expect(lanes.rows).toEqual(['codegen ▇▇▇▇▇▇░░ 3 of 4 ok · 1 sent to a larger model', 'queue empties in 386 s'])
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
    expect(r.text).toBe('pane open (/apex close closes it)')
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ plugin: 'datapce', surface, ...PANE })
      expect(await ui.find({ type: 'Text', text: 'Subagents' })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: 'Find pressure gate state file · explore' })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: /asked session model, ran opus · running/ })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: /no quality label yet/ })).toBeDefined()
      await ui.unmount()
    }
  })

  test('/apex close closes the pane', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    const r = await $.command.run(command('apex', 'close'))
    expect(world.closed).toEqual(['datapce'])
    expect(r.text).toBe('pane closed')
  })

  test('command output carries no datapce prefix (the host adds the plugin name)', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    for (const args of ['', 'open', 'close', 'handoff', 'bogus']) {
      const r = await $.command.run(command('apex', args))
      expect(r.text ?? '').not.toMatch(/datapce:/i)
      expect(r.text ?? '').not.toBe('')
    }
  })

  test('/apex json answers the sections', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    const r = await $.command.run(command('apex', 'json'))
    const parsed = JSON.parse(r.text ?? '{}') as { sections: { title: string }[] }
    expect(parsed.sections.map(x => x.title)).toEqual(['Now', 'Limits and budget', 'Subagents', 'Track record', 'Session'])
  })

  test('there is no enforce subcommand: it persists nothing and rewrites nothing', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', { [cellKey('explore', 'GREEN', 'sonnet')]: { n: 35, pass: 35, state: 'READY', winN: 5, winPass: 5, below: 0, above: 0, cusum: 0, p0: 1 } })
    await $.session.start(SESSION)
    const r = await $.command.run(command('apex', 'enforce on'))
    expect(r.text).toBe('unknown argument "enforce on" — usage: /apex [open|close|handoff|json]')
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
    expect(ev.rows.slice(0, 2)).toEqual(['explore · opus · 1 run · ok 100%', '  avg 41k tokens · avg 41.0s'])
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
    expect(r.text?.startsWith('Now\n  GREEN · no action needed')).toBe(true)
    expect(r.text).toContain('Subagents\n  no subagents yet')
  })
})
