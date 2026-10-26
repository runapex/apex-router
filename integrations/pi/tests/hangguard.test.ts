import assert from "node:assert/strict";
import { test } from "node:test";

import { decide, DEFAULT_TIMEOUT_S } from "../lib/hangguard.ts";

const cases: { cmd: string; t?: number; jobs?: number; out?: string; timeout?: number; changes: string[]; block?: boolean }[] = [
	{ cmd: 'codex exec -m x "review" > /tmp/o.txt 2>&1', out: 'codex exec -m x "review" > /tmp/o.txt 2>&1 < /dev/null', timeout: DEFAULT_TIMEOUT_S, changes: ["stdin", "timeout"] },
	{ cmd: "cd ~/r && (nohup apex-router xval -m a review > /tmp/x.log 2>&1 &); echo started", t: 30, out: "cd ~/r && (nohup apex-router xval -m a review > /tmp/x.log 2>&1 < /dev/null &); echo started", changes: ["stdin"] },
	{ cmd: "pi -p --no-session 'go' > out.log 2>&1 &", t: 30, out: "pi -p --no-session 'go' > out.log 2>&1 < /dev/null &", changes: ["stdin"] },
	{ cmd: 'codex exec "x" & echo hi', t: 60, out: 'codex exec "x" < /dev/null & echo hi', changes: ["stdin"] },
	{ cmd: "claude -p 'x'", t: 60, out: "claude -p 'x' < /dev/null", changes: ["stdin"] },
	{ cmd: 'codex exec "x" < /dev/null', t: 60, changes: [] },
	{ cmd: "cat p.md | codex exec -", t: 60, changes: [] },
	{ cmd: "codex exec - <<'EOF'\nreview\nEOF", t: 60, changes: [] },
	{ cmd: "echo 'codex exec is a tool'", t: 60, changes: [] },
	{ cmd: "ssh devtest22 true", t: 60, out: "ssh -o BatchMode=yes -o ConnectTimeout=15 devtest22 true", changes: ["ssh"] },
	{ cmd: "scp a.txt h:/tmp/", t: 60, out: "scp -o BatchMode=yes -o ConnectTimeout=15 a.txt h:/tmp/", changes: ["ssh"] },
	{ cmd: "ssh -o BatchMode=yes h true", t: 60, changes: [] },
	{ cmd: "expect -c 'spawn ssh h'", t: 60, changes: [] },
	{ cmd: "sleep 300; tail -3 log", t: 400, jobs: 2, changes: ["poll"], block: true },
	{ cmd: "sleep 300; tail -3 log", t: 400, jobs: 0, changes: [] },
	{ cmd: "sleep 60; tail log", t: 400, jobs: 2, changes: [] },
	{ cmd: "ls", out: "ls", timeout: DEFAULT_TIMEOUT_S, changes: ["timeout"] },
	{ cmd: 'codex exec "x" # apex-hangguard: off', changes: [] },
	{ cmd: "git log --oneline | head -3 && echo a & wait", t: 30, changes: [] },
	// replay regressions (2026-10-15): line continuation, and tool names inside quoted text
	{ cmd: 'codex exec -s read-only -o /tmp/o.md \\\n"review the fix"', t: 900, out: 'codex exec -s read-only -o /tmp/o.md \\\n"review the fix" < /dev/null', changes: ["stdin"] },
	{ cmd: "./scripts/pce_ssh.sh devtest22 -c 'ssh other true'", t: 60, changes: [] },
	{ cmd: 'grep -rn "codex exec" docs/ | head', t: 60, changes: [] },
	{ cmd: 'echo "a;b" && ssh h "uptime; who"', t: 60, out: 'echo "a;b" && ssh -o BatchMode=yes -o ConnectTimeout=15 h "uptime; who"', changes: ["ssh"] },
	{ cmd: "echo a \\; codex exec x", t: 60, changes: [] }, // `\;` is an argument of echo, not a separator
	{ cmd: '# now scp to rc-ven-a\nls', t: 60, changes: [] },
	{ cmd: 'while read scp ds <<< "$spec"; do echo $scp; done', t: 60, changes: [] },
	{ cmd: "sshpass -p ubuntu ssh -o StrictHostKeyChecking=no u@h true", t: 60, changes: [] },
	{ cmd: 'codex exec -s read-only "review" \\\n| tail -60', t: 900, out: 'codex exec -s read-only "review" < /dev/null \\\n| tail -60', changes: ["stdin"] },
	{ cmd: 'codex exec "unbalanced quote', t: 60, changes: [] },
	{ cmd: 'out=$(pi --provider f --model m -p "x" 2>&1 | tail -1); echo "$out"', t: 60, out: 'out=$(pi --provider f --model m -p "x" 2>&1 < /dev/null | tail -1); echo "$out"', changes: ["stdin"] },
	{ cmd: "timeout 400 codex exec -m m x > o.txt 2>&1", t: 500, out: "timeout 400 codex exec -m m x > o.txt 2>&1 < /dev/null", changes: ["stdin"] },
	{ cmd: "cat <<EOF > f.txt\nssh h true\nEOF", t: 60, changes: [] },
	// cross-validation findings (Codex, 2026-10-15)
	{ cmd: "printf payload | { :; codex exec -; }", t: 60, changes: [] },
	{ cmd: "printf p | ( codex exec - )", t: 60, changes: [] },
	{ cmd: "printf payload | ( :; codex exec - )", t: 60, changes: [] },
	{ cmd: "printf payload | { :; { codex exec -; }; }", t: 60, changes: [] },
	{ cmd: "printf p | ( :; ( :; codex exec - ) )", t: 60, changes: [] },
	{ cmd: "(nohup codex exec x > o.log 2>&1 &); echo ok", t: 60, out: "(nohup codex exec x > o.log 2>&1 < /dev/null &); echo ok", changes: ["stdin"] },
	{ cmd: 'printf "%s\n" ${MISSING:-a;ssh host}', t: 60, changes: [] },
	{ cmd: 'ssh -o "BatchMode=no" h true', t: 60, changes: [] },
	{ cmd: "ssh -o ConnectTimeout=90 h true", t: 60, changes: [] },
	{ cmd: "echo `codex exec x`", t: 60, changes: [] },
	{ cmd: "{ codex exec x; } > o.log 2>&1", t: 60, out: "{ codex exec x < /dev/null; } > o.log 2>&1", changes: ["stdin"] },
];

for (const c of cases) {
	test(`hangguard: ${c.cmd.slice(0, 60).replace(/\n/g, "\\n")}`, () => {
		const d = decide(c.cmd, c.t, { jobsRunning: c.jobs ?? 0 });
		assert.deepEqual(d.changes, c.changes);
		assert.equal(d.command, c.out ?? c.cmd);
		if (c.timeout !== undefined) assert.equal(d.timeout, c.timeout);
		else if (c.t !== undefined) assert.equal(d.timeout, c.t);
		assert.equal(Boolean(d.block), Boolean(c.block));
		if (c.block) assert.match(d.reason ?? "", /agent_wait/);
	});
}

test("hangguard: APEX_PI_BASH_TIMEOUT overrides the default", () => {
	assert.equal(decide("ls", undefined, { env: { APEX_PI_BASH_TIMEOUT: "90" } }).timeout, 90);
});

// ---- log (B1) ----
import { chmodSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, statSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { appendLog, logFile, logRow, pruneLogs } from "../lib/hangguard.ts";

test("hangguard log: a row has no command text (a command can hold a secret)", () => {
	const secret = "curl -H 'Authorization: Bearer sk-SECRET' https://x";
	const d = decide(secret, undefined, {});
	const row = logRow(7, d, { timeout: undefined });
	assert.deepEqual(row, { ts: 7, changes: ["timeout"], blocked: false, timeout_before: null, timeout_after: DEFAULT_TIMEOUT_S });
	assert.ok(!JSON.stringify(row).includes("sk-SECRET"));
	assert.ok(!JSON.stringify(row).includes("curl"));
});

test("hangguard log: one file per process per day, mode 0600, appends in order; never throws", () => {
	const dir = join(mkdtempSync(join(tmpdir(), "hg-")), "pi-hangguard");
	const t = new Date(2026, 9, 26, 12).getTime();
	appendLog(dir, { a: 1 }, t, 111);
	appendLog(dir, { a: 2 }, t, 111);
	appendLog(dir, { b: 1 }, t, 222); // another process: its own file
	appendLog(dir, { a: 3 }, t + 86_400_000, 111); // next day: a new file
	assert.deepEqual(readdirSync(dir).sort(), ["2026-10-26-111.jsonl", "2026-10-26-222.jsonl", "2026-10-27-111.jsonl"]);
	assert.equal(readFileSync(logFile(dir, t, 111), "utf8"), '{"a":1}\n{"a":2}\n');
	assert.equal(statSync(logFile(dir, t, 111)).mode & 0o777, 0o600);
	writeFileSync(join(dir, "..", "file"), "x");
	appendLog(join(dir, "..", "file", "sub"), { c: 1 }, t, 1); // mkdir fails: no throw
});

test("hangguard log: prune deletes only own-format files older than the limit", () => {
	const dir = mkdtempSync(join(tmpdir(), "hg-"));
	const now = Date.now();
	for (const f of ["2026-01-01-1.jsonl", "2026-10-26-2.jsonl", "notes.txt", "2026-01-01-1.jsonl.bak"]) writeFileSync(join(dir, f), "{}\n");
	const old = (now - 15 * 86_400_000) / 1000;
	for (const f of ["2026-01-01-1.jsonl", "notes.txt", "2026-01-01-1.jsonl.bak"]) utimesSync(join(dir, f), old, old);
	pruneLogs(dir, now, 14);
	assert.deepEqual(readdirSync(dir).sort(), ["2026-01-01-1.jsonl.bak", "2026-10-26-2.jsonl", "notes.txt"]);
	pruneLogs(join(dir, "missing"), now); // no dir: no throw
});


test("hangguard log: an existing wide dir and file get 0700 / 0600 (review pass 3)", () => {
	const dir = join(mkdtempSync(join(tmpdir(), "hg-")), "pi-hangguard");
	mkdirSync(dir, { mode: 0o755 });
	chmodSync(dir, 0o755);
	const t = new Date(2026, 9, 26, 12).getTime();
	writeFileSync(logFile(dir, t, 333), "", { mode: 0o644 });
	chmodSync(logFile(dir, t, 333), 0o644);
	appendLog(dir, { a: 1 }, t, 333);
	assert.equal(statSync(dir).mode & 0o777, 0o700);
	assert.equal(statSync(logFile(dir, t, 333)).mode & 0o777, 0o600);
});
