#!/usr/bin/env node
// Fake `apex-router` for the inbox tests. argv[2] = mode; the rest is the real CLI argv ("agents", "--json", ...).
// Modes: ok | empty | slow | huge | badjson | fail | resolve-fail | result-fail. FAKE_AGENTS_LOG: append one JSON line of argv per call.
import { appendFileSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const [mode = "ok", ...argv] = process.argv.slice(2);
if (process.env.FAKE_AGENTS_LOG) appendFileSync(process.env.FAKE_AGENTS_LOG, JSON.stringify(argv) + "\n");
const fixture = () => readFileSync(join(dirname(fileURLToPath(import.meta.url)), "inbox.json"), "utf8");

if (argv[0] !== "agents") { process.stderr.write("usage: apex-router agents ...\n"); process.exit(2); }
if (argv[1] === "resolve") {
  if (mode === "resolve-fail") { process.stderr.write("no such item\n"); process.exit(1); }
  process.stdout.write("resolved\n"); process.exit(0);
}
if (argv[1] === "result" && mode !== "slow") {
  if (mode === "result-fail") { process.stderr.write("agents result: no result.md for this id\n"); process.exit(1); }
  process.stdout.write("line one\nAPI_TOKEN=[hidden]\n\u001b[31mred\u001b[0m\n"); process.exit(0);
}
if (mode === "slow") { await new Promise(r => setTimeout(r, 30_000)); }
// huge: 4 MiB in one write; no process.exit, so the pipe gets all of it (macOS pipes are async).
if (mode === "huge") { process.stdout.write("x".repeat(4 * 1024 * 1024)); }
if (mode === "badjson") { process.stdout.write("{not json"); process.exit(0); }
if (mode === "fail") { process.stderr.write("Traceback: \u001b[31mboom\u001b[0m\nsecond line\n"); process.exit(1); }
if (mode === "empty") { process.stdout.write(JSON.stringify({ now: 1793300000, complete: true, counts: { running: 0, needs: 0, done: 5 }, items: [] })); process.exit(0); }
else if (mode !== "huge") process.stdout.write(fixture());
