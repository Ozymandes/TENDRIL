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
// Emphasis caps in prose (THE, ONLY, FAST, DISK, DEMO...) are NOT identity.
// A token keeps its written form only when it is a KNOWN acronym/product
// token, carries a digit (RC2, GLM-5.3, S21), or is internal-CamelCase
// (Z.AI, GitHub, iOS). Everything else sentence-cases; when unsure,
// sentence case. Map is lowercase key -> canonical display.
const ACRONYMS = new Map(Object.entries({
	live: "LIVE", rc2: "RC2", ui: "UI", ux: "UX", api: "API", ca: "CA",
	cli: "CLI", mcp: "MCP", pr: "PR", qa: "QA", glm: "GLM", apk: "APK",
	s21: "S21", ntfy: "ntfy", "z.ai": "Z.AI", pi: "Pi", herdr: "Herdr",
	tendril: "Tendril", omarchy: "Omarchy", android: "Android",
}));
// product/surface words alone are never a title ("Tendril LINK", "Pi")
const PRODUCT_WORDS = new Set(
	("tendril link pi herdr live omarchy android glm z.ai codex claude "
		+ "arch linux macos windows ui").split(" "));

function earlyBeClause(clause) {
	// narration ("Claude opus is the sole reviewer"): a bare is/are within
	// the first few words is a statement, never a topic
	return /^[a-zA-Z0-9' .\-]{0,40}\b(is|are|was|were)\b/i.test(clause);
}

function isCamel(tok) {
	return /[a-z]/.test(tok) && /[A-Z]/.test(tok.slice(1));
}

function normalizeWord(w, first) {
	const key = w.toLowerCase();
	if (ACRONYMS.has(key)) return ACRONYMS.get(key);
	// a hyphenated token whose suffix segments are single letters is a
	// designation ("Wave-A"): suffixes stay uppercase, the head follows the
	// first/mid rule
	const segs = w.split("-");
	if (segs.length > 1 && segs.slice(1).every((s) => s.length === 1 && /[a-zA-Z]/.test(s))) {
		const head = first
			? segs[0].charAt(0).toUpperCase() + segs[0].slice(1).toLowerCase()
			: segs[0].toLowerCase();
		return [head].concat(segs.slice(1).map((s) => s.toUpperCase()))
			.join("-");
	}
	if (/\d/.test(w) || isCamel(w)) return w;
	if (first) return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
	return w.toLowerCase();
}

// A title never ENDS on a function word, determiner, preposition, auxiliary,
// dangling adverb/adjective or a bare verb — a 32-char cut that lands there
// is a dangling clause. Back off to the last noun-like word.
const TAIL_STOP = new Set(`
to of the a an for with and or only is are be been being was were am in on at
by from into onto over under up down out off about as so that this these
those it its their our your my his her we they he she i you do does did done
has have had will would can could should shall may might must not no nor yes
ok okay run runs ran make makes making build building use uses used using
get gets getting go goes going work works working act acts act open opens
opened opening close start starts started stop stops stopped launch launches
launched add adds adding remove removes removing update updates updating
create creates creating delete write writes writing read reads reading send
send check checking verify verify test tests testing try trying move copy
keep let know think see look feel seem exist exists existed help need needs
want take takes give find tell ask say said come comes came become becomes
became leave leaves put mean means meant stay stays remain remains happen
happens happened begin begins began begin end ends ended continue continues
continued finish finishes finished complete completes completed fail fails
failed latest authoritative current important new own same other more most
very first last next each every all any some then now here there please
exactly verbatim yet still also both per via after before between during
without within across against among around behind below above near since
until toward towards upon while if because than when what which who whom
whose where why how least itself themselves myself yourself
`.split(/\s+/));
const ADJ_TAIL_RE = /(ive|ful|ous|less|able|ible)$|ed$|ing$|ly$/;
const ED_NOUNS = new Set(["changed", "field", "content", "ground", "sound", "thing", "king", "ring", "spring", "warning", "family", "supply", "july", "ally", "rely"]);
// --- round-3 shape rules ---
// Negative instructions state a CONSTRAINT, never a topic ("Do not touch
// remote-agents"): the clause is skipped for the next qualifying one.
const NEGATIVE_CLAUSE_RE = /^(?:do not|don't|dont|never|avoid|stop|no)\b/i;
// a candidate never STARTS on a preposition ("With parallel capability ...")
const LEAD_PREP = new Set(`
with without within under over after before during between among amongst
across against around behind below beneath beside besides beyond despite
except inside outside through throughout toward towards until upon from
into onto off out up down near since via per at by in of on to for about
amid along
`.split(/\s+/));
const LEAD_ARTICLE = new Set(["the", "a", "an"]);
// "... to/into/from the <noun>" location tails are a direction, not a topic
const LOC_PREPS = new Set(["to", "into", "from"]);
const LOC_DET = new Set(["the", "a", "an", "his", "her", "my", "your", "our", "their", "its", "this", "that"]);
// vague modifiers carry no identity; a verb-led candidate whose only object
// is vague-plus-generic-or-product is a weak fragment ("Open the pdf",
// "Locate the real local Tendril"), never a title
const VAGUE_MODS = new Set(
	("real actual local remote current new whole entire full proper simple "
		+ "quick single one two some various own same other").split(" "));
// generic nouns are never the lone object of a verb-led title, and never a
// summary term ("Result sentence", "Seconds agent", "Open the pdf")
const GENERIC_NOUNS = new Set(`
result results sentence sentences word words composition compositions
second seconds agent agents prompt prompts truth truths adapter adapters
file files output outputs error errors pdf pdfs thing things stuff
`.split(/\s+/));
// bare verbs for the round-3 shapes: three in a row ("Locate inspect
// synchronize") and verb-led weak objects
const TITLE_VERBS = new Set(`
open close locate inspect synchronize synchronise rename copy move run fix
update make build use get go work start stop launch add remove create delete
write read send check verify test try finalize finalise prepare merge
complete finish restore audit install integrate migrate deploy configure
initialize initialise implement refactor
`.split(/\s+/));
// internal-CamelCase product names stay salient summary terms; any other
// CamelCase token is a code identifier ("MouseArea", "AgentReach"), never one
const PRODUCT_CAMEL = new Set(["github", "gitlab", "postgresql"]);
// version/number-suffixed tokens ("Prompt-2", "-v1.0", "GLM-5.3") are build
// artefacts, never summary terms
const VERSION_SUFFIX_RE = /[-._]v?\d/;

function verbStemOk(lw) {
	if (TITLE_VERBS.has(lw)) return true;
	for (const suf of ["ies", "es", "s", "ing", "ed"]) {
		if (!lw.endsWith(suf)) continue;
		const stem = lw.slice(0, lw.length - suf.length);
		if (TITLE_VERBS.has(stem)) return true;
		if (suf === "ies" && TITLE_VERBS.has(stem + "y")) return true;
		if ((suf === "es" || suf === "s") && TITLE_VERBS.has(stem + "e")) return true;
		const doubled = stem.length >= 3 && stem[stem.length - 1] === stem[stem.length - 2]
			&& TITLE_VERBS.has(stem.slice(0, -1));
		if ((suf === "ing" || suf === "ed") && (TITLE_VERBS.has(stem + "e") || doubled)) return true;
		return false;
	}
	return false;
}

function isVerb(w) {
	const lw = w.toLowerCase().replace(/^[.,;:!?()[\]{}"']+|[.,;:!?()[\]{}"']+$/g, "");
	return lw.length > 0 && verbStemOk(lw);
}

// A leading gerund of a KNOWN verb narrates an activity ("Running Pi update
// results"); unknown -ing words may be nouns and stay.
function isGerund(w) {
	const lw = w.toLowerCase().replace(/^[.,;:!?()[\]{}"']+|[.,;:!?()[\]{}"']+$/g, "");
	if (!lw.endsWith("ing") || lw.length < 6) return false;
	const stem = lw.slice(0, -3);
	return TITLE_VERBS.has(stem) || TITLE_VERBS.has(stem + "e")
		|| (stem.length >= 3 && stem[stem.length - 1] === stem[stem.length - 2]
			&& TITLE_VERBS.has(stem.slice(0, -1)));
}

// True when any hyphen/plus segment of the token is a generic noun
// ("GPU-adapter" -> adapter).
function genericTerm(tok) {
	return tok.toLowerCase().split(/[-+]/).some((p) => GENERIC_NOUNS.has(p));
}
const DEBRIS_CHARS_RE = /["'{}\[\]<>=|\\`]/;
const FILLERS = [
	"please", "pls", "plz", "hey", "hi", "ok", "okay", "so", "then", "now",
	"also", "and", "but", "can you", "could you", "would you", "will you",
	"i want you to", "i need you to", "i'd like you to", "lets", "let's",
	"let us", "we need to", "we should", "help me", "try to", "kindly",
	"reply with", "respond with", "answer with",
	"exactly", "verbatim", "when ready", "asap",
];
const PREAMBLE_RE = /^(you are|you're|you will act|you must act|act as|as an? [a-z]|your role|i want you to act|take the role|role:)\b/i;
// boilerplate openers (cert prompts, orchestration scaffolding, error
// strings): the line is never a topic — those sessions fall through
const SKIP_LINE_RES = [
	/^this (is|machine|session)\b/i,
	/^we (are|'re|will be)\b/i,
	/^i want this\b/i,
	// meta-statement ("The engine is real") — narration, never a topic
	/^the [a-z' ]{0,40}\b(is|are|was|were)\b/i,
	/^(wait|sleep)\b/i,
	/^repository root\b/i,
	/^this session exists\b/i,
	/^we (are|'re|will be) now\b/i,
	/^we are now\b/i,
	/^important (current|:|update)\b/i,
	/^use (your|the) (shell|subagent) tool\b/i,
	/^current state\b/i,
	/^preflight only\b/i,
	// tool-invocation cert prompts ("Call z_ai_vision …",
	// "Use the zai-mcp-vision tool")
	/^call [a-z][a-z0-9]*_[a-z0-9_]+\b/i,
	/^use (your|the) [a-z0-9.-]+ tool\b/i,
	/^execute this\b/i,
	// first-person status narration ("Am on Omarchy arch linux")
	/^(am|i am|i'm)\b/i,
	/^on (omarchy|arch|linux|mac\b|macos)\b/i,
	/^current (time|git|state|directory|status|problem)\b/i,
	/^(have|had) around\b/i,
	// "I have around one hour …", "I'm on Omarchy …" — status narration
	/^(i|you|we) (have|had) (around|about|only)\b/i,
	/^i[\u2019']?m\b/i,
	/^confirm that this\b/i,
	/^(have|had) around\b/i,
	// "I have around one hour …", "I'm on Omarchy …" — status narration
	/^(i|you|we) (have|had) (around|about|only)\b/i,
	/^i[\u2019']?m\b/i,
	/^call (the )?[a-z0-9._-]+ tool\b/i,
	// bare tool-call cert prompts ("Call summarize …")
	/^call [a-z_]{3,}\b/i,
	// orchestrator-brief section headers picked as "titles"
	/^(the )?(primary goal|outcome|most important (rule|thing)|critical routing rule|system context|project root)\b/i,
	/^reply (exactly|with exactly)\b/i,
	/^mcp error\b/i,
	/^error\s*:/i,
	/^traceback \(most recent call last\)/i,
	/^read\b.{1,64}\bfirst\b/i,
];
// sentinel routing tokens: ALL-CAPS-WITH-DASHES(-OK). LINE-skip needs >= 3
// segments (TENDRIL-D10-LIVE-OK); single-dash heads ("ONE-DAY sprint") are
// emphasis caps, not sentinels.
const SENTINEL_RE = /^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$/;
const SENTINEL_LINE_RE = /^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){2,}$/;
// label prefixes that STRIP to their content ("Task: Herdr recovery fix")
const LABEL_RE = /^(task|context|role|note|important|current state)\s*:\s*/i;
// dates are never salient summary terms: Aug-30, 2026-10-04, 10/04, 2026
const DATE_TOKEN_RE = /^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-._]?[0-9]{1,2}$|^\d{4}-\d{2}-\d{2}$|^\d{1,2}[/.-]\d{1,2}(?:[/.]\d{2,4})?$|^\d{4}$/i;
// verb/protocol words are never summary terms ("<Model> Running",
// "<sentinel> Reply" are banned shapes)
const SUMMARY_STOP = new Set(`
running run runs ran reply replies replied respond responding answer answers
answered output exactly verbatim please working waits waiting wait active
idle done fail failed failing error errors start starts started starting stop
stops stopped summary current state says said using use used try trying
tried let lets new now things stuff works worked keep keeps kept going goes
gone make makes made need needs wanted want thanks good great nice best
better worse bad bash zsh
shell tool tools subagent subagents sleep full true false launch launches
launching one two return returns image images finish finishes finished
taskdone containing including regarding involving project root
request requests background hello already reference command commands
task calls call summarize analysis visual through max word urls url create
created sudo nothing something anything everything
`.split(/\s+/));
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

function shoutyClause(clause) {
	const words = clause.split(TOKEN_SPLIT_RE).filter((w) => w.length > 0);
	return words.length > 0 && words.every(
		(w) => w === w.toUpperCase() && /[A-Za-z]/.test(w));
}

function allProductWords(words) {
	return words.length > 0 && words.every(
		(w) => PRODUCT_WORDS.has(cleanToken(w).toLowerCase()));
}

function tailNounLike(words, allowIngTail = false) {
	if (words.length === 0) return false;
	const lw = words[words.length - 1].toLowerCase()
		.replace(/^[.,;:!?()[\]{}"']+|[.,;:!?()[\]{}"']+$/g, "");
	if (!lw) return false;
	if (TAIL_STOP.has(lw)) return false;
	if (lw.endsWith("-quality")) return false;
	if (ED_NOUNS.has(lw)) return true;
	if (allowIngTail && lw.endsWith("ing")) return true;
	if (ADJ_TAIL_RE.test(lw) && lw.length >= 5) return false;
	return true;
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
	return words.map((w, i) => normalizeWord(w, i === 0));
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
		if (line.startsWith("{") || line.startsWith("[")) {
			continue; // a JSON/protocol line (paths glue on!), not prose
		}
		// a markdown header IS a title-shaped line: use its text
		line = line.replace(HEADER_RE, "").trim();
		if (!line) continue;
		// sentinel routing tokens, cert prompts, scaffolding, errors:
		// never a topic — keep scanning later lines
		if (SENTINEL_LINE_RE.test(cleanToken(line.split(TOKEN_SPLIT_RE, 1)[0]))) {
			continue;
		}
		if (SKIP_LINE_RES.some((rx) => rx.test(line))) continue;
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
		// label prefixes strip to their content ("Task: Herdr recovery fix")
		line = line.replace(LABEL_RE, "").trim();
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
	// the product name alone ("TENDRIL LINK —") and short shouty heads defer
	// to the next, more specific segment of the title line
	const defers = (c) => {
		const words = meaningfulWords(c.split(/\s+/));
		return words.length < 2
			|| (words.length <= 2 && shoutyClause(c))
			|| allProductWords(words);
	};
	// iteratively strip leading politeness/filler phrases
	const prep = (c) => {
		let changed = true;
		while (changed) {
			changed = false;
			const low = c.toLowerCase();
			for (const f of FILLERS) {
				if (low.startsWith(f + " ") || low.startsWith(f + ":") || low === f) {
					c = c.slice(f.length).replace(/^[ ,.:;\-—–]+/, "");
					changed = true;
					break;
				}
			}
		}
		return c.trim() || null;
	};
	const start = clauses.length > 1 && defers(clauses[0]) ? 1 : 0;
	let clause = null;
	for (let c of clauses.slice(start)) {
		// a negative instruction ("Do not touch remote-agents") states a
		// constraint, never a topic — take the next qualifying clause
		if (NEGATIVE_CLAUSE_RE.test(c)) continue;
		c = prep(c);
		if (!c || NEGATIVE_CLAUSE_RE.test(c)) continue;
		// a candidate never STARTS on a preposition ("With parallel ...")
		if (LEAD_PREP.has(cleanToken(c.split(TOKEN_SPLIT_RE, 1)[0]).toLowerCase())) continue;
		clause = c;
		break;
	}
	if (clause === null) return null;
	// scaffolding clauses hide behind fillers ("and execute this as one"):
	// re-check AFTER the strip
	if (/^(execute this|this is|we are|i want this|wait|sleep)\b/i.test(clause)
		|| earlyBeClause(clause)) {
		return null; // scaffolding/narration, never a topic
	}
	return clause || null;
}

function finalize(words, limit, allowIngTail = false) {
	const cleaned = meaningfulWords(
		words.map((w) => cleanToken(w)).filter((w) => w.length > 0));
	if (cleaned.length < 2) return null;
	let low = cleaned.map((w) => w.toLowerCase());
	// three bare verbs in a row is a verb salad, never a topic
	// ("Locate inspect synchronize")
	for (let i = 0; i + 2 < low.length; i += 1) {
		if (isVerb(low[i]) && isVerb(low[i + 1]) && isVerb(low[i + 2])) return null;
	}
	// "... to/into/from the <noun>" location tails are a direction, not a
	// topic ("Copy one file to the windows" -> "Copy one file")
	const stripLocTails = (ws, lo) => {
		for (let n = 0; n < 2; n += 1) {
			if (ws.length >= 4 && LOC_PREPS.has(lo[lo.length - 3])
				&& LOC_DET.has(lo[lo.length - 2])) {
				ws.splice(-3);
				lo.splice(-3);
			} else break;
		}
	};
	stripLocTails(cleaned, low);
	let parts = sentenceCase(cleaned);
	low = parts.map((w) => w.toLowerCase());
	// a leading article is scaffolding, not identity ("The de comitè parity")
	while (parts.length > 2 && LEAD_ARTICLE.has(low[0])) {
		parts.shift();
		low.shift();
	}
	if (parts.length > 0) parts = sentenceCase(parts);
	// clause-boundary back-off: never END on a function word, determiner,
	// preposition, auxiliary, dangling adjective or bare verb
	while (parts.length > 0 && !tailNounLike(parts, allowIngTail)) parts.pop();
	if (parts.length < 2) return null;
	let title = parts.join(" ");
	title = title.replace(/^[\t ,;:—–\-·]+|[\t ,;:—–\-·]+$/g, "");
	if (title.length > limit) {
		let cut = title.slice(0, limit);
		const sp = cut.lastIndexOf(" ");
		if (sp > 0) cut = cut.slice(0, sp);
		title = cut.replace(/^[\t ,;:—–\-·]+|[\t ,;:—–\-·]+$/g, "");
		parts = title.split(" ");
		low = parts.map((w) => w.toLowerCase());
		stripLocTails(parts, low);
		while (parts.length >= 2 && !tailNounLike(parts, allowIngTail)) parts.pop();
		title = parts.join(" ");
	}
	if (parts.length < 2) return null;
	const letters = [...title].filter((c) => /[a-zA-Z\u00c0-\u024f]/.test(c)).length;
	const body = title.replace(/ /g, "").length;
	if (letters < 4 || letters < body / 2) return null;
	if (DEBRIS_CHARS_RE.test(title)) return null; // JSON/protocol debris
	const norm = title.toLowerCase().replace(/[^a-z0-9 ]/g, "").trim();
	if (BANNED.has(norm) || GREETING_RE.test(norm)) return null;
	if (allProductWords(title.split(" "))) return null; // product name alone
	return title.length > 0 ? title : null;
}

// A verb-led candidate whose only remaining object is generic ("Open the
// pdf") or a product name ("Locate the real local Tendril") is a weak
// fragment, never a title: null (fall through).
function weakVerbObject(title) {
	const words = title.split(" ");
	if (words.length === 0 || !isVerb(words[0])) return title;
	const skip = new Set([...STOPWORDS, ...VAGUE_MODS]);
	const rest = meaningfulWords(words.slice(1).filter(
		(w) => !skip.has(cleanToken(w).toLowerCase()) && !isVerb(w)));
	if (rest.length === 0) return null; // bare verb, no topic at all
	if (rest.length === 1) {
		const r = rest[0].toLowerCase();
		if (GENERIC_NOUNS.has(r) || PRODUCT_WORDS.has(r)) return null;
	}
	return title;
}

export function deriveTitle(text, limit = DERIVE_CAP) {
	const clause = normalizeCandidate(text);
	if (clause === null) return null;
	const words = clause.split(TOKEN_SPLIT_RE).filter((w) => w.length > 0);
	const kept = words.filter((w) => !isNoise(cleanToken(w)));
	// judge the SURVIVING prose head: noise tokens (dates, paths, ids) can
	// head the clause and only drop inside finalize
	const heads = meaningfulWords(kept.map((w) => cleanToken(w))
		.filter((w) => w.length > 0));
	if (heads.length > 0) {
		// a leading gerund of a known verb narrates an activity, never a
		// topic ("Running Pi update results")
		if (isGerund(heads[0])) return null;
		// the surviving head never starts on a preposition
		// ("... With parallel capability")
		if (LEAD_PREP.has(heads[0].toLowerCase())) return null;
	}
	const title = finalize(kept, limit);
	return title ? weakVerbObject(title) : null;
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
			if (tok.includes(":") || tok.includes("=")) continue;
			const key = tok.toLowerCase();
			if (STOPWORDS.has(key) || SUMMARY_STOP.has(key)) continue;
			if (SENTINEL_RE.test(tok) || DATE_TOKEN_RE.test(tok)) continue;
			// summary debris (round 3): config/path fragments, version-
			// suffixed tokens ("Prompt-2", "murex-specimen-engine-v1.0"),
			// generic nouns ("Result sentence") and internal-CamelCase code
			// identifiers ("MouseArea", "AgentReach") are never salient
			if (/[._/-]/.test(tok)) continue;
			if (VERSION_SUFFIX_RE.test(tok)) continue;
			if (genericTerm(tok)) continue;
			if (isCamel(tok) && !PRODUCT_CAMEL.has(key)) continue;
			// hash/package artefacts ("CBFD75", "foo2zjs") are never topics
			if (/^[A-Za-z0-9]+$/.test(tok) && /\d/.test(tok) && /[A-Za-z]/.test(tok)
				&& ((tok === tok.toUpperCase() && tok.length >= 4)
					|| (tok === tok.toLowerCase() && tok.length >= 5))) continue;
			if (!scores.has(key)) scores.set(key, { score: 0, first: order++ });
			const entry = scores.get(key);
			if (!seen.has(key)) {
				entry.score += 3; // presence per turn
				seen.add(key);
			}
			if (isCamel(tok)
				|| (tok === tok.toUpperCase() && /[A-Za-z]/.test(tok)
					&& ACRONYMS.has(key))) {
				entry.score += 3; // CamelCase / known acronym
			}
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
	// a summary needs at least one real (non-acronym) word: pairs of bare
	// acronym tokens ("MCP Pi", "UI API", "LIVE QA") are debris, not a topic
	if (top.every((k) => [...cased.get(k)].filter((c) => c >= "a" && c <= "z").length < 3)) return null;
	return finalize(top.map((k) => cased.get(k)), limit, true);
}

export function humanizeRole(name) {
	if (typeof name !== "string" || !name.trim()) return null;
	let n = name.trim()
		.replace(/[#+_\-][0-9a-fA-F]{6,8}$/, "")
		.replace(/#+/g, " ")
		.replace(/[-_\s]+/g, " ")
		.trim()
		.replace(/^[ ._-]+|[ ._-]+$/g, "");
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
