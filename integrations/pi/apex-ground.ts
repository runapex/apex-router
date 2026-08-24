/**
 * apex-ground — automatic citation grounding for pi (E1: discipline → mechanism).
 *
 * After EVERY assistant message that cites code (`file:line`), run the codeqa grounding
 * oracle — a DETERMINISTIC filesystem check (no model, can't hallucinate): does the cited
 * file exist in a registered repo, and is the cited span within the live file?
 *
 *   - STALE citation  -> warning notification naming the broken citation(s). A stale cite
 *     means the finding references a line that isn't there — factually broken regardless
 *     of how plausible the prose reads.
 *   - grounded/all-ok -> silent (a quiet pass is the common case; don't nag).
 *   - nothing to ground (no citations / no registered repos) -> silent self-skip.
 *
 * Config (env):
 *   CODEQA_REPOS        repo registry dir (default ~/.apex/codeqa-repos)
 *   APEX_GROUND_PYTHON  python with apex_router importable
 *                       (default ~/.local/share/uv/tools/apex-router/bin/python)
 *
 * Install:  pi install ~/.apex-router/integrations/pi/apex-ground.ts
 */

import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const PY_BIN = process.env.APEX_GROUND_PYTHON
	|| join(homedir(), ".local", "share", "uv", "tools", "apex-router", "bin", "python");
const REPOS = process.env.CODEQA_REPOS || join(homedir(), ".apex", "codeqa-repos");

// Same shape as the oracle's citation regex: path with alpha-initial extension, then :N.
const CITE_RE = /[\w./-]+\.[A-Za-z]\w*:\d+/;

function textOf(message: any): string {
	const parts = (message?.content ?? [])
		.filter((b: any) => b?.type === "text" && typeof b.text === "string")
		.map((b: any) => b.text);
	return parts.join("\n");
}

export default function (pi: ExtensionAPI) {
	pi.on("message_end", async (event, ctx) => {
		const msg: any = event.message;
		if (msg?.role !== "assistant") return;
		const text = textOf(msg);
		if (!text || !CITE_RE.test(text)) return; // nothing groundable — self-skip
		if (!existsSync(REPOS)) return;

		execFile(
			PY_BIN,
			["-m", "apex_router.codeqa.cli", "ground"],
			{ env: { ...process.env, CODEQA_REPOS: REPOS }, timeout: 30_000, maxBuffer: 1024 * 1024 },
			(err, stdout) => {
				if (err || !stdout) return; // oracle failure stays silent (advisory only)
				const out = String(stdout);
				if (!out.includes("STALE")) return;
				const summary = out.split("\n")[0];
				const stale = out.split("\n").filter((l) => /stale/i.test(l)).slice(0, 5).join("\n");
				ctx.ui.notify(
					`⚠ ${summary}\n${stale}\nStale citations point at lines that don't exist — verify before trusting.`,
					"warning",
				);
			},
		);
	});
}
