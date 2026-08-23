/**
 * apex-route — per-task model/family switching for pi, on top of apex-router.
 *
 * Two ways to switch, both mid-session, no restart:
 *
 *   1. Inline task cue — prefix a message with `>><family> ` and just that task
 *      runs on the chosen family; the prefix is stripped before the model sees it,
 *      and your ORIGINAL model is restored on your next ordinary message.
 *      (`>>` is used instead of `@` because pi reserves `@` for file mentions.)
 *      A bare `>><family>` (no task) is a sticky switch, same as /apex-route.
 *        >>local   fix this flaky test         -> local Ornith tier (via ollama)
 *        >>kimi    summarise this diff          -> Kimi K2 (via the apex proxy)
 *        >>frontier design the migration plan   -> Claude Sonnet (via the apex proxy)
 *        >>deep    audit this for race hazards  -> Claude Opus (via the apex proxy)
 *
 *   2. Sticky switch — `/apex-route <family>` changes the active model until you
 *      change it again. `/apex-route` with no argument lists the families and
 *      shows which one is active.
 *
 * Families resolve to concrete provider/model pairs in ROUTES below. Override the
 * table without editing this file by dropping a JSON map at
 *   ~/.apex-router/pi-routes.json      e.g. { "local": { "provider": "ollama",
 *                                              "id": "qwen2.5-coder:7b" } }
 *
 * The anthropic + moonshotai providers are expected to point at the apex-router
 * proxy (see integrations/pi/models.json), so every frontier/kimi turn is
 * measured and routable by apex-router; local turns go straight to ollama.
 *
 * Install:  pi install ~/.apex-router/integrations/pi/apex-route.ts
 * Or ad hoc: pi -e ~/.apex-router/integrations/pi/apex-route.ts
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

type Route = { provider: string; id: string };

// Default family -> provider/model. Keep ids in sync with `pi --list-models`.
const ROUTES: Record<string, Route> = {
	local: { provider: "ollama", id: "hf.co/ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M" },
	kimi: { provider: "moonshotai", id: "kimi-k2.6" },
	frontier: { provider: "anthropic", id: "claude-sonnet-4-5" },
	deep: { provider: "anthropic", id: "claude-opus-4-5" },
};

// Optional user override file: merges over (and can extend) ROUTES.
function loadRoutes(): Record<string, Route> {
	try {
		const path = join(homedir(), ".apex-router", "pi-routes.json");
		const parsed = JSON.parse(readFileSync(path, "utf8")) as Record<string, Route>;
		return { ...ROUTES, ...parsed };
	} catch {
		return ROUTES;
	}
}

export default function (pi: ExtensionAPI) {
	const routes = loadRoutes();

	async function switchTo(family: string, ctx: ExtensionContext): Promise<boolean> {
		const route = routes[family];
		if (!route) {
			ctx.ui.notify(`apex-route: unknown family "${family}" (have: ${Object.keys(routes).join(", ")})`, "warning");
			return false;
		}
		const model = ctx.modelRegistry.find(route.provider, route.id);
		if (!model) {
			ctx.ui.notify(`apex-route: model ${route.provider}/${route.id} not found — check models.json`, "warning");
			return false;
		}
		const ok = await pi.setModel(model);
		if (!ok) ctx.ui.notify(`apex-route: no API key for ${route.provider}/${route.id}`, "error");
		return ok;
	}

	// 1. Inline task cue: `>><family> <task>` switches the model for JUST this task,
	// then restores the prior model on the next ordinary message. A bare `>><family>`
	// is a sticky switch. `>>` avoids pi's reserved `@` (file) and `/` (command) sigils.
	let savedModel: ExtensionContext["model"] | undefined; // original, pending one-shot restore
	async function restoreIfPending(): Promise<void> {
		if (savedModel) {
			await pi.setModel(savedModel);
			savedModel = undefined;
		}
	}
	pi.on("input", async (event, ctx) => {
		const m = /^>>\s*([a-zA-Z0-9_-]+)(?:\s+([\s\S]+))?$/.exec(event.text);
		if (!m || !routes[m[1]]) {
			// ordinary message (or an unknown family we don't own): first undo any
			// one-shot cue left active from the previous turn, then pass through.
			await restoreIfPending();
			return { action: "continue" };
		}
		const [, family, rest] = m;
		const snapshot = ctx.model;
		if (!(await switchTo(family, ctx))) {
			// failed switch (unknown model / no key): do NOT strip the cue — leave the
			// text intact so the failure is visible instead of silently mis-routing.
			return { action: "continue" };
		}
		if (!rest || !rest.trim()) {
			savedModel = undefined; // bare cue = sticky switch, nothing to restore
			ctx.ui.notify(`apex-route: switched to ${family} (sticky)`, "info");
			return { action: "handled" };
		}
		if (!savedModel) savedModel = snapshot; // remember original for the one-shot restore
		return { action: "transform", text: rest };
	});

	// 2. Sticky switch + listing.
	pi.registerCommand("apex-route", {
		description: "Switch model family (usage: /apex-route [local|kimi|frontier|deep])",
		handler: async (args, ctx) => {
			const family = args.trim();
			if (!family) {
				const active = ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : "none";
				const lines = Object.entries(routes).map(([k, r]) => `  ${k} -> ${r.provider}/${r.id}`);
				ctx.ui.notify(`apex-route families:\n${lines.join("\n")}\nactive model: ${active}`, "info");
				return;
			}
			if (await switchTo(family, ctx)) ctx.ui.notify(`apex-route: switched to ${family}`, "info");
		},
	});

	// Status indicator so the active family/model is always visible.
	pi.on("model_select", (event, ctx) => {
		const family = Object.entries(routes).find(
			([, r]) => r.provider === event.model.provider && r.id === event.model.id,
		)?.[0];
		ctx.ui.setStatus("apex-route", family ? `⟿ ${family}` : `⟿ ${event.model.id}`);
	});
}
