/**
 * learn — one-command study pipeline on top of booksearch + apex-router.
 *
 *   /learn linked lists
 *
 * Runs three stages in your live session, then restores your original model:
 *   0. RETRIEVE (local): booksearch Top-5 references from ~/books (nomic-embed).
 *   1. VALIDATE (sonnet): a frontier model vets the sources — authoritative? on-topic?
 *      which sections to read? — so you don't study from a weak match.
 *   2. EXPLAIN (opus): a deep model writes a comprehensive explanation and correlates
 *      it to YOUR current code/files (it can read them), citing the validated sources.
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
						`irrelevant ones, and list the specific sections/pages/notebooks worth reading. Be concise.`,
				);
				await ctx.waitForIdle();

				// Stage 2 — comprehensive explanation with the deep model, correlated to the user's code.
				ctx.ui.setStatus("learn", "✍️ explaining (opus)…");
				if (!(await pi.setModel(explain))) {
					ctx.ui.notify(`learn: no API key for ${PROVIDER}/${EXPLAIN_MODEL}`, "error");
					return;
				}
				await pi.sendUserMessage(
					`Using the validated sources above, write a comprehensive explanation of **${topic}**, and ` +
						`correlate it to MY current work: read the relevant files in this session/project and tie the ` +
						`concepts to my actual code (name the files/functions). Include worked reasoning and concrete ` +
						`next steps, and cite the sources by title + location (page or line range).`,
				);
				await ctx.waitForIdle();
			} finally {
				if (original) await pi.setModel(original); // hand the session back to your starting model
				ctx.ui.setStatus("learn", "");
			}
			ctx.ui.notify(`/learn ${topic}: done${original ? ` — restored ${original.id}` : ""}`, "info");
		},
	});
}
