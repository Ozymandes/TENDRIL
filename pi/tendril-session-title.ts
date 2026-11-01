// TENDRIL session-name courier — installed by the TENDRIL installer beside
// Herdr's own integration:
//   cp pi/tendril-session-title.ts ~/.pi/agent/extensions/
//
// This extension delivers ONE kind of name into Pi: a name a human or the
// scheduler chose on the host side and queued for this pane in
// ~/.local/state/tendril/pending-titles.json —
//   - a rename from TENDRIL Link or `tendril rename-session` ("manual"),
//   - the title of the scheduled task that launched this pane ("scheduled").
// It applies the name with pi.setSessionName() and appends a `tendril-title`
// custom entry recording where the name came from, because Pi's own
// session_info entry cannot say who wrote it.
//
// It NEVER invents a name. Before TENDRIL 0.2.6 this extension derived a
// title from the first prompt and stored it with setSessionName(), which Pi
// records exactly like a user's `/name` — so heuristic fragments ("Is the
// absolute path") reached every surface as the user's own session name and
// could never be corrected. Automatic titles now live only on the host
// (bin/tendril_titles.py), labelled as derived, replaceable, and never
// written into the session itself.
//
// A manual rename replaces the session's current name (the human renamed
// it); a scheduled title only fills a session that has no name yet. It
// never touches subagent child sessions, and performs no network and no
// LLM calls.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const PENDING_TTL_S = 600;
const PENDING_SCHEMA = 1;
const POLL_MS = 10000;
const TITLE_CAP = 48;
const MARKER = "tendril-title";
const PANE_RE = /^[A-Za-z0-9]+:[A-Za-z0-9]+$/;

// ---------------------------------------------------------------------------
// bounded own-session reads (header only)

function sessionFileOf(ctx) {
	try {
		const file = ctx?.sessionManager?.getSessionFile?.();
		return typeof file === "string" && file.startsWith("/") ? file : null;
	} catch {
		return null;
	}
}

function hasParentSession(file) {
	try {
		const fd = fs.openSync(file, "r");
		try {
			const buf = Buffer.alloc(4096);
			const n = fs.readSync(fd, buf, 0, 4096, 0);
			const d = JSON.parse(buf.subarray(0, n).toString("utf8").split("\n", 1)[0]);
			return !!(d && d.type === "session" && typeof d.parentSession === "string");
		} finally {
			fs.closeSync(fd);
		}
	} catch {
		return false; // unreadable header: treat as parentless
	}
}

function isChildLayout(file) {
	// pi-subagents layouts: <stem>/<runId>/run-*/session.jsonl and <stem>/forks/
	if (file.split(path.sep).includes("forks")) return true;
	return /\/run-[^/]*$/.test(path.dirname(file)) && file.endsWith("/session.jsonl");
}

// ---------------------------------------------------------------------------
// pending names (the host queues; the pane's own extension consumes)

function pendingPath() {
	return path.join(os.homedir(), ".local", "state", "tendril", "pending-titles.json");
}

function readPending() {
	try {
		const doc = JSON.parse(fs.readFileSync(pendingPath(), "utf8"));
		if (doc && doc.schema_version === PENDING_SCHEMA && Array.isArray(doc.entries)) {
			return doc.entries.filter((e) => e && typeof e === "object");
		}
	} catch {
		// absent/corrupt: nothing pending
	}
	return [];
}

// Pops this pane's entry: {title, source} | null. `kind` "manual" stays
// manual; "scheduled" is the scheduler's title.
export function consumePending(paneId, nowSec) {
	if (!PANE_RE.test(paneId || "")) return null;
	const entries = readPending();
	let match = null;
	const keep = [];
	for (const e of entries) {
		const ts = typeof e.ts === "number" ? e.ts : 0;
		if (!ts || nowSec - ts >= PENDING_TTL_S) continue;
		if (e.pane_id === paneId && match === null && typeof e.title === "string" && e.title.trim()) {
			match = {
				title: e.title.trim().slice(0, TITLE_CAP),
				source: e.kind === "manual" ? "manual" : "scheduler",
			};
		} else {
			keep.push(e);
		}
	}
	if (match === null) return null;
	try {
		fs.mkdirSync(path.dirname(pendingPath()), { recursive: true, mode: 0o700 });
		const tmp = pendingPath() + ".tmp." + process.pid + "." + Math.random().toString(36).slice(2);
		fs.writeFileSync(tmp, JSON.stringify({ schema_version: PENDING_SCHEMA, entries: keep }));
		fs.renameSync(tmp, pendingPath());
	} catch {
		// consumed in memory even if the rewrite races: the pane applies the
		// name once; a lost rewrite only leaves the entry to expire.
	}
	return match;
}

// ---------------------------------------------------------------------------

export default function (pi) {
	let sessionFile = null;
	const paneId = process.env.HERDR_PANE_ID || null;
	let pollTimer = null;

	const named = () => typeof pi.getSessionName === "function" && !!pi.getSessionName();
	const isChild = () => !!sessionFile && (isChildLayout(sessionFile) || hasParentSession(sessionFile));

	function deliverPending() {
		if (!paneId || isChild()) return;
		const hit = consumePending(paneId, Math.floor(Date.now() / 1000));
		if (!hit) return;
		if (hit.source === "scheduler" && named()) return; // never over a name
		try {
			pi.setSessionName(hit.title);
			// provenance Pi's session_info cannot carry: who chose this name
			if (typeof pi.appendEntry === "function") {
				pi.appendEntry(MARKER, { source: hit.source, name: hit.title });
			}
		} catch {
			// naming must never disturb the run
		}
	}

	pi.on("session_start", async (_event, ctx) => {
		sessionFile = sessionFileOf(ctx);
		try {
			deliverPending();
		} catch {
			// never fail a session start
		}
		// low-frequency poll so a rename from the phone reaches the live
		// session without waiting for the next prompt
		if (!pollTimer && paneId) {
			pollTimer = setInterval(() => {
				try {
					deliverPending();
				} catch {
					// keep polling quietly
				}
			}, POLL_MS);
			if (pollTimer.unref) pollTimer.unref();
		}
	});

	pi.on("before_agent_start", async () => {
		try {
			deliverPending();
		} catch {
			// never block a turn
		}
		return undefined;
	});
}
