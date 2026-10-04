// pce-core routing — TypeScript mirror of cell_new/cell_observe in src/apex_router/core/routing.py.
import type { Cell } from '../../types/index.d.ts'
import { cusumLower } from './drift.ts'
import { wilson } from './stats.ts'

export const MIN_N = 30
export const TARGET = 0.9
export const WINDOW = 10
export const ENTER = 2
export const EXIT = 3
export const REBASE = 3
export const P0_CAP = 0.98

export const cellNew = (): Cell => ({ n: 0, pass: 0, state: 'COLD', winN: 0, winPass: 0, below: 0, above: 0, cusum: 0, p0: null })

export function cellObserve(cell: Cell, passed: boolean, minN = MIN_N, target = TARGET, window = WINDOW): Cell {
  const x = passed ? 1 : 0
  const c: Cell = { ...cell, n: cell.n + 1, pass: cell.pass + x, winN: cell.winN + 1, winPass: cell.winPass + x }
  let alarm = false
  if (c.winN >= window) {
    const rate = c.winPass / c.winN
    const below = rate < target
    if ((c.state === 'READY' || c.state === 'DRIFTING') && c.p0 !== null) {
      const p0 = Math.min(c.p0, P0_CAP)
      const se = Math.sqrt((p0 * (1 - p0)) / c.winN)
      ;[c.cusum, alarm] = cusumLower(c.cusum, (rate - p0) / se)
    }
    if (c.state === 'READY') {
      c.below = below ? c.below + 1 : 0
      if (c.below >= ENTER || alarm) {
        c.state = 'DRIFTING'
        c.below = 0
        c.above = 0
      }
    } else if (c.state === 'DRIFTING') {
      if (below) {
        c.below += 1
        c.above = 0
      } else {
        c.above += 1
        c.below = 0
      }
      if (c.above >= EXIT) {
        c.state = 'READY'
        c.above = 0
        c.cusum = 0
      } else if (c.below >= REBASE) return cellNew()
    }
    c.winN = 0
    c.winPass = 0
  }
  if (c.state === 'COLD' || c.state === 'WARMING') {
    if (c.n >= minN && wilson(c.pass, c.n)[0] >= target) {
      c.state = 'READY'
      c.p0 = c.pass / c.n
      c.cusum = 0
      c.below = 0
      c.above = 0
    } else c.state = c.n === 0 ? 'COLD' : 'WARMING'
  }
  return c
}
