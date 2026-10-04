// §7 signals as pure functions; tests/signals.test.ts pins each to its reference numbers.
// Descriptive statistics (kurtosis, medians) are never alerts (§7 "explicit non-rules").
import type { Breaker, BreakerState, Level, LevelState, TokenBucket } from '../types/index.d.ts'

export type Bucket = readonly [total: number, bad: number]
export type Burn = { alert: boolean; short: number | null; long: number | null }

export const SLO_429 = 0.98
export const SLO_TRANSPORT = 0.97
export const SHORT_MIN = 5
export const LONG_MIN = 60
export const SHORT_LIMIT = 10
export const LONG_LIMIT = 6

/** Prefix sums over 1-min buckets; burn(w) = (bad_w/tot_w)/(1−slo); alert iff both windows burn hot. */
export function burnAlert(
  buckets: readonly Bucket[],
  slo: number,
  short = SHORT_MIN,
  long = LONG_MIN,
  shortLimit = SHORT_LIMIT,
  longLimit = LONG_LIMIT,
): Burn {
  const tot = [0]
  const bad = [0]
  for (const [t, b] of buckets) {
    tot.push(tot[tot.length - 1]! + t)
    bad.push(bad[bad.length - 1]! + b)
  }
  const end = tot.length - 1
  const burn = (w: number): number | null => {
    const start = Math.max(0, end - w)
    const t = tot[end]! - tot[start]!
    return t === 0 ? null : (bad[end]! - bad[start]!) / t / (1 - slo)
  }
  const s = burn(short)
  const l = burn(long)
  return { alert: s !== null && l !== null && s > shortLimit && l > longLimit, short: s, long: l }
}

/** Budget burn over the last w minutes: (spend per minute) / (budget / 1440). */
export function spendBurn(spend: readonly number[], budgetUsd: number, w: number): number | null {
  if (budgetUsd <= 0 || spend.length === 0) return null
  const window = spend.slice(-w)
  let total = 0
  for (const x of window) total += x
  return total / window.length / (budgetUsd / 1440)
}

const RANK: Record<Level, number> = { GREEN: 0, AMBER: 1, RED: 2 }

export const worst = (levels: readonly Level[]): Level => levels.reduce<Level>((a, b) => (RANK[b] > RANK[a] ? b : a), 'GREEN')

/** GREEN/AMBER/RED with enter_streak 2, exit_streak 3 over 1-minute windows. */
export function levelStep(s: LevelState, observed: Level, enter = 2, exit = 3): LevelState {
  if (RANK[observed] > RANK[s.level]) {
    const up = s.up + 1
    return up >= enter ? { level: observed, up: 0, down: 0 } : { level: s.level, up, down: 0 }
  }
  if (RANK[observed] < RANK[s.level]) {
    const down = s.down + 1
    return down >= exit ? { level: observed, up: 0, down: 0 } : { level: s.level, up: 0, down }
  }
  return { level: s.level, up: 0, down: 0 }
}

export function limitLevel(percentUsed: number | null): Level {
  if (percentUsed === null) return 'GREEN'
  return percentUsed >= 90 ? 'RED' : percentUsed >= 70 ? 'AMBER' : 'GREEN'
}

/** Routing SLI burn per cell: (1 − pass/total)/(1 − target). */
export const routingBurn = (pass: number, total: number, target = 0.9): number | null => (total > 0 ? (1 - pass / total) / (1 - target) : null)

export const BREAKER_THRESHOLD = 3
export const BREAKER_COOL_MS = 600_000

export const breakerNew = (): Breaker => ({ fails: 0, openedAt: null })

export function breakerState(b: Breaker, now: number, coolMs = BREAKER_COOL_MS): BreakerState {
  if (b.openedAt === null) return 'closed'
  return now - b.openedAt < coolMs ? 'open' : 'half-open'
}

/** Open after `threshold` consecutive failures; a success (the half-open probe included) resets. */
export function breakerRecord(b: Breaker, now: number, ok: boolean, threshold = BREAKER_THRESHOLD): Breaker {
  if (ok) return { fails: 0, openedAt: null }
  const fails = b.fails + 1
  return { fails, openedAt: fails >= threshold ? now : b.openedAt }
}

export const bucketNew = (rate: number, burst: number, t = 0): TokenBucket => ({ rate, burst, tokens: burst, t })

export function bucketTake(b: TokenBucket, now: number, cost = 1): { allowed: boolean; bucket: TokenBucket } {
  const tokens = Math.min(b.burst, b.tokens + (now - b.t) * b.rate)
  return tokens >= cost
    ? { allowed: true, bucket: { ...b, tokens: tokens - cost, t: now } }
    : { allowed: false, bucket: { ...b, tokens, t: now } }
}

/** Heavy spawns per minute the rate-limit window can sustain (burst 2 set by the caller). */
export function admissionRate(spawnsPerMin: number, slopePctPerMin: number, headroomPct: number, minutesToReset: number): number {
  if (slopePctPerMin <= 0 || minutesToReset <= 0) return 10
  return Math.min(10, Math.max(0.2, (spawnsPerMin * (headroomPct / minutesToReset)) / slopePctPerMin))
}

/** P(any of n parallel calls is slow) = 1 − (1 − p)^n; shown when n ≥ 3. */
export const fanoutRisk = (p: number, n: number): number => 1 - (1 - p) ** n

export const availability = (mtbf: number, mttr: number): number => (mtbf + mttr > 0 ? mtbf / (mtbf + mttr) : 1)

export const series = (...parts: readonly number[]): number => parts.reduce((x, y) => x * y, 1)

export const drainEta = (backlog: number, cap: number, arrive: number): number | null => (cap > arrive ? backlog / (cap - arrive) : null)
