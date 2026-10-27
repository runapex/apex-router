/**
 * Compaction runner (P1 P2 P3 P4 P5 P6). Orchestrates ONE session_before_compact event. All pi calls come in
 * through `deps`, so the tests run it with fakes. apex-compact.ts gives the real pi functions.
 *
 * Order of the summary text (first that works wins; each failure is a reason code in telemetry):
 *   P3 replay   — APEX_COMPACT_CACHE=1: the last captured provider payload + one instruction (cache prefix).
 *                 Only when the capture's branch leaf id is the leaf at compaction (the last branch entry):
 *                 a capture of another branch, or one with a user message after it, is not used (p3-branch-changed). The /compact
 *                 custom instructions are appended to the replay instruction (as pi does: "Additional focus").
 *   P5 model    — APEX_COMPACT_MODEL=<provider/model>: pi compact() with that model.
 *   P4 budget   — pi compact() with the session model and the summary budget (APEX_COMPACT_SUMMARY_TOKENS).
 *   no budget   — pi compact() with the session model and the user's reserveTokens (a length stop at P4).
 *   pi default  — return undefined: pi compacts as before (no ledger in this compaction; the next one finds the
 *                 ledger of the compaction before it).
 * The ledger (P1) and the gate (P2) are added to each summary that this runner makes.
 */
import {
	budgetInstruction, buildReplayPayload, capLedger, DEFAULT_LEDGER_TOKENS, DEFAULT_SUMMARY_TOKENS, envInt, fileLists,
	gate, ledgerTokens, mergeFileOps, moveCutEarlier, readLedger, replayInstruction, replyText, reserveForBudget,
	stripPinned, updateLedger, type EntryLike, type Kind, type Ledger, type PrepLike,
} from "./compact.ts";

export type Usage = { input?: number; output?: number; cacheRead?: number; cacheWrite?: number; cost?: { total?: number } };
export type Reply = { content?: unknown; stopReason?: string; usage?: Usage; errorMessage?: string };
export type ModelRef = { provider: string; id: string; contextWindow?: number };
export type Summary = { text: string; usage?: Usage; details?: { readFiles?: string[]; modifiedFiles?: string[] } };
/** leafId: the session leaf (getLeafId) when the payload was sent. The extension clears the capture on
 *  session_start, session_tree and model_select. */
export type Captured = { payload: unknown; provider: string; id: string; at: number; leafId?: string | null };

export type CompactEventLike = {
	preparation: PrepLike;
	branchEntries: EntryLike[];
	customInstructions?: string;
	reason?: string;
	signal?: AbortSignal;
};

export type Deps = {
	model?: ModelRef;
	env: Record<string, string | undefined>;
	now: () => number;
	/** pi compact() for `model` with `prep` (it appends file lists). Throws on any failure. */
	defaultSummary: (model: ModelRef, prep: PrepLike, instructions: string | undefined, signal?: AbortSignal) => Promise<Summary>;
	/** P5: resolve "provider/model" (undefined = not found). */
	findModel?: (spec: string) => ModelRef | undefined;
	/** P3: the last captured payload of the session model. */
	captured?: () => Captured | undefined;
	/** P3: send `payload` unchanged for `model` (ctx.modelRegistry.complete with onPayload). */
	replay?: (model: ModelRef, payload: Record<string, unknown>, maxTokens: number, signal?: AbortSignal) => Promise<Reply>;
	/** P6: entry -> context message, and its token estimate. */
	toMessage?: (e: EntryLike) => unknown | undefined;
	tokens?: (m: unknown) => number;
};

export type CompactOut = {
	compaction?: { summary: string; firstKeptEntryId: string; tokensBefore: number; usage?: Usage; details: Record<string, unknown> };
	row: Record<string, unknown>;
};

const n = (x: unknown): number | null => (typeof x === "number" && Number.isFinite(x) ? x : null);

/** The newest compaction on the branch that carries a ledger, and the newest compaction at all. */
export function previousCompactions(entries: EntryLike[]): { last?: EntryLike; ledger: Ledger | null } {
	let last: EntryLike | undefined;
	for (let k = entries.length - 1; k >= 0; k--) {
		const e = entries[k]!;
		if (e.type !== "compaction") continue;
		last ??= e;
		const l = readLedger(e.details);
		if (l) return { last, ledger: l };
	}
	return { last, ledger: null };
}

export async function runCompaction(ev: CompactEventLike, deps: Deps): Promise<CompactOut> {
	const env = deps.env;
	const t0 = deps.now();
	const prep0 = ev.preparation;
	const row: Record<string, unknown> = { ts: t0, v: 1, reason: ev.reason ?? null, split: !!prep0.isSplitTurn, tokens_before: prep0.tokensBefore };
	if (env.APEX_COMPACT === "0") return { row: { ...row, path: "off" } };

	// P1 ledger: carried from the newest compaction with a ledger, then the new entries
	const prev = previousCompactions(ev.branchEntries);
	const ledger = updateLedger(prev.ledger, ev.branchEntries);
	const capTokens = envInt(env, "APEX_COMPACT_LEDGER_TOKENS", DEFAULT_LEDGER_TOKENS, 200)!;
	row.ledger_dropped = capLedger(ledger, capTokens);
	row.ledger_tokens = ledgerTokens(ledger);
	row.carried = !!prev.ledger;

	// P6: keep more recent tokens (opt-in)
	let prep: PrepLike = prep0;
	let firstKeptEntryId = prep0.firstKeptEntryId;
	const keep = envInt(env, "APEX_COMPACT_KEEP_TOKENS", undefined, 1000);
	if (keep !== undefined && deps.toMessage && deps.tokens) {
		const window = deps.model?.contextWindow ?? 0;
		const maxKeep = window > 0 ? Math.floor((window - prep0.settings.reserveTokens) / 2) : Number.POSITIVE_INFINITY;
		const moved = keep > prep0.settings.keepRecentTokens ? moveCutEarlier(ev.branchEntries, prep0, keep, deps.toMessage, deps.tokens, maxKeep) : null;
		if (moved) {
			prep = moved.prep;
			firstKeptEntryId = moved.firstKeptEntryId;
		}
		row.p6 = moved ? "moved" : "kept";
	}

	// the earlier pinned block is not fed back to the model; earlier file lists stay cumulative
	prep = { ...prep, previousSummary: stripPinned(prep.previousSummary), fileOps: mergeFileOps(prep.fileOps, prev.last?.details) };

	// P4 budget
	const budget = envInt(env, "APEX_COMPACT_SUMMARY_TOKENS", DEFAULT_SUMMARY_TOKENS, 500)!;
	row.budget = budget;
	const instr = [ev.customInstructions, budgetInstruction(budget)].filter(Boolean).join("\n\n");
	const budgeted = (p: PrepLike): PrepLike => ({ ...p, settings: { ...p.settings, reserveTokens: reserveForBudget(budget) } });
	const fails: string[] = [];
	const aborted = () => !!ev.signal?.aborted;
	let out: Summary | undefined;
	let path = "pi-default";

	// P3: replay the cached prefix (opt-in)
	if (env.APEX_COMPACT_CACHE === "1") {
		row.p3 = "skip";
		const cap = deps.captured?.();
		const maxAge = envInt(env, "APEX_COMPACT_CACHE_MAX_AGE_S", 300, 1)! * 1000;
		// The capture must be on THIS branch, and only the reply to it (assistant + tool results) may follow:
		// a new user message, or a leaf of another branch, means the payload does not cover the context.
		const at = cap?.leafId ? ev.branchEntries.findIndex(e => e.id === cap.leafId) : -1;
		const after = at >= 0 ? ev.branchEntries.slice(at + 1) : [];
		const onBranch = at >= 0 && !after.some(e => (e as { message?: { role?: string } }).message?.role === "user");
		if (!deps.model || !deps.replay) fails.push("p3-no-deps");
		else if (!cap) fails.push("p3-no-capture");
		else if (cap.provider !== deps.model.provider || cap.id !== deps.model.id) fails.push("p3-model-changed");
		else if (!onBranch) fails.push("p3-branch-changed");
		else if (t0 - cap.at > maxAge) fails.push("p3-stale");
		else {
			const ri = replayInstruction(budget) + (ev.customInstructions ? `\n\nAdditional focus: ${ev.customInstructions}` : "");
			const payload = buildReplayPayload(cap.payload, ri, budget);
			if (!payload) fails.push("p3-shape");
			else {
				try {
					const reply = await deps.replay(deps.model, payload, budget, ev.signal);
					const text = replyText(reply);
					row.p3_input = n(reply.usage?.input);
					row.p3_cache_read = n(reply.usage?.cacheRead);
					row.p3_cache_write = n(reply.usage?.cacheWrite);
					row.p3_stop = reply.stopReason ?? null;
					if (text) {
						const fl = fileLists(prep.fileOps);
						out = { text: text + fl.text, usage: reply.usage, details: { readFiles: fl.readFiles, modifiedFiles: fl.modifiedFiles } };
						path = "p3-replay";
						row.p3 = "ok";
					} else fails.push("p3-bad-reply");
				} catch {
					fails.push("p3-error");
				}
			}
		}
		if (!out) row.p3 = "fallback";
	}

	// P5: another model for the summary text (opt-in)
	const spec = env.APEX_COMPACT_MODEL?.trim();
	if (!out && spec && !aborted()) {
		const m = deps.findModel?.(spec);
		if (!m) fails.push("p5-no-model");
		else {
			try {
				out = await deps.defaultSummary(m, budgeted(prep), instr, ev.signal);
				path = "p5-model";
				row.p5_model = `${m.provider}/${m.id}`;
			} catch {
				fails.push("p5-error");
			}
		}
	}

	// P4 with the session model, then once without the budget
	if (!out && deps.model && !aborted()) {
		try {
			out = await deps.defaultSummary(deps.model, budgeted(prep), instr, ev.signal);
			path = "budget";
		} catch {
			fails.push("budget-error");
			if (!aborted()) {
				try {
					out = await deps.defaultSummary(deps.model, prep, ev.customInstructions, ev.signal);
					path = "no-budget";
				} catch {
					fails.push("no-budget-error");
				}
			}
		}
	}
	row.path = out ? path : "pi-default";
	row.fails = fails;
	if (!out) return { row: { ...row, ms: deps.now() - t0 } };

	// P2 gate + P1 render
	const g = gate(out.text, ledger);
	const k = (o: Record<Kind, number>) => ({ rule: o.rule, id: o.id, nogo: o.nogo, error: o.error });
	Object.assign(row, {
		items: k(g.counts), missed: k(g.missed), appended: k(g.appended),
		summary_chars: out.text.length, final_chars: g.text.length,
		usage_input: n(out.usage?.input), usage_output: n(out.usage?.output), usage_cache_read: n(out.usage?.cacheRead),
		cost: n(out.usage?.cost?.total), p6_first_kept_moved: firstKeptEntryId !== prep0.firstKeptEntryId, ms: deps.now() - t0,
	});
	return {
		compaction: {
			summary: g.text,
			firstKeptEntryId,
			tokensBefore: prep0.tokensBefore,
			usage: out.usage,
			details: { readFiles: out.details?.readFiles ?? [], modifiedFiles: out.details?.modifiedFiles ?? [], apexLedger: ledger, apexPath: path },
		},
		row,
	};
}
