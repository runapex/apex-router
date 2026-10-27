// Fake pi session entries for the apex-compact tests. Built by hand; no real transcript text.
import type { EntryLike, PrepLike } from "../../lib/compact.ts";

export class FakeSession {
	entries: (EntryLike & { parentId?: string | null; firstKeptEntryId?: string; summary?: string })[] = [];
	private n = 0;
	private call = 0;
	private nextId(p: string): string {
		return `${p}${String(++this.n).padStart(3, "0")}`;
	}
	user(text: string): string {
		const id = this.nextId("u");
		this.entries.push({ type: "message", id, message: { role: "user", content: [{ type: "text", text }] } });
		return id;
	}
	assistant(text: string): string {
		const id = this.nextId("a");
		this.entries.push({ type: "message", id, message: { role: "assistant", content: [{ type: "text", text }] } });
		return id;
	}
	/** assistant tool call + its result. Returns [callEntryId, resultEntryId]. */
	tool(name: string, args: Record<string, unknown>, result: string, isError = false): [string, string] {
		const tc = `call_${++this.call}`;
		const a = this.nextId("a");
		this.entries.push({ type: "message", id: a, message: { role: "assistant", content: [{ type: "toolCall", id: tc, name, arguments: args }] } });
		const r = this.nextId("t");
		this.entries.push({ type: "message", id: r, message: { role: "toolResult", toolCallId: tc, toolName: name, isError, content: [{ type: "text", text: result }] } });
		return [a, r];
	}
	compaction(summary: string, firstKeptEntryId: string, details: unknown): string {
		const id = this.nextId("c");
		this.entries.push({ type: "compaction", id, summary, firstKeptEntryId, details });
		return id;
	}
	/** A preparation like pi prepareCompaction(): cut at `firstKeptEntryId`. */
	prep(firstKeptEntryId: string, previousSummary?: string): PrepLike {
		const k = this.entries.findIndex(e => e.id === firstKeptEntryId);
		let lo = 0;
		for (let i = this.entries.length - 1; i >= 0; i--) {
			const e = this.entries[i]!;
			if (e.type === "compaction") {
				const b = this.entries.findIndex(x => x.id === e.firstKeptEntryId);
				lo = b >= 0 ? b : i + 1;
				break;
			}
		}
		return {
			firstKeptEntryId,
			messagesToSummarize: this.entries.slice(lo, k).filter(e => e.type === "message").map(e => e.message),
			turnPrefixMessages: [],
			isSplitTurn: false,
			tokensBefore: 150_000,
			previousSummary,
			fileOps: { read: new Set(["src/a.ts"]), written: new Set(), edited: new Set(["src/b.ts"]) },
			settings: { enabled: true, reserveTokens: 49152, keepRecentTokens: 20000 },
		};
	}
}

/** Token estimate like pi estimateTokens (chars / 4). */
export function fakeTokens(m: unknown): number {
	const c = (m as { content?: { text?: string; arguments?: unknown }[] }).content ?? [];
	let chars = 0;
	for (const b of c) chars += (b.text?.length ?? 0) + (b.arguments ? JSON.stringify(b.arguments).length : 0);
	return Math.ceil(chars / 4);
}

export const fakeToMessage = (e: EntryLike): unknown | undefined => (e.type === "message" ? e.message : undefined);
