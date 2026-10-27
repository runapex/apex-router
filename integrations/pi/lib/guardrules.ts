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
 * Self-protect: while any rule is active, write / edit of the rules file is blocked, and a bash command that
 * changes it: > / >> to it, sed -i, perl -i, tee, truncate, rm, mv (any operand), cp / ln / install / rsync
 * with the file as the last operand (the target), chmod / chown, dd of=. rm, rmdir, mv or chmod of the parent
 * directory (or a directory above it) is blocked too. Reads stay allowed ("cp rules.json backup", cat, grep).
 * A script (python, node) or find -delete is not seen: this is a guard against mistakes, not a sandbox.
 * ReDoS: a rule regex with a nested quantifier ("(a+)+") is rejected, and a rule tests at most the first
 * MAX_TEXT chars of the call.
 * Source: set by the code only ("file", "session", "built-in"). A "source" field in the file is ignored.
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
	/** ignored on input: the code sets the source */
	source?: string;
};
export type RuleSource = "file" | "session" | "built-in";
export type Compiled = Omit<GuardRule, "source"> & {
	re: RegExp;
	source: RuleSource;
	/** epoch ms of `expires`; check() skips the rule after it */
	expiresAt?: number;
	/** a matcher in code (built-in rules); used instead of `re` */
	fn?: (text: string) => boolean;
};
export type Verdict = { block: false } | { block: true; rule: Compiled; reason: string };

const ID = /^[A-Za-z0-9_.:-]{1,64}$/;
/** A rule tests at most this many chars of the call text (ReDoS bound). */
export const MAX_TEXT = 8192;

/** True when a group that holds + * or { is followed by + * or { ("(a+)+", "(\\w*)*", "(?:a|b+){2,}"). Such a
 *  regex can take exponential time (ReDoS) and blocks the event loop. */
export function nestedQuantifier(src: string): boolean {
	const stack: boolean[] = [];
	let inner = false;
	for (let k = 0; k < src.length; k++) {
		const c = src[k];
		if (c === "\\") {
			k++;
			continue;
		}
		if (c === "[") {
			k++;
			if (src[k] === "^") k++;
			if (src[k] === "]") k++;
			while (k < src.length && src[k] !== "]") {
				if (src[k] === "\\") k++;
				k++;
			}
			continue;
		}
		if (c === "(") {
			stack.push(inner);
			inner = false;
			continue;
		}
		if (c === ")") {
			const had: boolean = inner;
			inner = (stack.pop() ?? false) || had;
			const nx = src[k + 1];
			if (had && (nx === "+" || nx === "*" || nx === "{")) return true;
			continue;
		}
		if (c === "+" || c === "*" || c === "{") inner = true;
	}
	return false;
}

/** Compile and check rules. Bad rules are skipped with an error text (no rule text in it, the id only). The
 *  source comes from the caller (code), never from the rule. An expired rule is skipped; a rule that expires
 *  later keeps `expiresAt` and check() skips it after that time. */
export function compileRules(raw: unknown, source: "file" | "session", now: number = Date.now()): { rules: Compiled[]; errors: string[] } {
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
		let expiresAt: number | undefined;
		if (g.expires) {
			const t = Date.parse(g.expires);
			if (!Number.isFinite(t)) {
				errors.push(`${source}: rule ${g.id} has a bad "expires"`);
				continue;
			}
			if (t < now) continue;
			expiresAt = t;
		}
		if (nestedQuantifier(g.match)) {
			errors.push(`${source}: rule ${g.id} has a nested quantifier (slow regex)`);
			continue;
		}
		try {
			const flags = (typeof g.flags === "string" ? g.flags : "").replace(/[^imsu]/g, "");
			rules.push({
				id: g.id, tool: typeof g.tool === "string" ? g.tool : "bash", match: g.match, flags, reason: g.reason,
				owner: typeof g.owner === "string" ? g.owner : undefined, expires: g.expires, expiresAt, enabled: g.enabled,
				re: new RegExp(g.match, flags), source,
			});
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

/** Rules that have not expired at `now`. */
export const liveRules = (rules: Compiled[], now: number = Date.now()): Compiled[] =>
	rules.filter(r => r.expiresAt === undefined || r.expiresAt >= now);

export function check(rules: Compiled[], toolName: string, input: unknown, now: number = Date.now()): Verdict {
	const full = callText(toolName, input);
	// user regexes see the head and the tail (bounded work); built-in checks (fn) see the full text
	const text = full.length <= 2 * MAX_TEXT ? full : `${full.slice(0, MAX_TEXT)}\n${full.slice(-MAX_TEXT)}`;
	for (const r of rules) {
		if (r.tool !== "*" && r.tool !== toolName) continue;
		if (r.expiresAt !== undefined && r.expiresAt < now) continue;
		let hit: boolean;
		// built-in rules are ours and linear: they see the full text; user regexes see head + tail (xval pass 3)
		const subject = r.source === "built-in" ? full : text;
		if (r.fn) hit = r.fn(full);
		else {
			r.re.lastIndex = 0;
			hit = r.re.test(subject);
		}
		if (hit) return { block: true, rule: r, reason: blockMessage(r) };
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

const lastPart = (p: string): string => p.replace(/\/+$/, "").split("/").pop() ?? p;
const parentOf = (p: string): string => {
	const k = p.replace(/\/+$/, "").lastIndexOf("/");
	return k > 0 ? p.slice(0, k) : k === 0 ? "/" : ".";
};
const WRAPPERS = new Set(["sudo", "env", "command", "exec", "nohup", "nice", "time", "builtin", "doas"]);

/**
 * True when a bash command changes or removes the rules file `file` (see the header). `home` expands "~" and
 * $HOME in operands. Reads are allowed. Tokens are split on whitespace; quotes are stripped from each token.
 */
export function bashWritesRulesFile(cmd: string, file: string, home?: string): boolean {
	const base = lastPart(file);
	const dir = parentOf(file);
	const dirBase = lastPart(dir);
	const esc = base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
	if (new RegExp(`>{1,2}\\|?\\s*\\S*${esc}`).test(cmd)) return true; // redirect to the file
	const expand = (t: string): string => {
		if (home && (t === "~" || t.startsWith("~/"))) return home + t.slice(1);
		if (home && (t === "$HOME" || t.startsWith("$HOME/"))) return home + t.slice(5);
		if (home && (t === "${HOME}" || t.startsWith("${HOME}/"))) return home + t.slice(7);
		return t;
	};
	const isFile = (t: string): boolean => t === base || t.endsWith(`/${base}`);
	/** the parent directory or a directory above it ("/x/*" and "/x/" count as "/x") */
	const isDirUp = (t0: string): boolean => {
		const t = expand(t0).replace(/\/\*$/, "").replace(/\/+$/, "");
		if (t === dirBase) return true;                        // relative name of the parent dir
		if (!t.startsWith("/") && t !== "") return false;      // other relative paths: not seen (documented)
		return t === "" || dir === t || dir.startsWith(`${t}/`);
	};
	const isDir = (t0: string): boolean => {
		const t = expand(t0).replace(/\/+$/, "");
		return t === dir || t === dirBase;
	};
	const hits = (t: string) => isFile(t) || isDirUp(t);
	for (const seg of cmd.split(/&&|\|\||[;&|\n()`]|\$\(/)) {
		const toks = seg.trim().split(/\s+/).filter(Boolean).map(t => t.replace(/^['"]+|['"]+$/g, ""));
		let k = 0;
		while (k < toks.length && (WRAPPERS.has(toks[k]!) || /^[A-Za-z_]\w*=/.test(toks[k]!))) k++;
		if (k >= toks.length) continue;
		const name = lastPart(toks[k]!);
		const args = toks.slice(k + 1);
		const ops = args.filter(t => !t.startsWith("-"));
		const opt = (re: RegExp) => args.some(t => re.test(t));
		switch (name) {
			case "rm": case "unlink": case "shred": case "truncate": case "mv":
			case "chmod": case "chown": case "chgrp": case "chflags": case "chattr":
				if (ops.some(hits)) return true;
				break;
			case "rmdir":
				if (ops.some(isDirUp)) return true;
				break;
			case "tee":
				if (ops.some(isFile)) return true;
				break;
			case "cp": case "ln": case "install": case "rsync": case "ditto": {
				const ti = args.findIndex(t => t === "-t" || t.startsWith("--target-directory"));
				const target = ti >= 0 ? (args[ti]!.includes("=") ? args[ti]!.split("=")[1]! : args[ti + 1] ?? "") : ops[ops.length - 1];
				if (!target || ops.length < (ti >= 0 ? 1 : 2)) break;
				if (isFile(target)) return true;
				// "cp x/<base> <dir>/" overwrites the rules file too
				if (isDir(target) && ops.some(o => o !== target && isFile(o))) return true;
				break;
			}
			case "sed": case "gsed":
				if (opt(/^(?:-[a-zA-Z]*i|--in-place)/) && ops.some(isFile)) return true;
				break;
			case "perl":
				if (opt(/^-[a-zA-Z]*i/) && ops.some(isFile)) return true;
				break;
			case "dd":
				if (args.some(t => t.startsWith("of=") && isFile(t.slice(3)))) return true;
				break;
		}
	}
	return false;
}

/** Self-protect rules for the rules file. Only while at least one rule is active. */
export function selfProtect(file: string, home?: string): Compiled[] {
	const base = file.split("/").pop() ?? file;
	const esc = base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
	const reason = "the guard rules file is changed only by the user";
	return [
		{ id: "guard-file-write", tool: "write", match: esc, reason, source: "built-in", re: new RegExp(`"path":"[^"]*${esc}"`) },
		{ id: "guard-file-edit", tool: "edit", match: esc, reason, source: "built-in", re: new RegExp(`"path":"[^"]*${esc}"`) },
		{
			id: "guard-file-bash", tool: "bash", match: `(write verb) ${esc} or its directory`, reason, source: "built-in",
			re: new RegExp(esc), fn: (cmd: string) => bashWritesRulesFile(cmd, file, home),
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
		if (nestedQuantifier(match!)) return { op: "help", error: "the regex has a nested quantifier like (a+)+ (slow); write it without one" };
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
