// Every engine call the modules make, as plain functions. register.ts binds it from `$` (hostOf):
// the engine's scan follows `$` only into functions declared in the same file, so modules take a
// Host and never see `$` (the reference plugin's pattern).
import type { CommandSpec, FsEntry, FsStat, PaneOpenArgs, ProcessRunInit, ProcessRunResult, Timer } from 'claude-code'

import type { BackendView, CellView, Dispatch, InjectView, Measure, ProfileView, SignalsView } from '../types/index.d.ts'

export type Host = {
  now(): Promise<number>
  every(ms: number, fn: () => void): Timer
  home(): Promise<string>
  sessionId(): Promise<string>
  read(path: string): Promise<string>
  write(path: string, text: string): Promise<void>
  exists(path: string): Promise<boolean>
  list(path: string): Promise<FsEntry[]>
  stat(path: string): Promise<FsStat>
  run(argv: readonly string[], init?: ProcessRunInit): Promise<ProcessRunResult>
  storeGet(key: string): Promise<unknown>
  storeSet(key: string, value: unknown): Promise<void>
  toast(text: string): void
  status(text: string | undefined): void
  invalidate(event: 'tool.describe'): void
  open(args: PaneOpenArgs): Promise<unknown>
  close(id: string): Promise<void>
  registerCommand(spec: CommandSpec): Promise<unknown>
  publish: {
    measure(v: Measure | null): Promise<void>
    dispatches(v: Dispatch[]): Promise<void>
    cells(v: CellView[]): Promise<void>
    signals(v: SignalsView): Promise<void>
    backend(v: BackendView): Promise<void>
    profile(v: ProfileView): Promise<void>
    inject(v: InjectView): Promise<void>
    enforce(v: boolean): Promise<void>
  }
}
