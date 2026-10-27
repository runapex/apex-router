// node --test integrations/pi/tests/ — apex-compact ledger (P1), gate (P2), budget (P4), cut (P6), replay (P3).
// Fixtures are fake sessions (tests/fixtures/fakesession.ts), not real transcripts.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
	auditMatch, buildReplayPayload, capLedger, emptyLedger, extractIds, extractNogo, extractRules, fileLists, gate, label,
	ledgerTokens, mergeFileOps, moveCutEarlier, PINNED_HEADER, PINNED_MARKER, readLedger, replyText, reserveForBudget,
	stripPinned, updateLedger, type Item, type Ledger,
} from "../lib/compact.ts";
import { FakeSession, fakeTokens, fakeToMessage } from "./fixtures/fakesession.ts";

const active = (l: Ledger, kind?: string) => l.items.filter(i => i.status === "active" && (!kind || i.kind === kind));

// ---- extraction -------------------------------------------------------------------------------------------
test("extractRules: short rule message is kept verbatim (the lost rule of the study)", () => {
	const t = "every move must be regressed, no negative impact on the other boxes";
	assert.deepEqual(extractRules(t), [t]);
	assert.deepEqual(extractRules("don't post anything, apply resolutions and resume"), ["don't post anything, apply resolutions and resume"]);
	assert.deepEqual(extractRules("only devtest22 and devtest66 we shouldn't touch"), ["only devtest22 and devtest66 we shouldn't touch"]);
});

test("extractRules: no rule in chat, a slash command, a bare 'stop' or a retire message", () => {
	assert.deepEqual(extractRules("looks good, continue"), []);
	assert.deepEqual(extractRules("/compact never mind"), []);
	assert.deepEqual(extractRules("stop"), []);
	assert.deepEqual(extractRules("forget that"), []);
});

test("extractRules: a long brief gives its rule sentences only, each verbatim", () => {
	const brief = `${"Context line about the build. ".repeat(30)}\nYou must never push to origin.\nThe report goes to the parent.\nDo not touch tests/conftest.py.`;
	const r = extractRules(brief);
	assert.deepEqual(r, ["You must never push to origin.", "Do not touch tests/conftest.py."]);
});

test("extractIds: sha, EYE-n, pi job id, #PR; not words, dates or plain numbers", () => {
	const t = "fix in 3076a01 for EYE-147593, job 20261026-101112-ab12, PR #4521; not deadbeef, not 20261026, not 1234567, not facade";
	assert.deepEqual(extractIds(t), ["3076a01", "EYE-147593", "20261026-101112-ab12", "#4521"]);
});

test("extractNogo: NO-GO lines, max 3 per message", () => {
	assert.deepEqual(extractNogo("verdict: NO-GO — race in lock\nok\nNO GO: missing test"), ["verdict: NO-GO — race in lock", "NO GO: missing test"]);
	assert.deepEqual(extractNogo("GO"), []);
});

test("updateLedger: rules with source id, ids, NO-GO, last failing call with error (600 chars max)", () => {
	const s = new FakeSession();
	const u1 = s.user("never ssh to devtest66, it is shared");
	s.assistant("Working on EYE-147593 at 3076a01.");
	s.tool("bash", { command: "make test" }, "ok");
	s.tool("bash", { command: "ssh devtest22 ls" }, "x".repeat(900), true);
	const [, r2] = s.tool("bash", { command: "npm run build" }, "error TS2345: bad type\n" + "y".repeat(900), true);
	s.assistant("xval verdict: NO-GO — the lock is not released");
	const l = updateLedger(null, s.entries);
	const rule = active(l, "rule")[0]!;
	assert.equal(rule.text, "never ssh to devtest66, it is shared");
	assert.equal(rule.source, u1);
	assert.equal(rule.scope, "session");
	assert.deepEqual(active(l, "id").map(i => i.text), ["EYE-147593", "3076a01"]);
	const errs = l.items.filter(i => i.kind === "error");
	assert.equal(errs.length, 1, "only the latest error");
	assert.equal(errs[0]!.source, r2);
	assert.equal(errs[0]!.tool, "bash");
	assert.match(errs[0]!.args!, /npm run build/);
	assert.ok(errs[0]!.text.startsWith("error TS2345: bad type"));
	assert.ok(errs[0]!.text.length <= 601);
	assert.equal(active(l, "nogo").length, 1);
	assert.equal(l.scannedTo, s.entries.at(-1)!.id);
});

// ---- retirement -------------------------------------------------------------------------------------------
test("retire: 'forget R<n>' by label, 'forget that' = newest rule", () => {
	const s = new FakeSession();
	s.user("never push to main without a review");
	s.user("always run the gate script before a merge");
	let l = updateLedger(null, s.entries);
	const [r1, r2] = l.items;
	s.user(`forget ${label(r1!)}`);
	l = updateLedger(l, s.entries);
	assert.equal(l.items.find(i => i.seq === r1!.seq)!.status, "retired");
	assert.equal(l.items.find(i => i.seq === r2!.seq)!.status, "active");
	s.user("forget that");
	l = updateLedger(l, s.entries);
	assert.equal(l.items.find(i => i.seq === r2!.seq)!.status, "retired");
	assert.equal(active(l, "rule").length, 0);
});

test("retire: 'ignore R<n>' by label; a rule with 'ignore' in it is a rule, not a retire", () => {
	const s = new FakeSession();
	s.user("never push to the release branch");
	s.user("never ignore lint errors in the build");
	let l = updateLedger(null, s.entries);
	assert.equal(active(l, "rule").length, 2, "'never ignore lint errors' is a rule");
	s.user(`ok, ignore ${label(l.items[0]!)} now`);
	l = updateLedger(l, s.entries);
	assert.deepEqual(active(l, "rule").map(i => i.text), ["never ignore lint errors in the build"]);
});

test("retire: 'R<n> done' marks done; a vague 'done, commit it' does not touch an 'always' rule", () => {
	const s = new FakeSession();
	s.user("always run the tests before each commit");
	let l = updateLedger(null, s.entries);
	s.user("done, the tests pass, commit it");
	l = updateLedger(l, s.entries);
	assert.equal(active(l, "rule").length, 1, "a vague done keeps the rule");
	s.user(`${label(l.items[0]!)} done`);
	l = updateLedger(l, s.entries);
	assert.equal(l.items[0]!.status, "done");
});

test("retire: 'replace R<n>: <text>' replaces a rule; the same rule again re-activates", () => {
	const s = new FakeSession();
	s.user("never post to the Jira ticket EYE-1234 without asking");
	let l = updateLedger(null, s.entries);
	s.user(`replace ${label(l.items.find(i => i.kind === "rule")!)}: never post to the Jira ticket, write a draft file`);
	l = updateLedger(l, s.entries);
	const rules = l.items.filter(i => i.kind === "rule");
	assert.equal(rules[0]!.status, "retired");
	assert.equal(rules[1]!.status, "active");
	s.user("never post to the Jira ticket EYE-1234 without asking");
	l = updateLedger(l, s.entries);
	assert.equal(l.items.find(i => i.seq === rules[0]!.seq)!.status, "active");
});

test("retire: a GO verdict closes open NO-GO findings; a long brief with 'ignore' retires nothing", () => {
	const s = new FakeSession();
	s.assistant("review verdict: NO-GO, test missing");
	s.user("never touch scripts/az-token-cached");
	let l = updateLedger(null, s.entries);
	s.user(`${"background text. ".repeat(40)} ignore the warnings from az.`);
	s.assistant("xval review verdict: GO");
	l = updateLedger(l, s.entries);
	assert.equal(active(l, "nogo").length, 0);
	assert.equal(l.items.find(i => i.kind === "nogo")!.status, "done");
	assert.equal(active(l, "rule").length, 1);
});

test("F1 extractRules: fenced code, '>' quote lines and quoted text are not user rules", () => {
	const log = "```\n2026-10-27 ERROR worker: you must never retry the job without a lock\n```\nwhat does this mean?";
	assert.deepEqual(extractRules(log), [], "a pasted log line in a fence");
	assert.deepEqual(extractRules("```\nnever trust the cache, always reload\n"), [], "a fence that is not closed");
	assert.deepEqual(extractRules("> you must always restart the agent after a change\nwhy did it say that?"), [], "a quote line");
	assert.deepEqual(extractRules('the log said "never retry the job without a lock" - why?'), [], "quoted text");
	const long = `${"Context line about the build. ".repeat(30)}\n\`\`\`\nERROR: you must never retry without a lock\n\`\`\`\n> always restart the agent first\nDo not touch tests/conftest.py.`;
	assert.deepEqual(extractRules(long), ["Do not touch tests/conftest.py."]);
	// a real rule next to a paste is kept verbatim (without the paste)
	assert.deepEqual(extractRules("> log: always retry\nnever push to main without a review"), ["never push to main without a review"]);
	assert.deepEqual(extractRules("don't post anything"), ["don't post anything"], "an apostrophe is not a quote");
});

test("F1 gate: each pinned rule is a quoted user line with its source entry id", () => {
	const s = new FakeSession();
	const u = s.user("never ssh to devtest66, it is shared");
	const l = updateLedger(null, s.entries);
	assert.match(gate("## Goal\nx", l).text, new RegExp(`- R1 \\[user, entry ${u}\\]: "never ssh to devtest66, it is shared"`));
});

test("F2 retire: word overlap does not retire an independent rule; only an explicit reference does", () => {
	const s = new FakeSession();
	s.user("Never delete the nightly database backups");
	s.user("Always encrypt the nightly database backups");
	let l = updateLedger(null, s.entries);
	assert.equal(active(l, "rule").length, 2, "a new rule with shared words does not replace the old one");
	s.user("ok, drop the nightly database backups encryption rule");
	l = updateLedger(l, s.entries);
	assert.equal(active(l, "rule").length, 2, "a retire verb with word overlap only retires nothing");
	s.user("drop R2");
	l = updateLedger(l, s.entries);
	assert.deepEqual(active(l, "rule").map(i => i.text), ["Never delete the nightly database backups"]);
	s.user("forget: Never delete the nightly database backups");
	l = updateLedger(l, s.entries);
	assert.equal(active(l, "rule").length, 0, "the exact rule text retires it");
});

// ---- cap --------------------------------------------------------------------------------------------------
test("cap: drop retired, then done, then oldest ids; active rules and the error stay", () => {
	const l = emptyLedger();
	const mk = (kind: Item["kind"], status: Item["status"], text: string): Item => {
		const it: Item = { key: `${kind}:${text}`, kind, seq: l.nextSeq++, text, source: "x", scope: "session", status };
		l.items.push(it);
		return it;
	};
	const retired = mk("rule", "retired", "r".repeat(400));
	const done = mk("rule", "done", "d".repeat(400));
	const ids = Array.from({ length: 30 }, (_, k) => mk("id", "active", `EYE-${1000 + k}`));
	const rule = mk("rule", "active", "never ".repeat(60));
	const err = mk("error", "active", "e".repeat(600));
	const before = ledgerTokens(l);
	const dropped = capLedger(l, before - 250);
	assert.ok(dropped >= 2);
	assert.ok(!l.items.includes(retired) && !l.items.includes(done), "retired and done go first");
	assert.ok(l.items.includes(rule) && l.items.includes(err));
	assert.ok(l.items.includes(ids.at(-1)!));
	capLedger(l, 200);
	assert.ok(l.items.includes(err), "the error is never dropped");
	assert.ok(ledgerTokens(l) <= 200 || l.items.length === 1);
});

test("cap: default 3000 tokens holds a big ledger", () => {
	const s = new FakeSession();
	for (let k = 0; k < 400; k++) s.assistant(`commit ${(0xabc0000 + k).toString(16)} for EYE-${k + 10}`);
	const l = updateLedger(null, s.entries);
	capLedger(l);
	assert.ok(ledgerTokens(l) <= 3000);
});

// ---- gate (P2) + render (P1) ------------------------------------------------------------------------------
test("gate: appends a missing id, pins rules / NO-GO / error verbatim, counts misses", () => {
	const s = new FakeSession();
	s.user("every move must be regressed, no negative impact");
	s.assistant("working on EYE-147593 at 3076a01");
	s.tool("bash", { command: "pytest" }, "FAILED test_x", true);
	s.assistant("verdict: NO-GO missing test");
	const l = updateLedger(null, s.entries);
	const summary = "## Goal\nFix EYE-147593.\n## Constraints & Preferences\n- (none)";
	const g = gate(summary, l);
	assert.ok(g.text.startsWith(summary));
	assert.ok(g.text.includes(PINNED_MARKER) && g.text.includes(PINNED_HEADER));
	assert.ok(g.text.includes("every move must be regressed, no negative impact"));
	assert.match(g.text, /### Ids \(not in the summary above\)\n3076a01/);
	assert.ok(!/### Ids[^#]*EYE-147593/.test(g.text), "an id that is in the summary is not repeated");
	assert.ok(g.text.includes("FAILED test_x") && g.text.includes("NO-GO missing test"));
	assert.deepEqual(g.missed, { rule: 1, id: 1, nogo: 1, error: 1 });
	assert.deepEqual(g.appended, { rule: 1, id: 1, nogo: 1, error: 1 });
});

test("gate: a paraphrased rule counts as present (audit heuristic); empty ledger leaves the summary", () => {
	assert.ok(auditMatch("never touch devtest22 or devtest66", "- never touch devtest22 (shared) or devtest66"));
	assert.equal(gate("## Goal\nx", emptyLedger()).text, "## Goal\nx");
});

test("stripPinned: the pinned block is not fed back into the next summary prompt", () => {
	const s = new FakeSession();
	s.user("never push to main");
	const g = gate("## Goal\nx\n\n<read-files>\na\n</read-files>", updateLedger(null, s.entries));
	assert.equal(stripPinned(g.text), "## Goal\nx\n\n<read-files>\na\n</read-files>");
	assert.equal(stripPinned("plain"), "plain");
	assert.equal(stripPinned(undefined), undefined);
});

// ---- carry-over across 2 compactions --------------------------------------------------------------------
test("carry-over: a rule from before compaction 1 survives compaction 2 via details.apexLedger", () => {
	const s = new FakeSession();
	const u1 = s.user("never ssh to devtest66");
	s.assistant("ok");
	const k1 = s.user("start the work on EYE-5");
	const l1 = updateLedger(null, s.entries);
	const g1 = gate("## Goal\nwork", l1);
	s.compaction(g1.text, k1, { apexLedger: l1 });
	// after compaction 1 the old messages are gone from the next preparation; only the ledger carries the rule
	s.assistant("done step 1");
	const k2 = s.user("continue with step 2");
	const prev = readLedger(s.entries.find(e => e.type === "compaction")!.details);
	assert.ok(prev);
	const l2 = updateLedger(prev, s.entries);
	const g2 = gate("## Goal\nstep 2 (the model forgot the rule)", l2);
	s.compaction(g2.text, k2, { apexLedger: l2 });
	const rule = active(l2, "rule").find(i => i.text === "never ssh to devtest66")!;
	assert.equal(rule.source, u1);
	assert.ok(g2.text.includes("never ssh to devtest66"));
	assert.equal(l2.items.filter(i => i.text === "never ssh to devtest66").length, 1, "no duplicate");
	assert.equal(l2.scannedTo, k2);
	assert.equal(stripPinned(g2.text), "## Goal\nstep 2 (the model forgot the rule)");
});

test("readLedger: malformed details give null", () => {
	assert.equal(readLedger(undefined), null);
	assert.equal(readLedger({ readFiles: [] }), null);
	assert.equal(readLedger({ apexLedger: { items: "x" } }), null);
});

// ---- P4 budget ----------------------------------------------------------------------------------------------
test("reserveForBudget: pi gives the summary 0.8 * reserveTokens", () => {
	assert.equal(Math.floor(0.8 * reserveForBudget(6000)), 6000);
});

// ---- P6 cut -------------------------------------------------------------------------------------------------
test("P6 moveCutEarlier: cut moves to an earlier user message, never to a tool result", () => {
	const s = new FakeSession();
	s.user("first task " + "a".repeat(4000));
	s.tool("bash", { command: "ls" }, "b".repeat(8000));
	const u2 = s.user("second task " + "c".repeat(4000));
	s.tool("bash", { command: "ls" }, "d".repeat(8000));
	const u3 = s.user("third task " + "e".repeat(400));
	s.assistant("f".repeat(400));
	const prep = s.prep(u3);
	const r = moveCutEarlier(s.entries, prep, 3000, fakeToMessage, fakeTokens);
	assert.ok(r);
	assert.equal(r!.firstKeptEntryId, u2);
	const kept = s.entries.find(e => e.id === r!.firstKeptEntryId)!;
	assert.equal(kept.message!.role, "user");
	assert.equal(r!.prep.messagesToSummarize.length, 3);
	assert.equal(r!.prep.isSplitTurn, false);
	// a huge keep stops at the earliest user message that still leaves something to summarize
	assert.equal(moveCutEarlier(s.entries, prep, 1e9, fakeToMessage, fakeTokens)!.firstKeptEntryId, u2);
	// the max keep blocks the move: no change
	const one = new FakeSession();
	one.user("only turn");
	const u = one.user("second");
	assert.equal(moveCutEarlier(one.entries, one.prep(u), 1e9, fakeToMessage, fakeTokens), null, "nothing earlier to keep");
	assert.equal(moveCutEarlier(s.entries, prep, 3000, fakeToMessage, fakeTokens, 500), null);
});

// ---- P3 replay payload ----------------------------------------------------------------------------------------
test("P3 buildReplayPayload: Anthropic — same prefix, instruction in the last user turn, thinking kept", () => {
	const payload = {
		model: "claude-x", system: [{ type: "text", text: "sys" }], tools: [{ name: "bash" }], max_tokens: 32000,
		thinking: { type: "enabled", budget_tokens: 10000 }, tool_choice: { type: "auto" },
		messages: [{ role: "user", content: [{ type: "text", text: "hi" }] }, { role: "assistant", content: [{ type: "text", text: "x" }] },
			{ role: "user", content: [{ type: "tool_result", tool_use_id: "t", content: "r", cache_control: { type: "ephemeral" } }] }],
	};
	const p = buildReplayPayload(payload, "SUMMARIZE", 6000)!;
	assert.deepEqual(p.system, payload.system);
	assert.deepEqual(p.tools, payload.tools);
	assert.deepEqual(p.thinking, payload.thinking);
	const msgs = p.messages as { role: string; content: unknown[] }[];
	assert.equal(msgs.length, 3);
	assert.deepEqual(msgs.slice(0, 2), payload.messages.slice(0, 2));
	assert.deepEqual(msgs[2]!.content[0], payload.messages[2]!.content[0], "the cache breakpoint block is unchanged");
	assert.deepEqual(msgs[2]!.content[1], { type: "text", text: "SUMMARIZE" });
	assert.equal(p.max_tokens, 32000);
	assert.equal(p.tool_choice, undefined);
	assert.equal(payload.messages[2]!.content.length, 1, "the captured payload is not changed");
});

test("P3 buildReplayPayload: OpenAI chat and responses shapes; unknown shape gives null", () => {
	const chat = buildReplayPayload({ model: "m", messages: [{ role: "system", content: "s" }, { role: "tool", content: "r" }], max_completion_tokens: 100 }, "S", 6000)!;
	assert.deepEqual((chat.messages as unknown[]).at(-1), { role: "user", content: "S" });
	assert.equal(chat.max_completion_tokens, 8048);
	const resp = buildReplayPayload({ model: "m", input: [{ role: "user", content: "x" }] }, "S", 6000)!;
	assert.equal((resp.input as unknown[]).length, 2);
	assert.equal(buildReplayPayload({ contents: [] }, "S", 6000), null);
	assert.equal(buildReplayPayload(null, "S", 6000), null);
});

test("replyText: error, length stop, tool call and empty text give null", () => {
	assert.equal(replyText({ content: [{ type: "text", text: "ok" }], stopReason: "stop" }), "ok");
	assert.equal(replyText({ content: [{ type: "text", text: "ok" }], stopReason: "length" }), null);
	assert.equal(replyText({ content: [], stopReason: "error" }), null);
	assert.equal(replyText({ content: [{ type: "toolCall" }], stopReason: "toolUse" }), null);
	assert.equal(replyText({ content: [{ type: "thinking" }], stopReason: "stop" }), null);
});

test("file lists: earlier lists merge in (pi skips them after a hook compaction)", () => {
	const ops = mergeFileOps({ read: new Set(["a"]), written: new Set(), edited: new Set(["b"]) }, { readFiles: ["c", "b"], modifiedFiles: ["d"] });
	const f = fileLists(ops);
	assert.deepEqual(f.readFiles, ["a", "c"]);
	assert.deepEqual(f.modifiedFiles, ["b", "d"]);
	assert.match(f.text, /<read-files>\na\nc\n<\/read-files>/);
});

test("xval pass 3 F1: rules are stored verbatim with their framing; quotes only decide rule-ness", () => {
	// a quoted instruction stays inside its framing ("The log says ..."), never as a bare rule
	assert.deepEqual(extractRules('Always run tests. The log says "Never run tests".'), ['Always run tests. The log says "Never run tests".']);
	// literals in quotes are part of the rule and must survive
	assert.deepEqual(extractRules('Always pass "--dry-run" to deployment commands.'), ['Always pass "--dry-run" to deployment commands.']);
	const s = new FakeSession();
	s.user('Never deploy to "production" on Friday');
	s.user('Never deploy to "staging" on Friday');
	assert.equal(active(updateLedger(null, s.entries), "rule").length, 2, "rules that differ only in a quoted word stay apart");
	// a long message: a sentence that is only a quoted log line is not a rule
	const long = `${"Context line about the build. ".repeat(30)}\nThe log says "never retry without a lock". Do not touch tests/conftest.py.`;
	assert.deepEqual(extractRules(long), ["Do not touch tests/conftest.py."]);
});

test("xval pass 3 F2: exact-text retire works for rules that contain a done-word", () => {
	const s = new FakeSession();
	s.user("Never delete completed backups");
	s.user("Never close resolved tickets");
	let l = updateLedger(null, s.entries);
	s.user("forget: Never delete completed backups");
	l = updateLedger(l, s.entries);
	assert.deepEqual(active(l, "rule").map(i => i.text), ["Never close resolved tickets"]);
	s.user("Never close resolved tickets - done");
	l = updateLedger(l, s.entries);
	assert.equal(active(l, "rule").length, 0);
});

test("xval pass 2 F2: the exact text of a longer rule does not retire a shorter rule it contains", () => {
	const s = new FakeSession();
	s.user("Never delete backups");
	s.user("Never delete backups from production");
	let l = updateLedger(null, s.entries);
	assert.equal(active(l, "rule").length, 2);
	s.user("forget: Never delete backups from production");
	l = updateLedger(l, s.entries);
	assert.deepEqual(active(l, "rule").map(i => i.text), ["Never delete backups"]);
	s.user('drop "Never delete backups"');
	l = updateLedger(l, s.entries);
	assert.equal(active(l, "rule").length, 0, "a quoted exact rule text retires it");
});

test("xval pass 2: a long replace retires the old rule and does not add the command as a rule", () => {
	const s = new FakeSession();
	s.user("Always deploy to staging first");
	let l = updateLedger(null, s.entries);
	const text = "Always deploy to staging first and " + "wait for the canary metrics and the on-call OK, ".repeat(14) + "then prod.";
	assert.ok(text.length > 640);
	s.user(`replace R1: ${text}`);
	l = updateLedger(l, s.entries);
	const rules = active(l, "rule");
	assert.equal(rules.length, 1);
	assert.ok(!/^replace R1/i.test(rules[0]!.text), "the replace command itself is not a rule");
	assert.ok(rules[0]!.text.startsWith("Always deploy to staging first and wait"));
});
