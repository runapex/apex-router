import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { briefText, RULES_PATH, rulesText, steEnabled, withSte } from "../lib/ste.ts";

test("ste: reads the shared rules file (one source of truth with the Python side)", () => {
	const j = JSON.parse(readFileSync(RULES_PATH, "utf8"));
	assert.equal(rulesText(), j.prompt_block.join("\n"));
	assert.equal(briefText(), j.brief_block.join("\n"));
	assert.match(rulesText(), /ASD-STE100/);
});

test("ste: withSte appends once, never twice", () => {
	const once = withSte("base");
	assert.ok(once.startsWith("base\n\n## Output format: ASD-STE100"));
	assert.equal(withSte(once), once);
});

test("ste: missing rules file fails open (no injection)", () => {
	assert.equal(rulesText("/nonexistent/ste.json"), "");
	assert.equal(withSte("base", "/nonexistent/ste.json"), "base");
});

test("ste: APEX_STE=0 disables, default on", () => {
	assert.equal(steEnabled({}), true);
	assert.equal(steEnabled({ APEX_STE: "0" }), false);
});
