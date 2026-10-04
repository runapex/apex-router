// Live signals (§6–§7): one tick per minute closes the counters into windows and recomputes what
// the band, pane, router and injection read. Toasts here: RED pressure, a lane breaker opening,
// budget burn(short) > 10 ∧ burn(long) > 6 — each at most once per 10 minutes (toastOnce).
// No session-anomaly card (pivot P6): the view's `anomaly` stays null and nothing is persisted.
import type { Level, Measure, SignalsView } from '../types/index.d.ts'
import { budgetBurn } from './core/cost.ts'
import type { Host } from './host.ts'
import { everyOnce, toastOnce, type Runtime } from './runtime.ts'
import {
  admissionRate, breakerNew, breakerRecord, breakerState, burnAlert, fanoutRisk, levelStep, limitLevel, LONG_LIMIT, SHORT_LIMIT,
  SLO_TRANSPORT, spendBurn, worst,
} from './signals.ts'
import { EMPTY_SIGNALS } from './state.ts'

export const TICK_MS = 60_000
const WINDOW_MIN = 60
const MIN_LONG_MINUTES = 5
const SLOPE_MS = 10 * 60_000

export function tick(rt: Runtime, now: number, m: Measure | null, _prev: SignalsView): { view: SignalsView; toasts: [string, string][] } {
  const toasts: [string, string][] = []
  rt.minutes = [...rt.minutes, rt.minute].slice(-WINDOW_MIN)
  rt.minute = { steps: 0, failed: 0, spend: 0 }

  const burn = burnAlert(rt.minutes.map(b => [b.steps, b.failed] as const), SLO_TRANSPORT)
  const observed: Level = worst([limitLevel(m?.limitPercent ?? null), burn.alert ? 'AMBER' : 'GREEN', worst(Object.values(rt.backend.families))])
  const before = rt.level.level
  rt.level = levelStep(rt.level, observed)
  if (rt.level.level === 'RED' && before !== 'RED') toasts.push(['red', 'datapce: pressure RED — serialize heavy fan-out'])
  if (rt.level.level !== 'GREEN') {
    const hours = [...rt.profile.hours]
    const h = new Date(now).getHours()
    hours[h] = (hours[h] ?? 0) + 1
    rt.profile = { ...rt.profile, hours }
    rt.profileDirty = true
  }

  const wasOpen = new Set(Object.entries(rt.breakers).filter(([, b]) => breakerState(b, now) === 'open').map(([k]) => k))
  // Only calls that were actually made are outcomes, in the order they happened. A failure of a call
  // made while the breaker was open is not a probe and must not extend the cooldown; the first call
  // after the cooldown (half-open) is the single probe — it either closes the breaker or reopens it.
  for (const ev of rt.bashEvents.splice(0).sort((a, b) => a.t - b.t)) {
    const b = rt.breakers[ev.cmd] ?? breakerNew()
    const ok = ev.signal === null && !ev.isError
    if (!ok && breakerState(b, ev.t) === 'open') continue
    rt.breakers[ev.cmd] = breakerRecord(b, ev.t, ok)
  }
  const breakers = Object.fromEntries(Object.entries(rt.breakers).map(([k, b]) => [k, breakerState(b, now)]))
  for (const [k, state] of Object.entries(breakers)) {
    if (state === 'open' && !wasOpen.has(k)) toasts.push([`breaker:${k}`, `datapce: ${k} lane breaker open — escalate for 10 min`])
  }

  const budget = rt.options.budgetUsd
  const spend = rt.minutes.map(b => b.spend)
  const short = spendBurn(spend, budget, 5)
  // Under 5 closed minutes the 60 min window is the same data as the 5 min one: not a second opinion yet.
  const long = spend.length >= MIN_LONG_MINUTES ? spendBurn(spend, budget, WINDOW_MIN) : null
  // startedAt is 0 after a hot reload (identify did not run): whole-session burn is unknown then.
  const whole = budget > 0 && m?.costUsd != null && rt.startedAt > 0 ? budgetBurn(m.costUsd, Math.max(1, (now - rt.startedAt) / 60_000), budget) : null
  if (short !== null && long !== null && short > SHORT_LIMIT && long > LONG_LIMIT) {
    toasts.push(['budget', `datapce: budget burning ${short.toFixed(0)}× (5 min) / ${long.toFixed(0)}× (60 min)`])
  }

  const hist = rt.limitHistory.filter(s => now - s.t <= SLOPE_MS)
  const first = hist[0]
  const last = hist[hist.length - 1]
  const slope = first !== undefined && last !== undefined && last.t > first.t ? (last.pct - first.pct) / ((last.t - first.t) / 60_000) : 0
  const resetMs = m?.resetsAt != null ? Date.parse(m.resetsAt) : Number.NaN
  const minutesToReset = Number.isFinite(resetMs) ? (resetMs - now) / 60_000 : 0
  const spawnsPerMin = rt.heavySpawns.filter(t => now - t <= SLOPE_MS).length / 10
  const rate = admissionRate(spawnsPerMin, slope, 100 - (m?.limitPercent ?? 0), minutesToReset)
  rt.bucket = { ...rt.bucket, rate }

  let steps = 0
  let failed = 0
  for (const b of rt.minutes) {
    steps += b.steps
    failed += b.failed
  }
  const running = [...rt.dispatches.values()].filter(d => d.outcome === 'running').length

  return {
    view: {
      level: rt.level.level,
      burnShort: burn.short,
      burnLong: burn.long,
      budgetBurn: whole?.burn ?? null,
      budgetBurnShort: short,
      budgetBurnLong: long,
      minutesToExhaust: whole?.minutesToExhaust ?? null,
      breakers,
      anomaly: null,
      fanoutRisk: running >= 3 ? fanoutRisk(steps > 0 ? failed / steps : 0, running) : null,
      admissionRate: rate,
    },
    toasts,
  }
}

/** session.start (after backend): tick every minute. */
export async function start(host: Host, rt: Runtime, measureNow: () => Measure | null): Promise<void> {
  let prev: SignalsView = EMPTY_SIGNALS
  everyOnce(host, rt, 'live.tick', TICK_MS, () => {
    void (async () => {
      const now = await host.now()
      const { view, toasts } = tick(rt, now, measureNow(), prev)
      prev = view
      for (const [kind, text] of toasts) toastOnce(host, rt, kind, text, now)
      await host.publish.signals(view)
    })().catch(() => undefined)
  })
}
