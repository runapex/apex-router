// node --test integrations/pi/tests/gates.test.ts — spawn gates (owner order 2026-10-27): backpressure on pending
// branches and open owner decisions, the review-round limit, the xval evidence line. Real git in temp repos;
// no real ~/.apex-router file is read or written.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
	EVIDENCE_LINE, JobManager, ROUND_MESSAGE, ROUND_WINDOW_MS, endsGo, endsNoGo, headInfo, openDecisions, pendingBranches, reviewRounds, runGit,
	spawnGate, withEvidence, FS_IO, type GitRun,
} from "../lib/agents.ts";

const DAY = 86_400_000;
const tmp = (p: string) => mkdtempSync(join(tmpdir(), p));

function g(cwd: string, args: string[], date?: Date): string {
	const env = { ...process.env, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null" } as NodeJS.ProcessEnv;
	if (date) env.GIT_COMMITTER_DATE = env.GIT_AUTHOR_DATE = date.toISOString();
	return execFileSync("git", ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "-C", cwd, ...args], { env, encoding: "utf8", stdio: "pipe" });
}

function repo(): { repo: string; wts: string } {
	const r = tmp("gate-repo-");
	g(r, ["init", "-q", "-b", "main"]);
	writeFileSync(join(r, "a.txt"), "base\n");
	g(r, ["add", "a.txt"]);
	g(r, ["commit", "-q", "-m", "base"]);
	return { repo: r, wts: tmp("gate-wts-") };
}

/** A worktree on apexjob/<name>; with `commit`, one commit dated `date`. Returns the worktree path. */
function wt(rp: { repo: string; wts: string }, name: string, commit = true, date = new Date()): string {
	const dir = join(rp.wts, name);
	g(rp.repo, ["worktree", "add", "-q", "-b", `apexjob/${name}`, dir, "main"]);
	if (commit) {
		writeFileSync(join(dir, `${name}.txt`), name);
		g(dir, ["add", `${name}.txt`]);
		g(dir, ["commit", "-q", "-m", name], date);
	}
	return dir;
}

function decisions(rows: unknown[]): string {
	const p = join(tmp("gate-dec-"), "decisions.jsonl");
	writeFileSync(p, rows.map(r => (typeof r === "string" ? r : JSON.stringify(r))).join("\n") + "\n");
	return p;
}
const open = (n: number) => Array.from({ length: n }, (_, i) => ({ id: `D${i + 1}`, ts: 1, topic: `t${i + 1}`, text: `question ${i + 1}`, state: "open" }));

/** An xval job dir with meta.json (repo, branch) and a result.md whose last line is `verdict`. */
function xvalJob(root: string, id: string, repoDir: string, branch: string, verdict: string, kind = "xval", created = Date.now(), head?: string) {
	mkdirSync(join(root, id), { recursive: true });
	writeFileSync(join(root, id, "meta.json"), JSON.stringify({ id, state: "done", agent: kind, kind, task: "x".repeat(3000), cwd: "/x", argv: [], repo: repoDir, branch, head: head ?? `${id.replace(/\D/g, "").slice(-12)}abcdef`, created }, null, 1));
	writeFileSync(join(root, id, "result.md"), `# job ${id}\n\nfindings...\n\n${verdict}\n\n`);
}

const NO_DEC = () => join(tmp("gate-nodec-"), "decisions.jsonl");

// ------------------------------------------------------------------ SUCCESS 1
test("gate 1: pending = apexjob/* with a live worktree, a commit not in main, last commit < 7 days", () => {
	const rp = repo();
	wt(rp, "fresh"); // pending
	wt(rp, "empty", false); // no commit beyond main: not pending
	wt(rp, "old", true, new Date(Date.now() - 10 * DAY)); // last commit 10 days old: not pending
	const gone = wt(rp, "nowt"); // branch with a commit but its worktree is removed: not pending
	g(rp.repo, ["worktree", "remove", "--force", gone]);
	const pruned = wt(rp, "pruned"); // worktree dir deleted by hand (prunable): not pending
	rmSync(pruned, { recursive: true, force: true });
	wt(rp, "merged"); // merged into main: not pending
	g(rp.repo, ["merge", "-q", "--ff-only", "apexjob/merged"]);
	g(rp.repo, ["branch", "apexjob/plain"]); // old branch, never a worktree
	const p = pendingBranches(rp.repo, runGit, FS_IO.exists, Date.now(), Date.now() + 5000);
	assert.deepEqual(p, ["apexjob/fresh"]);
	// the same answer from inside a worktree of the repo
	assert.deepEqual(pendingBranches(join(rp.wts, "fresh"), runGit, FS_IO.exists, Date.now(), Date.now() + 5000), ["apexjob/fresh"]);
});

test("gate 1: a folder that is not a git repo has no pending branches (null, no warning)", () => {
	assert.equal(pendingBranches(tmp("gate-nogit-"), runGit, FS_IO.exists, Date.now(), Date.now() + 5000), null);
});

// ------------------------------------------------------------------ SUCCESS 2
test("gate 2: worker and coordinator are refused at 3 pending branches; the refusal names them", () => {
	const rp = repo();
	for (const n of ["b1", "b2", "b3"]) wt(rp, n);
	for (const agent of ["worker", "coordinator"]) {
		const r = spawnGate({ agent, kind: "pi", cwd: rp.repo, decisionsPath: NO_DEC(), jobsRoot: tmp("gate-jobs-") });
		assert.equal(r.allow, false, agent);
		for (const n of ["apexjob/b1", "apexjob/b2", "apexjob/b3"]) assert.match(r.message, new RegExp(n));
	}
});

test("gate 2: 2 pending branches pass", () => {
	const rp = repo();
	for (const n of ["b1", "b2"]) wt(rp, n);
	const r = spawnGate({ agent: "worker", kind: "pi", cwd: rp.repo, decisionsPath: NO_DEC(), jobsRoot: tmp("gate-jobs-") });
	assert.equal(r.allow, true);
	assert.deepEqual(r.warnings, []);
});

// ------------------------------------------------------------------ SUCCESS 3
test("gate 3: worker is refused at 5 open decisions; the refusal names them", () => {
	const r = spawnGate({ agent: "worker", kind: "pi", cwd: tmp("gate-nogit-"), decisionsPath: decisions(open(5)), jobsRoot: tmp("gate-jobs-") });
	assert.equal(r.allow, false);
	for (const id of ["D1", "D2", "D3", "D4", "D5"]) assert.match(r.message, new RegExp(`\\b${id}\\b`));
});

test("gate 3: the latest row per id wins; closed and bad rows do not count", () => {
	const rows: unknown[] = [...open(6), { id: "D6", ts: 2, topic: "t6", text: "question 6", state: "closed" }, "{not json", { id: 7, state: "open" }, { id: "D9", state: "maybe" }];
	const d = openDecisions(decisions(rows), FS_IO);
	assert.deepEqual(d.open.map(x => x.id), ["D1", "D2", "D3", "D4", "D5"]);
	assert.equal(d.bad, 3);
	const four = decisions([...open(5), { id: "D5", ts: 2, topic: "t5", text: "question 5", state: "closed" }, "garbage"]);
	const r = spawnGate({ agent: "coordinator", kind: "pi", cwd: tmp("gate-nogit-"), decisionsPath: four, jobsRoot: tmp("gate-jobs-") });
	assert.equal(r.allow, true);
	assert.equal(r.warnings.length, 1); // one line: 1 bad row skipped
	assert.match(r.warnings[0]!, /1 bad row/);
});

test("gate 3: a missing decisions file means 0 open decisions, no warning", () => {
	const r = spawnGate({ agent: "worker", kind: "pi", cwd: tmp("gate-nogit-"), decisionsPath: NO_DEC(), jobsRoot: tmp("gate-jobs-") });
	assert.deepEqual(r, { ...r, allow: true, warnings: [] });
});

// ------------------------------------------------------------------ SUCCESS 4
test("gate 4: scout, reviewer and xval are never refused by the branch and decision gates", () => {
	const rp = repo();
	for (const n of ["b1", "b2", "b3", "b4"]) wt(rp, n);
	const dec = decisions(open(9));
	for (const [agent, kind] of [["scout", "pi"], ["reviewer", "pi"], ["xval", "xval"]] as const) {
		const r = spawnGate({ agent, kind, cwd: rp.repo, decisionsPath: dec, jobsRoot: tmp("gate-jobs-") });
		assert.equal(r.allow, true, agent);
	}
});

// ------------------------------------------------------------------ SUCCESS 5
test("gate 5: an override of 10+ characters passes once and is returned as gate_override", () => {
	const dec = decisions(open(5));
	const short = spawnGate({ agent: "worker", kind: "pi", cwd: tmp("gate-nogit-"), decisionsPath: dec, jobsRoot: tmp("gate-jobs-"), override: "too short" });
	assert.equal(short.allow, false);
	assert.match(short.message, /10 characters/);
	const ok = spawnGate({ agent: "worker", kind: "pi", cwd: tmp("gate-nogit-"), decisionsPath: dec, jobsRoot: tmp("gate-jobs-"), override: "owner said go for the hotfix" });
	assert.equal(ok.allow, true);
	assert.equal(ok.override, "owner said go for the hotfix");
	// no refusal to pass: no override is recorded
	const none = spawnGate({ agent: "worker", kind: "pi", cwd: tmp("gate-nogit-"), decisionsPath: NO_DEC(), jobsRoot: tmp("gate-jobs-"), override: "owner said go for the hotfix" });
	assert.equal(none.allow, true);
	assert.equal(none.override, undefined);
});

test("gate 5+6: JobManager.spawn writes gate_override, repo, branch and head into meta.json", () => {
	const root = tmp("gate-jobs-");
	const m = new JobManager({ root, argv: () => [process.execPath, "-e", ""], pressure: () => "GREEN", tickMs: 60_000 });
	const j = m.spawn({ task: "t", agent: "xval", kind: "xval", gate_override: "owner said go for the hotfix", repo: "/r/.git", branch: "apexjob/x", head: "abc123" });
	const meta = JSON.parse(readFileSync(join(root, j.id, "meta.json"), "utf8"));
	assert.equal(meta.gate_override, "owner said go for the hotfix");
	assert.equal(meta.repo, "/r/.git");
	assert.equal(meta.branch, "apexjob/x");
	assert.equal(meta.head, "abc123");
	const k = m.spawn({ task: "t" }); // no gate fields: none are written
	const meta2 = JSON.parse(readFileSync(join(root, k.id, "meta.json"), "utf8"));
	for (const f of ["gate_override", "repo", "branch", "head"]) assert.equal(f in meta2, false, f);
	m.stop();
});

// ------------------------------------------------------------------ SUCCESS 6
test("gate 6: headInfo gives the repo, branch and HEAD of a cwd", () => {
	const rp = repo();
	const dir = wt(rp, "hx");
	const h = headInfo(dir, runGit, 5000);
	assert.equal(h!.branch, "apexjob/hx");
	assert.equal(h!.head, g(dir, ["rev-parse", "HEAD"]).trim());
	assert.equal(h!.repo, headInfo(rp.repo, runGit, 5000)!.repo); // one repo for every worktree
	assert.equal(headInfo(tmp("gate-nogit-"), runGit, 5000), null);
});

test("gate 6: endsNoGo reads the last non-empty line", () => {
	assert.equal(endsNoGo("a\nNO-GO\n\n"), true);
	assert.equal(endsNoGo("a\n**VERDICT: NO-GO**\n"), true);
	assert.equal(endsNoGo("NO-GO\nGO"), false);
	assert.equal(endsNoGo("VERDICT: GO-WITH-CHANGES"), false);
	assert.equal(endsNoGo("Read-only source review. No tests run."), false);
	assert.equal(endsNoGo(""), false);
});

test("gate 6: xval is refused after 3 NO-GO rounds on the same branch; override passes", () => {
	const rp = repo();
	const dir = wt(rp, "rv");
	const h = headInfo(dir, runGit, 5000)!;
	const root = tmp("gate-jobs-");
	xvalJob(root, "20261027095900-zzzz", h.repo, "apexjob/rv", "NO-GO"); // before the last GO: an earlier change
	xvalJob(root, "20261027100000-cccc", h.repo, "apexjob/rv", "GO");
	xvalJob(root, "20261027100100-aaaa", h.repo, "apexjob/rv", "VERDICT: NO-GO");
	xvalJob(root, "20261027100200-bbbb", h.repo, "apexjob/rv", "NO-GO");
	xvalJob(root, "20261027100300-dddd", h.repo, "apexjob/other", "NO-GO"); // other branch
	xvalJob(root, "20261027100400-eeee", "/other/.git", "apexjob/rv", "NO-GO"); // other repo
	xvalJob(root, "20261027100500-ffff", h.repo, "apexjob/rv", "NO-GO", "pi"); // not an xval job
	assert.equal(reviewRounds(root, h.repo, "apexjob/rv", FS_IO, Date.now() + 5000).nogo.length, 2);
	const two = spawnGate({ agent: "xval", kind: "xval", cwd: dir, decisionsPath: NO_DEC(), jobsRoot: root });
	assert.equal(two.allow, true);
	assert.equal(two.head?.branch, "apexjob/rv");
	xvalJob(root, "20261027100600-gggg", h.repo, "apexjob/rv", "VERDICT: NO-GO");
	const three = spawnGate({ agent: "xval", kind: "xval", cwd: dir, decisionsPath: NO_DEC(), jobsRoot: root });
	assert.equal(three.allow, false);
	assert.match(three.message, new RegExp(ROUND_MESSAGE));
	assert.equal(ROUND_MESSAGE, "send the open finding to the owner as one question");
	const over = spawnGate({ agent: "xval", kind: "xval", cwd: dir, decisionsPath: NO_DEC(), jobsRoot: root, override: "owner asked for round 4" });
	assert.equal(over.allow, true);
	assert.equal(over.override, "owner asked for round 4");
});

test("gate 6: the job scan reads only the newest 2000 job names", () => {
	const root = tmp("gate-jobs-");
	xvalJob(root, "20200101000000-aaaa", "/r/.git", "b", "NO-GO"); // oldest: outside the newest 2000
	for (let i = 0; i < 2000; i++) mkdirSync(join(root, `2026${String(i).padStart(10, "0")}-x`));
	assert.equal(reviewRounds(root, "/r/.git", "b", FS_IO, Date.now() + 5000).nogo.length, 0);
});

// ------------------------------------------------------------------ SUCCESS 7
test("gate 7: every xval task gets the evidence line once", () => {
	assert.equal(EVIDENCE_LINE, "A P1 counts only with evidence: a line number and a concrete input or a failing test. A GO lists the claims you checked. If you cannot read the code, the last line is BLOCKED, not NO-GO.");
	const t = withEvidence("Review the diff.");
	assert.equal(t, `Review the diff.\n\n${EVIDENCE_LINE}`);
	assert.equal(withEvidence(t), t);
});

// ------------------------------------------------------------------ SUCCESS 9
test("gate 9: a failed check allows the spawn with one warning line", () => {
	const bad: GitRun = () => {
		throw new Error("fatal: index file corrupt");
	};
	const r = spawnGate({ agent: "worker", kind: "pi", cwd: "/tmp", decisionsPath: NO_DEC(), jobsRoot: tmp("gate-jobs-"), git: bad });
	assert.equal(r.allow, true);
	assert.equal(r.warnings.length, 1);
	assert.match(r.warnings[0]!, /gate check failed/);
	const dirAsFile = tmp("gate-dec-"); // a folder where the file must be: read error
	const r2 = spawnGate({ agent: "worker", kind: "pi", cwd: tmp("gate-nogit-"), decisionsPath: dirAsFile, jobsRoot: tmp("gate-jobs-") });
	assert.equal(r2.allow, true);
	assert.equal(r2.warnings.length, 1);
	const rx = spawnGate({ agent: "xval", kind: "xval", cwd: "/tmp", decisionsPath: NO_DEC(), jobsRoot: tmp("gate-jobs-"), git: bad });
	assert.equal(rx.allow, true);
	assert.equal(rx.warnings.length, 1);
});

test("gate 9: runGit stops a hung command at its timeout", () => {
	const t0 = Date.now();
	assert.throws(() => runGit(["-e", "setTimeout(() => {}, 10000)"], 200, process.execPath));
	assert.ok(Date.now() - t0 < 2000, `took ${Date.now() - t0} ms`);
});

test("gate 9: a slow git uses the time budget and then allows with a warning", () => {
	let calls = 0;
	const slow: GitRun = (_a, timeoutMs) => {
		calls++;
		assert.ok(timeoutMs <= 1000, `timeout ${timeoutMs}`);
		const until = Date.now() + timeoutMs;
		while (Date.now() < until) { /* hang until the timeout */ }
		throw new Error("ETIMEDOUT");
	};
	const t0 = Date.now();
	const r = spawnGate({ agent: "worker", kind: "pi", cwd: "/tmp", decisionsPath: NO_DEC(), jobsRoot: tmp("gate-jobs-"), git: slow });
	assert.ok(Date.now() - t0 < 1000, `took ${Date.now() - t0} ms`);
	assert.equal(r.allow, true);
	assert.equal(r.warnings.length, 1);
	assert.ok(calls >= 1);
});

test("gate 9: a full check on a real repo takes under 1 s", () => {
	const rp = repo();
	for (const n of ["b1", "b2"]) wt(rp, n);
	const root = tmp("gate-jobs-");
	for (let i = 0; i < 300; i++) xvalJob(root, `20261027${String(i).padStart(6, "0")}-q${i}`, "/r/.git", "b", "NO-GO");
	for (const [agent, kind, cwd] of [["worker", "pi", rp.repo], ["xval", "xval", join(rp.wts, "b1")]] as const) {
		const t0 = Date.now();
		const r = spawnGate({ agent, kind, cwd, decisionsPath: decisions(open(4)), jobsRoot: root });
		assert.ok(Date.now() - t0 < 1000, `${agent} took ${Date.now() - t0} ms`);
		assert.equal(r.allow, true);
	}
});

test("gate 6: a GO starts the count again, and rounds older than 7 days do not count (main is not refused for ever)", () => {
	const root = tmp("gate-jobs-");
	const now = Date.now();
	for (const [i, v] of ["NO-GO", "NO-GO", "NO-GO", "GO", "NO-GO"].entries())
		xvalJob(root, `2026102710${String(i).padStart(2, "0")}00-r${i}`, "/r/.git", "main", v, "xval", now - 1000 * (10 - i));
	assert.deepEqual(reviewRounds(root, "/r/.git", "main", FS_IO, now + 5000, now).nogo, ["2026102710040" + "0-r4"]);
	const old = tmp("gate-jobs-");
	for (let i = 0; i < 4; i++) xvalJob(old, `2026101010${i}000-o${i}`, "/r/.git", "main", "NO-GO", "xval", now - ROUND_WINDOW_MS - 1000);
	xvalJob(old, "20261027120000-n0", "/r/.git", "main", "NO-GO", "xval", now - 1000);
	assert.equal(reviewRounds(old, "/r/.git", "main", FS_IO, now + 5000, now).nogo.length, 1);
	assert.equal(endsGo("GO"), true);
	assert.equal(endsGo("**GO**"), true);
	assert.equal(endsGo("NO-GO"), false);
	assert.equal(endsGo("GO-WITH-CHANGES"), false);
});

test("gate 6: an incomplete scan never refuses (fail open), even with 3 NO-GO rounds found", () => {
	const rp = repo();
	const dir = wt(rp, "inc");
	const h = headInfo(dir, runGit, 5000)!;
	const root = tmp("gate-jobs-");
	for (let i = 0; i < 3; i++) xvalJob(root, `2026102710${i}000-n${i}`, h.repo, "apexjob/inc", "NO-GO");
	xvalJob(root, "20261027110000-go", h.repo, "apexjob/inc", "GO");
	const unreadable = { ...FS_IO, read: (p: string, cap: number, tail?: boolean) => {
		if (p.includes("20261027110000-go") && p.endsWith("result.md")) throw Object.assign(new Error("EACCES"), { code: "EACCES" });
		return FS_IO.read(p, cap, tail);
	} };
	const g = spawnGate({ agent: "xval", kind: "xval", cwd: dir, decisionsPath: NO_DEC(), jobsRoot: root, io: unreadable });
	assert.equal(g.allow, true);
	assert.ok(g.warnings.some(w => /scan incomplete/.test(w)));
});

test("gate 3: a cut decisions file gives a warning, not a silent pass", () => {
	const p = join(tmp("gate-dec-"), "decisions.jsonl");
	const rows = Array.from({ length: 5 }, (_, i) => ({ id: `D${i}`, state: "open", text: "q" })).concat(
		Array.from({ length: 3000 }, (_, i) => ({ id: `C${i}`, state: "closed", text: "x".repeat(400) })));
	writeFileSync(p, rows.map(r => JSON.stringify(r)).join("\n") + "\n");
	const g = spawnGate({ agent: "worker", kind: "pi", cwd: tmp("gate-norepo-"), decisionsPath: p, jobsRoot: tmp("gate-jobs-") });
	assert.ok(g.warnings.some(w => /bad row/.test(w)), JSON.stringify(g.warnings));
});

test("gate 6: parallel part-reviews of one commit count as ONE round (owner 2026-10-28)", () => {
	const root = tmp("gate-jobs-");
	const now = Date.now();
	const H1 = "1111111111111111111111111111111111111111", H2 = "2222222222222222222222222222222222222222";
	for (const p of ["a", "b", "c"]) xvalJob(root, `20261028022600-${p}`, "/r/.git", "apexjob/x", "NO-GO", "xval", now - 5000, H1);
	assert.equal(reviewRounds(root, "/r/.git", "apexjob/x", FS_IO, now + 5000, now).nogo.length, 1);
	xvalJob(root, "20261028024100-d", "/r/.git", "apexjob/x", "NO-GO", "xval", now - 3000, H2);
	assert.equal(reviewRounds(root, "/r/.git", "apexjob/x", FS_IO, now + 5000, now).nogo.length, 2);
});

test("gate 6: a GO on a commit cancels the NO-GO parts of that same commit", () => {
	const root = tmp("gate-jobs-");
	const now = Date.now();
	const H0 = "0000000000000000000000000000000000000000", H1 = "1111111111111111111111111111111111111111";
	xvalJob(root, "20261028020000-z", "/r/.git", "apexjob/x", "NO-GO", "xval", now - 9000, H0);
	xvalJob(root, "20261028021000-a", "/r/.git", "apexjob/x", "GO", "xval", now - 6000, H1);
	xvalJob(root, "20261028021001-b", "/r/.git", "apexjob/x", "NO-GO", "xval", now - 5900, H1);
	assert.equal(reviewRounds(root, "/r/.git", "apexjob/x", FS_IO, now + 5000, now).nogo.length, 0);
});

test("gate 6: a BLOCKED review (it could not read the code) is not a round and does not reset the count", () => {
	const root = tmp("gate-jobs-");
	const now = Date.now();
	const H = (c: string) => c.repeat(40);
	xvalJob(root, "20261028030000-a", "/r/.git", "apexjob/x", "NO-GO", "xval", now - 9000, H("1"));
	xvalJob(root, "20261028030100-b", "/r/.git", "apexjob/x", "BLOCKED", "xval", now - 8000, H("2"));
	xvalJob(root, "20261028030200-c", "/r/.git", "apexjob/x", "**BLOCKED**", "xval", now - 7000, H("3"));
	const r = reviewRounds(root, "/r/.git", "apexjob/x", FS_IO, now + 5000, now);
	assert.deepEqual(r.nogo, ["20261028030000-a"]);
	assert.equal(endsNoGo("cannot read the diff\nBLOCKED"), false);
	assert.equal(endsGo("cannot read the diff\nBLOCKED"), false);
	assert.match(EVIDENCE_LINE, /BLOCKED, not NO-GO/);
});
