// §6 pane (/apex), advise-only: Now · Limits and budget · Subagents · Local models (24 h, backend) ·
// Track record (the cost/availability ledger) · Session. No enforce switch, no anomaly card.
import type { On } from 'claude-code'
import { atom, read } from 'claude-code'

import type { BackendView, CellView, Dispatch, InjectView, Measure, ProfileView, SignalsView, Tier } from '../types/index.d.ts'
import { alertsOf, bandParts, differsOf, meter, usd } from './band.tsx'
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
const measure = atom({ plugin: 'datapce', key: 'measure' } as const, null as Measure | null)

const DISPATCH_ROWS = 10
const USAGE = 'usage: /apex [open|close|handoff|json]'
export const NO_LABEL = 'no quality label yet: ok% counts finished runs, not answer quality'

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
  /** The latest session.measure figures (null before the first one). */
  m?: Measure | null
  /** userConfig budgetUsd (0 = no budget). */
  budgetUsd?: number
}

export type Section = { id: string; title: string; rows: string[] }

const secs = (ms: number | null): string => (ms === null ? '' : ` in ${Math.round(ms / 1000)}s`)
const fixed = (x: number | null, digits = 1): string => (x === null ? 'unknown' : x.toFixed(digits))
const durMu = (ms: number | null): string => (ms === null ? 'unknown' : ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`)
const plural = (n: number, w: string): string => `${n} ${w}${n === 1 ? '' : 's'}`
/** The router's basis note in plain words: `n 25` → `25 runs`. */
const plainBasis = (basis: string): string => basis.replace(/\bn (\d+)\b/g, (_, n: string) => plural(Number(n), 'run'))

/** "Now": one summary row per condition, the same words as the band. */
function nowRows(i: PaneInput): string[] {
  const alerts = alertsOf({ m: i.m ?? null, s: i.s, budgetUsd: i.budgetUsd ?? 0 })
  const rows = alerts.map(a => `${i.s.level} · ${a.cause} · ${a.action}`)
  if (i.s.exitIn !== null && i.s.observed !== i.s.level) rows.push(`pressure ${i.s.level}; last check ${i.s.observed}; returns to ${i.s.observed} after ${plural(i.s.exitIn, 'more calm check')}`)
  if (rows.length === 0) {
    const differs = differsOf(i.ds)
    rows.push(`${i.s.level} · no action needed${differs > 0 ? ` · ${differs} of ${plural(i.ds.length, 'subagent')} differ from advice` : ''}`)
  }
  const { dropped } = bandParts({ m: i.m ?? null, s: i.s, ds: i.ds, b: i.b, budgetUsd: i.budgetUsd ?? 0 })
  if (dropped.length > 0) rows.push(`not in the band (no room): ${dropped.join(' · ')}`)
  rows.push('advice only: datapce never changes the model; you decide')
  return rows
}

function dispatchRows(ds: readonly Dispatch[]): string[] {
  if (ds.length === 0) return ['no subagents yet']
  const advised = ds.filter(d => d.advised !== null)
  const agree = advised.filter(d => d.advised === tierOf(d.resolved)).length
  const done = ds.filter(d => d.outcome !== 'running')
  const running = ds.length - done.length
  const tally = `${done.filter(d => d.outcome === 'ok').length} of ${done.length} finished ok${running > 0 ? `, ${running} running` : ''}`
  const differ = advised.length - agree
  const rows = [advised.length > 0 ? `${tally} · ${differ} of ${ds.length} differ from advice (${advised.length} had advice)` : `${tally} · no model advice recorded`]
  for (const d of [...ds].reverse().slice(0, DISPATCH_ROWS)) {
    const ran = tierOf(d.resolved) ?? d.resolved ?? 'unknown'
    const asked = d.requested === 'inherit' ? 'session model' : d.requested
    const tok = d.tokens === null ? '' : ` · ${kTok(d.tokens)} tokens`
    rows.push(`${d.description === '' ? 'workflow' : d.description} · ${d.taskType}`)
    rows.push(`  asked ${asked}, ran ${ran} · ${d.outcome}${secs(d.durationMs)}${tok}`)
    if (d.advised !== null) rows.push(d.advised === tierOf(d.resolved) ? `  advice: ${d.advised} (matched)` : `  advice: ${d.advised} (${plainBasis(d.basis)})`)
  }
  return rows
}

const BREAKER: Record<string, string> = { open: 'paused after failures', 'half-open': 'trying again', closed: 'ok' }

function signalRows(s: SignalsView, b: BackendView, m: Measure | null, budgetUsd: number): string[] {
  const rows = [`pressure ${s.level}`]
  if (m != null) {
    if (m.limitPercent !== null) rows.push(`${m.limitKind === 'five_hour' ? '5-hour' : m.limitKind === 'seven_day' ? '7-day' : (m.limitKind ?? '')} limit ${Math.round(m.limitPercent)}% used${m.resetsAt === null ? '' : ` · resets ${m.resetsAt.slice(11, 16)} UTC`}`)
    if (m.ctxPercent !== null) rows.push(`context ${Math.round(m.ctxPercent)}% full`)
    if (m.costUsd !== null) rows.push(budgetUsd > 0 ? `spent ${usd(m.costUsd)} of $${budgetUsd} budget` : `spent ${usd(m.costUsd)} (no budget set)`)
  }
  if (s.budgetBurn !== null) rows.push(`spend pace ${fixed(s.budgetBurn)}× your daily budget pace${s.minutesToExhaust === null ? '' : ` · empty in about ${Math.round(s.minutesToExhaust)} min at this rate`}`)
  const times = (x: number | null): string => (x === null ? 'unknown' : `${x.toFixed(1)}×`)
  rows.push(`failure rate vs target: last 5 min ${times(s.burnShort)}, last hour ${times(s.burnLong)}`)
  const families = Object.entries(b.families)
  if (families.length > 0) rows.push(`model families: ${families.map(([f, l]) => `${f} ${l}`).join(', ')}`)
  for (const [l, st] of Object.entries(s.breakers)) rows.push(`lane ${l}: ${BREAKER[st] ?? st}`)
  if (s.level !== 'RED') rows.push(`suggested: at most ${+s.admissionRate.toFixed(1)} new large subagents a minute`)
  if (s.fanoutRisk !== null) rows.push(`chance at least one parallel subagent is slow: ${(100 * s.fanoutRisk).toFixed(0)}%`)
  if (b.conformance !== null) rows.push(`ran the tier asked for ${Math.round(100 * b.conformance)}%`)
  return rows
}

function laneRows(b: BackendView): string[] {
  const rows = Object.entries(b.lanes ?? {}).map(([lane, x]) => `${lane} ${meter(x.n > 0 ? (100 * x.ok) / x.n : 0)} ${x.ok} of ${x.n} ok · ${x.escalated} sent to a larger model`)
  if (b.drainEtaS !== null) rows.push(`queue empties in ${b.drainEtaS} s`)
  return rows.length > 0 ? rows : ['no local model work in 24 h']
}

/**
 * The ledger: per task type × tier (pressure levels pooled), two rows: runs, ok% with the kinds of
 * the failures; then average tokens (all, cache included) and average time.
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
    .flatMap(a => {
      const failed = a.n - a.pass
      const kinds = [
        a.unavailable > 0 ? `${a.unavailable} unavailable` : '',
        failed - a.unavailable > 0 ? `${failed - a.unavailable} other` : '',
      ].filter(k => k !== '')
      const ok = `ok ${Math.round((100 * a.pass) / a.n)}%${kinds.length > 0 ? ` (${kinds.join(', ')})` : ''}`
      const tok = a.tokN > 0 ? `${kTok(a.tokSum / a.tokN)} tokens` : 'tokens unknown'
      const dur = a.durN > 0 ? durMu(a.durSum / a.durN) : 'time unknown'
      return [`${a.taskType} · ${a.tier} · ${plural(a.n, 'run')} · ${ok}`, `  avg ${tok} · avg ${dur}`]
    })
}

function evidenceRows(cs: readonly CellView[], ds: readonly Dispatch[]): string[] {
  const rows = ledgerRows(cs)
  const finished = ds.some(d => d.outcome !== 'running')
  return [...(rows.length > 0 ? rows : [finished ? 'no track record yet (updates after the session ledger saves)' : 'no track record yet']), NO_LABEL]
}

function profileRows(p: ProfileView, b: BackendView, inj: InjectView): string[] {
  const top = (m: Record<string, number>): string =>
    Object.entries(m)
      .sort((x, y) => y[1] - x[1])
      .slice(0, 5)
      .map(([k, n]) => `${k} ${n}`)
      .join(', ') || 'unknown'
  return [
    `task mix: ${top(p.taskMix)}`,
    `skills: ${top(p.skills)}`,
    `repos seen: ${p.repos}`,
    `backend: ${b.present ? `present${b.health === null ? '' : ` · availability ${b.health.toFixed(3)}`}` : 'absent'}`,
    `evidence added to prompts: ${inj.bytes} bytes in ${plural(inj.sections, 'section')} (group: ${inj.arm})`,
  ]
}

/** Pane body width (the docked pane): every row fits it. */
export const PANE_WIDTH = 72

/** Wrap a row at a word to PANE_WIDTH; continuation rows keep the indent plus two spaces. Nothing is cut. */
export function wrapRow(row: string, width = PANE_WIDTH): string[] {
  if (row.length <= width) return [row]
  const lead = /^ */.exec(row)![0]
  const out: string[] = []
  let line = lead
  for (const w of row.trim().split(' ')) {
    const next = line.trim() === '' ? `${line}${w}` : `${line} ${w}`
    if (next.length > width && line.trim() !== '') {
      out.push(line)
      line = `${lead}  ${w}`
    } else line = next
  }
  out.push(line)
  return out
}

export function paneSections(i: PaneInput): Section[] {
  return paneSectionsRaw(i).map(x => ({ ...x, rows: x.rows.flatMap(r => wrapRow(r)) }))
}

function paneSectionsRaw(i: PaneInput): Section[] {
  const out: Section[] = [
    { id: 'now', title: 'Now', rows: nowRows(i) },
    { id: 'signals', title: 'Limits and budget', rows: signalRows(i.s, i.b, i.m ?? null, i.budgetUsd ?? 0) },
    { id: 'dispatches', title: 'Subagents', rows: dispatchRows(i.ds) },
  ]
  if (i.b.present) out.push({ id: 'lanes', title: 'Local models (24 h)', rows: laneRows(i.b) })
  out.push({ id: 'evidence', title: 'Track record', rows: evidenceRows(i.cells, i.ds) })
  out.push({ id: 'profile', title: 'Session', rows: profileRows(i.profile, i.b, i.inject) })
  return out
}

export const summaryText = (sections: readonly Section[]): string => sections.map(s => [s.title, ...s.rows.map(r => `  ${r}`)].join('\n')).join('\n')

/** session.start (last): register /apex; open the pane unasked only when pane = auto. */
export async function start(host: Host, rt: Runtime): Promise<void> {
  try {
    await host.registerCommand({
      name: 'apex',
      description: 'datapce: now, limits and budget, subagents, local models, track record, session',
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
      return { text: 'handoff block added — fill every field, then start a fresh session.', context: [HANDOFF_TEMPLATE] }
    }
    if (args === 'close') {
      await $.ui.close({ id: PANE_ID })
      return { text: 'pane closed' }
    }
    if (args !== '' && args !== 'open' && args !== 'json') return { text: `unknown argument "${args}" — ${USAGE}` }
    const sections = paneSections({
      ds: await read($, dispatches),
      s: await read($, signals),
      b: await read($, backend),
      cells: await read($, cells),
      profile: await read($, profile),
      inject: await read($, inject),
      m: await read($, measure),
      budgetUsd: rt.options.budgetUsd,
    })
    if (args === 'json') return { text: JSON.stringify({ sections }) }
    if (rt.options.pane === 'off') return { text: summaryText(sections) }
    await $.ui.open({ id: PANE_ID, title: 'datapce', focus: true, closeOnEscape: true })
    return { text: 'pane open (/apex close closes it)' }
  })

  on('ui.render', { component: 'Pane', requestId: 'datapce' }, async ($, e) => {
    const sections = paneSections({
      ds: await read($, dispatches),
      s: await read($, signals),
      b: await read($, backend),
      cells: await read($, cells),
      profile: await read($, profile),
      inject: await read($, inject),
      m: await read($, measure),
      budgetUsd: rt.options.budgetUsd,
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
