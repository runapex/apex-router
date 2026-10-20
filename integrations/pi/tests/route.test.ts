import { test } from 'node:test';
import assert from 'node:assert/strict';
import child from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { promisify } from 'node:util';
import fs from 'node:fs';

// Inject the CLI seam before importing the extension. No real logs or providers run.
test('route writers record infra and session, agent, and tool identities', async (t) => {
  const calls: any[] = [];
  const exec = (...args: any[]) => {
    calls.push(args);
    const callback = args.at(-1);
    callback(null, JSON.stringify({ task_type: 'debug' }), '');
    return {};
  };
  (exec as any)[promisify.custom] = async (...args: any[]) => {
    calls.push(args);
    return { stdout: JSON.stringify({ task_type: 'debug' }), stderr: '' };
  };
  const originalExec = child.execFile;
  child.execFile = exec as any;
  t.after(() => { child.execFile = originalExec; syncBuiltinESMExports(); });
  const read = fs.readFileSync;
  t.mock.method(fs, 'readFileSync', (path: any, ...args: any[]) => {
    if (String(path).endsWith('/apex-router')) throw new Error('must not read executable');
    if (String(path).endsWith('models.json') || String(path).endsWith('pi-routes.json')) throw new Error('no registry');
    return (read as any)(path, ...args);
  });
  syncBuiltinESMExports();
  const { default: install } = await import('../apex-route.ts');
  const handlers: Record<string, any> = {};
  const model = { provider: 'foundry', id: 'it-entra-claude-sonnet-5-5' };
  const pi: any = {
    on: (name: string, fn: any) => { handlers[name] = fn; },
    registerCommand: () => {}, setModel: async () => true, setThinkingLevel: () => {},
  };
  const ctx: any = {
    model, thinkingLevel: 'medium', modelRegistry: { find: () => model },
    sessionManager: { getSessionId: () => 'real-session' },
    getContextUsage: () => ({ tokens: 42 }), ui: { notify: () => {} },
  };
  install(pi);
  await handlers.input({ text: 'debug this' }, ctx);
  await handlers.agent_end({ messages: [{ stopReason: 'error', errorMessage: 'provider overloaded' }], agentId: 'a', toolUseId: 't' }, ctx);
  await new Promise(resolve => setImmediate(resolve));
  await handlers.input({ text: '>>sonnet debug this' }, ctx);
  await handlers.turn_end({ message: { stopReason: 'error', errorMessage: 'rate limit 429' }, agent_id: 'b', tool_use_id: 'u' }, ctx);
  await new Promise(resolve => setImmediate(resolve));
  const logs = calls.filter(c => c[1]?.[0] === 'route-log');
  assert.ok(logs.every(c => String(c[0]).endsWith('/apex-router')));
  const rows = logs.map(c => Object.fromEntries(
    Array.from({ length: (c[1].length - 1) / 2 }, (_, i) =>
      [c[1][1 + i * 2].slice(2).replaceAll('-', '_'), c[1][2 + i * 2]])));
  assert.equal(rows.length, 2, JSON.stringify(calls.map(c => c.slice(0, 2))));
  assert.deepEqual(rows.map(r => [r.session_id, r.agent_id, r.tool_use_id, r.cause]),
    [['real-session', 'a', 't', 'infra'], ['real-session', 'b', 'u', 'infra']]);
  const parentTool = process.env.PI_TOOL_USE_ID;
  process.env.PI_TOOL_USE_ID = 'stale-parent-tool';
  t.after(() => {
    if (parentTool === undefined) delete process.env.PI_TOOL_USE_ID;
    else process.env.PI_TOOL_USE_ID = parentTool;
  });
  for (const message of [
    { stopReason: 'aborted', errorMessage: 'user aborted' },
    { stopReason: 'error', errorMessage: 'request cancelled' },
    { stopReason: 'error', errorMessage: 'invalid tool arguments' },
    { stopReason: 'stop' },
  ]) {
    await handlers.input({ text: 'debug this' }, ctx);
    await handlers.agent_end({ messages: [message] }, ctx);
    await new Promise(resolve => setImmediate(resolve));
  }
  const extra = calls.filter(c => c[1]?.[0] === 'route-log').slice(2).map(c => c[1]);
  assert.equal(extra.length, 2, 'aborts must not record outcomes');
  assert.equal(extra[0][extra[0].indexOf('--cause') + 1], 'capability');
  assert.ok(extra[0].includes('turn: capability error'));
  assert.ok(!extra[1].includes('--cause'));
  assert.ok(extra.every(args => !args.includes('--tool-use-id')));
  assert.ok(logs.every(c => !c[1].some((arg: string) => arg.includes('provider error/empty'))));
  syncBuiltinESMExports();
});
