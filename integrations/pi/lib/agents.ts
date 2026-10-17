/**
 * Sub-agent job manager (DESIGN-pi-subagents.md P2). Pure Node: no pi imports, so node --test drives it with a
 * fake child. The pi extension (apex-agents.ts) only wires these methods to tools.
 *
 * State lives on disk under <root>/<id>/ (meta.json, events.jsonl, stderr.log, result.md, system.md), so a job
 * survives a parent turn, /reload and a parent restart. The child writes its stdout straight to events.jsonl
 * (not through a pipe), so it keeps running if the parent exits.
 *
 * One writer per job: the manager whose `owner` token is in meta.json. Only the owner starts, ingests, watches,
 * finishes and notifies the job. A manager adopts a job (under a lock) only when the owner's process is gone.
 *
 * Every job ends in one state: done | failed | killed | timed_out(idle) | timed_out(wall) | killed(cost).
 * Cancel follows the P0 result: snapshot descendant pids, SIGTERM them, wait, SIGKILL their process groups
 * (pi's bash tool runs commands in their own group, and SIGKILL alone leaves them as orphans).
 */
import { spawn, execFileSync } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, readSync, readdirSync, renameSync, rmdirSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export type JobState = "queued" | "queued(pressure)" | "running" | "done" | "failed" | "killed" | "timed_out(idle)" | "timed_out(wall)" | "killed(cost)";
export const FINAL: ReadonlySet<string> = new Set(["done", "failed", "killed", "timed_out(idle)", "timed_out(wall)", "killed(cost)"]);

export type Spec = {
	task: string;
	agent?: string;
	kind?: "pi" | "xval";
	cwd?: string;
	model?: string;
	provider?: string;
	tools?: string[];
	thinking?: string;
	maxDepth?: number; // APEX_AGENT_MAX_DEPTH for the child; undefined = inherit
	systemPrompt?: string;
	timeout_s?: number;
	idle_s?: number;
	max_cost?: number;
	parentSession?: string;
};

export type Usage = { turns: number; input: number; output: number; cost: number };

export type Meta = {
	id: string;
	state: JobState;
	reason?: string;
	agent: string;
	kind: string;
	task: string;
	cwd: string;
	argv: string[];
	pid?: number;
	created: number;
	started?: number;
	ended?: number;
	last_event?: number;
	timeout_s: number;
	idle_s: number;
	max_cost?: number;
	max_depth?: number;
	session_id: string;
	resume?: string;
	exit_code?: number | null;
	usage: Usage;
	last_tool?: string;
	final_preview?: string;
	parentSession?: string;
	parent_pid?: number;
	owner?: string;
	/** Bytes of events.jsonl already counted into usage (lets an adopter continue without a double count). */
	events_offset?: number;
	notify_attempts?: number;
	/** Error of the LAST assistant message; a later successful message clears it (pi retried and recovered). */
	model_error?: string;
	/** Time of the last 429 / rate-limit retry in this job (lowers the cap for new jobs). */
	rate_limited_at?: number;
	retries?: number;
	/** true when 3 deliveries failed; the result stays in result.md and agent_status shows it */
	notify_failed?: boolean;
	/** `ps -o lstart=` of the child at spawn. A pid with another start time is a reused pid: not our job. */
	pid_start?: string;
	notified?: boolean;
};

export type Options = {
	root?: string;
	/** Builds the child command line. Default: pi (kind pi) or apex-router xval (kind xval). Tests inject a fake. */
	argv?: (spec: Spec, meta: Meta, dir: string) => string[];
	cap?: number;
	pressure?: () => "GREEN" | "AMBER" | "RED" | "UNKNOWN";
	defaultTimeoutS?: number;
	defaultIdleS?: number;
	graceMs?: number;
	tickMs?: number;
	onDone?: (meta: Meta) => void;
	/** Owner token of this manager (default: pid + random). Tests give two managers in one process their own. */
	self?: string;
	/** Is the owner process of a job alive? Default: `kill -0 parent_pid`. */
	ownerAlive?: (meta: Meta) => boolean;
	now?: () => number;
};

export const DEFAULT_ROOT = join(homedir(), ".apex-router", "pi-jobs");
export const RESULT_CAP = 50_000;
export const RATE_LIMIT = /\b429\b|rate.?limit|RateLimitReached|overloaded/i;
/** After a 429 in any job, new jobs start one at a time for this long. */
export const RATE_LIMIT_COOLDOWN_MS = 120_000;

export function readPressure(path = join(homedir(), ".apex-router", "pressure.json"), now = Date.now()): "GREEN" | "AMBER" | "RED" | "UNKNOWN" {
	try {
		const st = statSync(path);
		if (now - st.mtimeMs > 30 * 60_000) return "GREEN"; // stale snapshot: fail open
		const lvl = JSON.parse(readFileSync(path, "utf8")).level;
		return lvl === "AMBER" || lvl === "RED" || lvl === "GREEN" ? lvl : "UNKNOWN";
	} catch {
		return "GREEN";
	}
}

/** Env of a child job: depth + 1. A per-job max depth may only RAISE the inherited limit for that
 *  subtree up to MAX_DEPTH_CAP, and a child's max is never above the cap, so a tree stays bounded. */
export function childEnv(env: NodeJS.ProcessEnv, jobId: string, maxDepth?: number): NodeJS.ProcessEnv {
	const depth = Number(env.APEX_AGENT_DEPTH ?? "0") + 1;
	const inherited = Number(env.APEX_AGENT_MAX_DEPTH ?? "1");
	const out: NodeJS.ProcessEnv = { ...env, APEX_AGENT_DEPTH: String(depth), APEX_AGENT_JOB: jobId };
	if (maxDepth !== undefined && Number.isFinite(maxDepth)) {
		// The coordinator runs at `depth`; it may start children while depth < its max.
		out.APEX_AGENT_MAX_DEPTH = String(Math.min(Math.max(depth + maxDepth - 1, inherited), 3));
	}
	return out;
}

export function defaultArgv(spec: Spec, meta: Meta, dir: string): string[] {
	if ((spec.kind ?? "pi") === "xval") {
		return ["apex-router", "xval", "-m", spec.model ?? "it-entra-gpt-6-astra", "-s", "read-only", "--skip-git-repo-check", spec.task];
	}
	const a = ["pi", "--mode", "json", "-p", "--session-id", meta.session_id];
	if (spec.provider) a.push("--provider", spec.provider);
	if (spec.model) a.push("--model", spec.model);
	if (spec.thinking) a.push("--thinking", spec.thinking);
	if (spec.tools?.length) a.push("--tools", spec.tools.join(","));
	if (existsSync(join(dir, "system.md"))) a.push("--append-system-prompt", join(dir, "system.md"));
	a.push(`Task: ${spec.task}`);
	return a;
}

const alive = (pid: number | undefined): boolean => {
	if (!pid) return false;
	try {
		process.kill(pid, 0);
		return true;
	} catch (e) {
		return (e as NodeJS.ErrnoException).code === "EPERM";
	}
};

export function startTime(pid: number | undefined): string | undefined {
	if (!pid) return undefined;
	try {
		return execFileSync("ps", ["-o", "lstart=", "-p", String(pid)], { encoding: "utf8" }).trim().replace(/\s+/g, " ") || undefined;
	} catch {
		return undefined;
	}
}

/** The process is alive AND is the same process (not a reused pid). */
/** Liveness: alive, and not a different process (when the start time is known). */
const ours = (m: Meta): boolean => alive(m.pid) && (m.pid_start === undefined || startTime(m.pid) === m.pid_start);
/** Kill permission: the start time is known AND matches. Without it, apex never sends a group SIGKILL. */
const sure = (m: Meta): boolean => alive(m.pid) && m.pid_start !== undefined && startTime(m.pid) === m.pid_start;

export type Proc = { pid: number; ppid: number; pgid: number; start: string };

/** One `ps` snapshot of every process: pid, ppid, pgid, start time (whitespace collapsed). */
export function psTable(): Proc[] {
	try {
		return execFileSync("ps", ["-A", "-o", "pid=,ppid=,pgid=,lstart="], { encoding: "utf8" })
			.split("\n").map(l => l.trim().split(/\s+/)).filter(f => f.length >= 4)
			.map(f => ({ pid: Number(f[0]), ppid: Number(f[1]), pgid: Number(f[2]), start: f.slice(3).join(" ") }))
			.filter(p => Number.isFinite(p.pid) && Number.isFinite(p.pgid));
	} catch {
		return [];
	}
}

/** The job's process tree in a snapshot: BFS over ppid from the root (the root must match its start time). */
export function treeOf(root: number, rootStart: string, table: Proc[]): Proc[] {
	const r = table.find(p => p.pid === root);
	if (!r || r.start !== rootStart) return [];
	const kids = new Map<number, Proc[]>();
	for (const p of table) kids.set(p.ppid, [...(kids.get(p.ppid) ?? []), p]);
	const out: Proc[] = [];
	const seen = new Set<number>();
	const q = [r];
	while (q.length) {
		const p = q.shift()!;
		if (seen.has(p.pid)) continue;
		seen.add(p.pid);
		out.push(p);
		q.push(...(kids.get(p.pid) ?? []));
	}
	return out;
}

/**
 * Which processes to SIGKILL after the grace period. A process in `after` belongs to the job when:
 *   (a) it is in the first tree with the same start time, or
 *   (b) it is new (its pid was not in `before`) and its parent belongs to the job, or its group is a group of
 *       the first tree (a child started during SIGTERM handling, also after its parent exited).
 * Groups to kill: the groups of those processes, minus `exclude` and pgid <= 1. A reused pid is never in (a)
 * (different start time). A first-tree group whose leader pid is now another process is reused: rule (b)
 * ignores it.
 * Known limit: a child that calls setsid() during SIGTERM handling, after which its parent exits, has no link
 * to the job in a ps table. It can survive. Only OS containment (Linux cgroups) closes this; macOS has none.
 */
export function killTargets(tree0: Proc[], before: Proc[], after: Proc[], exclude: Set<number>): { pgids: number[]; pids: number[] } {
	const key = (p: Proc) => `${p.pid}|${p.start}`;
	const t0 = new Set(tree0.map(key));
	// a first-tree group is still the job's only if its leader pid (pid == pgid) is not now a different process
	const reused = (g: number) => after.some(p => p.pid === g && !t0.has(key(p)));
	const groups0 = new Set(tree0.map(p => p.pgid).filter(g => !reused(g)));
	const was = new Set(before.map(p => p.pid));
	const job = new Set<number>();
	for (let changed = true; changed; ) {
		changed = false;
		for (const p of after) {
			if (job.has(p.pid)) continue;
			const fresh = !was.has(p.pid);
			if (t0.has(key(p)) || (fresh && (job.has(p.ppid) || groups0.has(p.pgid)))) {
				job.add(p.pid);
				changed = true;
			}
		}
	}
	const procs = after.filter(p => job.has(p.pid));
	const pgids = [...new Set(procs.map(p => p.pgid))].filter(g => g > 1 && !exclude.has(g));
	return { pgids, pids: procs.map(p => p.pid).filter(p => p > 1) };
}

function ownPgid(pid: number = process.pid): number {
	try {
		return Number(execFileSync("ps", ["-o", "pgid=", "-p", String(pid)], { encoding: "utf8" }).trim()) || -1;
	} catch {
		return -1;
	}
}

export class JobManager {
	readonly root: string;
	readonly self: string;
	private o: Required<Omit<Options, "onDone" | "root" | "self">> & { onDone?: (m: Meta) => void };
	private timer: NodeJS.Timeout | null = null;
	private cancelling = new Set<string>();
	private stopped = false;
	private tickNo = 0;
	private tried = new Map<string, number>();

	/** Owner tokens of the managers in this process that are not stopped. */
	static live = new Set<string>();

	constructor(opts: Options = {}) {
		this.root = opts.root ?? DEFAULT_ROOT;
		this.self = opts.self ?? `${process.pid}-${Math.random().toString(36).slice(2, 8)}`;
		JobManager.live.add(this.self);
		this.o = {
			argv: opts.argv ?? defaultArgv,
			cap: opts.cap ?? 4,
			pressure: opts.pressure ?? (() => readPressure()),
			defaultTimeoutS: opts.defaultTimeoutS ?? 3600,
			defaultIdleS: opts.defaultIdleS ?? 900,
			graceMs: opts.graceMs ?? 10_000,
			tickMs: opts.tickMs ?? 2000,
			now: opts.now ?? Date.now,
			onDone: opts.onDone,
			// the owner is gone when its process ended, or when it was a replaced manager in THIS process
			// (pi /reload builds a new extension instance with a new token in the same pid)
			ownerAlive: opts.ownerAlive ?? (m => alive(m.parent_pid) && !(m.parent_pid === process.pid && m.owner !== this.self && !JobManager.live.has(m.owner ?? ""))),
		};
		mkdirSync(this.root, { recursive: true });
	}

	// ------------------------------------------------------------------ disk
	private dir(id: string) {
		return join(this.root, id);
	}
	read(id: string): Meta | null {
		try {
			return JSON.parse(readFileSync(join(this.dir(id), "meta.json"), "utf8")) as Meta;
		} catch {
			return null;
		}
	}
	/** Atomic: a reader never sees a half-written meta.json. */
	private write(m: Meta) {
		const p = join(this.dir(m.id), "meta.json");
		const tmp = `${p}.${this.self}.tmp`;
		writeFileSync(tmp, JSON.stringify(m, null, 1));
		renameSync(tmp, p);
	}
	/** Per-job lock (mkdir is atomic). A lock older than 30 s is stale and is broken. */
	private withLock<T>(id: string, fn: () => T): T | undefined {
		const l = join(this.dir(id), ".lock");
		try {
			mkdirSync(l);
		} catch {
			try {
				if (Date.now() - statSync(l).mtimeMs < 30_000) return undefined;
				rmdirSync(l);
				mkdirSync(l);
			} catch {
				return undefined;
			}
		}
		try {
			return fn();
		} finally {
			try {
				rmdirSync(l);
			} catch {
				/* already gone */
			}
		}
	}
	/** This manager is the single writer of the job. */
	manages(m: Meta): boolean {
		return m.owner === this.self;
	}
	managed(): Meta[] {
		return this.list().filter(m => this.manages(m));
	}
	list(): Meta[] {
		if (!existsSync(this.root)) return [];
		return readdirSync(this.root).map(id => this.read(id)).filter((m): m is Meta => m !== null).sort((a, b) => a.created - b.created);
	}
	running(): Meta[] {
		return this.list().filter(m => m.state === "running");
	}
	/** Jobs whose process is alive now. Read-only: no tick, no writes (the hang guard calls this on every bash call). */
	live(): Meta[] {
		return this.running().filter(m => alive(m.pid));
	}

	// ------------------------------------------------------------------ spawn
	spawn(spec: Spec): Meta {
		const now = this.o.now();
		const id = `${new Date(now).toISOString().slice(0, 19).replace(/[-:T]/g, "")}-${Math.random().toString(36).slice(2, 6)}`;
		const dir = this.dir(id);
		mkdirSync(dir, { recursive: true });
		if (spec.systemPrompt) writeFileSync(join(dir, "system.md"), spec.systemPrompt);
		const m: Meta = {
			id, state: "queued", agent: spec.agent ?? "worker", kind: spec.kind ?? "pi", task: spec.task,
			cwd: spec.cwd ?? process.cwd(), argv: [], created: now,
			timeout_s: spec.timeout_s ?? this.o.defaultTimeoutS, idle_s: spec.idle_s ?? this.o.defaultIdleS,
			max_cost: spec.max_cost, max_depth: spec.maxDepth, session_id: `apexjob-${id}`, usage: { turns: 0, input: 0, output: 0, cost: 0 },
			parentSession: spec.parentSession, parent_pid: process.pid, owner: this.self,
		};
		m.argv = this.o.argv(spec, m, dir);
		if (m.kind === "pi") m.resume = `cd ${JSON.stringify(m.cwd)} && pi --session-id ${m.session_id}`;
		this.write(m);
		this.pump();
		this.ensureTimer();
		return this.read(id)!;
	}

	/** Start queued jobs while the cap (lowered by pressure) allows. */
	private pump() {
		const lvl = this.o.pressure();
		const now = this.o.now();
		const throttled = this.list().some(x => x.rate_limited_at !== undefined && now - x.rate_limited_at < RATE_LIMIT_COOLDOWN_MS);
		const cap = lvl === "RED" ? 0 : throttled ? 1 : lvl === "AMBER" ? Math.min(this.o.cap, 2) : this.o.cap;
		let free = cap - this.running().length; // the cap counts every job on the machine
		for (const m of this.managed().filter(x => x.state === "queued" || x.state === "queued(pressure)")) {
			if (free <= 0) {
				const st: JobState = lvl === "RED" || ((lvl === "AMBER" || throttled) && this.running().length < this.o.cap) ? "queued(pressure)" : "queued";
				if (m.state !== st) this.write({ ...m, state: st });
				continue;
			}
			this.start(m);
			free--;
		}
	}

	private start(m: Meta) {
		const dir = this.dir(m.id);
		const out = openSync(join(dir, "events.jsonl"), "a");
		const err = openSync(join(dir, "stderr.log"), "a");
		const [cmd, ...args] = m.argv;
		let pid: number | undefined;
		try {
			const child = spawn(cmd!, args, {
				cwd: m.cwd, detached: true, stdio: ["ignore", out, err],
				env: childEnv(process.env, m.id, m.max_depth),
			});
			pid = child.pid;
			child.on("error", e => {
				writeFileSync(join(dir, "stderr.log"), `spawn error: ${e.message}\n`, { flag: "a" });
			});
			child.on("exit", code => {
				if (this.stopped) return; // a stopped manager (old parent) leaves the job to whoever re-attaches
				const cur = this.read(m.id);
				if (cur) this.write({ ...cur, exit_code: code });
				this.tick();
			});
			child.unref();
		} finally {
			closeSync(out);
			closeSync(err);
		}
		const now = this.o.now();
		this.write({ ...m, state: pid ? "running" : "failed", reason: pid ? undefined : "spawn failed", pid, pid_start: startTime(pid), started: now, last_event: now, ...(pid ? {} : { ended: now }) });
		if (!pid) this.finish(this.read(m.id)!);
	}

	// ------------------------------------------------------------------ monitor
	private ensureTimer() {
		if (this.timer) return;
		this.timer = setInterval(() => this.tick(), this.o.tickMs);
		this.timer.unref();
	}
	/** Stop monitoring. Running children keep going; a later manager on the same root re-attaches. */
	stop() {
		this.stopped = true;
		JobManager.live.delete(this.self);
		if (this.timer) clearInterval(this.timer);
		this.timer = null;
	}

	/** Read new event lines; update liveness, usage, last tool, final text. */
	private ingest(m: Meta): Meta {
		const path = join(this.dir(m.id), "events.jsonl");
		if (!existsSync(path)) return m;
		const size = statSync(path).size;
		const off = m.events_offset ?? 0;
		if (size <= off) return m;
		const fd = openSync(path, "r");
		const buf = Buffer.alloc(size - off);
		readSync(fd, buf, 0, buf.length, off);
		closeSync(fd);
		const text = buf.toString("utf8");
		const cut = text.lastIndexOf("\n");
		if (cut < 0 && m.kind === "pi") return { ...m, last_event: this.o.now() };
		const n: Meta = { ...m, last_event: this.o.now(), usage: { ...m.usage }, events_offset: off + Buffer.byteLength(m.kind === "pi" ? text.slice(0, cut + 1) : text) };
		if (m.kind !== "pi") return n;
		for (const line of text.slice(0, cut + 1).split("\n")) {
			if (!line.trim()) continue;
			let e: any;
			try {
				e = JSON.parse(line);
			} catch {
				continue;
			}
			if (e.type === "auto_retry_start") {
				n.retries = (n.retries ?? 0) + 1;
				if (RATE_LIMIT.test(String(e.errorMessage ?? ""))) n.rate_limited_at = this.o.now();
			}
			if (e.type === "tool_execution_start") n.last_tool = `${e.toolName}: ${JSON.stringify(e.args ?? {}).slice(0, 120)}`;
			if (e.type === "message_end" && e.message?.role === "assistant") {
				const u = e.message.usage ?? {};
				n.usage.turns++;
				n.usage.input += u.input ?? 0;
				n.usage.output += u.output ?? 0;
				n.usage.cost += u.cost?.total ?? 0;
				const t = (e.message.content ?? []).filter((b: any) => b?.type === "text").map((b: any) => b.text).join("\n").trim();
				if (t) n.final_preview = t.slice(0, 400);
				if (e.message.stopReason === "error") {
					n.model_error = e.message.errorMessage ?? "model error";
					if (RATE_LIMIT.test(n.model_error)) n.rate_limited_at = this.o.now();
				} else delete n.model_error; // a later good message: pi retried and the job recovered
			}
		}
		return n;
	}

	tick() {
		this.tickNo++;
		if (this.tickNo % 15 === 0) this.adoptOrphans();
		const now = this.o.now();
		for (const m0 of this.managed().filter(x => x.state === "running")) {
			if (this.cancelling.has(m0.id)) continue;
			const m = this.ingest(m0);
			this.write(m);
			if (!ours(m)) {
				this.finish(m);
				continue;
			}
			if (m.max_cost !== undefined && m.usage.cost > m.max_cost) void this.cancel(m.id, "killed(cost)");
			else if (now - (m.started ?? now) > m.timeout_s * 1000) void this.cancel(m.id, "timed_out(wall)");
			else if (now - (m.last_event ?? now) > m.idle_s * 1000) void this.cancel(m.id, "timed_out(idle)");
		}
		this.pump();
		this.notifyPending();
	}

	/** Decide the final state of a job whose process is gone, write result.md, notify. */
	private finish(m0: Meta) {
		const m = this.ingest(m0);
		if (!FINAL.has(m.state)) {
			const clean = m.kind === "pi" ? !m.model_error && m.final_preview !== undefined && (m.exit_code ?? 0) === 0 : m.exit_code === 0;
			m.state = clean ? "done" : "failed";
			if (!clean) {
				if (m.model_error && RATE_LIMIT.test(m.model_error))
					m.reason = `rate limited (429) after ${m.retries ?? 0} pi retries. Resume later: ${m.resume ?? "agent_spawn again"}`;
				else m.reason = m.model_error ?? (m.exit_code === undefined || m.exit_code === null ? "process ended without a final answer" : `exit code ${m.exit_code}`);
			}
		}
		m.ended = m.ended ?? this.o.now();
		writeFileSync(join(this.dir(m.id), "result.md"), this.renderResult(m)); // before the terminal meta: a crash between leaves a re-finishable job
		this.write(m);
		this.notifyPending();
	}

	/** Notify the owner's ended jobs. `notified` is written after a successful delivery; a failed delivery is
	 * tried again on the next tick, at most 3 times. */
	private notifyPending() {
		const tickNo = this.tickNo;
		for (const m of this.managed()) {
			if (!FINAL.has(m.state) || m.notified) continue;
			if (this.tried.get(m.id) === tickNo) continue; // one try per job per tick
			this.tried.set(m.id, tickNo);
			if (!existsSync(join(this.dir(m.id), "result.md"))) writeFileSync(join(this.dir(m.id), "result.md"), this.renderResult(m));
			const attempts = (m.notify_attempts ?? 0) + 1;
			let ok = true;
			try {
				this.o.onDone?.(m);
			} catch {
				ok = false; // a failing notifier never breaks the monitor
			}
			this.write({ ...m, notified: ok || attempts >= 3, notify_attempts: attempts, ...(ok ? {} : attempts >= 3 ? { notify_failed: true } : {}) });
		}
	}

	finalText(m: Meta): string {
		const dir = this.dir(m.id);
		if (m.kind !== "pi") {
			const p = join(dir, "events.jsonl");
			const t = existsSync(p) ? readFileSync(p, "utf8") : "";
			return t.slice(-RESULT_CAP);
		}
		let last = "";
		const p = join(dir, "events.jsonl");
		if (existsSync(p)) {
			for (const line of readFileSync(p, "utf8").split("\n")) {
				if (!line.includes('"message_end"')) continue;
				try {
					const e = JSON.parse(line);
					if (e.message?.role !== "assistant") continue;
					const t = (e.message.content ?? []).filter((b: any) => b?.type === "text").map((b: any) => b.text).join("\n").trim();
					if (t) last = t;
				} catch {
					/* partial line */
				}
			}
		}
		return last.length > RESULT_CAP ? `${last.slice(0, RESULT_CAP)}\n[truncated: full text in ${p}]` : last;
	}

	private renderResult(m: Meta): string {
		const err = existsSync(join(this.dir(m.id), "stderr.log")) ? readFileSync(join(this.dir(m.id), "stderr.log"), "utf8").slice(-1500).trim() : "";
		const secs = Math.round(((m.ended ?? this.o.now()) - (m.started ?? m.created)) / 1000);
		return [
			`# job ${m.id} — ${m.state}${m.reason ? ` (${m.reason})` : ""}`,
			`agent ${m.agent} · ${secs} s · ${m.usage.turns} turns · $${m.usage.cost.toFixed(4)}${m.last_tool ? ` · last tool ${m.last_tool}` : ""}`,
			m.resume ? `resume: ${m.resume}` : "",
			"",
			this.finalText(m) || "(no final answer)",
			m.state !== "done" && err ? `\n## stderr (tail)\n${err}` : "",
		].filter(x => x !== "").join("\n");
	}

	// ------------------------------------------------------------------ queries
	status(id?: string): Meta[] {
		this.tick();
		return id ? [this.read(id)].filter((m): m is Meta => m !== null) : this.list();
	}

	result(id: string): { meta: Meta; text: string; path: string } | null {
		const m = this.read(id);
		if (!m) return null;
		const path = join(this.dir(id), "result.md");
		return { meta: m, text: existsSync(path) ? readFileSync(path, "utf8") : `job ${id} is ${m.state}`, path };
	}

	async wait(ids: string[], timeoutMs: number, mode: "all" | "any" = "all", signal?: AbortSignal): Promise<Meta[]> {
		const end = this.o.now() + timeoutMs;
		for (;;) {
			this.tick();
			const ms = ids.map(id => this.read(id)).filter((m): m is Meta => m !== null);
			const done = ms.filter(m => FINAL.has(m.state));
			if (ms.length === 0 || (mode === "any" ? done.length > 0 : done.length === ms.length)) return ms;
			if (this.o.now() >= end || signal?.aborted) return ms;
			await new Promise(r => setTimeout(r, Math.min(500, this.o.tickMs)));
		}
	}

	/** SIGTERM every descendant, wait, SIGKILL every descendant process group. */
	async cancel(id: string, state: JobState = "killed"): Promise<Meta | null> {
		let m = this.read(id);
		if (!m) return null;
		if (!this.manages(m) && !FINAL.has(m.state)) {
			if (this.o.ownerAlive(m)) return m; // another live session owns it: refuse (the caller reports this)
			m = this.claim(m) ?? m;
			if (!this.manages(m)) return m;
		}
		if (m.state === "queued" || m.state === "queued(pressure)") {
			const n: Meta = { ...m, state, reason: "cancelled before start", ended: this.o.now() };
			this.write(n);
			this.finish(n);
			return this.read(id);
		}
		if (FINAL.has(m.state) || !m.pid || this.cancelling.has(id)) return m;
		if (!ours(m)) {
			this.finish(m); // the process is gone (or the pid now belongs to another process): do not kill it
			return this.read(id);
		}
		if (!sure(m)) {
			// identity unknown (no start time): SIGTERM the child only; pi cleans up its own bash groups (P0 #4)
			try {
				process.kill(m.pid, "SIGTERM");
			} catch {
				/* gone */
			}
			const n: Meta = { ...this.ingest(m), state, reason: "cancelled (SIGTERM only: process identity unknown)", ended: this.o.now() };
			this.finish(n);
			return this.read(id);
		}
		this.cancelling.add(id);
		try {
			const before = psTable();
			const tree = treeOf(m.pid, m.pid_start!, before);
			if (tree.length === 0) tree.push({ pid: m.pid, ppid: 0, pgid: -1, start: m.pid_start! }); // no ps table: SIGTERM the child only
			for (const { pid } of tree) {
				try {
					process.kill(pid, "SIGTERM");
				} catch {
					/* gone */
				}
			}
			const until = Date.now() + this.o.graceMs;
			while (Date.now() < until && tree.some(t => alive(t.pid))) await new Promise(r => setTimeout(r, 200));
			// never the manager's own group or its parent's (the user shell)
			const { pgids, pids } = killTargets(tree, before, psTable(), new Set([ownPgid(), ownPgid(process.ppid)]));
			for (const pg of pgids) {
				try {
					process.kill(-pg, "SIGKILL");
				} catch {
					/* gone or not ours */
				}
			}
			for (const pid of pids) {
				try {
					process.kill(pid, "SIGKILL");
				} catch {
					/* gone */
				}
			}
			const cur = this.read(id)!;
			const n: Meta = { ...this.ingest(cur), state, reason: state === "killed" ? "cancelled" : cur.reason ?? state, ended: this.o.now() };
			this.write(n);
			this.finish(n);
			return this.read(id);
		} finally {
			this.cancelling.delete(id);
		}
	}

	/** Take over one orphan job under its lock. Returns the new meta, or null when another manager won. */
	private claim(m: Meta): Meta | null {
		return this.withLock(m.id, () => {
			const cur = this.read(m.id);
			if (!cur || this.manages(cur) || this.o.ownerAlive(cur)) return null;
			const n: Meta = { ...cur, owner: this.self, parent_pid: process.pid };
			this.write(n);
			return n;
		}) ?? null;
	}

	/** After a parent restart: adopt the jobs whose owner process is gone (running, queued, or ended within the
	 * last hour and not yet notified), then monitor them. Returns the adopted jobs. */
	adopt(): Meta[] {
		this.stopped = false;
		JobManager.live.add(this.self);
		const took = this.adoptOrphans();
		this.ensureTimer();
		this.tick();
		return took.map(m => this.read(m.id) ?? m);
	}
	private adoptOrphans(): Meta[] {
		const now = this.o.now();
		const took: Meta[] = [];
		for (const m of this.list()) {
			if (this.manages(m) || this.o.ownerAlive(m)) continue;
			const open = !FINAL.has(m.state) || (!m.notified && now - (m.ended ?? 0) < 3600_000);
			if (!open) continue;
			const n = this.claim(m);
			if (n) took.push(n);
		}
		return took;
	}
	/** @deprecated alias of adopt() */
	reattach(): Meta[] {
		return this.adopt();
	}
}
