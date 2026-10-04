import { describe, expect, test } from 'claude-code/testing'

import { hasGap, jacobiEigen, principalAngle, residualTrend, solveNormalEquations } from '../hooks/core/linalg.ts'
import {
  covariance, ewma, ewmaQ16, halfLife, nearestRank, welfordCovNew, welfordCovPush, welfordNew, welfordPush, welfordVariance, wilson,
} from '../hooks/core/stats.ts'
import { close } from './fixtures/close.ts'
import { CORE } from './fixtures/core.ts'

describe('core/stats ≡ apex_router.core.stats', () => {
  test('welford', () => {
    let w = welfordNew()
    for (const x of CORE.welford.xs) w = welfordPush(w, x)
    expect(w.n).toBe(CORE.welford.n)
    close(w.mean, CORE.welford.mean)
    close(w.m2, CORE.welford.m2)
    close(welfordVariance(w), CORE.welford.var)
  })

  test('welford covariance', () => {
    let s = welfordCovNew(2)
    for (const row of CORE.welford_cov.rows) s = welfordCovPush(s, row)
    expect(s.n).toBe(CORE.welford_cov.n)
    CORE.welford_cov.mean.forEach((m, i) => close(s.mean[i], m))
    const cov = covariance(s)
    CORE.welford_cov.cov.forEach((row, i) => row.forEach((v, j) => close(cov[i]![j], v)))
  })

  test('ewma float and Q16 (bit-exact)', () => {
    let s: number | null = null
    let q: number | null = null
    CORE.ewma.xs.forEach((x, i) => {
      s = ewma(s, x, CORE.ewma.a)
      q = ewmaQ16(q, x)
      close(s, CORE.ewma.out[i]!)
      expect(q).toBe(CORE.ewma.q16[i]!)
    })
    expect(halfLife(0.2).toFixed(1)).toBe('3.1')
  })

  test('wilson', () => {
    for (const [k, n, lo, hi] of CORE.wilson) {
      const [l, h] = wilson(k!, n!)
      close(l, lo!)
      close(h, hi!)
    }
    expect(() => wilson(0, 0)).toThrow('n must be positive')
  })

  test('nearest rank', () => {
    for (const [q, v] of CORE.nearest_rank.cases) expect(nearestRank(CORE.nearest_rank.values, q!)).toBe(v!)
    expect(nearestRank([], 0.5)).toBeNull()
  })
})

describe('core/linalg ≡ apex_router.core.linalg', () => {
  test('jacobi eigenpairs', () => {
    for (const fx of CORE.jacobi) {
      const { values, vectors } = jacobiEigen(fx.A)
      fx.values.forEach((v, i) => close(values[i], v))
      fx.vectors.forEach((vec, i) => vec.forEach((x, j) => close(vectors[i]![j], x)))
    }
  })

  test('principal angle and gap', () => {
    for (const [u, w, angle] of CORE.principal_angle) close(principalAngle(u as number[], w as number[]), angle as number)
    for (const [values, k, gap] of CORE.has_gap) expect(hasGap(values as number[], k as number)).toBe(gap as boolean)
  })

  test('normal-equations solve and residual trend', () => {
    const beta = solveNormalEquations(CORE.solve.X, CORE.solve.y, CORE.solve.ridge)
    CORE.solve.beta.forEach((b, i) => close(beta[i], b))
    close(residualTrend(CORE.residual_trend.idx, CORE.residual_trend.resid), CORE.residual_trend.slope)
  })
})
