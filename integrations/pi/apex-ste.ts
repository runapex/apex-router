/**
 * apex-ste — ASD-STE100 Simplified Technical English is the default output format in pi.
 * Appends the shared rules block (src/apex_router/data/ste_rules.json) to the system prompt before every agent
 * run. Sub-agents started by apex-agents load this extension too, so their briefs and reports use STE.
 * Off: APEX_STE=0. Command: /ste (show the rules and the brief template).
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { briefText, rulesText, steEnabled, withSte } from "./lib/ste.ts";

export default function (pi: ExtensionAPI) {
	pi.on("before_agent_start", async event => {
		if (!steEnabled()) return;
		const next = withSte(event.systemPrompt);
		return next === event.systemPrompt ? undefined : { systemPrompt: next };
	});

	pi.registerCommand("ste", {
		description: "Show the ASD-STE100 rules and the sub-agent brief template (apex-router default format)",
		handler: async (_args, ctx) => {
			ctx.ui.notify(steEnabled() ? `${rulesText()}\n\n${briefText()}` : "STE is off (APEX_STE=0).", "info");
		},
	});
}
