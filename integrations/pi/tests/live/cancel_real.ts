// Live check (needs a model): a real pi child runs `sleep 977` in bash; agent_cancel must remove pi and the sleep.
// node integrations/pi/tests/live/cancel_real.ts
import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { JobManager } from "../../lib/agents.ts";

const m = new JobManager({ root: mkdtempSync(join(tmpdir(), "apexlive-")), pressure: () => "GREEN", graceMs: 10_000 });
const j = m.spawn({ task: "Run this bash command and wait for it: sleep 977", agent: "worker", provider: "foundry", model: "it-entra-claude-haiku-4-5", tools: ["bash"] });
const sleeps = () => execFileSync("ps", ["-A", "-o", "pid=,command="], { encoding: "utf8" }).split("\n").filter(l => /\bsleep 977\b/.test(l) && !l.includes("Run this"));
const t0 = Date.now();
while (Date.now() - t0 < 90_000 && sleeps().length === 0) await new Promise(r => setTimeout(r, 1000));
const before = sleeps().length;
const r = await m.cancel(j.id);
await new Promise(r => setTimeout(r, 500));
const after = sleeps();
let piAlive = true;
try { process.kill(r!.pid!, 0); } catch { piAlive = false; }
const pass = before > 0 && after.length === 0 && !piAlive && r!.state === "killed";
console.log(JSON.stringify({ pass, sleep_before: before, sleep_after: after, pi_alive: piAlive, state: r!.state, resume: r!.resume }));
m.stop();
process.exit(pass ? 0 : 1);
