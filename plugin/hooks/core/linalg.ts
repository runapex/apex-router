// pce-core linalg — TypeScript mirror of src/apex_router/core/linalg.py.

export function dot(a: readonly number[], b: readonly number[]): number {
  let s = 0
  for (let i = 0; i < Math.min(a.length, b.length); i++) s += a[i]! * b[i]!
  return s
}

export function solveNormalEquations(X: readonly (readonly number[])[], y: readonly number[], ridge = 1e-6): number[] {
  const n = X.length
  const p = n ? X[0]!.length : 0
  const A = Array.from({ length: p }, () => Array.from({ length: p }, () => 0))
  const b = Array.from({ length: p }, () => 0)
  for (let i = 0; i < n; i++) {
    const xi = X[i]!
    const yi = y[i]!
    for (let a = 0; a < p; a++) {
      b[a]! += xi[a]! * yi
      const xia = xi[a]!
      const row = A[a]!
      for (let c = 0; c < p; c++) row[c]! += xia * xi[c]!
    }
  }
  for (let a = 0; a < p; a++) A[a]![a]! += ridge
  for (let col = 0; col < p; col++) {
    let piv = col
    for (let r = col + 1; r < p; r++) if (Math.abs(A[r]![col]!) > Math.abs(A[piv]![col]!)) piv = r
    if (Math.abs(A[piv]![col]!) < 1e-12) continue
    if (piv !== col) {
      ;[A[col], A[piv]] = [A[piv]!, A[col]!]
      ;[b[col], b[piv]] = [b[piv]!, b[col]!]
    }
    const pivval = A[col]![col]!
    for (let r = 0; r < p; r++) {
      if (r === col) continue
      const factor = A[r]![col]! / pivval
      if (factor === 0) continue
      for (let c = col; c < p; c++) A[r]![c]! -= factor * A[col]![c]!
      b[r]! -= factor * b[col]!
    }
  }
  return Array.from({ length: p }, (_, i) => (Math.abs(A[i]![i]!) > 1e-12 ? b[i]! / A[i]![i]! : 0))
}

export function residualTrend(idx: readonly number[], resid: readonly number[]): number {
  const n = idx.length
  if (n < 2) return 0
  let mx = 0
  let my = 0
  for (let i = 0; i < n; i++) {
    mx += idx[i]!
    my += resid[i]!
  }
  mx /= n
  my /= n
  let num = 0
  let den = 0
  for (let i = 0; i < n; i++) {
    num += (idx[i]! - mx) * (resid[i]! - my)
    den += (idx[i]! - mx) ** 2
  }
  return den > 0 ? num / den : 0
}

export function jacobiEigen(A: readonly (readonly number[])[], tol = 1e-12, maxSweeps = 64): { values: number[]; vectors: number[][] } {
  const n = A.length
  const a = A.map(row => row.map(Number))
  const v = Array.from({ length: n }, (_, i) => Array.from({ length: n }, (_, j): number => (i === j ? 1 : 0)))
  for (let sweep = 0; sweep < maxSweeps; sweep++) {
    let off = 0
    for (let p = 0; p < n; p++) for (let q = p + 1; q < n; q++) off += a[p]![q]! * a[p]![q]!
    let total = 0
    for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) total += a[i]![j]! * a[i]![j]!
    if (total === 0 || off <= tol * tol * total) break
    for (let p = 0; p < n - 1; p++) {
      for (let q = p + 1; q < n; q++) {
        const apq = a[p]![q]!
        if (apq === 0) continue
        const theta = (a[q]![q]! - a[p]![p]!) / (2 * apq)
        const t = (theta >= 0 ? 1 : -1) / (Math.abs(theta) + Math.sqrt(theta * theta + 1))
        const c = 1 / Math.sqrt(t * t + 1)
        const s = t * c
        for (let k = 0; k < n; k++) {
          const akp = a[k]![p]!
          const akq = a[k]![q]!
          a[k]![p] = c * akp - s * akq
          a[k]![q] = s * akp + c * akq
        }
        for (let k = 0; k < n; k++) {
          const apk = a[p]![k]!
          const aqk = a[q]![k]!
          a[p]![k] = c * apk - s * aqk
          a[q]![k] = s * apk + c * aqk
        }
        for (let k = 0; k < n; k++) {
          const vkp = v[k]![p]!
          const vkq = v[k]![q]!
          v[k]![p] = c * vkp - s * vkq
          v[k]![q] = s * vkp + c * vkq
        }
      }
    }
  }
  const order = Array.from({ length: n }, (_, i) => i).sort((i, j) => a[j]![j]! - a[i]![i]!)
  const values = order.map(i => a[i]![i]!)
  const vectors = order.map(i => {
    const col = Array.from({ length: n }, (_, k) => v[k]![i]!)
    const lead = col.find(x => Math.abs(x) > 1e-12) ?? 0
    return lead < 0 ? col.map(x => -x) : col
  })
  return { values, vectors }
}

export function hasGap(values: readonly number[], k: number, rel = 1e-6): boolean {
  if (k <= 0 || k >= values.length) return false
  const scale = Math.max(Math.abs(values[0]!), Number.MIN_VALUE)
  return values[k - 1]! - values[k]! > rel * scale
}

export function principalAngle(u: readonly number[], w: readonly number[]): number {
  const nu = Math.sqrt(dot(u, u))
  const nw = Math.sqrt(dot(w, w))
  if (nu === 0 || nw === 0) return Math.PI / 2
  const c = Math.min(1, Math.abs(dot(u, w)) / (nu * nw))
  return Math.acos(c)
}
