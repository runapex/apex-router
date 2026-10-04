// File and process helpers over the Host. Every call fails open: a write that cannot happen returns
// false or null, never throws into a hook. Appends go through tee -a (O_APPEND): $.fs has no append.
import type { Host } from './host.ts'

export async function appendLines(host: Host, path: string, lines: readonly string[]): Promise<boolean> {
  if (lines.length === 0) return true
  try {
    const r = await host.run(['/usr/bin/tee', '-a', '--', path], { stdin: `${lines.join('\n')}\n`, timeoutMs: 5000 })
    return r.exitCode === 0
  } catch {
    return false
  }
}

export async function ensureDir(host: Host, dir: string): Promise<void> {
  try {
    if (!(await host.exists(dir))) await host.write(`${dir}/.keep`, '')
  } catch {
    // fail open: the next append reports the failure and keeps its rows
  }
}

export async function readText(host: Host, path: string): Promise<string | null> {
  try {
    return await host.read(path)
  } catch {
    return null
  }
}

export async function readJson(host: Host, path: string): Promise<unknown> {
  const text = await readText(host, path)
  if (text === null) return null
  try {
    return JSON.parse(text) as unknown
  } catch {
    return null
  }
}

export async function tailLines(host: Host, path: string, n: number): Promise<string[]> {
  try {
    const r = await host.run(['/usr/bin/tail', '-n', String(n), '--', path], { timeoutMs: 5000 })
    return r.exitCode === 0 ? r.stdout.split('\n').filter(l => l !== '') : []
  } catch {
    return []
  }
}

export function jsonLines(lines: readonly string[]): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = []
  for (const line of lines) {
    try {
      const v = JSON.parse(line) as unknown
      if (v !== null && typeof v === 'object' && !Array.isArray(v)) out.push(v as Record<string, unknown>)
    } catch {
      // a torn or foreign line is skipped
    }
  }
  return out
}
