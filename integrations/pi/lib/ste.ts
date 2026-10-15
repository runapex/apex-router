/**
 * ASD-STE100 rules for pi, read from the ONE source of truth that the Python side also uses:
 * src/apex_router/data/ste_rules.json. Fail-open: if the file is missing, the blocks are empty and
 * nothing is injected. Pure module (no pi imports) so node --test can load it.
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const RULES_PATH = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "src", "apex_router", "data", "ste_rules.json");

type Rules = { prompt_block: string[]; brief_block: string[] };
let cache: Rules | null | undefined;

export function loadRules(path: string = RULES_PATH): Rules | null {
	if (cache !== undefined && path === RULES_PATH) return cache;
	let r: Rules | null = null;
	try {
		if (existsSync(path)) r = JSON.parse(readFileSync(path, "utf8")) as Rules;
	} catch {
		r = null;
	}
	if (path === RULES_PATH) cache = r;
	return r;
}

export const steEnabled = (env: NodeJS.ProcessEnv = process.env): boolean => env.APEX_STE !== "0";

export function rulesText(path?: string): string {
	return loadRules(path)?.prompt_block.join("\n") ?? "";
}

export function briefText(path?: string): string {
	return loadRules(path)?.brief_block.join("\n") ?? "";
}

/** The system-prompt suffix for an STE session: the rules block once, never twice. */
export function withSte(systemPrompt: string, path?: string): string {
	const block = rulesText(path);
	if (!block || systemPrompt.includes(block.split("\n")[0]!)) return systemPrompt;
	return `${systemPrompt}\n\n${block}`;
}
