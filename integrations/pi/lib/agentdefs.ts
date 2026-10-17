/**
 * Agent definitions for apex-agents. Built-ins are written in ASD-STE100. Files in ~/.pi/agent/agents/*.md
 * (frontmatter: name, description, tools, model, provider, kind, thinking, max_depth) override a built-in with the
 * same name. max_depth > 1 lets the agent start its own sub-agents (a coordinator); the default 1 does not.
 * Pure module: node --test loads it.
 */
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export type AgentDef = {
	name: string; description: string; tools?: string[]; model?: string; provider?: string; kind?: "pi" | "xval";
	thinking?: string; max_depth?: number; prompt: string; source: string;
};

export const THINKING = new Set(["off", "minimal", "low", "medium", "high", "xhigh"]);
export const MAX_DEPTH_CAP = 3; // hard ceiling: a coordinator can never build an unbounded tree

export const BUILTIN: AgentDef[] = [
	{
		name: "scout", source: "builtin", tools: ["read", "grep", "find", "ls", "bash"],
		description: "Find code and facts fast. Read-only. Returns a short list of findings with file:line.",
		prompt: [
			"You are a scout. Find the facts that the task asks for.",
			"Do not change files. Do not run commands that change state.",
			"Return a numbered list. Give one finding in each item, with file:line.",
			"Give only the facts. Do not give a plan.",
		].join("\n"),
	},
	{
		name: "worker", source: "builtin",
		description: "Do one task end to end with all tools. Returns what changed and how it was tested.",
		prompt: [
			"You are a worker. Do the task in the brief.",
			"Do only the task. Do not change other files.",
			"Test the change. Record the command and the result.",
			"Return: CHANGED (files), TESTED (command and result), OPEN (problems that stay).",
		].join("\n"),
	},
	{
		name: "reviewer", source: "builtin", tools: ["read", "grep", "find", "ls", "bash"],
		description: "Review a change. Read-only. Returns findings with file:line and a verdict.",
		prompt: [
			"You are a reviewer. Find problems in the change. Do not fix them.",
			"Do not change files.",
			"Give one finding in each item: file:line, the problem, and how you know.",
			"End with one line: VERDICT: GO, GO-WITH-CHANGES or NO-GO.",
		].join("\n"),
	},
	{
		name: "coordinator", source: "builtin", thinking: "high", max_depth: 2,
		description: "Main coordinator. Splits a task, routes each part to a model tier and a folder, cross-validates with xval, returns one answer.",
		prompt: [
			"You are the coordinator. You plan, delegate, check and merge. You do little of the work yourself.",
			"1. Read the task. Split it into parts. Name the folder (repo) that owns each part.",
			"2. Find folders: read ~/.pi/agent/folders.md if it exists. Else run: ls ~/dev. Use only folders that exist.",
			"3. Route each part (model-routing): scout on a light or mid model for search; worker on a heavy model for code; reviewer for checks.",
			"4. Start each part with agent_spawn. Set cwd to the folder of that part. Set thinking: low for search, high for hard code.",
			"5. Start independent parts at the same time. Give each worker its own files or use worktree=true.",
			"6. Before you trust code or a decision, start agent=xval (GPT, other vendor) to refute it. Triage each finding.",
			"7. Never let two workers edit the same file at the same time.",
			"8. Return: the answer, a table of parts (folder, agent, model, result), the xval verdict, and open items.",
			"Do not push, merge or delete without an explicit instruction in the task.",
		].join("\n"),
	},
	{
		name: "xval", source: "builtin", kind: "xval",
		description: "Independent Codex review (apex-router xval, read-only, it-entra-gpt-6-astra). Returns the review text.",
		prompt: "",
	},
];

function frontmatter(text: string): { meta: Record<string, string>; body: string } {
	const m = /^---\n([\s\S]*?)\n---\n?([\s\S]*)$/.exec(text);
	if (!m) return { meta: {}, body: text };
	const meta: Record<string, string> = {};
	for (const line of m[1]!.split("\n")) {
		const kv = /^(\w+)\s*:\s*(.*)$/.exec(line.trim());
		if (kv) meta[kv[1]!] = kv[2]!.replace(/^["']|["']$/g, "");
	}
	return { meta, body: m[2]!.trim() };
}

export function parseDepth(v: string | undefined): number | undefined {
	if (!v || !/^\d+$/.test(v)) return undefined;
	const n = Number(v);
	return n >= 1 ? Math.min(n, MAX_DEPTH_CAP) : undefined;
}

export function loadAgents(dir: string = join(homedir(), ".pi", "agent", "agents")): AgentDef[] {
	const out = new Map(BUILTIN.map(a => [a.name, a]));
	if (existsSync(dir)) {
		for (const f of readdirSync(dir).filter(f => f.endsWith(".md"))) {
			try {
				const { meta, body } = frontmatter(readFileSync(join(dir, f), "utf8"));
				if (!meta.name) continue;
				out.set(meta.name, {
					name: meta.name, description: meta.description ?? "", prompt: body, source: join(dir, f),
					tools: meta.tools ? meta.tools.split(",").map(s => s.trim()).filter(Boolean) : undefined,
					model: meta.model || undefined, provider: meta.provider || undefined,
					kind: meta.kind === "xval" ? "xval" : "pi",
					thinking: meta.thinking && THINKING.has(meta.thinking) ? meta.thinking : undefined,
					max_depth: parseDepth(meta.max_depth),
				});
			} catch {
				/* a broken file never hides the others */
			}
		}
	}
	return [...out.values()];
}
