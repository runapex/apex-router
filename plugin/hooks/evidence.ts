// §5 evidence: cells (task_type × pressure × tier) → advice {tier, effort, confidence, basis}.
import type { Cell, CellState, CellView, Level, Tier, Welford } from '../types/index.d.ts'
import { cellNew, cellObserve, MIN_N, TARGET } from './core/routing.ts'
import { wilson } from './core/stats.ts'
import { rankOf, TIERS, tiersBelow } from './tiers.ts'

export type Stats = Record<string, Record<string, Welford>>
export type Advice = { tier: Tier; effort: null; confidence: 'COLD' | 'WARMING' | 'READY'; own: boolean; basis: string }
export type EvidenceRow = { taskType: string; tier: Tier; n: number; passPct: number; tokMean: number | null; state: CellState }

export const SHED_TASKS: ReadonlySet<string> = new Set(['explore', 'mechanical'])
export const LEVELS: readonly Level[] = ['GREEN', 'AMBER', 'RED']

/** v1 has no answer-quality label: completion (answer && !aborted) is ~100% on real traffic, so READY
 * would reward the cheapest tier for merely finishing. Cells keep counting; READY and inherited tier
 * advice and the planning-skill table stay off until a quality label exists (v1.1, spec §18).
 * Pressure shedding is policy, not evidence, and stays on. */
export const QUALITY_LABELS = false
const STATES: readonly CellState[] = ['COLD', 'WARMING', 'READY', 'DRIFTING']

export const cellKey = (taskType: string, level: Level, tier: Tier): string => `${taskType}|${level}|${tier}`

export function parseKey(key: string): { taskType: string; level: Level; tier: Tier } | null {
  const [taskType, level, tier, extra] = key.split('|')
  if (!taskType || extra !== undefined || !LEVELS.includes(level as Level) || !TIERS.includes(tier as Tier)) return null
  return { taskType, level: level as Level, tier: tier as Tier }
}

export const wilsonLo = (pass: number, n: number): number => (n > 0 ? wilson(pass, n)[0] : 0)

export function tokMean(stats: Stats, key: string): number | null {
  const w = stats[key]?.tokens
  return w !== undefined && w.n > 0 ? w.mean : null
}

export function durationMean(stats: Stats, key: string): number | null {
  const w = stats[key]?.duration_ms
  return w !== undefined && w.n > 0 ? w.mean : null
}

/** Runs of a cell that ended "unavailable" (the stat is a 0/1 Welford: n × mean). */
export function unavailableOf(stats: Stats, key: string): number {
  const w = stats[key]?.unavailable
  return w !== undefined ? Math.round(w.n * w.mean) : 0
}

export const kTok = (t: number | null): string => (t === null ? '—' : `${Math.round(t / 1000)}k`)

export function basisOf(requested: Tier, tier: Tier, c: Cell, level: Level, tok: number | null): string {
  return `${requested}→${tier}: ${c.pass}/${c.n} ok${tok === null ? '' : `, ${kTok(tok)} tok`}; ${level} now`
}

function aggregate(cells: Record<string, Cell>, match: (p: { taskType: string; level: Level; tier: Tier }) => boolean): { n: number; pass: number } {
  let n = 0
  let pass = 0
  for (const [key, c] of Object.entries(cells)) {
    const p = parseKey(key)
    // P5: a DRIFTING cell's history no longer describes it — it lends nothing to an aggregate.
    if (p !== null && c.state !== 'DRIFTING' && match(p)) {
      n += c.n
      pass += c.pass
    }
  }
  return { n, pass }
}

export function adviceFor(
  cells: Record<string, Cell>, stats: Stats, taskType: string, level: Level, requested: Tier | null,
  quality: boolean = QUALITY_LABELS,
): Advice | null {
  if (requested === null) return null
  const lower = tiersBelow(requested)
  if (quality) {
    for (const t of lower) {
      const key = cellKey(taskType, level, t)
      const c = cells[key]
      if (c?.state === 'READY') return { tier: t, effort: null, confidence: 'READY', own: true, basis: basisOf(requested, t, c, level, tokMean(stats, key)) }
    }
    for (const t of lower) {
      const own = cells[cellKey(taskType, level, t)]
      if (own !== undefined && own.state !== 'COLD') continue
      // P5: the only parent is the same task type at other pressure levels ("all tasks" was dropped:
      // another task type's pass rate says nothing about this one).
      const agg = aggregate(cells, p => p.taskType === taskType && p.tier === t)
      if (agg.n >= MIN_N && wilsonLo(agg.pass, agg.n) >= TARGET) {
        return { tier: t, effort: null, confidence: 'WARMING', own: false, basis: `${requested}→${t}: inherited from ${taskType} all levels, ${agg.pass}/${agg.n} ok` }
      }
    }
  }
  if (level !== 'GREEN' && SHED_TASKS.has(taskType) && lower.length > 0) {
    const t = lower[lower.length - 1]!
    const c = cells[cellKey(taskType, level, t)]
    const confidence = c === undefined || c.state === 'COLD' ? 'COLD' : 'WARMING'
    return { tier: t, effort: null, confidence, own: false, basis: `${level} shed: ${requested}→${t}${c ? ` (${c.n}/${MIN_N})` : ''}` }
  }
  return null
}

export function recordOutcome(cells: Record<string, Cell>, key: string, passed: boolean): { before: CellState; after: CellState } {
  const prev = cells[key] ?? cellNew()
  const next = cellObserve(prev, passed)
  cells[key] = next
  return { before: prev.state, after: next.state }
}

export function cellViews(cells: Record<string, Cell>, stats: Stats): CellView[] {
  const views: CellView[] = []
  for (const [key, c] of Object.entries(cells)) {
    const p = parseKey(key)
    if (p === null) continue
    // Stats are lifetime; n/pass restart on a rebase (or the stats were dropped): clamp to the cell.
    views.push({
      key, taskType: p.taskType, level: p.level, tier: p.tier, state: c.state, n: c.n, pass: c.pass, wilsonLo: wilsonLo(c.pass, c.n),
      tokMean: tokMean(stats, key), tokN: Math.min(c.n, stats[key]?.tokens?.n ?? 0), durationMean: durationMean(stats, key),
      durationN: Math.min(c.n, stats[key]?.duration_ms?.n ?? 0), unavailable: Math.min(c.n - c.pass, unavailableOf(stats, key)),
    })
  }
  return views.sort(
    (a, b) => a.taskType.localeCompare(b.taskType) || LEVELS.indexOf(a.level) - LEVELS.indexOf(b.level) || rankOf(a.tier) - rankOf(b.tier),
  )
}

/** One row per task type at `level`: the cheapest READY tier, else the busiest non-COLD cell. COLD → no row. */
export function evidenceRows(cells: Record<string, Cell>, stats: Stats, level: Level): EvidenceRow[] {
  const best = new Map<string, CellView>()
  for (const v of cellViews(cells, stats)) {
    if (v.level !== level || v.state === 'COLD') continue
    const cur = best.get(v.taskType)
    const better =
      cur === undefined ||
      (v.state === 'READY' && (cur.state !== 'READY' || rankOf(v.tier) < rankOf(cur.tier))) ||
      (v.state !== 'READY' && cur.state !== 'READY' && v.n > cur.n)
    if (better) best.set(v.taskType, v)
  }
  return [...best.values()].map(v => ({
    taskType: v.taskType,
    tier: v.tier,
    n: v.n,
    passPct: v.n > 0 ? Math.round((100 * v.pass) / v.n) : 0,
    tokMean: v.tokMean,
    state: v.state,
  }))
}

const isCount = (x: unknown): x is number => typeof x === 'number' && Number.isInteger(x) && x >= 0
const isNum = (x: unknown): x is number => typeof x === 'number' && Number.isFinite(x)

export function asCells(v: unknown): Record<string, Cell> {
  const out: Record<string, Cell> = {}
  if (v === null || typeof v !== 'object' || Array.isArray(v)) return out
  for (const [key, raw] of Object.entries(v as Record<string, unknown>)) {
    if (parseKey(key) === null || raw === null || typeof raw !== 'object') continue
    const c = raw as Record<string, unknown>
    const ok =
      isCount(c.n) && isCount(c.pass) && c.pass <= c.n && STATES.includes(c.state as CellState) &&
      isCount(c.winN) && isCount(c.winPass) && isCount(c.below) && isCount(c.above) && isNum(c.cusum) &&
      (c.p0 === null || isNum(c.p0))
    if (ok) out[key] = c as unknown as Cell
  }
  return out
}

export function asStats(v: unknown): Stats {
  const out: Stats = Object.create(null) as Stats
  if (v === null || typeof v !== 'object' || Array.isArray(v)) return out
  for (const [key, metrics] of Object.entries(v as Record<string, unknown>)) {
    if (key === '__proto__' || metrics === null || typeof metrics !== 'object') continue
    const kept: Record<string, Welford> = Object.create(null) as Record<string, Welford>
    for (const [metric, w] of Object.entries(metrics as Record<string, unknown>)) {
      if (metric === '__proto__') continue
      const r = w as Record<string, unknown> | null
      if (r !== null && typeof r === 'object' && isCount(r.n) && isNum(r.mean) && isNum(r.m2)) kept[metric] = { n: r.n, mean: r.mean, m2: r.m2 }
    }
    if (Object.keys(kept).length > 0) out[key] = kept
  }
  return out
}
