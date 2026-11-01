#!/usr/bin/env python3
"""tendril_titles — the session naming layer (DESIGN-FINAL-POLISH §11).

Titles are OPERATIONAL identity: the primary string every surface shows for
a session. The HOST resolves one title per session per snapshot and ships it
on the wire as additive optional fields `title` (sanitized, <= 48 chars) and
`title_source` — nothing else changes (CONTRACT-0.2.md §1/§8).

No cloud, no LLM, no off-machine transcript text: titles are derived
locally from metadata the harness already records, and they travel only
over the existing private host->phone link. A title is an AUTHORED /
LOCALLY-DERIVED token (<= 48 chars), never terminal content — which is what
keeps it allowed on the wire at all (CONTRACT §0 bans terminal titles and
raw prompt text; `terminal_title` is never read here).

Precedence (first that yields wins; resolve_agent is the one authority):
  1. herdr        the pane's explicit Herdr label (`herdr pane rename`)
  2. manual |     the MOST RECENT explicit human name: a TENDRIL rename
     native       (phone / host CLI) or the harness's own session name (Pi
                  `/name` / `--name`, Claude custom title)
  3. scheduler    title of the task that launched the pane
  4. instruction  deterministic deriver on the first user turn
  5. workspace    "<workspace label> · <Harness>" fallback
  6. omit         the phone falls back to the harness name

Guesses are never names. Pi stores `/name` and an extension's
setSessionName() identically, and TENDRIL's pre-0.2.6 extension wrote its
own derived titles that way — so a heuristic fragment ("Is the absolute
path") reached the phone labelled as the user's own native name, outranked
everything and could never improve. The extension no longer names sessions
itself; it only delivers names the host queued (a phone rename, a scheduled
task title) and records a `tendril-title` provenance entry beside each.
Legacy stored guesses are recognised (looks_auto_named) and re-derived.
The level-5 salient-term summary is retired from session naming: two
co-occurring words are not evidence of what a session is about.

Stability: scheduler and instruction titles persist in
~/.local/state/tendril/session-titles.json (0600, keyed by the harness
session identity — transcript path or Claude UUID) and are not recomputed
while DERIVER_VERSION is unchanged; an improved deriver re-derives each
cached guess once. Pane reuse can never inherit a title: the cache is keyed
by the session identity, and every entry also stores the session's start
time — a mismatched start invalidates the entry.

Claude Code panes report agent_session {kind:"id", value:<uuid>} — no
local path. Their transcript is located under ~/.claude/projects/*/<uuid>.jsonl
(regular, under $HOME, bounded reads, mtime-memoized) and resolved through
the same precedence: native summary / custom title entry, else the first
meaningful user turn (meta, sidechain, command-wrapper and tool_result-only
entries are never instructions), else the salient-term summary — else the
workspace fallback, exactly as for Pi.
"""

import json
import os
import re
import stat
import sys
import tempfile
import time

# --------------------------------------------------------------------------
# paths / storage

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".local", "state", "tendril")
CACHE_FILE = os.path.join(STATE_DIR, "session-titles.json")
PENDING_FILE = os.path.join(STATE_DIR, "pending-titles.json")
PI_SESSIONS_DIR = os.path.join(HOME, ".pi", "agent", "sessions")

CACHE_SCHEMA = 1
PENDING_SCHEMA = 1
PENDING_TTL_S = 600                     # a pending native rename expires
PENDING_CAP = 32

WIRE_TITLE_CAP = 48                     # contract: `title` <= 48 chars
DERIVE_CAP = 32                         # §11 level-4 cap
CHILD_TASK_CAP = 24                     # the short task label after the role

RESOLVE_BUDGET_S = 1.5                  # whole-snapshot title budget

SOURCES = ("manual", "herdr", "native", "scheduler", "instruction",
           "summary", "workspace")
# Sources whose title is a deterministic GUESS (cached per deriver version).
DERIVED_SOURCES = ("instruction", "summary")
# Bump whenever derive_title's output can change for the same input: every
# cached derived title re-derives once under the new rules (v2: questions,
# path-first statements and verb-only commands no longer derive; the
# summary level is retired from session naming).
DERIVER_VERSION = 2
# The Pi custom entry TENDRIL's extension appends beside a name it applied
# (`{"source": "manual"|"scheduler", "name": ...}`) — provenance Pi's own
# session_info entry cannot carry.
TITLE_MARKER = "tendril-title"

# The scheduler names agents `tendril-<id8>` (remote-agents _schedule_launcher);
# such a name is a routing token, NOT a native title — level 3 owns it.
SCHEDULER_NAME_RE = re.compile(r"^tendril-[0-9a-f]{8}$")
HASH_SUFFIX_RE = re.compile(r"-[0-9a-fA-F]{6,32}$")
PANE_RE = re.compile(r"^[A-Za-z0-9]+:[A-Za-z0-9]+$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ISO_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))??"
    r"(?:[Zz]|([+-])(\d{2}):?(\d{2}))?$")

HEADER_BYTES = 4096
SCAN_WINDOW = 256 * 1024                # bounded transcript scan window
SCAN_MAX = 512 * 1024                   # hard per-file read cap
INSTRUCTION_CAP = 8192                  # per user turn fed to the deriver

# in-process mtime memo for bounded transcript reads within one process
_MEMO = {}                              # (path, mtime, kind) -> value
_MEMO_CAP = 512


def _memo_get(key):
    if len(_MEMO) > _MEMO_CAP:
        _MEMO.clear()
    return _MEMO.get(key)


def _memo_put(key, value):
    if len(_MEMO) > _MEMO_CAP:
        _MEMO.clear()
    _MEMO[key] = value
    return value


# --------------------------------------------------------------------------
# wire sanitization


def sanitize_title(v, cap=WIRE_TITLE_CAP):
    """Wire-safe title: control chars stripped, whitespace collapsed,
    trimmed, capped. None for non-strings and empty results."""
    if not isinstance(v, str):
        return None
    out = []
    pending = False
    for ch in v:
        if ch.isspace() or ch == "\u00a0":
            pending = bool(out)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F or 0x80 <= ord(ch) <= 0x9F \
                or 0x202A <= ord(ch) <= 0x202E or 0x2066 <= ord(ch) <= 0x2069:
            continue                        # C0/C1 + bidi controls: stripped
        else:
            if pending and out:
                out.append(" ")
            pending = False
            out.append(ch)
        if len(out) >= cap:
            break
    s = "".join(out)[:cap].strip()
    return s or None


# --------------------------------------------------------------------------
# the deterministic deriver (levels 4-5) — kept in lockstep with
# pi/tendril-session-title.ts; bin/title-vectors.json is the shared contract.

_BANNED = {
    "coding task", "general session", "pi agent", "new session", "help",
    "hi", "hello", "hey", "yo", "session", "task", "coding", "continue",
    "done", "ok", "okay", "yes", "no", "test", "testing", "prompt",
    "assistant", "agent", "the task", "my task", "greetings", "howdy",
}
_GREETING_RE = re.compile(
    r"^(hi+|hello+|hey+|yo+|howdy|greetings|good (morning|afternoon|"
    r"evening|day))\b", re.IGNORECASE)

# ---------------------------------------------------------------- casings
# Emphasis caps in prose (THE, ONLY, FAST, DISK, DEMO…) are NOT identity.
# A token keeps its written form only when it is a KNOWN acronym/product
# token, carries a digit (RC2, GLM-5.3, S21), or is internal-CamelCase
# (Z.AI, GitHub, iOS). Everything else sentence-cases; when unsure, sentence
# case. Map is lowercase key → canonical display.
_ACRONYMS = {
    "live": "LIVE", "rc2": "RC2", "ui": "UI", "ux": "UX", "api": "API",
    "ca": "CA", "cli": "CLI", "mcp": "MCP", "pr": "PR", "qa": "QA",
    "glm": "GLM", "apk": "APK", "s21": "S21", "ntfy": "ntfy",
    "z.ai": "Z.AI", "pi": "Pi", "herdr": "Herdr", "tendril": "Tendril",
    "omarchy": "Omarchy", "android": "Android",
}
# product/surface words alone are never a title ("Tendril LINK", "Pi")
_PRODUCT_WORDS = frozenset(
    "tendril link pi herdr live omarchy android glm z.ai codex claude "
    "arch linux macos windows ui".split())

def _early_be_clause(clause):
    """Narration ("Claude opus is the sole reviewer"): a bare is/are within
    the first few words is a statement, never a topic. Case-insensitive —
    a capitalised head ("Is the absolute path …") is the same narration."""
    return bool(re.match(r"^[a-zA-Z0-9' .\-]{0,40}\b(is|are|was|were)\b",
                         clause, re.IGNORECASE))


# A surviving prose head that is an auxiliary or a wh-word opens a QUESTION
# or a statement ("Is the absolute path …", "Can you check …", "What does
# …"), never a topic. Judged AFTER noise tokens (paths, ids, urls) drop: a
# prompt that begins "/home/x/repo is the absolute path" must not surface
# "Is the absolute path" once the path falls away.
_LEAD_AUX = frozenset("""
is are was were am be been do does did can could should would will shall
may might must has have had what why how where when which who whom whose
isn't aren't wasn't weren't don't doesn't didn't can't couldn't shouldn't
wouldn't won't
""".split())


def _is_camel(tok):
    return any(ch.islower() for ch in tok) \
        and any(ch.isupper() for ch in tok[1:])

def _normalize_word(w, first=False):
    """One word of a finished candidate: allowlist/digit/CamelCase keep
    their written form, everything else sentence-cases. A hyphenated token
    whose suffix segments are single letters is a designation ("Wave-A"):
    the suffixes stay uppercase, the head follows the first/mid rule."""
    key = w.lower()
    if key in _ACRONYMS:
        return _ACRONYMS[key]
    segs = w.split("-")
    if len(segs) > 1 and all(
            len(s) == 1 and s.isalpha() for s in segs[1:]):
        head = segs[0][:1].upper() + segs[0][1:].lower() if first \
            else segs[0].lower()
        return "-".join([head] + [s.upper() for s in segs[1:]])
    if any(ch.isdigit() for ch in w) or _is_camel(w):
        return w
    if first:
        return w[:1].upper() + w[1:].lower()
    return w.lower()

# ------------------------------------------------------------- tail back-off
# A title never ENDS on a function word, determiner, preposition, auxiliary,
# dangling adverb/adjective or a bare verb — a 32-char cut that lands there
# is a dangling clause. Back off to the last noun-like word; fewer than two
# meaningful words afterwards → fall through (None).
_TAIL_STOP = frozenset("""
to of the a an for with and or only is are be been being was were am in on at
by from into onto over under up down out off about as so that this these
those it its their our your my his her we they he she i you do does did done
has have had will would can could should shall may might must not no nor yes
ok okay run runs ran make makes making build building use uses used using
get gets getting go goes going work works working act acts act open opens
opened opening close start starts started stop stops stopped launch launches
launched configure configures configured install installs installed initialize initializes initialized download implement implements implementing integrate integrates integrated migrate migrated deploy deploys deployed add adds adding remove removes removing update updates
updating create creates creating delete write writes writing read reads
reading send send check checking verify verify test tests testing try trying
move copy keep let know think see look feel seem exist exists existed help
need needs want take takes give find tell ask say said come comes came
become becomes became leave leaves put mean means meant stay stays remain
remains happen happens happened begin begins began begin end ends ended
continue continues continued finish finishes finished complete completes
completed fail fails failed latest authoritative current important new own
same other more most very first last next each every all any some then now
here there please exactly verbatim yet still also both per via after before
between during without within across against among around behind below above
near since until toward towards upon while if because than when what which
who whom whose where why how least itself themselves myself yourself
final already
""".split())
_ADJ_TAIL_RE = re.compile(
    r"(ive|ful|ous|less|able|ible)$"        # authoritative, meaningful…
    r"|ed$"                                  # coordinated, integrated…
    r"|ing$"                                 # containing, running…
    r"|ly$")                                 # fully, exactly…
_ED_NOUNS = frozenset({"changed", "field", "content", "ground", "sound",
                       "thing", "king", "ring", "spring", "warning",
                       "family", "supply", "july", "ally", "rely"})

# ------------------------------------------------------- round-3 shape rules
# Negative instructions state a CONSTRAINT, never a topic ("Do not touch
# remote-agents"): the clause is skipped for the next qualifying one.
_NEGATIVE_CLAUSE_RE = re.compile(
    r"^(?:do not|don't|dont|never|avoid|stop|no)\b", re.IGNORECASE)
# a candidate never STARTS on a preposition ("With parallel capability …")
_LEAD_PREP = frozenset("""
with without within under over after before during between among amongst
across against around behind below beneath beside besides beyond despite
except inside outside through throughout toward towards until upon from
into onto off out up down near since via per at by in of on to for about
amid along
""".split())
_LEAD_ARTICLE = frozenset({"the", "a", "an"})
# "… to/into/from the <noun>" location tails are a direction, not a topic
_LOC_PREPS = frozenset({"to", "into", "from"})
_LOC_DET = frozenset({"the", "a", "an", "his", "her", "my", "your", "our",
                      "their", "its", "this", "that"})
# vague modifiers carry no identity; a verb-led candidate whose only object
# is vague-plus-generic-or-product is a weak fragment ("Open the pdf",
# "Locate the real local Tendril"), never a title
_VAGUE_MODS = frozenset(
    "real actual local remote current new whole entire full proper simple "
    "quick single one two some various own same other".split())
# generic nouns are never the lone object of a verb-led title, and never a
# summary term ("Result sentence", "Seconds agent", "Open the pdf")
_GENERIC_NOUNS = frozenset("""
result results sentence sentences word words composition compositions
second seconds agent agents prompt prompts truth truths adapter adapters
file files output outputs error errors pdf pdfs thing things stuff
worktree worktrees repo repos repository repositories directory directories
folder folders
""".split())
# bare verbs for the round-3 shapes: three in a row ("Locate inspect
# synchronize") and verb-led weak objects
_TITLE_VERBS = frozenset("""
open close locate inspect synchronize synchronise rename copy move run fix
update make build use get go work start stop launch add remove create delete
write read send check verify test try finalize finalise prepare merge
complete finish restore audit install integrate migrate deploy configure
initialize initialise implement refactor execute
""".split())
# internal-CamelCase product names stay salient summary terms; any other
# CamelCase token is a code identifier ("MouseArea", "AgentReach"), never one
_PRODUCT_CAMEL = frozenset({"github", "gitlab", "postgresql"})
# version/number-suffixed tokens ("Prompt-2", "-v1.0", "GLM-5.3") are build
# artefacts, never summary terms
_VERSION_SUFFIX_RE = re.compile(r"[-._]v?\d")


def _verb_stem_ok(lw):
    if lw in _TITLE_VERBS:
        return True
    for suf in ("ies", "es", "s", "ing", "ed"):
        if not lw.endswith(suf):
            continue
        stem = lw[:-len(suf)]
        if stem in _TITLE_VERBS:
            return True
        if suf == "ies" and stem + "y" in _TITLE_VERBS:
            return True
        if suf in ("es", "s") and stem + "e" in _TITLE_VERBS:
            return True
        if suf == "ing" and (stem + "e" in _TITLE_VERBS
                             or (len(stem) >= 3 and stem[-1] == stem[-2]
                                 and stem[:-1] in _TITLE_VERBS)):
            return True
        if suf == "ed" and (stem + "e" in _TITLE_VERBS
                            or (len(stem) >= 3 and stem[-1] == stem[-2]
                                and stem[:-1] in _TITLE_VERBS)):
            return True
        return False
    return False


def _is_verb(w):
    lw = w.lower().strip(".,;:!?()[]{}\"'")
    return bool(lw) and _verb_stem_ok(lw)


def _is_gerund(w):
    """A leading gerund narrates an activity ("Running Pi update results");
    only gerunds of KNOWN verbs count — unknown -ing words may be nouns."""
    lw = w.lower().strip(".,;:!?()[]{}\"'")
    if not lw.endswith("ing") or len(lw) < 6:
        return False
    stem = lw[:-3]
    return stem in _TITLE_VERBS or stem + "e" in _TITLE_VERBS \
        or (len(stem) >= 3 and stem[-1] == stem[-2]
            and stem[:-1] in _TITLE_VERBS)


def _generic_term(tok):
    """True when any hyphen/plus segment of the token is a generic noun
    ("GPU-adapter" → adapter)."""
    return any(p in _GENERIC_NOUNS for p in re.split(r"[-+]", tok.lower()))


def _tail_nounlike(words, allow_ing_tail=False):
    """True when the LAST word can end a title (noun-like / product token).
    Summaries allow -ing tails: their gerunds are salient topics
    ("Herdr flapping"), where an instruction cut's would dangle."""
    if not words:
        return False
    w = words[-1]
    lw = w.lower().strip(".,;:!?()[]{}\"'")
    if not lw:
        return False
    if lw in _TAIL_STOP:
        return False
    if lw in _ED_NOUNS:
        return True
    if lw.endswith("-quality"):
        return False
    if allow_ing_tail and lw.endswith("ing"):
        return True
    if _ADJ_TAIL_RE.search(lw) and len(lw) >= 5:
        return False
    return True

# --------------------------------------------------------- boilerplate lines
# Openers whose line is orchestration/cert/protocol scaffolding, never a
# topic: those sessions fall through to summary/workspace.
_SKIP_LINE_RES = (
    re.compile(r"^this (is|machine|session)\b", re.IGNORECASE),
    re.compile(r"^we (are|'re|will be)\b", re.IGNORECASE),
    re.compile(r"^i want this\b", re.IGNORECASE),
    # meta-statement ("The engine is real") — narration, never a topic
    re.compile(r"^the [a-z' ]{0,40}\b(is|are|was|were)\b", re.IGNORECASE),
    re.compile(r"^(wait|sleep)\b", re.IGNORECASE),
    re.compile(r"^repository root\b", re.IGNORECASE),
    re.compile(r"^this session exists\b", re.IGNORECASE),
    re.compile(r"^we (are|'re|will be) now\b", re.IGNORECASE),
    re.compile(r"^we are now\b", re.IGNORECASE),
    re.compile(r"^important (current|:|update)\b", re.IGNORECASE),
    re.compile(r"^use (your|the) (shell|subagent) tool\b", re.IGNORECASE),
    re.compile(r"^current state\b", re.IGNORECASE),
    re.compile(r"^preflight only\b", re.IGNORECASE),
    re.compile(r"^reply (exactly|with exactly)\b", re.IGNORECASE),
    re.compile(r"^mcp error\b", re.IGNORECASE),
    re.compile(r"^error\s*:", re.IGNORECASE),
    re.compile(r"^traceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r"^read\b.{1,64}\bfirst\b", re.IGNORECASE),
    # tool-invocation cert prompts ("Call z_ai_vision …",
    # "Use the zai-mcp-vision tool")
    re.compile(r"^call [a-z][a-z0-9]*_[a-z0-9_]+\b", re.IGNORECASE),
    re.compile(r"^use (your|the) [a-z0-9.-]+ tool\b", re.IGNORECASE),
    re.compile(r"^execute this\b", re.IGNORECASE),
    # first-person status narration ("Am on Omarchy arch linux")
    re.compile(r"^(am|i am|i'm)\b", re.IGNORECASE),
    re.compile(r"^on (omarchy|arch|linux|mac\b|macos)\b", re.IGNORECASE),
    re.compile(r"^(have|had) around\b", re.IGNORECASE),
    # "I have around one hour …", "I'm on Omarchy …" — status narration
    re.compile(r"^(i|you|we) (have|had) (around|about|only)\b", re.IGNORECASE),
    re.compile(r"^i[\u2019']?m\b", re.IGNORECASE),
    re.compile(r"^confirm that this\b", re.IGNORECASE),
    re.compile(r"^call (the )?[a-z0-9._-]+ tool\b", re.IGNORECASE),
    # bare tool-call cert prompts ("Call summarize …")
    re.compile(r"^call [a-z_]{3,}\b", re.IGNORECASE),
    re.compile(r"^current (time|git|state|directory|status|problem)\b",
               re.IGNORECASE),
    # orchestrator-brief section headers picked as "titles"
    re.compile(r"^(the )?(primary goal|outcome|most important (rule|thing)"
               r"|critical routing rule|system context|project root)\b",
               re.IGNORECASE),
)
# sentinel routing tokens: ALL-CAPS-WITH-DASHES(-OK) — a cert/session token,
# never a topic. LINE-skip needs >= 3 segments (TENDRIL-D10-LIVE-OK):
# single-dash heads ("ONE-DAY sprint") are emphasis caps, not sentinels.
_SENTINEL_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
_SENTINEL_LINE_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){2,}$")
# dates are never salient summary terms: Aug-30, 2026-10-04, 10/04, 2026
_DATE_TOKEN_RE = re.compile(
    r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-._]?[0-9]{1,2}$"
    r"|^\d{4}-\d{2}-\d{2}$|^\d{1,2}[/.-]\d{1,2}(?:[/.]\d{2,4})?$|^\d{4}$",
    re.IGNORECASE)
# verb/protocol words are never summary terms ("<Model> Running",
# "<sentinel> Reply" are banned shapes)
# lowercase hyphen/dot compounds with NO digits are role/file labels
# ("general-purpose", "phase-one-complete"), never topics
_LABEL_COMPOUND_RE = re.compile(r"^[a-z]+(?:[-._][a-z]+)+$")
_SUMMARY_STOP = frozenset(
    "running run runs ran reply replies replied respond responding answer "
    "answers answered output exactly verbatim please working waits waiting "
    "wait active idle done fail failed failing error errors start starts "
    "started starting stop stops stopped summary current state says said "
    "using use used try trying tried let lets new now things stuff works "
    "worked keep keeps kept going goes gone make makes made need needs "
    "wanted want thanks good great nice best better worse bad bash zsh shell tool tools subagent subagents sleep full true false launch launches launching one two return returns image images finish finishes finished taskdone containing including regarding involving project root "
       "request requests background hello already reference command commands "
       "task calls call summarize analysis visual through max word urls url "
       "create created sudo nothing something anything everything".split())
# machine role ids a harness assigns sessions: `general-purpose#20894abc`
_ROLE_ID_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9._-]{1,40})#[0-9a-fA-F]{6,8}$")
# bare harness role names a Pi session may be natively labelled with: a ROLE,
# never a title — they get the same role · label treatment as `<role>#<id8>`
_ROLE_ONLY = frozenset({"explore", "plan", "general purpose",
                        "general-purpose"})
# label prefixes that STRIP to their content (label form keeps the topic)
_LABEL_RE = re.compile(
    r"^(task|context|role|note|important|current state)\s*:\s*",
    re.IGNORECASE)
# a line whose FIRST token is one of these reads as a command, not prose
_SHELL_WORDS = frozenset(
    "ps ls cd cat grep rm mv cp echo git npm npx pnpm yarn python python3 "
    "pip curl wget ssh scp make cargo docker kubectl systemctl sudo awk sed "
    "find tail head jq xargs chmod chown sh bash zsh node deno bun vi vim "
    "herdr tendril pi claude codex tmux ln mkdir touch diff patch sort uniq "
    "wc tr cut open xdg-open am journalctl".split())
# the shell-word guard fires only on command-SHAPED lines: metachars or flags
_SHELL_SHAPE_RE = re.compile(r"[|><;]|&&|\s-{1,2}[A-Za-z]|\$\(")
_FILLERS = (
    "please", "pls", "plz", "hey", "hi", "ok", "okay", "so", "then", "now",
    "also", "and", "but", "can you", "could you", "would you", "will you",
    "i want you to", "i need you to", "i'd like you to", "lets", "let's",
    "let us", "we need to", "we now need to", "now we need to", "we should", "help me", "try to", "kindly",
    "reply with", "respond with", "answer with",
    "exactly", "verbatim", "when ready", "asap",
)
# a title is prose: these characters never appear in a kept candidate
_DEBRIS_CHARS_RE = re.compile(r'["\'{}\[\]<>=|\\`]')
_PREAMBLE_RE = re.compile(
    r"^(you are|you're|you will act|you must act|act as|as an? [a-z]|"
    r"your role|i want you to act|take the role|role:)\b", re.IGNORECASE)
_HEADER_RE = re.compile(r"^#{1,6}\s+")
_BANNER_RE = re.compile(r"^([-_=*~·\u2500-\u257f]{5,})$")
_QUOTE_RE = re.compile(r"^>\s?")
_LIST_RE = re.compile(r"^([-*+•]|\d{1,2}[.)])\s+")
_MD_EMPH_RE = re.compile(r"^[*_`#>~\s]+|[*_`#>~\s]+$")
_URL_RE = re.compile(
    r"^(https?://|www\.)\S+$|^\S+/(?:\S+/)*\S+$", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")
_FLAG_RE = re.compile(r"^-{1,2}[A-Za-z][A-Za-z0-9-]*$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{8,}$")
_UUIDISH_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$")
_SECRET_RE = re.compile(
    r"^(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|gho_[A-Za-z0-9]{8,}|"
    r"xox[bp]-[A-Za-z0-9-]{8,}|AKIA[A-Z0-9]{8,}|Bearer\s+\S{8,})$")
# long mixed-class tokens (the shape of credentials), judged per token
_LONG_TOKEN_RE = re.compile(r"^[A-Za-z0-9_+/=-]{20,}$")
_WORD_RE = re.compile(r"[A-Za-z\u00c0-\u024f]{2,}")
_CLAUSE_SPLIT_RE = re.compile(
    r"(?<=[.!?])\s+|\s+[—–|;]\s*|\s*:\s+|,\s+(?=and |then |but )")
_TOKEN_SPLIT_RE = re.compile(r"\s+")

_STOPWORDS = frozenset("""
a an the and or but if then else for of to in on at by with from as is are
was were be been being do does did doing have has had i you he she it we
they me him her us them my your his its our their this that these those
what which who whom when where why how all any both each few more most
other some such no nor not only own same so than too very can will just
should now about into over under again further once here there why's
what's let lets get got make made need wants want like also very ok okay
sure thanks please yeah yup nah maybe stuff things work works keep keeps
keeping going still
""".split())


def _shouty_clause(clause):
    """True when every word in the clause is ALL-CAPS (a shouty banner)."""
    words = [w for w in _TOKEN_SPLIT_RE.split(clause) if w]
    return bool(words) and all(
        w.isupper() and any(ch.isalpha() for ch in w) for w in words)


def _all_product_words(words):
    """True when every word is a product/surface name — the product name
    alone is never a title ("Tendril LINK", "Pi")."""
    return bool(words) and all(
        _clean_token(w).lower() in _PRODUCT_WORDS for w in words)


def _sentence_case(words):
    """Allowlist/digit/CamelCase keep their written form; emphasis caps in
    prose (THE, ONLY, FAST, DISK…) and unsure cases sentence-case down."""
    return [_normalize_word(w, first=(i == 0)) for i, w in enumerate(words)]


def _clean_token(tok):
    tok = _MD_EMPH_RE.sub("", tok).strip(".,;:!?()[]{}\"'")
    return tok


def _is_noise(tok):
    if not tok:
        return True
    if _URL_RE.match(tok) or _EMAIL_RE.match(tok) or _FLAG_RE.match(tok):
        return True
    if _HEX_RE.match(tok) or _UUIDISH_RE.match(tok) or _SECRET_RE.match(tok):
        return True
    if _LONG_TOKEN_RE.match(tok):
        alpha = sum(ch.isalpha() for ch in tok)
        classes = len({any(c.islower() for c in tok),
                       any(c.isupper() for c in tok),
                       any(c.isdigit() for c in tok),
                       any(not c.isalnum() for c in tok)})
        # credential-shaped: long, mixed classes, not sentence vocabulary
        if classes >= 3 and alpha < len(tok) / 2:
            return True
    return False


def _meaningful_words(words):
    return [w for w in words if _WORD_RE.search(w)]


def _normalize_candidate(text):
    """Shared reduction: lines → first usable line → clause → filler-stripped
    token list. None when nothing usable survives."""
    if not isinstance(text, str) or not text.strip():
        return None
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    in_fence = False
    candidate = None
    for raw in lines:
        line = raw.strip()
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        if _BANNER_RE.match(line) or _QUOTE_RE.match(line) \
                or line.startswith("|"):
            continue
        if line[:1] in "{" + "[":
            continue        # a JSON/protocol line (paths glue on!), not prose
        # a markdown header IS a title-shaped line: use its text
        line = _HEADER_RE.sub("", line).strip()
        if not line:
            continue
        # boilerplate openers (cert prompts, orchestration scaffolding,
        # error strings, sentinels): the line is never a topic — keep
        # scanning later lines; a session with none falls through.
        # Bullet markers strip FIRST ("- The engine is real" is narration).
        line = _LIST_RE.sub("", line).strip()
        if not line:
            continue
        if _SENTINEL_LINE_RE.match(_clean_token(
                _TOKEN_SPLIT_RE.split(line, 1)[0])):
            continue
        if any(rx.match(line) for rx in _SKIP_LINE_RES):
            continue
        # role preambles: drop the preamble SENTENCES, keep the rest of the
        # line when it continues past them ("You are X. Optimize Y")
        while line and _PREAMBLE_RE.match(line):
            parts = re.split(r"(?<=[.!?])\s+", line, maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                line = parts[1].strip()
            else:
                line = ""
        if not line:
            continue
        # label prefixes strip to their content ("Task: Herdr recovery fix")
        line = _LABEL_RE.sub("", line).strip()
        if not line:
            continue
        first = _clean_token(_TOKEN_SPLIT_RE.split(line, 1)[0]).lower()
        if first in _SHELL_WORDS and _SHELL_SHAPE_RE.search(line):
            continue                        # a command line, never a title
        stripped = _LIST_RE.sub("", line)
        if not stripped:
            continue
        candidate = stripped
        break
    if not candidate:
        return None
    candidate = _MD_EMPH_RE.sub("", candidate).strip()
    if not candidate:
        return None
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(candidate)
               if c and c.strip()]
    if not clauses:
        return None
    # the product name alone ("TENDRIL LINK —") and short shouty heads defer
    # to the next, more specific segment of the title line
    def _defers(c):
        words = _meaningful_words(c.split())
        return len(words) < 2 or (len(words) <= 2 and _shouty_clause(c)) \
            or _all_product_words(words)

    def _prep(c):
        # iteratively strip leading politeness/filler phrases
        changed = True
        while changed:
            changed = False
            low = c.lower()
            for f in _FILLERS:
                if low.startswith(f + " ") or low.startswith(f + ":") \
                        or low == f:
                    c = c[len(f):].lstrip(" ,.:;-—–")
                    changed = True
                    break
        return c.strip() or None

    start = 1 if (len(clauses) > 1 and _defers(clauses[0])) else 0
    clause = None
    for c in clauses[start:]:
        # a negative instruction ("Do not touch remote-agents") states a
        # constraint, never a topic — take the next qualifying clause
        if _NEGATIVE_CLAUSE_RE.match(c):
            continue
        c = _prep(c)
        if not c or _NEGATIVE_CLAUSE_RE.match(c):
            continue
        # a candidate never STARTS on a preposition ("With parallel …")
        if _clean_token(c.split(" ", 1)[0]).lower() in _LEAD_PREP:
            continue
        clause = c
        break
    if clause is None:
        return None
    # scaffolding clauses hide behind fillers ("and execute this as one"):
    # re-check AFTER the strip
    if re.match(r"^(execute this|this is|we are|i want this|wait|sleep)\b",
                clause, re.IGNORECASE) or _early_be_clause(clause):
        return None                     # scaffolding/narration, never a topic
    return clause or None


def _finalize(words, limit, allow_ing_tail=False):
    words = [w for w in (_clean_token(w) for w in words) if w]
    words = _meaningful_words(words)
    if len(words) < 2:
        return None
    low = [w.lower() for w in words]
    # three bare verbs in a row is a verb salad, never a topic
    # ("Locate inspect synchronize")
    if any(_is_verb(low[i]) and _is_verb(low[i + 1]) and _is_verb(low[i + 2])
           for i in range(len(low) - 2)):
        return None

    def _strip_loc_tails(ws, lo):
        # "… to/into/from the <noun>" location tails are a direction, not a
        # topic ("Copy one file to the windows" → "Copy one file")
        for _ in range(2):
            if len(ws) >= 4 and lo[-3] in _LOC_PREPS and lo[-2] in _LOC_DET:
                del ws[-3:]
                del lo[-3:]
            else:
                break

    _strip_loc_tails(words, low)
    words = _sentence_case(words)
    low = [w.lower() for w in words]
    # a leading article is scaffolding, not identity ("The de comitè parity")
    while len(words) > 2 and low[0] in _LEAD_ARTICLE:
        words.pop(0)
        low.pop(0)
    if words:
        words = _sentence_case(words)
    # clause-boundary back-off: never END on a function word, determiner,
    # preposition, auxiliary, dangling adjective or bare verb — back off to
    # the last noun-like word; too few words afterwards → fall through
    while words and not _tail_nounlike(words, allow_ing_tail):
        words.pop()
    if len(words) < 2:
        return None
    title = " ".join(words)
    # trailing punctuation debris never survives
    title = title.strip(" \t,;:—–-·")
    if len(title) > limit:
        cut = title[:limit]
        if " " in cut:
            cut = cut[:cut.rfind(" ")]
        title = cut.strip(" \t,;:—–-·")
        words = title.split(" ")
        low = [w.lower() for w in words]
        _strip_loc_tails(words, low)
        while len(words) >= 2 and not _tail_nounlike(words, allow_ing_tail):
            words.pop()
        title = " ".join(words)
    if len(words) < 2:
        return None
    # debris guard: letters must dominate, at least 4 letters total
    letters = sum(ch.isalpha() for ch in title)
    if letters < 4 or letters < len(title.replace(" ", "")) / 2:
        return None
    if _DEBRIS_CHARS_RE.search(title):
        return None                     # JSON/protocol debris, never prose
    norm = re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()
    if norm in _BANNED or _GREETING_RE.match(norm):
        return None
    if _all_product_words(title.split(" ")):
        return None                     # the product name alone
    return title or None


def _weak_verb_object(title):
    """A verb-led candidate whose only remaining object is generic
    ("Open the pdf") or a product name ("Locate the real local Tendril") is
    a weak fragment, never a title: None (fall through)."""
    words = title.split(" ")
    if not words or not _is_verb(words[0]):
        return title
    skip = _STOPWORDS | _VAGUE_MODS
    rest = _meaningful_words([
        w for w in words[1:]
        if _clean_token(w).lower() not in skip and not _is_verb(w)])
    if not rest:
        return None                     # bare verb, no topic at all
    if len(rest) == 1:
        r = rest[0].lower()
        if r in _GENERIC_NOUNS or r in _PRODUCT_WORDS:
            return None
    return title


def derive_title(text, limit=DERIVE_CAP):
    """§11 level 4: a stable title from the first user instruction, or None.
    Deterministic, local, no network, no LLM."""
    clause = _normalize_candidate(text)
    if clause is None:
        return None
    words = [w for w in _TOKEN_SPLIT_RE.split(clause) if w]
    kept = [w for w in words if not _is_noise(_clean_token(w))]
    # a secret must NEVER surface: dropped along the way; if nothing
    # meaningful remains the candidate falls through (None).
    # Judge the SURVIVING prose head: noise tokens (dates, paths, ids) can
    # head the clause and only drop inside finalize.
    heads = _meaningful_words([_clean_token(w) for w in kept])
    if heads:
        # a leading gerund of a known verb narrates an activity, never a
        # topic ("Running Pi update results")
        if _is_gerund(heads[0]):
            return None
        # the surviving head never starts on a preposition
        # ("… With parallel capability")
        if heads[0].lower() in _LEAD_PREP:
            return None
        # nor on an auxiliary / wh-word: a question or a statement
        if heads[0].lower() in _LEAD_AUX:
            return None
        # narration re-checked on the surviving prose (the raw clause's
        # head may have been a path the narration guard could not read)
        if _early_be_clause(" ".join(heads)):
            return None
    # a question is never a topic, wherever its verb sits
    if clause.rstrip().endswith("?"):
        return None
    title = _finalize(kept, limit)
    return _weak_verb_object(title) if title else None


def derive_summary(turns, limit=DERIVE_CAP):
    """§11 level 5: deterministic salient-term extraction across the first
    user turns — a NOUN PHRASE, never "<Model> Running", "<sentinel> Reply"
    or a date. Sentinel routing tokens, dates and verb/protocol words are
    never salient terms. None when nothing salient — the caller falls
    through."""
    if not isinstance(turns, list):
        return None
    turns = [t for t in turns[:3] if isinstance(t, str) and t.strip()]
    if not turns:
        return None
    scores = {}                             # lowercase key → [score, first]
    order = []
    for turn in turns:
        seen_in_turn = set()
        for raw in _TOKEN_SPLIT_RE.split(turn.replace("\n", " ")):
            tok = _clean_token(raw)
            if not tok or _is_noise(tok):
                continue
            if ":" in tok or "=" in tok:
                continue                    # config fragments (async:true)
            word = _WORD_RE.match(tok)
            if not word:
                continue
            key = tok.lower()
            if key in _STOPWORDS or key in _SUMMARY_STOP:
                continue
            if _SENTINEL_RE.match(tok) or _DATE_TOKEN_RE.match(tok):
                continue                    # routing tokens and dates
            if _LABEL_COMPOUND_RE.match(tok):
                continue                    # general-purpose, phase-one…
            # summary debris (round 3): config/path fragments, version-
            # suffixed tokens ("Prompt-2", "murex-specimen-engine-v1.0"),
            # generic nouns ("Result sentence") and internal-CamelCase code
            # identifiers ("MouseArea", "AgentReach") are never salient
            if any(ch in tok for ch in "._/-"):
                continue
            if _VERSION_SUFFIX_RE.search(tok):
                continue
            if _generic_term(tok):
                continue
            if _is_camel(tok) and tok.lower() not in _PRODUCT_CAMEL:
                continue
            # hash/package artefacts ("CBFD75", "foo2zjs") are never topics
            if tok.isalnum() and any(ch.isdigit() for ch in tok) \
                    and any(ch.isalpha() for ch in tok) \
                    and ((tok.isupper() and len(tok) >= 4)
                         or (tok.islower() and len(tok) >= 5)):
                continue
            if key not in scores:
                scores[key] = [0, len(order)]
                order.append(key)
            entry = scores[key]
            if key not in seen_in_turn:
                entry[0] += 3               # presence per turn
                seen_in_turn.add(key)
            if _is_camel(tok) or (tok == tok.upper()
                                  and any(ch.isalpha() for ch in tok)
                                  and key in _ACRONYMS):
                entry[0] += 3               # CamelCase / known acronym
            if any(ch.isdigit() for ch in tok):
                entry[0] += 2
            if any(ch in "._+/-" for ch in tok):
                entry[0] += 1
    ranked = sorted(order, key=lambda k: (-scores[k][0], scores[k][1]))
    top = [k for k in ranked[:2] if scores[k][0] >= 3]
    if len(top) < 2:
        return None
    # rebuild with the first-seen casing of each term
    cased = {}
    for turn in turns:
        for raw in _TOKEN_SPLIT_RE.split(turn.replace("\n", " ")):
            tok = _clean_token(raw)
            if tok and tok.lower() in scores and tok.lower() not in cased:
                cased[tok.lower()] = tok
    # a summary needs at least one real (non-acronym) word: pairs of bare
    # acronym tokens ("MCP Pi", "UI API", "LIVE QA") are debris, not a topic
    if all(sum(ch.islower() for ch in cased[k]) < 3 for k in top):
        return None
    words = [cased[k] for k in top]
    return _finalize(words, limit, allow_ing_tail=True)


def humanize_role(name):
    """`researcher_a1b2c3d4` → `Researcher`; `backend-scout#2cbfa86a` →
    `Backend scout`; `Explore` → `Explore`. The id tail (`_id8`, `#id8`)
    is a machine identifier, never prose."""
    if not isinstance(name, str) or not name.strip():
        return None
    n = name.strip()
    n = re.sub(r"[#+_-][0-9a-fA-F]{6,8}$", "", n)   # _<id8> / #<id8> tails
    n = re.sub(r"[#]+", " ", n)
    n = re.sub(r"[-_\s]+", " ", n).strip(" ._")
    if not n:
        return None
    if n[:1].islower():
        n = n[:1].upper() + n[1:]
    return n


# --------------------------------------------------------------------------
# bounded transcript readers (read-only; metadata only)


def _safe_mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _is_regular(path):
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def _under_home(rp, home=None):
    # Both sides resolved: a home reached through a symlink (macOS /var ->
    # /private/var, /home -> /var/home) must not make every file an "escape".
    home = os.path.realpath(home or HOME)
    return rp.startswith(home.rstrip(os.sep) + os.sep)


def _iter_jsonl(path, tail=False, window=SCAN_WINDOW):
    """Yield parsed dicts from the FIRST (or, with tail, the FIRST and LAST
    windows of a) JSONL file. Bounded by `window` per read."""
    try:
        size = os.stat(path).st_size
    except OSError:
        return
    if size <= 0:
        return
    ranges = []
    if tail and size > window:
        ranges.append((0, window))
        ranges.append((max(0, size - window), size))
    else:
        ranges.append((0, min(size, window)))
    seen = set()
    with open(path, "rb") as f:
        for off, end in ranges:
            f.seek(off)
            blob = f.read(end - off)
            if off:
                # drop the partial first line of a mid-file window
                nl = blob.find(b"\n")
                if nl >= 0:
                    blob = blob[nl + 1:]
            for line in blob.split(b"\n"):
                line = line.strip()
                if not line or len(line) > 262144:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if isinstance(d, dict):
                    # identity dedupe ONLY for entries that carry one: Claude
                    # custom-title rows (ai-title) have no id/timestamp, and
                    # collapsing them would freeze the FIRST title forever.
                    key = d.get("id"), d.get("type"), d.get("timestamp")
                    if key[0] or key[2]:
                        if key in seen:
                            continue
                        seen.add(key)
                    yield d


def _iso_to_epoch(v):
    if not isinstance(v, str):
        return None
    try:
        import calendar
        m = ISO_TS_RE.match(v.strip())
        if not m:
            return None
        y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
        frac = m.group(7)
        return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
    except Exception:
        return None


def session_header(path):
    """Whitelist projection of the transcript header line:
    {id, ts, parent} — parent = parentSession (children only)."""
    key = (path, _safe_mtime(path), "header")
    hit = _memo_get(key)
    if hit is not None:
        return hit
    out = {}
    try:
        with open(path, "rb") as f:
            head = f.read(HEADER_BYTES)
    except OSError:
        return out
    line = head.split(b"\n", 1)[0][:HEADER_BYTES]
    try:
        d = json.loads(line)
    except Exception:
        d = None
    if isinstance(d, dict) and d.get("type") == "session":
        if isinstance(d.get("id"), str) and d["id"]:
            out["id"] = d["id"]
        ts = _iso_to_epoch(d.get("timestamp"))
        if ts:
            out["ts"] = ts
        ps = d.get("parentSession")
        if isinstance(ps, str) and ps:
            out["parent"] = ps
    return _memo_put(key, out)


def pi_native_info(path):
    """The Pi transcript's session NAME with its provenance evidence:
    {name, ts, only, marker} — `name` the latest session_info name (None
    when unnamed), `ts` its epoch, `only` True when that entry is also the
    FIRST session_info in the file (the name was set once and never
    changed), `marker` the source a TENDRIL provenance entry recorded for
    that exact name ("manual" / "scheduler") or None. Head + tail windows,
    cached per mtime.

    Pi stores `/name`, `--name` and an extension's setSessionName() as the
    SAME session_info entry — nothing in it says who wrote it. The marker
    is how TENDRIL's own extension records the names it applied."""
    key = (path, _safe_mtime(path), "nameinfo")
    hit = _memo_get(key)
    if hit is not None:
        return hit
    first_id = None
    best = None                             # (ts, name, id)
    markers = {}                            # name -> source
    for d in _iter_jsonl(path, tail=True):
        t = d.get("type")
        if t == "custom" and d.get("customType") == TITLE_MARKER:
            data = d.get("data")
            if isinstance(data, dict) and isinstance(data.get("name"), str) \
                    and data.get("source") in ("manual", "scheduler"):
                markers[sanitize_title(data["name"])] = data["source"]
            continue
        if t != "session_info":
            continue
        name = d.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if first_id is None:
            first_id = d.get("id") or id(d)
        ts = _iso_to_epoch(d.get("timestamp")) or 0
        if best is None or ts >= best[0]:
            best = (ts, name.strip(), d.get("id") or id(d))
    out = {"name": None, "ts": None, "only": False, "marker": None}
    if best is not None:
        name = sanitize_title(best[1], WIRE_TITLE_CAP)
        out = {"name": name, "ts": best[0] or None,
               "only": best[2] == first_id, "marker": markers.get(name)}
    return _memo_put(key, out)


def pi_native_name(path):
    """The latest session_info name in a Pi transcript, or None."""
    return pi_native_info(path).get("name")


def _phrase_tokens(text):
    """Lowercased prose tokens of a text, noise (paths, ids, secrets)
    removed — the token stream a derived title's words were cut from."""
    out = []
    for raw in _TOKEN_SPLIT_RE.split(text or ""):
        tok = _clean_token(raw)
        # the deriver keeps only words with 2+ letters (_meaningful_words):
        # "a", "&", "+" and bare numbers never reach a derived title
        if tok and not _is_noise(tok) and _WORD_RE.search(tok):
            out.append(tok.lower())
    return out


def looks_auto_named(name, first_instruction):
    """True when a Pi session name has the exact shape of a title TENDRIL's
    pre-0.2.6 extension derived from the first instruction and wrote with
    setSessionName() — a heuristic guess that was stored like a user's
    `/name`. The shape: <= DERIVE_CAP chars, 2-8 words, already in the
    deriver's own sentence-case normal form, and its words a contiguous run
    of the first instruction's prose tokens (a dropped leading article
    allowed). Human names (`w02-p8app`, `ABYSSAL ULTIMATE`, `Omarchy
    Ricing`, "Fix login" typed for a prompt about something else) fail it.
    Callers also require the name to be the only one ever set."""
    if not isinstance(name, str) or not isinstance(first_instruction, str):
        return False
    if len(name) > DERIVE_CAP:
        return False
    words = name.split(" ")
    if not 2 <= len(words) <= 8:
        return False
    if " ".join(_sentence_case(words)) != name:
        return False
    want = [w.lower() for w in words]
    toks = _phrase_tokens(first_instruction[:INSTRUCTION_CAP])
    n = len(want)
    for i in range(len(toks) - n + 1):
        if toks[i:i + n] == want:
            return True
        if toks[i] in _LEAD_ARTICLE and toks[i + 1:i + 1 + n] == want:
            return True
    return False


def _message_text(d):
    """The text of one transcript message entry, or None. Structure only —
    the text NEVER leaves this module except inside the deriver."""
    m = d.get("message")
    if not isinstance(m, dict):
        return None
    if m.get("role") != "user":
        return None
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for item in c:
            if isinstance(item, dict) and item.get("type") == "text" \
                    and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts) if parts else None
    return None


# ------------------------------------------------------------------------
# Claude Code transcripts (~/.claude/projects/<project>/<uuid>.jsonl)
# — read-only, bounded, same guards as the Pi path.

# a slash-command expansion / injected context is orchestration, never an
# instruction (string or text-block content alike)
_CLAUDE_CMD_WRAP_RE = re.compile(
    r"<command-|<local-command-stdout>|<system-reminder>")


def claude_transcript_path(uuid, home=None):
    """Locate one Claude Code transcript: ~/.claude/projects/<project>/
    <uuid>.jsonl. Guards mirror the Pi path: the file must exist, be a
    REGULAR file and resolve under $HOME; the directory walk is bounded and
    memoized per process (positive hits only — a missing transcript is
    re-probed, it may appear later). None when no transcript exists."""
    home = home or HOME
    if not isinstance(uuid, str) or not UUID_RE.match(uuid):
        return None
    mkey = ("cllocate", home, uuid)
    hit = _memo_get(mkey)
    if hit is not None:
        return hit.get("path")
    root = os.path.join(home, ".claude", "projects")
    path = None
    try:
        entries = os.listdir(root)[:512]
    except OSError:
        entries = []
    target = uuid + ".jsonl"
    for entry in entries:
        d = os.path.join(root, entry)
        if not os.path.isdir(d):
            continue
        p = os.path.join(d, target)
        if not _is_regular(p):
            continue
        rp = os.path.realpath(p)
        if not _under_home(rp, home):
            continue
        path = p
        break
    return _memo_put(mkey, {"path": path}).get("path")


def _claude_session_start(path):
    """The transcript's own start epoch: the first line's timestamp, else
    the file mtime — the pane-reuse pin for UUID-keyed sessions (stable as
    the transcript grows). Cached per mtime."""
    key = (path, _safe_mtime(path), "clstart")
    hit = _memo_get(key)
    if hit is not None and "start" in hit:
        return hit["start"]
    start = None
    try:
        with open(path, "rb") as f:
            head = f.read(HEADER_BYTES)
    except OSError:
        head = b""
    line = head.split(b"\n", 1)[0][:HEADER_BYTES]
    try:
        d = json.loads(line)
    except Exception:
        d = None
    if isinstance(d, dict):
        start = _iso_to_epoch(d.get("timestamp"))
    if not start:
        start = _safe_mtime(path)
    return _memo_put(key, {"start": start}).get("start")


def claude_native_title(path):
    """The newest native title in a Claude transcript — a `summary` entry
    (compaction) or the custom-title entry (ai-title) — sanitized, or None.
    JSONL is append-ordered, so the LAST non-empty candidate wins. Cached
    per mtime."""
    key = (path, _safe_mtime(path), "clnat")
    hit = _memo_get(key)
    if hit is not None and "name" in hit:
        return hit["name"]
    best = None
    for d in _iter_jsonl(path, tail=True):
        t = d.get("type")
        if t == "summary":
            v = d.get("summary")
        elif t == "ai-title":
            v = d.get("aiTitle")
        else:
            continue
        if isinstance(v, str) and v.strip():
            best = v.strip()
    name = sanitize_title(best, WIRE_TITLE_CAP)
    return _memo_put(key, {"name": name}).get("name")


def _claude_user_text(d):
    """One Claude entry's user-instruction text, or None. Skips non-user
    rows, isMeta, isSidechain, tool_result-only bodies and command
    wrappers; text blocks are joined when present."""
    if d.get("type") != "user":
        return None
    if d.get("isMeta") or d.get("isSidechain"):
        return None
    m = d.get("message")
    if not isinstance(m, dict) or m.get("role") != "user":
        return None
    c = m.get("content")
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        parts = []
        for item in c:
            if isinstance(item, dict) and item.get("type") == "text" \
                    and isinstance(item.get("text"), str):
                parts.append(item["text"])
        text = "\n".join(parts) if parts else None
    else:
        return None
    if not text or not text.strip():
        return None
    if _CLAUDE_CMD_WRAP_RE.search(text):
        return None                     # a command wrapper, never a topic
    return text.strip()[:INSTRUCTION_CAP]


def claude_first_instructions(path, max_turns=3, window=SCAN_MAX):
    """The first <= max_turns meaningful Claude user instructions, oldest
    first, each capped. Cached per mtime."""
    key = (path, _safe_mtime(path), "clinstr", window)
    hit = _memo_get(key)
    if hit is not None and "turns" in hit:
        return hit["turns"]
    turns = []
    for d in _iter_jsonl(path, tail=False, window=window):
        if len(turns) >= max_turns:
            break
        text = _claude_user_text(d)
        if not text:
            continue
        turns.append(text)
    return _memo_put(key, {"turns": turns}).get("turns") or []


def first_instructions(path, max_turns=3, window=SCAN_MAX):
    """The first <= max_turns user instructions, each capped, oldest first.
    Cached per mtime; the raw text never leaves this module's callers that
    own the deriver."""
    key = (path, _safe_mtime(path), "instr", window)
    hit = _memo_get(key)
    if hit is not None and "turns" in hit:
        return hit["turns"]
    turns = []
    for d in _iter_jsonl(path, tail=False, window=window):
        if len(turns) >= max_turns:
            break
        if d.get("type") not in ("message", "user"):
            continue
        text = _message_text(d)
        if not text or not text.strip():
            continue
        turns.append(text.strip()[:INSTRUCTION_CAP])
    return _memo_put(key, {"turns": turns}).get("turns") or []


def session_key(agent, home=None):
    """The cache key for one herdr agent row: the agent_session value
    (transcript path / Claude UUID) — NOT the pane id, so a reused pane
    can never inherit another session's title. None when absent."""
    home = home or HOME
    sess = agent.get("agent_session") if isinstance(agent, dict) else None
    if not isinstance(sess, dict):
        return None
    value = sess.get("value")
    if not isinstance(value, str) or not value:
        return None
    kind = sess.get("kind")
    if kind == "path":
        rp = os.path.realpath(value)
        return rp if _under_home(rp, home) else None
    if kind == "id":
        return value if UUID_RE.match(value) else None
    return None


# --------------------------------------------------------------------------
# cache (the override + levels 3-5 persistence)


def _load_json(path):
    try:
        with open(path, "rb") as f:
            d = json.loads(f.read(4 * 1024 * 1024))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_json(path, doc, mode=0o600):
    try:
        d = os.path.dirname(path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        body = json.dumps(doc, separators=(",", ":")).encode("utf-8")
        fd, tmp = tempfile.mkstemp(prefix=".titles-", dir=d)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass


def load_cache(cache_path=None):
    cache_path = cache_path or CACHE_FILE
    d = _load_json(cache_path)
    titles = d.get("titles")
    return titles if isinstance(titles, dict) else {}


def _save_cache(titles, cache_path=None):
    _save_json(cache_path or CACHE_FILE, {"schema_version": CACHE_SCHEMA, "titles": titles})


def _cache_valid(entry, start):
    """Pane-reuse guard: the entry pins the session's own start; a session
    whose start differs (a reused pane, a new transcript) never inherits.
    A DERIVED entry is valid only for the deriver version that wrote it:
    an improved deriver re-derives each cached guess exactly once."""
    if not isinstance(entry, dict):
        return False
    if not isinstance(entry.get("title"), str) or not entry["title"]:
        return False
    if entry.get("source") not in SOURCES:
        return False
    if entry.get("source") in DERIVED_SOURCES \
            and entry.get("dv") != DERIVER_VERSION:
        return False
    cs, s = entry.get("session_start"), start
    if isinstance(cs, (int, float)) and isinstance(s, (int, float)):
        if abs(cs - s) > 120:
            return False
    return True


def _start_of(agent, key):
    """The session's start epoch when the transcript is local: the header
    ts, else the file mtime. None for UUID-keyed (Claude) sessions."""
    if key and os.path.isabs(key) and key.endswith(".jsonl"):
        h = session_header(key)
        if h.get("ts"):
            return h["ts"]
        return _safe_mtime(key)
    return None


def _store(titles, key, title, source, start, pane=None):
    entry = {"title": title, "source": source, "at": int(time.time())}
    if source in DERIVED_SOURCES:
        entry["dv"] = DERIVER_VERSION
    prev = titles.get(key)
    if source != "manual" and isinstance(prev, dict) \
            and isinstance(prev.get("cleared"), str):
        entry["cleared"] = prev["cleared"]  # a reset outlives re-derivation
    if isinstance(start, (int, float)):
        entry["session_start"] = int(start)
    if pane:
        entry["pane"] = pane
    titles[key] = entry
    return entry


# --------------------------------------------------------------------------
# resolution


def _herdr_title(agent):
    """Level 1: the name a human gave this pane IN HERDR — `herdr pane
    rename` (PaneInfo.label, joined onto the agent row as `pane_label` by
    collect()), or `name` when a Herdr build reports one on the agent row.
    Herdr is the session manager: its explicit label is the authoritative
    human name. A scheduler routing token (`tendril-<id8>`) never counts;
    a token-shaped agent name (`focus-runner-9a1b2c`) is humanized."""
    label = agent.get("pane_label")
    if isinstance(label, str) and label.strip() \
            and not SCHEDULER_NAME_RE.match(label.strip()):
        t = sanitize_title(label)
        if t:
            return t
    name = agent.get("name")
    if isinstance(name, str) and name.strip() \
            and not SCHEDULER_NAME_RE.match(name.strip()):
        stripped = HASH_SUFFIX_RE.sub("", name.strip()).strip(" -_.")
        if len(stripped) >= 3:
            # a Herdr agent name is a TOKEN ("focus-runner"), not prose:
            # separators become spaces and the head capitalizes. A name
            # the human wrote with spaces stays as written.
            if " " not in stripped and ("-" in stripped or "_" in stripped):
                stripped = stripped.replace("_", " ").replace("-", " ")
                stripped = stripped[:1].upper() + stripped[1:]
            return sanitize_title(stripped)
    return None


def _native_title(agent, key, titles, now, home=None):
    """The harness's own explicit session name: (title, source, ts) | None.
    Pi: the session_info name — EXCEPT a name TENDRIL's pre-0.2.6
    extension derived and stored itself (looks_auto_named, set once, no
    provenance marker): that is a guess, never the user's `/name`, and it
    is re-derived like any other guess. A name the extension applied from
    the host's queue carries a marker naming its real source. Claude: the
    transcript's native summary / custom title. A machine-assigned role id
    (`general-purpose#20894abc`) is NEVER shown raw: it is the child's ROLE
    — humanized, plus the short task label from the session's own first
    instruction when one derives."""
    home = home or HOME
    if key and os.path.isabs(key) and key.endswith(".jsonl") \
            and _is_regular(key):
        info = pi_native_info(key)
        name = info.get("name")
        if not name:
            return None
        cleared = (titles or {}).get(key) or {}
        if isinstance(cleared, dict) and cleared.get("cleared") == name:
            return None                 # reset from the phone: not a name
        role = _ROLE_ID_RE.match(name)
        if role:
            r = _role_native_title(key, role.group(1), home)
            return (r[0], r[1], info.get("ts")) if r else None
        # bare role names ("Explore", "Plan", "General purpose") are the
        # harness's agent role, not a title: label them like children do
        if name.strip().lower().replace("_", " ") in _ROLE_ONLY:
            r = _role_native_title(key, name, home, total_cap=32)
            return (r[0], r[1], info.get("ts")) if r else None
        if info.get("marker"):
            return name, info["marker"], info.get("ts")
        if info.get("only"):
            turns = first_instructions(key, max_turns=1)
            if turns and looks_auto_named(name, turns[0]):
                return None             # a stored guess, not a native name
        return name, "native", info.get("ts")
    if key and UUID_RE.match(key):
        cl = claude_transcript_path(key, home)
        if cl:
            name = claude_native_title(cl)
            if name:
                return name, "native", None
    return None


def _role_native_title(path, role, home, total_cap=None):
    """Role natives: `Backend scout · <short task label>` — the humanized
    role name plus the first instruction's label; `None` for the label keeps
    the bare humanized role (never the raw hash). `total_cap` (role-only
    natives) bounds the WHOLE title, label included, at 32 chars."""
    role_name = humanize_role(role)
    if not role_name:
        return None
    label_cap = CHILD_TASK_CAP
    if total_cap:
        label_cap = min(label_cap, total_cap - len(role_name) - 3)
    label = None
    if label_cap >= 6:
        try:
            turns = first_instructions(path, max_turns=1, window=65536) \
                if path else []
            t = derive_title(turns[0]) if turns else None
            label = sanitize_title(t, label_cap) if t else None
        except Exception:
            label = None
    if label:
        return "%s · %s" % (role_name, label), "instruction"
    return role_name, "native"


def _target_kind(task):
    """"new" | "existing" — the snapshot section carries the kind word, the
    store the target dict; a task from before §7.3 targets always created
    its own session."""
    t = task.get("target")
    if isinstance(t, dict):
        t = t.get("kind")
    return t if t in ("new", "existing") else "new"


def _scheduler_title(pane, schedule_tasks):
    """Level 3: the title of the task that LAUNCHED this pane (the store
    keeps pane_id once a task starts; terminal tasks keep it too). A task
    merely SENT to an existing session never names it: that session's
    identity predates the task."""
    if not pane or not isinstance(schedule_tasks, list):
        return None
    best = None
    for t in schedule_tasks:
        if not isinstance(t, dict) or t.get("pane_id") != pane:
            continue
        if _target_kind(t) == "existing":
            continue
        title = t.get("title")
        if isinstance(title, str) and title.strip():
            started = t.get("started_at")
            best = (title.strip(),
                    started if isinstance(started, (int, float)) else 0)
            if t.get("state") == "RUNNING":
                break
    if not best:
        return None
    return sanitize_title(best[0], WIRE_TITLE_CAP)


def _sent_to_existing(pane, schedule_tasks, title):
    """True when the live schedule shows `title` belongs to a task that was
    sent to this pane's EXISTING session (a cached scheduler title from
    before 0.2.6 that must not stay the session's identity)."""
    for t in schedule_tasks or []:
        if isinstance(t, dict) and t.get("pane_id") == pane \
                and _target_kind(t) == "existing" \
                and sanitize_title(t.get("title"), WIRE_TITLE_CAP) == title:
            return True
    return False


def resolve_agent(agent, workspace_label, schedule_tasks,
                  titles=None, home=None, now=None):
    """(title|None, source|None) for one agent row. The precedence (each
    level only when every level above it yields nothing):

      1. herdr       the pane's explicit Herdr label (`herdr pane rename`)
      2. manual /    the most recent EXPLICIT human name: a TENDRIL rename
         native      (phone / host CLI) or the harness's own session name
                     (Pi `/name` / `--name`, Claude custom title) — a Pi
                     rename made after a TENDRIL rename wins, and the reverse
      3. scheduler   the title of the task that launched the pane
      4. instruction the deterministic deriver on the first user turn
                     (cached per deriver version, never recomputed within it)
      5. workspace   "<workspace label> · <Harness>"
      6. omit        the phone falls back to the harness name

    A heuristic is never allowed to outrank or overwrite a human name, and
    a guess is never presented as a human name. `titles` = the loaded cache
    (mutated in memory; the caller persists)."""
    home = home or HOME
    now = now if now is not None else time.time()
    titles = titles if titles is not None else load_cache()
    harness = str(agent.get("agent") or "").strip().lower() or "agent"
    pane = agent.get("pane_id")
    key = session_key(agent, home)
    claude_tx = claude_transcript_path(key, home) \
        if key and UUID_RE.match(key) else None
    start = _start_of(agent, key)
    if start is None and claude_tx:
        start = _claude_session_start(claude_tx)

    # 1 herdr
    h = _herdr_title(agent)
    if h:
        return h, "herdr"

    cached = titles.get(key) if key else None
    valid = _cache_valid(cached, start)
    manual = cached if valid and cached.get("source") == "manual" else None

    # 2 manual / native — the most recent explicit human act wins
    nat = _native_title(agent, key, titles, now, home=home)
    if manual and nat:
        n_title, n_source, n_ts = nat
        m_at = manual.get("at")
        if n_title == manual["title"] \
                or not isinstance(n_ts, (int, float)) \
                or not isinstance(m_at, (int, float)) or n_ts <= m_at:
            return manual["title"], "manual"
        return n_title, n_source       # renamed in the harness afterwards
    if manual:
        return manual["title"], "manual"
    sched = _scheduler_title(pane, schedule_tasks)
    if nat:
        n_title, n_source, _ts = nat
        # a scheduler-applied native name IS the scheduler's title
        if n_source == "native" and sched and n_title == sched:
            n_source = "scheduler"
        return n_title, n_source

    # 3 scheduler
    if sched:
        if key:
            _store(titles, key, sched, "scheduler", start, pane)
        return sched, "scheduler"
    if valid and cached.get("source") == "scheduler" \
            and not _sent_to_existing(pane, schedule_tasks, cached["title"]):
        return cached["title"], "scheduler"

    # 4 instruction — cached guesses hold within one deriver version
    if valid and cached.get("source") in DERIVED_SOURCES:
        return cached["title"], cached["source"]
    tx_turns = []
    if key and os.path.isabs(key) and key.endswith(".jsonl") \
            and _is_regular(key) and _under_home(os.path.realpath(key), home):
        tx_turns = first_instructions(key, max_turns=1)
    elif claude_tx:
        tx_turns = claude_first_instructions(claude_tx, max_turns=1)
    if tx_turns:
        t4 = derive_title(tx_turns[0])
        t4 = sanitize_title(t4, WIRE_TITLE_CAP) if t4 else None
        if t4:
            _store(titles, key, t4, "instruction", start, pane)
            return t4, "instruction"

    # 5 workspace fallback (deterministic; not persisted)
    wl = sanitize_title(workspace_label, 24) if workspace_label else None
    if wl:
        harness_disp = harness[:1].upper() + harness[1:] \
            if harness[:1].islower() else harness[:24]
        return "%s · %s" % (wl, harness_disp), "workspace"
    # 6 omit — the phone falls back to the harness
    return None, None


def annotate_document(doc, rows, home=None, cache_path=None, now=None):
    """Attach `title` + `title_source` to every agent in a snapshot doc and
    `title` to every child. `rows` are collect()'s selector rows in doc
    order. Best-effort by contract: any failure just leaves fields off."""
    home = home or HOME
    now = now if now is not None else time.time()
    cache_path = cache_path or CACHE_FILE
    deadline = now + RESOLVE_BUDGET_S
    titles = load_cache(cache_path)
    touched = False
    tasks = (doc.get("schedule") or {}).get("tasks") \
        if isinstance(doc.get("schedule"), dict) else None
    ws_docs = doc.get("workspaces") or []
    for wi, ws in enumerate(ws_docs):
        if wi >= len(rows):
            break
        row = rows[wi]
        raw = {a.get("pane_id"): a for a in row.get("agents", [])
               if isinstance(a, dict)}
        for out in ws.get("agents", []):
            a = raw.get(out.get("pane_id"))
            if a is None:
                continue
            if time.monotonic() > deadline and "title" not in out:
                continue            # over budget: leave fields off this round
            try:
                title, source = resolve_agent(
                    a, row.get("label"), tasks, titles=titles, home=home,
                    now=now)
            except Exception:
                continue
            if title and source:
                out["title"] = title
                out["title_source"] = source
                touched = True
    if touched:
        _save_cache(titles, cache_path)


# --------------------------------------------------------------------------
# children


_CHILD_MEMO_TTL = 900.0
_CHILD_READ_BUDGET = 24               # max child instruction reads per process
_child_reads = [0]


def child_title(path, name, home=None, now=None):
    """A child's display title: the humanized role name, plus the short
    task label derived from the child's own first instruction when one
    yields — `Researcher · Fix parser races`. Never just `subagent`;
    None when even the role is unknown (the phone keeps its fallbacks).
    Returns (title|None, source|None)."""
    home = home or HOME
    now = now if now is not None else time.time()
    role = humanize_role(name)
    label = None
    if path and os.path.isabs(path) and path.endswith(".jsonl") \
            and _is_regular(path) and _under_home(os.path.realpath(path), home) \
            and _child_reads[0] < _CHILD_READ_BUDGET:
        key = (path, _safe_mtime(path), "childlabel")
        hit = _memo_get(key)
        if hit is not None and hit.get("at", 0) > now - _CHILD_MEMO_TTL:
            label = hit.get("label")
        else:
            _child_reads[0] += 1
            turns = first_instructions(path, max_turns=1, window=65536)
            t = derive_title(turns[0]) if turns else None
            label = sanitize_title(t, CHILD_TASK_CAP) if t else None
            _memo_put(key, {"label": label, "at": now})
    if role and label:
        return "%s · %s" % (role, label), "instruction"
    if role:
        return role, "native"           # harness-recorded role metadata
    if label:
        return label, "instruction"
    return None, None


# --------------------------------------------------------------------------
# pending native titles (scheduler launch + manual rename → the live Pi
# extension consumes the entry for its own pane and calls setSessionName)


def enqueue_native(pane_id, title, kind="scheduled", now=None):
    """Queue one native-name delivery for a live Pi pane. Best-effort:
    the wire title is already correct without it (levels 1/3), this only
    makes Pi itself show the same name."""
    t = sanitize_title(title)
    if not t or not PANE_RE.match(pane_id or ""):
        return False
    now = now if now is not None else time.time()
    doc = _load_json(PENDING_FILE)
    entries = doc.get("entries") if doc.get("schema_version") \
        == PENDING_SCHEMA else []
    entries = [e for e in entries if isinstance(e, dict)]
    now_i = int(now)
    entries = [e for e in entries
               if isinstance(e.get("ts"), int)
               and now_i - e["ts"] < PENDING_TTL_S
               and e.get("pane_id") != pane_id]
    entries.append({"pane_id": pane_id, "title": t, "kind": kind,
                    "ts": now_i})
    while len(entries) > PENDING_CAP:
        entries.pop(0)
    _save_json(PENDING_FILE,
               {"schema_version": PENDING_SCHEMA, "entries": entries})
    return True


def consume_native(pane_id, now=None):
    """Extension side: pop the pending entry for one pane (or None)."""
    if not PANE_RE.match(pane_id or ""):
        return None
    now = now if now is not None else time.time()
    doc = _load_json(PENDING_FILE)
    if doc.get("schema_version") != PENDING_SCHEMA:
        return None
    entries = [e for e in doc.get("entries", []) if isinstance(e, dict)]
    now_i = int(now)
    match = None
    keep = []
    for e in entries:
        ts = e.get("ts")
        if not isinstance(ts, int) or now_i - ts >= PENDING_TTL_S:
            continue
        if e.get("pane_id") == pane_id and match is None \
                and isinstance(e.get("title"), str):
            match = e
        else:
            keep.append(e)
    if match is None:
        return None
    _save_json(PENDING_FILE,
               {"schema_version": PENDING_SCHEMA, "entries": keep})
    return sanitize_title(match["title"])


# --------------------------------------------------------------------------
# rename (level 1) — host verb + phone verb


def rename_session(pane_id, title, agent=None, home=None, now=None):
    """Manual override for one pane's session. `title` None/--clear removes
    the override. `agent` is the live herdr row for the pane (the caller
    resolves it); no row → refuse (a pane-guarded override must never be
    written blind). Returns (ok, safe phrase)."""
    home = home or HOME
    now = now if now is not None else time.time()
    if not PANE_RE.match(pane_id or ""):
        return False, "bad pane id"
    if agent is None or not isinstance(agent, dict):
        return False, "pane not found"
    key = session_key(agent, home)
    if not key:
        return False, "session not identified"
    if title is None:
        titles = load_cache()
        prev = titles.get(key)
        if isinstance(prev, dict) and prev.get("source") == "manual":
            # a tombstone, not a delete: the manual name was also delivered
            # into the harness's own session name, which must not resurface
            # as "native" once the human has reset it
            titles[key] = {"cleared": prev.get("title"),
                           "at": int(now)}
            _save_cache(titles)
        return True, None
    t = sanitize_title(title)
    if not t:
        return False, "empty title"
    start = _start_of(agent, key)
    titles = load_cache()
    _store(titles, key, t, "manual", start, pane_id)
    _save_cache(titles)
    # native name too, when the pane is a live Pi session: the extension
    # picks the pending entry up within seconds. (An RPC route exists only
    # for headless `pi --mode rpc` sessions, which never host a herdr pane.)
    if str(agent.get("agent") or "").lower() == "pi":
        try:
            enqueue_native(pane_id, t, kind="manual", now=now)
        except Exception:
            pass
    return True, t


# --------------------------------------------------------------------------
# backfill (one-shot, idempotent; writes ONLY the host cache)


def _root_pi_sessions(home=None):
    """Bounded newest-first [(path, mtime)] of ROOT Pi transcripts: no
    parentSession in the header, and never the run/forks/child layouts."""
    home = home or HOME
    root = os.path.join(home, ".pi", "agent", "sessions")
    out = []
    if not os.path.isdir(root):
        return out
    for entry in os.listdir(root)[:512]:
        d = os.path.join(root, entry)
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not n.endswith(".jsonl"):
                continue
            p = os.path.join(d, n)
            mt = _safe_mtime(p)
            if mt is not None and _is_regular(p):
                out.append((p, mt))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def _root_claude_sessions(home=None):
    """Bounded newest-first [(path, uuid, mtime)] of Claude Code
    transcripts: ~/.claude/projects/<project>/<uuid>.jsonl, regular files
    under $HOME only."""
    home = home or HOME
    root = os.path.join(home, ".claude", "projects")
    out = []
    if not os.path.isdir(root):
        return out
    for entry in os.listdir(root)[:512]:
        d = os.path.join(root, entry)
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not n.endswith(".jsonl"):
                continue
            uuid = n[:-len(".jsonl")]
            if not UUID_RE.match(uuid):
                continue
            p = os.path.join(d, n)
            mt = _safe_mtime(p)
            if mt is not None and _is_regular(p) \
                    and _under_home(os.path.realpath(p), home):
                out.append((p, uuid, mt))
    out.sort(key=lambda x: x[2], reverse=True)
    return out


def backfill(dry_run=False, home=None, cache_path=None, limit=512,
             now=None, report_samples=15):
    """One-shot migration (§11): derive titles for root Pi sessions and
    Claude Code transcripts that have no native title and no cache entry,
    and persist level 4-5 titles in the host cache ONLY — existing JSONL
    files are never appended to. Idempotent: a second run is a no-op.
    Returns a report dict."""
    home = home or HOME
    now = now if now is not None else time.time()
    cache_path = cache_path or CACHE_FILE
    titles = load_cache(cache_path)
    derived = {"instruction": 0, "summary": 0}
    skipped = {"named": 0, "child": 0, "cached": 0, "underived": 0}
    claude = {"scanned": 0,
              "derived": {"instruction": 0, "summary": 0},
              "skipped": {"native": 0, "cached": 0, "underived": 0}}
    samples = []
    scanned = 0
    for path, _mt in _root_pi_sessions(home)[:limit]:
        scanned += 1
        h = session_header(path)
        if h.get("parent"):
            skipped["child"] += 1
            continue
        info = pi_native_info(path)
        if info.get("name"):
            turns1 = first_instructions(path, max_turns=1)
            if info.get("marker") or not info.get("only") or not (
                    turns1 and looks_auto_named(info["name"], turns1[0])):
                skipped["named"] += 1
                continue
        if _cache_valid(titles.get(path), None):
            skipped["cached"] += 1
            continue
        turns = first_instructions(path, max_turns=1)
        title = derive_title(turns[0]) if turns else None
        source = "instruction" if title else None
        if not title:
            skipped["underived"] += 1
            continue
        title = sanitize_title(title, WIRE_TITLE_CAP)
        if not title:
            skipped["underived"] += 1
            continue
        derived[source] += 1
        if len(samples) < report_samples:
            samples.append(title)
        if not dry_run:
            _store(titles, path, title, source, h.get("ts") or _safe_mtime(path))
    # Claude Code transcripts: same migration, keyed by the session UUID
    # (the identity live panes carry), guarded the same way, cache-only.
    for path, uuid, _mt in _root_claude_sessions(home)[:limit]:
        claude["scanned"] += 1
        if _cache_valid(titles.get(uuid), None):
            claude["skipped"]["cached"] += 1
            continue
        native = claude_native_title(path)
        if native:
            claude["skipped"]["native"] += 1
            continue
        turns = claude_first_instructions(path, max_turns=1)
        title = derive_title(turns[0]) if turns else None
        source = "instruction" if title else None
        if not title:
            claude["skipped"]["underived"] += 1
            continue
        title = sanitize_title(title, WIRE_TITLE_CAP)
        if not title:
            claude["skipped"]["underived"] += 1
            continue
        claude["derived"][source] += 1
        if len(samples) < report_samples:
            samples.append(title)
        if not dry_run:
            _store(titles, uuid, title, source,
                   _claude_session_start(path))
    if not dry_run and (derived["instruction"] or derived["summary"]
                        or claude["derived"]["instruction"]
                        or claude["derived"]["summary"]):
        _save_cache(titles, cache_path)
    return {
        "dry_run": bool(dry_run),
        "scanned": scanned,
        "derived": derived,
        "skipped": skipped,
        "claude": claude,
        "cache_entries": len(load_cache(cache_path)),
        "samples": samples,
    }


# --------------------------------------------------------------------------
# CLI: `remote-agents titles backfill [--dry-run]` (host console)


def _cmd_backfill(argv):
    dry = "--dry-run" in argv
    if [a for a in argv if a not in ("--dry-run",)]:
        print("usage: remote-agents titles backfill [--dry-run]",
              file=sys.stderr)
        return 2
    report = backfill(dry_run=dry)
    print(json.dumps(report, separators=(",", ":")))
    return 0


def main(argv):
    if argv[:1] == ["backfill"]:
        return _cmd_backfill(argv[1:])
    print("usage: remote-agents titles backfill [--dry-run]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
