/**
 * apex-route — per-task model/family switching for pi, on top of apex-router.
 *
 * Families come from the SHARED model registry `~/.apex-router/models.json` (the same
 * file codeqa/tier_router reads — one edit moves every component). Resolution:
 *   1. models.json `pi_families` (a family pins {"provider","id"} or {"provider","tier"},
 *      tier resolved via the registry's `tiers` map; "local" follows ornith.env — the
 *      ACTIVE tier, so >>local never triggers a second resident model load)
 *   2. ~/.apex-router/pi-routes.json overlay (back-compat)
 *   3. built-in defaults
 *
 *   >>local    fix this flaky test         -> active Ornith tier (ollama, direct)
 *   >>kimi     summarise this diff         -> Kimi (via the apex proxy)
 *   >>frontier design the migration plan   -> sonnet tier (via the apex proxy)
 *   >>deep     audit this for race hazards -> opus tier (via the apex proxy)
 *   >>auto     <task>                      -> apex-router resolve picks the model
 *                                             (adaptive core; static floor until cells promote)
 *
 * Also on board:
 *  - SESSION IDENTITY: every provider request carries x-claude-code-session-id =
 *    pi's session id, so the apex proxy telemetry attributes pi traffic per session
 *    (previously session_id=null for pi — the most expensive sessions were anonymous).
 *  - PER-FAMILY EFFORT: a family may set "effort" in the registry; it is applied to the
 *    anthropic payload per request (output_config.effort) — the cache-free cost dial.
 *  - ESCALATION AUTO-LOG: a one-shot >>cue turn logs its outcome (ok/escalated) to
 *    apex-router route-log, closing the measure→advise loop without human bookkeeping.
 *
 * A bare `>><family>` (no task) is a sticky switch, same as /apex-route.
 * `/apex-route` with no argument lists the families and shows which one is active.
 *
 * Install:  pi install ~/.apex-router/integrations/pi/apex-route.ts
 */

import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const execFileP = promisify(execFile);

type Route = { provider: string; id: string; effort?: string };

const APEX_HOME = join(homedir(), ".apex-router");
const APEX_BIN = process.env.APEX_ROUTER_BIN || join(homedir(), ".local", "bin", "apex-router");

// Built-in last resort (kept in sync with apex_router.model_registry.DEFAULTS).
const DEFAULT_ROUTES: Record<string, Route> = {
	local: { provider: "ollama", id: "hf.co/ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M" },
	kimi: { provider: "moonshotai", id: "kimi-k2.6" },
	// code-specialized Kimi (DECISION-kimi-codex-routing): ~3x cheaper than k3 for <=262k ctx
	"kimi-code": { provider: "moonshotai", id: "kimi-k2.7-code" },
	"kimi-deep": { provider: "moonshotai", id: "kimi-k3" },  // 1M ctx — long sessions (K1)
	frontier: { provider: "anthropic", id: "claude-sonnet-5", effort: "medium" },
	deep: { provider: "anthropic", id: "claude-opus-4-8", effort: "high" },
};

function readJson(path: string): any | undefined {
	try {
		return JSON.parse(readFileSync(path, "utf8"));
	} catch {
		return undefined;
	}
}

function activeOrnithModel(): string | undefined {
	// The ACTIVE local tier is written here by `apex-router ornith-tier` — following it
	// means >>local never asks ollama to load a second, non-resident tier.
	try {
		const env = readFileSync(join(APEX_HOME, "ornith.env"), "utf8");
		const m = /^ORNITH_API_MODEL=(.+)$/m.exec(env);
		return m?.[1]?.trim() || undefined;
	} catch {
		return undefined;
	}
}

function loadRoutes(): Record<string, Route> {
	const routes: Record<string, Route> = { ...DEFAULT_ROUTES };
	const reg = readJson(join(APEX_HOME, "models.json"));
	const tiers = reg?.tiers && typeof reg.tiers === "object" ? reg.tiers : {};
	const fams = reg?.pi_families && typeof reg.pi_families === "object" ? reg.pi_families : {};
	for (const [name, spec] of Object.entries<any>(fams)) {
		if (!spec || typeof spec.provider !== "string") continue;
		const entry: Partial<Route> = { provider: spec.provider };
		if (spec.source === "ornith.env") {
			const id = activeOrnithModel();
			if (!id) continue;
			entry.id = id;
		} else if (typeof spec.tier === "string") {
			const id = tiers[spec.tier];
			if (typeof id !== "string" || !id) continue;
			entry.id = id;
		} else if (typeof spec.id === "string" && spec.id) {
			entry.id = spec.id;
		} else {
			continue;
		}
		if (typeof spec.effort === "string" && spec.effort) entry.effort = spec.effort;
		routes[name] = entry as Route;
	}
	// Back-compat overlay: an explicit pi-routes.json still wins per family.
	const overlay = readJson(join(APEX_HOME, "pi-routes.json"));
	if (overlay && typeof overlay === "object") {
		for (const [name, r] of Object.entries<any>(overlay)) {
			if (r && typeof r.provider === "string" && typeof r.id === "string") {
				routes[name] = { ...routes[name], provider: r.provider, id: r.id };
			}
		}
	}
	return routes;
}

/** `apex-router resolve --text <task> --json` → {model, task_type} | undefined. Fail-open. */
async function resolveTask(task: string): Promise<{ model?: string; task_type?: string } | undefined> {
	try {
		const { stdout } = await execFileP(APEX_BIN, ["resolve", "--text", task, "--json"], {
			timeout: 15_000,
			maxBuffer: 1024 * 1024,
		});
		return JSON.parse(stdout);
	} catch {
		return undefined;
	}
}

/** Fail-safe outcome log — must never break a turn (route-log exits 0 by contract). */
function logOutcome(taskType: string, startTier: string, outcome: "ok" | "escalated", note = ""): void {
	execFile(APEX_BIN, ["route-log", "--task-type", taskType, "--start-tier", startTier,
		"--outcome", outcome, "--note", note], { timeout: 10_000 }, () => {});
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

	/** >>auto: let the adaptive core pick. Returns the family-less switch success. */
	async function switchAuto(task: string, ctx: ExtensionContext): Promise<boolean> {
		const resolved = await resolveTask(task);
		const id = resolved?.model;
		if (!id) {
			ctx.ui.notify("apex-route >>auto: resolve failed — staying on current model", "warning");
			return false;
		}
		// Find the model across providers we actually have registered.
		for (const provider of ["anthropic", "moonshotai", "ollama"]) {
			const model = ctx.modelRegistry.find(provider, id);
			if (model) {
				const ok = await pi.setModel(model);
				if (ok) {
					resolvedModelId = id; // for the outcome log (not the literal "auto")
					ctx.ui.notify(
						`>>auto → ${provider}/${id} (${resolved?.task_type || "unclassified"})`, "info");
					return true;
				}
			}
		}
		ctx.ui.notify(`>>auto: resolved ${id} but no provider has it — staying put`, "warning");
		return false;
	}

	// Inline task cue: `>><family> <task>` switches for JUST this task, then restores.
	let savedModel: ExtensionContext["model"] | undefined;
	// Pending one-shot cue bookkeeping for the escalation auto-log.
	let pendingCue: { family: string; task: string; taskType?: string } | undefined;
	let resolvedModelId: string | undefined; // >>auto: the model resolve() picked

	async function restoreIfPending(): Promise<void> {
		if (savedModel) {
			await pi.setModel(savedModel);
			savedModel = undefined;
		}
	}

	pi.on("input", async (event, ctx) => {
		const m = /^>>\s*([a-zA-Z0-9_-]+)(?:\s+([\s\S]+))?$/.exec(event.text);
		if (!m || (m[1] !== "auto" && !routes[m[1]])) {
			await restoreIfPending();
			return { action: "continue" };
		}
		const [, family, rest] = m;
		const snapshot = ctx.model;
		const switched = family === "auto" && rest?.trim()
			? await switchAuto(rest, ctx)
			: await switchTo(family, ctx);
		if (!switched) {
			return { action: "continue" }; // leave the cue visible — no silent mis-route
		}
		if (!rest || !rest.trim()) {
			savedModel = undefined; // bare cue = sticky switch
			ctx.ui.notify(`apex-route: switched to ${family} (sticky)`, "info");
			return { action: "handled" };
		}
		if (!savedModel) savedModel = snapshot;
		// task_type for the outcome log: reuse resolve's classification when we already
		// called it (>>auto), else classify cheaply in the background at turn end.
		pendingCue = { family, task: rest };
		return { action: "transform", text: rest };
	});

	// Escalation auto-log: when a one-shot cue turn finishes, record ok/escalated.
	// "escalated" = the cheap turn observably failed (provider error or empty answer) —
	// observable failure only, never a quality judgment (that's cross-validate's job).
	pi.on("turn_end", async (event, _ctx) => {
		if (!pendingCue) return;
		const cue = pendingCue;
		pendingCue = undefined;
		const msg: any = event.message;
		// POSITIVE failure signals only (model-routing doctrine): a provider error. A
		// tool-call-only message legitimately has no text — that is NOT a failure (was
		// mis-logged as escalation on the first turn of every agentic run).
		const failed = msg?.stopReason === "error" || Boolean(msg?.errorMessage);
		const startTier = cue.family === "auto"
			? (resolvedModelId ?? "auto")
			: (routes[cue.family]?.id ?? cue.family);
		if (cue.taskType) {
			logOutcome(cue.taskType, startTier, failed ? "escalated" : "ok",
				failed ? "auto: provider error/empty" : "auto");
		} else {
			// classify once, then log — both fail-safe CLI calls.
			resolveTask(cue.task).then((r) =>
				logOutcome(r?.task_type || "adhoc", startTier, failed ? "escalated" : "ok",
					failed ? "auto: provider error/empty" : "auto"));
		}
	});

	// Session identity: attribute pi traffic per-session through the proxy (B1).
	pi.on("before_provider_headers", (event, ctx) => {
		if (!event.headers["x-claude-code-session-id"]) {
			try {
				event.headers["x-claude-code-session-id"] = ctx.sessionManager.getSessionId();
			} catch {
				// attribution must never break a request
			}
		}
	});

	// Per-family effort: the cache-free output-cost dial, applied to the anthropic payload.
	pi.on("before_provider_request", (event, ctx) => {
		try {
			const fam = Object.entries(routes).find(
				([, r]) => r.provider === ctx.model?.provider && r.id === ctx.model?.id,
			)?.[1];
			if (!fam?.effort || fam.provider !== "anthropic") return undefined;
			const payload: any = event.payload;
			if (payload && typeof payload === "object" && !payload.output_config?.effort) {
				return { ...payload, output_config: { ...(payload.output_config || {}), effort: fam.effort } };
			}
		} catch {
			// effort is an optimization — never break the request
		}
		return undefined;
	});

	// /apex-offload — queue a LOCAL-offload job (the gated-codegen lane is the one that
	// can book frontier savings: it runs the caller's tests and escalates on failure).
	// Usage: /apex-offload codegen <spec> --tests <tests-file>
	//        /apex-offload adhoc <task>
	pi.registerCommand("apex-offload", {
		description: "Queue a local-offload job: /apex-offload codegen <spec> --tests <file> | adhoc <task>",
		handler: async (args, ctx) => {
			const m = /^(codegen|adhoc)\s+([\s\S]+)$/.exec(args.trim());
			if (!m) {
				ctx.ui.notify("usage: /apex-offload codegen <spec> --tests <file> | adhoc <task>", "warning");
				return;
			}
			const [, lane, rest] = m;
			const py = join(homedir(), ".local", "share", "uv", "tools", "apex-router", "bin", "python");
			let cmd: string[];
			if (lane === "codegen") {
				const tm = /^([\s\S]+?)\s+--tests\s+(\S+)\s*$/.exec(rest);
				if (!tm) {
					ctx.ui.notify("codegen needs tests (the lane's correctness gate): "
						+ "/apex-offload codegen <spec> --tests <file>", "warning");
					return;
				}
				cmd = ["-m", "apex_router.ornith.queue_task", "--lane", "codegen",
					"--spec", tm[1], "--tests-file", tm[2]];
			} else {
				cmd = ["-m", "apex_router.ornith.queue_task", "--lane", "adhoc", "--task", rest];
			}
			try {
				const { stdout } = await execFileP(py, cmd, { timeout: 15_000 });
				ctx.ui.notify(`apex-offload: queued ${lane} job (${String(stdout).trim()}) — `
					+ "the worker drains within ~5s; results in ~/.apex-router/queue/jobs/done", "info");
			} catch (e) {
				ctx.ui.notify(`apex-offload: enqueue failed: ${(e as Error).message}`, "error");
			}
		},
	});

	// Sticky switch + listing.
	pi.registerCommand("apex-route", {
		description: "Switch model family (usage: /apex-route [local|kimi|frontier|deep|auto])",
		handler: async (args, ctx) => {
			const family = args.trim();
			if (!family) {
				const active = ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : "none";
				const lines = Object.entries(routes).map(
					([k, r]) => `  ${k} -> ${r.provider}/${r.id}${r.effort ? ` (effort ${r.effort})` : ""}`);
				ctx.ui.notify(`apex-route families:\n${lines.join("\n")}\n  auto -> apex-router resolve (adaptive)\nactive model: ${active}`, "info");
				return;
			}
			if (family === "auto") {
				ctx.ui.notify("apex-route: auto needs a task — use >>auto <task>", "info");
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
