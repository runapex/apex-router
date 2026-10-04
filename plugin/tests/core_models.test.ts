import { describe, expect, test } from 'claude-code/testing'

import type { Cell } from '../types/index.d.ts'
import {
  correlationOf, explain, mad, maxAbsScales, median, pcaFit, qResidual, robustZ, tSquared, t2Limit,
} from '../hooks/core/anomaly.ts'
import { budgetBurn, fitCost } from '../hooks/core/cost.ts'
import { cusumLower, penaltyRun, varianceL1 } from '../hooks/core/drift.ts'
import { cellNew, cellObserve } from '../hooks/core/routing.ts'
import { close } from './fixtures/close.ts'
import { CORE } from './fixtures/core.ts'

describe('core/anomaly ≡ apex_router.core.anomaly', () => {
  test('Q and T² cases', () => {
    for (const [xc, q, t2] of CORE.anomaly.cases) {
      close(qResidual(xc as number[], CORE.anomaly.V), q as number)
      close(tSquared(xc as number[], CORE.anomaly.V, CORE.anomaly.values), t2 as number)
    }
    close(t2Limit(2), -2 * Math.log(0.01))
  })

  test('correlation and PCA', () => {
    const { corr, sd } = correlationOf(CORE.anomaly.cov)
    CORE.anomaly.sd.forEach((s, i) => close(sd[i], s))
    CORE.anomaly.corr.forEach((row, i) => row.forEach((v, j) => close(corr[i]![j], v)))
    const { values, vectors } = pcaFit(corr, 2)
    CORE.anomaly.pca.values.forEach((v, i) => close(values[i], v))
    CORE.anomaly.pca.vectors.forEach((vec, i) => vec.forEach((x, j) => close(vectors[i]![j], x)))
  })

  test('scales, median/MAD, robust z, explain card', () => {
    expect(maxAbsScales(CORE.anomaly.scales.rows)).toEqual(CORE.anomaly.scales.scales)
    close(median(CORE.anomaly.mad.xs), CORE.anomaly.mad.median)
    close(mad(CORE.anomaly.mad.xs), CORE.anomaly.mad.mad)
    for (const [x, z] of CORE.anomaly.mad.robust) close(robustZ(x!, CORE.anomaly.mad.median, CORE.anomaly.mad.mad), z!)
    for (const [q, t2, qh, th, card] of CORE.anomaly.explain) {
      expect(explain(q as number, t2 as number, qh as number[], th as number[])).toEqual(card as never)
    }
  })
})

describe('core/drift ≡ apex_router.core.drift', () => {
  test('cusum trace', () => {
    let s = 0
    CORE.drift.cusum.zs.forEach((z, i) => {
      const [next, alarm] = cusumLower(s, z)
      s = next
      close(next, CORE.drift.cusum.trace[i]![0] as number)
      expect(alarm).toBe(CORE.drift.cusum.trace[i]![1] as boolean)
    })
  })

  test('penalty state machine trace', () => {
    expect(penaltyRun(CORE.drift.penalty.scores)).toEqual(CORE.drift.penalty.trace as never)
  })

  test('variance share L1', () => {
    for (const [l0, l1, v] of CORE.drift.variance_l1) close(varianceL1(l0 as number[], l1 as number[]), v as number)
  })
})

describe('core/cost ≡ apex_router.core.cost', () => {
  test('OLS cost fit and budget burn', () => {
    const fit = fitCost(CORE.cost.xs, CORE.cost.ys)!
    const ref = CORE.cost.fit
    close(fit.a, ref.a)
    close(fit.b, ref.b)
    close(fit.r2, ref.r2)
    close(fit.trend, ref.trend, 1e-6)
    expect(fit.n).toBe(ref.n)
    expect(fitCost([1, 2], [1, 2])).toBeNull()
    for (const [spent, minutes, budget, result] of CORE.cost.budget) {
      const r = budgetBurn(spent as number, minutes as number, budget as number)
      if (result === null) expect(r).toBeNull()
      else {
        const want = result as { burn: number; minutesToExhaust: number | null }
        close(r!.burn, want.burn)
        if (want.minutesToExhaust === null) expect(r!.minutesToExhaust).toBeNull()
        else close(r!.minutesToExhaust, want.minutesToExhaust)
      }
    }
  })
})

describe('core/routing ≡ apex_router.core.routing (cell state machine)', () => {
  test('every scenario reproduces state by state', () => {
    for (const [name, fx] of Object.entries(CORE.cells)) {
      let c: Cell = cellNew()
      fx.seq.forEach((x, i) => {
        c = cellObserve(c, x === 1, fx.min_n, fx.target)
        expect(c.state, `${name}[${i}]`).toBe(fx.states[i] as Cell['state'])
      })
      expect({ ...c, cusum: 0 }, name).toEqual({ ...(fx.final as Cell), cusum: 0 })
      close(c.cusum, fx.final.cusum, 1e-9, name)
    }
  })
})
