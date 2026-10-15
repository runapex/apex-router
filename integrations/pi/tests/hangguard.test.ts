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
