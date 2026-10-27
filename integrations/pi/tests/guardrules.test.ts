import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { applyGuardCmd, check, compileRules, parseGuardCmd, selfProtect } from "../lib/guardrules.ts";

const NOW = Date.parse("2026-10-27T00:00:00Z");
const rules = (raw: unknown) => compileRules(raw, "file", NOW).rules;

test("a matching bash call is blocked; the message names rule, source and reason", () => {
	const r = rules({ rules: [{ id: "no-66", match: "\\bdevtest66\\b", reason: "shared box", owner: "juri" }] });
	const v = check(r, "bash", { command: "ssh devtest66 uptime" });
	assert.equal(v.block, true);
	if (v.block) {
		assert.match(v.reason, /"no-66"/);
		assert.match(v.reason, /source: file/);
		assert.match(v.reason, /shared box/);
	}
});

test("no match, another tool, a disabled or expired rule: no block", () => {
	const r = rules({ rules: [
		{ id: "a", match: "devtest66", reason: "x" },
		{ id: "b", match: "push", reason: "x", enabled: false },
		{ id: "c", match: "rm -rf", reason: "x", expires: "2026-10-01" },
	] });
	assert.equal(check(r, "bash", { command: "ssh devtest22 uptime" }).block, false);
	assert.equal(check(r, "read", { path: "/tmp/devtest66.log" }).block, false);   // rule is bash-only
	assert.equal(check(r, "bash", { command: "git push" }).block, false);
	assert.equal(check(r, "bash", { command: "rm -rf /tmp/x" }).block, false);
});

test("tool=* tests the JSON of any tool input", () => {
	const r = rules([{ id: "p", tool: "*", match: "medium\\.com/p/[^/]+/responses", reason: "no posts" }]);
	assert.equal(check(r, "write", { path: "/tmp/a", content: "https://medium.com/p/abc/responses" }).block, true);
});

test("bad rules are skipped with an error that names only the id", () => {
	const { rules: r, errors } = compileRules({ rules: [
		{ id: "ok", match: "x", reason: "y" }, { id: "bad-re", match: "(", reason: "y" },
		{ id: "no-reason", match: "x" }, { match: "x", reason: "y" }, { id: "bad-date", match: "x", reason: "y", expires: "soon" },
	] }, "file", NOW);
	assert.deepEqual(r.map(x => x.id), ["ok"]);
	assert.equal(errors.length, 4);
	assert.ok(errors.every(e => !e.includes("(")));
});

test("no file content means no rules and no error", () => {
	assert.deepEqual(compileRules(undefined, "file", NOW), { rules: [], errors: [] });
});

test("/guard add, list, rm parse and apply", () => {
	const add = parseGuardCmd("add no-push tool=bash \\bgit\\s+push\\b -- review first");
	assert.equal(add.op, "add");
	let list = applyGuardCmd([], add);
	assert.equal(list.length, 1);
	assert.equal(check(compileRules(list, "session", NOW).rules, "bash", { command: "git push origin main" }).block, true);
	assert.deepEqual(parseGuardCmd("list"), { op: "list" });
	list = applyGuardCmd(list, parseGuardCmd("rm no-push"));
	assert.equal(list.length, 0);
	assert.equal(parseGuardCmd("add x ( -- y").op, "help");       // bad regex
	assert.equal(parseGuardCmd("add bad/id x -- y").op, "help");   // bad id
});

test("self-protect blocks writes to the rules file, not reads", () => {
	const sp = selfProtect("/Users/u/.apex-router/pi-guardrules.json");
	assert.equal(check(sp, "write", { path: "/Users/u/.apex-router/pi-guardrules.json", content: "{}" }).block, true);
	assert.equal(check(sp, "edit", { path: "/Users/u/.apex-router/pi-guardrules.json" }).block, true);
	assert.equal(check(sp, "bash", { command: "echo '{}' > ~/.apex-router/pi-guardrules.json" }).block, true);
	assert.equal(check(sp, "bash", { command: "sed -i '' 's/a/b/' ~/.apex-router/pi-guardrules.json" }).block, true);
	assert.equal(check(sp, "bash", { command: "cat ~/.apex-router/pi-guardrules.json" }).block, false);
});

test("the shipped example compiles without errors", () => {
	const raw = JSON.parse(readFileSync(new URL("../guardrules.example.json", import.meta.url), "utf8"));
	const { rules: r, errors } = compileRules(raw, "file", NOW);
	assert.deepEqual(errors, []);
	assert.deepEqual(r.map(x => x.id), ["no-devtest66", "no-public-post"]);   // no-git-push is disabled
	assert.equal(check(r, "bash", { command: "ssh devtest66" }).block, true);
	assert.equal(check(r, "bash", { command: "ssh devtest22" }).block, false);
});
