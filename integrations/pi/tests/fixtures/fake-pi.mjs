#!/usr/bin/env node
// Fake `pi --mode json -p` child for the job-manager tests. Writes scripted JSONL events to stdout.
// argv[2] = mode: normal | slow | silent | crash | grandchild | ignore-term | term-spawn | error | recover-429 | fail-429
import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";

const mode = process.argv[2] ?? "normal";
const out = o => process.stdout.write(JSON.stringify(o) + "\n");
const msg = (text, extra = {}) => out({ type: "message_end", message: { role: "assistant", content: [{ type: "text", text }], usage: { input: 10, output: 5, cost: { total: 0.001 } }, stopReason: "stop", ...extra } });
const sleep = ms => new Promise(r => setTimeout(r, ms));

out({ type: "agent_start" });
if (mode === "normal") { await sleep(300); msg("FINAL ANSWER: 42"); out({ type: "agent_end" }); process.exit(0); }
if (mode === "error") { msg("", { stopReason: "error", errorMessage: "upstream 500" }); out({ type: "agent_end" }); process.exit(0); }
const E429 = '429 {"error":{"code":"RateLimitReached","message":"Rate limit of 3000000 per 60s exceeded for UserByModelByMinuteOutputTokens. Please wait 15 seconds before retrying."}}';
if (mode === "recover-429") {
  msg("", { stopReason: "error", errorMessage: E429 });
  out({ type: "auto_retry_start", attempt: 1, maxAttempts: 5, delayMs: 10, errorMessage: E429 });
  await sleep(50); out({ type: "auto_retry_end", success: true, attempt: 1 });
  msg("FINAL ANSWER: recovered"); out({ type: "agent_end" }); process.exit(0);
}
if (mode === "fail-429") {
  for (let a = 1; a <= 3; a++) { msg("", { stopReason: "error", errorMessage: E429 }); out({ type: "auto_retry_start", attempt: a, maxAttempts: 3, delayMs: 10, errorMessage: E429 }); await sleep(20); }
  msg("", { stopReason: "error", errorMessage: E429 }); out({ type: "auto_retry_end", success: false, attempt: 3, finalError: E429 }); out({ type: "agent_end" }); process.exit(0);
}
if (mode === "crash") { process.stderr.write("boom: fake crash\n"); process.exit(3); }
if (mode === "slow") { for (;;) { out({ type: "tool_execution_update", toolName: "bash" }); await sleep(500); } }
if (mode === "silent") { out({ type: "tool_execution_start", toolName: "bash", args: { command: "sleep 999" } }); await sleep(1e9); }
if (mode === "term-spawn") {
  // on SIGTERM: start a child in this process group, record its pid, then exit (Codex pass-3 repro)
  process.on("SIGTERM", () => {
    const late = spawn("sleep", ["300"], { stdio: "ignore" });
    writeFileSync(process.env.FAKE_GC_PID_FILE ?? "/tmp/fake-gc.pid", String(late.pid));
    late.unref();
    setTimeout(() => process.exit(0), 100);
  });
  for (;;) { out({ type: "tool_execution_update", toolName: "bash" }); await sleep(500); }
}
if (mode === "grandchild" || mode === "ignore-term") {
  if (mode === "ignore-term") process.on("SIGTERM", () => {});
  const gc = spawn("sleep", ["300"], { detached: true, stdio: "ignore" }); // own process group, like pi's bash tool
  writeFileSync(process.env.FAKE_GC_PID_FILE ?? "/tmp/fake-gc.pid", String(gc.pid));
  out({ type: "tool_execution_start", toolName: "bash", args: { command: "sleep 300" } });
  for (;;) { out({ type: "tool_execution_update", toolName: "bash" }); await sleep(500); }
}
