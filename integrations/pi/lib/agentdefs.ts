/**
 * Agent definitions for apex-agents. Built-ins are written in ASD-STE100. Files in ~/.pi/agent/agents/*.md
 * (frontmatter: name, description, tools, model, provider, kind) override a built-in with the same name.
 * Pure module: node --test loads it.
 */
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export type AgentDef = { name: string; description: string; tools?: string[]; model?: string; provider?: string; kind?: "pi" | "xval"; prompt: string; source: string };

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
				});
			} catch {
				/* a broken file never hides the others */
			}
		}
	}
	return [...out.values()];
}
