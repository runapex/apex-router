/**
 * Hang guard core (DESIGN-pi-subagents.md P1). Pure: decides how to rewrite or block ONE bash call.
 * The extension (apex-hangguard.ts) applies the decision to the mutable tool_call input.
 *
 * Rules, each fixing a measured hang class in ~/.pi/agent/sessions:
 *   timeout  — a call with no timeout gets APEX_PI_BASH_TIMEOUT (default 600 s).
 *   stdin    — codex exec / apex-router xval / claude -p / pi -p with no stdin redirect gets `< /dev/null`
 *              (codex exec waits on stdin even with a prompt argument).
 *   ssh      — ssh/scp without BatchMode gets `-o BatchMode=yes -o ConnectTimeout=15` (a password prompt
 *              becomes an error, not a hang).
 *   poll     — a bare `sleep N` (N > 120) is blocked while an apex sub-agent job runs: use agent_wait.
 * Opt-out per call: a `# apex-hangguard: off` comment in the command.
 *
 * Safety rule: the timeout is always safe to add. The command text changes only when the scanner understands
 * the whole command (quotes balanced, no heredoc) and the program is in command position. In all other cases
 * the command text stays unchanged. A missed rewrite costs one possible hang; a wrong rewrite breaks a command.
 */
export type Decision = { command: string; timeout?: number; block?: boolean; reason?: string; changes: string[] };

export const DEFAULT_TIMEOUT_S = 600;
export const POLL_LIMIT_S = 120;

/** Optional launch prefixes before the program: `(`, `$(`, `{`, nohup, timeout N, env A=b, time, exec, A=b. */
const PREFIX = String.raw`^\s*(?:\$?\(\s*|\{\s+)*(?:(?:nohup|time|exec|command)\s+|timeout\s+(?:-\S+\s+)*\S+\s+|env\s+(?:\w+=\S*\s+)*|\w+=\$\(\s*|\w+=\S*\s+)*`;
const AGENT_CLI = new RegExp(`${PREFIX}(?:codex\\s+exec|apex-router\\s+xval|claude\\s+(?:-p|--print)|pi\\s+(?:\\S+\\s+)*?(?:-p|--print|--mode\\s+json))(?=\\s|$)`);
const SSH = new RegExp(`(${PREFIX})(ssh|scp)(?=\\s)`);

export function defaultTimeout(env: NodeJS.ProcessEnv = process.env): number {
	const v = Number(env.APEX_PI_BASH_TIMEOUT);
	return Number.isFinite(v) && v > 0 ? v : DEFAULT_TIMEOUT_S;
}

type Scan = { raw: string[]; bare: string[]; seps: string[]; ok: boolean; groupedPipe: boolean[] };

/**
 * Split on top-level ; && || | & and newlines. `raw` keeps the text; `bare` has the same length with quoted
 * text, escaped characters and comments blanked, so rules match only shell syntax. ok=false when quotes do
 * not balance or a heredoc is present (the scanner does not model them).
 * Subshell parens are not tracked: `( nohup codex exec … & )` splits at `&`, which puts the redirect next to
 * the command itself.
 */
export function scan(cmd: string): Scan {
	const raw: string[] = [], bare: string[] = [], seps: string[] = [], groupedPipe: boolean[] = [];
	let r = "", b = "", q: string | null = null, ok = true;
	// open { / ( groups; each entry is true when the group follows a pipe (its commands read the pipe)
	const groups: boolean[] = [];
	let lastSep = "", inPipeGroup = false; // this segment has text inside a group that reads a pipe
	const cut = (sep: string) => {
		raw.push(r);
		bare.push(b);
		seps.push(sep);
		groupedPipe.push(inPipeGroup || groups.some(Boolean));
		r = b = "";
		lastSep = sep;
		inPipeGroup = groups.some(Boolean);
	};
	for (let i = 0; i < cmd.length; i++) {
		const c = cmd[i]!;
		if (c === "\\" && q !== "'") {
			const pair = cmd.slice(i, i + 2);
			r += pair;
			b += " ".repeat(pair.length);
			i++;
			continue;
		}
		if (q) {
			r += c;
			b += c === q ? c : c === "\n" ? "\n" : " ";
			if (c === q) q = null;
			continue;
		}
		if (c === "'" || c === '"' || c === "`") {
			q = c;
			r += c;
			b += c;
			continue;
		}
		if (c === "$" && cmd[i + 1] === "{") {
			// parameter expansion: opaque up to the matching }
			let depth = 0, k = i + 1;
			for (; k < cmd.length; k++) {
				if (cmd[k] === "{") depth++;
				else if (cmd[k] === "}" && --depth === 0) break;
			}
			if (k >= cmd.length) ok = false;
			r += cmd.slice(i, k + 1);
			b += " ".repeat(Math.min(k + 1, cmd.length) - i);
			i = k;
			continue;
		}
		if ((c === "{" || c === "(") && /(^|[\s;&|(])$/.test(b.replace(/\$$/, ""))) {
			const segHasCmd = b.trim().replace(/[{($]/g, "") !== "";
			groups.push(!segHasCmd && (lastSep === "|" || groups.some(Boolean)));
			if (groups.some(Boolean)) inPipeGroup = true;
		}
		if ((c === "}" || c === ")") && groups.length) groups.pop();
		if (c === "#" && (b === "" || /\s$/.test(b))) {
			const end = cmd.indexOf("\n", i);
			const stop = end < 0 ? cmd.length : end;
			r += cmd.slice(i, stop);
			b += " ".repeat(stop - i);
			i = stop - 1;
			continue;
		}
		if (c === "<" && cmd[i + 1] === "<" && cmd[i + 2] !== "<") ok = false; // heredoc (<<< is a here-string)
		const two = cmd.slice(i, i + 2);
		if (two === "&&" || two === "||") {
			cut(two);
			i++;
			continue;
		}
		if (c === ";" || c === "\n" || c === "|") {
			cut(c);
			continue;
		}
		// a lone `&` (background) separates commands; `2>&1`, `&>file` and `>&2` do not
		if (c === "&" && cmd[i - 1] !== ">" && cmd[i + 1] !== ">") {
			cut("&");
			continue;
		}
		r += c;
		b += c;
	}
	raw.push(r);
	bare.push(b);
	groupedPipe.push(inPipeGroup || groups.some(Boolean));
	return { raw, bare, seps, ok: ok && q === null, groupedPipe };
}

const hasStdin = (bare: string): boolean => /(^|[^<])<(?!<)\s*\S/.test(bare);

export function decide(command: string, timeout: number | undefined, opts: { jobsRunning?: number; env?: NodeJS.ProcessEnv } = {}): Decision {
	const d: Decision = { command, timeout, changes: [] };
	if (/#\s*apex-hangguard:\s*off/.test(command)) return d;
	const s = scan(command);
	if (s.ok) {
		const expect = /\bexpect\b/.test(s.bare.join(" "));
		const out: string[] = [];
		for (let i = 0; i < s.raw.length; i++) {
			let raw = s.raw[i]!;
			const bare = s.bare[i]!;
			const piped = (i > 0 && s.seps[i - 1] === "|") || s.groupedPipe[i]!;
			if (AGENT_CLI.test(bare) && !hasStdin(bare) && !piped) {
				// insert before trailing space and a trailing `\`+newline continuation
				const m = /^(.*?)((?:\s|\\\n)*)$/s.exec(raw)!;
				raw = `${m[1]} < /dev/null${m[2]}`;
				d.changes.push("stdin");
			}
			const ssh = SSH.exec(bare);
			if (ssh && !/BatchMode|ConnectTimeout/i.test(raw) && !expect) {
				const at = ssh[1]!.length + ssh[2]!.length;
				raw = `${raw.slice(0, at)} -o BatchMode=yes -o ConnectTimeout=15${raw.slice(at)}`;
				d.changes.push("ssh");
			}
			out.push(raw, s.seps[i] ?? "");
		}
		d.command = out.join("");
	}
	const sleep = /^\s*(?:cd\s+[^;&]+(?:&&|;)\s*)?sleep\s+(\d+)\s*(?:;|&&|$)/.exec(command);
	if (sleep && Number(sleep[1]) > POLL_LIMIT_S && (opts.jobsRunning ?? 0) > 0) {
		return { ...d, block: true, reason: `Do not poll with sleep while an apex sub-agent job runs. Use agent_wait with a timeout. (${opts.jobsRunning} job(s) running)`, changes: [...d.changes, "poll"] };
	}
	if (timeout === undefined || timeout === null) {
		d.timeout = defaultTimeout(opts.env);
		d.changes.push("timeout");
	}
	return d;
}
