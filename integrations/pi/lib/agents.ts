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
 *
 * Cost cap (A1): at SOFT_CAP (85 %) of max_cost the manager writes <dir>/steer.json once and records soft_cap_at.
 * The child's apex-agents extension (APEX_AGENT_JOB_DIR) reads it, steers the model to write the final report now
 * and blocks new tool calls. The hard stop "killed(cost)" fires only above max_cost x SALVAGE (1.10). A job that
 * ends cleanly after the soft cap is "done" with `capped: true` (no new state: every FINAL consumer stays correct).
 * Cancel follows the P0 result: snapshot descendant pids, SIGTERM them, wait, SIGKILL their process groups
 * (pi's bash tool runs commands in their own group, and SIGKILL alone leaves them as orphans).
 */
import { spawn, execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { closeSync, existsSync, fstatSync, mkdirSync, opendirSync, openSync, readFileSync, readSync, readdirSync, renameSync, rmdirSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { basename, join } from "node:path";

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
	/** Spawn gates: the override reason that passed a refusal, and the git place of the cwd (xval review rounds). */
	gate_override?: string;
	repo?: string;
	branch?: string;
	head?: string;
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
	/** Bytes of stderr.log seen (kind != "pi": stderr growth is liveness, not result). */
	stderr_offset?: number;
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
	/** Time the soft-cap message was written to steer.json (85 % of max_cost). */
	soft_cap_at?: number;
	/** Cost when the soft cap fired. */
	soft_cap_cost?: number;
	/** A2: time of the final turn (assistant message with stopReason "stop") after the soft cap. The report exists,
	 *  so a hard cost stop must not kill the job: it ends by itself. */
	final_turn_at?: number;
	/** true on a "done" job that ended after the soft-cap message (the final report is a salvage report). */
	capped?: boolean;
	/** Spawn gates (owner order 2026-10-27): the override reason, if a refusal was passed. */
	gate_override?: string;
	/** git common dir, branch and HEAD of the cwd at spawn (recorded for xval jobs: review rounds). */
	repo?: string;
	branch?: string;
	head?: string;
};

/** Fraction of max_cost that sends the "write the final report now" message. */
export const SOFT_CAP = 0.85;
/** Salvage budget: hard stop only above max_cost x SALVAGE. */
export const SALVAGE = 1.1;
/** A2: after the final turn, the child gets this long to exit by itself before a hard cost stop kills it. */
export const FINAL_EXIT_GRACE_MS = 30_000;
export const STEER_FILE = "steer.json";
export const SOFT_CAP_MESSAGE =
	"COST LIMIT: you used 85 % of your cost budget. Stop the work now. Do not call tools. "
	+ "Write your final report now as your last message: what is done, what is not done, what stays open.";

/** Cost decision for one tick. "soft": send the soft-cap message (once). "hard": stop the job. */
export function costAction(cost: number, maxCost: number | undefined, softSent: boolean): "none" | "soft" | "hard" {
	if (maxCost === undefined || !Number.isFinite(maxCost)) return "none";
	if (cost > maxCost * SALVAGE) return "hard";
	if (!softSent && cost >= maxCost * SOFT_CAP) return "soft";
	return "none";
}

/** Write <dir>/steer.json atomically. Parent and child use this one function: same content shape. */
export function writeSteer(dir: string, at: number, tag: string): void {
	const p = join(dir, STEER_FILE);
	const tmp = `${p}.${tag}.tmp`;
	writeFileSync(tmp, JSON.stringify({ kind: "soft_cap", message: SOFT_CAP_MESSAGE, at }));
	renameSync(tmp, p);
}

/** Child side (C1a): the child steers itself at SOFT_CAP of its own budget, also when the parent runs old code.
 *  Writes steer.json once (only when no steer.json exists). Returns true when it wrote the file. */
export function childSelfSteer(dir: string | undefined, cost: number, maxCost: number | undefined, at: number): boolean {
	if (!dir || costAction(cost, maxCost, false) === "none" || readSteer(dir)) return false;
	writeSteer(dir, at, `child-${process.pid}`);
	return true;
}

/** Child side (F1): the job dir and budget of this child, also under a parent with old code.
 *  Every parent passes APEX_AGENT_JOB (no path separator, not "." or ".."). Dir: APEX_AGENT_JOB_DIR when its last
 *  segment is that id, else <root>/<id>. Budget: APEX_AGENT_MAX_COST only with an owned dir, else max_cost of
 *  <dir>/meta.json (read once). Fail safe: no id, no dir or no budget gives undefined (no self-steer). */
export function childCostCap(env: NodeJS.ProcessEnv, root: string = DEFAULT_ROOT): { dir: string | undefined; maxCost: number | undefined } {
	const id = env.APEX_AGENT_JOB ?? "";
	const okId = id !== "" && id !== "." && id !== ".." && !/[/\\]/.test(id);
	if (!okId) return { dir: undefined, maxCost: undefined };
	// A parent from before 3796382 copies its env as is: APEX_AGENT_JOB_DIR / APEX_AGENT_MAX_COST can be the
	// grandparent's. Trust them only when the dir belongs to this job (its last segment is APEX_AGENT_JOB).
	const envDir = env.APEX_AGENT_JOB_DIR;
	const owned = !!envDir && basename(envDir) === id;
	const dir = owned ? envDir : join(root, id);
	const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
	const envCost = owned && env.APEX_AGENT_MAX_COST ? num(Number(env.APEX_AGENT_MAX_COST)) : undefined;
	if (envCost !== undefined) return { dir, maxCost: envCost };
	try {
		return { dir, maxCost: num(JSON.parse(readFileSync(join(dir, "meta.json"), "utf8"))?.max_cost) };
	} catch {
		return { dir, maxCost: undefined };
	}
}

/** SHA-256 of the files (a missing file hashes as empty). C1b: detects extension code that changed after load. */
export function codeHash(paths: string[]): string {
	const h = createHash("sha256");
	for (const p of paths) {
		h.update(p);
		try {
			h.update(readFileSync(p));
		} catch {
			h.update("\0missing");
		}
	}
	return h.digest("hex");
}
/** M1: an xval job (codex) reports no USD usage, so the manager cannot enforce max_cost: the budget was ignored
 *  in silence (25 of 251 xval jobs set one). The budget is dropped and the caller gets this note. Pure. */
export const XVAL_NO_COST_NOTE = "max_cost ignored: xval jobs report no USD cost, so no budget is enforced. Use timeout_s.";
export function effectiveMaxCost(kind: string | undefined, maxCost: number | undefined): { maxCost: number | undefined; note: string } {
	if (kind === "xval" && maxCost !== undefined) return { maxCost: undefined, note: XVAL_NO_COST_NOTE };
	return { maxCost, note: "" };
}
export const STALE_CODE_NOTE = "pi extension code changed since load; run /reload.";

/** Child-side gate for one tool call. After the soft cap: block the call; steer the model once. */
export function softCapGate(dir: string | undefined, steered: boolean): { block: false } | { block: true; reason: string; steer: boolean } {
	const s = readSteer(dir);
	if (!s) return { block: false };
	return { block: true, reason: s.message, steer: !steered };
}

/** The soft-cap steer of a job dir, or null. Used by the child's extension (pure: tests call it). */
export function readSteer(dir: string | undefined): { kind: string; message: string; at: number } | null {
	if (!dir) return null;
	try {
		const s = JSON.parse(readFileSync(join(dir, STEER_FILE), "utf8"));
		return typeof s?.message === "string" ? s : null;
	} catch {
		return null;
	}
}

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

/** Env of a child job. The child's spawn limit comes ONLY from its own agent definition, never from the
 *  inherited env: a plain child (no maxDepth) gets max = its depth, so it cannot spawn; a child with
 *  maxDepth N may nest N - 1 more levels. Hard ceiling MAX_TREE_DEPTH, and bad inputs mean "leaf". */
export const MAX_TREE_DEPTH = 3;
export function childEnv(env: NodeJS.ProcessEnv, jobId: string, maxDepth?: number, jobDir?: string, maxCost?: number): NodeJS.ProcessEnv {
	const parent = Number(env.APEX_AGENT_DEPTH ?? "0");
	const depth = Math.min((Number.isInteger(parent) && parent >= 0 ? parent : MAX_TREE_DEPTH) + 1, MAX_TREE_DEPTH + 1);
	const extra = maxDepth !== undefined && Number.isInteger(maxDepth) && maxDepth > 1 ? maxDepth - 1 : 0;
	const max = Math.min(depth + extra, MAX_TREE_DEPTH);
	const out: NodeJS.ProcessEnv = { ...env, APEX_AGENT_DEPTH: String(depth), APEX_AGENT_MAX_DEPTH: String(max), APEX_AGENT_JOB: jobId };
	if (jobDir) out.APEX_AGENT_JOB_DIR = jobDir;
	else delete out.APEX_AGENT_JOB_DIR; // never inherit the parent's job dir (it would read the parent's steer)
	if (maxCost !== undefined && Number.isFinite(maxCost)) out.APEX_AGENT_MAX_COST = String(maxCost);
	else delete out.APEX_AGENT_MAX_COST; // never inherit the parent's budget
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
			...(spec.gate_override !== undefined ? { gate_override: spec.gate_override } : {}),
			...(spec.repo !== undefined ? { repo: spec.repo } : {}),
			...(spec.branch !== undefined ? { branch: spec.branch } : {}),
			...(spec.head !== undefined ? { head: spec.head } : {}),
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
				env: childEnv(process.env, m.id, m.max_depth, dir, m.max_cost),
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
	private ingest(m0: Meta): Meta {
		const m = this.ingestStderr(m0);
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
				// A2: a final turn after the soft cap = the report is written; the cost of THIS message can pass 110 %
				if (e.message.stopReason === "stop" && t && n.soft_cap_at !== undefined && n.final_turn_at === undefined) n.final_turn_at = this.o.now();
				if (e.message.stopReason === "error") {
					n.model_error = e.message.errorMessage ?? "model error";
					if (RATE_LIMIT.test(n.model_error)) n.rate_limited_at = this.o.now();
				} else delete n.model_error; // a later good message: pi retried and the job recovered
			}
		}
		return n;
	}

	/** C9: kind != "pi" (xval / codex) writes its progress to stderr.log. Growth there is liveness only. */
	private ingestStderr(m: Meta): Meta {
		if (m.kind === "pi") return m;
		const p = join(this.dir(m.id), "stderr.log");
		let size: number;
		try {
			size = statSync(p).size;
		} catch {
			return m; // F2: missing during a rotate: no throw, no change
		}
		// F2: growth, or a shrink (truncate / rotate / replace) resets the offset; both count as activity
		return size !== (m.stderr_offset ?? 0) ? { ...m, last_event: this.o.now(), stderr_offset: size } : m;
	}

	/** F3: the child can write steer.json itself (C1a). Record soft_cap_at from it, so finish() sees the cap.
	 *  The parent stays the only meta.json writer. */
	private syncSteer(m: Meta): Meta {
		if (m.soft_cap_at !== undefined) return m;
		const s = readSteer(this.dir(m.id));
		return s && typeof s.at === "number" ? { ...m, soft_cap_at: s.at } : m;
	}

	tick() {
		this.tickNo++;
		if (this.tickNo % 15 === 0) this.adoptOrphans();
		const now = this.o.now();
		for (const m0 of this.managed().filter(x => x.state === "running")) {
			if (this.cancelling.has(m0.id)) continue;
			const m = this.syncSteer(this.ingest(m0));
			this.write(m);
			if (!ours(m)) {
				this.finish(m);
				continue;
			}
			const act = costAction(m.usage.cost, m.max_cost, m.soft_cap_at !== undefined);
			// A2: the final report exists (final turn after the soft cap): let the child exit; kill only after a grace
			const reported = m.final_turn_at !== undefined && now - m.final_turn_at < FINAL_EXIT_GRACE_MS;
			if (act === "hard" && !reported) void this.cancel(m.id, "killed(cost)");
			else if (act === "hard") { /* wait for the child to exit after its final report */ }
			else if (act === "soft") this.softCap(m);
			else if (now - (m.started ?? now) > m.timeout_s * 1000) void this.cancel(m.id, "timed_out(wall)");
			else if (now - (m.last_event ?? now) > m.idle_s * 1000) void this.cancel(m.id, "timed_out(idle)");
		}
		this.pump();
		this.notifyPending();
	}

	/** Write the soft-cap message for the child once, and record soft_cap_at. No text goes into meta.json. */
	private softCap(m: Meta) {
		const now = this.o.now();
		writeSteer(this.dir(m.id), now, this.self);
		this.write({ ...m, soft_cap_at: now, soft_cap_cost: m.usage.cost });
	}

	/** Decide the final state of a job whose process is gone, write result.md, notify. */
	private finish(m0: Meta) {
		const m = this.syncSteer(this.ingest(m0));
		if (!FINAL.has(m.state)) {
			const clean = m.kind === "pi" ? !m.model_error && m.final_preview !== undefined && (m.exit_code ?? 0) === 0 : m.exit_code === 0;
			m.state = clean ? "done" : "failed";
			if (clean && m.soft_cap_at !== undefined) {
				m.capped = true;
				m.reason = "capped: final report after the cost soft cap";
			}
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

/** A2 advice only (same rule as src/apex_router/cost_advice.py): done-cost p75/p90 for ONE (agent, model, thinking)
 *  cell, n >= COST_ADVICE_MIN_N, no pooled threshold. Never changes max_cost. */
export const COST_ADVICE_MIN_N = 8;
export const AGENT_RUNS = join(homedir(), ".apex-router", "labels", "agent_runs.jsonl");
export function costAdvice(rows: unknown[], agent: string, model: string | undefined, thinking: string | undefined): { n: number; p75?: number; p90?: number } {
	const xs = rows
		.filter((r: any) => r && r.state === "done" && r.capped !== true && r.agent === agent && r.model === (model ?? null) && r.thinking === (thinking ?? null))
		.map((r: any) => r.cost_usd)
		.filter((c: unknown): c is number => typeof c === "number" && c > 0)
		.sort((a, b) => a - b);
	if (xs.length < COST_ADVICE_MIN_N) return { n: xs.length };
	const pct = (q: number) => {
		const k = (xs.length - 1) * q;
		const lo = Math.floor(k);
		const hi = Math.min(lo + 1, xs.length - 1);
		return Math.round((xs[lo]! + (xs[hi]! - xs[lo]!) * (k - lo)) * 1e4) / 1e4;
	};
	return { n: xs.length, p75: pct(0.75), p90: pct(0.9) };
}
/** The advice line when max_cost is below the cell p75, else "". Any read error gives "". */
export function costAdviceLine(maxCost: number | undefined, agent: string, model: string | undefined, thinking: string | undefined, path = AGENT_RUNS): string {
	if (maxCost === undefined) return "";
	try {
		const rows = readFileSync(path, "utf8").split("\n").flatMap(l => {
			try {
				return l.trim() ? [JSON.parse(l)] : [];
			} catch {
				return [];
			}
		});
		const a = costAdvice(rows, agent, model, thinking);
		if (a.p75 === undefined || maxCost >= a.p75) return "";
		return `cost advice (no change made): max_cost $${maxCost.toFixed(2)} is below the done-cost p75 $${a.p75.toFixed(2)} (p90 $${a.p90!.toFixed(2)}, n=${a.n}) of ${agent}/${model}/${thinking}.`;
	} catch {
		return "";
	}
}

// ------------------------------------------------------------------ spawn gates (owner order 2026-10-27)
/** Backpressure: a worker or coordinator does not start when GATE_PENDING_LIMIT branches wait for review or merge
 *  in the target repo, or GATE_DECISION_LIMIT owner decisions are open. Review rounds: a new xval on a branch with
 *  ROUND_LIMIT earlier xval NO-GO results is refused. An override reason (>= OVERRIDE_MIN chars) passes one spawn.
 *  Fail open: a check that fails (git error, timeout, bad file) allows the spawn and adds one warning line.
 *  Bounded: every git call has a timeout from one budget (GATE_BUDGET_MS); reads are capped; the job scan reads
 *  the newest JOB_SCAN_CAP names. Pure: git and file access are injected, so tests drive them. */
export const GATED_AGENTS: ReadonlySet<string> = new Set(["worker", "coordinator"]);
export const GATE_PENDING_LIMIT = 3;
export const GATE_DECISION_LIMIT = 5;
export const ROUND_LIMIT = 3;
export const PENDING_MAX_AGE_MS = 7 * 86_400_000;
export const OVERRIDE_MIN = 10;
export const OVERRIDE_CAP = 300;
export const GATE_BUDGET_MS = 800;
export const JOB_SCAN_CAP = 2000;
/** Hard ceiling of directory entries read before the sort (a huge folder never reads without end). */
export const JOB_LIST_CEILING = 20_000;
export const META_READ_CAP = 256 * 1024;
export const RESULT_TAIL_CAP = 4096;
export const DECISIONS_READ_CAP = 1 << 20;
export const DECISIONS_PATH = join(homedir(), ".apex-router", "decisions.jsonl");
export const ROUND_MESSAGE = "send the open finding to the owner as one question";
export const EVIDENCE_LINE = "A P1 counts only with evidence: a line number and a concrete input or a failing test. A GO lists the claims you checked.";

/** Append the fixed evidence line to an xval task (once). */
export function withEvidence(task: string): string {
	return task.includes(EVIDENCE_LINE) ? task : `${task.trimEnd()}\n\n${EVIDENCE_LINE}`;
}

/** Runs git with these args; throws on a non-zero exit or at the timeout. Returns stdout. */
export type GitRun = (args: string[], timeoutMs: number) => string;
export const runGit = (args: string[], timeoutMs: number, bin = "git"): string =>
	execFileSync(bin, args, {
		encoding: "utf8", timeout: Math.max(1, Math.floor(timeoutMs)), killSignal: "SIGKILL", stdio: ["ignore", "pipe", "pipe"], maxBuffer: 4 << 20,
		env: { ...process.env, GIT_OPTIONAL_LOCKS: "0", GIT_TERMINAL_PROMPT: "0", LC_ALL: "C" },
	});

export type GateIO = {
	exists: (p: string) => boolean;
	/** At most `cap` bytes from the start (or the end with `tail`). null when the file does not exist. */
	read: (p: string, cap: number, tail?: boolean) => { text: string; truncated: boolean } | null;
	/** Names in a folder, at most `ceiling`. null when the folder does not exist. */
	list: (dir: string, ceiling: number) => { names: string[]; capped: boolean } | null;
};

export const FS_IO: GateIO = {
	exists: p => existsSync(p),
	read: (p, cap, tail = false) => {
		let fd: number;
		try {
			fd = openSync(p, "r");
		} catch (e) {
			if ((e as NodeJS.ErrnoException).code === "ENOENT") return null;
			throw e;
		}
		try {
			const st = fstatSync(fd);
			if (!st.isFile()) throw new Error(`not a file: ${p}`);
			const n = Math.min(st.size, cap);
			const buf = Buffer.alloc(n);
			const got = readSync(fd, buf, 0, n, tail ? st.size - n : 0);
			return { text: buf.toString("utf8", 0, got), truncated: st.size > cap };
		} finally {
			closeSync(fd);
		}
	},
	list: (dir, ceiling) => {
		let d;
		try {
			d = opendirSync(dir);
		} catch (e) {
			if ((e as NodeJS.ErrnoException).code === "ENOENT") return null;
			throw e;
		}
		const names: string[] = [];
		try {
			for (let e = d.readSync(); e !== null; e = d.readSync()) {
				if (names.length >= ceiling) return { names, capped: true };
				names.push(e.name);
			}
		} finally {
			d.closeSync();
		}
		return { names, capped: false };
	},
};

const notRepo = (e: unknown): boolean => /not a git repository/i.test(`${(e as { stderr?: unknown }).stderr ?? ""} ${(e as Error)?.message ?? ""}`);
const left = (deadline: number): number => {
	const ms = deadline - Date.now();
	if (ms <= 0) throw new Error("time budget used");
	return ms;
};

/** Pending branches of the repo of `cwd`: apexjob/* with a live worktree, at least 1 commit not in the base branch
 *  (main, else master) and a last commit under 7 days old. null when cwd is not in a git repo. Throws on other errors. */
export function pendingBranches(cwd: string, git: GitRun, exists: (p: string) => boolean, now: number, deadline: number): string[] | null {
	let wl: string;
	try {
		wl = git(["-C", cwd, "worktree", "list", "--porcelain"], left(deadline));
	} catch (e) {
		if (notRepo(e)) return null;
		throw e;
	}
	const live = new Set<string>();
	for (const block of wl.split(/\n\s*\n/)) {
		const path = /^worktree (.+)$/m.exec(block)?.[1];
		const ref = /^branch refs\/heads\/(apexjob\/.+)$/m.exec(block)?.[1];
		if (path && ref && !/^prunable\b/m.test(block) && exists(path)) live.add(ref);
	}
	if (live.size === 0) return [];
	const base = git(["-C", cwd, "for-each-ref", "--format=%(refname)", "refs/heads/main", "refs/heads/master"], left(deadline))
		.split("\n").map(s => s.trim()).filter(Boolean).sort()[0]; // "refs/heads/main" sorts before ".../master"
	if (!base) throw new Error("no base branch (main or master)");
	const out = git(["-C", cwd, "for-each-ref", `--no-merged=${base}`, "--format=%(refname) %(committerdate:unix)", "refs/heads/apexjob/"], left(deadline));
	const pending: string[] = [];
	for (const l of out.split("\n")) {
		const m = /^refs\/heads\/(\S+) (\d+)$/.exec(l.trim());
		if (m && live.has(m[1]!) && now - Number(m[2]) * 1000 < PENDING_MAX_AGE_MS) pending.push(m[1]!);
	}
	return pending.sort();
}

/** git common dir (one value for every worktree of a repo), branch (undefined when detached) and HEAD of cwd.
 *  null when cwd is not in a git repo. */
export function headInfo(cwd: string, git: GitRun, timeoutMs: number): { repo: string; branch?: string; head: string } | null {
	let out: string;
	try {
		out = git(["-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir", "HEAD", "--abbrev-ref", "HEAD"], timeoutMs);
	} catch (e) {
		if (notRepo(e)) return null;
		throw e;
	}
	const [repo, head, ref] = out.split("\n").map(s => s.trim());
	if (!repo || !head || !/^[0-9a-f]{40,64}$/.test(head)) throw new Error("git rev-parse: unexpected output");
	return { repo, head, ...(ref && ref !== "HEAD" ? { branch: ref } : {}) };
}

/** true when the last non-empty line (markdown marks removed) ends in NO-GO. */
export function endsNoGo(text: string): boolean {
	const last = text.split("\n").map(l => l.replace(/[*_`#>|]/g, "").trim()).filter(Boolean).pop() ?? "";
	return /\bNO-GO[.!]?$/i.test(last);
}

const JOB_ID = /^\d{14}-[A-Za-z0-9]+$/;
/** Earlier xval jobs on (repo, branch) whose result.md ends in NO-GO. Reads the newest JOB_SCAN_CAP job names only.
 *  skipped: unreadable or too large meta.json. partial: the time budget ended before the scan did. */
export function reviewRounds(root: string, repo: string, branch: string, io: GateIO, deadline: number): { nogo: string[]; skipped: number; partial: boolean; capped: boolean } {
	const r = { nogo: [] as string[], skipped: 0, partial: false, capped: false };
	const ls = io.list(root, JOB_LIST_CEILING);
	if (!ls) return r;
	r.capped = ls.capped;
	const ids = ls.names.filter(n => JOB_ID.test(n)).sort().slice(-JOB_SCAN_CAP).reverse();
	for (const id of ids) {
		if (Date.now() >= deadline) {
			r.partial = true;
			break;
		}
		let m: { kind?: unknown; repo?: unknown; branch?: unknown };
		try {
			const f = io.read(join(root, id, "meta.json"), META_READ_CAP);
			if (!f) continue; // a folder without meta.json is not a job
			if (f.truncated) {
				r.skipped++;
				continue;
			}
			m = JSON.parse(f.text);
		} catch {
			r.skipped++;
			continue;
		}
		if (m?.kind !== "xval" || m.repo !== repo || m.branch !== branch) continue;
		try {
			const res = io.read(join(root, id, "result.md"), RESULT_TAIL_CAP, true);
			if (res && endsNoGo(res.text)) r.nogo.push(id);
		} catch {
			r.skipped++;
		}
	}
	return r;
}

export type Decision = { id: string; ts?: number; topic?: string; text?: string; state: "open" | "closed" };
/** Open decisions: the latest row per id wins. A bad row is skipped and counted. Reads at most the last
 *  DECISIONS_READ_CAP bytes (a cut first line is dropped, not counted). A missing file is 0 decisions. Throws on a read error. */
export function openDecisions(path: string, io: GateIO): { open: Decision[]; bad: number } {
	const f = io.read(path, DECISIONS_READ_CAP, true);
	if (!f) return { open: [], bad: 0 };
	const lines = f.text.split("\n");
	if (f.truncated) lines.shift();
	const latest = new Map<string, Decision>();
	let bad = 0;
	for (const l of lines) {
		if (!l.trim()) continue;
		let r: any;
		try {
			r = JSON.parse(l);
		} catch {
			bad++;
			continue;
		}
		if (!r || typeof r !== "object" || typeof r.id !== "string" || !r.id || (r.state !== "open" && r.state !== "closed")) {
			bad++;
			continue;
		}
		latest.delete(r.id);
		latest.set(r.id, r);
	}
	return { open: [...latest.values()].filter(d => d.state === "open"), bad };
}

export type GateInput = {
	agent: string;
	kind: string;
	cwd: string;
	override?: string;
	decisionsPath?: string;
	jobsRoot?: string;
	git?: GitRun;
	io?: GateIO;
	now?: number;
	budgetMs?: number;
};
export type GateResult = {
	allow: boolean;
	/** The refusal text (empty when allowed). Names the branches, decisions or review rounds. */
	message: string;
	/** One line per failed check (fail open) or note. */
	warnings: string[];
	/** The override reason, only when it passed a refusal (goes into meta.json as gate_override). */
	override?: string;
	/** git place of cwd (xval only). */
	head?: { repo: string; branch?: string; head: string };
};

const short = (s: unknown, n: number) => String(s ?? "").replace(/\s+/g, " ").slice(0, n);

/** All spawn gates for one agent_spawn. Never throws. */
export function spawnGate(inp: GateInput): GateResult {
	const git = inp.git ?? ((a, t) => runGit(a, t));
	const io = inp.io ?? FS_IO;
	const now = inp.now ?? Date.now();
	const deadline = Date.now() + (inp.budgetMs ?? GATE_BUDGET_MS);
	const warnings: string[] = [];
	const reasons: string[] = [];
	const fail = (what: string, e: unknown) => warnings.push(`spawn gate check failed (${what}: ${short((e as Error)?.message ?? e, 120)}); the spawn is allowed.`);
	let head: GateResult["head"];

	if (GATED_AGENTS.has(inp.agent)) {
		try {
			const p = pendingBranches(inp.cwd, git, io.exists, now, deadline);
			if (p && p.length >= GATE_PENDING_LIMIT)
				reasons.push(`${p.length} branches wait for review or merge (limit ${GATE_PENDING_LIMIT}): ${p.join(", ")}. Review, merge or remove them first.`);
		} catch (e) {
			fail("pending branches", e);
		}
		try {
			const d = openDecisions(inp.decisionsPath ?? DECISIONS_PATH, io);
			if (d.bad > 0) warnings.push(`decisions: ${d.bad} bad row(s) skipped.`);
			if (d.open.length >= GATE_DECISION_LIMIT)
				reasons.push(`${d.open.length} owner decisions are open (limit ${GATE_DECISION_LIMIT}): ${d.open.map(x => `${x.id} [${short(x.topic, 40)}] ${short(x.text, 60)}`).join("; ")}. Close them first: apex-router decisions close ID.`);
		} catch (e) {
			fail("decisions", e);
		}
	}

	if (inp.kind === "xval") {
		try {
			head = headInfo(inp.cwd, git, left(deadline)) ?? undefined;
			if (head?.branch) {
				const r = reviewRounds(inp.jobsRoot ?? DEFAULT_ROOT, head.repo, head.branch, io, deadline);
				if (r.skipped || r.partial || r.capped) warnings.push(`review rounds: scan incomplete (${r.skipped} job(s) skipped${r.partial ? ", time budget used" : ""}${r.capped ? ", job list capped" : ""}).`);
				if (r.nogo.length >= ROUND_LIMIT)
					reasons.push(`branch ${head.branch} has ${r.nogo.length} earlier xval rounds that ended in NO-GO (limit ${ROUND_LIMIT}): ${r.nogo.slice(0, 10).join(", ")}. Do not start round ${r.nogo.length + 1}: ${ROUND_MESSAGE}.`);
			}
		} catch (e) {
			fail("review rounds", e);
		}
	}

	if (reasons.length === 0) return { allow: true, message: "", warnings, ...(head ? { head } : {}) };
	const why = (inp.override ?? "").trim();
	if (why.length >= OVERRIDE_MIN) {
		const reason = why.slice(0, OVERRIDE_CAP);
		warnings.push(`spawn gate override: ${reasons.length} refusal(s) passed; reason recorded in meta.json as gate_override.`);
		return { allow: true, message: "", warnings, override: reason, ...(head ? { head } : {}) };
	}
	return {
		allow: false,
		message: `agent_spawn refused (owner order 2026-10-27):\n- ${reasons.join("\n- ")}\nTo pass this one spawn, give override with a reason of ${OVERRIDE_MIN} characters or more.`,
		warnings,
		...(head ? { head } : {}),
	};
}
