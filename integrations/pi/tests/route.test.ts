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
    if (String(path).endsWith('/apex-router')) return '#!/fake/python\n';
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
  await handlers.agent_end({ messages: [{ stopReason: 'error' }], agentId: 'a', toolUseId: 't' }, ctx);
  await new Promise(resolve => setImmediate(resolve));
  await handlers.input({ text: '>>sonnet debug this' }, ctx);
  await handlers.turn_end({ message: { stopReason: 'error' }, agent_id: 'b', tool_use_id: 'u' }, ctx);
  await new Promise(resolve => setImmediate(resolve));
  const rows = calls.filter(c => c[1]?.includes('apex_router.route_log')).map(c => JSON.parse(c[1].at(-1)));
  assert.equal(rows.length, 2, JSON.stringify(calls.map(c => c.slice(0, 2))));
  assert.deepEqual(rows.map(r => [r.session_id, r.agent_id, r.tool_use_id, r.cause]),
    [['real-session', 'a', 't', 'infra'], ['real-session', 'b', 'u', 'infra']]);
  syncBuiltinESMExports();
});
