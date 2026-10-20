// UX measurement fixture (RSI loop, 2026-10). Renders the band, the status entry and the /apex pane
// for fixed scenarios and prints one `UXJSON {...}` line per scenario. scripts/ux_score.py reads
// these lines and scores them. Keep the scenarios fixed: every round of the loop must score the
// same inputs, or the rounds cannot be compared.
import { describe, expect, test } from 'claude-code/testing'

import type { BackendView, CellView, Dispatch, Measure, SignalsView } from '../types/index.d.ts'
import { bandLine, statusText } from '../hooks/band.tsx'
import { paneSections } from '../hooks/pane.tsx'
import { EMPTY_BACKEND, EMPTY_INJECT, EMPTY_PROFILE_STORE, EMPTY_SIGNALS, profileView } from '../hooks/state.ts'

const d = (over: Partial<Dispatch>): Dispatch => ({
  toolUseId: 't', agentId: null, description: 'Find pressure gate state file', subagentType: 'Explore', taskType: 'explore',
  requested: 'inherit', resolved: 'claude-opus-5-5', advised: null, advisedState: null, applied: false, basis: '',
  outcome: 'ok', startedAt: 0, durationMs: 41000, tokens: 41000, level: 'GREEN', ...over,
})

const cell = (over: Partial<CellView>): CellView => ({
  key: 'k', taskType: 'explore', level: 'GREEN', tier: 'sonnet', state: 'WARMING', n: 12, pass: 11, wilsonLo: 0.6,
  tokMean: 38000, tokN: 12, durationMean: 52000, durationN: 12, unavailable: 1, ...over,
})

type Scenario = {
  id: string
  /** What a user must be able to read from the band (checked by the scorer as plain facts). */
  facts: Record<string, string | number>
  /** True when the user must act now (the band must then say what to do). */
  actionNeeded: boolean
  m: Measure | null
  s: SignalsView
  ds: Dispatch[]
  b: BackendView
  cells: CellView[]
  budgetUsd: number
}

const M = (ctx: number, lim: number, cost: number): Measure => ({ ctxPercent: ctx, limitKind: 'five_hour', limitPercent: lim, resetsAt: '2026-10-03T20:00:00Z', costUsd: cost })

const BACK: BackendView = { ...EMPTY_BACKEND, present: true, conformance: 0.86, lanes: { codegen: { n: 4, ok: 3, escalated: 1 } }, families: { claude: 'GREEN', codex: 'AMBER' } }

const SCENARIOS: Scenario[] = [
  {
    id: 'fresh', facts: { level: 'GREEN', ctx: 10, cost: '0.12' }, actionNeeded: false,
    m: M(10, 3, 0.12), s: EMPTY_SIGNALS, ds: [], b: EMPTY_BACKEND, cells: [], budgetUsd: 0,
  },
  {
    id: 'calm-fanout', facts: { level: 'GREEN', limit: 22, ctx: 34, cost: '1.80', subagents: 3 }, actionNeeded: false,
    m: M(34, 22, 1.8), s: EMPTY_SIGNALS,
    ds: [d({}), d({ description: 'Review diff', taskType: 'review', resolved: 'claude-sonnet-5-5' }), d({ outcome: 'running', durationMs: null, tokens: null })],
    b: EMPTY_BACKEND, cells: [cell({})], budgetUsd: 0,
  },
  {
    id: 'advice-differs', facts: { level: 'AMBER', limit: 62, ctx: 61, cost: '4.12', budget: 10, subagents: 7, differs: 2, conformance: 86 }, actionNeeded: false,
    m: M(61, 62, 4.12), s: { ...EMPTY_SIGNALS, level: 'AMBER', observed: 'AMBER', burnShort: 1.4, burnLong: 1.1 },
    ds: [
      d({ advised: 'sonnet', advisedState: 'READY', basis: 'explore·sonnet ok 92% n 25' }),
      d({ advised: 'haiku', advisedState: 'READY', resolved: 'claude-sonnet-5-5', basis: 'mechanical·haiku ok 95% n 40', taskType: 'mechanical' }),
      d({ advised: 'sonnet', resolved: 'claude-sonnet-5-5' }), d({}), d({}), d({ outcome: 'failed' }), d({}),
    ],
    b: BACK, cells: [cell({}), cell({ tier: 'opus', n: 30, pass: 27, unavailable: 2 }), cell({ taskType: 'review', tier: 'sonnet', n: 5, pass: 5, unavailable: 0 })], budgetUsd: 10,
  },
  {
    id: 'red-limit', facts: { level: 'RED', limit: 95, ctx: 61, cost: '4.12' }, actionNeeded: true,
    m: M(61, 95, 4.12), s: { ...EMPTY_SIGNALS, level: 'RED', observed: 'RED', burnShort: 3.2, burnLong: 2.4, breakers: { codegen: 'open' }, fanoutRisk: 0.31 },
    ds: [d({}), d({ outcome: 'failed' }), d({ outcome: 'failed' })], b: EMPTY_BACKEND, cells: [cell({})], budgetUsd: 0,
  },
  {
    id: 'context-full', facts: { level: 'GREEN', ctx: 88, cost: '6.40' }, actionNeeded: true,
    m: M(88, 40, 6.4), s: EMPTY_SIGNALS, ds: [d({})], b: EMPTY_BACKEND, cells: [], budgetUsd: 0,
  },
  {
    id: 'budget-burn', facts: { level: 'AMBER', cost: '9.10', budget: 10, ctx: 45 }, actionNeeded: true,
    m: M(45, 50, 9.1), s: { ...EMPTY_SIGNALS, level: 'AMBER', observed: 'GREEN', exitIn: 2, budgetBurn: 2.6, minutesToExhaust: 18 },
    ds: [d({}), d({})], b: BACK, cells: [cell({})], budgetUsd: 10,
  },
]

describe('ux scenarios (measurement fixture)', () => {
  for (const sc of SCENARIOS) {
    test(sc.id, () => {
      const band = bandLine({ m: sc.m, s: sc.s, ds: sc.ds, b: sc.b, budgetUsd: sc.budgetUsd })
      const status = statusText(sc.s.level, sc.m?.costUsd ?? null)
      // m and budgetUsd ride along so a pane that wants a "Now" summary can read them (extra fields are ignored otherwise).
      const pin = { ds: sc.ds, s: sc.s, b: sc.b, cells: sc.cells, profile: profileView(EMPTY_PROFILE_STORE), inject: EMPTY_INJECT, m: sc.m, budgetUsd: sc.budgetUsd }
      const sections = paneSections(pin)
      console.log(`UXJSON ${JSON.stringify({ id: sc.id, facts: sc.facts, actionNeeded: sc.actionNeeded, band, status, sections })}`)
      expect(band.length).toBeGreaterThan(0)
    })
  }
})
