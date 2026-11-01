// Behavioural contract for pi/tendril-session-title.ts, run against a fake
// Pi API in a temporary HOME. Invoked by tests/test_titles.py (skipped when
// node is absent) and usable directly:
//   node pi/extension-test.mjs
//
// The extension must NEVER invent a session name: it only delivers names
// the host queued (a manual rename, a scheduled task title), records a
// `tendril-title` provenance entry beside each, lets a manual rename replace
// an existing name, and never lets a scheduled title overwrite one.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const home = fs.mkdtempSync(path.join(os.tmpdir(), "tendril-ext-"));
process.env.HOME = home;
process.env.HERDR_PANE_ID = "w1:p1";
const state = path.join(home, ".local", "state", "tendril");
fs.mkdirSync(state, { recursive: true });
const { default: install } = await import("./tendril-session-title.ts");

let failures = 0;
const check = (cond, what) => {
	if (!cond) {
		failures += 1;
		console.log(`FAIL ${what}`);
	}
};

function fakePi(initialName = null) {
	const handlers = {};
	const pi = {
		name: initialName, entries: [],
		on: (ev, fn) => { handlers[ev] = fn; },
		getSessionName: () => pi.name,
		setSessionName: (n) => { pi.name = n; },
		appendEntry: (type, data) => { pi.entries.push({ type, data }); },
	};
	install(pi);
	const sessionFile = path.join(home, "s.jsonl");
	fs.writeFileSync(sessionFile, JSON.stringify({ type: "session", id: "x" }) + "\n");
	const ctx = { sessionManager: { getSessionFile: () => sessionFile } };
	return { pi, start: () => handlers.session_start({ reason: "startup" }, ctx),
		prompt: (p) => handlers.before_agent_start({ prompt: p }) };
}

function queue(title, kind) {
	fs.writeFileSync(path.join(state, "pending-titles.json"), JSON.stringify({
		schema_version: 1,
		entries: [{ pane_id: "w1:p1", title, kind, ts: Math.floor(Date.now() / 1000) }],
	}));
}

// 1. nothing queued: the first prompt never becomes a name
{
	const f = fakePi();
	await f.start();
	await f.prompt("/home/seeno/Projects/tendril is the absolute path, use it");
	check(f.pi.name === null, "a prompt must never become the session name");
	check(f.pi.entries.length === 0, "no provenance entry without a delivered name");
}
// 2. a queued manual rename is applied, with provenance
{
	queue("Updater reliability", "manual");
	const f = fakePi();
	await f.start();
	check(f.pi.name === "Updater reliability", "manual rename applied");
	check(f.pi.entries.length === 1 && f.pi.entries[0].type === "tendril-title"
		&& f.pi.entries[0].data.source === "manual"
		&& f.pi.entries[0].data.name === "Updater reliability", "manual provenance entry");
}
// 3. a manual rename replaces an existing name (the human renamed it)
{
	queue("Chosen on the phone", "manual");
	const f = fakePi("Is the absolute path");
	await f.start();
	check(f.pi.name === "Chosen on the phone", "manual rename replaces a name");
}
// 4. a scheduled title fills an unnamed session, never a named one
{
	queue("BADM report", "scheduled");
	const f = fakePi();
	await f.prompt("whatever the task says");
	check(f.pi.name === "BADM report", "scheduled title fills an unnamed session");
	check(f.pi.entries[0]?.data?.source === "scheduler", "scheduler provenance entry");
	queue("Other task", "scheduled");
	const g = fakePi("My own name");
	await g.start();
	check(g.pi.name === "My own name", "a scheduled title never overwrites a name");
}
// 5. another pane's entry is never consumed
{
	fs.writeFileSync(path.join(state, "pending-titles.json"), JSON.stringify({
		schema_version: 1,
		entries: [{ pane_id: "w9:p9", title: "Not mine", kind: "manual", ts: Math.floor(Date.now() / 1000) }],
	}));
	const f = fakePi();
	await f.start();
	check(f.pi.name === null, "another pane's name is never applied");
}

fs.rmSync(home, { recursive: true, force: true });
if (failures > 0) {
	console.log(`${failures} failure(s)`);
	process.exit(1);
}
console.log("ok: extension contract holds (never invents a name)");
process.exit(0);
