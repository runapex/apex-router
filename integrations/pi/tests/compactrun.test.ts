// node --test integrations/pi/tests/ — apex-compact runner: P3 / P5 / P4 fallbacks, P6, telemetry has no text.
import assert from "node:assert/strict";
import { test } from "node:test";

import { PINNED_MARKER, readLedger, reserveForBudget, type PrepLike } from "../lib/compact.ts";
import { runCompaction, type Deps, type ModelRef } from "../lib/compactrun.ts";
import { FakeSession, fakeTokens, fakeToMessage } from "./fixtures/fakesession.ts";

const SESSION: ModelRef = { provider: "anthropic", id: "opus", contextWindow: 200_000 };
const CHEAP: ModelRef = { provider: "anthropic", id: "sonnet", contextWindow: 200_000 };
const RULE = "never ssh to devtest66, it is shared";

function session() {
	const s = new FakeSession();
	s.user(RULE);
	s.assistant("working on EYE-147593 at 3076a01");
	s.tool("bash", { command: "pytest -q" }, "FAILED tests/test_x.py::test_y - AssertionError", true);
	const keep = s.user("now step 2");
	const leaf = s.assistant("ok");
	return { s, keep, leaf };
}

type Call = { model: string; reserve: number; instr?: string; prev?: string };
function deps(over: Partial<Deps> = {}, env: Record<string, string> = {}) {
	const calls: Call[] = [];
	const replays: Record<string, unknown>[] = [];
	const d: Deps = {
		model: SESSION, env, now: () => 1_000_000,
		defaultSummary: async (m: ModelRef, prep: PrepLike, instr?: string) => {
			calls.push({ model: m.id, reserve: prep.settings.reserveTokens, instr, prev: prep.previousSummary });
			return { text: `## Goal\nsummary by ${m.id}\n\n<read-files>\nsrc/a.ts\n</read-files>`, usage: { input: 100, output: 10, cacheRead: 0, cost: { total: 0.01 } }, details: { readFiles: ["src/a.ts"], modifiedFiles: ["src/b.ts"] } };
		},
		findModel: spec => (spec === "anthropic/sonnet" ? CHEAP : undefined),
		...over,
	};
	return { d, calls, replays };
}

const ev = (s: FakeSession, keep: string, extra: Partial<{ previousSummary: string; customInstructions: string }> = {}) =>
	({ preparation: s.prep(keep, extra.previousSummary), branchEntries: s.entries, customInstructions: extra.customInstructions, reason: "threshold" });

test("default path (P4): session model, fixed budget, ledger pinned, details carry the ledger", async () => {
	const { s, keep } = session();
	const { d, calls } = deps();
	const out = await runCompaction(ev(s, keep), d);
	assert.equal(out.row.path, "budget");
	assert.equal(calls.length, 1);
	assert.equal(calls[0]!.model, "opus");
	assert.equal(calls[0]!.reserve, reserveForBudget(6000));
	assert.match(calls[0]!.instr!, /under 6000 tokens/);
	const c = out.compaction!;
	assert.equal(c.firstKeptEntryId, keep);
	assert.equal(c.tokensBefore, 150_000);
	assert.ok(c.summary.includes(PINNED_MARKER) && c.summary.includes(RULE));
	assert.ok(c.summary.includes("3076a01") && c.summary.includes("FAILED tests/test_x.py"));
	const l = readLedger(c.details)!;
	assert.ok(l.items.some(i => i.text === RULE));
	assert.deepEqual(c.details.readFiles, ["src/a.ts"]);
});

test("P4: APEX_COMPACT_SUMMARY_TOKENS sets the budget; custom /compact instructions are kept", async () => {
	const { s, keep } = session();
	const { d, calls } = deps({}, { APEX_COMPACT_SUMMARY_TOKENS: "2000" });
	await runCompaction(ev(s, keep, { customInstructions: "focus on the lock" }), d);
	assert.equal(calls[0]!.reserve, reserveForBudget(2000));
	assert.match(calls[0]!.instr!, /^focus on the lock\n\n.*under 2000 tokens/s);
});

test("P4: a failing budget call falls back once without the budget", async () => {
	const { s, keep } = session();
	let n = 0;
	const { d, calls } = deps();
	const inner = d.defaultSummary;
	d.defaultSummary = async (m, p, i, sig) => {
		if (n++ === 0) throw new Error("Summarization failed: generation hit the token cap");
		return inner(m, p, i, sig);
	};
	const out = await runCompaction(ev(s, keep), d);
	assert.equal(out.row.path, "no-budget");
	assert.deepEqual(out.row.fails, ["budget-error"]);
	assert.equal(calls[0]!.reserve, 49152, "the user's reserveTokens");
	assert.ok(out.compaction!.summary.includes(RULE));
});

test("all summary calls fail: no compaction (pi default), one telemetry row", async () => {
	const { s, keep } = session();
	const { d } = deps({ defaultSummary: async () => { throw new Error("boom"); } });
	const out = await runCompaction(ev(s, keep), d);
	assert.equal(out.compaction, undefined);
	assert.equal(out.row.path, "pi-default");
	assert.deepEqual(out.row.fails, ["budget-error", "no-budget-error"]);
});

test("the earlier pinned block is not sent back to the model", async () => {
	const { s, keep } = session();
	const { d, calls } = deps();
	await runCompaction(ev(s, keep, { previousSummary: `## Goal\nold\n\n${PINNED_MARKER}\n## Pinned (verbatim)\n- R1 x` }), d);
	assert.equal(calls[0]!.prev, "## Goal\nold");
});

// ---- P5 -----------------------------------------------------------------------------------------------------
test("P5: APEX_COMPACT_MODEL is used for the summary text", async () => {
	const { s, keep } = session();
	const { d, calls } = deps({}, { APEX_COMPACT_MODEL: "anthropic/sonnet" });
	const out = await runCompaction(ev(s, keep), d);
	assert.equal(out.row.path, "p5-model");
	assert.equal(out.row.p5_model, "anthropic/sonnet");
	assert.deepEqual(calls.map(c => c.model), ["sonnet"]);
});

test("P5 fallback: an error or an unknown model falls back to the session model", async () => {
	const { s, keep } = session();
	const a = deps({}, { APEX_COMPACT_MODEL: "anthropic/sonnet" });
	const inner = a.d.defaultSummary;
	a.d.defaultSummary = async (m, p, i, sig) => {
		if (m.id === "sonnet") throw new Error("401 no key");
		return inner(m, p, i, sig);
	};
	const out = await runCompaction(ev(s, keep), a.d);
	assert.equal(out.row.path, "budget");
	assert.deepEqual(out.row.fails, ["p5-error"]);
	assert.deepEqual(a.calls.map(c => c.model), ["opus"]);
	const b = deps({}, { APEX_COMPACT_MODEL: "nope/none" });
	const out2 = await runCompaction(ev(s, keep), b.d);
	assert.equal(out2.row.path, "budget");
	assert.deepEqual(out2.row.fails, ["p5-no-model"]);
});

// ---- P3 -----------------------------------------------------------------------------------------------------
const PAYLOAD = { model: "opus", system: [{ type: "text", text: "sys" }], max_tokens: 32000, messages: [{ role: "user", content: [{ type: "text", text: "hi" }] }] };

test("P3: replays the captured payload, logs cacheRead and input", async () => {
	const { s, keep, leaf } = session();
	const sent: Record<string, unknown>[] = [];
	const { d, calls } = deps({
		captured: () => ({ payload: PAYLOAD, provider: "anthropic", id: "opus", at: 1_000_000 - 10_000, leafId: leaf }),
		replay: async (_m, p) => {
			sent.push(p);
			return { content: [{ type: "text", text: "## Goal\nfrom cache" }], stopReason: "stop", usage: { input: 2000, cacheRead: 150_000, cacheWrite: 0, output: 900 } };
		},
	}, { APEX_COMPACT_CACHE: "1" });
	const out = await runCompaction(ev(s, keep), d);
	assert.equal(out.row.path, "p3-replay");
	assert.equal(out.row.p3, "ok");
	assert.equal(out.row.p3_cache_read, 150_000);
	assert.equal(out.row.p3_input, 2000);
	assert.equal(calls.length, 0, "no default summary call");
	const msgs = sent[0]!.messages as { content: { text: string }[] }[];
	assert.match(msgs.at(-1)!.content.at(-1)!.text, /structured summary/);
	assert.ok(out.compaction!.summary.startsWith("## Goal\nfrom cache"));
	assert.ok(out.compaction!.summary.includes("<modified-files>\nsrc/b.ts"), "file lists are added on the P3 path");
	assert.ok(out.compaction!.summary.includes(RULE));
});

test("P3 fallback: error, bad reply, stale capture, model change, no capture -> default path", async () => {
	const { s, keep, leaf } = session();
	const cases: [string, Partial<Deps>][] = [
		["p3-error", { captured: () => ({ payload: PAYLOAD, provider: "anthropic", id: "opus", at: 1_000_000, leafId: leaf }), replay: async () => { throw new Error("400 bad request"); } }],
		["p3-bad-reply", { captured: () => ({ payload: PAYLOAD, provider: "anthropic", id: "opus", at: 1_000_000, leafId: leaf }), replay: async () => ({ content: [{ type: "text", text: "partial" }], stopReason: "length" }) }],
		["p3-stale", { captured: () => ({ payload: PAYLOAD, provider: "anthropic", id: "opus", at: 1_000_000 - 301_000, leafId: leaf }), replay: async () => { throw new Error("not called"); } }],
		["p3-model-changed", { captured: () => ({ payload: PAYLOAD, provider: "anthropic", id: "sonnet", at: 1_000_000, leafId: leaf }), replay: async () => { throw new Error("not called"); } }],
		["p3-no-capture", { captured: () => undefined, replay: async () => { throw new Error("not called"); } }],
		["p3-shape", { captured: () => ({ payload: { contents: [] }, provider: "anthropic", id: "opus", at: 1_000_000, leafId: leaf }), replay: async () => { throw new Error("not called"); } }],
	];
	for (const [code, over] of cases) {
		const { d, calls } = deps(over, { APEX_COMPACT_CACHE: "1" });
		const out = await runCompaction(ev(s, keep), d);
		assert.equal(out.row.path, "budget", code);
		assert.deepEqual(out.row.fails, [code], code);
		assert.equal(out.row.p3, "fallback", code);
		assert.equal(calls.length, 1, code);
		assert.ok(out.compaction!.summary.includes(RULE), code);
	}
});

test("F3 P3: a capture of another branch leaf (or without a leaf id) is not replayed", async () => {
	const { s, keep, leaf } = session();
	const replay = async () => ({ content: [{ type: "text", text: "## Goal\nother branch" }], stopReason: "stop" });
	for (const leafId of ["a999", undefined, null]) {
		const { d, calls } = deps({ captured: () => ({ payload: PAYLOAD, provider: "anthropic", id: "opus", at: 1_000_000, leafId }), replay }, { APEX_COMPACT_CACHE: "1" });
		const out = await runCompaction(ev(s, keep), d);
		assert.equal(out.row.path, "budget", String(leafId));
		assert.deepEqual(out.row.fails, ["p3-branch-changed"], String(leafId));
		assert.equal(calls.length, 1);
		assert.ok(!out.compaction!.summary.includes("other branch"));
	}
	const ok = deps({ captured: () => ({ payload: PAYLOAD, provider: "anthropic", id: "opus", at: 1_000_000, leafId: leaf }), replay }, { APEX_COMPACT_CACHE: "1" });
	assert.equal((await runCompaction(ev(s, keep), ok.d)).row.path, "p3-replay", "the same leaf replays");
});

test("F8 P3: /compact custom instructions are appended to the replay instruction", async () => {
	const { s, keep, leaf } = session();
	const sent: Record<string, unknown>[] = [];
	const { d } = deps({
		captured: () => ({ payload: PAYLOAD, provider: "anthropic", id: "opus", at: 1_000_000, leafId: leaf }),
		replay: async (_m, p) => {
			sent.push(p);
			return { content: [{ type: "text", text: "## Goal\nx" }], stopReason: "stop" };
		},
	}, { APEX_COMPACT_CACHE: "1" });
	const out = await runCompaction(ev(s, keep, { customInstructions: "focus on the lock" }), d);
	assert.equal(out.row.path, "p3-replay");
	const last = (sent[0]!.messages as { content: { text: string }[] }[]).at(-1)!.content.at(-1)!.text;
	assert.match(last, /structured summary[\s\S]*\n\nAdditional focus: focus on the lock$/);
});

test("P3 then P5 both fail: session model with budget", async () => {
	const { s, keep } = session();
	const { d, calls } = deps({ captured: () => undefined, replay: async () => { throw new Error("not called"); } }, { APEX_COMPACT_CACHE: "1", APEX_COMPACT_MODEL: "x/y" });
	const out = await runCompaction(ev(s, keep), d);
	assert.deepEqual(out.row.fails, ["p3-no-capture", "p5-no-model"]);
	assert.equal(out.row.path, "budget");
	assert.equal(calls[0]!.model, "opus");
});

// ---- P6 -----------------------------------------------------------------------------------------------------
test("P6: off by default; APEX_COMPACT_KEEP_TOKENS moves firstKeptEntryId to an earlier user message", async () => {
	const s = new FakeSession();
	s.user("task one " + "a".repeat(4000));
	s.tool("bash", { command: "ls" }, "b".repeat(8000));
	const u2 = s.user("task two " + "c".repeat(4000));
	s.tool("bash", { command: "ls" }, "d".repeat(8000));
	const u3 = s.user("task three");
	s.assistant("ok");
	const base = deps({ toMessage: fakeToMessage, tokens: fakeTokens });
	const off = await runCompaction(ev(s, u3), base.d);
	assert.equal(off.compaction!.firstKeptEntryId, u3);
	assert.equal(off.row.p6, undefined);
	const on = deps({ toMessage: fakeToMessage, tokens: fakeTokens }, { APEX_COMPACT_KEEP_TOKENS: "40000" });
	const out = await runCompaction(ev(s, u3), on.d);
	assert.equal(out.compaction!.firstKeptEntryId, u2);
	assert.equal(out.row.p6, "moved");
	assert.equal(out.row.p6_first_kept_moved, true);
});

// ---- off switch + telemetry ---------------------------------------------------------------------------------
test("APEX_COMPACT=0: nothing happens", async () => {
	const { s, keep } = session();
	const { d, calls } = deps({}, { APEX_COMPACT: "0" });
	const out = await runCompaction(ev(s, keep), d);
	assert.equal(out.compaction, undefined);
	assert.equal(out.row.path, "off");
	assert.equal(calls.length, 0);
});

test("telemetry row: counts and codes only — no rule text, no id, no command, no error text", async () => {
	const { s, keep } = session();
	const { d } = deps({}, { APEX_COMPACT_MODEL: "anthropic/sonnet", APEX_COMPACT_CACHE: "1" });
	const out = await runCompaction(ev(s, keep), d);
	const j = JSON.stringify(out.row);
	for (const secret of ["devtest66", "3076a01", "EYE-147593", "pytest", "AssertionError", "summary by"]) assert.ok(!j.includes(secret), secret);
	assert.deepEqual(out.row.items, { rule: 1, id: 2, nogo: 0, error: 1 });
	assert.equal(typeof (out.row.missed as { id: number }).id, "number");
});
