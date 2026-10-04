// pce-core cost — TypeScript mirror of src/apex_router/core/cost.py.
import { residualTrend, solveNormalEquations } from './linalg.ts'

export type CostFit = { a: number; b: number; r2: number; trend: number; n: number }
export type Budget = { burn: number; minutesToExhaust: number | null }

export function fitCost(xs: readonly number[], ys: readonly number[]): CostFit | null {
  const n = xs.length
  if (n < 3 || ys.length !== n) return null
  const [a = 0, b = 0] = solveNormalEquations(xs.map(x => [1, x]), ys)
  const resid = ys.map((y, i) => y - (a + b * xs[i]!))
  let meanY = 0
  for (const y of ys) meanY += y
  meanY /= n
  let ssRes = 0
  let ssTot = 0
  for (let i = 0; i < n; i++) {
    ssRes += resid[i]! ** 2
    ssTot += (ys[i]! - meanY) ** 2
  }
  const r2 = ssTot > 0 ? 1 - ssRes / ssTot : 0
  return { a, b, r2, trend: residualTrend(xs.map((_, i) => i), resid), n }
}

export function budgetBurn(spentUsd: number, minutes: number, budgetUsd: number): Budget | null {
  if (budgetUsd <= 0 || minutes <= 0) return null
  const rate = spentUsd / minutes
  const burn = rate / (budgetUsd / 1440)
  return { burn, minutesToExhaust: rate > 0 ? Math.max(0, budgetUsd - spentUsd) / rate : null }
}
