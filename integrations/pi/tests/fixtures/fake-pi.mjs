#!/usr/bin/env node
// Fake `pi --mode json -p` child for the job-manager tests. Writes scripted JSONL events to stdout.
// argv[2] = mode: xval-truncate | xval-rotate | self-steer | xval-stderr | spend | spend-ignore | normal | slow | silent | crash | grandchild | ignore-term | term-spawn | error | recover-429 | fail-429
import { spawn } from "node:child_process";
import { appendFileSync, existsSync, renameSync, truncateSync, writeFileSync } from "node:fs";
import { join } from "node:path";

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
// xval-stderr: like apex-router xval / codex: progress only on stderr for ~3 s, then the result on stdout, exit 0.
if (mode === "xval-stderr") {
  for (let i = 0; i < 10; i++) { process.stderr.write(`codex progress ${i}\n`); await sleep(300); }
  process.stdout.write("XVAL RESULT: no findings\n"); process.exit(0);
}
// self-steer: like a child at 85 % of its own budget: writes steer.json itself, a final report, exits 0 at once.
if (mode === "self-steer") {
  writeFileSync(join(process.env.APEX_AGENT_JOB_DIR, "steer.json"), JSON.stringify({ kind: "soft_cap", message: "COST LIMIT", at: 4242 }));
  msg("SALVAGE REPORT: self-steered"); out({ type: "agent_end" }); process.exit(0);
}
// xval-truncate: big stderr, then stderr.log is truncated to 0; small progress after that, then the result (F2).
// xval-rotate: big stderr, then stderr.log is renamed away (missing for 600 ms); a new small stderr.log grows (F2).
if (mode === "xval-truncate" || mode === "xval-rotate") {
  const log = join(process.env.APEX_AGENT_JOB_DIR, "stderr.log");
  process.stderr.write("x".repeat(4000) + "\n"); await sleep(600);
  if (mode === "xval-truncate") truncateSync(log, 0);
  else { renameSync(log, `${log}.1`); await sleep(600); }
  for (let i = 0; i < 12; i++) {
    if (mode === "xval-truncate") process.stderr.write(`p${i}\n`); else appendFileSync(log, `p${i}\n`);
    await sleep(300);
  }
  process.stdout.write("XVAL RESULT: rotated ok\n"); process.exit(0);
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
// spend: $0.10 per turn; obeys the soft cap (steer.json in APEX_AGENT_JOB_DIR): writes a final report, exits 0.
// spend-ignore: $0.10 per turn and never stops (the hard stop must kill it).
if (mode === "spend" || mode === "spend-ignore") {
  const steer = join(process.env.APEX_AGENT_JOB_DIR ?? "/nonexistent", "steer.json");
  for (let i = 0; ; i++) {
    if (mode === "spend" && existsSync(steer)) {
      out({ type: "message_end", message: { role: "assistant", content: [{ type: "text", text: "SALVAGE REPORT: done A, open B" }], usage: { input: 10, output: 5, cost: { total: 0.05 } }, stopReason: "stop" } });
      out({ type: "agent_end" }); process.exit(0);
    }
    out({ type: "message_end", message: { role: "assistant", content: [{ type: "text", text: `step ${i}` }], usage: { input: 10, output: 5, cost: { total: 0.1 } }, stopReason: "toolUse" } });
    await sleep(150);
  }
}
