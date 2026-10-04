// pce-core anomaly — TypeScript mirror of src/apex_router/core/anomaly.py.
import { dot, jacobiEigen } from './linalg.ts'

export const CHI2_99: Readonly<Record<number, number>> = {
  1: 6.634896601021214,
  2: 9.210340371976184,
  3: 11.344866730144373,
  4: 13.276704135987622,
  5: 15.08627246938899,
}
export const WARMUP_N = 50
export const HIST_MAX = 200

export type Card = { score: number; term: 'Q' | 'T2'; q: number; t2: number }

export function maxAbsScales(rows: readonly (readonly number[])[]): number[] {
  const d = rows[0]?.length ?? 0
  const scales: number[] = []
  for (let j = 0; j < d; j++) {
    let m = 0
    for (const row of rows) m = Math.max(m, Math.abs(row[j]!))
    scales.push(m > 0 ? m : 1)
  }
  return scales
}

export const prescale = (x: readonly number[], scales: readonly number[]): number[] => x.map((xi, j) => xi / scales[j]!)

export const zscore = (x: readonly number[], mean: readonly number[], sd: readonly number[]): number[] =>
  x.map((xi, j) => (xi - mean[j]!) / (sd[j]! > 0 ? sd[j]! : 1))

export function median(xs: readonly number[]): number {
  if (xs.length === 0) return 0
  const s = [...xs].sort((a, b) => a - b)
  const m = Math.floor(s.length / 2)
  return s.length % 2 ? s[m]! : (s[m - 1]! + s[m]!) / 2
}

export function mad(xs: readonly number[]): number {
  const med = median(xs)
  return median(xs.map(x => Math.abs(x - med)))
}

// Signed, like the Python oracle: outlier checks must compare Math.abs(z) > 3.5.
export const robustZ = (x: number, med: number, madValue: number): number => (madValue > 0 ? (0.6745 * (x - med)) / madValue : 0)

export function correlationOf(cov: readonly (readonly number[])[]): { corr: number[][]; sd: number[] } {
  const sd = cov.map((row, i) => (row[i]! > 0 ? Math.sqrt(row[i]!) : 0))
  const corr = cov.map((row, i) => row.map((cij, j) => cij / ((sd[i] || 1) * (sd[j] || 1))))
  return { corr, sd }
}

export function pcaFit(matrix: readonly (readonly number[])[], k: number): { values: number[]; vectors: number[][] } {
  const { values, vectors } = jacobiEigen(matrix)
  return { values: values.slice(0, k), vectors: vectors.slice(0, k) }
}

export function qResidual(xc: readonly number[], V: readonly (readonly number[])[]): number {
  const proj = V.map(v => dot(v, xc))
  let total = 0
  for (let j = 0; j < xc.length; j++) {
    let recon = 0
    V.forEach((v, i) => {
      recon += proj[i]! * v[j]!
    })
    total += (xc[j]! - recon) ** 2
  }
  return total
}

export function tSquared(xc: readonly number[], V: readonly (readonly number[])[], values: readonly number[]): number {
  let t = 0
  V.forEach((v, i) => {
    const lam = values[i]!
    if (lam > 0) {
      const s = dot(v, xc)
      t += (s * s) / lam
    }
  })
  return t
}

export const t2Limit = (k: number): number | null => CHI2_99[k] ?? null

export function ecdf(value: number, history: readonly number[]): number {
  if (history.length === 0) return 0
  let count = 0
  for (const h of history) if (h <= value) count++
  return count / history.length
}

export function explain(q: number, t2: number, qHist: readonly number[], t2Hist: readonly number[]): Card {
  const rq = ecdf(q, qHist)
  const rt = ecdf(t2, t2Hist)
  return rq >= rt ? { score: rq, term: 'Q', q: rq, t2: rt } : { score: rt, term: 'T2', q: rq, t2: rt }
}
