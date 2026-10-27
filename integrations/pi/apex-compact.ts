/**
 * apex-compact — compaction that keeps user rules, ids, NO-GO findings and the last error, and a tool_call
 * guard for hard limits (docs/research/2026-10-26-pi-compaction-precision-cost.md, options P0-P6).
 *
 * session_before_compact (P1 P2 P4, opt-in P3 P5 P6): lib/compactrun.ts makes the summary, then appends the
 *   "## Pinned (verbatim)" block from the ledger (carried in CompactionEntry.details.apexLedger). On any
 *   failure it returns nothing and pi compacts as before. Telemetry: ~/.apex-router/pi-compact/
 *   YYYY-MM-DD-<pid>.jsonl (counts and reason codes only, never text; mode 0600; 14 days).
 * before_provider_request (P3 only, APEX_COMPACT_CACHE=1): keeps a reference to the last payload and the branch
 *   leaf id. session_start, session_tree and model_select clear it (no replay of another branch or model).
 * tool_call (P0): blocks a call that matches an active deny rule of ~/.apex-router/pi-guardrules.json or of
 *   /guard (session). Example rules: integrations/pi/guardrules.example.json.
 *
 * Env (defaults): APEX_COMPACT=0 off · APEX_COMPACT_SUMMARY_TOKENS 6000 · APEX_COMPACT_LEDGER_TOKENS 3000 ·
 * APEX_COMPACT_CACHE unset (P3 off) · APEX_COMPACT_CACHE_MAX_AGE_S 300 · APEX_COMPACT_MODEL unset (P5 off) ·
 * APEX_COMPACT_KEEP_TOKENS unset (P6 off) · APEX_GUARD=0 off · APEX_GUARDRULES_FILE ~/.apex-router/pi-guardrules.json.
 */
import { readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import {
	compact, estimateTokens, sessionEntryToContextMessages,
	type ExtensionAPI, type ExtensionContext, type SessionEntry,
} from "@earendil-works/pi-coding-agent";

import type { EntryLike, PrepLike } from "./lib/compact.ts";
import { runCompaction, type Captured, type ModelRef, type Reply } from "./lib/compactrun.ts";
import { applyGuardCmd, check, compileRules, formatRules, GUARD_HELP, liveRules, parseGuardCmd, selfProtect, type Compiled, type GuardRule } from "./lib/guardrules.ts";
import { appendLog, pruneLogs } from "./lib/hangguard.ts";

const COMPACT_LOG = join(homedir(), ".apex-router", "pi-compact");
const GUARD_LOG = join(homedir(), ".apex-router", "pi-guard");
const GUARD_ENTRY = "apex-guard";
const rulesFile = () => process.env.APEX_GUARDRULES_FILE || join(homedir(), ".apex-router", "pi-guardrules.json");

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyModel = any;

export default function (pi: ExtensionAPI) {
	let captured: Captured | undefined;
	let sessionRules: GuardRule[] = [];
	let fileCache: { file: string; mtime: number; rules: Compiled[] } | undefined;
	let warned = "";
	let pruned = false;

	// ---- P0 guard -------------------------------------------------------------------------------------------
	const fileRules = (ctx: ExtensionContext): Compiled[] => {
		const file = rulesFile();
		let mtime = -1;
		try {
			mtime = statSync(file).mtimeMs;
		} catch {
			fileCache = { file, mtime, rules: [] };
			return [];
		}
		if (fileCache && fileCache.file === file && fileCache.mtime === mtime) return fileCache.rules;
		let rules: Compiled[] = [], errors: string[] = [];
		try {
			({ rules, errors } = compileRules(JSON.parse(readFileSync(file, "utf8")), "file"));
		} catch {
			errors = [`${file}: not valid JSON (no file rules are active)`];
		}
		fileCache = { file, mtime, rules };
		const w = errors.join("; ");
		if (w && w !== warned && ctx.hasUI) ctx.ui.notify(`apex guard: ${w}`, "warning");
		warned = w;
		return rules;
	};
	// the file cache key is the mtime: a rule with "expires" stays in the cache, so expiry is checked at each
	// call (liveRules here, and again in check())
	const activeRules = (ctx: ExtensionContext): Compiled[] => {
		const now = Date.now();
		const fromFile = liveRules(fileRules(ctx), now);
		const fromSession = compileRules(sessionRules, "session", now).rules;
		const all = [...fromFile, ...fromSession];
		return all.length && fromFile.length ? [...all, ...selfProtect(rulesFile(), homedir())] : all;
	};
	const restoreSession = (ctx: ExtensionContext) => {
		sessionRules = [];
		for (const e of ctx.sessionManager.getBranch()) {
			if (e.type === "custom" && e.customType === GUARD_ENTRY && Array.isArray((e.data as { rules?: unknown })?.rules)) {
				sessionRules = (e.data as { rules: GuardRule[] }).rules;
			}
		}
	};

	pi.on("session_start", async (_e, ctx) => {
		captured = undefined;
		restoreSession(ctx);
		if (!pruned) {
			pruned = true;
			pruneLogs(COMPACT_LOG, Date.now());
			pruneLogs(GUARD_LOG, Date.now());
		}
	});
	pi.on("session_tree", async (_e, ctx) => {
		captured = undefined; // the capture holds the old branch
		restoreSession(ctx);
	});
	pi.on("model_select", async () => {
		captured = undefined;
	});

	pi.on("tool_call", async (event, ctx) => {
		if (process.env.APEX_GUARD === "0") return;
		let v;
		try {
			v = check(activeRules(ctx), event.toolName, event.input);
		} catch {
			const now = Date.now();
			appendLog(GUARD_LOG, { ts: now, rule: null, error: "guard-exception", tool: event.toolName }, now);
			return; // fail open: a guard bug must not stop all tools (documented: guard against mistakes, not a sandbox)
		}
		if (!v.block) return;
		const now = Date.now();
		appendLog(GUARD_LOG, { ts: now, rule: v.rule.id, source: v.rule.source, tool: event.toolName }, now); // no input text
		return { block: true, reason: v.reason };
	});

	pi.registerCommand("guard", {
		description: "apex guard rules for this session: /guard list | add <id> [tool=x] <regex> -- <reason> | rm <id>",
		handler: async (args, ctx) => {
			const cmd = parseGuardCmd(args ?? "");
			if (cmd.op === "list") {
				ctx.ui.notify(formatRules(activeRules(ctx)), "info");
				return;
			}
			if (cmd.op === "help") {
				ctx.ui.notify(cmd.error ? `${cmd.error}\n${GUARD_HELP}` : GUARD_HELP, cmd.error ? "warning" : "info");
				return;
			}
			if (cmd.op === "rm" && !sessionRules.some(r => r.id === cmd.id)) {
				ctx.ui.notify(`apex guard: no session rule "${cmd.id}" (file rules are changed in ${rulesFile()})`, "warning");
				return;
			}
			sessionRules = applyGuardCmd(sessionRules, cmd);
			pi.appendEntry(GUARD_ENTRY, { rules: sessionRules });
			ctx.ui.notify(cmd.op === "add" ? `apex guard: rule "${cmd.rule.id}" is active for this session` : `apex guard: rule "${cmd.id}" removed`, "info");
		},
	});

	// ---- P3 capture -----------------------------------------------------------------------------------------
	pi.on("before_provider_request", (event, ctx) => {
		if (process.env.APEX_COMPACT_CACHE !== "1" || process.env.APEX_COMPACT === "0" || !ctx.model) return;
		captured = { payload: event.payload, provider: ctx.model.provider, id: ctx.model.id, at: Date.now(), leafId: ctx.sessionManager.getLeafId() };
		return undefined; // never changes the request
	});

	// ---- P1 P2 P4 P5 P6 compaction --------------------------------------------------------------------------
	pi.on("session_before_compact", async (event, ctx) => {
		if (process.env.APEX_COMPACT === "0") return;
		const reg = ctx.modelRegistry;
		const sessionModel = ctx.model as AnyModel;
		const summarize = async (model: AnyModel, prep: PrepLike, instr: string | undefined, signal?: AbortSignal) => {
			const auth = await reg.getApiKeyAndHeaders(model);
			if (!auth.ok) throw new Error("no auth");
			const m = auth.baseUrl ? { ...model, baseUrl: auth.baseUrl } : model;
			const provider = reg.getProvider(model.provider) as AnyModel;
			// the provider's own streamSimple (as pi does for the session); else the registry stream
			const streamFn = provider?.streamSimple
				? (mm: AnyModel, c: AnyModel, o: AnyModel) => provider.streamSimple(mm, c, o)
				: (mm: AnyModel, c: AnyModel, o: AnyModel) => {
					const { reasoning: _r, ...rest } = o ?? {};
					const p = reg.complete(mm, c, rest);
					return { result: () => p };
				};
			const thinking = model === sessionModel ? ctx.thinkingLevel : undefined;
			// a null header value means "delete this default header" (pi drops them the same way)
			const headers = auth.headers ? Object.fromEntries(Object.entries(auth.headers).filter((kv): kv is [string, string] => typeof kv[1] === "string")) : undefined;
			const r = await compact(prep as AnyModel, m, auth.apiKey, headers, instr, signal, thinking as AnyModel, streamFn as AnyModel, auth.env);
			return { text: r.summary, usage: r.usage as AnyModel, details: r.details as AnyModel };
		};
		const ref = (m: AnyModel): ModelRef | undefined => (m ? { provider: m.provider, id: m.id, contextWindow: m.contextWindow, _m: m } as ModelRef : undefined);
		const real = (m: ModelRef): AnyModel => (m as AnyModel)._m;
		let out;
		try {
			out = await runCompaction(
				{ preparation: event.preparation as unknown as PrepLike, branchEntries: event.branchEntries as unknown as EntryLike[], customInstructions: event.customInstructions, reason: event.reason, signal: event.signal },
				{
					model: ref(sessionModel),
					env: process.env,
					now: Date.now,
					defaultSummary: (m, prep, instr, signal) => summarize(real(m), prep, instr, signal),
					findModel: spec => {
						const k = spec.indexOf("/");
						return k > 0 ? ref(reg.find(spec.slice(0, k), spec.slice(k + 1))) : undefined;
					},
					captured: () => captured,
					replay: async (m, payload, maxTokens, signal): Promise<Reply> =>
						(await reg.complete(real(m), { systemPrompt: "", messages: [{ role: "user", content: [{ type: "text", text: "." }], timestamp: Date.now() }] }, {
							maxTokens, signal, cacheRetention: "short", sessionId: ctx.sessionManager.getSessionId(),
							onPayload: () => payload,
						} as AnyModel)) as unknown as Reply,
					toMessage: e => sessionEntryToContextMessages(e as unknown as SessionEntry)[0],
					tokens: m => estimateTokens(m as AnyModel),
				},
			);
		} catch {
			return; // pi default
		}
		appendLog(COMPACT_LOG, out.row, Date.now());
		if (!out.compaction) return;
		if (ctx.hasUI) {
			const r = out.row as { path?: string; missed?: { rule: number; id: number } };
			ctx.ui.notify(`apex-compact: ${r.path}; pinned ledger added (summary missed ${r.missed?.rule ?? 0} rule(s), ${r.missed?.id ?? 0} id(s))`, "info");
		}
		return { compaction: out.compaction as AnyModel };
	});
}
