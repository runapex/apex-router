// node --test integrations/pi/tests/   — job manager against a fake child (no model, no pi).
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { JobManager, killTargets, type Meta, type Proc, type Spec } from "../lib/agents.ts";

const FAKE = join(dirname(fileURLToPath(import.meta.url)), "fixtures", "fake-pi.mjs");

function mgr(over: Partial<ConstructorParameters<typeof JobManager>[0]> = {}, mode = "normal") {
	const root = mkdtempSync(join(tmpdir(), "apexjobs-"));
	const done: Meta[] = [];
	const m = new JobManager({
		root,
		argv: (spec: Spec) => [process.execPath, FAKE, spec.task.startsWith("mode:") ? spec.task.slice(5) : mode],
		pressure: () => "GREEN",
		tickMs: 200,
		graceMs: 1500,
		onDone: x => done.push(x),
		...over,
	});
	return { m, done, root };
}

const alive = (pid: number) => {
	try {
		process.kill(pid, 0);
		return true;
	} catch {
		return false;
	}
};

test("normal job: spawn returns at once, result is pushed, final text kept", async () => {
	const { m, done } = mgr();
	const j = m.spawn({ task: "mode:normal" });
	assert.equal(j.state, "running");
	const [r] = await m.wait([j.id], 10_000);
	assert.equal(r!.state, "done");
	assert.match(m.result(j.id)!.text, /FINAL ANSWER: 42/);
	assert.equal(r!.usage.turns, 1);
	assert.equal(done.length, 1);
	assert.equal(done[0]!.id, j.id);
	m.stop();
});

test("model error is failed, not done", async () => {
	const { m } = mgr();
	const j = m.spawn({ task: "mode:error" });
	const [r] = await m.wait([j.id], 10_000);
	assert.equal(r!.state, "failed");
	assert.match(r!.reason ?? "", /upstream 500/);
	m.stop();
});

test("429 then recovery (pi retried): done, not failed", async () => {
	const { m } = mgr();
	const j = m.spawn({ task: "mode:recover-429" });
	const [r] = await m.wait([j.id], 10_000);
	assert.equal(r!.state, "done");
	assert.equal(r!.model_error, undefined);
	assert.equal(r!.retries, 1);
	assert.match(m.result(j.id)!.text, /recovered/);
	m.stop();
});

test("429 after all pi retries: failed with a rate-limit reason and the resume command", async () => {
	const { m } = mgr();
	const j = m.spawn({ task: "mode:fail-429" });
	const [r] = await m.wait([j.id], 10_000);
	assert.equal(r!.state, "failed");
	assert.match(r!.reason ?? "", /^rate limited \(429\) after 3 pi retries\. Resume later: cd .* pi --session-id apexjob-/);
	m.stop();
});

test("after a 429, new jobs start one at a time for the cooldown", async () => {
	const { m } = mgr({ cap: 4 });
	const a = m.spawn({ task: "mode:recover-429" });
	await m.wait([a.id], 10_000);
	const ids = [m.spawn({ task: "mode:slow", timeout_s: 30 }).id, m.spawn({ task: "mode:slow", timeout_s: 30 }).id];
	assert.deepEqual(ids.map(id => m.read(id)!.state), ["running", "queued(pressure)"]);
	for (const id of ids) await m.cancel(id);
	m.stop();
});

test("crash: failed with exit code and stderr in the result", async () => {
	const { m } = mgr();
	const j = m.spawn({ task: "mode:crash" });
	const [r] = await m.wait([j.id], 10_000);
	assert.equal(r!.state, "failed");
	assert.match(m.result(j.id)!.text, /boom: fake crash/);
	m.stop();
});

test("silent hang: timed_out(idle) within idle_s + grace, process gone", async () => {
	const { m } = mgr();
	const j = m.spawn({ task: "mode:silent", idle_s: 1.5 });
	const t0 = Date.now();
	const [r] = await m.wait([j.id], 15_000);
	assert.equal(r!.state, "timed_out(idle)");
	assert.ok(Date.now() - t0 < 1500 + 1500 + 3000, `took ${Date.now() - t0} ms`);
	assert.equal(alive(r!.pid!), false);
	assert.match(r!.last_tool ?? "", /sleep 999/);
	m.stop();
});

test("wall clock: an alive-but-slow job is timed_out(wall)", async () => {
	const { m } = mgr();
	const j = m.spawn({ task: "mode:slow", timeout_s: 2, idle_s: 60 });
	const [r] = await m.wait([j.id], 15_000);
	assert.equal(r!.state, "timed_out(wall)");
	m.stop();
});

for (const mode of ["grandchild", "ignore-term"]) {
	test(`cancel (${mode}): job killed and the detached grandchild is gone`, async () => {
		const { m, root } = mgr();
		const pidFile = join(root, `gc-${mode}.pid`);
		process.env.FAKE_GC_PID_FILE = pidFile;
		const j = m.spawn({ task: `mode:${mode}` });
		for (let i = 0; i < 50 && !existsSync(pidFile); i++) await new Promise(r => setTimeout(r, 100));
		const gc = Number(readFileSync(pidFile, "utf8"));
		assert.ok(alive(gc), "grandchild started");
		const r = await m.cancel(j.id);
		assert.equal(r!.state, "killed");
		await new Promise(r => setTimeout(r, 300));
		assert.equal(alive(gc), false, "grandchild must not survive");
		assert.equal(alive(r!.pid!), false, "child must not survive");
		m.stop();
	});
}

test("cancel (term-spawn): a child started during SIGTERM handling, after its parent exits, is killed", async () => {
	const { m, root } = mgr();
	const pidFile = join(root, "late.pid");
	process.env.FAKE_GC_PID_FILE = pidFile;
	const j = m.spawn({ task: "mode:term-spawn" });
	await new Promise(r => setTimeout(r, 500));
	const r = await m.cancel(j.id);
	assert.equal(r!.state, "killed");
	const late = Number(readFileSync(pidFile, "utf8"));
	await new Promise(r => setTimeout(r, 300));
	assert.equal(alive(late), false, "late child must not survive");
	m.stop();
});

test("killTargets: rules (a) tree, (b) new child by parent or group; reused pid and other groups are safe", () => {
	const P = (pid: number, ppid: number, pgid: number, start = "t0"): Proc => ({ pid, ppid, pgid, start });
	const tree0 = [P(100, 1, 100), P(101, 100, 101)]; // child pi (own group) and its bash command (own group)
	const before = [...tree0, P(500, 1, 500), P(600, 1, 600)];
	const after = [
		P(101, 1, 101),          // bash survived, reparented: (a)
		P(200, 1, 100, "t1"),    // new, parent gone, in the child's group: (b) by group
		P(201, 101, 201, "t1"),  // new, own group, parent is a job process: (b) by parent
		// (a reused pid 100 cannot appear here: the kernel does not reuse a pid while group 100 has members;
		//  the reused-group case has its own test below)
		P(500, 1, 500),          // the user shell group: unrelated
		P(601, 600, 600, "t1"),  // new child of an unrelated process: not ours
	];
	const r = killTargets(tree0, before, after, new Set([500]));
	assert.deepEqual(r.pgids.sort((a, b) => a - b), [100, 101, 201]);
	assert.deepEqual(r.pids.sort((a, b) => a - b), [101, 200, 201]);
});

test("killTargets: a reused group (leader pid is now another process) is not killed (Codex pass 4)", () => {
	const P = (pid: number, ppid: number, pgid: number, start = "t0"): Proc => ({ pid, ppid, pgid, start });
	const tree0 = [P(100, 1, 100, "old")];
	const after = [P(100, 1, 100, "new"), P(200, 100, 100, "new")]; // unrelated process took pid 100 and group 100
	assert.deepEqual(killTargets(tree0, tree0, after, new Set([500, 600])), { pgids: [], pids: [] });
});

test("cap: 6 jobs with cap 4 -> 4 running, 2 queued; queue drains", async () => {
	const { m } = mgr({ cap: 4 });
	const ids = Array.from({ length: 6 }, () => m.spawn({ task: "mode:normal" }).id);
	const states = ids.map(id => m.read(id)!.state);
	assert.equal(states.filter(s => s === "running").length, 4);
	assert.equal(states.filter(s => s === "queued").length, 2);
	const rs = await m.wait(ids, 20_000);
	assert.deepEqual(rs.map(r => r.state), Array(6).fill("done"));
	m.stop();
});

test("pressure RED queues a new job as queued(pressure); it starts when GREEN", async () => {
	let lvl: "GREEN" | "RED" = "RED";
	const { m } = mgr({ pressure: () => lvl });
	const j = m.spawn({ task: "mode:normal" });
	assert.equal(m.read(j.id)!.state, "queued(pressure)");
	lvl = "GREEN";
	const [r] = await m.wait([j.id], 10_000);
	assert.equal(r!.state, "done");
	m.stop();
});

test("cancel a queued job: killed without starting a process", async () => {
	const { m } = mgr({ pressure: () => "RED" });
	const j = m.spawn({ task: "mode:normal" });
	const r = await m.cancel(j.id);
	assert.equal(r!.state, "killed");
	assert.equal(r!.pid, undefined);
	m.stop();
});

test("parent restart: a new manager adopts a live job and finishes it", async () => {
	const { m, root } = mgr();
	const j = m.spawn({ task: "mode:slow", timeout_s: 60 });
	m.stop();
	const done: Meta[] = [];
	assert.ok(m.read(j.id)!.pid_start, "pid_start recorded at spawn");
	// the first manager is in this process, so "owner gone" is simulated with ownerAlive
	const m2 = new JobManager({ root, pressure: () => "GREEN", tickMs: 200, graceMs: 1000, onDone: x => done.push(x), ownerAlive: x => x.owner !== m.self });
	assert.deepEqual(m2.adopt().map(x => x.id), [j.id]);
	assert.equal(m2.read(j.id)!.owner, m2.self);
	process.kill(m2.read(j.id)!.pid!, "SIGKILL");
	const [r] = await m2.wait([j.id], 10_000);
	assert.equal(r!.state, "failed");
	assert.equal(done.length, 1);
	m2.stop();
});

test("a job id is notified exactly once", async () => {
	const { m, done } = mgr();
	const j = m.spawn({ task: "mode:normal" });
	await m.wait([j.id], 10_000);
	m.tick();
	m.tick();
	assert.equal(done.filter(d => d.id === j.id).length, 1);
	m.stop();
});

test("pi /reload: a new manager in the same process adopts the old manager's running job", async () => {
	const { m, root } = mgr();
	const j = m.spawn({ task: "mode:slow", timeout_s: 60 });
	m.stop(); // session_shutdown of the old extension instance
	const done: Meta[] = [];
	const m2 = new JobManager({ root, pressure: () => "GREEN", tickMs: 200, graceMs: 1000, onDone: x => done.push(x) }); // default ownerAlive
	assert.deepEqual(m2.adopt().map(x => x.id), [j.id]);
	await m2.cancel(j.id);
	assert.equal(done.length, 1);
	m2.stop();
});

test("a manager that is not stopped keeps its job (same process)", () => {
	const { m, root } = mgr({ pressure: () => "RED" });
	const j = m.spawn({ task: "mode:normal" });
	const m2 = new JobManager({ root, pressure: () => "GREEN" });
	assert.deepEqual(m2.adopt(), []);
	assert.equal(m2.read(j.id)!.owner, m.self);
	m2.stop();
	m.stop();
});

test("one writer: a second live manager does not notify, start, or cancel another manager's job", async () => {
	const { m, root, done } = mgr({ pressure: () => "RED" });
	const other: Meta[] = [];
	const m2 = new JobManager({ root, pressure: () => "GREEN", tickMs: 200, onDone: x => other.push(x), ownerAlive: () => true });
	const j = m.spawn({ task: "mode:normal" });
	m2.tick(); // GREEN for m2, but the queued job is m's: m2 must not start it
	assert.equal(m2.read(j.id)!.state, "queued(pressure)");
	const c = await m2.cancel(j.id);
	assert.equal(c!.state, "queued(pressure)"); // refused: owner alive
	m.stop();
	m2.stop();
	assert.equal(other.length + done.length, 0);
});

test("adopt continues the event offset: usage is not counted twice", async () => {
	const { m, root } = mgr();
	const j = m.spawn({ task: "mode:normal" });
	const [r] = await m.wait([j.id], 10_000);
	assert.equal(r!.usage.turns, 1);
	m.stop();
	// re-open the ended, notified job as running in a dead owner's name, then adopt it
	const meta = m.read(j.id)!;
	writeFileSync(join(root, j.id, "meta.json"), JSON.stringify({ ...meta, state: "running", pid: 999_999, notified: false }));
	const m2 = new JobManager({ root, pressure: () => "GREEN", tickMs: 200, ownerAlive: () => false });
	m2.adopt();
	const [r2] = await m2.wait([j.id], 5000);
	assert.equal(r2!.usage.turns, 1, "the adopter must not count the first turn again");
	m2.stop();
});

test("a failed notification is tried again, then given up after 3 tries", async () => {
	let calls = 0;
	const { m } = mgr({ onDone: () => { calls++; throw new Error("deliver failed"); } });
	const j = m.spawn({ task: "mode:normal" });
	await m.wait([j.id], 10_000);
	const after = calls; // wait() ticks too: at most one try per tick
	for (let i = 0; i < 5; i++) m.tick();
	assert.equal(calls, 3);
	assert.ok(after <= 3);
	assert.equal(m.read(j.id)!.notified, true);
	assert.equal(m.read(j.id)!.notify_failed, true);
	m.stop();
});

test("cancel does not kill a reused pid", async () => {
	const { m, root } = mgr();
	const j = m.spawn({ task: "mode:slow", timeout_s: 60 });
	await new Promise(r => setTimeout(r, 300));
	const meta = m.read(j.id)!;
	process.kill(meta.pid!, "SIGKILL");
	await new Promise(r => setTimeout(r, 200));
	// pretend the dead child's pid now belongs to this test process
	writeFileSync(join(root, j.id, "meta.json"), JSON.stringify({ ...meta, pid: process.pid, pid_start: "Thu Jan  1 00:00:00 1970" }));
	const r = await m.cancel(j.id);
	assert.notEqual(r!.state, "killed");
	assert.equal(alive(process.pid), true);
	m.stop();
});

test("a live manager adopts a job whose owner ends later (not only at start)", async () => {
	const { m, root } = mgr();
	let ownerUp = true;
	const m2 = new JobManager({ root, pressure: () => "GREEN", tickMs: 50, ownerAlive: x => (x.owner === m.self ? ownerUp : true) });
	m2.adopt(); // nothing to adopt yet
	const j = m.spawn({ task: "mode:silent", idle_s: 1 });
	m.stop();
	ownerUp = false; // the owner process ends after m2 started
	const [r] = await m2.wait([j.id], 15_000);
	assert.equal(r!.owner, m2.self);
	assert.equal(r!.state, "timed_out(idle)");
	m2.stop();
});

test("meta.json writes are atomic (no .tmp left, always parseable)", async () => {
	const { m, root } = mgr();
	const j = m.spawn({ task: "mode:slow", timeout_s: 3 });
	const t0 = Date.now();
	while (Date.now() - t0 < 1500) {
		JSON.parse(readFileSync(join(root, j.id, "meta.json"), "utf8"));
		await new Promise(r => setTimeout(r, 5));
	}
	await m.cancel(j.id);
	assert.deepEqual(readdirSync(join(root, j.id)).filter(f => f.endsWith(".tmp")), []);
	m.stop();
});

test("no fake child left after the suite", () => {
	const left = execFileSync("ps", ["-A", "-o", "command="], { encoding: "utf8" }).split("\n").filter(l => l.startsWith(process.execPath) && l.includes("fake-pi.mjs"));
	assert.deepEqual(left, []);
});

// ---- coordinator: per-agent thinking / max depth -------------------------------------------
import { BUILTIN, loadAgents, parseDepth } from "../lib/agentdefs.ts";
import { childEnv } from "../lib/agents.ts";

test("childEnv: plain job inherits max depth; depth + 1", () => {
	const e = childEnv({ APEX_AGENT_DEPTH: "0" }, "j1");
	assert.equal(e.APEX_AGENT_DEPTH, "1");
	assert.equal(e.APEX_AGENT_MAX_DEPTH, undefined);
	assert.equal(e.APEX_AGENT_JOB, "j1");
});

test("childEnv: coordinator (max_depth 2) from top level may start workers, workers may not", () => {
	const coord = childEnv({ APEX_AGENT_DEPTH: "0" }, "c", 2);
	assert.equal(coord.APEX_AGENT_DEPTH, "1");
	assert.equal(coord.APEX_AGENT_MAX_DEPTH, "2");              // 1 < 2: may spawn
	const worker = childEnv(coord, "w");                        // worker inherits max 2
	assert.equal(worker.APEX_AGENT_DEPTH, "2");
	assert.ok(Number(worker.APEX_AGENT_DEPTH) >= Number(worker.APEX_AGENT_MAX_DEPTH)); // may not spawn
});

test("childEnv: max depth is capped at 3 and never lowers an inherited limit", () => {
	assert.equal(childEnv({ APEX_AGENT_DEPTH: "2", APEX_AGENT_MAX_DEPTH: "3" }, "x", 9).APEX_AGENT_MAX_DEPTH, "3");
	assert.equal(childEnv({ APEX_AGENT_DEPTH: "0", APEX_AGENT_MAX_DEPTH: "3" }, "x", 1).APEX_AGENT_MAX_DEPTH, "3");
});

test("parseDepth and frontmatter: thinking + max_depth; bad values ignored", () => {
	assert.equal(parseDepth("2"), 2);
	assert.equal(parseDepth("99"), 3);
	for (const bad of [undefined, "", "0", "-1", "x", "1.5"]) assert.equal(parseDepth(bad), undefined);
	const dir = mkdtempSync(join(tmpdir(), "agentdefs-"));
	writeFileSync(join(dir, "c.md"), "---\nname: coord2\ndescription: d\nthinking: high\nmax_depth: 2\n---\nbody\n");
	writeFileSync(join(dir, "b.md"), "---\nname: bad\nthinking: ultra\nmax_depth: lots\n---\nbody\n");
	const all = loadAgents(dir);
	const c = all.find(a => a.name === "coord2")!;
	assert.equal(c.thinking, "high");
	assert.equal(c.max_depth, 2);
	const b = all.find(a => a.name === "bad")!;
	assert.equal(b.thinking, undefined);
	assert.equal(b.max_depth, undefined);
});

test("builtin coordinator: high thinking, depth 2, mentions xval and cwd", () => {
	const c = BUILTIN.find(a => a.name === "coordinator")!;
	assert.equal(c.thinking, "high");
	assert.equal(c.max_depth, 2);
	assert.match(c.prompt, /xval/);
	assert.match(c.prompt, /cwd/);
});
