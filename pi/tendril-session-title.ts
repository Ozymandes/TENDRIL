// TENDRIL session auto-titler (DESIGN-FINAL-POLISH §11) — installed by the
// TENDRIL installer beside Herdr's own integration:
//   cp pi/tendril-session-title.ts ~/.pi/agent/extensions/
//
// On the FIRST `before_agent_start` (and on `session_start` for resumed
// sessions) this extension names the session LOCALLY when it has no name:
//   1. a pending native title the host queued for this pane
//      (~/.local/state/tendril/pending-titles.json — scheduler launches and
//      manual renames land here), else
//   2. a deterministic title derived from the first user instruction.
// It NEVER renames an already-named session, never touches subagent child
// sessions, and performs no network and no LLM calls.
//
// Titles are authored/locally-derived tokens (<= 32 chars here, <= 48 on
// the wire) — never terminal content. The host resolves the wire title
// independently (bin/tendril_titles.py); both derivers share one contract:
// bin/title-vectors.json (keep them in lockstep).

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const DERIVE_CAP = 32;
const PENDING_TTL_S = 600;
const PENDING_SCHEMA = 1;
const SCAN_MAX = 512 * 1024;
const INSTRUCTION_CAP = 8192;
const POLL_MS = 10000;

const SCHEDULER_NAME_RE = /^tendril-[0-9a-f]{8}$/;

// ---------------------------------------------------------------------------
// the deterministic deriver — kept in lockstep with bin/tendril_titles.py;
// bin/title-vectors.json is the shared test contract for both.

const BANNED = new Set([
	"coding task", "general session", "pi agent", "new session", "help",
	"hi", "hello", "hey", "yo", "session", "task", "coding", "continue",
	"done", "ok", "okay", "yes", "no", "test", "testing", "prompt",
	"assistant", "agent", "the task", "my task", "greetings", "howdy",
]);

const GREETING_RE = /^(hi+|hello+|hey+|yo+|howdy|greetings|good (morning|afternoon|evening|day))\b/;
const FILLERS = [
	"please", "pls", "plz", "hey", "hi", "ok", "okay", "so", "then", "now",
	"also", "and", "but", "can you", "could you", "would you", "will you",
	"i want you to", "i need you to", "i'd like you to", "lets", "let's",
	"let us", "we need to", "we should", "help me", "try to", "kindly",
];
const PREAMBLE_RE = /^(you are|you're|you will act|you must act|act as|as an? [a-z]|your role|i want you to act|take the role|role:)\b/i;
const HEADER_RE = /^#{1,6}\s+/;
const BANNER_RE = /^([-_=*~·─-╿]{5,})$/;
const QUOTE_RE = /^>\s?/;
const LIST_RE = /^([-*+•]|\d{1,2}[.)])\s+/;
const MD_EMPH_RE = /^[*_`#>~\s]+|[*_`#>~\s]+$/g;
const URL_RE = /^(https?:\/\/|www\.)\S+$|^\S+\/(?:\S+\/)*\S+$/i;
const EMAIL_RE = /^[\w.+-]+@[\w-]+\.[\w.-]+$/;
const FLAG_RE = /^-{1,2}[A-Za-z][A-Za-z0-9-]*$/;
const HEX_RE = /^[0-9a-fA-F]{8,}$/;
const UUIDISH_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const SECRET_RE = /^(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|gho_[A-Za-z0-9]{8,}|xox[bp]-[A-Za-z0-9-]{8,}|AKIA[A-Z0-9]{8,}|Bearer\s+\S{8,})$/;
const LONG_TOKEN_RE = /^[A-Za-z0-9_+/=-]{20,}$/;
const WORD_RE = /[A-Za-z\u00c0-\u024f]{2,}/;
const CLAUSE_SPLIT_RE = /(?<=[.!?])\s+|\s+[—–|;]\s*|\s*:\s+|,\s+(?=and |then |but )/;
const TOKEN_SPLIT_RE = /\s+/;
const SHELL_SHAPE_RE = /[|><;]|&&|\s-{1,2}[A-Za-z]|\$\(/;
const SHELL_WORDS = new Set([
	"ps", "ls", "cd", "cat", "grep", "rm", "mv", "cp", "echo", "git", "npm",
	"npx", "pnpm", "yarn", "python", "python3", "pip", "curl", "wget", "ssh",
	"scp", "make", "cargo", "docker", "kubectl", "systemctl", "sudo", "awk",
	"sed", "find", "tail", "head", "jq", "xargs", "chmod", "chown", "sh",
	"bash", "zsh", "node", "deno", "bun", "vi", "vim", "tmux", "ln", "mkdir",
	"touch", "diff", "patch", "sort", "uniq", "wc", "tr", "cut", "journalctl",
]);
const STOPWORDS = new Set(`
a an the and or but if then else for of to in on at by with from as is are
was were be been being do does did doing have has had i you he she it we
they me him her us them my your his its our their this that these those
what which who whom when where why how all any both each few more most
other some such no nor not only own same so than too very can will just
should now about into over under again further once here there why's
what's let lets get got make made need wants want like also very ok okay
sure thanks please yeah yup nah maybe stuff things work works keep keeps
keeping going still
`.split(/\s+/));

function shoutyOrPreserve(token) {
	if (/\d/.test(token)) return true;
	if (/[a-z]/.test(token)) return true;
	if (!/[A-Z]/.test(token)) return true;
	const segments = token.split(/[-/_.]+/).filter((s) => s.length > 0);
	if (segments.length === 0) return true;
	if (segments.some((s) => s.length <= 4)) return true; // LIVE, RC2, IT, Z.AI
	return false; // DESIGN-LANGUAGE, UPLIFT → sentence case
}

function shoutyClause(clause) {
	const words = clause.split(TOKEN_SPLIT_RE).filter((w) => w.length > 0);
	return words.length > 0 && words.every(
		(w) => w === w.toUpperCase() && /[A-Za-z]/.test(w));
}

function cleanToken(tok) {
	return tok.replace(MD_EMPH_RE, "").trim().replace(/^[.,;:!?()[\]{}"']+|[.,;:!?()[\]{}"']+$/g, "");
}

function isNoise(tok) {
	if (!tok) return true;
	if (URL_RE.test(tok) || EMAIL_RE.test(tok) || FLAG_RE.test(tok)) return true;
	if (HEX_RE.test(tok) || UUIDISH_RE.test(tok) || SECRET_RE.test(tok)) return true;
	if (LONG_TOKEN_RE.test(tok)) {
		const alpha = [...tok].filter((c) => /[a-zA-Z]/.test(c)).length;
		let classes = 0;
		if (/[a-z]/.test(tok)) classes += 1;
		if (/[A-Z]/.test(tok)) classes += 1;
		if (/\d/.test(tok)) classes += 1;
		if (/[^A-Za-z0-9]/.test(tok)) classes += 1;
		// credential-shaped: long, mixed classes, not sentence vocabulary
		if (classes >= 3 && alpha < tok.length / 2) return true;
	}
	return false;
}

function meaningfulWords(words) {
	return words.filter((w) => WORD_RE.test(w));
}

function sentenceCase(words) {
	return words.map((w, i) => {
		if (i === 0) {
			if (!shoutyOrPreserve(w)) {
				return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
			}
			if (/[A-Z]/.test(w.slice(1))) return w;
			return w.charAt(0).toUpperCase() + w.slice(1);
		}
		return shoutyOrPreserve(w) ? w : w.toLowerCase();
	});
}

function normalizeCandidate(text) {
	if (typeof text !== "string" || !text.trim()) return null;
	const lines = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
	let inFence = false;
	let candidate = null;
	for (const raw of lines) {
		let line = raw.trim();
		if (line.startsWith("```") || line.startsWith("~~~")) {
			inFence = !inFence;
			continue;
		}
		if (inFence || !line) continue;
		if (BANNER_RE.test(line) || QUOTE_RE.test(line) || line.startsWith("|")) {
			continue;
		}
		// a markdown header IS a title-shaped line: use its text
		line = line.replace(HEADER_RE, "").trim();
		if (!line) continue;
		// role preambles: drop the preamble SENTENCES, keep the rest
		while (line && PREAMBLE_RE.test(line)) {
			const idx = line.search(/(?<=[.!?])\s/);
			if (idx >= 0 && line.slice(idx + 1).trim()) {
				line = line.slice(idx + 1).trim();
			} else {
				line = "";
			}
		}
		if (!line) continue;
		const first = cleanToken(line.split(TOKEN_SPLIT_RE, 1)[0]).toLowerCase();
		if (SHELL_WORDS.has(first) && SHELL_SHAPE_RE.test(line)) continue;
		const stripped = line.replace(LIST_RE, "");
		if (!stripped) continue;
		candidate = stripped;
		break;
	}
	if (!candidate) return null;
	candidate = candidate.replace(MD_EMPH_RE, "").trim();
	if (!candidate) return null;
	const clauses = candidate.split(CLAUSE_SPLIT_RE)
		.map((c) => (c || "").trim()).filter((c) => c.length > 0);
	if (clauses.length === 0) return null;
	let clause = clauses[0];
	const nWords = meaningfulWords(clause.split(/\s+/)).length;
	if (nWords < 2 && clauses.length > 1) {
		clause = clauses[1];
	} else if (nWords <= 2 && clauses.length > 1 && shoutyClause(clause)) {
		clause = clauses[1];
	}
	// iteratively strip leading politeness/filler phrases
	let changed = true;
	while (changed) {
		changed = false;
		const low = clause.toLowerCase();
		for (const f of FILLERS) {
			if (low.startsWith(f + " ") || low === f) {
				clause = clause.slice(f.length).replace(/^[ ,.:;\-—–]+/, "");
				changed = true;
				break;
			}
		}
	}
	return clause || null;
}

function finalize(words, limit) {
	const cleaned = meaningfulWords(
		words.map((w) => cleanToken(w)).filter((w) => w.length > 0));
	if (cleaned.length < 2) return null;
	let title = sentenceCase(cleaned).join(" ");
	title = title.replace(/^[\t ,;:—–\-·]+|[\t ,;:—–\-·]+$/g, "");
	if (title.length > limit) {
		let cut = title.slice(0, limit);
		const sp = cut.lastIndexOf(" ");
		if (sp > 0) cut = cut.slice(0, sp);
		title = cut.replace(/^[\t ,;:—–\-·]+|[\t ,;:—–\-·]+$/g, "");
	}
	const letters = [...title].filter((c) => /[a-zA-Z\u00c0-\u024f]/.test(c)).length;
	const body = title.replace(/ /g, "").length;
	if (letters < 4 || letters < body / 2) return null;
	const norm = title.toLowerCase().replace(/[^a-z0-9 ]/g, "").trim();
	if (BANNED.has(norm) || GREETING_RE.test(norm)) return null;
	return title.length > 0 ? title : null;
}

export function deriveTitle(text, limit = DERIVE_CAP) {
	const clause = normalizeCandidate(text);
	if (clause === null) return null;
	const words = clause.split(TOKEN_SPLIT_RE).filter((w) => w.length > 0);
	const kept = words.filter((w) => !isNoise(cleanToken(w)));
	return finalize(kept, limit);
}

export function deriveSummary(turns, limit = DERIVE_CAP) {
	if (!Array.isArray(turns)) return null;
	turns = turns.filter((t) => typeof t === "string" && t.trim()).slice(0, 3);
	if (turns.length === 0) return null;
	const scores = new Map(); // lowercase key → {score, first}
	let order = 0;
	for (const turn of turns) {
		const seen = new Set();
		for (const raw of turn.replace(/\n/g, " ").split(TOKEN_SPLIT_RE)) {
			const tok = cleanToken(raw);
			if (!tok || isNoise(tok) || !WORD_RE.test(tok)) continue;
			const key = tok.toLowerCase();
			if (STOPWORDS.has(key)) continue;
			if (!scores.has(key)) scores.set(key, { score: 0, first: order++ });
			const entry = scores.get(key);
			if (!seen.has(key)) {
				entry.score += 3; // presence per turn
				seen.add(key);
			}
			if (/[A-Z]/.test(tok.slice(1))) entry.score += 3; // CamelCase
			else if (shoutyOrPreserve(tok) && tok !== tok.toLowerCase()) entry.score += 3;
			if (/\d/.test(tok)) entry.score += 2;
			if (/[._+/-]/.test(tok)) entry.score += 1;
		}
	}
	const ranked = [...scores.keys()].sort((a, b) => {
		const ea = scores.get(a);
		const eb = scores.get(b);
		if (ea.score !== eb.score) return eb.score - ea.score;
		return ea.first - eb.first;
	});
	const top = ranked.slice(0, 2).filter((k) => scores.get(k).score >= 3);
	if (top.length < 2) return null;
	// rebuild with the first-seen casing of each term
	const cased = new Map();
	for (const turn of turns) {
		for (const raw of turn.replace(/\n/g, " ").split(TOKEN_SPLIT_RE)) {
			const tok = cleanToken(raw);
			if (tok && scores.has(tok.toLowerCase()) && !cased.has(tok.toLowerCase())) {
				cased.set(tok.toLowerCase(), tok);
			}
		}
	}
	return finalize(top.map((k) => cased.get(k)), limit);
}

export function humanizeRole(name) {
	if (typeof name !== "string" || !name.trim()) return null;
	let n = name.trim().replace(/[_-][0-9a-fA-F]{6,8}$/, "").replace(/[_\s]+/g, " ").trim().replace(/^[ ._-]+|[ ._-]+$/g, "");
	if (!n) return null;
	if (n.charAt(0) >= "a" && n.charAt(0) <= "z") {
		n = n.charAt(0).toUpperCase() + n.slice(1);
	}
	return n;
}

// ---------------------------------------------------------------------------
// bounded transcript reads (own session only; metadata + first instruction)

function sessionFileOf(ctx) {
	try {
		const file = ctx?.sessionManager?.getSessionFile?.();
		return typeof file === "string" && file.startsWith("/") ? file : null;
	} catch {
		return null;
	}
}

function readHeader(file) {
	const out = { parent: null };
	try {
		const fd = fs.openSync(file, "r");
		try {
			const buf = Buffer.alloc(4096);
			const n = fs.readSync(fd, buf, 0, 4096, 0);
			const text = buf.subarray(0, n).toString("utf8");
			const line = text.split("\n", 1)[0];
			const d = JSON.parse(line);
			if (d && d.type === "session" && typeof d.parentSession === "string") {
				out.parent = d.parentSession;
			}
		} finally {
			fs.closeSync(fd);
		}
	} catch {
		// unreadable header: treat as parentless
	}
	return out;
}

function isChildLayout(file) {
	// pi-subagents layouts: <stem>/<runId>/run-*/session.jsonl and <stem>/forks/
	const parts = file.split(path.sep);
	if (parts.includes("forks")) return true;
	const dir = path.dirname(file);
	return /\/run-[^/]*$/.test(dir) && file.endsWith("/session.jsonl");
}

function firstInstructionFromFile(file, maxTurns = 1) {
	const turns = [];
	try {
		const size = fs.statSync(file).size;
		if (size <= 0) return turns;
		const fd = fs.openSync(file, "r");
		try {
			const buf = Buffer.alloc(Math.min(size, SCAN_MAX));
			fs.readSync(fd, buf, 0, buf.length, 0);
			for (const line of buf.toString("utf8").split("\n")) {
				if (turns.length >= maxTurns) break;
				if (!line.trim() || line.length > 262144) continue;
				let d;
				try {
					d = JSON.parse(line);
				} catch {
					continue;
				}
				if (!d || (d.type !== "message" && d.type !== "user")) continue;
				const m = d.message;
				if (!m || m.role !== "user") continue;
				const c = m.content;
				let text = null;
				if (typeof c === "string") text = c;
				else if (Array.isArray(c)) {
					const parts = [];
					for (const item of c) {
						if (item && item.type === "text" && typeof item.text === "string") {
							parts.push(item.text);
						}
					}
					text = parts.length > 0 ? parts.join("\n") : null;
				}
				if (text && text.trim()) turns.push(text.trim().slice(0, INSTRUCTION_CAP));
			}
		} finally {
			fs.closeSync(fd);
		}
	} catch {
		// unreadable: no derived title this round
	}
	return turns;
}

// ---------------------------------------------------------------------------
// pending native titles (the host queues; the pane's own extension consumes)

function statePath(name) {
	return path.join(os.homedir(), ".local", "state", "tendril", name);
}

const PANE_RE = /^[A-Za-z0-9]+:[A-Za-z0-9]+$/;

function readPending() {
	try {
		const doc = JSON.parse(fs.readFileSync(statePath("pending-titles.json"), "utf8"));
		if (doc && doc.schema_version === PENDING_SCHEMA && Array.isArray(doc.entries)) {
			return doc.entries.filter((e) => e && typeof e === "object");
		}
	} catch {
		// absent/corrupt: nothing pending
	}
	return [];
}

function consumePending(paneId, nowSec) {
	if (!PANE_RE.test(paneId || "")) return null;
	const entries = readPending();
	let match = null;
	const keep = [];
	for (const e of entries) {
		const ts = typeof e.ts === "number" ? e.ts : 0;
		if (!ts || nowSec - ts >= PENDING_TTL_S) continue;
		if (e.pane_id === paneId && match === null && typeof e.title === "string" && e.title.trim()) {
			match = e.title.trim().slice(0, 48);
		} else {
			keep.push(e);
		}
	}
	if (match === null) return null;
	try {
		fs.mkdirSync(path.dirname(statePath("pending-titles.json")), { recursive: true, mode: 0o700 });
		const tmp = statePath("pending-titles.json") + ".tmp." + process.pid + "." + Math.random().toString(36).slice(2);
		fs.writeFileSync(tmp, JSON.stringify({ schema_version: PENDING_SCHEMA, entries: keep }));
		fs.renameSync(tmp, statePath("pending-titles.json"));
	} catch {
		// consumed in memory even if the rewrite races: the pane applies the
		// name once; a lost rewrite only leaves the entry to expire.
	}
	return match;
}

// ---------------------------------------------------------------------------

export default function (pi) {
	let checkedThisSession = false;
	let sessionFile = null;
	let paneId = process.env.HERDR_PANE_ID || null;
	let pollTimer = null;

	function isChild() {
		if (!sessionFile) return false;
		if (isChildLayout(sessionFile)) return true;
		return !!readHeader(sessionFile).parent;
	}

	function applyName(name, why) {
		if (!name || typeof pi.getSessionName === "function" && pi.getSessionName()) {
			return;
		}
		try {
			pi.setSessionName(name);
		} catch {
			// naming must never disturb the run
		}
	}

	function tryPending(nowSec) {
		if (!paneId || !PANE_RE.test(paneId)) return false;
		const title = consumePending(paneId, nowSec);
		if (title) {
			applyName(title, "pending");
			return true;
		}
		return false;
	}

	function deriveFromPrompt(prompt) {
		const title = deriveTitle(prompt);
		if (title) applyName(title, "instruction");
	}

	pi.on("session_start", async (_event, ctx) => {
		sessionFile = sessionFileOf(ctx);
		checkedThisSession = false;
		// Resumed sessions: name them from their own first instruction when
		// they have no name yet. Fresh sessions wait for the first prompt.
		try {
			const reason = _event?.reason;
			if ((reason === "resume" || reason === "startup") && !isChild()
				&& !(typeof pi.getSessionName === "function" && pi.getSessionName())) {
				if (tryPending(Math.floor(Date.now() / 1000))) return;
				if (sessionFile) {
					const turns = firstInstructionFromFile(sessionFile, 1);
					if (turns.length > 0) deriveFromPrompt(turns[0]);
				}
			}
		} catch {
			// never fail a session start
		}
		// low-frequency poll so a manual rename reaches the live native
		// name without waiting for the next prompt
		if (!pollTimer && paneId) {
			pollTimer = setInterval(() => {
				try {
					if (pi.getSessionName && pi.getSessionName()) return;
					if (isChild()) return;
					tryPending(Math.floor(Date.now() / 1000));
				} catch {
					// keep polling quietly
				}
			}, POLL_MS);
			if (pollTimer.unref) pollTimer.unref();
		}
	});

	pi.on("before_agent_start", async (event) => {
		if (checkedThisSession && (pi.getSessionName && pi.getSessionName())) {
			return undefined;
		}
		if (typeof pi.getSessionName === "function" && pi.getSessionName()) {
			checkedThisSession = true;
			return undefined;
		}
		checkedThisSession = true;
		if (isChild()) return undefined; // subagent children: never renamed
		if (tryPending(Math.floor(Date.now() / 1000))) return undefined;
		deriveFromPrompt(event?.prompt);
		return undefined;
	});
}
