/**
 * P0 spike only (DESIGN-pi-subagents.md, check 1). Registers `p0_defer`: the tool returns at once and a
 * timer pushes a custom message into the session 3 s LATER, after the tool call has settled. The check
 * passes when that deferred message wakes the agent (it then answers GOT-PING).
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";

export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "p0_defer",
		label: "p0 defer",
		description: "Schedules a ping that arrives a few seconds later as a new message.",
		parameters: Type.Object({}),
		async execute() {
			setTimeout(() => {
				try {
					pi.sendMessage(
						{ customType: "p0-ping", content: "DEFERRED-PING: the background job finished.", display: true },
						{ deliverAs: "followUp", triggerTurn: true },
					);
				} catch (e) {
					process.stderr.write(`P0_SENDMESSAGE_THREW ${String(e)}\n`);
				}
			}, 3000);
			return { content: [{ type: "text", text: "scheduled; the ping arrives later" }], details: {} };
		},
	});
}
