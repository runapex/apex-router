// §6 pane (/apex), advise-only (pivot P1/P5/P6): Dispatches · Signals · Lanes 24h (backend) ·
// Evidence (the cost/availability ledger) · Profile. No enforce switch, no anomaly card.
import type { On } from 'claude-code'
import { atom, read } from 'claude-code'

import type { BackendView, CellView, Dispatch, InjectView, ProfileView, SignalsView, Tier } from '../types/index.d.ts'
import { meter } from './band.tsx'
import { kTok } from './evidence.ts'
import { HANDOFF_TEMPLATE } from './handoff.ts'
import type { Host } from './host.ts'
import { record } from './observe.ts'
import type { Runtime } from './runtime.ts'
import { EMPTY_BACKEND, EMPTY_INJECT, EMPTY_PROFILE_STORE, EMPTY_SIGNALS, PANE_ID, profileView } from './state.ts'
import { rankOf, tierOf } from './tiers.ts'

const dispatches = atom({ plugin: 'datapce', key: 'dispatches' } as const, [] as Dispatch[])
const signals = atom({ plugin: 'datapce', key: 'signals' } as const, EMPTY_SIGNALS)
const backend = atom({ plugin: 'datapce', key: 'backend' } as const, EMPTY_BACKEND)
const cells = atom({ plugin: 'datapce', key: 'cells' } as const, [] as CellView[])
const profile = atom({ plugin: 'datapce', key: 'profile' } as const, profileView(EMPTY_PROFILE_STORE))
const inject = atom({ plugin: 'datapce', key: 'inject' } as const, EMPTY_INJECT)

const DISPATCH_ROWS = 20
const USAGE = 'usage: /apex [open|close|handoff|json]'
export const NO_LABEL = 'no quality label yet — ok% is completion and availability, not answer quality'

const KNOWN_ARGS: readonly string[] = ['open', 'close', 'handoff', 'json']
/** §10: only a known subcommand is logged; anything else the user typed is not. */
const loggedArg = (args: string): string => {
  const w = args.split(/\s+/)[0] ?? ''
  return KNOWN_ARGS.includes(w) ? w : w === '' ? '' : 'unknown'
}

export type PaneInput = {
  ds: readonly Dispatch[]
  s: SignalsView
  b: BackendView
  cells: readonly CellView[]
  profile: ProfileView
  inject: InjectView
}

export type Section = { id: string; title: string; rows: string[] }

const secs = (ms: number | null): string => (ms === null ? '' : ` ${Math.round(ms / 1000)}s`)
const fixed = (x: number | null, digits = 1): string => (x === null ? '—' : x.toFixed(digits))
const durMu = (ms: number | null): string => (ms === null ? '—' : ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`)

function dispatchRows(ds: readonly Dispatch[]): string[] {
  if (ds.length === 0) return ['no dispatches yet']
  const advised = ds.filter(d => d.advised !== null)
  const agree = advised.filter(d => d.advised === tierOf(d.resolved)).length
  const done = ds.filter(d => d.outcome !== 'running')
  const rows = [`agreement ${agree}/${advised.length} with advice · ok ${done.filter(d => d.outcome === 'ok').length}/${done.length}`]
  for (const d of [...ds].reverse().slice(0, DISPATCH_ROWS)) {
    const ran = tierOf(d.resolved) ?? d.resolved ?? '—'
    const tok = d.tokens === null ? '' : ` ${kTok(d.tokens)}`
    const delta = d.advised !== null && d.advised !== tierOf(d.resolved) ? ` ▲ ${d.advised}: ${d.basis}` : ''
    rows.push(`${d.description === '' ? 'workflow' : d.description} · ${d.taskType} · ${d.requested}→${ran} · ${d.outcome}${secs(d.durationMs)}${tok}${delta}`)
  }
  return rows
}

function signalRows(s: SignalsView, b: BackendView): string[] {
  const rows = [`pressure ${s.level} · burn 5m ${fixed(s.burnShort)} / 60m ${fixed(s.burnLong)}`]
  const families = Object.entries(b.families)
  if (families.length > 0) rows.push(`families: ${families.map(([f, l]) => `${f} ${l}`).join(', ')}`)
  const lanes = Object.entries(s.breakers)
  if (lanes.length > 0) rows.push(`breakers: ${lanes.map(([l, st]) => `${l} ${st}`).join(', ')}`)
  if (s.budgetBurn !== null) rows.push(`budget burn ${fixed(s.budgetBurn)}× · exhausts in ${s.minutesToExhaust === null ? '—' : `${Math.round(s.minutesToExhaust)} min`}`)
  rows.push(`heavy spawns ≤ ${fixed(s.admissionRate)}/min (advisory)`)
  if (s.fanoutRisk !== null) rows.push(`fan-out: P(any slow) ${(100 * s.fanoutRisk).toFixed(1)}%`)
  return rows
}

function laneRows(b: BackendView): string[] {
  const rows = Object.entries(b.lanes ?? {}).map(([lane, x]) => `${lane} ${meter(x.n > 0 ? (100 * x.ok) / x.n : 0)} ${x.ok}/${x.n} ok · ${x.escalated} escalated`)
  if (b.drainEtaS !== null) rows.push(`queue drains in ${b.drainEtaS} s`)
  return rows.length > 0 ? rows : ['no lane activity in 24 h']
}

/**
 * The ledger: one row per task type × tier (pressure levels pooled): n, ok% with the kinds of the
 * failures, token mean, duration mean.
 */
export function ledgerRows(cs: readonly CellView[]): string[] {
  type Acc = { taskType: string; tier: Tier; n: number; pass: number; unavailable: number; tokN: number; tokSum: number; durN: number; durSum: number }
  const acc = new Map<string, Acc>()
  for (const c of cs) {
    const key = `${c.taskType}|${c.tier}`
    const a = acc.get(key) ?? { taskType: c.taskType, tier: c.tier, n: 0, pass: 0, unavailable: 0, tokN: 0, tokSum: 0, durN: 0, durSum: 0 }
    a.n += c.n
    a.pass += c.pass
    a.unavailable += Math.min(c.unavailable, c.n - c.pass)
    if (c.tokMean !== null) {
      a.tokN += c.tokN
      a.tokSum += c.tokMean * c.tokN
    }
    if (c.durationMean !== null) {
      a.durN += c.durationN
      a.durSum += c.durationMean * c.durationN
    }
    acc.set(key, a)
  }
  return [...acc.values()]
    .filter(a => a.n > 0)
    .sort((x, y) => x.taskType.localeCompare(y.taskType) || rankOf(x.tier) - rankOf(y.tier))
    .map(a => {
      const failed = a.n - a.pass
      const kinds = [
        a.unavailable > 0 ? `${a.unavailable} unavailable` : '',
        failed - a.unavailable > 0 ? `${failed - a.unavailable} other` : '',
      ].filter(k => k !== '')
      const ok = `ok ${Math.round((100 * a.pass) / a.n)}%${kinds.length > 0 ? ` (${kinds.join(', ')})` : ''}`
      return `${a.taskType} · ${a.tier} · n ${a.n} · ${ok} · tok(all) μ ${kTok(a.tokN > 0 ? a.tokSum / a.tokN : null)} · dur μ ${durMu(a.durN > 0 ? a.durSum / a.durN : null)}`
    })
}

function evidenceRows(cs: readonly CellView[]): string[] {
  const rows = ledgerRows(cs)
  return [...(rows.length > 0 ? rows : ['no finished dispatches yet']), NO_LABEL]
}

function profileRows(p: ProfileView, b: BackendView, inj: InjectView): string[] {
  const top = (m: Record<string, number>): string =>
    Object.entries(m)
      .sort((x, y) => y[1] - x[1])
      .slice(0, 5)
      .map(([k, n]) => `${k} ${n}`)
      .join(', ') || '—'
  return [
    `task mix: ${top(p.taskMix)}`,
    `skills: ${top(p.skills)}`,
    `repos seen: ${p.repos}`,
    `backend: ${b.present ? `present${b.health === null ? '' : ` · health A=${b.health.toFixed(3)}`}` : 'absent'}`,
    `injection: ${inj.arm} arm · ${inj.bytes} B in ${inj.sections} sections`,
  ]
}

export function paneSections(i: PaneInput): Section[] {
  const out: Section[] = [
    { id: 'dispatches', title: 'Dispatches', rows: dispatchRows(i.ds) },
    { id: 'signals', title: 'Signals', rows: signalRows(i.s, i.b) },
  ]
  if (i.b.present) out.push({ id: 'lanes', title: 'Lanes 24h', rows: laneRows(i.b) })
  out.push({ id: 'evidence', title: 'Evidence', rows: evidenceRows(i.cells) })
  out.push({ id: 'profile', title: 'Profile', rows: profileRows(i.profile, i.b, i.inject) })
  return out
}

export const summaryText = (sections: readonly Section[]): string => sections.map(s => [s.title, ...s.rows.map(r => `  ${r}`)].join('\n')).join('\n')

/** session.start (last): register /apex; open the pane unasked only when pane = auto. */
export async function start(host: Host, rt: Runtime): Promise<void> {
  try {
    await host.registerCommand({
      name: 'apex',
      description: 'datapce: dispatches, signals, lanes, evidence, profile',
      argumentHint: '[open|close|handoff|json]',
      immediate: true,
    })
    if (rt.options.pane === 'auto' && rt.surface === 'terminal') await host.open({ id: PANE_ID, title: 'datapce' })
  } catch {
    // no command, no pane: the band and status still work
  }
}

export function install(on: On, rt: Runtime): void {
  on('command.run', { command: 'apex' }, async ($, e) => {
    const args = e.args.trim().toLowerCase()
    record(rt, { ev: 'command', ts: await $.clock.now(), command: 'apex', args: loggedArg(args) })
    if (args === 'handoff') {
      return { text: 'datapce: handoff block added — fill every field, then start a fresh session.', context: [HANDOFF_TEMPLATE] }
    }
    if (args === 'close') {
      await $.ui.close({ id: PANE_ID })
      return { text: 'datapce: pane closed' }
    }
    if (args !== '' && args !== 'open' && args !== 'json') return { text: `datapce: unknown argument "${args}" — ${USAGE}` }
    const sections = paneSections({
      ds: await read($, dispatches),
      s: await read($, signals),
      b: await read($, backend),
      cells: await read($, cells),
      profile: await read($, profile),
      inject: await read($, inject),
    })
    if (args === 'json') return { text: JSON.stringify({ sections }) }
    if (rt.options.pane === 'off') return { text: summaryText(sections) }
    await $.ui.open({ id: PANE_ID, title: 'datapce', focus: true, closeOnEscape: true })
    return { text: 'datapce: pane open (/apex close closes it)' }
  })

  on('ui.render', { component: 'Pane', requestId: 'datapce' }, async ($, e) => {
    const sections = paneSections({
      ds: await read($, dispatches),
      s: await read($, signals),
      b: await read($, backend),
      cells: await read($, cells),
      profile: await read($, profile),
      inject: await read($, inject),
    })
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box key="datapce-pane" flexDirection="column">
        {sections.map(s => (
          <Box key={`datapce-pane-${s.id}`} flexDirection="column" marginBottom={1}>
            <Text bold>{s.title}</Text>
            {s.rows.map(r => (
              <Text wrap="truncate-end">{r}</Text>
            ))}
          </Box>
        ))}
      </Box>
    )
  })
}
