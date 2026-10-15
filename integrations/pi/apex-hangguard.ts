/**
 * apex-hangguard — stops the bash hangs measured in ~/.pi/agent/sessions (DESIGN-pi-subagents.md P1).
 * Default timeout, `< /dev/null` for agent CLIs, BatchMode for ssh/scp, and no sleep-polling while an apex
 * sub-agent job runs. Each change is logged to ~/.apex-router/pi-hangguard.jsonl. Opt out per call with
 * `# apex-hangguard: off`. Off entirely: APEX_HANGGUARD=0.
 */
import { appendFileSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { isToolCallEventType, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { JobManager } from "./lib/agents.ts";
import { decide } from "./lib/hangguard.ts";

const LOG = join(homedir(), ".apex-router", "pi-hangguard.jsonl");

export default function (pi: ExtensionAPI) {
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
		const before = { command: event.input.command, timeout: event.input.timeout };
		const d = decide(event.input.command, event.input.timeout, { jobsRunning: running });
		if (d.changes.length) {
			try {
				mkdirSync(join(homedir(), ".apex-router"), { recursive: true });
				appendFileSync(LOG, JSON.stringify({ ts: Date.now(), changes: d.changes, before, after: { command: d.command, timeout: d.timeout }, blocked: !!d.block }) + "\n");
			} catch {
				/* logging never blocks a call */
			}
		}
		if (d.block) return { block: true, reason: d.reason };
		event.input.command = d.command;
		if (d.timeout !== undefined) event.input.timeout = d.timeout;
	});
}
