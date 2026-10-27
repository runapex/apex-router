/**
 * Compaction ledger core (docs/research/2026-10-26-pi-compaction-precision-cost.md, options P1 P2 P4 P6 P3).
 * Pure: no pi imports, no I/O. The extension (apex-compact.ts) and the runner (lib/compactrun.ts) apply it.
 *
 * P1 ledger: deterministic extraction from the session entries, carried in CompactionEntry.details.
 *   rule  — a user sentence with must / never / always / do not / don't / only / without (verbatim, with the
 *           source entry id). Status active | done | retired. Pasted text is not read: fenced code blocks,
 *           lines that start with ">" and text in quotes do not make a rule.
 *   id    — git sha (7-40 hex with a digit and a letter), EYE-<n>, pi job id, #<PR>, from user + assistant text.
 *   nogo  — a NO-GO line (assistant, user or tool result). Open until a later GO verdict.
 *   error — the last failing tool call: tool, args (300 chars), error text (first 600 chars). Only the latest.
 * Retire rules: only an explicit reference retires a rule: "forget R<n>" / "drop R<n>" (or ignore, cancel,
 * lift ...), "forget that" (the newest rule), or the exact rule text in a retire message. "replace R<n>: <text>"
 * retires R<n> and adds <text> as a new rule. "<label> done" marks it done. Word overlap never retires a rule.
 * Cap: about APEX_COMPACT_LEDGER_TOKENS (default 3000). Drop retired, then done, then oldest closed nogo,
 * then oldest ids, then oldest rules. The latest error stays.
 * P2 gate: each active item must be in the final text; missing ids are appended, rules / nogo / error are
 * always pinned verbatim. Telemetry holds counts only, never text.
 */

export const LEDGER_VERSION = 1;
export const PINNED_MARKER = "<!-- apex-compact:pinned -->";
export const PINNED_HEADER = "## Pinned (verbatim)";
export const DEFAULT_LEDGER_TOKENS = 3000;
export const DEFAULT_SUMMARY_TOKENS = 6000;
export const ERROR_CHARS = 600;
export const ARGS_CHARS = 300;
const RULE_CHARS = 600;
const NOGO_CHARS = 300;
const SCAN_CHARS = 20_000; // only the first 20 k chars of one message are scanned (pasted logs are noise)

export type Kind = "rule" | "id" | "nogo" | "error";
export type Status = "active" | "done" | "retired";
export type Item = {
	key: string; // kind + normalized text: the dedupe key
	kind: Kind;
	seq: number; // label number, stable across compactions (R<seq>, N<seq>, E<seq>)
	text: string; // verbatim
	source: string; // session entry id the text came from
	scope: "session" | "until-go" | "latest";
	status: Status;
	retiredBy?: string; // entry id of the message that retired / closed / replaced it
	tool?: string; // error only
	args?: string; // error only
};
export type Ledger = { v: number; nextSeq: number; scannedTo?: string; items: Item[] };

export const emptyLedger = (): Ledger => ({ v: LEDGER_VERSION, nextSeq: 1, items: [] });

/** A ledger from CompactionEntry.details.apexLedger, or null when absent / malformed. */
export function readLedger(details: unknown): Ledger | null {
	const l = (details as { apexLedger?: Ledger } | undefined)?.apexLedger;
	if (!l || typeof l !== "object" || !Array.isArray(l.items) || typeof l.nextSeq !== "number") return null;
	return { v: LEDGER_VERSION, nextSeq: l.nextSeq, scannedTo: l.scannedTo, items: l.items.filter(i => i && typeof i.text === "string" && typeof i.kind === "string").map(i => ({ ...i })) };
}

export const label = (i: Item): string => `${{ rule: "R", id: "I", nogo: "N", error: "E" }[i.kind]}${i.seq}`;
export const norm = (s: string): string => s.toLowerCase().replace(/\s+/g, " ").trim();

// ---- patterns ---------------------------------------------------------------------------------------------
/** A user sentence that states a rule. "only" and "stop" alone are too common in long text; see isRule. */
const RULE_STRONG = /\b(must(?:\s+not)?|never|always|do not|don't|dont|shall not|may not|mustn't|is forbidden|not allowed)\b/i;
const RULE_WEAK = /\b(only|without|stop)\b/i;
export const IDS = /\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b|\bEYE-\d+\b|\b\d{8}-?\d{6}-[a-z0-9]{4}\b|(?<![\w#&])#\d{3,6}\b/g;
/** A retire verb at the start of a clause ("forget R3", "ok, drop the push rule"), not inside a rule ("never
 *  ignore lint errors" is a rule, not a retire message). */
const RETIRE = /(?:^|[.;:,!?\n]\s*|\b(?:please|ok|okay|now|you can|we can)\s+)(?:forget|ignore|disregard|cancel|drop|lift|revoke|remove)\b|\b(?:no longer (?:needed|applies|valid|relevant)|not needed any ?more|scratch that|never ?mind|rule is (?:off|gone|retired))\b/i;
const DONE = /\b(done|finished|completed|resolved)\b/i;
/** A leading retire verb ("ok, forget: ", "please drop ") and a trailing done-word ("... - done."), anchored. */
const LEAD_RETIRE = /^\W*(?:(?:ok(?:ay)?|please|now)[,.]?\s+)*(?:forget|ignore|disregard|cancel|drop|lift|revoke|remove)\b\s*(?:the rule)?\s*[:\-\u2013\u2014]?\s*/i;
const TRAIL_DONE = /\s*[,:;\-\u2013\u2014]?\s*(?:is\s+)?(?:done|finished|completed|resolved)\W*$/i;
const PRONOUN_ONLY = /^\W*(?:ok(?:ay)?[,.]?\s+)?(?:please\s+)?(?:forget|ignore|drop|cancel|scratch|disregard|never ?mind)\s*(?:that|it|this|the last (?:rule|one)|what i said)?\s*(?:rule)?\W*$/i;
const NOGO = /\bNO[- ]?GO\b/;
const GO_VERDICT = /(?<![-\w])(?:GO|PASS)(?![-\w])/;
const GO_CONTEXT = /\b(verdict|review|gate|result|decision|go\/no-go|xval|approved?)\b/i;
const LABEL_REF = /\b([RINE])(\d{1,5})\b/g;
const REPLACE = /^\W*(?:ok(?:ay)?[,.]?\s+)?(?:please\s+)?replace\s+R(\d{1,5})\s*(?::|with\b)\s*([\s\S]+)$/i;
const STOP = new Set(["that", "this", "with", "from", "have", "will", "your", "about", "there", "their", "them", "then", "than", "when", "what", "which", "where", "into", "only", "must", "never", "always", "dont", "don't", "should", "would", "could", "rule", "please", "again", "just", "also", "make", "sure", "done", "forget", "ignore", "drop", "cancel", "longer", "anymore", "more", "before", "after", "every", "rules", "need", "needs"]);

export function words(s: string, min = 4): Set<string> {
	return new Set((s.toLowerCase().match(/[a-z0-9_.-]+/g) ?? []).filter(w => w.length >= min && !STOP.has(w)));
}

/** Audit heuristic (scripts/compaction_audit.py): >= 50 % of the first 6 words of >= 6 chars occur in `hay`. */
export function auditMatch(rule: string, hay: string): boolean {
	const w = (rule.toLowerCase().match(/[a-z0-9_.-]{6,}/g) ?? []).slice(0, 6);
	if (!w.length) return hay.toLowerCase().includes(norm(rule));
	const h = hay.toLowerCase();
	return w.filter(x => h.includes(x)).length / w.length >= 0.5;
}

/** User text without pasted parts: fenced code blocks (``` or ~~~; a fence that is not closed runs to the end)
 *  and quote lines (">"). */
export function stripPasted(text: string): string {
	const out: string[] = [];
	let fence: string | null = null;
	for (const line of text.split("\n")) {
		const m = /^\s*(`{3,}|~{3,})/.exec(line);
		if (fence) {
			if (m && m[1]![0] === fence[0] && m[1]!.length >= fence.length) fence = null;
			continue;
		}
		if (m) {
			fence = m[1]!;
			continue;
		}
		if (/^\s*>/.test(line)) continue;
		out.push(line);
	}
	return out.join("\n");
}

/** Text in quotes ("..", “..”, «..», '..' not inside a word) is removed: a quoted log line or message is not a rule. */
const QUOTED = /"[^"\n]*"|\u201c[^\u201d\n]*\u201d|\u00ab[^\u00bb\n]*\u00bb|(?<![A-Za-z0-9])'[^'\n]+'(?![A-Za-z0-9])/g;
export const unquoted = (s: string): string => s.replace(QUOTED, " ");

/** Rule sentences of one user message, verbatim. A short message (< 800 chars) is one candidate. Fenced code,
 *  ">" lines and quoted text are not read (a pasted log line is not a user rule). */
export function extractRules(text: string): string[] {
	const raw = text.slice(0, SCAN_CHARS).trim();
	if (!raw || /^[/!]/.test(raw)) return []; // slash command or user bash
	const t = stripPasted(raw).trim();
	if (!t || PRONOUN_ONLY.test(t)) return [];
	const isRule = (s0: string, short: boolean) => {
		const s = unquoted(s0);
		return (RULE_STRONG.test(s) || (short && RULE_WEAK.test(s))) && words(s, 3).size >= 2;
	};
	// Quotes only decide IF a text is a rule (a quoted log line alone is not). The stored rule is the user's
	// text verbatim, with its framing ("the log says ..."): editing it lost literals like "--dry-run" (xval pass 3).
	if (t.length < 800) return isRule(t, true) ? [t] : [];
	const out: string[] = [];
	for (const raw of t.split(/\n+|(?<=[.!?])\s+(?=[A-Z0-9"'`*(-])/)) {
		const s = raw.trim();
		if (s.length >= 12 && s.length <= RULE_CHARS && !/^```/.test(s) && isRule(s, false)) out.push(s);
		if (out.length >= 12) break;
	}
	return out;
}

export function extractIds(text: string): string[] {
	return [...new Set(text.slice(0, SCAN_CHARS).match(IDS) ?? [])];
}

export function extractNogo(text: string): string[] {
	const out: string[] = [];
	for (const line of text.slice(0, SCAN_CHARS).split("\n")) {
		if (NOGO.test(line)) out.push(line.trim().slice(0, NOGO_CHARS));
		if (out.length >= 3) break;
	}
	return out;
}

const isGoVerdict = (text: string): boolean =>
	text.split("\n").some(l => !NOGO.test(l) && GO_VERDICT.test(l) && GO_CONTEXT.test(l));

// ---- session entries ----------------------------------------------------------------------------------------
type Block = { type?: string; text?: string; id?: string; name?: string; arguments?: unknown };
export type EntryLike = { type: string; id: string; message?: { role?: string; content?: unknown; toolCallId?: string; toolName?: string; isError?: boolean }; details?: unknown };

function contentText(c: unknown): string {
	if (typeof c === "string") return c;
	if (!Array.isArray(c)) return "";
	return (c as Block[]).filter(b => b?.type === "text" && typeof b.text === "string").map(b => b.text).join("\n");
}

function oneLine(s: string, n: number): string {
	const t = s.replace(/\s+/g, " ").trim();
	return t.length > n ? `${t.slice(0, n)}…` : t;
}

/** Fold one session branch into the ledger. Entries up to `prev.scannedTo` were folded before and are skipped
 *  (when that id is not on the branch, all entries are scanned again; dedupe keeps that safe). Pure. */
export function updateLedger(prev: Ledger | null, entries: EntryLike[]): Ledger {
	const l: Ledger = prev ? { ...prev, items: prev.items.map(i => ({ ...i })) } : emptyLedger();
	let start = 0;
	if (l.scannedTo) {
		const k = entries.findIndex(e => e.id === l.scannedTo);
		if (k >= 0) start = k + 1;
	}
	const byKey = new Map(l.items.map(i => [i.key, i]));
	const calls = new Map<string, { name: string; args: string }>();
	for (let k = 0; k < start; k++) collectCalls(entries[k]!, calls); // a call before the watermark can fail after it
	const add = (kind: Kind, text: string, source: string, scope: Item["scope"]): Item => {
		const key = `${kind}:${norm(text)}`;
		const old = byKey.get(key);
		if (old) {
			if (old.status !== "active" && kind !== "id") {
				old.status = "active"; // said again: active again
				delete old.retiredBy;
			}
			old.source = source;
			return old;
		}
		const it: Item = { key, kind, seq: l.nextSeq++, text, source, scope, status: "active" };
		l.items.push(it);
		byKey.set(key, it);
		return it;
	};
	for (let k = start; k < entries.length; k++) {
		const e = entries[k]!;
		if (e.type !== "message" || !e.message) continue;
		const m = e.message;
		const text = contentText(m.content);
		if (m.role === "user") {
			const rt = applyRetire(l, text, e.id);
			if (rt.replacement) add("rule", rt.replacement, e.id, "session");
			else if (!rt.retire) for (const r of extractRules(text)) add("rule", r, e.id, "session");
			for (const id of extractIds(text)) add("id", id, e.id, "session");
			for (const n of extractNogo(text)) add("nogo", n, e.id, "until-go");
			if (isGoVerdict(text)) closeNogo(l, e.id);
		} else if (m.role === "assistant") {
			collectCalls(e, calls);
			for (const id of extractIds(text)) add("id", id, e.id, "session");
			for (const n of extractNogo(text)) add("nogo", n, e.id, "until-go");
			if (isGoVerdict(text)) closeNogo(l, e.id);
		} else if (m.role === "toolResult") {
			for (const n of extractNogo(text)) add("nogo", n, e.id, "until-go");
			if (m.isError) {
				const call = calls.get(m.toolCallId ?? "");
				l.items = l.items.filter(i => i.kind !== "error");
				for (const [key, i] of byKey) if (i.kind === "error") byKey.delete(key);
				const it = add("error", oneLine(text, ERROR_CHARS) || "(no error text)", e.id, "latest");
				it.tool = m.toolName ?? call?.name ?? "?";
				it.args = call ? oneLine(call.args, ARGS_CHARS) : "";
			}
		}
	}
	if (entries.length) l.scannedTo = entries[entries.length - 1]!.id;
	return l;
}

function collectCalls(e: EntryLike, calls: Map<string, { name: string; args: string }>): void {
	if (e.type !== "message" || e.message?.role !== "assistant" || !Array.isArray(e.message.content)) return;
	for (const b of e.message.content as Block[]) {
		if (b?.type === "toolCall" && b.id) {
			let args = "";
			try {
				args = typeof b.arguments === "string" ? b.arguments : JSON.stringify(b.arguments ?? {});
			} catch {
				args = "";
			}
			calls.set(b.id, { name: b.name ?? "?", args });
		}
	}
}

function retire(i: Item, status: Status, by: string): void {
	if (i.status === "retired") return;
	i.status = status;
	i.retiredBy = by;
}

/**
 * forget / ignore / done / replace messages. A false retire loses a rule (the failure this extension fixes), so
 * only an explicit reference counts: a label ("forget R3", "drop R3", "R3 done"), "forget that" (the newest
 * rule), the exact rule text, or "replace R<n>: <text>". Word overlap never retires a rule ("always encrypt
 * backups" must not retire "never delete backups"). Only short messages (< 400 chars) retire; a long brief that
 * says "ignore the warnings" must not. Returns retire = true for a retire message (it is not a new rule) and
 * the new rule text of a replace message.
 */
export function applyRetire(l: Ledger, text: string, entryId: string): { retire: boolean; replacement?: string } {
	const t = text.trim();
	if (!t) return { retire: false };
	const rep = t.length <= SCAN_CHARS ? REPLACE.exec(t) : null;   // any length: a long replace must not leave the old rule
	if (rep) {
		const target = l.items.find(i => i.kind === "rule" && i.status === "active" && label(i) === `R${Number(rep[1])}`);
		if (target) retire(target, "retired", entryId);
		const nt = rep[2]!.trim();
		return { retire: true, replacement: nt.length >= 3 ? nt : undefined };
	}
	if (t.length >= 400) return { retire: false };
	const isRetire = RETIRE.test(t), isDone = DONE.test(t);
	if (!isRetire && !isDone) return { retire: false };
	const status: Status = isRetire ? "retired" : "done";
	// 1. explicit labels: "forget R3", "R3 done", "N2 resolved"
	const refs = [...t.matchAll(LABEL_REF)].map(m => `${m[1]}${m[2]}`);
	if (refs.length) {
		for (const i of l.items) if (i.status === "active" && refs.includes(label(i))) retire(i, i.kind === "nogo" && isDone ? "done" : status, entryId);
		return { retire: isRetire };
	}
	const rules = l.items.filter(i => i.kind === "rule" && i.status === "active");
	// 2. "forget that": the newest active rule
	if (isRetire && PRONOUN_ONLY.test(t)) {
		const last = rules.sort((a, b) => b.seq - a.seq)[0];
		if (last) retire(last, "retired", entryId);
		return { retire: true };
	}
	// 3. the exact rule text, anchored: "<verb>[:] <rule>", "<rule> - done", or the rule in quotes. Only the
	//    leading verb or trailing done-word is cut; the rule text itself is never edited (xval pass 3).
	const edge = (x: string): string => norm(x).replace(/^[\W_]+|[\W_]+$/g, "");
	// a candidate exists only when the anchored verb / done-word was really there: a message that just repeats
	// a rule (which can contain "completed") must not retire it (xval pass 4)
	const cut = (re: RegExp): string | null => (re.test(t) ? edge(t.replace(re, "")) : null);
	const cands = new Set<string>([
		cut(LEAD_RETIRE), cut(TRAIL_DONE),
		...(isRetire || isDone ? [...t.matchAll(/"([^"\n]+)"|\u201c([^\u201d\n]+)\u201d/g)].map(m => edge(m[1] ?? m[2] ?? "")) : []),
	].filter((x): x is string => !!x));
	let hit = false;
	for (const r of rules) if (cands.has(edge(r.text))) {
		retire(r, status, entryId);
		hit = true;
	}
	return { retire: isRetire || hit };   // a message that retired a rule is not itself a new rule
}

function closeNogo(l: Ledger, entryId: string): void {
	for (const i of l.items) if (i.kind === "nogo" && i.status === "active") retire(i, "done", entryId);
}

// ---- cap --------------------------------------------------------------------------------------------------
export const itemTokens = (i: Item): number => Math.ceil((i.text.length + (i.args?.length ?? 0) + 40) / 4);
export const ledgerTokens = (l: Ledger): number => l.items.reduce((s, i) => s + itemTokens(i), 0);

/** Keep the ledger under `maxTokens`. Drop order: retired, done, closed-nogo, oldest ids, oldest open nogo,
 *  oldest active rules. The latest error is kept. Returns the number of dropped items. */
export function capLedger(l: Ledger, maxTokens: number = DEFAULT_LEDGER_TOKENS): number {
	const rank = (i: Item): number => {
		if (i.kind === "error") return 99;
		if (i.status === "retired") return 0;
		if (i.status === "done") return 1;
		if (i.kind === "id") return 2;
		if (i.kind === "nogo") return 3;
		return 4;
	};
	const order = [...l.items].sort((a, b) => rank(a) - rank(b) || a.seq - b.seq);
	let total = ledgerTokens(l), dropped = 0;
	const gone = new Set<Item>();
	for (const i of order) {
		if (total <= maxTokens) break;
		if (rank(i) === 99) continue;
		gone.add(i);
		total -= itemTokens(i);
		dropped++;
	}
	if (gone.size) l.items = l.items.filter(i => !gone.has(i));
	return dropped;
}

// ---- render + gate (P1 + P2) --------------------------------------------------------------------------------
/** Remove the pinned block (and all after it) from an earlier summary: the ledger, not the LLM, carries it. */
export function stripPinned(summary: string | undefined): string | undefined {
	if (summary === undefined) return undefined;
	const k = summary.indexOf(PINNED_MARKER);
	return k < 0 ? summary : summary.slice(0, k).trimEnd();
}

export type GateResult = { text: string; counts: Record<Kind, number>; missed: Record<Kind, number>; appended: Record<Kind, number> };

/**
 * P2 gate + P1 render. `summary` is the LLM text (with file lists). Each active item is checked against it:
 * rules / nogo / error by the audit word overlap, ids by exact text. The pinned block then holds all active
 * rules, open nogo and the error verbatim, plus each id the summary missed. A final check makes sure each
 * active item's text is in the result and appends it if not.
 */
export function gate(summary: string, l: Ledger): GateResult {
	const active = l.items.filter(i => i.status === "active");
	const zero = (): Record<Kind, number> => ({ rule: 0, id: 0, nogo: 0, error: 0 });
	const counts = zero(), missed = zero(), appended = zero();
	const low = summary.toLowerCase();
	const missingIds: Item[] = [];
	for (const i of active) {
		counts[i.kind]++;
		const hit = i.kind === "id" ? low.includes(i.text.toLowerCase()) : auditMatch(i.text, summary);
		if (!hit) missed[i.kind]++;
		if (i.kind === "id" && !hit) missingIds.push(i);
	}
	const sec: string[] = [];
	const rules = active.filter(i => i.kind === "rule");
	const nogo = active.filter(i => i.kind === "nogo");
	const err = active.find(i => i.kind === "error");
	if (rules.length) sec.push(`### User rules (active; quoted user text, source = session entry id)\n${rules.map(i => `- ${label(i)} [user, entry ${i.source}]: "${i.text}"`).join("\n")}`);
	if (nogo.length) sec.push(`### Open NO-GO findings\n${nogo.map(i => `- ${label(i)} [${i.source}] ${i.text}`).join("\n")}`);
	if (err) sec.push(`### Last failing tool call [${err.source}]\n- tool: ${err.tool ?? "?"}\n- args: ${err.args ?? ""}\n- error: ${err.text}`);
	if (missingIds.length) sec.push(`### Ids (not in the summary above)\n${missingIds.map(i => i.text).join(", ")}`);
	for (const k of ["rule", "nogo", "error"] as const) appended[k] = active.filter(i => i.kind === k).length;
	appended.id = missingIds.length;
	let text = summary;
	if (sec.length) {
		text = `${summary.trimEnd()}\n\n${PINNED_MARKER}\n${PINNED_HEADER}\nThese items come from the session text, not from a model. To end a rule, the user says "forget R<n>", "replace R<n>: <text>" or "R<n> done".\n\n${sec.join("\n\n")}`;
	}
	// final check: every active item text is in the result
	const lowText = text.toLowerCase();
	const extra = active.filter(i => !lowText.includes(i.text.toLowerCase()));
	if (extra.length) text += `\n${extra.map(i => `- ${label(i)} ${i.text}`).join("\n")}`;
	return { text, counts, missed, appended };
}

// ---- P4 budget + env ----------------------------------------------------------------------------------------
export function envInt(env: Record<string, string | undefined>, name: string, def: number | undefined, min = 1): number | undefined {
	const raw = env[name];
	if (raw === undefined || raw === "") return def;
	const v = Number(raw);
	return Number.isFinite(v) && v >= min ? Math.floor(v) : def;
}

/** pi compact() gives the history summary 0.8 * reserveTokens and a split-turn prefix 0.5 * reserveTokens.
 *  This reserveTokens makes the history summary budget `summaryTokens` (+ the thinking budget, if any). */
export const reserveForBudget = (summaryTokens: number, thinkingTokens = 0): number => Math.ceil((summaryTokens + thinkingTokens) / 0.8);

export const budgetInstruction = (summaryTokens: number): string =>
	`Keep the whole summary under ${summaryTokens} tokens. When space is short, shorten old Done items and old ` +
	`decisions first; keep the goal, the current work, the next steps and exact errors. Do not copy the ` +
	`"Pinned (verbatim)" block: the system adds it after your summary.`;

// ---- P6 keep more recent tokens ------------------------------------------------------------------------------
export type PrepLike = {
	firstKeptEntryId: string;
	messagesToSummarize: unknown[];
	turnPrefixMessages: unknown[];
	isSplitTurn: boolean;
	tokensBefore: number;
	previousSummary?: string;
	fileOps: { read: Set<string>; written: Set<string>; edited: Set<string> };
	settings: { enabled: boolean; reserveTokens: number; keepRecentTokens: number };
};

/**
 * Move the cut earlier so about `keepTokens` recent tokens stay raw. The new cut is a user message (a turn
 * start), never a tool result, never later than pi's cut and never before the previous kept boundary. The
 * kept part never grows above `maxKeepTokens` (the runner passes half of the usable window). Returns null when no change is possible or nothing would be left to summarize.
 */
export function moveCutEarlier<E extends EntryLike>(
	entries: E[], prep: PrepLike, keepTokens: number,
	toMessage: (e: E) => unknown | undefined, tokens: (m: unknown) => number,
	maxKeepTokens: number = Number.POSITIVE_INFINITY,
): { prep: PrepLike; firstKeptEntryId: string } | null {
	const cutIdx = entries.findIndex(e => e.id === prep.firstKeptEntryId);
	if (cutIdx < 0) return null;
	let lo = 0;
	for (let k = entries.length - 1; k >= 0; k--) {
		const e = entries[k]! as EntryLike & { firstKeptEntryId?: string };
		if (e.type === "compaction") {
			const b = entries.findIndex(x => x.id === e.firstKeptEntryId);
			lo = b >= 0 ? b : k + 1;
			break;
		}
	}
	// a cut must leave at least one message to summarize
	let firstMsg = lo;
	while (firstMsg < cutIdx && (entries[firstMsg]!.type === "compaction" || !toMessage(entries[firstMsg]!))) firstMsg++;
	let acc = 0, best = -1;
	for (let k = entries.length - 1; k > firstMsg; k--) {
		const e = entries[k]!;
		const m = e.type === "compaction" ? undefined : toMessage(e);
		if (m) acc += tokens(m);
		if (acc > maxKeepTokens) break; // keep the kept part well below the window, or compaction loops
		if (k < cutIdx && e.type === "message" && e.message?.role === "user") {
			best = k;
			if (acc >= keepTokens) break;
		}
	}
	if (best < 0 || best >= cutIdx) return null;
	const msgs: unknown[] = [];
	for (let k = lo; k < best; k++) {
		const e = entries[k]!;
		if (e.type === "compaction") continue;
		const m = toMessage(e);
		if (m) msgs.push(m);
	}
	if (!msgs.length) return null;
	const id = entries[best]!.id;
	return { firstKeptEntryId: id, prep: { ...prep, firstKeptEntryId: id, messagesToSummarize: msgs, turnPrefixMessages: [], isSplitTurn: false } };
}

// ---- P3 replay of the cached prefix -------------------------------------------------------------------------
export const replayInstruction = (summaryTokens: number): string =>
	`STOP the task. Do not call tools. Do not continue the work. Write a structured summary of the whole ` +
	`conversation above (the earlier compaction summary in it included), so that another agent can continue ` +
	`from it. Use these sections: ## Goal, ## Constraints & Preferences, ## Progress (### Done, ### In ` +
	`Progress, ### Blocked), ## Key Decisions, ## Next Steps, ## Critical Context. Keep exact file paths, ` +
	`function names, ids and error messages. ${budgetInstruction(summaryTokens)}`;

/**
 * The captured provider payload plus one final user instruction. The prefix (system, tools, messages) is not
 * changed, so a provider prompt cache can hit. Known shapes: `messages` (Anthropic, OpenAI chat) and `input`
 * (OpenAI responses). Returns null for an unknown shape. The output budget gets `summaryTokens` more room;
 * an Anthropic thinking budget is kept (a change to it drops the message cache).
 */
export function buildReplayPayload(payload: unknown, instruction: string, summaryTokens: number): Record<string, unknown> | null {
	if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
	const p = { ...(payload as Record<string, unknown>) };
	const thinking = (p.thinking as { type?: string; budget_tokens?: number } | undefined)?.budget_tokens ?? 0;
	if (Array.isArray(p.messages)) {
		const msgs = [...(p.messages as Record<string, unknown>[])];
		const last = msgs[msgs.length - 1];
		// Anthropic: top-level `system`, no `max_completion_tokens`. Two user turns in a row are not allowed
		// there, so the instruction goes into the last user turn as one more block (after its cache breakpoint).
		const anthropic = "system" in p && !("max_completion_tokens" in p);
		if (anthropic && last?.role === "user") {
			const old = Array.isArray(last.content) ? (last.content as unknown[]) : [{ type: "text", text: String(last.content ?? "") }];
			msgs[msgs.length - 1] = { ...last, content: [...old, { type: "text", text: instruction }] };
		} else {
			msgs.push({ role: "user", content: anthropic ? [{ type: "text", text: instruction }] : instruction });
		}
		p.messages = msgs;
		if ("max_completion_tokens" in p) p.max_completion_tokens = Math.max(Number(p.max_completion_tokens) || 0, summaryTokens + 2048);
		else p.max_tokens = Math.max(Number(p.max_tokens) || 0, summaryTokens + thinking + 1024);
	} else if (Array.isArray(p.input)) {
		p.input = [...(p.input as unknown[]), { role: "user", content: [{ type: "input_text", text: instruction }] }];
		p.max_output_tokens = Math.max(Number(p.max_output_tokens) || 0, summaryTokens + 4096);
	} else {
		return null;
	}
	delete p.tool_choice; // a forced tool choice cannot hold: the replay needs text
	return p;
}

/** Text of an assistant reply; null when it is an error, cut by length, calls a tool or is empty. */
export function replyText(msg: { content?: unknown; stopReason?: string } | undefined): string | null {
	if (!msg || msg.stopReason === "error" || msg.stopReason === "length" || msg.stopReason === "aborted") return null;
	const blocks = Array.isArray(msg.content) ? (msg.content as Block[]) : [];
	if (blocks.some(b => b?.type === "toolCall")) return null;
	const t = blocks.filter(b => b?.type === "text").map(b => b.text ?? "").join("\n").trim();
	return t || null;
}

// ---- file lists (same output as pi computeFileLists + formatFileOperations) --------------------------------
export function fileLists(ops: PrepLike["fileOps"]): { readFiles: string[]; modifiedFiles: string[]; text: string } {
	const modified = new Set([...ops.edited, ...ops.written]);
	const readFiles = [...ops.read].filter(f => !modified.has(f)).sort();
	const modifiedFiles = [...modified].sort();
	const sec: string[] = [];
	if (readFiles.length) sec.push(`<read-files>\n${readFiles.join("\n")}\n</read-files>`);
	if (modifiedFiles.length) sec.push(`<modified-files>\n${modifiedFiles.join("\n")}\n</modified-files>`);
	return { readFiles, modifiedFiles, text: sec.length ? `\n\n${sec.join("\n\n")}` : "" };
}

/** pi reads file lists of an earlier compaction only when pi made it (fromHook false). Merge them here so the
 *  lists stay cumulative after this extension wrote a compaction. Returns a new FileOperations. */
export function mergeFileOps(ops: PrepLike["fileOps"], prevDetails: unknown): PrepLike["fileOps"] {
	const out = { read: new Set(ops.read), written: new Set(ops.written), edited: new Set(ops.edited) };
	const d = prevDetails as { readFiles?: unknown; modifiedFiles?: unknown } | undefined;
	if (Array.isArray(d?.readFiles)) for (const f of d!.readFiles) if (typeof f === "string") out.read.add(f);
	if (Array.isArray(d?.modifiedFiles)) for (const f of d!.modifiedFiles) if (typeof f === "string") out.edited.add(f);
	return out;
}
