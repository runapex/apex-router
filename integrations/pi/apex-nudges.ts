/**
 * apex-nudges — three advisory session-hygiene nudges for pi, ported from the
 * Claude Code "Stop" hooks to pi's `turn_end` event.
 *
 * Ports (same INTENT, reusing the SAME Python engines by shelling out — no logic
 * reimplemented in TS):
 *   1. codeqa-freshness  (~/.claude/hooks/codeqa-freshness-check.sh)
 *        cheap gate (a .py/.rb file under a watched repo changed in the last 30 min)
 *        → background `codeqa validate --all --route` → warn if a stale-claim signal
 *          appears in the output.
 *   2. cache-handoff     (~/.claude/hooks/cache-handoff-nudge.sh, SIMPLIFIED)
 *        pi exposes no Claude transcript/telemetry, so the richer cache-read-token
 *        signal the Claude hook uses is NOT available here. This is the TURN-COUNT
 *        FALLBACK: a per-session turn counter; nudge once past a threshold.
 *   3. memory-compact    (~/.claude/hooks/memory-compact-nudge.sh)
 *        if the project memory index is large (bytes or file count), background-run
 *        the proposer (memory_compact.py, NO --apply — proposal only, never mutates)
 *        and nudge once that a compaction proposal is available.
 *
 * RUN-NOW / NOTIFY-NEXT-TURN (pi ctx is TURN-SCOPED!): pi replaces the extension
 * `ctx` after each turn, so a captured `ctx` used inside a deferred `execFile`
 * callback is STALE and `ctx.ui.notify` THROWS ("ctx is stale after session
 * replacement or reload"). Contract: ALL `ctx.ui.notify` calls happen
 * SYNCHRONOUSLY inside the turn_end handler; a fire-and-forget subprocess callback
 * may do ctx-FREE work ONLY. So the two subprocess-backed checks (codeqa-freshness,
 * memory-compact) use the SAME pattern the Claude Stop hooks use: a background run
 * WRITES its verdict to a small per-session result file, and a LATER turn READS
 * that file and notifies synchronously. Pi turn_end fires every turn, so "next
 * turn" always comes. cache-handoff notifies synchronously in-handler (no
 * subprocess) — that one is safe as-is.
 *
 * All three are ADVISORY / FAIL-OPEN / NON-BLOCKING and fire AT MOST ONCE per
 * session per check:
 *   - the turn is never delayed by a model/subprocess (all heavy work is
 *     fire-and-forget via execFile with a swallowing, ctx-FREE callback);
 *   - a missing binary / dir / file → silent skip;
 *   - nothing is ever mutated (validate is read-only; memory_compact runs WITHOUT
 *     --apply; cache-handoff only notifies).
 *
 * MACHINE-LOCAL DEFAULTS: the paths below (ornith checkout, watched repos, the
 * memory_compact engine location) default to conventional ~/dev locations and are
 * each env-overridable — mirror how apex-route.ts
 * lets APEX_HOME / APEX_ROUTER_BIN be overridden — so this file stays portable in
 * the public repo. Env overrides:
 *   APEX_PI_ORNITH_DIR          (default ~/dev/ml/ornith)
 *   APEX_PI_WATCH_DIRS          (comma-separated; default ~/dev/apex,~/dev/avenger)
 *   CACHE_HANDOFF_MSG_THRESHOLD (default 200)
 *   APEX_PI_MEMORY_DIR          (no default — set it to a Claude project's memory dir to enable the memory-compact nudge; unset = that check self-skips)
 *   MEMORY_COMPACT_INDEX_BYTES  (default 8192)
 *   MEMORY_COMPACT_FILE_COUNT   (default 50)
 *   MEMORY_COMPACT_ENGINE       (default ~/dev/apex-router/scripts/memory_compact.py)
 *   MEMORY_COMPACT_PYTHON       (default python3)
 *   APEX_PI_NUDGE_DIR           (result files; default ~/.apex-router/pi-nudges)
 *
 * Install:  pi install ~/.apex-router/integrations/pi/apex-nudges.ts
 */

import { execFile } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

// ---- machine-local defaults (all env-overridable) --------------------------
const ORNITH_DIR = process.env.APEX_PI_ORNITH_DIR || join(homedir(), "dev", "ml", "ornith");
const WATCH_DIRS = process.env.APEX_PI_WATCH_DIRS
	? process.env.APEX_PI_WATCH_DIRS.split(",").map((s) => s.trim()).filter(Boolean)
	: [join(homedir(), "dev", "apex"), join(homedir(), "dev", "avenger")];
// No portable default: a Claude project's memory dir is `~/.claude/projects/<slug>/memory`
// where <slug> is machine-specific. Set APEX_PI_MEMORY_DIR to enable the memory-compact nudge;
// left unset, that check self-skips (see maybeMemoryCompact). Keeps this file free of any one
// machine's path in the public repo.
const MEMORY_DIR = process.env.APEX_PI_MEMORY_DIR || "";
const MEMORY_ENGINE = process.env.MEMORY_COMPACT_ENGINE
	|| join(homedir(), "dev", "apex-router", "scripts", "memory_compact.py");
const MEMORY_PYTHON = process.env.MEMORY_COMPACT_PYTHON || "python3";
const NUDGE_DIR = process.env.APEX_PI_NUDGE_DIR || join(homedir(), ".apex-router", "pi-nudges");

function envInt(name: string, dflt: number): number {
	const v = process.env[name];
	if (!v) return dflt;
	const n = parseInt(v, 10);
	return Number.isFinite(n) && n > 0 ? n : dflt;
}
const CACHE_HANDOFF_MSG_THRESHOLD = envInt("CACHE_HANDOFF_MSG_THRESHOLD", 200);
const MEMORY_COMPACT_INDEX_BYTES = envInt("MEMORY_COMPACT_INDEX_BYTES", 8192);
const MEMORY_COMPACT_FILE_COUNT = envInt("MEMORY_COMPACT_FILE_COUNT", 50);

// ---- once-per-session guards (per process) ---------------------------------
// A pi turn_end fires EVERY turn (unlike Claude's Stop). Two DISTINCT guards,
// each keyed `${sessionId}:${check}`:
//   fired   — a check has NOTIFIED this session (the synchronous notify fires once);
//   started — a check's background subprocess has been SPAWNED this session (so we
//             don't re-run codeqa/memory_compact every single turn).
const fired = new Set<string>();
const started = new Set<string>();
const FRESHNESS = "codeqa-freshness";
const CACHE = "cache-handoff";
const MEMORY = "memory-compact";
const key = (sid: string, check: string) => `${sid}:${check}`;

// Per-session turn counter — the cache-handoff turn-count fallback signal.
const turnCounts = new Map<string, number>();

// ---- per-session result files (run-now / notify-next-turn) -----------------
// The session id feeds a filename, so sanitize to a safe basename (pi ids are
// already tame, but never trust an id as a path component).
const safeSid = (sid: string) => sid.replace(/[^A-Za-z0-9._-]/g, "_");
const resultPath = (sid: string, check: string) => join(NUDGE_DIR, `${check}-${safeSid(sid)}.result`);
const STALE_MARKER = "PI_NUDGE_STALE";
const PROPOSAL_MARKER = "PI_NUDGE_PROPOSAL";

function readResult(path: string): string {
	try {
		return readFileSync(path, "utf8");
	} catch {
		return ""; // no prior run yet → nothing to notify
	}
}

function writeResult(path: string, content: string): void {
	try {
		mkdirSync(dirname(path), { recursive: true });
		writeFileSync(path, content);
	} catch {
		// a result we can't persist just means no notify next turn — never throw
	}
}

/**
 * 1. codeqa-freshness — cheap gate then background validate. The background run
 * WRITES its verdict to a per-session result file (ctx-FREE callback); a LATER
 * turn reads that file and notifies SYNCHRONOUSLY. Warn only on a real stale
 * signal (the ✗ detail lines or a NONZERO stale count).
 */
function checkCodeqaFreshness(sid: string, ctx: ExtensionContext): void {
	// (1) Notify from a PRIOR run's result — SYNCHRONOUS, ctx is valid here.
	if (!fired.has(key(sid, FRESHNESS))) {
		const res = readResult(resultPath(sid, FRESHNESS));
		if (res.startsWith(STALE_MARKER)) {
			fired.add(key(sid, FRESHNESS));
			ctx.ui.notify(
				"⚠ codeqa: a recent code change may have made an architecture digest "
					+ "stale — run `codeqa validate --all --route` to see which claims to refresh.",
				"warning",
			);
		}
	}
	// (2) Kick off THIS turn's background run — callback MUST NOT touch ctx.
	if (started.has(key(sid, FRESHNESS))) return;
	if (!existsSync(ORNITH_DIR)) return; // codeqa not present → nothing to do
	const repos = WATCH_DIRS.filter((d) => existsSync(join(d, ".git")));
	if (repos.length === 0) return;
	// Cheap gate: any .py/.rb under a watched repo modified in the last 30 min?
	// (execFile → no shell, so the find grouping tokens are literal args.)
	execFile(
		"find",
		[...repos, "(", "-name", "*.py", "-o", "-name", "*.rb", ")", "-mmin", "-30"],
		{ timeout: 15_000, maxBuffer: 1 << 20 },
		(_gateErr, gateOut) => {
			if (!gateOut || !gateOut.trim()) return; // no recent product-code change
			if (started.has(key(sid, FRESHNESS))) return; // raced with another turn
			started.add(key(sid, FRESHNESS)); // claim BEFORE the expensive validate run
			execFile(
				"uv",
				["run", "python", "-m", "codeqa.cli", "validate", "--all", "--route"],
				{
					cwd: ORNITH_DIR,
					env: { ...process.env, PYTHONPATH: ORNITH_DIR },
					timeout: 300_000,
					maxBuffer: 4 << 20,
				},
				(_err, out) => {
					// ctx-FREE: write the verdict for a LATER turn to read + notify. A stale
					// signal is the ✗ detail lines or a NONZERO stale count — never the benign
					// "0 stale claim(s)" summary. Prepend the marker line iff stale.
					const text = out || "";
					const stale = /^ *✗|[1-9][0-9]* stale claim/m.test(text);
					writeResult(resultPath(sid, FRESHNESS), stale ? `${STALE_MARKER}\n${text}` : text);
				},
			);
		},
	);
}

/**
 * 2. cache-handoff (turn-count fallback) — pi has no cache-read-token telemetry,
 * so we count turns per session and nudge once past the threshold. This notify is
 * SYNCHRONOUS (no subprocess), so it stays in-handler and ctx is always valid.
 * Documented limitation: the Claude hook's richer cache-read-token signal is NOT
 * available in pi, so this is a coarser turn-count proxy for the same "prefix is
 * expensive now" intent.
 */
function checkCacheHandoff(sid: string, ctx: ExtensionContext): void {
	const n = (turnCounts.get(sid) ?? 0) + 1;
	turnCounts.set(sid, n);
	if (n < CACHE_HANDOFF_MSG_THRESHOLD) return;
	if (fired.has(key(sid, CACHE))) return;
	fired.add(key(sid, CACHE));
	ctx.ui.notify(
		`apex-nudge: this session has ${n} turns — its growing prefix is re-read every `
			+ "turn (prefix cost grows with session length); consider starting a fresh pi "
			+ "session to cap that growth.",
		"info",
	);
}

/**
 * 3. memory-compact — if the project memory index is large (bytes OR file count),
 * background-run the proposer (read-only, NO --apply) which WRITES a "proposal
 * available" result file (ctx-FREE callback); a LATER turn reads it and notifies
 * SYNCHRONOUSLY. Missing engine/dir → silent skip.
 */
function checkMemoryCompact(sid: string, ctx: ExtensionContext): void {
	// (1) Notify from a PRIOR run's result — SYNCHRONOUS, ctx is valid here.
	if (!fired.has(key(sid, MEMORY))) {
		const res = readResult(resultPath(sid, MEMORY));
		if (res.startsWith(PROPOSAL_MARKER)) {
			fired.add(key(sid, MEMORY));
			const msg = res.slice(PROPOSAL_MARKER.length).trim();
			ctx.ui.notify(msg || "apex-nudge: a project-memory compaction proposal is available.", "info");
		}
	}
	// (2) Kick off THIS turn's background run — callback MUST NOT touch ctx.
	if (started.has(key(sid, MEMORY))) return;
	if (!existsSync(MEMORY_ENGINE) || !existsSync(MEMORY_DIR)) return;
	let indexBytes = 0;
	let fileCount = 0;
	try {
		const index = join(MEMORY_DIR, "MEMORY.md");
		indexBytes = existsSync(index) ? statSync(index).size : 0;
		fileCount = readdirSync(MEMORY_DIR).filter((f) => f.endsWith(".md")).length;
	} catch {
		return; // unreadable memory dir → silent skip
	}
	if (indexBytes < MEMORY_COMPACT_INDEX_BYTES && fileCount < MEMORY_COMPACT_FILE_COUNT) return;
	started.add(key(sid, MEMORY)); // claim BEFORE the run — proposal generated at most once
	// Proposal only: --dir with NO --apply, so the live memory dir is never mutated.
	execFile(
		MEMORY_PYTHON,
		[MEMORY_ENGINE, "--dir", MEMORY_DIR],
		{ timeout: 120_000, maxBuffer: 4 << 20 },
		(err) => {
			if (err) return; // engine failed → no result written (never claim a proposal that isn't there)
			// ctx-FREE: write the proposal-available verdict + message for a LATER turn.
			const msg = `apex-nudge: project memory is a growing per-session prefix (${indexBytes}B index, `
				+ `${fileCount} files) — a compaction proposal is available; review with `
				+ `\`python ${MEMORY_ENGINE} --dir ${MEMORY_DIR}\` (add --apply to archive cold `
				+ "files; git-guarded, advisory).";
			writeResult(resultPath(sid, MEMORY), `${PROPOSAL_MARKER}\n${msg}`);
		},
	);
}

export default function (pi: ExtensionAPI) {
	// Every turn: run the three INDEPENDENT checks, each wrapped so no error can
	// propagate into (or delay) the turn. All ctx use is SYNCHRONOUS here; all
	// heavy work is fire-and-forget with ctx-FREE callbacks.
	pi.on("turn_end", async (_event, ctx) => {
		let sid: string;
		try {
			sid = ctx.sessionManager.getSessionId();
		} catch {
			return; // no session id → nothing to key the guards on
		}
		if (!sid) return;
		try {
			checkCodeqaFreshness(sid, ctx);
		} catch {
			// advisory only — never surface into the turn
		}
		try {
			checkCacheHandoff(sid, ctx);
		} catch {
			// advisory only
		}
		try {
			checkMemoryCompact(sid, ctx);
		} catch {
			// advisory only
		}
	});
}
