/**
 * apex-agents — sub-agents for pi (DESIGN-pi-subagents.md P2/P3).
 *
 * Tools: agent_spawn, agent_status, agent_wait, agent_cancel, agent_result.
 * A job is a separate `pi --mode json -p` process (or `apex-router xval` for the xval agent) with its own context.
 * agent_spawn returns at once. The parent gets a message when the job ends, so it never polls with sleep.
 * Each job ends in a known state: done, failed, killed, timed_out(idle), timed_out(wall), killed(cost).
 * Briefs and the child system prompt use ASD-STE100 (apex-router default format).
 *
 * Env: APEX_AGENT_CAP (4), APEX_AGENT_MAX_DEPTH (1: children do not start children), APEX_AGENTS_KILL_ON_EXIT=1.
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve as resolvePath } from "node:path";
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

import { loadAgents, THINKING, type AgentDef } from "./lib/agentdefs.ts";
import { FINAL, JobManager, type Meta } from "./lib/agents.ts";
import { briefText, rulesText, steEnabled } from "./lib/ste.ts";

const MAX_WAIT_S = 600;
const depth = () => Number(process.env.APEX_AGENT_DEPTH ?? "0");
const maxDepth = () => Number(process.env.APEX_AGENT_MAX_DEPTH ?? "1");

const text = (t: string, details: Record<string, unknown> = {}) => ({ content: [{ type: "text" as const, text: t }], details });

function line(m: Meta): string {
	const secs = Math.round(((m.ended ?? Date.now()) - (m.started ?? m.created)) / 1000);
	const age = m.last_event ? Math.round((Date.now() - m.last_event) / 1000) : null;
	return `${m.id} ${m.agent} ${m.state}${m.reason ? ` (${m.reason})` : ""} · ${secs}s · ${m.usage.turns} turns · $${m.usage.cost.toFixed(3)}`
		+ (m.state === "running" && age !== null ? ` · last event ${age}s ago` : "")
		+ (m.state === "running" && m.last_tool ? ` · now: ${m.last_tool.slice(0, 80)}` : "");
}

function systemPrompt(def: AgentDef): string {
	return [def.prompt, steEnabled() ? rulesText() : "", "Give your final answer as the last message. Keep it short. The parent agent reads only the final message."]
		.filter(Boolean).join("\n\n");
}

function worktree(cwd: string, id: string): string {
	const dir = join(homedir(), ".apex-router", "pi-jobs-wt", id);
	mkdirSync(join(homedir(), ".apex-router", "pi-jobs-wt"), { recursive: true });
	execFileSync("git", ["-C", cwd, "worktree", "add", "-q", "-b", `apexjob/${id}`, dir, "HEAD"], { stdio: "pipe" });
	return dir;
}

export default function (pi: ExtensionAPI) {
	// A sub-agent at the depth limit gets no tools and no manager: it cannot start jobs or take a parent's messages.
	if (depth() >= maxDepth()) return;
	let jobs: JobManager | null = null;
	const waiting = new Map<string, number>(); // job id -> active waits; a waited job gets its result from the wait, not a push
	const holdWait = async <T>(ids: string[], fn: () => Promise<T>): Promise<T> => {
		ids.forEach(id => waiting.set(id, (waiting.get(id) ?? 0) + 1));
		try {
			return await fn();
		} finally {
			ids.forEach(id => (waiting.get(id)! <= 1 ? waiting.delete(id) : waiting.set(id, waiting.get(id)! - 1)));
		}
	};

	const notify = (m: Meta) => {
		if (waiting.has(m.id)) return; // a waited job gets its result from the wait
		const r = jobs?.result(m.id);
		pi.sendMessage(
			{ customType: "apex-agent-done", content: `Sub-agent job ${line(m)}\n\n${(r?.text ?? "").slice(0, 4000)}\n\nFull result: agent_result ${m.id}`, display: true, details: { id: m.id, state: m.state } },
			{ deliverAs: "followUp", triggerTurn: true },
		);
	};

	// The manager notifies only the jobs it owns (it started them, or adopted them after their owner ended).
	const manager = () => (jobs ??= new JobManager({ cap: Number(process.env.APEX_AGENT_CAP ?? "4"), onDone: notify }));

	pi.on("session_start", async () => {
		manager().adopt(); // jobs of a pi process that ended (restart): continue to watch them and deliver their results
	});
	pi.on("session_shutdown", async () => {
		if (!jobs) return;
		if (process.env.APEX_AGENTS_KILL_ON_EXIT === "1") for (const m of jobs.managed()) if (!FINAL.has(m.state)) await jobs.cancel(m.id);
		jobs.stop();
		jobs = null;
	});

	const agentNames = () => loadAgents().map(a => `${a.name}: ${a.description}`).join("\n");

	pi.registerTool({
		name: "agent_spawn",
		label: "Spawn sub-agent",
		description: `Start a sub-agent with its own context. Returns a job id at once. You get a message when the job ends: do not poll with sleep.\nAgents:\n${agentNames()}\nWrite the task as an ASD-STE100 brief:\n${briefText()}`,
		promptSnippet: "Start a sub-agent job (own context, background, deadline, cancel)",
		promptGuidelines: [
			"Use agent_spawn to delegate an independent task. Use agent_wait or the completion message to get the result; do not use bash sleep loops, nohup pi -p, or nohup codex exec.",
			"Use agent_spawn with agent=xval for an independent Codex review instead of running codex exec in bash.",
		],
		parameters: Type.Object({
			task: Type.String({ description: "The brief, in ASD-STE100 (TASK / CONTEXT / STEPS / LIMITS / RESULT)" }),
			agent: Type.Optional(Type.String({ description: "scout | worker | reviewer | xval | a name from ~/.pi/agent/agents" })),
			cwd: Type.Optional(Type.String()),
			worktree: Type.Optional(Type.Boolean({ description: "run in a new git worktree on branch apexjob/<id>" })),
			model: Type.Optional(Type.String()),
			provider: Type.Optional(Type.String()),
			thinking: Type.Optional(Type.String({ description: "off | minimal | low | medium | high | xhigh (default: the agent's, else yours)" })),
			timeout_s: Type.Optional(Type.Number({ description: "wall-clock limit (default 3600)" })),
			idle_s: Type.Optional(Type.Number({ description: "no-event limit (default 900; keep above the child's bash timeout)" })),
			max_cost: Type.Optional(Type.Number({ description: "stop the job above this cost in USD" })),
			wait_s: Type.Optional(Type.Number({ description: "wait up to this many seconds for the result (max 600); 0 = background" })),
		}),
		async execute(_id, p, signal, _onUpdate, ctx: ExtensionContext) {
			if (depth() >= maxDepth()) return text(`agent_spawn refused: this is a sub-agent at depth ${depth()} (max ${maxDepth()}). Do the task yourself.`);
			const def = loadAgents().find(a => a.name === (p.agent ?? "worker"));
			if (!def) return text(`Unknown agent "${p.agent}". Agents:\n${agentNames()}`);
			let cwd = p.cwd ? resolvePath(p.cwd.replace(/^~(?=$|\/)/, homedir())) : ctx.cwd;
			if (!existsSync(cwd) || !statSync(cwd).isDirectory()) return text(`agent_spawn refused: cwd ${cwd} is not a folder.`);
			if (p.thinking && !THINKING.has(p.thinking)) return text(`agent_spawn refused: thinking must be one of ${[...THINKING].join(", ")}.`);
			if (p.worktree) {
				try {
					cwd = worktree(cwd, `${Date.now().toString(36)}`);
				} catch (e) {
					return text(`worktree failed: ${(e as Error).message}`);
				}
			}
			const m = manager().spawn({
				task: p.task, agent: def.name, kind: def.kind ?? "pi", cwd,
				model: p.model ?? def.model ?? (def.kind === "xval" ? undefined : ctx.model?.id),
				provider: p.provider ?? def.provider ?? (def.kind === "xval" ? undefined : ctx.model?.provider),
				thinking: def.kind === "xval" ? undefined : (p.thinking ?? def.thinking ?? ctx.thinkingLevel),
				maxDepth: def.kind === "xval" ? undefined : def.max_depth,
				tools: def.tools, systemPrompt: def.kind === "xval" ? undefined : systemPrompt(def),
				timeout_s: p.timeout_s, idle_s: p.idle_s, max_cost: p.max_cost,
			});
			const wait = Math.min(Math.max(p.wait_s ?? 0, 0), MAX_WAIT_S);
			if (wait > 0) {
				const [r] = await holdWait([m.id], () => manager().wait([m.id], wait * 1000, "all", signal));
				if (r && FINAL.has(r.state)) return text(manager().result(m.id)!.text, { id: m.id, state: r.state });
				return text(`Job still runs after ${wait}s: ${line(r ?? m)}. You get a message when it ends.`, { id: m.id });
			}
			const oneShot = ctx.mode === "print" || ctx.mode === "json";
			const next = oneShot
				? "This run is non-interactive (-p): it ends when you stop, so no completion message can arrive. Call agent_wait on this job before your final answer."
				: "You get a message when it ends. Use agent_wait to block with a limit.";
			return text(`Started ${line(m)}${p.worktree ? `\nworktree: ${cwd}` : ""}\n${next}`, { id: m.id });
		},
	});

	pi.registerTool({
		name: "agent_status",
		label: "Sub-agent status",
		description: "Show the state of one sub-agent job, or of all jobs: state, time, turns, cost, last event, current tool.",
		parameters: Type.Object({ job_id: Type.Optional(Type.String()) }),
		async execute(_id, p) {
			const ms = manager().status(p.job_id);
			return text(ms.length ? ms.slice(-20).map(line).join("\n") : "No jobs.");
		},
	});

	pi.registerTool({
		name: "agent_wait",
		label: "Wait for sub-agents",
		description: `Wait for jobs to end, with a time limit (max ${MAX_WAIT_S}s). mode all (default) or any. Returns the states and, for ended jobs, the results.`,
		parameters: Type.Object({
			job_ids: Type.Array(Type.String()),
			timeout_s: Type.Number({ description: `max ${MAX_WAIT_S}` }),
			mode: Type.Optional(Type.String({ description: "all | any" })),
		}),
		async execute(_id, p, signal) {
			const ms = await holdWait(p.job_ids, () => manager().wait(p.job_ids, Math.min(p.timeout_s, MAX_WAIT_S) * 1000, p.mode === "any" ? "any" : "all", signal));
			const body = ms.map(m => (FINAL.has(m.state) ? `${manager().result(m.id)!.text.slice(0, 20_000)}` : line(m))).join("\n\n---\n\n");
			return text(body || "No such jobs.");
		},
	});

	pi.registerTool({
		name: "agent_cancel",
		label: "Cancel sub-agent",
		description: "Stop a job and every process it started (SIGTERM, then SIGKILL after 10 s). The session stays resumable.",
		parameters: Type.Object({ job_id: Type.String() }),
		async execute(_id, p) {
			const m = await manager().cancel(p.job_id);
			if (m && !FINAL.has(m.state) && !manager().manages(m)) return text(`Not cancelled: job ${m.id} belongs to another live pi session (pid ${m.parent_pid}). Cancel it there.`);
			return text(m ? line(m) : `No job ${p.job_id}.`);
		},
	});

	pi.registerTool({
		name: "agent_result",
		label: "Sub-agent result",
		description: "Get the final answer of a job (max 50 KB), the result file path and the resume command.",
		parameters: Type.Object({ job_id: Type.String() }),
		async execute(_id, p) {
			const r = manager().result(p.job_id);
			return text(r ? `${r.text}\n\nresult file: ${r.path}` : `No job ${p.job_id}.`);
		},
	});

	pi.registerCommand("agents", {
		description: "List sub-agent jobs (apex-agents)",
		handler: async (_a, ctx) => {
			const ms = manager().status();
			ctx.ui.notify(ms.length ? ms.slice(-20).map(line).join("\n") : "No jobs.", "info");
		},
	});
}
