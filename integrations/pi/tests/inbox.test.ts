// node --test integrations/pi/tests/   — agent inbox overlay and widget (docs/design/AGENTS-INBOX.md sections 4-7, 9)
// against a fake `apex-router` CLI (fixtures/fake-agents-cli.mjs). No pi, no model.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
	DEFAULT_TIMEOUT_MS, InboxOverlay, InboxStore, makeCliRunner, MAX_STDOUT, osc52, overlayOptionsFor, parseInbox,
	readResultTail, startPoller, widgetLine, widgetText, type CliRunner, type TuiFns,
} from "../lib/inbox.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const FAKE = join(HERE, "fixtures", "fake-agents-cli.mjs");
const FIXTURE = readFileSync(join(HERE, "fixtures", "inbox.json"), "utf8");
// pi-tui is not in node_modules of this repo: load the copy that ships with pi (the only width measure, contract 7).
const TUI_PATH = "/opt/homebrew/lib/node_modules/@earendil-works/pi-coding-agent/node_modules/@earendil-works/pi-tui/dist/index.js";
const piTui = (await import(TUI_PATH)) as { visibleWidth(s: string): number; truncateToWidth(s: string, w: number, e?: string, pad?: boolean): string; matchesKey(d: string, k: string): boolean };
const TUI: TuiFns = { visibleWidth: piTui.visibleWidth, truncateToWidth: piTui.truncateToWidth, matchesKey: piTui.matchesKey };

const plainTheme = { fg: (_c: string, s: string) => s, bg: (_c: string, s: string) => s, bold: (s: string) => s };
const colorTheme = {
	fg: (c: string, s: string) => `\x1b[38;5;${c.length}m${s}\x1b[39m`,
	bg: (_c: string, s: string) => `\x1b[48;5;236m${s}\x1b[49m`,
	bold: (s: string) => `\x1b[1m${s}\x1b[22m`,
};

function fake(mode = "ok", over: { timeoutMs?: number; maxBytes?: number } = {}) {
	const log = join(mkdtempSync(join(tmpdir(), "inbox-")), "calls.jsonl");
	process.env.FAKE_AGENTS_LOG = log;
	const run = makeCliRunner({ bin: process.execPath, prefix: [FAKE, mode], ...over });
	const calls = () => (existsSync(log) ? readFileSync(log, "utf8").trim().split("\n").filter(Boolean).map(l => JSON.parse(l) as string[]) : []);
	return { run, calls };
}

const KEY = { up: "\x1b[A", down: "\x1b[B", enter: "\r", tab: "\t", esc: "\x1b", ctrlShiftA: "\x1b[97;6u" };

function overlay(store: InboxStore, theme = plainTheme, over: Partial<ConstructorParameters<typeof InboxOverlay>[0]> = {}) {
	const ev = { closed: 0, renders: 0, copied: [] as string[], results: [] as string[] };
	const o = new InboxOverlay({
		store, theme, tui: TUI, rows: () => 40,
		onClose: () => ev.closed++,
		requestRender: () => ev.renders++,
		copy: t => (ev.copied.push(t), "copied"),
		readResult: it => (ev.results.push(it.id), { ok: true, lines: ["line one", "中文 result line", "\x1b[31mred\x1b[0m"] }),
		...over,
	});
	return { o, ev };
}

const text = (lines: string[]) => lines.join("\n");

// ---- 4. JSON parse ----------------------------------------------------------------------------------------

test("parseInbox: keeps valid items, drops an unsafe id, strips control characters, caps text", () => {
	const r = parseInbox(FIXTURE);
	assert.ok(r.ok);
	const ids = r.inbox.items.map(i => i.id);
	assert.equal(ids.length, 6);
	assert.ok(!ids.some(i => i.includes("$")), "an id with shell characters is dropped");
	for (const it of r.inbox.items) {
		assert.ok(!/[\x00-\x1f\x7f]/.test(it.label), `label has no control characters: ${JSON.stringify(it.label)}`);
		assert.ok(it.label.length <= 60 + 1);
		for (const b of it.open) assert.ok(b.text.length <= 160 + 1 && !/[\x00-\x1f]/.test(b.text));
	}
	assert.deepEqual(r.inbox.counts, { running: 2, needs: 3, done: 2 });
	assert.equal(r.inbox.items[0]!.open[0]!.pri, "P1");
});

test("parseInbox: junk, wrong shape and too many items never throw", () => {
	assert.equal(parseInbox("{not json").ok, false);
	assert.equal(parseInbox("[]").ok, false);
	assert.equal(parseInbox("null").ok, false);
	assert.equal(parseInbox(JSON.stringify({ items: "x" })).ok, false);
	const many = { now: 1, complete: true, counts: {}, items: Array.from({ length: 5000 }, (_, i) => ({ id: `id-${i}`, group: "done", state: "done", label: "x", open: [] })) };
	const r = parseInbox(JSON.stringify(many));
	assert.ok(r.ok);
	assert.equal(r.inbox.items.length, 200, "at most 200 items");
	const badOpen = { now: 1, items: [{ id: "a", group: "needs", state: "done", open: Array.from({ length: 999 }, () => ({ key: "0123456789abcdef", text: "t" })) }] };
	const r2 = parseInbox(JSON.stringify(badOpen));
	assert.ok(r2.ok && r2.inbox.items[0]!.open.length <= 50, "open list is bounded");
});

// ---- 9. CLI calls: execFile, 3 s, 2 MiB ---------------------------------------------------------------------

test("CLI limits are 3 s and 2 MiB", () => {
	assert.equal(DEFAULT_TIMEOUT_MS, 3000);
	assert.equal(MAX_STDOUT, 2 * 1024 * 1024);
});

test("store loads `agents --json` through the CLI", async () => {
	const { run, calls } = fake("ok");
	const s = new InboxStore({ run });
	await s.refresh();
	assert.equal(s.error, undefined);
	assert.equal(s.data?.items.length, 6);
	assert.deepEqual(calls(), [["agents", "--json"]]);
});

test("timeout: one short reason, no throw, no hang", async () => {
	const { run } = fake("slow", { timeoutMs: 300 });
	const t0 = Date.now();
	const s = new InboxStore({ run });
	await s.refresh();
	assert.ok(Date.now() - t0 < 2500, "stops at the deadline");
	assert.match(s.error ?? "", /timeout/i);
});

test("unbounded stdout is cut at the cap", async () => {
	const { run } = fake("huge", { maxBytes: 256 * 1024 });
	const s = new InboxStore({ run });
	await s.refresh();
	assert.match(s.error ?? "", /too large/i);
	assert.equal(s.data, undefined);
});

test("bad JSON, exit 1 and a missing binary give one clean reason line", async () => {
	for (const mode of ["badjson", "fail"]) {
		const s = new InboxStore({ run: fake(mode).run });
		await s.refresh();
		assert.ok(s.error, mode);
		assert.ok(!/[\x00-\x1f]/.test(s.error!), `no control characters: ${JSON.stringify(s.error)}`);
		assert.ok(s.error!.length <= 120);
	}
	const s = new InboxStore({ run: makeCliRunner({ bin: "/nonexistent/apex-router-xyz" }) });
	await s.refresh();
	assert.match(s.error ?? "", /not found|ENOENT/i);
});

test("a failed refresh keeps the last good data", async () => {
	const ok = fake("ok").run;
	let mode: CliRunner = ok;
	const s = new InboxStore({ run: a => mode(a) });
	await s.refresh();
	mode = fake("fail").run;
	await s.refresh();
	assert.equal(s.data?.items.length, 6);
	assert.ok(s.error);
});

// ---- 5. resolve ----------------------------------------------------------------------------------------------

test("resolve passes id and key as separate argv words; --all for R", async () => {
	const { run, calls } = fake("ok");
	const s = new InboxStore({ run });
	assert.equal((await s.resolve("20261030224430-o41s", "0123456789abcdef")).ok, true);
	assert.equal((await s.resolve("20261030224430-o41s", "--all")).ok, true);
	const c = calls();
	assert.deepEqual(c[0], ["agents", "resolve", "20261030224430-o41s", "0123456789abcdef"]);
	assert.deepEqual(c.filter(x => x[1] === "resolve")[1], ["agents", "resolve", "20261030224430-o41s", "--all"]);
	assert.ok(c.some(x => x[1] === "--json"), "a resolve refreshes the list");
});

test("resolve refuses unsafe ids and keys without a CLI call", async () => {
	const { run, calls } = fake("ok");
	const s = new InboxStore({ run });
	for (const [id, key] of [["$(touch /tmp/pwned)", "--all"], ["-rf", "--all"], ["../../etc", "--all"], ["ok-id", "; rm -rf ~"], ["ok-id", "--undo"], ["ok-id", "XYZ"]] as const) {
		const r = await s.resolve(id, key);
		assert.equal(r.ok, false, `${id} ${key}`);
	}
	assert.deepEqual(calls(), []);
});

test("resolve failure is a reason, not a throw", async () => {
	const s = new InboxStore({ run: fake("resolve-fail").run });
	const r = await s.resolve("20261030224430-o41s", "--all");
	assert.equal(r.ok, false);
	assert.match(r.reason ?? "", /no such item|exit/i);
});

// ---- single flight, poller ----------------------------------------------------------------------------------

test("never more than one CLI call in flight", async () => {
	let live = 0;
	let peak = 0;
	let n = 0;
	const run: CliRunner = async () => {
		n++;
		live++;
		peak = Math.max(peak, live);
		await new Promise(r => setTimeout(r, 100));
		live--;
		return { ok: true, stdout: FIXTURE };
	};
	const s = new InboxStore({ run });
	await Promise.all([s.refresh(), s.refresh(), s.refresh(), s.resolve("20261030224430-o41s", "--all"), s.refresh()]);
	assert.equal(peak, 1);
	assert.equal(n, 1, "the calls during a load join it or get 'busy'");
});

test("poller: no call while busy, at most one in flight, stops clean", async () => {
	let n = 0;
	let live = 0;
	let peak = 0;
	const run: CliRunner = async () => {
		n++;
		live++;
		peak = Math.max(peak, live);
		await new Promise(r => setTimeout(r, 120));
		live--;
		return { ok: true, stdout: FIXTURE };
	};
	const s = new InboxStore({ run });
	let busy = true;
	let updates = 0;
	const stop = startPoller({ store: s, intervalMs: 20, isBusy: () => busy, onUpdate: () => updates++ });
	await new Promise(r => setTimeout(r, 150));
	assert.equal(n, 0, "no refresh while pi streams");
	busy = false;
	await new Promise(r => setTimeout(r, 400));
	assert.ok(n >= 1 && n <= 4, `calls ${n}`);
	assert.equal(peak, 1);
	assert.ok(updates >= 1);
	stop();
	await new Promise(r => setTimeout(r, 150));
	const after = n;
	await new Promise(r => setTimeout(r, 150));
	assert.equal(n, after, "no call after stop");
});

// ---- widget --------------------------------------------------------------------------------------------------

test("widget: one line only while an item runs or needs you", async () => {
	const s = new InboxStore({ run: fake("ok").run });
	assert.equal(widgetText(s), undefined, "nothing before the first load");
	await s.refresh();
	assert.deepEqual(widgetText(s), { text: "● 2 running  ⚠ 3 need you", tone: "normal" });
	const e = new InboxStore({ run: fake("empty").run });
	await e.refresh();
	assert.equal(widgetText(e), undefined, "hidden with 0 running and 0 needs");
	const f = new InboxStore({ run: fake("fail").run });
	await f.refresh();
	assert.equal(widgetText(f), undefined, "no widget for a CLI that never loaded");
	// shown before, then the CLI fails: one muted line
	let mode: CliRunner = fake("ok").run;
	const g = new InboxStore({ run: a => mode(a) });
	await g.refresh();
	mode = fake("fail").run;
	await g.refresh();
	const w = widgetText(g)!;
	assert.equal(w.tone, "muted");
	assert.match(w.text, /^Inbox not loaded: /);
});

// ---- 6. overlay: size and keys -------------------------------------------------------------------------------

test("overlay options: right half, minWidth 60; full width under 120 columns", () => {
	assert.deepEqual(overlayOptionsFor(200), { width: "50%", minWidth: 60, anchor: "right-center", maxHeight: "100%" });
	assert.deepEqual(overlayOptionsFor(120), { width: "50%", minWidth: 60, anchor: "right-center", maxHeight: "100%" });
	assert.equal(overlayOptionsFor(119).width, "100%");
	assert.equal(overlayOptionsFor(40).width, "100%");
});

test("overlay: Tab filters Needs / Running / Done / All; list on top, detail below", async () => {
	const s = new InboxStore({ run: fake("ok").run });
	await s.refresh();
	const { o } = overlay(s);
	let out = text(o.render(100));
	assert.match(out, /Needs 3/);
	assert.match(out, /review the inbox overlay/);
	assert.ok(!/build the widget/.test(out), "running items are not in the Needs filter");
	// detail of the first needs item
	assert.match(out, /P1/);
	assert.match(out, /NO-GO/);
	assert.match(out, /apexjob\/mvtuzw5r/);
	assert.match(out, /ahead 3/);
	assert.match(out, /\$1\.23/);
	assert.match(out, /8m/, "age from end time (500 s)");
	assert.ok(!/Old blocker/.test(out), "a resolved blocker is not shown");
	o.handleInput(KEY.tab);
	out = text(o.render(100));
	assert.match(out, /build the widget/);
	assert.match(out, /now: bash node --test/, "running item shows its now-command");
	o.handleInput(KEY.tab);
	out = text(o.render(100));
	assert.match(out, /old work/);
	o.handleInput(KEY.tab);
	out = text(o.render(100));
	assert.match(out, /old work/);
	assert.match(out, /build the widget/);
	o.handleInput(KEY.tab);
	assert.match(text(o.render(100)), /review the inbox overlay/, "Tab wraps to Needs");
});

test("overlay: up/down move, enter expands all blockers", async () => {
	const s = new InboxStore({ run: fake("ok").run });
	await s.refresh();
	const { o } = overlay(s);
	o.handleInput(KEY.down);
	assert.match(text(o.render(100)), /id 20261030220000-fail/);
	o.handleInput(KEY.up);
	o.handleInput(KEY.up);
	assert.match(text(o.render(100)), /id 20261030224430-o41s/, "up stops at the top");
	o.handleInput(KEY.enter);
	const out = text(o.render(100));
	assert.match(out, /Blocker two/);
	assert.match(out, /resume: cd /);
});

test("overlay: r resolves the selected blocker, R all, c copies resume, o shows result", async () => {
	const { run, calls } = fake("ok");
	const s = new InboxStore({ run });
	await s.refresh();
	const { o, ev } = overlay(s);
	o.handleInput(KEY.enter); // expand: the blocker cursor is on P1
	o.handleInput(KEY.down); // P2
	o.handleInput("r");
	await o.idle();
	assert.deepEqual(calls().find(c => c[1] === "resolve"), ["agents", "resolve", "20261030224430-o41s", "fedcba9876543210"]);
	o.handleInput("R");
	await o.idle();
	assert.deepEqual(calls().filter(c => c[1] === "resolve").at(-1), ["agents", "resolve", "20261030224430-o41s", "--all"]);
	o.handleInput("c");
	assert.deepEqual(ev.copied, ["cd /Users/x/dev/apex-router && pi --session-id 1234"]);
	assert.match(text(o.render(100)), /Copied/);
	o.handleInput("o");
	assert.deepEqual(ev.results, ["20261030224430-o41s"]);
	const out = text(o.render(100));
	assert.match(out, /中文 result line/);
	assert.ok(!out.includes("\x1b[31m"), "result text is cleaned");
	o.handleInput(KEY.esc);
	assert.equal(ev.closed, 0, "Esc in the result view goes back to the list");
	assert.match(text(o.render(100)), /review the inbox overlay/);
	assert.ok(ev.renders > 0);
});

test("overlay: c without OSC 52 shows the command", async () => {
	const s = new InboxStore({ run: fake("ok").run });
	await s.refresh();
	const { o } = overlay(s, plainTheme, { copy: () => "shown" });
	o.handleInput("c");
	assert.match(text(o.render(120)), /cd \/Users\/x\/dev\/apex-router && pi --session-id 1234/);
});

test("overlay: Esc closes; the toggle key closes", async () => {
	const s = new InboxStore({ run: fake("ok").run });
	await s.refresh();
	const a = overlay(s);
	a.o.handleInput(KEY.esc);
	assert.equal(a.ev.closed, 1);
	const b = overlay(s);
	b.o.handleInput(KEY.ctrlShiftA);
	assert.equal(b.ev.closed, 1);
});

test("overlay: a CLI failure shows one muted line, no throw", async () => {
	const s = new InboxStore({ run: fake("fail").run });
	await s.refresh();
	const { o } = overlay(s);
	const lines = o.render(80);
	assert.equal(lines.filter(l => l.includes("Inbox not loaded: ")).length, 1);
	for (const k of ["r", "R", "c", "o", KEY.enter, KEY.down, KEY.tab]) o.handleInput(k);
	await o.idle();
	o.render(80);
});

// ---- 7. every line fits the width ----------------------------------------------------------------------------

test("every rendered line fits the width (40, 60, 80, 120, 200; wide characters, long labels, colour)", async () => {
	const okStore = new InboxStore({ run: fake("ok").run });
	await okStore.refresh();
	const failStore = new InboxStore({ run: fake("fail").run });
	await failStore.refresh();
	const longCopy = () => "shown" as const;
	for (const theme of [plainTheme, colorTheme]) {
		for (const width of [40, 60, 80, 120, 200]) {
			const views: Array<[string, string[]]> = [];
			const { o } = overlay(okStore, theme, { copy: longCopy, readResult: () => ({ ok: true, lines: ["中".repeat(300), "x".repeat(500), "😀".repeat(200)] }) });
			for (let f = 0; f < 4; f++) {
				views.push([`filter${f}`, o.render(width)]);
				o.handleInput(KEY.enter);
				views.push([`filter${f}-expanded`, o.render(width)]);
				o.handleInput(KEY.down);
				views.push([`filter${f}-down`, o.render(width)]);
				o.handleInput(KEY.enter);
				o.handleInput(KEY.tab);
			}
			o.handleInput("c");
			views.push(["copy", o.render(width)]);
			o.handleInput("o");
			views.push(["result", o.render(width)]);
			views.push(["fail", overlay(failStore, theme).o.render(width)]);
			for (const [name, lines] of views) {
				assert.ok(lines.length > 0 && lines.length <= 40, `${name}@${width}: ${lines.length} rows`);
				for (const l of lines) {
					assert.ok(TUI.visibleWidth(l) <= width, `${name}@${width}: ${TUI.visibleWidth(l)} > ${width}: ${JSON.stringify(l)}`);
					assert.ok(!/[\n\r\t]/.test(l), `${name}@${width}: no line breaks or tabs`);
					// pi-tui truncateToWidth adds only an SGR reset (\x1b[0m) at a cut; every other escape is an injection.
					if (theme === plainTheme) assert.ok(!l.replaceAll("\x1b[0m", "").includes("\x1b"), `${name}@${width}: CLI text cannot inject escapes`);
				}
			}
		}
	}
});

test("widget line fits the width (1, 10, 40, 60, 80, 120, 200) and is one line", async () => {
	const s = new InboxStore({ run: fake("ok").run });
	await s.refresh();
	const long = { text: `Inbox not loaded: ${"中".repeat(300)}`, tone: "muted" as const };
	for (const theme of [plainTheme, colorTheme]) {
		for (const width of [1, 10, 40, 60, 80, 120, 200]) {
			for (const w of [widgetText(s), long]) {
				const lines = widgetLine(w, theme, TUI, width);
				assert.equal(lines.length, 1);
				assert.ok(TUI.visibleWidth(lines[0]!) <= width, `${width}: ${JSON.stringify(lines[0])}`);
			}
		}
	}
	assert.deepEqual(widgetLine(undefined, plainTheme, TUI, 80), []);
});

// ---- result.md tail, OSC 52 ----------------------------------------------------------------------------------

test("readResultTail: last 200 lines of a pi job, bounded read, refuses FIFOs and bad ids", () => {
	const root = mkdtempSync(join(tmpdir(), "inbox-jobs-"));
	const id = "20261030224430-o41s";
	mkdirSync(join(root, id));
	writeFileSync(join(root, id, "result.md"), Array.from({ length: 500 }, (_, i) => `line ${i} \x1b[31mx\x1b[0m`).join("\n"));
	const r = readResultTail({ id, kind: "pi-job" }, root);
	assert.ok(r.ok);
	assert.equal(r.lines.length, 200);
	assert.equal(r.lines.at(-1), "line 499 x");
	const big = "20261030224430-big1";
	mkdirSync(join(root, big));
	writeFileSync(join(root, big, "result.md"), "y".repeat(10 * 1024 * 1024) + "\nlast");
	const rb = readResultTail({ id: big, kind: "pi-job" }, root);
	assert.ok(rb.ok && rb.lines.at(-1) === "last" && rb.lines.join("").length <= 128 * 1024);
	const fifo = "20261030224430-fifo";
	mkdirSync(join(root, fifo));
	execFileSync("mkfifo", [join(root, fifo, "result.md")]);
	const t0 = Date.now();
	assert.equal(readResultTail({ id: fifo, kind: "pi-job" }, root).ok, false);
	assert.ok(Date.now() - t0 < 1000, "a FIFO does not block");
	assert.equal(readResultTail({ id: "../../etc", kind: "pi-job" }, root).ok, false);
	assert.equal(readResultTail({ id: "agent-abc123", kind: "claude-bg" }, root).ok, false);
	assert.equal(readResultTail({ id: "20991231000000-none", kind: "pi-job" }, root).ok, false);
});

test("osc52: base64 of one clean line", () => {
	assert.equal(osc52("abc"), "\x1b]52;c;YWJj\x07");
	assert.equal(osc52("a\nb\x1b"), osc52("ab"), "no line break or escape reaches the clipboard");
	assert.equal(osc52("x".repeat(200_000)), undefined, "too long for OSC 52");
});
