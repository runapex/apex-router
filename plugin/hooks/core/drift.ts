// pce-core drift — TypeScript mirror of src/apex_router/core/drift.py.
export { principalAngle as subspaceAngle } from './linalg.ts'

export function varianceL1(l0: readonly number[], l1: readonly number[]): number {
  let s0 = 0
  for (const x of l0) s0 += x
  let s1 = 0
  for (const x of l1) s1 += x
  if (s0 <= 0 || s1 <= 0) return 0
  let total = 0
  for (let i = 0; i < Math.min(l0.length, l1.length); i++) total += Math.abs(l0[i]! / s0 - l1[i]! / s1)
  return total
}

export function cusumLower(s: number, z: number, kappa = 0.5, h = 4): [number, boolean] {
  const next = Math.max(0, s - z - kappa)
  return [next, next > h]
}

export const penaltyRamp = (T: number, lo = 2, hi = 6, pMax = 10): number =>
  Math.trunc(pMax * Math.min(1, Math.max(0, (T - lo) / (hi - lo))))

export type PenaltyState = 'COLD' | 'WARM' | 'DRIFTING'

export function penaltyRun(scores: readonly number[], enter = 2, exit = 3, warmAfter = 3): [number, PenaltyState, number][] {
  let state: PenaltyState = 'COLD'
  let streak = 0
  let seen = 0
  const out: [number, PenaltyState, number][] = []
  for (const T of scores) {
    seen += 1
    if (state === 'COLD' && seen >= warmAfter) state = 'WARM'
    else if (state === 'WARM') {
      streak = T >= 2 ? streak + 1 : 0
      if (streak >= enter) {
        state = 'DRIFTING'
        streak = 0
      }
    } else if (state === 'DRIFTING') {
      streak = T < 2 ? streak + 1 : 0
      if (streak >= exit) {
        state = 'WARM'
        streak = 0
      }
    }
    out.push([T, state, state === 'DRIFTING' ? penaltyRamp(T) : 0])
  }
  return out
}
