/**
 * Agent inbox for pi (docs/design/AGENTS-INBOX.md, D24/D25): data, CLI calls, the overlay component and the widget line.
 *
 * Data comes only from `apex-router agents --json` (section 4). A resolve is `apex-router agents resolve <id> <key>|--all`
 * (section 5). Every CLI call uses execFile (no shell), a 3 s deadline and a 2 MiB stdout cap (section 9). At most one
 * CLI call is in flight: a refresh during a call joins it; a resolve during a call gets "busy".
 *
 * The width functions come from pi-tui (the only width measure, section 7). They are passed in (TuiFns), because this
 * repo has no node_modules for the tests; apex-agents.ts passes the real pi-tui functions.
 * Every text from the CLI is cleaned (no control or escape characters) and capped before it reaches a line.
 */
import { execFile } from "node:child_process";

export const DEFAULT_TIMEOUT_MS = 3000;
export const MAX_STDOUT = 2 * 1024 * 1024;
export const MAX_ITEMS = 200;
export const MAX_OPEN = 50;
export const RESULT_LINES = 200;
export const REFRESH_MS = 5000;
export const TOGGLE_KEY = "ctrl+shift+a";
const OSC52_MAX = 100_000;

/** A safe item id: one argv word that is not an option and not a path. */
export const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
/** A blocker key: sha256, 16 hex. */
export const KEY_RE = /^[0-9a-f]{16}$/;

// ---- text cleaning -------------------------------------------------------------------------------------------

// CSI, OSC (BEL or ST end), other ESC sequences; then C0/C1 controls, DEL, bidi and zero-width format characters.
const ANSI_RE = /\x1b\[[0-9;?<=>!]*[ -\/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[PX^_][^\x1b]*(?:\x1b\\)?|\x1b[@-Z\\-_]?/g;
const CTRL_RE = /[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]/g;

function capChars(s: string, max: number): string {
	if (s.length <= max) return s;
	let out = "";
	for (const ch of s) {
		if (out.length + ch.length > max - 1) break;
		out += ch;
	}
	return `${out}…`;
}

/** One line of display text: no escapes, no controls, single spaces, at most `max` UTF-16 units. */
export function clean(v: unknown, max: number): string {
	if (typeof v !== "string") return "";
	return capChars(v.replace(ANSI_RE, "").replace(/[\t\n\r\v\f]+/g, " ").replace(CTRL_RE, "").replace(/ {2,}/g, " ").trim(), max);
}

/** One line of result text: like clean, but keeps the indent. */
function cleanLine(s: string, max: number): string {
	return capChars(s.replace(ANSI_RE, "").replace(/\t/g, "  ").replace(CTRL_RE, "").trimEnd(), max);
}

// ---- 4. JSON -------------------------------------------------------------------------------------------------

export type Group = "needs" | "running" | "done";
export interface Blocker { key: string; pri: string | null; text: string; resolved: boolean }
export interface Item {
	id: string; kind: string; role: string; group: Group; state: string; verdict: string | null;
	label: string; cwd_short: string; branch: string | null; ahead: number | null;
	start: number | null; end: number | null; cost: number | null;
	open: Blocker[]; now: { tool: string; short: string } | null; resume: string | null;
}
export interface Inbox { now: number; complete: boolean; counts: { running: number; needs: number; done: number }; items: Item[] }
export type Parsed = { ok: true; inbox: Inbox } | { ok: false; reason: string };

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const cnt = (v: unknown): number => {
	const n = num(v);
	return n === null || n < 0 ? 0 : Math.min(Math.floor(n), 1e9);
};
const GROUPS = new Set<Group>(["needs", "running", "done"]);
const PRI = new Set(["P1", "P2", "P3"]);
const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

function parseItem(r: unknown): Item | null {
	if (!isObj(r) || typeof r.id !== "string" || !ID_RE.test(r.id) || !GROUPS.has(r.group as Group)) return null;
	const open = (Array.isArray(r.open) ? r.open.slice(0, MAX_OPEN) : []).filter(isObj).map(b => ({
		key: typeof b.key === "string" && KEY_RE.test(b.key) ? b.key : "",
		pri: typeof b.pri === "string" && PRI.has(b.pri) ? b.pri : null,
		text: clean(b.text, 160),
		resolved: b.resolved === true,
	}));
	const now = isObj(r.now) ? { tool: clean(r.now.tool, 20), short: clean(r.now.short, 80) } : null;
	return {
		id: r.id, kind: clean(r.kind, 16), role: clean(r.role, 16), group: r.group as Group,
		state: clean(r.state, 16), verdict: clean(r.verdict, 16) || null,
		label: clean(r.label, 60) || r.id, cwd_short: clean(r.cwd_short, 60), branch: clean(r.branch, 80) || null,
		ahead: num(r.ahead), start: num(r.start), end: num(r.end), cost: num(r.cost),
		open, now: now && (now.tool || now.short) ? now : null, resume: clean(r.resume, 1000) || null,
	};
}

/** Parse `agents --json` stdout. Never throws. */
export function parseInbox(stdout: string): Parsed {
	let j: unknown;
	try {
		j = JSON.parse(stdout);
	} catch {
		return { ok: false, reason: "bad JSON from apex-router agents" };
	}
	if (!isObj(j) || !Array.isArray(j.items)) return { ok: false, reason: "unexpected JSON shape from apex-router agents" };
	const items = j.items.slice(0, MAX_ITEMS * 2).map(parseItem).filter((x): x is Item => x !== null).slice(0, MAX_ITEMS);
	const c = isObj(j.counts) ? j.counts : {};
	return {
		ok: true,
		inbox: {
			now: num(j.now) ?? Date.now() / 1000,
			complete: j.complete !== false,
			counts: { running: cnt(c.running), needs: cnt(c.needs), done: cnt(c.done) },
			items,
		},
	};
}

// ---- 9. CLI calls ----------------------------------------------------------------------------------------------

export type CliResult = { ok: true; stdout: string } | { ok: false; reason: string };
export type CliRunner = (args: string[]) => Promise<CliResult>;

/** execFile (no shell) with a deadline and a stdout cap. Never rejects. */
export function makeCliRunner(o: { bin?: string; prefix?: string[]; timeoutMs?: number; maxBytes?: number } = {}): CliRunner {
	const bin = o.bin ?? process.env.APEX_ROUTER_BIN ?? "apex-router";
	const timeout = o.timeoutMs ?? DEFAULT_TIMEOUT_MS;
	const maxBuffer = o.maxBytes ?? MAX_STDOUT;
	return args =>
		new Promise<CliResult>(resolve => {
			let done = false;
			const finish = (r: CliResult) => {
				if (done) return;
				done = true;
				clearTimeout(guard);
				resolve(r);
			};
			// Backstop: a child that ignores SIGKILL or keeps a pipe open must not hold the UI.
			const guard = setTimeout(() => finish({ ok: false, reason: `timeout after ${timeout / 1000} s` }), timeout + 1000);
			guard.unref?.();
			try {
				execFile(bin, [...(o.prefix ?? []), ...args], { shell: false, timeout, maxBuffer, killSignal: "SIGKILL", encoding: "utf8", windowsHide: true }, (err, stdout, stderr) => {
					if (!err) return finish({ ok: true, stdout: String(stdout) });
					const e = err as NodeJS.ErrnoException & { killed?: boolean; signal?: string | null; code?: unknown };
					if (e.code === "ENOENT") return finish({ ok: false, reason: `${clean(bin, 60)} not found` });
					if (e.code === "ERR_CHILD_PROCESS_STDIO_MAXBUFFER") return finish({ ok: false, reason: `output too large (over ${Math.round(maxBuffer / 1024)} KiB)` });
					if (e.killed || e.signal) return finish({ ok: false, reason: `timeout after ${timeout / 1000} s` });
					const first = clean(String(stderr ?? "").split("\n").find(l => l.trim()) ?? "", 90);
					finish({ ok: false, reason: `exit ${typeof e.code === "number" ? e.code : "?"}${first ? `: ${first}` : ""}` });
				});
			} catch (e) {
				finish({ ok: false, reason: clean((e as Error).message, 100) || "start failed" });
			}
		});
}

// ---- store -----------------------------------------------------------------------------------------------------

export class InboxStore {
	data: Inbox | undefined;
	error: string | undefined;
	/** True after a load that had a running item or a needs item: then a later failure shows in the widget. */
	shownBefore = false;
	private readonly run: CliRunner;
	private inflight: Promise<unknown> | null = null;
	private loading: Promise<void> | null = null;

	constructor(o: { run: CliRunner }) {
		this.run = o.run;
	}

	get busy(): boolean {
		return this.inflight !== null;
	}

	private track<T>(p: Promise<T>): Promise<T> {
		this.inflight = p;
		void p.finally(() => {
			if (this.inflight === p) this.inflight = null;
		});
		return p;
	}

	/** Load the list. A call during a load joins it. During a resolve it waits for it, then loads. Never rejects. */
	refresh(): Promise<void> {
		if (this.loading) return this.loading;
		const after = this.inflight ?? Promise.resolve();
		const p = (async () => {
			await after.catch(() => undefined);
			const r = await this.track(this.run(["agents", "--json"]));
			if (!r.ok) {
				this.error = r.reason;
				return;
			}
			const parsed = parseInbox(r.stdout);
			if (!parsed.ok) {
				this.error = parsed.reason;
				return;
			}
			this.data = parsed.inbox;
			this.error = undefined;
			if (parsed.inbox.counts.running + parsed.inbox.counts.needs > 0) this.shownBefore = true;
		})().catch(e => {
			this.error = clean((e as Error)?.message, 100) || "load failed";
		});
		this.loading = p;
		void p.finally(() => {
			if (this.loading === p) this.loading = null;
		});
		return p;
	}

	/** The masked result.md tail of a pi job from `apex-router agents result <id>` (xval x2e6: a direct read of
	 *  result.md showed secrets and prompt text). Never rejects. */
	async result(it: { id: string; kind: string }): Promise<ResultTail> {
		if (it.kind !== "pi-job") return { ok: false, reason: "No result.md for this kind of agent" };
		if (!ID_RE.test(it.id) || it.id.includes("..")) return { ok: false, reason: "bad item id" };
		const r = await this.run(["agents", "result", it.id]);
		if (!r.ok) return { ok: false, reason: r.reason.includes("no result.md") ? "No result.md yet" : r.reason };
		const lines = r.stdout.split("\n");
		while (lines.length && lines.at(-1) === "") lines.pop();
		return { ok: true, lines: lines.slice(-RESULT_LINES) };
	}

	/** Resolve one blocker (key) or all (--all). Refuses unsafe input and a call while another call runs. */
	async resolve(id: string, key: string): Promise<{ ok: boolean; reason?: string }> {
		if (!ID_RE.test(id)) return { ok: false, reason: "bad item id" };
		if (key !== "--all" && !KEY_RE.test(key)) return { ok: false, reason: "bad blocker key" };
		if (this.inflight || this.loading) return { ok: false, reason: "busy, try again" };
		const r = await this.track(this.run(["agents", "resolve", id, key]));
		if (!r.ok) return { ok: false, reason: r.reason };
		await this.refresh();
		return { ok: true };
	}
}

// ---- widget ----------------------------------------------------------------------------------------------------

export type WidgetText = { text: string; tone: "normal" | "muted" } | undefined;

/** The widget content: one line while an item runs or needs you; else nothing. */
export function widgetText(s: InboxStore): WidgetText {
	if (s.error) return s.shownBefore ? { text: `Inbox not loaded: ${s.error}`, tone: "muted" } : undefined;
	if (!s.data) return undefined;
	const { running, needs } = s.data.counts;
	if (running + needs === 0) return undefined;
	const parts = [running > 0 ? `● ${running} running` : "", needs > 0 ? `⚠ ${needs} need you` : ""].filter(Boolean);
	return { text: parts.join("  "), tone: "normal" };
}

/** The widget line, coloured and cut to the width. */
export function widgetLine(w: WidgetText, theme: ThemeFns, tui: TuiFns, width: number): string[] {
	if (!w || width < 1) return [];
	if (w.tone === "muted") return [tui.truncateToWidth(theme.fg("muted", w.text), width)];
	const styled = w.text
		.replace(/● \d+ running/, m => theme.fg("accent", m))
		.replace(/⚠ \d+ need you/, m => theme.fg("warning", m));
	return [tui.truncateToWidth(styled, width)];
}

/** Refresh every intervalMs. No call while busy (pi streams). At most one call in flight. Returns stop(). */
export function startPoller(o: { store: InboxStore; intervalMs?: number; isBusy?: () => boolean; onUpdate: () => void }): () => void {
	let stopped = false;
	let running = false;
	const tick = async () => {
		if (stopped || running) return;
		try {
			if (o.isBusy?.()) return;
		} catch {
			return;
		}
		running = true;
		try {
			await o.store.refresh();
			if (!stopped) o.onUpdate();
		} catch {
			/* never throws into the timer */
		} finally {
			running = false;
		}
	};
	const t = setInterval(() => void tick(), o.intervalMs ?? REFRESH_MS);
	t.unref?.();
	void tick();
	return () => {
		stopped = true;
		clearInterval(t);
	};
}

// ---- overlay helpers ---------------------------------------------------------------------------------------------

/** Right half (minWidth 60); full width under 120 columns. */
export function overlayOptionsFor(columns: number) {
	if (columns < 120) return { width: "100%" as const, anchor: "right-center" as const, maxHeight: "100%" as const };
	return { width: "50%" as const, minWidth: 60, anchor: "right-center" as const, maxHeight: "100%" as const };
}

/** OSC 52 copy sequence for one clean line, or undefined when the text is too long. */
export function osc52(text: string): string | undefined {
	const t = text.replace(ANSI_RE, "").replace(CTRL_RE, "");
	const b = Buffer.from(t, "utf8").toString("base64");
	return b.length > OSC52_MAX ? undefined : `\x1b]52;c;${b}\x07`;
}

/** Copy with OSC 52 when stdout is a terminal; else the caller shows the text. */
export function copyOrShow(text: string, out: NodeJS.WriteStream = process.stdout): "copied" | "shown" {
	const seq = osc52(text);
	if (!seq || !out.isTTY) return "shown";
	try {
		out.write(seq);
		return "copied";
	} catch {
		return "shown";
	}
}

export type ResultTail = { ok: true; lines: string[] } | { ok: false; reason: string };

// ---- overlay component ---------------------------------------------------------------------------------------------

export interface TuiFns {
	visibleWidth(s: string): number;
	truncateToWidth(s: string, w: number, ellipsis?: string, pad?: boolean): string;
	matchesKey(data: string, key: string): boolean;
}
export interface ThemeFns {
	fg(color: string, s: string): string;
	bg(color: string, s: string): string;
	bold(s: string): string;
}

const FILTERS = ["needs", "running", "done", "all"] as const;
const FILTER_NAME = { needs: "Needs", running: "Running", done: "Done", all: "All" } as const;
const FAILED = new Set(["failed", "timed_out"]);

function age(sec: number): string {
	const s = Math.max(0, Math.floor(sec));
	if (s < 60) return `${s}s`;
	if (s < 3600) return `${Math.floor(s / 60)}m`;
	if (s < 86400) return `${Math.floor(s / 3600)}h`;
	return `${Math.floor(s / 86400)}d`;
}

export interface OverlayOpts {
	store: InboxStore;
	theme: ThemeFns;
	tui: TuiFns;
	rows?: () => number;
	onClose: () => void;
	requestRender: () => void;
	copy?: (text: string) => "copied" | "shown";
	readResult?: (it: Item) => ResultTail | Promise<ResultTail>;
	toggleKey?: string;
}

export class InboxOverlay {
	private filter = 0;
	private selectedId: string | undefined;
	private selectedIdx = 0;
	private expanded = false;
	private blocker = 0;
	private view: "list" | "result" = "list";
	private result: string[] = [];
	private resultTitle = "";
	private scroll = 0;
	private msg: { text: string; tone: "muted" | "error" } | undefined;
	private pending: Promise<void> = Promise.resolve();
	private closed = false;
	private readonly o: OverlayOpts;

	constructor(o: OverlayOpts) {
		this.o = o;
		const c = o.store.data?.counts;
		this.filter = !c || c.needs > 0 ? 0 : c.running > 0 ? 1 : 3;
	}

	/** Resolves when the last key action (resolve) ends. For tests. */
	idle(): Promise<void> {
		return this.pending;
	}

	invalidate(): void {}

	dispose(): void {
		this.closed = true;
	}

	private visible(): Item[] {
		const items = this.o.store.data?.items ?? [];
		const f = FILTERS[this.filter]!;
		return f === "all" ? items : items.filter(i => i.group === f);
	}

	private current(list = this.visible()): Item | undefined {
		if (!list.length) return undefined;
		let i = this.selectedId ? list.findIndex(x => x.id === this.selectedId) : -1;
		if (i < 0) i = Math.min(this.selectedIdx, list.length - 1);
		this.selectedIdx = i;
		this.selectedId = list[i]!.id;
		return list[i];
	}

	private openOf(it: Item): Blocker[] {
		return it.open.filter(b => !b.resolved);
	}

	private say(text: string, tone: "muted" | "error" = "muted") {
		this.msg = { text: clean(text, 1000), tone };
	}

	private key(data: string, ...names: string[]): boolean {
		return names.some(n => this.o.tui.matchesKey(data, n));
	}

	handleInput(data: string): void {
		try {
			this.input(data);
		} catch (e) {
			this.say(`Error: ${(e as Error)?.message ?? e}`, "error");
		}
		this.o.requestRender();
	}

	private close() {
		if (this.closed) return;
		this.closed = true;
		this.o.onClose();
	}

	private input(data: string): void {
		const toggle = this.o.toggleKey ?? TOGGLE_KEY;
		if (this.view === "result") {
			if (this.key(data, "escape") || data === "o" || data === "q") this.view = "list";
			else if (this.key(data, toggle)) this.close();
			else if (this.key(data, "up")) this.scroll--;
			else if (this.key(data, "down")) this.scroll++;
			else if (this.key(data, "pageUp")) this.scroll -= 10;
			else if (this.key(data, "pageDown") || data === " ") this.scroll += 10;
			else if (this.key(data, "home") || data === "g") this.scroll = 0;
			else if (this.key(data, "end") || data === "G") this.scroll = Number.MAX_SAFE_INTEGER;
			return;
		}
		if (this.key(data, "escape", toggle)) return this.close();
		const list = this.visible();
		const it = this.current(list);
		if (this.key(data, "tab", "shift+tab")) {
			this.filter = (this.filter + (this.key(data, "shift+tab") ? FILTERS.length - 1 : 1)) % FILTERS.length;
			this.selectedId = undefined;
			this.selectedIdx = 0;
			this.expanded = false;
			this.blocker = 0;
			this.msg = undefined;
			return;
		}
		if (this.key(data, "up", "down")) {
			const d = this.key(data, "up") ? -1 : 1;
			const open = it ? this.openOf(it) : [];
			if (this.expanded && open.length > 1) {
				this.blocker = Math.max(0, Math.min(open.length - 1, this.blocker + d));
				return;
			}
			if (!list.length) return;
			this.selectedIdx = Math.max(0, Math.min(list.length - 1, this.selectedIdx + d));
			this.selectedId = list[this.selectedIdx]!.id;
			this.expanded = false;
			this.blocker = 0;
			this.msg = undefined;
			return;
		}
		if (this.key(data, "enter")) {
			if (!it) return;
			this.expanded = !this.expanded;
			this.blocker = 0;
			return;
		}
		if (data === "r" || data === "R" || this.key(data, "shift+r")) {
			if (!it) return this.say("Nothing selected");
			const all = data === "R" || this.key(data, "shift+r");
			let key = "--all";
			if (!all) {
				const b = this.openOf(it)[this.expanded ? this.blocker : 0];
				if (!b) return this.say("No open blocker on this item");
				if (!b.key) return this.say("This blocker has no key; use R");
				key = b.key;
			}
			this.say(all ? "Resolving all blockers…" : "Resolving blocker…");
			const id = it.id;
			this.pending = this.o.store.resolve(id, key).then(
				r => {
					this.blocker = 0;
					if (r.ok) this.say(all ? `Resolved all blockers of ${id}` : `Resolved 1 blocker of ${id}`);
					else this.say(`Not resolved: ${r.reason}`, "error");
					if (!this.closed) this.o.requestRender();
				},
				() => undefined,
			);
			return;
		}
		if (data === "c") {
			if (!it) return this.say("Nothing selected");
			if (!it.resume) return this.say("No resume command for this item");
			const how = (this.o.copy ?? copyOrShow)(it.resume);
			return this.say(how === "copied" ? `Copied: ${it.resume}` : `Resume: ${it.resume}`);
		}
		if (data === "o") {
			if (!it) return this.say("Nothing selected");
			const id = it.id;
			this.say("Loading result…");
			this.pending = Promise.resolve((this.o.readResult ?? (x => this.o.store.result(x)))(it)).then(
				r => {
					if (this.closed) return;
					if (!r.ok) this.say(r.reason);
					else {
						this.msg = undefined;
						this.result = r.lines.slice(-RESULT_LINES).map(l => cleanLine(l, 2000));
						this.resultTitle = `result.md · ${id}`;
						this.scroll = Number.MAX_SAFE_INTEGER;
						this.view = "result";
					}
					this.o.requestRender();
				},
				() => undefined,
			);
			return;
		}
	}

	// ---- render -------------------------------------------------------------------------------------------------

	private fit(s: string, w: number): string {
		return this.o.tui.truncateToWidth(s, w, "…");
	}

	/** Text wrapped by display width, at most maxLines lines; the last line ends in … when cut. */
	private wrap(s: string, w: number, maxLines: number): string[] {
		const out: string[] = [];
		let rest = s;
		while (rest && out.length < maxLines) {
			if (this.o.tui.visibleWidth(rest) <= w || out.length === maxLines - 1) {
				out.push(this.fit(rest, w));
				break;
			}
			let line = "";
			let i = 0;
			for (const ch of rest) {
				if (this.o.tui.visibleWidth(line + ch) > w) break;
				line += ch;
				i += ch.length;
			}
			if (!i) break;
			const sp = line.lastIndexOf(" ");
			if (sp > 0) i = sp + 1; // break at a space when the line has one
			out.push(rest.slice(0, i).trimEnd());
			rest = rest.slice(i).trimStart();
		}
		return out;
	}

	private tone(it: Item): { glyph: string; color: string } {
		if (FAILED.has(it.state)) return { glyph: "✗", color: "error" };
		if (it.group === "running") return { glyph: "●", color: "accent" };
		if (it.group === "needs") return { glyph: "⚠", color: "warning" };
		return { glyph: "✓", color: "success" };
	}

	private ageOf(it: Item): string {
		const now = this.o.store.data?.now ?? Date.now() / 1000;
		const t = it.group === "running" ? it.start : (it.end ?? it.start);
		return t === null ? "" : age(now - t);
	}

	render(width: number): string[] {
		const w = Math.max(1, Math.floor(width));
		const rows = Math.max(8, Math.min(200, Math.floor(this.o.rows?.() ?? 30)));
		let lines: string[];
		try {
			lines = this.view === "result" ? this.renderResult(w, rows) : this.renderList(w, rows);
		} catch (e) {
			lines = [this.o.theme.fg("muted", `Inbox not loaded: ${clean((e as Error)?.message, 100)}`)];
		}
		// Every line fits the width (contract 7); pad so the chat does not show through the panel.
		return lines.slice(0, rows).map(l => this.o.tui.truncateToWidth(l, w, "…", true));
	}

	private hr(w: number) {
		return this.o.theme.fg("dim", "─".repeat(w));
	}

	private renderResult(w: number, rows: number): string[] {
		const { theme: t } = this.o;
		const body = Math.max(1, rows - 3);
		const maxScroll = Math.max(0, this.result.length - body);
		this.scroll = Math.max(0, Math.min(this.scroll, maxScroll));
		const shown = this.result.slice(this.scroll, this.scroll + body);
		const range = this.result.length ? `${this.scroll + 1}-${this.scroll + shown.length}/${this.result.length}` : "empty";
		return [
			this.fit(`${t.bold(this.resultTitle)} ${t.fg("muted", range)}`, w),
			this.hr(w),
			...shown.map(l => this.fit(l, w)),
			...Array.from({ length: body - shown.length }, () => ""),
			this.fit(t.fg("dim", "↑↓ PgUp PgDn scroll · Esc back"), w),
		];
	}

	private renderList(w: number, rows: number): string[] {
		const { theme: t, store } = this.o;
		const out: string[] = [];
		const c = store.data?.counts;
		const all = store.data ? c!.needs + c!.running + c!.done : 0;
		const tabs = FILTERS.map((f, i) => {
			const n = f === "all" ? all : (c?.[f] ?? 0);
			const label = `${FILTER_NAME[f]} ${n}`;
			return i === this.filter ? t.bold(`[${label}]`) : t.fg("muted", label);
		}).join(" ");
		out.push(this.fit(`${t.bold("Agents")} ${tabs}${store.data && !store.data.complete ? t.fg("muted", " (partial)") : ""}`, w));
		if (store.error) out.push(this.fit(t.fg("muted", `Inbox not loaded: ${store.error}`), w));
		const hintText = "↑↓ move · ⏎ expand · Tab filter · r resolve · R all · c copy · o result · Esc close";
		const hints = this.wrap(hintText, w, 2).map(l => t.fg("dim", l));
		if (!store.data) {
			if (!store.error) out.push(this.fit(t.fg("muted", "Loading…"), w));
			return [...out, "", ...hints];
		}
		out.push(this.hr(w));
		const list = this.visible();
		const it = this.current(list);
		const msgLines = this.msg ? this.wrap(this.msg.text, w, 3).map(l => t.fg(this.msg!.tone === "error" ? "error" : "muted", l)) : [];
		const fixed = out.length + 1 /* hr */ + hints.length + msgLines.length;
		const free = Math.max(4, rows - fixed);
		const listH = Math.max(1, Math.min(list.length || 1, Math.floor(free * 0.45)));
		const detailH = Math.max(1, free - listH);
		if (!list.length) out.push(this.fit(t.fg("muted", "Nothing here."), w));
		const top = Math.max(0, Math.min(this.selectedIdx - Math.floor(listH / 2), list.length - listH));
		for (const x of list.slice(top, top + listH)) out.push(this.row(x, x.id === it?.id, w));
		out.push(this.hr(w));
		const detail = it ? this.detail(it, w) : [];
		out.push(...detail.slice(0, detailH));
		for (let i = detail.length; i < detailH; i++) out.push("");
		out.push(...msgLines);
		out.push(...hints);
		return out;
	}

	private row(x: Item, sel: boolean, w: number): string {
		const { theme: t, tui } = this.o;
		const tone = this.tone(x);
		const right = ` ${x.verdict ? `${x.verdict} ` : ""}${x.state} ${this.ageOf(x)}`;
		const rightW = tui.visibleWidth(right);
		const leftW = Math.max(1, w - rightW);
		const left = tui.truncateToWidth(`${sel ? "›" : " "} ${tone.glyph} ${x.label}`, leftW, "…", true);
		const line = rightW < w ? `${left}${t.fg(tone.color, right)}` : left;
		const fitted = tui.truncateToWidth(line, w, "…", true);
		return sel ? t.bg("selectedBg", fitted) : fitted;
	}

	private detail(it: Item, w: number): string[] {
		const { theme: t } = this.o;
		const tone = this.tone(it);
		const L: string[] = [];
		L.push(t.bold(it.label));
		L.push(t.fg("muted", `id ${it.id} · ${it.kind}${it.role ? ` · ${it.role}` : ""}`));
		L.push(`${t.bold(t.fg(tone.color, it.state))}${it.verdict ? ` · verdict ${t.bold(it.verdict)}` : ""}${this.ageOf(it) ? ` · age ${this.ageOf(it)}` : ""}${it.cost !== null ? ` · $${it.cost.toFixed(2)}` : ""}`);
		const place = [it.cwd_short, it.branch ? `branch ${it.branch}` : "", it.ahead !== null ? `ahead ${it.ahead}` : ""].filter(Boolean).join(" · ");
		if (place) L.push(place);
		if (it.group === "running" && it.now) L.push(`now: ${it.now.tool} ${it.now.short}`.trimEnd());
		const open = this.openOf(it);
		if (open.length) {
			L.push(t.bold(`Open blockers (${open.length})`));
			const shown = this.expanded ? open : open.slice(0, 3);
			shown.forEach((b, i) => {
				const cur = this.expanded && i === this.blocker;
				const pri = b.pri ? t.fg(b.pri === "P1" ? "error" : "warning", b.pri) : t.fg("muted", "--");
				const line = `${cur ? "›" : " "} ${pri} ${b.text}`;
				L.push(cur ? t.bg("selectedBg", this.fit(line, w)) : line);
			});
			if (!this.expanded && open.length > shown.length) L.push(t.fg("muted", `  +${open.length - shown.length} more (⏎)`));
		}
		if (this.expanded && it.resume) L.push(t.fg("muted", `resume: ${it.resume}`));
		return L.map(l => this.fit(l, w));
	}
}
