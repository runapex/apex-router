/**
 * Guard rules core (docs/research/2026-10-26-pi-compaction-precision-cost.md, option P0). Pure: decides if ONE
 * tool call matches an active deny rule. A rule in text (prompt, summary) can be lost or ignored; a rule here
 * blocks the call. The extension (apex-compact.ts) loads the rules and returns { block, reason }.
 *
 * Sources:
 *   file    — ~/.apex-router/pi-guardrules.json (APEX_GUARDRULES_FILE). No file = no rules. Example:
 *             integrations/pi/guardrules.example.json. A bad file gives no rules and one warning (fail open:
 *             a broken file must not stop all work).
 *   session — /guard add <id> [tool=<name>] <regex> -- <reason>. Kept in the session (custom entry), so a
 *             reload or resume keeps it; /guard rm <id> ends it.
 * Each rule has an owner (file or session), an optional `expires` (ISO date) and an `enabled` flag, so a rule
 * has an end. The block message names the rule, its reason and its source.
 * Self-protect: while any rule is active, write / edit of the rules file and a bash command that names the
 * file with a write verb (sed -i, perl -i, >, mv, rm, cp, tee, truncate, chmod, ln) are blocked too. A script
 * (python, node) that writes the file is not seen: this is a guard against mistakes, not a sandbox.
 */

export type GuardRule = {
	id: string;
	/** tool name, or "*" for all tools (default "bash") */
	tool?: string;
	/** JavaScript regex source, tested against the command (bash) or JSON of the input (other tools) */
	match: string;
	flags?: string;
	reason: string;
	owner?: string;
	/** ISO date or date-time; after it the rule is off */
	expires?: string;
	enabled?: boolean;
	source?: string;
};
export type Compiled = GuardRule & { re: RegExp; source: string };
export type Verdict = { block: false } | { block: true; rule: Compiled; reason: string };

const ID = /^[A-Za-z0-9_.:-]{1,64}$/;

/** Compile and check rules. Bad rules are skipped with an error text (no rule text in it, the id only). */
export function compileRules(raw: unknown, source: string, now: number = Date.now()): { rules: Compiled[]; errors: string[] } {
	const list = Array.isArray(raw) ? raw : Array.isArray((raw as { rules?: unknown })?.rules) ? (raw as { rules: unknown[] }).rules : null;
	if (!list) return { rules: [], errors: raw === undefined || raw === null ? [] : [`${source}: no "rules" array`] };
	const rules: Compiled[] = [], errors: string[] = [];
	for (const [k, r] of list.entries()) {
		const g = r as GuardRule;
		if (!g || typeof g !== "object" || typeof g.id !== "string" || !ID.test(g.id)) {
			errors.push(`${source}: rule #${k} has no valid id`);
			continue;
		}
		if (typeof g.match !== "string" || !g.match || typeof g.reason !== "string" || !g.reason.trim()) {
			errors.push(`${source}: rule ${g.id} needs "match" and "reason"`);
			continue;
		}
		if (g.enabled === false) continue;
		if (g.expires) {
			const t = Date.parse(g.expires);
			if (!Number.isFinite(t)) {
				errors.push(`${source}: rule ${g.id} has a bad "expires"`);
				continue;
			}
			if (t < now) continue;
		}
		try {
			const flags = (g.flags ?? "").replace(/[^imsu]/g, "");
			rules.push({ ...g, tool: g.tool ?? "bash", re: new RegExp(g.match, flags), source: g.source ?? source });
		} catch {
			errors.push(`${source}: rule ${g.id} has a bad regex`);
		}
	}
	return { rules, errors };
}

/** The text a rule is tested against. */
export function callText(toolName: string, input: unknown): string {
	const i = (input ?? {}) as Record<string, unknown>;
	if ((toolName === "bash" || toolName === "powershell") && typeof i.command === "string") return i.command;
	try {
		return JSON.stringify(i);
	} catch {
		return "";
	}
}

export function check(rules: Compiled[], toolName: string, input: unknown): Verdict {
	const text = callText(toolName, input);
	for (const r of rules) {
		if (r.tool !== "*" && r.tool !== toolName) continue;
		r.re.lastIndex = 0;
		if (r.re.test(text)) return { block: true, rule: r, reason: blockMessage(r) };
	}
	return { block: false };
}

export function blockMessage(r: Compiled): string {
	const owner = r.owner ? `, owner ${r.owner}` : "";
	const end = r.expires ? `, ends ${r.expires}` : "";
	const how = r.source === "session" ? `/guard rm ${r.id}` : `edit the rules file (only the user may do this)`;
	return `apex guard: blocked by rule "${r.id}" (source: ${r.source}${owner}${end}). Reason: ${r.reason}. ` +
		`Do not try the same action in another form. Tell the user. To end the rule: ${how}.`;
}

/** Self-protect rules for the rules file. Only while at least one rule is active. */
export function selfProtect(file: string): Compiled[] {
	const base = file.split("/").pop() ?? file;
	const esc = base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
	const reason = "the guard rules file is changed only by the user";
	return [
		{ id: "guard-file-write", tool: "write", match: esc, reason, source: "built-in", re: new RegExp(`"path":"[^"]*${esc}"`) },
		{ id: "guard-file-edit", tool: "edit", match: esc, reason, source: "built-in", re: new RegExp(`"path":"[^"]*${esc}"`) },
		{
			id: "guard-file-bash", tool: "bash", match: esc, reason, source: "built-in",
			re: new RegExp(`(?:\\b(?:sed\\s+(?:-\\S*\\s+)*-i|perl\\s+-\\S*i|rm|mv|cp|tee|truncate|chmod|ln)\\b[^\\n;|&]*${esc}|>{1,2}\\s*\\S*${esc})`),
		},
	];
}

// ---- /guard command ----------------------------------------------------------------------------------------
export type GuardCmd =
	| { op: "list" }
	| { op: "rm"; id: string }
	| { op: "add"; rule: GuardRule }
	| { op: "help"; error?: string };

export const GUARD_HELP = "Usage: /guard list | /guard add <id> [tool=<name>|tool=*] <regex> -- <reason> | /guard rm <id>";

/** Parse the /guard arguments. The regex is the text between the id (and tool=) and " -- ". */
export function parseGuardCmd(args: string): GuardCmd {
	const a = args.trim();
	if (!a || a === "list" || a === "ls") return { op: "list" };
	const rm = /^(?:rm|del|remove)\s+(\S+)\s*$/.exec(a);
	if (rm) return { op: "rm", id: rm[1]! };
	const add = /^add\s+(\S+)\s+(?:tool=(\S+)\s+)?(.+?)\s+--\s+(.+)$/s.exec(a);
	if (add) {
		const [, id, tool, match, reason] = add;
		if (!ID.test(id!)) return { op: "help", error: "the id may use letters, digits and _ . : - (64 max)" };
		try {
			new RegExp(match!);
		} catch {
			return { op: "help", error: "the regex is not valid" };
		}
		return { op: "add", rule: { id: id!, tool: tool ?? "bash", match: match!, reason: reason!.trim(), owner: "user (session)", source: "session" } };
	}
	return { op: "help", error: a.startsWith("add") ? 'add needs: <id> <regex> -- <reason>' : undefined };
}

/** Session rules after one /guard op. Pure: returns a new list. */
export function applyGuardCmd(list: GuardRule[], cmd: GuardCmd): GuardRule[] {
	if (cmd.op === "add") return [...list.filter(r => r.id !== cmd.rule.id), cmd.rule];
	if (cmd.op === "rm") return list.filter(r => r.id !== cmd.id);
	return list;
}

export function formatRules(rules: Compiled[]): string {
	if (!rules.length) return "apex guard: no active rules.";
	return rules.map(r => `- ${r.id} [${r.source}] tool=${r.tool} /${r.match}/ — ${r.reason}${r.expires ? ` (ends ${r.expires})` : ""}`).join("\n");
}
