// UX invariant fixture (scorer v2). Renders the band, the status entry and the /apex pane for fixed
// edge cases plus a seeded fuzz set, and prints one `UXINV {...}` line per case. Each line carries
// an ORACLE built here from the spec, not from the plugin code: which actions the user must see.
// scripts/ux_score.py checks the rendered text against the oracle. Keep the seed and the generator
// fixed: every round of the loop must score the same inputs.
//
// The imports are namespaces on purpose: the same file must also run on older commits, where some
// exports (alertsOf) do not exist yet.
import { describe, expect, test } from 'claude-code/testing'

import type { BackendView, Dispatch, Level, Measure, SignalsView } from '../types/index.d.ts'
import * as band from '../hooks/band.tsx'
import * as pane from '../hooks/pane.tsx'
import * as state from '../hooks/state.ts'

const SEED = 20261020
const FUZZ_N = 400

/** mulberry32: small, fixed, portable. */
function rng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

type Case = { id: string; m: Measure | null; s: SignalsView; ds: Dispatch[]; b: BackendView; budgetUsd: number }

const PAUSE = 'pause new subagents'
const COMPACT = 'run /compact'

/** The spec (SPEC-round1 B1, B9-B11; round-2 M2), written independently of band.tsx. */
function oracle(c: Case): { actions: string[]; first: string | null; firstKind: string | null; quotaCause: boolean } {
  const kinds: { kind: string; action: string }[] = []
  if (c.s.level === 'RED') kinds.push({ kind: 'limit', action: PAUSE })
  const cost = c.m?.costUsd ?? null
  if (c.budgetUsd > 0 && cost !== null) {
    const mins = c.s.minutesToExhaust
    if ((mins !== null && mins <= 60) || cost >= 0.9 * c.budgetUsd) kinds.push({ kind: 'budget', action: PAUSE })
  }
  const ctx = c.m?.ctxPercent ?? null
  if (ctx !== null && ctx >= 80) kinds.push({ kind: 'context', action: COMPACT })
  const lim = c.m?.limitPercent ?? null
  return {
    actions: [...new Set(kinds.map(k => k.action))],
    first: kinds[0]?.action ?? null,
    firstKind: kinds[0]?.kind ?? null,
    quotaCause: kinds[0]?.kind === 'limit' && lim !== null && lim >= 80,
  }
}

/** Figures the user must find in the band or in the pane Now section. The scorer matches each with its label. */
function facts(c: Case): Record<string, unknown> {
  const f: Record<string, unknown> = {}
  const m = c.m
  if (m?.limitPercent != null) {
    // Spec labels: the three host kinds have short names; any other kind is untrusted text, shown as
    // ASCII of 12 characters or fewer, or as plain "limit".
    const known: Record<string, string> = { five_hour: '5h', seven_day: '7d', spend_limit: 'spend' }
    const k = m.limitKind ?? ''
    const plain = k.replace(/[^A-Za-z0-9_ -]/g, '').slice(0, 12).trim()
    f.limit = { pct: Math.round(m.limitPercent), labels: [...new Set([known[k] ?? plain, 'limit'].filter(x => x !== ''))] }
  }
  if (m?.ctxPercent != null) f.ctx = Math.round(m.ctxPercent)
  if (m?.costUsd != null) {
    const x = m.costUsd
    const short = x < 1000 ? `$${x.toFixed(2)}` : x < 999_950 ? `$${+(x / 1e3).toFixed(1)}k` : x < 999_950_000 ? `$${+(x / 1e6).toFixed(1)}M` : x < 999_950_000_000 ? `$${+(x / 1e9).toFixed(1)}B` : x < 999.5e12 ? `$${Math.round(x / 1e12)}T` : '$999T+'
    f.cost = [`$${x.toFixed(2)}`, short]
  }
  if (c.ds.length > 0) f.subagents = c.ds.length
  return f
}

const TIERS = ['claude-haiku-4-5', 'claude-sonnet-5-5', 'claude-opus-5-5', 'gpt-6.1-sol', null] as const
const ADVICE = ['haiku', 'sonnet', 'opus', null] as const
const KINDS = ['five_hour', 'seven_day', 'spend_limit', 'weekly_opus_extra_long_kind', '界限'.repeat(10)]
const DESCS = ['Find pressure gate state file', 'Review diff', '', 'x'.repeat(200), '調べる'.repeat(30), 'path/' + 'a'.repeat(90)]

const pick = <T,>(r: () => number, xs: readonly T[]): T => xs[Math.floor(r() * xs.length)] as T
const maybe = <T,>(r: () => number, p: number, f: () => T): T | null => (r() < p ? f() : null)

function money(r: () => number): number {
  const band = r()
  if (band < 0.6) return Math.round(r() * 1500) / 100
  if (band < 0.8) return 1000 + r() * 1e6
  if (band < 0.95) return 1e9 + r() * 1e13
  return 1e15 * (1 + r() * 10)
}

function fuzzCase(r: () => number, k: number): Case {
  const level = pick(r, ['GREEN', 'AMBER', 'RED'] as const) as Level
  const m: Measure | null = r() < 0.08 ? null : {
    ctxPercent: maybe(r, 0.9, () => Math.round(r() * 1000) / 10),
    limitKind: pick(r, KINDS),
    limitPercent: maybe(r, 0.85, () => Math.round(r() * 1000) / 10),
    resetsAt: maybe(r, 0.7, () => '2026-10-03T20:00:00Z'),
    costUsd: maybe(r, 0.9, () => money(r)),
  }
  const budgetUsd = pick(r, [0, 0, 5, 10, 25, 1e6, 1e12])
  const nd = pick(r, [0, 0, 1, 3, 7, 30])
  const ds: Dispatch[] = Array.from({ length: nd }, (_, j) => ({
    toolUseId: `t${j}`, agentId: null, description: pick(r, DESCS), subagentType: 'Explore',
    taskType: pick(r, ['explore', 'review', 'mechanical', 'debug'] as const), requested: pick(r, ['inherit', 'sonnet', 'opus']),
    resolved: pick(r, TIERS), advised: pick(r, ADVICE), advisedState: 'READY', applied: false, basis: maybe(r, 0.5, () => 'explore·sonnet ok 92% n 25') ?? '',
    outcome: pick(r, ['ok', 'ok', 'failed', 'running'] as const), startedAt: 0, durationMs: maybe(r, 0.8, () => Math.round(r() * 300_000)),
    tokens: maybe(r, 0.8, () => Math.round(r() * 2_000_000)), level,
  }))
  const s: SignalsView = {
    ...state.EMPTY_SIGNALS,
    level,
    observed: pick(r, ['GREEN', 'AMBER', 'RED'] as const) as Level,
    exitIn: maybe(r, 0.3, () => 1 + Math.floor(r() * 4)),
    burnShort: maybe(r, 0.6, () => Math.round(r() * 50) / 10),
    burnLong: maybe(r, 0.6, () => Math.round(r() * 50) / 10),
    budgetBurn: budgetUsd > 0 ? maybe(r, 0.7, () => Math.round(r() * 100) / 10) : null,
    minutesToExhaust: budgetUsd > 0 ? maybe(r, 0.7, () => pick(r, [5, 18, 45, 59, 61, 240, 1e12])) : null,
    breakers: r() < 0.2 ? { codegen: 'open' } : {},
    fanoutRisk: maybe(r, 0.4, () => r()),
    admissionRate: Math.round(r() * 200) / 10,
  }
  const b: BackendView = r() < 0.5 ? state.EMPTY_BACKEND : {
    ...state.EMPTY_BACKEND, present: true, conformance: maybe(r, 0.7, () => r()),
    lanes: maybe(r, 0.7, () => ({ codegen: { n: 4, ok: 3, escalated: 1 } })), families: { claude: 'GREEN', codex: 'AMBER' },
  }
  return { id: `fuzz-${k}`, m, s, ds, b, budgetUsd }
}

const M = (ctx: number | null, lim: number | null, cost: number | null, kind = 'five_hour'): Measure => ({ ctxPercent: ctx, limitKind: kind, limitPercent: lim, resetsAt: null, costUsd: cost })
const S = (level: Level, over: Partial<SignalsView> = {}): SignalsView => ({ ...state.EMPTY_SIGNALS, level, observed: level, ...over })
const E = state.EMPTY_BACKEND

/** Fixed edge cases, one per defect type the round-1..3 judges found. */
const EDGE: Case[] = [
  { id: 'red-quota-low', m: M(40, 3, 1), s: S('RED'), ds: [], b: E, budgetUsd: 0 },
  { id: 'red-quota-high', m: M(40, 95, 1), s: S('RED'), ds: [], b: E, budgetUsd: 0 },
  { id: 'amber-budget-pause', m: M(45, 50, 9.1), s: S('AMBER', { minutesToExhaust: 18, budgetBurn: 7.2, admissionRate: 10 }), ds: [], b: E, budgetUsd: 10 },
  { id: 'green-ctx', m: M(88, 40, 6.4), s: S('GREEN'), ds: [], b: E, budgetUsd: 0 },
  { id: 'huge-budget', m: M(40, 10, 999_999), s: S('GREEN', { minutesToExhaust: 18 }), ds: [], b: E, budgetUsd: 1_000_000 },
  { id: 'giant-money', m: M(40, 10, 1e15), s: S('GREEN', { minutesToExhaust: 18 }), ds: [], b: E, budgetUsd: 1e15 },
  { id: 'cjk-limit-kind', m: M(40, 95, 4.12, '界'.repeat(40)), s: S('RED'), ds: [], b: E, budgetUsd: 0 },
  { id: 'three-alerts', m: M(91, 97, 9.5), s: S('RED', { minutesToExhaust: 10 }), ds: [], b: E, budgetUsd: 10 },
  { id: 'no-measure', m: null, s: S('AMBER'), ds: [], b: E, budgetUsd: 10 },
  { id: 'tiny-budget', m: M(40, 10, 4.12), s: S('GREEN'), ds: [], b: E, budgetUsd: 1e-7 },
  { id: 'huge-cost-no-budget', m: M(40, 10, 1e21), s: S('GREEN'), ds: [], b: E, budgetUsd: 0 },
  { id: 'unknown-burn', m: M(30, 20, 1), s: S('GREEN', { burnShort: null, burnLong: null }), ds: [], b: E, budgetUsd: 0 },
]

function render(c: Case) {
  const bi = { m: c.m, s: c.s, ds: c.ds, b: c.b, budgetUsd: c.budgetUsd }
  const line: string = band.bandLine(bi)
  const alerts: { action: string }[] = typeof (band as Record<string, unknown>).alertsOf === 'function' ? (band as any).alertsOf(bi) : []
  const status: string = (band as any).statusText(c.s.level, c.m?.costUsd ?? null, alerts[0])
  const pin = { ds: c.ds, s: c.s, b: c.b, cells: [], profile: state.profileView(state.EMPTY_PROFILE_STORE), inject: state.EMPTY_INJECT, m: c.m, budgetUsd: c.budgetUsd }
  const sections: { id: string; title: string; rows: string[] }[] = pane.paneSections(pin)
  return { band: line, status, sections }
}

describe('ux invariants (measurement fixture)', () => {
  test('edge cases and seeded fuzz', () => {
    const r = rng(SEED)
    const cases = [...EDGE, ...Array.from({ length: FUZZ_N }, (_, k) => fuzzCase(r, k))]
    console.log(`UXINV_META ${JSON.stringify({ n: cases.length, seed: SEED })}`)
    for (const c of cases) {
      let out: ReturnType<typeof render> | { error: string }
      try {
        out = render(c)
      } catch (e) {
        out = { error: String(e).slice(0, 200) }
      }
      const o = oracle(c)
      console.log(`UXINV ${JSON.stringify({ id: c.id, edge: !c.id.startsWith('fuzz-'), level: c.s.level, oracle: o, facts: facts(c), budget: c.budgetUsd > 0, ...out })}`)
    }
    expect(cases.length).toBe(EDGE.length + FUZZ_N)
  })
})
