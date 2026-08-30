/**
 * learn — one-command study pipeline on top of booksearch + apex-router.
 *
 *   /learn linked lists
 *
 * Runs three stages in your live session, then restores your original model:
 *   0. RETRIEVE (local): booksearch Top-5 references from ~/books (nomic-embed).
 *   1. VALIDATE (sonnet): a frontier model vets the sources and emits a STRUCTURED verdict
 *      (JSON: authoritative/weak/sections/focus_questions) — not prose.
 *   2. EXPLAIN (opus): prompted as (P, Σ) per SKILL.state (arXiv:2608.26263): the immutable
 *      spec (topic + sources) restated, plus the parsed verdict JSON as the operative state —
 *      not "the validated sources above" scrollback. The structured handoff keeps sonnet's
 *      prose (hedges, weak-source discussion) out of opus's operative context and makes the
 *      inter-model contract auditable. Fail-open: if the verdict can't be parsed, stage 2
 *      falls back to the legacy transcript reference.
 *
 * Each stage runs as a normal turn (you see the output), and your starting model
 * (e.g. the local Ornith tier) is restored at the end.
 *
 * Models resolve from the SHARED model registry `~/.apex-router/models.json` (the
 * `learn` section: validate_tier/explain_tier through `tiers`) — the same file every
 * apex-router component reads, so a tier bump moves /learn too. Env still wins:
 *   LEARN_VALIDATE_MODEL   default: registry sonnet tier
 *   LEARN_EXPLAIN_MODEL    default: registry opus tier
 *   BOOKSEARCH_BIN         default ~/.local/bin/booksearch
 *
 * Requires: booksearch indexed (`booksearch ingest`), and the anthropic provider
 * wired through the apex proxy (integrations/pi/models.json).
 *
 * Install:  pi install ~/.apex-router/integrations/pi/learn.ts
 */

import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const execFileP = promisify(execFile);
const BIN = process.env.BOOKSEARCH_BIN || join(homedir(), ".local", "bin", "booksearch");

// Registry-driven defaults (fall back to the built-ins on any read failure).
function learnModels(): { provider: string; validate: string; explain: string } {
	const fallback = { provider: "anthropic", validate: "claude-sonnet-5", explain: "claude-opus-4-8" };
	try {
		const reg = JSON.parse(readFileSync(join(homedir(), ".apex-router", "models.json"), "utf8"));
		const tiers = reg?.tiers || {};
		const spec = reg?.learn || {};
		return {
			provider: typeof spec.provider === "string" ? spec.provider : fallback.provider,
			validate: tiers[spec.validate_tier || "sonnet"] || fallback.validate,
			explain: tiers[spec.explain_tier || "opus"] || fallback.explain,
		};
	} catch {
		return fallback;
	}
}
const LEARN = learnModels();
const VALIDATE_MODEL = process.env.LEARN_VALIDATE_MODEL || LEARN.validate;
const EXPLAIN_MODEL = process.env.LEARN_EXPLAIN_MODEL || LEARN.explain;
const PROVIDER = process.env.LEARN_PROVIDER || LEARN.provider;

// --- SKILL.state slot contract: VALIDATE emits Σ, EXPLAIN consumes (P, Σ) ------------------

interface Verdict {
	authoritative?: string[];
	weak?: string[];
	sections?: string[];
	focus_questions?: string[];
}

const VERDICT_CONTRACT =
	`End your reply with a \`\`\`json block containing ONLY this verdict object (no other keys):\n` +
	`{"authoritative": ["title — why it counts"], "weak": ["title — why it's weak"], ` +
	`"sections": ["title: pages/sections to read"], "focus_questions": ["what the explanation must answer"]}`;

/** Extract the last assistant message's text from the live branch (post-waitForIdle). */
export function lastAssistantText(ctx: { sessionManager: { getBranch(): unknown[] } }): string {
	const branch = ctx.sessionManager.getBranch();
	for (let i = branch.length - 1; i >= 0; i--) {
		const e = branch[i] as { type?: string; message?: { role?: string; content?: unknown } };
		if (e?.type !== "message" || e.message?.role !== "assistant") continue;
		const content = e.message.content;
		if (!Array.isArray(content)) return "";
		return content
			.filter((b): b is { type: string; text: string } =>
				typeof b === "object" && b !== null && (b as { type?: string }).type === "text")
			.map((b) => b.text)
			.join("\n");
	}
	return "";
}

/**
 * Parse the verdict's ```json block (or a bare JSON object). Fail-open: null on any failure —
 * AND on any CONTRACT violation: `authoritative` must be a non-empty string array (an empty
 * one would tell EXPLAIN "use ONLY these sources: none" — fail-closed source starvation), and
 * every other key present must be a string array. A malformed verdict takes the legacy
 * transcript fallback instead of poisoning stage 2.
 */
export function parseVerdict(text: string): Verdict | null {
	const fenced = text.match(/```json\s*\n([\s\S]*?)```/);
	const lo = text.indexOf("{");
	const hi = text.lastIndexOf("}");
	const candidates = [fenced?.[1], lo >= 0 && hi > lo ? text.slice(lo, hi + 1) : undefined];
	const KEYS: (keyof Verdict)[] = ["authoritative", "weak", "sections", "focus_questions"];
	const isStrArr = (v: unknown): v is string[] =>
		Array.isArray(v) && v.every((x) => typeof x === "string");
	for (const c of candidates) {
		if (!c) continue;
		try {
			const o = JSON.parse(c);
			if (!o || typeof o !== "object" || Array.isArray(o)) continue;
			const keys = Object.keys(o);
			if (keys.some((k) => !(KEYS as string[]).includes(k))) continue; // unknown keys
			if (keys.some((k) => !isStrArr((o as Record<string, unknown>)[k]))) continue; // types
			if (!isStrArr((o as Verdict).authoritative) || (o as Verdict).authoritative!.length === 0)
				continue; // the starvation guard
			return o as Verdict;
		} catch {
			/* try next candidate */
		}
	}
	return null;
}

export default function (pi: ExtensionAPI) {
	pi.registerCommand("learn", {
		description: "Study pipeline: booksearch Top-5 -> sonnet validate -> opus explain (usage: /learn <topic>)",
		handler: async (args, ctx) => {
			const topic = args.trim();
			if (!topic) {
				ctx.ui.notify("usage: /learn <topic>", "warning");
				return;
			}

			const validate = ctx.modelRegistry.find(PROVIDER, VALIDATE_MODEL);
			const explain = ctx.modelRegistry.find(PROVIDER, EXPLAIN_MODEL);
			if (!validate || !explain) {
				ctx.ui.notify(
					`learn: need ${PROVIDER}/${VALIDATE_MODEL} and ${PROVIDER}/${EXPLAIN_MODEL} in models.json`,
					"error",
				);
				return;
			}
			const original = ctx.model;

			// Stage 0 — local retrieval (no frontier tokens spent here).
			ctx.ui.setStatus("learn", "📚 retrieving…");
			let sources: string;
			try {
				const { stdout } = await execFileP(BIN, ["query", topic, "-k", "5", "--no-explain"], {
					signal: ctx.signal,
					maxBuffer: 4 * 1024 * 1024,
				});
				sources = stdout.trim();
			} catch (err) {
				ctx.ui.notify(`booksearch failed (did you run \`booksearch ingest\`?): ${(err as Error).message}`, "error");
				ctx.ui.setStatus("learn", "");
				return;
			}
			if (!sources) {
				ctx.ui.notify("learn: no local references found for that topic", "warning");
				ctx.ui.setStatus("learn", "");
				return;
			}

			try {
				// Stage 1 — validate with the frontier model.
				ctx.ui.setStatus("learn", "🔍 validating (sonnet)…");
				if (!(await pi.setModel(validate))) {
					ctx.ui.notify(`learn: no API key for ${PROVIDER}/${VALIDATE_MODEL}`, "error");
					return;
				}
				await pi.sendUserMessage(
					`I'm studying **${topic}**. booksearch returned these Top-5 local references ` +
						`(semantic match over my own library):\n\n${sources}\n\n` +
						`Validate them: which are authoritative and on-topic for learning ${topic}, flag any weak or ` +
						`irrelevant ones, and list the specific sections/pages/notebooks worth reading. Be concise.\n\n` +
						VERDICT_CONTRACT,
				);
				await ctx.waitForIdle();

				// The structured handoff: parse sonnet's verdict into Σ. Fail-open to legacy behavior.
				const verdict = parseVerdict(lastAssistantText(ctx));

				// Stage 2 — comprehensive explanation with the deep model, correlated to the user's code.
				ctx.ui.setStatus("learn", "✍️ explaining (opus)…");
				if (!(await pi.setModel(explain))) {
					ctx.ui.notify(`learn: no API key for ${PROVIDER}/${EXPLAIN_MODEL}`, "error");
					return;
				}
				const explainPrompt = verdict
					? // (P, Σ): immutable spec restated + verdict state as the operative handoff.
						`I'm studying **${topic}**. The candidate local references (P):\n\n${sources}\n\n` +
						`The sources were already validated; the verdict (Σ) is:\n\`\`\`json\n${JSON.stringify(verdict, null, 2)}\n\`\`\`\n\n` +
						`Using ONLY the sources the verdict marks authoritative, write a comprehensive explanation ` +
						`of **${topic}** covering the verdict's focus_questions and reading its listed sections, and ` +
						`correlate it to MY current work: read the relevant files in this session/project and tie the ` +
						`concepts to my actual code (name the files/functions). Include worked reasoning and concrete ` +
						`next steps, and cite the sources by title + location (page or line range).`
					: // legacy transcript reference (parse failed — fail open, never break /learn)
						`Using the validated sources above, write a comprehensive explanation of **${topic}**, and ` +
						`correlate it to MY current work: read the relevant files in this session/project and tie the ` +
						`concepts to my actual code (name the files/functions). Include worked reasoning and concrete ` +
						`next steps, and cite the sources by title + location (page or line range).`;
				await pi.sendUserMessage(explainPrompt);
				await ctx.waitForIdle();
			} finally {
				if (original) await pi.setModel(original); // hand the session back to your starting model
				ctx.ui.setStatus("learn", "");
			}
			ctx.ui.notify(`/learn ${topic}: done${original ? ` — restored ${original.id}` : ""}`, "info");
		},
	});
}
