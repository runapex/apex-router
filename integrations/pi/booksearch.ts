/**
 * booksearch — Top-K local-book references for the problem you're working on,
 * injected into the pi conversation. 100% local: nomic-embed for retrieval and
 * the Ornith tier for the "why this book" explanation (see scripts/booksearch.py).
 *
 *   /books deriving the chain rule for multivariable functions
 *   /books how do B-trees stay balanced?
 *
 * The command shells out to the `booksearch` CLI, then injects the ranked
 * references as a user message so the agent can cite them while it works.
 *
 * Prereqs: run `booksearch ingest` once (indexes ~/books), and have the wrapper
 * on PATH (installer --pi-integration wires it, or ~/.local/bin/booksearch).
 *
 * Install:  pi install ~/.apex-router/integrations/pi/booksearch.ts
 */

import { execFile } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const execFileP = promisify(execFile);
const BIN = process.env.BOOKSEARCH_BIN || join(homedir(), ".local", "bin", "booksearch");

export default function (pi: ExtensionAPI) {
	pi.registerCommand("books", {
		description: "Top-K local-book references for a problem (usage: /books <problem>)",
		handler: async (args, ctx) => {
			const problem = args.trim();
			if (!problem) {
				ctx.ui.notify("usage: /books <problem or question>", "warning");
				return;
			}
			ctx.ui.setStatus("books", "📚 searching…");
			try {
				const { stdout } = await execFileP(BIN, ["query", problem, "-k", "5", "--json"], {
					signal: ctx.signal,
					maxBuffer: 4 * 1024 * 1024,
				});
				const data = JSON.parse(stdout) as {
					results: { title: string; page_start: number; page_end: number; score: number; why?: string }[];
				};
				if (!data.results?.length) {
					ctx.ui.notify("booksearch: no references found (did you run `booksearch ingest`?)", "warning");
					return;
				}
				const lines = data.results.map((r, i) => {
					const loc = r.page_end !== r.page_start ? `pp.${r.page_start}–${r.page_end}` : `p.${r.page_start}`;
					const why = r.why ? ` — ${r.why}` : "";
					return `${i + 1}. ${r.title} (${loc}, score ${r.score.toFixed(3)})${why}`;
				});
				await ctx.sendUserMessage(
					`Top local-book references for: "${problem}"\n\n${lines.join("\n")}\n\n` +
						`These are from my local library (semantic match + local-model reasoning). ` +
						`Use them as sources where relevant.`,
				);
			} catch (err) {
				ctx.ui.notify(`booksearch failed: ${(err as Error).message}`, "error");
			} finally {
				ctx.ui.setStatus("books", "");
			}
		},
	});
}
