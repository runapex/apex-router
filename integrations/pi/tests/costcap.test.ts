// node --test integrations/pi/tests/*.test.ts — A1 cost soft cap: pure decision, fake clock, fake child.
import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { childCostCap, childEnv, childSelfSteer, codeHash, costAction, costAdvice, costAdviceLine, FINAL, JobManager, readSteer, SOFT_CAP_MESSAGE, softCapGate, type Meta, type Spec } from "../lib/agents.ts";

const FAKE = join(dirname(fileURLToPath(import.meta.url)), "fixtures", "fake-pi.mjs");

function mgr(now?: () => number) {
	const root = mkdtempSync(join(tmpdir(), "apexcost-"));
	const done: Meta[] = [];
	const m = new JobManager({
		root, argv: (spec: Spec) => [process.execPath, FAKE, spec.task], pressure: () => "GREEN",
		tickMs: 100, graceMs: 1000, onDone: x => done.push(x), ...(now ? { now } : {}),
	});
	return { m, done, root };
}

test("costAction: soft at 85 %, hard only above 110 %, once, no cap = none", () => {
	assert.equal(costAction(0.84, 1, false), "none");
	assert.equal(costAction(0.85, 1, false), "soft");
	assert.equal(costAction(0.95, 1, true), "none");
	assert.equal(costAction(1.05, 1, true), "none"); // the old hard stop fired here; now it is salvage budget
	assert.equal(costAction(1.10, 1, true), "none");
	assert.equal(costAction(1.11, 1, true), "hard");
	assert.equal(costAction(1.5, 1, false), "hard"); // a jump past both: hard wins
	assert.equal(costAction(99, undefined, false), "none");
});

test("childEnv: job dir is passed, and a parent job dir is never inherited", () => {
	assert.equal(childEnv({}, "j", undefined, "/x/j").APEX_AGENT_JOB_DIR, "/x/j");
	assert.equal(childEnv({ APEX_AGENT_JOB_DIR: "/parent" }, "j").APEX_AGENT_JOB_DIR, undefined);
});

test("childEnv: max_cost goes to the child as APEX_AGENT_MAX_COST; unset stays unset (C1a)", () => {
	assert.equal(childEnv({}, "j", undefined, "/x/j", 2.5).APEX_AGENT_MAX_COST, "2.5");
	assert.equal(childEnv({}, "j", undefined, "/x/j").APEX_AGENT_MAX_COST, undefined);
	assert.equal(childEnv({ APEX_AGENT_MAX_COST: "9" }, "j", undefined, "/x/j").APEX_AGENT_MAX_COST, undefined); // never inherited
});

test("child self-steer: at 85 % of its budget, no parent steer.json, the child writes it once (C1a)", () => {
	const d = mkdtempSync(join(tmpdir(), "apexself-"));
	assert.equal(childSelfSteer(d, 0.84, 1, 7), false);
	assert.equal(existsSync(join(d, "steer.json")), false);
	assert.equal(childSelfSteer(d, 0.5, undefined, 7), false); // no budget: no steer
	assert.equal(childSelfSteer(undefined, 0.9, 1, 7), false); // no job dir: no steer
	assert.equal(childSelfSteer(d, 0.85, 1, 7), true);
	assert.deepEqual(JSON.parse(readFileSync(join(d, "steer.json"), "utf8")), { kind: "soft_cap", message: SOFT_CAP_MESSAGE, at: 7 });
	assert.equal(childSelfSteer(d, 0.95, 1, 8), false); // once: an existing steer.json is kept
	assert.equal(readSteer(d)!.at, 7);
	assert.deepEqual(softCapGate(d, false), { block: true, reason: SOFT_CAP_MESSAGE, steer: true });
});

test("parent and child write the same steer.json content (C1a)", async () => {
	let t = 2_000_000;
	const { m, root } = mgr(() => t);
	const j = m.spawn({ task: "spend", max_cost: 0.5 });
	await m.wait([j.id], 15_000);
	m.stop();
	const parent = JSON.parse(readFileSync(join(root, j.id, "steer.json"), "utf8"));
	const d = mkdtempSync(join(tmpdir(), "apexself-"));
	childSelfSteer(d, 0.5, 0.5, parent.at);
	assert.deepEqual(JSON.parse(readFileSync(join(d, "steer.json"), "utf8")), parent);
});

test("childCostCap: old parent (only APEX_AGENT_JOB): job dir from the default root, budget from meta.json; child self-steers (F1)", () => {
	const root = mkdtempSync(join(tmpdir(), "apexold-"));
	mkdirSync(join(root, "job1"));
	writeFileSync(join(root, "job1", "meta.json"), JSON.stringify({ id: "job1", max_cost: 1 }));
	const c = childCostCap({ APEX_AGENT_JOB: "job1" }, root);
	assert.deepEqual(c, { dir: join(root, "job1"), maxCost: 1 });
	assert.equal(childSelfSteer(c.dir, 0.9, c.maxCost, 7), true);
	assert.deepEqual(softCapGate(c.dir, false), { block: true, reason: SOFT_CAP_MESSAGE, steer: true }); // gate reads the same dir
	// parent since 3796382: APEX_AGENT_JOB_DIR set, no APEX_AGENT_MAX_COST: budget from meta.json
	assert.deepEqual(childCostCap({ APEX_AGENT_JOB: "job1", APEX_AGENT_JOB_DIR: join(root, "job1") }, root), { dir: join(root, "job1"), maxCost: 1 });
	// env budget wins over meta.json when the env dir is this job's dir
	assert.deepEqual(childCostCap({ APEX_AGENT_JOB: "job1", APEX_AGENT_JOB_DIR: join(root, "job1"), APEX_AGENT_MAX_COST: "2.5" }, root), { dir: join(root, "job1"), maxCost: 2.5 });
});

test("childCostCap: mixed-version nesting: an inherited parent dir and budget are not trusted (F1, xval pass 2)", () => {
	const root = mkdtempSync(join(tmpdir(), "apexnest-"));
	for (const [id, cost] of [["parent", 50], ["child", 2]] as const) {
		mkdirSync(join(root, id));
		writeFileSync(join(root, id, "meta.json"), JSON.stringify({ id, max_cost: cost }));
	}
	// an old parent copied its own env to the child: JOB_DIR and MAX_COST belong to "parent"
	const c = childCostCap({ APEX_AGENT_JOB: "child", APEX_AGENT_JOB_DIR: join(root, "parent"), APEX_AGENT_MAX_COST: "50" }, root);
	assert.deepEqual(c, { dir: join(root, "child"), maxCost: 2 });
	assert.equal(childSelfSteer(c.dir, 1.8, c.maxCost, 7), true);
	assert.equal(readSteer(join(root, "parent")), null); // the parent's steer.json is not touched
	// no job id: nothing is trusted, even a dir in the env
	assert.deepEqual(childCostCap({ APEX_AGENT_JOB_DIR: join(root, "parent"), APEX_AGENT_MAX_COST: "50" }, root), { dir: undefined, maxCost: undefined });
});

test("childCostCap: no budget gives no self-steer; a bad job id gives no dir (F1)", () => {
	const root = mkdtempSync(join(tmpdir(), "apexold-"));
	mkdirSync(join(root, "nocap"));
	writeFileSync(join(root, "nocap", "meta.json"), JSON.stringify({ id: "nocap" }));
	const c = childCostCap({ APEX_AGENT_JOB: "nocap" }, root);
	assert.deepEqual(c, { dir: join(root, "nocap"), maxCost: undefined });
	assert.equal(childSelfSteer(c.dir, 99, c.maxCost, 7), false);
	assert.deepEqual(childCostCap({ APEX_AGENT_JOB: "missing" }, root), { dir: join(root, "missing"), maxCost: undefined }); // no meta.json
	writeFileSync(join(root, "nocap", "meta.json"), "{junk");
	assert.equal(childCostCap({ APEX_AGENT_JOB: "nocap" }, root).maxCost, undefined); // bad meta.json: fail safe
	for (const id of ["../nocap", "a/b", "a\\b", "..", ".", ""]) assert.deepEqual(childCostCap({ APEX_AGENT_JOB: id }, root), { dir: undefined, maxCost: undefined }, id);
	assert.deepEqual(childCostCap({}, root), { dir: undefined, maxCost: undefined });
});

test("child steer.json before the parent tick: finish() marks done with capped: true (F3)", async () => {
	const { m } = mgr();
	const j = m.spawn({ task: "self-steer", max_cost: 10 }); // parent cost stays far below 85 %
	const [r] = await m.wait([j.id], 15_000);
	m.stop();
	assert.equal(r!.state, "done");
	assert.equal(r!.capped, true);
	assert.equal(typeof r!.soft_cap_at, "number");
	assert.equal(r!.soft_cap_at, readSteer(join(m.root, j.id))!.at);
});

test("codeHash: changes when a file changes; a missing file does not throw (C1b)", () => {
	const d = mkdtempSync(join(tmpdir(), "apexhash-"));
	const a = join(d, "a.ts");
	writeFileSync(a, "one");
	const h1 = codeHash([a, join(d, "missing.ts")]);
	assert.equal(codeHash([a, join(d, "missing.ts")]), h1);
	writeFileSync(a, "two");
	assert.notEqual(codeHash([a, join(d, "missing.ts")]), h1);
});

test("softCapGate: no steer = allow; steer = block, steer the model once", () => {
	const d = mkdtempSync(join(tmpdir(), "apexsteer-"));
	assert.deepEqual(softCapGate(d, false), { block: false });
	assert.deepEqual(softCapGate(undefined, false), { block: false });
	
	writeFileSync(join(d, "steer.json"), JSON.stringify({ kind: "soft_cap", message: SOFT_CAP_MESSAGE, at: 1 }));
	assert.deepEqual(softCapGate(d, false), { block: true, reason: SOFT_CAP_MESSAGE, steer: true });
	assert.deepEqual(softCapGate(d, true), { block: true, reason: SOFT_CAP_MESSAGE, steer: false });
	assert.equal(readSteer(d)!.kind, "soft_cap");
});

test("obeying child: soft cap message, salvage report, state done + capped, soft_cap_at from the clock", async () => {
	let t = 1_000_000;
	const { m, done, root } = mgr(() => t);
	const j = m.spawn({ task: "spend", max_cost: 0.5 });
	t = 1_005_000; // fake clock: soft_cap_at must come from the injected clock
	const [r] = await m.wait([j.id], 15_000);
	m.stop();
	assert.equal(r!.state, "done");
	assert.equal(r!.capped, true);
	assert.equal(r!.soft_cap_at, 1_005_000);
	assert.ok(r!.soft_cap_cost! >= 0.5 * 0.85 && r!.soft_cap_cost! <= 0.55 + 1e-9, String(r!.soft_cap_cost));
	assert.ok(FINAL.has(r!.state)); // existing consumers see a plain final "done"
	const steer = JSON.parse(readFileSync(join(root, j.id, "steer.json"), "utf8"));
	assert.equal(steer.message, SOFT_CAP_MESSAGE);
	assert.match(m.result(j.id)!.text, /SALVAGE REPORT/);
	assert.match(m.result(j.id)!.text, /— done \(capped:/);
	assert.equal(done.length, 1);
	const meta = JSON.parse(readFileSync(join(root, j.id, "meta.json"), "utf8"));
	assert.equal(meta.soft_cap_at, 1_005_000);
	assert.equal(meta.task, "spend");
	assert.ok(!JSON.stringify(meta).includes("final report now")); // no message text in meta.json
});

test("ignoring child: runs past max_cost into the salvage budget, killed(cost) only above 110 %", async () => {
	const { m, root } = mgr();
	const j = m.spawn({ task: "spend-ignore", max_cost: 0.5 });
	const [r] = await m.wait([j.id], 20_000);
	m.stop();
	assert.equal(r!.state, "killed(cost)");
	assert.equal(r!.capped, undefined);
	assert.ok(r!.soft_cap_at !== undefined);
	assert.ok(r!.usage.cost > 0.5 * 1.1, String(r!.usage.cost));
	assert.ok(existsSync(join(root, j.id, "steer.json")));
});

test("no max_cost: no steer file, no soft cap fields (old behavior)", async () => {
	const { m, root } = mgr();
	const j = m.spawn({ task: "normal" });
	const [r] = await m.wait([j.id], 10_000);
	m.stop();
	assert.equal(r!.state, "done");
	assert.equal(r!.capped, undefined);
	assert.equal(r!.soft_cap_at, undefined);
	assert.ok(!existsSync(join(root, j.id, "steer.json")));
});

test("costAdvice: same numbers as cost_advice.py; n gate; no pooling; line only below p75", () => {
	const row = (c: number, o: Record<string, unknown> = {}) => ({ agent: "worker", model: "opus", thinking: "high", state: "done", cost_usd: c, ...o });
	const rows: unknown[] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(c => row(c));
	rows.push(row(100, { thinking: "medium" }), row(100, { state: "killed(cost)" }), row(100, { capped: true }), null, "x");
	assert.deepEqual(costAdvice(rows, "worker", "opus", "high"), { n: 10, p75: 7.75, p90: 9.1 }); // python test: 7.75 / 9.10
	assert.deepEqual(costAdvice(rows.slice(0, 7), "worker", "opus", "high"), { n: 7 });
	const d = mkdtempSync(join(tmpdir(), "apexadv-"));
	const p = join(d, "agent_runs.jsonl");
	writeFileSync(p, rows.map(r => JSON.stringify(r)).join("\n") + "\nnot json\n");
	assert.match(costAdviceLine(5, "worker", "opus", "high", p), /no change made.*\$5\.00 is below the done-cost p75 \$7\.75 \(p90 \$9\.10, n=10\)/);
	assert.equal(costAdviceLine(8, "worker", "opus", "high", p), "");
	assert.equal(costAdviceLine(undefined, "worker", "opus", "high", p), "");
	assert.equal(costAdviceLine(1, "worker", "opus", "high", join(d, "missing")), "");
});
