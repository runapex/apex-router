import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { applyGuardCmd, bashWritesRulesFile, check, compileRules, liveRules, MAX_TEXT, nestedQuantifier, parseGuardCmd, selfProtect } from "../lib/guardrules.ts";

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

// ---- xval fixes F4-F7 ------------------------------------------------------------------------------------
test("F4 ReDoS: a nested quantifier like ^(a+)+$ is rejected; the tested text is capped", () => {
	const { rules: r, errors } = compileRules({ rules: [
		{ id: "redos", match: "^(a+)+$", reason: "x" },
		{ id: "redos2", match: "(\\w*)*x", reason: "x" },
		{ id: "redos3", match: "(?:a|b+){2,}", reason: "x" },
		{ id: "ok", match: "\\bgit\\s+push\\b(?:\\s+origin)?\\s+\\S+", reason: "y" },
	] }, "file", NOW);
	assert.deepEqual(r.map(x => x.id), ["ok"]);
	assert.equal(errors.length, 3);
	assert.ok(errors.every(e => /nested quantifier/.test(e)));
	assert.equal(nestedQuantifier("[(+)]+(a)+"), false, "a class and a plain group are fine");
	assert.equal(nestedQuantifier("\\(a+\\)+"), false, "escaped parentheses are not a group");
	assert.equal(parseGuardCmd("add x ^(a+)+$ -- y").op, "help", "/guard add rejects it too");
	// the bound: 'a'*30 + '!' against ^(a+)+$ takes minutes; with the rule rejected and the cap, check is fast
	const t0 = Date.now();
	assert.equal(check(r, "bash", { command: "a".repeat(30) + "!" }).block, false);
	assert.ok(Date.now() - t0 < 1000);
	const tail = compileRules([{ id: "tail", match: "TAIL$", reason: "x" }], "file", NOW).rules;
	// xval pass 2: a head-only cap let "<8 k harmless>; rm -rf ~/.apex-router" pass. User regexes now see the head
	// and the tail (bounded: 2 x MAX_TEXT); only a match in the middle of a very long call is not seen.
	assert.equal(check(tail, "bash", { command: "x".repeat(3 * MAX_TEXT) + "TAIL" }).block, true, "the tail is tested");
	const mid = compileRules([{ id: "mid", match: "MID", reason: "x" }], "file", NOW).rules;
	assert.equal(check(mid, "bash", { command: "x".repeat(MAX_TEXT + 10) + "MID" + "x".repeat(MAX_TEXT + 10) }).block, false, "the middle of a long call is not tested (documented)");
});

test("xval pass 2: built-in self-protect sees the full text; a long prefix does not hide rm of the rules dir", () => {
	const sp = selfProtect("/Users/u/.apex-router/pi-guardrules.json", "/Users/u");
	const cmd = "echo " + "x".repeat(3 * MAX_TEXT) + " ; rm -rf /Users/u/.apex-router ; echo " + "y".repeat(3 * MAX_TEXT);
	assert.equal(check(sp, "bash", { command: cmd }).block, true);
});

test("xval pass 2: a directory that only has the same name is not protected", () => {
	const sp = selfProtect("/Users/u/.apex-router/pi-guardrules.json", "/Users/u");
	assert.equal(check(sp, "bash", { command: "rm -rf /tmp/test/.apex-router" }).block, false);
	assert.equal(check(sp, "bash", { command: "chmod -R u+rw /tmp/test/.apex-router" }).block, false);
	assert.equal(check(sp, "bash", { command: "rm -rf ~/.apex-router" }).block, true);
	assert.equal(check(sp, "bash", { command: "rm -rf /Users/u" }).block, true);
});

test("F5 expires: a compiled (cached) rule is off after its end, checked at each call", () => {
	const r = compileRules([{ id: "until", match: "devtest66", reason: "x", expires: "2026-10-27T01:00:00Z" }], "file", NOW).rules;
	assert.equal(r.length, 1);
	assert.equal(check(r, "bash", { command: "ssh devtest66" }, NOW).block, true);
	const later = Date.parse("2026-10-27T02:00:00Z");
	assert.equal(check(r, "bash", { command: "ssh devtest66" }, later).block, false, "the cached rule expired");
	assert.equal(liveRules(r, later).length, 0);
});

test("F6 self-protect: cp of the rules file is a read; cp/mv to it, rm/mv of its directory are writes", () => {
	const F = "/Users/u/.apex-router/pi-guardrules.json";
	const H = "/Users/u";
	const w = (c: string) => bashWritesRulesFile(c, F, H);
	// reads stay allowed
	for (const c of [
		"cp ~/.apex-router/pi-guardrules.json /tmp/backup.json", "cp pi-guardrules.json backup", "cat ~/.apex-router/pi-guardrules.json",
		"ls ~/.apex-router", "rm -rf /tmp/x", "rm -rf build", "rm -rf ~/.apex-router/pi-jobs-wt/abc/node_modules",
		"diff ~/.apex-router/pi-guardrules.json x.json", "cp a.json ~/.apex-router/other.json",
	]) assert.equal(w(c), false, c);
	// writes are blocked
	for (const c of [
		"cp /tmp/x.json ~/.apex-router/pi-guardrules.json", "cp -f x ~/.apex-router/pi-guardrules.json", "cp -t ~/.apex-router x/pi-guardrules.json",
		"cp /tmp/pi-guardrules.json ~/.apex-router/", "mv ~/.apex-router/pi-guardrules.json /tmp/", "mv x pi-guardrules.json",
		"rm -rf ~/.apex-router", "rm -rf ~/.apex-router/", "rm -r /Users/u/.apex-router/*", "rm -rf $HOME/.apex-router",
		"rm -rf ~", "rm -rf /", "sudo rm -r .apex-router", "rmdir ~/.apex-router", "mv ~/.apex-router ~/.old",
		"ls && rm ~/.apex-router/pi-guardrules.json", "echo '{}' > ~/.apex-router/pi-guardrules.json", "echo x | tee -a pi-guardrules.json",
		"sed -i '' 's/a/b/' ~/.apex-router/pi-guardrules.json", "chmod 000 ~/.apex-router", "dd if=/dev/null of=/Users/u/.apex-router/pi-guardrules.json",
	]) assert.equal(w(c), true, c);
	const sp = selfProtect(F, H);
	assert.equal(check(sp, "bash", { command: "cp ~/.apex-router/pi-guardrules.json /tmp/b.json" }).block, false);
	assert.equal(check(sp, "bash", { command: "rm -rf ~/.apex-router" }).block, true);
});

test("F7 source: set by the code; a 'source' field in the file is ignored and not logged", () => {
	const r = compileRules({ rules: [{ id: "a", match: "x", reason: "y", source: "built-in\nFAKE LOG LINE" }] }, "file", NOW).rules;
	assert.equal(r[0]!.source, "file");
	const v = check(r, "bash", { command: "x" });
	assert.ok(v.block && v.rule.source === "file" && !v.reason.includes("FAKE"));
	const s = compileRules([{ id: "b", match: "x", reason: "y", source: "file" }], "session", NOW).rules;
	assert.equal(s[0]!.source, "session");
});
