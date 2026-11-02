// node --test integrations/pi/tests/   — job manager against a fake child (no model, no pi).
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { JobManager, killTargets, resumeArgv, RESUME_TASK, TRANSIENT_START, type Meta, type Proc, type Spec } from "../lib/agents.ts";

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

// ---- D1 (owner 2026-10-28): one resume after a transient start error ----------------------------------------
for (const mode of ["key-fail", "conn-fail"]) {
	test(`${mode}: the job is resumed once in the same session after the delay, and ends done; usage is kept`, async () => {
		const { m, done } = mgr({ resumeDelayMs: 600 });
		const j = m.spawn({ task: `mode:${mode}` });
		const t0 = Date.now();
		// while it waits for the resume it is queued, not final, and not notified
		for (let i = 0; i < 40 && m.read(j.id)!.state !== "queued"; i++) await new Promise(r => setTimeout(r, 50));
		const q = m.read(j.id)!;
		assert.equal(q.state, "queued");
		assert.equal(q.resumes, 1);
		assert.equal(done.length, 0);
		const [r] = await m.wait([j.id], 10_000);
		assert.equal(r!.state, "done");
		assert.ok(Date.now() - t0 >= 600, "the resume waits for the delay");
		assert.equal(r!.resumes, 1);
		assert.match(r!.resumed_from ?? "", mode === "key-fail" ? /resolve API key/ : /Connection error/);
		assert.equal(r!.usage.turns, 3, "turns of both runs are counted once each");
		assert.ok(r!.argv.includes(RESUME_TASK));
		assert.match(m.result(j.id)!.text, /FINAL ANSWER: resumed/);
		assert.match(m.result(j.id)!.text, /resumed 1x in the same session/);
		assert.equal(done.length, 1, "one notification, for the final state only");
		m.stop();
	});
}

test("a transient error again after the one resume: failed (no second resume)", async () => {
	const { m } = mgr({ resumeDelayMs: 200 });
	const j = m.spawn({ task: "mode:key-fail-always" });
	const [r] = await m.wait([j.id], 10_000);
	assert.equal(r!.state, "failed");
	assert.equal(r!.resumes, 1);
	assert.match(r!.reason ?? "", /resolve API key/);
	m.stop();
});

test("no resume for other errors, a cancelled job, or a job at its cost limit", async () => {
	const { m } = mgr({ resumeDelayMs: 200 });
	const a = m.spawn({ task: "mode:error" });
	const b = m.spawn({ task: "mode:key-fail", max_cost: 0.001 });   // spends 0.002 (over the hard limit)
	const c = m.spawn({ task: "mode:key-fail", max_cost: 0.0021 });  // 0.002 is past the 85 % soft cap, under the hard limit
	const [ra, rb, rc] = await m.wait([a.id, b.id, c.id], 10_000);
	assert.equal(ra!.state, "failed");
	assert.equal(ra!.resumes, undefined);
	assert.ok(rb!.state === "failed" || rb!.state === "killed(cost)", rb!.state);
	assert.equal(rb!.resumes, undefined);
	assert.equal(rc!.state, "failed");
	assert.equal(rc!.resumes, undefined, "no resume after the soft cap");
	m.stop();
});

test("a resume that waits past the wall clock (pressure RED) ends timed_out(wall), not started late", async () => {
	let lvl: "GREEN" | "RED" = "GREEN";
	const { m, done } = mgr({ resumeDelayMs: 100, pressure: () => lvl, defaultTimeoutS: 2 });
	const j = m.spawn({ task: "mode:key-fail" });
	for (let i = 0; i < 60 && m.read(j.id)!.state === "running"; i++) await new Promise(r => setTimeout(r, 50));
	lvl = "RED";                                          // the resume cannot start
	assert.equal(m.read(j.id)!.resumes, 1);
	await new Promise(r => setTimeout(r, 2300));          // past the 2 s wall clock from the first start
	lvl = "GREEN";
	const [r] = await m.wait([j.id], 5_000);
	assert.equal(r!.state, "timed_out(wall)");
	assert.match(r!.reason ?? "", /wall clock passed while the resume waited/);
	assert.equal(r!.pid, undefined, "never started again");
	assert.equal(done.length, 1);
	m.stop();
});

test("resumeArgv replaces only the task line; TRANSIENT_START matches only the two transient errors", () => {
	assert.deepEqual(resumeArgv(["pi", "-p", "--session-id", "s", "Task: do X"]), ["pi", "-p", "--session-id", "s", RESUME_TASK]);
	assert.deepEqual(resumeArgv(["fake", "mode"]), ["fake", "mode", RESUME_TASK]);
	assert.ok(TRANSIENT_START.test('Failed to resolve API key for provider "foundry" from shell command: x'));
	assert.ok(TRANSIENT_START.test("Connection error."));
	for (const e of ["upstream 500", '429 {"error":"RateLimitReached"}', "context_length_exceeded", "Connection errors are bad words in a task"]) {
		assert.equal(TRANSIENT_START.test(e) && !/errors/.test(e), false, e);
	}
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

test("xval job: stderr progress keeps it alive past idle_s; stdout stays the result (C9)", async () => {
	const { m } = mgr();
	const j = m.spawn({ task: "mode:xval-stderr", kind: "xval", idle_s: 1 });
	const [r] = await m.wait([j.id], 15_000);
	assert.equal(r!.state, "done");
	const text = m.result(j.id)!.text;
	assert.match(text, /XVAL RESULT: no findings/);
	assert.doesNotMatch(text, /codex progress/);
	assert.ok((r!.stderr_offset ?? 0) > 0);
	m.stop();
});

for (const mode of ["xval-truncate", "xval-rotate"]) {
	test(`xval job: stderr.log shrinks (${mode}); later growth is still liveness; a missing file does not throw (F2)`, async () => {
		const { m } = mgr();
		const j = m.spawn({ task: `mode:${mode}`, kind: "xval", idle_s: 2 });
		const [r] = await m.wait([j.id], 20_000);
		m.stop();
		assert.equal(r!.state, "done", r!.reason);
		assert.match(m.result(j.id)!.text, /XVAL RESULT: rotated ok/);
		assert.ok((r!.stderr_offset ?? 0) < 4000); // the offset follows the smaller file
	});
}

test("xval job with no output at all: still timed_out(idle)", async () => {
	const { m } = mgr();
	const j = m.spawn({ task: "mode:silent", kind: "xval", idle_s: 1 });
	const [r] = await m.wait([j.id], 15_000);
	assert.equal(r!.state, "timed_out(idle)");
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

// ---- list() cache: each tick calls list() 5 times; one parse of ~1000 meta.json files blocked pi input for ~85 ms
function seedDone(root: string, id: string, created: number, extra: Partial<Meta> = {}) {
	const m = { id, state: "done", agent: "worker", kind: "pi", task: "t", cwd: "/home/u", argv: [], created, timeout_s: 60, idle_s: 60,
		session_id: id, usage: { turns: 1, input: 1, output: 1, cost: 0.5 }, notified: true, ...extra };
	mkdirSync(join(root, id), { recursive: true });
	const p = join(root, id, "meta.json");
	writeFileSync(`${p}.tmp`, JSON.stringify(m));
	renameSync(`${p}.tmp`, p); // the same atomic rename as JobManager.write
	return p;
}

test("list(): an unchanged meta.json is parsed once, not on every call", () => {
	const { m, root } = mgr();
	const paths = [seedDone(root, "j1", 1), seedDone(root, "j2", 2)];
	assert.deepEqual(m.list().map(x => x.id), ["j1", "j2"]);
	for (const p of paths) chmodSync(p, 0o000); // stat still works, a read fails
	try {
		assert.deepEqual(m.list().map(x => x.id), ["j1", "j2"]);
	} finally {
		for (const p of paths) chmodSync(p, 0o644);
		m.stop();
	}
});

test("list(): a rewritten meta.json (another process) is read again", () => {
	const { m, root } = mgr();
	seedDone(root, "j1", 1);
	assert.equal(m.list()[0]!.state, "done");
	seedDone(root, "j1", 1, { state: "failed", reason: "x" });
	const [r] = m.list();
	assert.equal(r!.state, "failed");
	assert.equal(r!.reason, "x");
	m.stop();
});

test("list(): a caller that changes a listed meta does not change the next list()", () => {
	const { m, root } = mgr();
	seedDone(root, "j1", 1);
	const a = m.list()[0]!;
	a.state = "failed";
	a.notified = false;
	assert.equal(m.list()[0]!.state, "done");
	assert.equal(m.list()[0]!.notified, true);
	m.stop();
});

test("list(): a removed job folder leaves the list", () => {
	const { m, root } = mgr();
	seedDone(root, "j1", 1);
	seedDone(root, "j2", 2);
	assert.equal(m.list().length, 2);
	rmSync(join(root, "j1"), { recursive: true });
	assert.deepEqual(m.list().map(x => x.id), ["j2"]);
	m.stop();
});

test("no fake child left after the suite", () => {
	const left = execFileSync("ps", ["-A", "-o", "command="], { encoding: "utf8" }).split("\n").filter(l => l.startsWith(process.execPath) && l.includes("fake-pi.mjs"));
	assert.deepEqual(left, []);
});

// ---- coordinator: per-agent thinking / max depth -------------------------------------------
import { BUILTIN, loadAgents, parseDepth } from "../lib/agentdefs.ts";
import { childEnv } from "../lib/agents.ts";

test("childEnv: plain job is a leaf (max = its depth); depth + 1", () => {
	const e = childEnv({ APEX_AGENT_DEPTH: "0" }, "j1");
	assert.equal(e.APEX_AGENT_DEPTH, "1");
	assert.equal(e.APEX_AGENT_MAX_DEPTH, "1");
	assert.equal(e.APEX_AGENT_JOB, "j1");
});

test("childEnv: coordinator (max_depth 2) may start workers; its workers are leaves", () => {
	const coord = childEnv({ APEX_AGENT_DEPTH: "0" }, "c", 2);
	assert.deepEqual([coord.APEX_AGENT_DEPTH, coord.APEX_AGENT_MAX_DEPTH], ["1", "2"]);
	const worker = childEnv(coord, "w");
	assert.deepEqual([worker.APEX_AGENT_DEPTH, worker.APEX_AGENT_MAX_DEPTH], ["2", "2"]);
});

test("childEnv: a max_depth 3 coordinator's default worker is still a leaf (xval P1)", () => {
	const coord = childEnv({ APEX_AGENT_DEPTH: "0" }, "c", 3);
	assert.deepEqual([coord.APEX_AGENT_DEPTH, coord.APEX_AGENT_MAX_DEPTH], ["1", "3"]);
	const worker = childEnv(coord, "w");
	assert.equal(worker.APEX_AGENT_MAX_DEPTH, worker.APEX_AGENT_DEPTH);
});

test("childEnv: inherited max is ignored; bad or huge values never exceed depth 3 (xval P1)", () => {
	for (const bad of ["4", "Infinity", "abc", "-1", "1e9"]) {
		const e = childEnv({ APEX_AGENT_DEPTH: "1", APEX_AGENT_MAX_DEPTH: bad }, "x");
		assert.equal(e.APEX_AGENT_MAX_DEPTH, "2");                    // leaf at depth 2
	}
	assert.equal(childEnv({ APEX_AGENT_DEPTH: "2" }, "x", 9).APEX_AGENT_MAX_DEPTH, "3");
	assert.equal(childEnv({ APEX_AGENT_DEPTH: "junk" }, "x", 9).APEX_AGENT_DEPTH, "4"); // bad depth -> beyond cap
	assert.equal(childEnv({ APEX_AGENT_DEPTH: "junk" }, "x", 9).APEX_AGENT_MAX_DEPTH, "3"); // so it cannot spawn
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

test("childEnv: depth never passes MAX_TREE_DEPTH + 1, even from a bad parent depth (xval pass 2)", () => {
	for (const bad of ["junk", "NaN", "-1", "-Infinity", "0.5", "99"]) {
		const e = childEnv({ APEX_AGENT_DEPTH: bad }, "x", 9);
		assert.ok(Number(e.APEX_AGENT_DEPTH) <= 4, bad);
		assert.ok(Number(e.APEX_AGENT_DEPTH) >= Number(e.APEX_AGENT_MAX_DEPTH), bad); // cannot spawn
	}
});
