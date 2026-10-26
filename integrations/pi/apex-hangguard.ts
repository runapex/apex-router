/**
 * apex-hangguard — stops the bash hangs measured in ~/.pi/agent/sessions (DESIGN-pi-subagents.md P1).
 * Default timeout, `< /dev/null` for agent CLIs, BatchMode for ssh/scp, and no sleep-polling while an apex
 * sub-agent job runs. Each change is logged to ~/.apex-router/pi-hangguard/YYYY-MM-DD-<pid>.jsonl (change
 * names and timeouts only, no command text; mode 0600; files older than 14 days are deleted). The old single
 * file ~/.apex-router/pi-hangguard.jsonl (it had command text) is set to mode 0600 at load. Opt out per call
 * with `# apex-hangguard: off`. Off entirely: APEX_HANGGUARD=0.
 */
import { chmodSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { isToolCallEventType, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { JobManager } from "./lib/agents.ts";
import { appendLog, decide, logRow, pruneLogs } from "./lib/hangguard.ts";

const LOG_DIR = join(homedir(), ".apex-router", "pi-hangguard");
const LEGACY_LOG = join(homedir(), ".apex-router", "pi-hangguard.jsonl");

export default function (pi: ExtensionAPI) {
	try {
		chmodSync(LEGACY_LOG, 0o600); // the old file held command text; it is never written again
	} catch {
		/* not there */
	}
	pruneLogs(LOG_DIR, Date.now());
	let jobs: JobManager | null = null;
	pi.on("tool_call", async event => {
		if (process.env.APEX_HANGGUARD === "0" || !isToolCallEventType("bash", event)) return;
		jobs ??= new JobManager();
		let running = 0;
		try {
			running = jobs.live().length;
		} catch {
			running = 0;
		}
		const before = { timeout: event.input.timeout };
		const d = decide(event.input.command, event.input.timeout, { jobsRunning: running });
		if (d.changes.length) {
			const now = Date.now();
			appendLog(LOG_DIR, logRow(now, d, before), now);
		}
		if (d.block) return { block: true, reason: d.reason };
		event.input.command = d.command;
		if (d.timeout !== undefined) event.input.timeout = d.timeout;
	});
}
