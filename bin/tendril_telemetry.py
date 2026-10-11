#!/usr/bin/env python3
"""tendril_telemetry — schema v2 per-agent telemetry for `remote-agents snapshot`.

Derives, READ-ONLY, per-agent facts from the harness's own session transcript
(pi JSONL, Claude Code JSONL) so the phone map can show model, effort, session
age, context load, output tokens, cost, the current tool, an operational
activity phrase, the last event and a short event trail (TENDRIL Link
Topology.kt, AgentTelemetry).

Privacy contract (enforced structurally, not by filtering output):
- transcript CONTENT is never retained: no prompt text, no command lines, no
  directories, no titles — only tool names and file BASENAMES (sanitized) may
  surface, and only inside `activity`;
- only regular files are read; pi paths must realpath under $HOME and end in
  .jsonl; Claude ids must match ^[0-9a-f-]{36}$ before globbing;
- nothing here prints or logs transcript content, ever;
- subagent meta files are whitelist-projected (runId, agent, exitCode, model
  and usage numbers only): their `task`/`acceptance` prompt text is never
  copied into the cache or onto the wire;
- a bash command is classified on the host into at most one phase word
  ("testing"); the command string itself is never stored or emitted.

Children (contract 0.2): pi sessions whose first-line header names this file
as `parentSession`, pi-subagents run dirs/forks under the parent's stem
directory and Claude Code `<sessionId>/subagents/agent-*.jsonl` sidechains
are scanned with the same per-line parsers into per-child accumulators
stored inside the parent's cache entry. Header verdicts (path -> parent or
not) are cached so each child header line is read once.

NEVER fabricate: a field that cannot be derived reliably is omitted (the key
is absent). Sessions of other harnesses (codex, ...) get no telemetry.

Performance: transcripts can be hundreds of MB with base64 image lines. Reads
are incremental — a small cache (~/.cache/tendril/telemetry-v2.json keyed by
realpath) stores accumulators and a byte offset, so each call only reads bytes
appended since last time (children too). Lines are pre-filtered with
byte-substring markers
BEFORE any json.loads; over-long lines are mined with bounded head/tail regex
windows instead of being parsed. A wall-clock budget covers all agents per
call; agents past it get no telemetry this round (their cache advances next
time). A cold full backfill of one huge file may take a few seconds once.

Stdlib only; every failure mode degrades to "no telemetry" — never an error.
"""
import glob
import json
import os
import re
import shlex
import stat
import tempfile
import time
from datetime import datetime, timezone

# ---------- tunables (module-level so tests can patch) ----------
HOME = os.path.realpath(os.path.expanduser("~"))
CACHE_DIR = os.path.expanduser("~/.cache/tendril")
CACHE_FILE = os.path.join(CACHE_DIR, "telemetry-v2.json")
BUDGET_S = 1.5              # wall-clock budget for ALL agents per call
CHILD_BUDGET_FRAC = 0.25    # children may use at most this share of it
CHILD_QUIET_S = 20          # quiet this long + terminal stop (or meta 0) = done
CHILD_ACTIVE_S = 120        # changed within this window = working/tool/thinking
CHILD_CAP = 8               # children per agent on the wire, newest first
CHILD_SCAN_CAP = 200        # candidate child files inspected per parent
HEADER_BYTES = 4096         # pi child header: first line, bounded read
META_MAX_BYTES = 16 * 1024  # subagent meta files are tiny; refuse bigger
SCAN_MASK = 0x7FF           # parent deadline check every N lines
CATCHUP_GRACE_S = 8.5       # extra allowance for one cold full backfill
OPEN_TOOL_TTL_S = 30 * 60   # an open tool older than this is stale
HUGE_LINE = 256 * 1024      # above this: regex windows instead of json.loads
HEAD_BYTES = 512            # bounded head window for over-long lines
WINDOW_BYTES = 4096         # bounded window around a marker in over-long lines
MAX_OPEN_CALLS = 64         # ring guard: unmatched tool calls we remember
MAX_CACHE_ENTRIES = 1024
TRAIL_MAX = 12
UUID_RE = re.compile(r"^[0-9a-f-]{36}$")



def _real_model(value):
    """A model id worth reporting: harnesses stamp placeholders such as
    Claude Code's "<synthetic>" on interrupt/error turns — those must never
    replace the last REAL model."""
    return isinstance(value, str) and bool(value) and not value.startswith("<")

def _mono():
    """Monotonic clock (module-level so tests can fake the time budget)."""
    return time.monotonic()


def _time():
    """Host wall clock for staleness (tests pass `now` instead)."""
    return time.time()


# ---------- small parsers ----------
_RE_ISO_TS = re.compile(rb'"timestamp":"([0-9T:.+\-Z]{10,40})"')
_RE_PI_CALLID = re.compile(rb'"toolCallId":"([^"\\]{1,128})"')
_RE_PI_TOOLNAME = re.compile(rb'"toolName":"([^"\\]{1,80})"')
_RE_PI_MS_TS = re.compile(rb'"timestamp":(\d{10,16})')
_RE_PI_INPUT = re.compile(rb'"input":(\d+)')
_RE_PI_OUTPUT = re.compile(rb'"output":(\d+)')
_RE_PI_CACHEREAD = re.compile(rb'"cacheRead":(\d+)')
_RE_PI_CACHEWRITE = re.compile(rb'"cacheWrite":(\d+)')
_RE_PI_COST = re.compile(rb'"cost":\{[^{}]{0,400}?"total":(\d+(?:\.\d+)?)')
_RE_CL_INPUT = re.compile(rb'"input_tokens":(\d+)')
_RE_CL_OUTPUT = re.compile(rb'"output_tokens":(\d+)')
_RE_CL_CACHEREAD = re.compile(rb'"cache_read_input_tokens":(\d+)')
_RE_CL_CACHEWRITE = re.compile(rb'"cache_creation_input_tokens":(\d+)')
_RE_CL_USEID = re.compile(rb'"tool_use_id":"([^"\\]{1,160})"')


def _num(v):
    """int/float (bools excluded) else None."""
    return v if type(v) in (int, float) else None


def _ms(v):
    """Message timestamp in epoch SECONDS from a millisecond int."""
    v = _num(v)
    return None if v is None or v <= 0 else v / 1000.0


def _iso_to_epoch(s):
    """ISO-8601 → epoch seconds; naive strings are treated as UTC."""
    if not isinstance(s, str) or not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        t = datetime.fromisoformat(s)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.timestamp()
    except ValueError:
        return None


def _rx1(rx, blob):
    m = rx.search(blob)
    return m.group(1) if m else None


def _rx_int(rx, blob):
    """First regex capture as int, else None."""
    m = rx.search(blob)
    return int(m.group(1)) if m else None


# ---------- which transcript belongs to this agent (safety gate) ----------
def _is_regular(path):
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def _under_home(realpath, home):
    return realpath.startswith(home.rstrip(os.sep) + os.sep)


def _spec(agent, home):
    """(harness, transcript realpath or None). Only paths that passed the
    safety gate come back; anything else is None."""
    harness = str(agent.get("agent") or "").strip().lower()
    sess = agent.get("agent_session")
    if not isinstance(sess, dict):
        return harness, None
    kind, value = sess.get("kind"), sess.get("value")
    if not isinstance(value, str) or not value:
        return harness, None
    if kind == "path" and harness == "pi":
        if not value.endswith(".jsonl"):
            return harness, None
        rp = os.path.realpath(value)         # also collapses .. traversal
        if not _under_home(rp, home) or not _is_regular(rp):
            return harness, None
        return harness, rp
    if kind == "id" and harness == "claude":
        if not UUID_RE.match(value):         # BEFORE any globbing
            return harness, None
        pattern = os.path.join(home, ".claude", "projects", "*", value + ".jsonl")
        for hit in sorted(glob.glob(pattern)):
            rp = os.path.realpath(hit)
            if _under_home(rp, home) and _is_regular(rp):
                return harness, rp
    return harness, None


# ---------- accumulators (cache entry) ----------
def _fresh_entry(size, mtime):
    return {"size": size, "mtime": mtime, "offset": 0,
            "started_at": None, "model": None, "provider": None,
            "effort": None, "context_tokens": None, "output_tokens": 0,
            "cost_sum": None,            # pi: sum of usage.cost.total
            "cost_last": None,           # claude: latest cost-state.totalCostUSD
            "last_event": None, "trail": "", "last_seen_at": None,
            "open_calls": {},
            # v3 additions (content-free, see _event/_open_call)
            "events": [],                # wire events, <= TRAIL_MAX, newest last
            "stop_reason": None,         # last assistant stopReason (children)
            "last_prompt_at": None, "last_assistant_at": None,
            "last_error_ts": None}


def _fresh_child(size, mtime):
    """Child accumulator: same parser state plus child-only facts. No nested
    children: grandchildren are out of scope (and off the wire)."""
    e = _fresh_entry(size, mtime)
    e.update({"name": None, "async": False, "meta": None,
              "session_id": None, "run_id": None})
    return e


def _fresh_parent(size, mtime):
    e = _fresh_entry(size, mtime)
    e.update({"children": {},            # child path -> child accumulator
              "child_headers": {}})      # child path -> header verdict (once)
    return e


def _touch(entry, ts):
    """Record an event time: seeds started_at, advances last_seen_at."""
    if ts is None:
        return
    if entry["started_at"] is None:
        entry["started_at"] = ts
    if entry["last_seen_at"] is None or ts > entry["last_seen_at"]:
        entry["last_seen_at"] = ts


def _wire_event(entry, kind, tool, ts, name=None):
    """Append one v3 wire event ({kind, tool?, name?, at}); cap newest-last.
    Only kinds, tool NAMES, sanitized agent names and epoch times ever
    enter this list."""
    at = ts if ts is not None else entry.get("last_seen_at")
    if at is None:
        return
    ev = {"kind": kind}
    if tool:
        ev["tool"] = tool
    if name:
        ev["name"] = name
    ev["at"] = int(at)
    events = entry.get("events")
    if not isinstance(events, list):
        events = entry["events"] = []
    events.append(ev)
    if len(events) > TRAIL_MAX:
        del events[:-TRAIL_MAX]


def _event(entry, kind, tool, error, ts):
    """last_event + one trail char (P prompt, R reply, T tool ok, E error)
    + one v3 wire event. Never any content — only kind, tool NAME and time."""
    ev = {"kind": kind}
    if tool:
        ev["tool"] = tool
    if kind == "tool_result":
        ev["error"] = bool(error)
    if ts is not None:
        ev["at"] = int(ts)
    elif entry.get("last_seen_at"):
        ev["at"] = int(entry["last_seen_at"])
    if "at" in ev:
        entry["last_event"] = ev
    ch = "E" if (kind == "tool_result" and error) else \
         {"prompt": "P", "reply": "R", "tool_result": "T"}.get(kind)
    if ch:
        entry["trail"] = (entry["trail"] + ch)[-TRAIL_MAX:]
    at_ts = ts if ts is not None else entry.get("last_seen_at")
    if kind == "prompt":
        entry["last_prompt_at"] = at_ts
    elif kind == "reply":
        entry["last_assistant_at"] = at_ts
    elif kind == "tool_result" and error:
        entry["last_error_ts"] = at_ts
    wire = {"prompt": "prompt", "reply": "reply"}.get(kind)
    if wire is None and kind == "tool_result":
        wire = "error" if error else "tool"
    if wire:
        _wire_event(entry, wire, tool if isinstance(tool, str) else None, at_ts)


def _sanitize_base(name):
    """File basename safe for the wire: [A-Za-z0-9._-], <= 40 chars."""
    out = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:40]
    return out if out.strip("_") else None   # pure punctuation: nothing to show


def _arg_base(args):
    """Bounded path-argument extraction: a few known keys, basename only."""
    if not isinstance(args, dict):
        return None
    for k in ("path", "file_path", "filePath", "file", "filename",
              "notebook_path"):
        v = args.get(k)
        if isinstance(v, str) and 0 < len(v) <= 4096 and "://" not in v \
                and not v.startswith("/dev/"):
            return _sanitize_base(os.path.basename(v))
    return None


# ---------- host-only bash phase classification (contract 0.2 §1) ----------
_TEST_ARGV0 = {"pytest", "gradle", "gradlew", "unittest", "run_suite.py"}
_TEST_RUNNERS = {"npm", "pnpm", "yarn", "cargo", "go", "make"}


def _bash_phase_word(tool, args):
    """`testing` when an open bash call's argv[0] basename is a known test
    runner (npm/pnpm/yarn/cargo/go/make additionally need a `test` argument).
    HOST-ONLY: the command string is read here and immediately discarded —
    only the returned word may ever be stored in the cache or emitted."""
    if tool not in ("bash", "Bash") or not isinstance(args, dict):
        return None
    cmd = args.get("command")
    if not isinstance(cmd, str) or not cmd or len(cmd) > 4096:
        return None
    head = cmd[:512]
    try:
        toks = shlex.split(head, posix=True)[:6]
    except ValueError:
        toks = head.split()[:6]
    if not toks:
        return None
    argv0 = os.path.basename(toks[0].strip())
    if argv0 in _TEST_ARGV0:
        return "testing"
    if argv0 in _TEST_RUNNERS and any(
            os.path.basename(t) == "test" for t in toks[1:4]):
        return "testing"
    return None


def _is_delegate(name):
    return isinstance(name, str) and (
        name in _DELEGATE_TOOLS or name.startswith("subagent"))


def _open_call(entry, cid, name, ts, args):
    if not isinstance(cid, str) or not cid:
        return
    nm = name if isinstance(name, str) else ""
    oc = entry["open_calls"]
    if len(oc) >= MAX_OPEN_CALLS and cid not in oc:
        oc.pop(next(iter(oc)))               # drop the oldest unmatched
    # slot 3 holds ONLY the derived phase word — never the command text.
    oc[cid] = [nm, ts, _arg_base(args), _bash_phase_word(nm, args)]
    if _is_delegate(nm):
        # §7.2: a subagent event may carry the child TYPE name; status is
        # unknown at open time and never guessed here.
        _wire_event(entry, "subagent", nm, ts,
                    name=_delegate_name(args))
    # plain tools emit NO open-time event: kind "tool" means completed
    # (contract 0.2 §7.2); the completion path (_event) emits it.


def _delegate_name(args):
    """Sanitized agent TYPE name from a delegate call's arguments (§7.2
    subagent event `name`), or None. Only the sanitized token survives."""
    if not isinstance(args, dict):
        return None
    for k in ("agent", "name", "subagent_type", "subagent"):
        sn = _sanitize_name(args.get(k))
        if sn:
            return sn
    return None


def _close_call(entry, cid):
    rec = entry["open_calls"].pop(cid, None) if isinstance(cid, str) else None
    return rec[0] if rec and rec[0] else None


# ---------- pi transcript ----------
def _pi_usage(entry, out, inp, cache_read, cache_write, cost_total):
    if out is not None:
        entry["output_tokens"] += int(out)
    if out is not None or inp is not None or cache_read is not None \
            or cache_write is not None:
        entry["context_tokens"] = int((inp or 0) + (cache_read or 0)
                                      + (cache_write or 0))
    if cost_total is not None:
        entry["cost_sum"] = (entry["cost_sum"] or 0.0) + float(cost_total)


def _pi_obj(d, entry):
    t = d.get("type")
    if t == "model_change":
        _touch(entry, _iso_to_epoch(d.get("timestamp")))
        if isinstance(d.get("modelId"), str):
            entry["model"] = d["modelId"]
        if isinstance(d.get("provider"), str):
            entry["provider"] = d["provider"]
        return
    if t == "thinking_level_change":
        _touch(entry, _iso_to_epoch(d.get("timestamp")))
        if isinstance(d.get("thinkingLevel"), str):
            entry["effort"] = d["thinkingLevel"]
        return
    if t != "message":
        return
    m = d.get("message")
    if not isinstance(m, dict):
        return
    role = m.get("role")
    ts = _ms(m.get("timestamp"))
    _touch(entry, ts)
    if role == "assistant":
        if _real_model(m.get("model")):
            entry["model"] = m["model"]
        if isinstance(m.get("provider"), str):
            entry["provider"] = m["provider"]
        if isinstance(m.get("thinkingLevel"), str):
            entry["effort"] = m["thinkingLevel"]
        if isinstance(m.get("stopReason"), str):
            entry["stop_reason"] = m["stopReason"]
        u = m.get("usage")
        if isinstance(u, dict):
            c = u.get("cost")
            _pi_usage(entry, _num(u.get("output")), _num(u.get("input")),
                      _num(u.get("cacheRead")), _num(u.get("cacheWrite")),
                      _num(c.get("total")) if isinstance(c, dict) else None)
        has_tool = has_text = False
        items = m.get("content")
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                if it.get("type") == "toolCall":
                    has_tool = True
                    _open_call(entry, it.get("id"), it.get("name"), ts,
                               it.get("arguments"))
                elif it.get("type") == "text":
                    has_text = True
        if not has_tool and has_text:        # a tool-call turn is not a reply
            _event(entry, "reply", None, False, ts)
    elif role == "user":
        items = m.get("content")
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("type") == "text":
                    _event(entry, "prompt", None, False, ts)
                    break
        elif isinstance(items, str) and items.strip():
            _event(entry, "prompt", None, False, ts)
    elif role == "toolResult":
        name = m.get("toolName")
        _close_call(entry, m.get("toolCallId"))
        _event(entry, "tool_result",
               name if isinstance(name, str) else None,
               bool(m.get("isError")), ts)


def _pi_line(raw, entry):
    if len(raw) > HUGE_LINE:
        # NEVER json.loads image-bearing lines: bounded head/tail mining only.
        head = raw[:HEAD_BYTES]
        ts_iso = _rx1(_RE_ISO_TS, head)
        _touch(entry, _iso_to_epoch(ts_iso.decode("utf-8", "replace")
                                    if ts_iso else None))
        if b'"toolCallId"' in head:
            cid = _rx1(_RE_PI_CALLID, head)
            name = _rx1(_RE_PI_TOOLNAME, head)
            _close_call(entry, cid.decode("utf-8", "replace") if cid else None)
            mts = _rx1(_RE_PI_MS_TS, raw[-HEAD_BYTES:])
            _event(entry, "tool_result",
                   name.decode("utf-8", "replace") if name else None,
                   b'"isError":true' in raw,
                   _ms(int(mts)) if mts else None)
        elif b'"usage"' in raw:
            at = raw.find(b'"usage"')
            win = raw[at:at + WINDOW_BYTES]
            cost = _rx1(_RE_PI_COST, win)
            _pi_usage(entry, _rx_int(_RE_PI_OUTPUT, win),
                      _rx_int(_RE_PI_INPUT, win),
                      _rx_int(_RE_PI_CACHEREAD, win),
                      _rx_int(_RE_PI_CACHEWRITE, win),
                      float(cost) if cost else None)
            if b'"toolCall"' not in raw and b'"text"' in raw:
                _event(entry, "reply", None, False, None)
        return
    # cheapest markers first: toolResult lines dominate, then assistant usage
    if b'"toolCallId"' in raw or b'"usage"' in raw or b'"role":"user"' in raw \
            or b'"role":"assistant"' in raw or b'model_change' in raw \
            or b'thinking_level_change' in raw:
        try:
            d = json.loads(raw)
        except Exception:
            return
        if isinstance(d, dict):
            _pi_obj(d, entry)


# ---------- claude transcript ----------
def _cl_usage(entry, out, inp, cache_read, cache_write):
    if out is not None:
        entry["output_tokens"] += int(out)
    if out is not None or inp is not None or cache_read is not None \
            or cache_write is not None:
        entry["context_tokens"] = int((inp or 0) + (cache_read or 0)
                                      + (cache_write or 0))


def _cl_obj(d, entry):
    t = d.get("type")
    ts = _iso_to_epoch(d.get("timestamp"))
    if t == "assistant":
        _touch(entry, ts)
        m = d.get("message")
        if not isinstance(m, dict):
            return
        if _real_model(m.get("model")):
            entry["model"] = m["model"]
        if isinstance(d.get("effort"), str):     # recorded by the harness
            entry["effort"] = d["effort"]
        sr = m.get("stop_reason")
        if isinstance(sr, str):
            entry["stop_reason"] = sr
        u = m.get("usage")
        if isinstance(u, dict):
            _cl_usage(entry, _num(u.get("output_tokens")),
                      _num(u.get("input_tokens")),
                      _num(u.get("cache_read_input_tokens")),
                      _num(u.get("cache_creation_input_tokens")))
        has_tool = has_text = False
        items = m.get("content")
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                if it.get("type") == "tool_use":
                    has_tool = True
                    _open_call(entry, it.get("id"), it.get("name"), ts,
                               it.get("input"))
                elif it.get("type") == "text":
                    has_text = True
        if not has_tool and has_text:
            _event(entry, "reply", None, False, ts)
    elif t == "user":
        _touch(entry, ts)
        m = d.get("message")
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str):
            if c.strip():
                _event(entry, "prompt", None, False, ts)
        elif isinstance(c, list):
            saw_text = False
            for it in c:
                if not isinstance(it, dict):
                    continue
                if it.get("type") == "tool_result":
                    _touch(entry, ts)
                    _event(entry, "tool_result",
                           _close_call(entry, it.get("tool_use_id")),
                           bool(it.get("is_error")), ts)
                elif it.get("type") == "text":
                    saw_text = True
            if saw_text:
                _event(entry, "prompt", None, False, ts)
    elif t == "cost-state":
        v = _num(d.get("totalCostUSD"))
        if v is not None:
            entry["cost_last"] = float(v)


def _cl_line(raw, entry):
    if len(raw) > HUGE_LINE:
        head = raw[:HEAD_BYTES]
        ts_iso = _rx1(_RE_ISO_TS, head)
        _touch(entry, _iso_to_epoch(ts_iso.decode("utf-8", "replace")
                                    if ts_iso else None))
        if b'"tool_use_id"' in head:
            cid = _rx1(_RE_CL_USEID, head)
            _close_call(entry, cid.decode("utf-8", "replace") if cid else None)
            _event(entry, "tool_result", None, b'"is_error":true' in raw,
                   _iso_to_epoch(_rx1(_RE_ISO_TS, raw[-HEAD_BYTES:])))
        elif b'"usage"' in raw:
            at = raw.find(b'"usage"')
            win = raw[at:at + WINDOW_BYTES]
            _cl_usage(entry, _rx_int(_RE_CL_OUTPUT, win),
                      _rx_int(_RE_CL_INPUT, win),
                      _rx_int(_RE_CL_CACHEREAD, win),
                      _rx_int(_RE_CL_CACHEWRITE, win))
            if b'"tool_use"' not in raw and b'"text"' in raw:
                _event(entry, "reply", None, False, None)
        return
    if b'"type":"assistant"' in raw or b'"type":"user"' in raw \
            or b'cost-state' in raw:
        try:
            d = json.loads(raw)
        except Exception:
            return
        if isinstance(d, dict):
            _cl_obj(d, entry)


# ---------- incremental scan driver ----------
_HANDLERS = {"pi": _pi_line, "claude": _cl_line}


def _scan(path, entry, harness, deadline, hard_deadline, mask=SCAN_MASK):
    """Read only bytes past entry["offset"]; update accumulators in place.
    Truncation/rotation (file shrank) resets the entry — child header
    verdicts survive (paths do not change meaning). A partial final line
    is left for the next call. Progress is persisted even when the time
    budget ends mid-file, so a huge backfill advances across calls. The
    deadline check runs every `mask+1` lines (children use a tighter mask)."""
    try:
        st = os.stat(path)
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode):
        return
    size, mtime = st.st_size, st.st_mtime
    if size < entry.get("size", 0) or entry["offset"] > size:
        keep = {k: entry[k] for k in ("child_headers",)
                if isinstance(entry.get(k), dict)}
        entry.clear()                        # truncated or replaced: reset
        entry.update(_fresh_entry(size, mtime))
        entry.update(keep)
    offset = entry["offset"]
    if offset == size:
        entry["size"], entry["mtime"] = size, mtime
        return
    limit = hard_deadline if offset == 0 else deadline
    handle = _HANDLERS.get(harness)
    if handle is None:
        return
    pos = offset
    seen = 0
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break                    # writer mid-line: come back later
                seen += 1
                try:
                    handle(raw, entry)
                except Exception:
                    pass                     # one bad line never kills a scan
                pos += len(raw)
                if entry["started_at"] is None:
                    ts = _rx1(_RE_ISO_TS, raw[:HEAD_BYTES])
                    if ts:
                        _touch(entry, _iso_to_epoch(
                            ts.decode("utf-8", "replace")))
                if (seen & mask) == 0 and _mono() > limit:
                    break                    # budget out; offset keeps progress
    except OSError:
        return
    entry["offset"] = pos
    entry["size"], entry["mtime"] = size, mtime


# ---------- activity phrase (operational, never content) ----------
_EDIT_TOOLS = {"edit", "write", "apply_patch", "Edit", "Write", "MultiEdit",
               "NotebookEdit"}
_READ_TOOLS = {"read", "Read"}
_RUN_TOOLS = {"bash", "Bash"}
_SEARCH_TOOLS = {"grep", "glob", "find", "Grep", "Glob", "ls"}
_WEB_TOOLS = {"web_search", "WebSearch", "fetch", "WebFetch", "web_fetch"}
_DELEGATE_TOOLS = {"Task", "Agent"}


def _tool_phrase(name, base):
    if name in _DELEGATE_TOOLS or name.startswith("subagent"):
        return "Delegating to a subagent"
    if name in _EDIT_TOOLS:
        return ("Editing " + base) if base else "Editing files"
    if name in _READ_TOOLS:
        return ("Reading " + base) if base else "Reading files"
    if name in _RUN_TOOLS:
        return "Running a command"           # never the command itself
    if name in _SEARCH_TOOLS:
        return "Searching the repository"
    if name in _WEB_TOOLS:
        return "Researching the web"
    return "Using " + (_sanitize_base(name) or "tool")


def _activity(status, tool_name, tool_base):
    if status == "blocked":
        return "Waiting for input"
    if tool_name:
        return _tool_phrase(tool_name, tool_base)
    if status == "working":
        return "Thinking"
    if status == "idle":
        return "Idle"
    if status == "done":
        return "Finished"
    return None                              # unknown status: omit, don't guess


def _wire_phase(status, tool_name, bash_phase):
    """Contract 0.2 §1 phase: one word, host-classified, never a command."""
    if tool_name:
        if bash_phase:
            return bash_phase
        if tool_name in _EDIT_TOOLS:
            return "editing"
        if tool_name in _READ_TOOLS or tool_name in _SEARCH_TOOLS:
            return "reviewing"
        return "tool"
    if status == "working":
        return "thinking"
    return None                              # absent otherwise


# ---------- provenance: provider / adapter / billing / context window ----------
# Contract 0.2 §7.1: provider is the canonical UPSTREAM VENDOR (a bridge or
# route name is never a provider); adapter names the bridge when one is
# used; billing is metered only for real per-call charges; context_window
# comes ONLY from an explicit local registry — never a built-in table,
# never guessed from a model name.
_PROVIDERS = {
    # raw transcript provider token → (canonical vendor, adapter | None)
    "claude-bridge": ("anthropic", "claude-bridge"),
    "openai-codex": ("openai", None),
    "openai": ("openai", None),
    "anthropic": ("anthropic", None),
    "zai": ("zai", None),
    "google": ("google", None),
    "mistral": ("mistral", None),
}
# Deliberately unmapped (provider stays ABSENT until the id maps
# unambiguously — no guessing): optollm and any unknown token.
_MODELS_STORE = ".pi/agent/models-store.json"
_BRIDGE_CFG = ".pi/agent/claude-bridge.json"
_BRIDGE_MODELS_TS = ".pi/agent/npm/node_modules/pi-claude-bridge/src/models.ts"
_CREDENTIALS = ".claude/.credentials.json"
_REG_CACHE = {}                       # key → (mtime | None, value)


def _registry(key, path, loader):
    """mtime-keyed registry read: re-reads only when the file changed;
    a missing file is cached as (None, None). Keyed by (name, path) so
    distinct homes never share an entry. Never raises."""
    cache_key = (key, path)
    try:
        mt = os.stat(path).st_mtime_ns
    except OSError:
        mt = None
    hit = _REG_CACHE.get(cache_key)
    if hit is not None and hit[0] == mt:
        return hit[1]
    val = None
    if mt is not None:
        try:
            val = loader(path)
        except Exception:
            val = None
    _REG_CACHE[cache_key] = (mt, val)
    return val


def _models_store(home):
    """{raw_provider: {model_id: contextWindow}} from Pi's models-store
    (its `models` value is a list of entries carrying id + contextWindow).
    Only that one field is copied; nothing else is read."""
    def load(path):
        with open(path, "rb") as f:
            d = json.loads(f.read(4 * 1024 * 1024))
        if not isinstance(d, dict):
            return None
        out = {}
        for prov, pv in d.items():
            models = pv.get("models") if isinstance(pv, dict) else None
            if not isinstance(models, list):
                continue
            ids = {}
            for m in models[:512]:
                if isinstance(m, dict) and isinstance(m.get("id"), str) \
                        and isinstance(m.get("contextWindow"), int) \
                        and not isinstance(m["contextWindow"], bool) \
                        and m["contextWindow"] > 0:
                    ids[m["id"]] = m["contextWindow"]
            if ids:
                out[prov] = ids
        return out or None
    return _registry("models", os.path.join(home, _MODELS_STORE), load)


def _bridge_settings(home):
    """(plan, extra_usage, forced_200k_ids) from claude-bridge.json — the
    plan-tier settings only (never any credential material)."""
    def load(path):
        with open(path, "rb") as f:
            d = json.loads(f.read(65536))
        p = d.get("provider") if isinstance(d, dict) else None
        if not isinstance(p, dict):
            return None
        plan = p.get("plan")
        force = p.get("forceTwoHundredK")
        return (plan if plan in ("pro", "max") else None,
                bool(p.get("longContextExtraUsage")),
                set(force) if isinstance(force, list) else set())
    return _registry("bridgecfg", os.path.join(home, _BRIDGE_CFG), load)


_RE_TS_CONST = re.compile(r"const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([0-9_]{1,12})\s*;")
_RE_TS_MEASURED = re.compile(r"MEASURED_ONE_M\s*=\s*new\s+Set\(\[(.*?)\]",
                             re.S)
_RE_TS_GATED = re.compile(r"PLAN_GATED_ONE_M[^{]*\{(.*?)\n\}", re.S)
_RE_TS_GATE_ROW = re.compile(
    r'"(claude-[a-z0-9._-]{1,64})":\s*\(settings\)\s*=>\s*(.+?),?\s*(?=\n|\}|$)')


def _bridge_model_policy(home):
    """(measured_1m, gated, two_k, one_m) parsed DEFENSIVELY from the
    installed claude-bridge model policy (src/models.ts). The bridge
    registers exactly this window per model — pi's own status-bar
    denominator — so it is the authoritative local definition:
    forceTwoHundredK → two_k; MEASURED_ONE_M ids → one_m; the two known
    plan-gated ids → per claude-bridge.json plan settings; everything
    else → two_k, which is the MEASURED served window of a bare id, not
    a placeholder (see the bridge's diag/CONTEXT-SIZE.md). Any parse
    failure → None: no window is ever emitted from a guessed source."""
    def load(path):
        with open(path, "rb") as f:
            raw = f.read(512 * 1024).decode("utf-8", "replace")
        consts = {k: int(v.replace("_", ""))
                  for k, v in _RE_TS_CONST.findall(raw)}
        two_k = consts.get("TWO_HUNDRED_K_CONTEXT")
        one_m = consts.get("ONE_M_CONTEXT")
        if not two_k or not one_m or one_m <= two_k:
            return None
        measured = set()
        m = _RE_TS_MEASURED.search(raw)
        if m:
            measured = set(re.findall(r'"(claude-[a-z0-9._-]{1,64})"',
                                      m.group(1)))
        gated = {}
        g = _RE_TS_GATED.search(raw)
        if g:
            for mid, pred in _RE_TS_GATE_ROW.findall(g.group(1)):
                gated[mid] = pred.strip()
        return measured, gated, two_k, one_m
    return _registry("bridgems", os.path.join(home, _BRIDGE_MODELS_TS), load)


def _gate_open(pred, plan, extra):
    """Evaluate ONLY the gate predicate shapes the installed file ships;
    anything else conservatively stays closed (the bridge's own safe
    default window applies)."""
    p = re.sub(r"\s+", "", pred)
    if p == 'settings.plan==="max"||settings.longContextExtraUsage':
        return plan == "max" or extra
    if p == "settings.longContextExtraUsage":
        return extra
    return False


def _bridge_window(model, home):
    """(window, 'bridge_config') for a claude-bridge model id, or
    (None, None) when the installed policy cannot be parsed."""
    pol = _bridge_model_policy(home)
    if not pol or not isinstance(model, str) or not model:
        return None, None
    measured, gated, two_k, one_m = pol
    settings = _bridge_settings(home)
    plan = settings[0] if settings else None
    extra = settings[1] if settings else False
    forced = settings[2] if settings else set()
    if model in forced:
        return two_k, "bridge_config"
    if model in measured:
        return one_m, "bridge_config"
    if model in gated:
        return (one_m if _gate_open(gated[model], plan, extra) else two_k,
                "bridge_config")
    return two_k, "bridge_config"          # bare id: measured 200K served


def _context_window(harness, raw_provider, model, home):
    """(window, source) — pi_models_store for pi provider+model ids listed
    there, bridge_config for claude-bridge via the installed policy.
    NOTHING else: a model missing from every registry emits no window."""
    if harness == "pi" and isinstance(model, str) and model:
        store = _models_store(home)
        if store and isinstance(raw_provider, str):
            w = store.get(raw_provider, {}).get(model)
            if isinstance(w, int) and not isinstance(w, bool) and w > 0:
                return w, "pi_models_store"
        if raw_provider == "claude-bridge":
            return _bridge_window(model, home)
    return None, None


def _cred_oauth(home):
    """True when ~/.claude/.credentials.json CONTAINS a claudeAiOauth
    object. Key presence only — no value is ever read, copied or logged."""
    def load(path):
        with open(path, "rb") as f:
            d = json.loads(f.read(65536))
        return isinstance(d, dict) and "claudeAiOauth" in d
    return bool(_registry("cred", os.path.join(home, _CREDENTIALS), load))


def _provenance(harness, entry, home):
    """§7.1 flat facts for one agent/child entry: provider (+adapter),
    billing, and cost_usd ONLY when billing is metered. Claude Code's
    upstream is Anthropic by definition; its billing is provider_managed
    on a claude.ai OAuth login (key presence only) and otherwise metered
    when a real totalCostUSD was recorded. pi billing follows the route:
    a claude-bridge turn is provider_managed (the transcript's cost.total
    is 0); a positive per-call cost is metered; zero/absent cost with
    tokens is unknown; nothing derived at all emits no billing."""
    out = {}
    if harness == "claude":
        out["provider"] = "anthropic"
        if _cred_oauth(home):
            out["billing"] = "provider_managed"
        else:
            cost = entry.get("cost_last")
            cost = float(cost) if isinstance(cost, (int, float)) else 0.0
            out["billing"] = "metered" if cost > 0 else "unknown"
            if cost > 0:
                out["cost_usd"] = round(cost, 4)
        return out
    raw = entry.get("provider")
    mapped = _PROVIDERS.get(raw) if isinstance(raw, str) else None
    if mapped is not None:
        provider, adapter = mapped
        out["provider"] = provider
        if adapter:
            out["adapter"] = adapter
    if raw == "claude-bridge":              # subscription route: never metered
        out["billing"] = "provider_managed"
        return out
    # billing does not depend on knowing the route: a positive transcript
    # cost is a real per-call charge even when the provider token is one
    # we do not map (the provider key then stays absent — no guessing).
    cost = entry.get("cost_sum")
    cost = float(cost) if isinstance(cost, (int, float)) else 0.0
    tokens = (entry.get("context_tokens") or 0) \
        + (entry.get("output_tokens") or 0)
    if cost > 0:
        out["billing"] = "metered"
        out["cost_usd"] = round(cost, 4)
    elif tokens > 0:
        out["billing"] = "unknown"
    return out


# ---------- wire emit ----------
def _emit(harness, entry, status, now, home=None):
    home = home or HOME
    out = {"harness": harness}
    for k in ("model", "effort"):
        v = entry.get(k)
        if isinstance(v, str) and v:
            out[k] = v
    out.update(_provenance(harness, entry, home))
    cw, cws = _context_window(harness, entry.get("provider"),
                              entry.get("model"), home)
    if cw:
        out["context_window"] = int(cw)
        out["context_window_source"] = cws
    if entry.get("started_at"):
        out["started_at"] = int(entry["started_at"])
    if entry.get("context_tokens"):
        out["context_tokens"] = int(entry["context_tokens"])
    if entry.get("output_tokens"):
        out["output_tokens"] = int(entry["output_tokens"])
    seen = entry.get("last_seen_at")
    oc = entry.get("open_calls") or {}
    tool_base = None
    rec_phase = None
    if oc and seen is not None and (now - seen) <= OPEN_TOOL_TTL_S:
        _cid, rec = max(oc.items(), key=lambda kv: kv[1][1] or 0)
        name, ts, tool_base = rec[0], rec[1], rec[2]
        rec_phase = rec[3] if len(rec) > 3 else None
        if name:
            out["tool"] = name
            if ts:
                out["tool_started_at"] = int(ts)
        else:
            tool_base = None
    phrase = _activity(status, out.get("tool"), tool_base)
    if phrase:
        out["activity"] = phrase
    ph = _wire_phase(status, out.get("tool"), rec_phase)
    if ph:
        out["phase"] = ph
    ev = entry.get("last_event")
    if isinstance(ev, dict) and "at" in ev:
        out["last_event"] = dict(ev)
    if entry.get("trail"):
        out["trail"] = entry["trail"]
    events = entry.get("events")
    if isinstance(events, list) and events:
        out["events"] = [dict(e) for e in events[-TRAIL_MAX:]
                         if isinstance(e, dict)]
    kids = entry.get("children")
    if isinstance(kids, dict) and kids:
        rows = sorted((ce for ce in kids.values() if isinstance(ce, dict)),
                      key=_child_last, reverse=True)
        children = [_child_emit(harness, ce, now, home,
                                entry.get("provider"))
                    for ce in rows[:CHILD_CAP]]
        if children:
            out["children"] = children
    return out


# ---------- cache ----------
def _load_cache(path):
    try:
        with open(path, "rb") as f:
            d = json.loads(f.read(4 * 1024 * 1024))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_cache(path, cache):
    try:
        d = os.path.dirname(path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        body = json.dumps(cache, separators=(",", ":")).encode("utf-8")
        fd, tmp = tempfile.mkstemp(prefix=".telemetry-", dir=d)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            dfd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    except OSError:
        pass


# ---------- subagent children (pi / claude) ----------
def _sanitize_name(v, cap=24):
    """Agent TYPE name for the wire: [A-Za-z0-9._-], <= 24 chars."""
    if not isinstance(v, str) or not v:
        return None
    out = re.sub(r"[^A-Za-z0-9._-]", "_", v)[:cap]
    return out if out.strip("._-") else None


def _short_id(*cands):
    """First 8 hex chars of the first usable uuid / runId candidate."""
    for c in cands:
        if isinstance(c, str) and c:
            s = re.sub(r"[^0-9a-fA-F]", "", c)
            if s:
                return s[:8].lower()
    return None


def _pi_header(path):
    """Whitelist projection of a pi session header (first line, <= 4 KB):
    id / parentSession / name / timestamp ONLY — never cwd. {} when the file
    exists but has no parseable session header; None when unreadable."""
    try:
        with open(path, "rb") as f:
            head = f.read(HEADER_BYTES)
    except OSError:
        return None
    line = head.split(b"\n", 1)[0][:HEADER_BYTES]
    try:
        d = json.loads(line)
    except Exception:
        return {}
    if not isinstance(d, dict) or d.get("type") != "session":
        return {}
    out = {}
    for k in ("id", "parentSession", "name"):
        v = d.get(k)
        if isinstance(v, str) and v:
            out[k] = v
    ts = _iso_to_epoch(d.get("timestamp"))
    if ts is not None:
        out["ts"] = ts
    return out


def _jsonl_candidates(*dirs):
    """Bounded newest-first [(path, mtime)] of regular *.jsonl files."""
    out = []
    for d in dirs:
        if not d:
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
    return out[:CHILD_SCAN_CAP]


def _safe_mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _child_ok(path, home):
    """Same safety gate as parent transcripts: realpath under $HOME, regular
    file, .jsonl. Symlinks escaping home never reach a parser."""
    if not isinstance(path, str) or not path.endswith(".jsonl"):
        return False
    rp = os.path.realpath(path)
    return _under_home(rp, home) and _is_regular(rp)


def _pi_child_candidates(parent_path, entry, home):
    """Bounded pi child candidates for one parent: same-dir sessions whose
    header names this file as parentSession (tintinweb Agent children), plus
    the pi-subagents layout <stem>/<runId>/run-*/session.jsonl and
    <stem>/forks/*.jsonl. Candidates are gated through _child_ok BEFORE any
    byte is read (a symlink escaping $HOME is never opened). Header verdicts
    are cached per parent entry so each child's first line is read at most
    once. Same-dir candidates must also be at least as new as parent
    start - 60s."""
    d = os.path.dirname(parent_path)
    stem = os.path.basename(parent_path)[:-len(".jsonl")]
    stem_dir = os.path.join(d, stem)
    headers = entry.setdefault("child_headers", {})
    if len(headers) > 512:                   # bounded verdict memory
        for k in list(headers)[:len(headers) - 512]:
            del headers[k]
    floor = entry.get("started_at")
    floor = (floor - 60.0) if isinstance(floor, (int, float)) else None
    out = []                                 # (path, mtime, run_id, async, cid, name, ts)
    for p, mt in _jsonl_candidates(d):
        if os.path.realpath(p) == parent_path:
            continue
        if floor is not None and mt < floor:
            continue
        v = headers.get(p)
        if not isinstance(v, dict):
            # gate BEFORE the first (and only) header read; _scan_children
            # re-verifies every candidate per call, so already-verdicted
            # paths skip the realpath/stat work entirely on warm calls.
            if not _child_ok(p, home):
                headers[p] = {"parent": False}
                continue
            h = _pi_header(p) or {}
            ps = h.get("parentSession")
            v = {"parent": bool(ps and os.path.realpath(ps) == parent_path),
                 "id": h.get("id"), "name": h.get("name"), "ts": h.get("ts")}
            headers[p] = v
        if v.get("parent"):
            out.append((p, mt, None, False, v.get("id"), v.get("name"),
                        v.get("ts")))
    # pi-subagents run dirs: <stem>/<runId>/run-*/session.jsonl (+ forks/)
    run_rows = []
    try:
        top = sorted(os.listdir(stem_dir))[:64]
    except OSError:
        top = []
    for rid in top:
        rdir = os.path.join(stem_dir, rid)
        if rid == "forks" or not os.path.isdir(rdir):
            continue
        try:
            subs = os.listdir(rdir)[:32]
        except OSError:
            continue
        for sub in subs:
            if not sub.startswith("run-"):
                continue
            sp = os.path.join(rdir, sub, "session.jsonl")
            mt = _safe_mtime(sp)
            if mt is not None and _is_regular(sp) and _child_ok(sp, home):
                run_rows.append((sp, mt, rid))
    run_rows.sort(key=lambda x: x[1], reverse=True)
    for sp, mt, rid in run_rows[:CHILD_SCAN_CAP]:
        out.append((sp, mt, rid, True, None, None, None))
    for fp, mt in _jsonl_candidates(os.path.join(stem_dir, "forks")):
        if _child_ok(fp, home):
            out.append((fp, mt, None, True, None, None, None))
    return out


def _cl_child_candidates(parent_path, home):
    """Claude Code sidechains: <munged>/<sessionId>/subagents/agent-*.jsonl.
    The directory's existence is the parentage proof; no header read. Files
    not matching the agent-*.jsonl shape, or escaping $HOME (symlink), are
    skipped before any byte is read."""
    d = os.path.dirname(parent_path)
    stem = os.path.basename(parent_path)[:-len(".jsonl")]
    sub = os.path.join(d, stem, "subagents")
    out = []
    for p, mt in _jsonl_candidates(sub):
        m = re.search(r"agent-([0-9a-fA-F][0-9a-zA-Z_-]{0,64})\.jsonl$",
                      os.path.basename(p))
        if m is None or not _child_ok(p, home):
            continue
        out.append((p, mt, None, False, m.group(1), None, None))
    return out


_META_KEYS = ("runId", "agent", "exitCode", "model")
_META_USAGE_KEYS = ("input", "output", "cacheRead", "cacheWrite",
                    "cost", "turns")


def _meta_project(d):
    """Whitelist projection of a subagent meta file. `task`/`acceptance`
    (prompt text!) are structurally unreachable: only the keys below are
    ever copied, so they can never reach the cache or the wire."""
    if not isinstance(d, dict):
        return None
    out = {}
    for k in _META_KEYS:
        v = d.get(k)
        if k == "exitCode":
            if type(v) is int:
                out[k] = v
        elif isinstance(v, str) and v:
            out[k] = v
    u = d.get("usage")
    if isinstance(u, dict):
        uu = {}
        for k in _META_USAGE_KEYS:
            v = u.get(k)
            if type(v) in (int, float):
                uu[k] = v
        if uu:
            out["usage"] = uu
    return out or None


def _child_meta(child_path, run_id):
    """Whitelisted meta projection for a pi-subagents run child, read from
    <stem>/<runId>/subagent-artifacts/<runId>_*_meta.json. A meta file is
    never guessed across children: the run id must prefix the file name.
    (projection, mtime) — projection None when nothing usable was found."""
    if not run_id:
        return None, None
    run_dir = os.path.dirname(os.path.dirname(child_path))
    art = os.path.join(run_dir, "subagent-artifacts")
    try:
        names = sorted(os.listdir(art))[:16]
    except OSError:
        return None, None
    prefix = run_id + "_"
    for n in names:
        if n.startswith(prefix) and n.endswith("_meta.json"):
            path = os.path.join(art, n)
            try:
                with open(path, "rb") as f:
                    raw = f.read(META_MAX_BYTES + 1)
            except OSError:
                continue
            if len(raw) > META_MAX_BYTES:
                continue                     # oversized: not a meta file
            try:
                proj = _meta_project(json.loads(raw))
            except Exception:
                proj = None
            if proj is not None:
                return proj, _safe_mtime(path)
    return None, None


def _terminal_stop(sr):
    return isinstance(sr, str) and bool(sr) and sr not in (
        "toolUse", "tool_use", "tool")


def _error_stop(sr):
    """An EXPLICIT error stop reason (e.g. pi's "error" family): failed
    evidence per contract 0.2 §7.2. Generic stops are never errors."""
    return isinstance(sr, str) and (sr == "error" or sr.startswith("error_"))


def _error_is_final(ce):
    """The child's last record is an error tool result: no assistant turn,
    no prompt and no later-opened tool call happened after it (§7.2
    'error as FINAL record'). An earlier tool error followed by more work
    is recovery, never failure."""
    err = ce.get("last_error_ts")
    if err is None:
        return False
    for k in ("last_assistant_at", "last_prompt_at"):
        v = ce.get(k)
        if isinstance(v, (int, float)) and v >= err:
            return False
    for rec in (ce.get("open_calls") or {}).values():
        if isinstance(rec, (list, tuple)) and len(rec) > 1 \
                and isinstance(rec[1], (int, float)) and rec[1] >= err:
            return False
    return True


def _child_last(ce):
    vals = [v for v in (ce.get("last_seen_at"), ce.get("mtime"))
            if isinstance(v, (int, float))]
    return max(vals) if vals else None


def _child_status(ce, now):
    """done | failed | working | tool | thinking | stale | unknown per
    contract 0.2 §7.2. exitCode wins; then explicit error evidence (an
    error stop reason, or an error as the FINAL record with no later
    assistant turn); otherwise a terminal stop → done; the active-window
    rules are unchanged; quiet past the active window with no terminal
    evidence → stale; unknown only with no timestamps at all. An earlier
    tool error followed by more work is NEVER a failure, and historical
    children are done/stale/unknown — never failed by inference."""
    code = (ce.get("meta") or {}).get("exitCode")
    if isinstance(code, int):
        return "done" if code == 0 else "failed"
    last = _child_last(ce)
    if last is None:
        return "unknown"
    quiet = now - last
    if _error_stop(ce.get("stop_reason")):
        return "failed"
    if quiet >= CHILD_QUIET_S and _error_is_final(ce):
        return "failed"
    if quiet <= CHILD_ACTIVE_S:
        if _terminal_stop(ce.get("stop_reason")) \
                and quiet >= CHILD_QUIET_S:
            return "done"
        if ce.get("open_calls"):
            return "tool"
        ap = ce.get("last_assistant_at")
        rp = ce.get("last_prompt_at")
        if isinstance(rp, (int, float)) and (not isinstance(ap, (int, float))
                                             or rp >= ap):
            return "working"
        return "thinking"
    if _terminal_stop(ce.get("stop_reason")):
        return "done"
    return "stale"


def _child_emit(harness, ce, now, home=None, parent_provider=None):
    """One wire child dict; unknown facts are omitted, never fabricated.
    §7.1 provenance rides on children too: the child transcript's own
    provider wins, falling back to the parent's route for children that
    have recorded none."""
    home = home or HOME
    status = _child_status(ce, now)
    out = {"kind": "subagent", "harness": harness, "status": status}
    sid = _short_id(ce.get("run_id"), ce.get("session_id"))
    if sid:
        out["id"] = sid
    name = _sanitize_name(ce.get("name")
                          or (ce.get("meta") or {}).get("agent"))
    if name:
        out["name"] = name
    for k in ("model", "effort"):
        v = ce.get(k)
        if isinstance(v, str) and v:
            out[k] = v
    if ce.get("started_at"):
        out["started_at"] = int(ce["started_at"])
    meta = ce.get("meta")
    mu = meta.get("usage") if isinstance(meta, dict) and \
        isinstance(meta.get("usage"), dict) else {}
    ctx = ce.get("context_tokens")
    if not ctx and mu:
        total = (mu.get("input") or 0) + (mu.get("cacheRead") or 0) \
                + (mu.get("cacheWrite") or 0)
        ctx = int(total) if total else None
    if ctx:
        ce["context_tokens"] = int(ctx)      # meta-derived: feed provenance
        out["context_tokens"] = int(ctx)
    outp = ce.get("output_tokens")
    if not outp and mu.get("output") is not None:
        outp = mu["output"]
    if outp:
        ce["output_tokens"] = int(outp)
        out["output_tokens"] = int(outp)
    raw = ce.get("provider") if isinstance(ce.get("provider"), str) \
        else parent_provider
    cost = ce.get("cost_sum") if harness == "pi" else None
    if cost is None and isinstance(mu.get("cost"), (int, float)):
        cost = mu["cost"]
    if cost is not None:
        ce["cost_sum"] = float(cost)         # _provenance reads cost_sum
    out.update(_provenance(harness, ce, home))
    cw, cws = _context_window(harness, raw, ce.get("model"), home)
    if cw:
        out["context_window"] = int(cw)
        out["context_window_source"] = cws
    seen = _child_last(ce)
    oc = ce.get("open_calls") or {}
    tool_base = None
    if oc and seen is not None and (now - seen) <= OPEN_TOOL_TTL_S:
        _cid, rec = max(oc.items(), key=lambda kv: kv[1][1] or 0)
        nm = rec[0]
        tool_base = rec[2] if len(rec) > 2 else None
        if nm:
            out["tool"] = nm
            if rec[1]:
                out["tool_started_at"] = int(rec[1])
        else:
            tool_base = None
    phrase = _activity(status, out.get("tool"), tool_base)
    if phrase:
        out["activity"] = phrase
    if status in ("done", "failed", "cancelled") and ce.get("last_seen_at"):
        out["ended_at"] = int(ce["last_seen_at"])
    if ce.get("async"):
        out["async"] = True
    return out


def _scan_children(harness, parent_path, entry, home, deadline, cands):
    """Scan candidate children incrementally into the parent's cache entry.
    At most CHILD_CAP newest-by-mtime children are kept; a child past the
    deadline is skipped whole (its offset persists — it resumes next call)."""
    kids = entry.setdefault("children", {})
    rows = []
    for p, mt, run_id, is_async, cid, cname, cts in cands:
        if not _child_ok(p, home):
            continue
        rp = os.path.realpath(p)
        if rp == parent_path:
            continue
        rows.append((rp, mt, run_id, is_async, cid, cname, cts))
    rows.sort(key=lambda r: r[1], reverse=True)
    rows = rows[:CHILD_CAP]
    keep = set()
    for rp, mt, run_id, is_async, cid, cname, cts in rows:
        keep.add(rp)
        ce = kids.get(rp)
        if not isinstance(ce, dict) or not isinstance(ce.get("open_calls"),
                                                      dict):
            ce = kids[rp] = _fresh_child(0, mt)
        if is_async:
            ce["async"] = True
        if run_id and not ce.get("run_id"):
            ce["run_id"] = run_id
        if cid and not ce.get("session_id"):
            ce["session_id"] = cid
        if cname and not ce.get("name"):
            ce["name"] = cname
        if cts and ce.get("started_at") is None:
            ce["started_at"] = cts
        if _mono() <= deadline:
            _scan(rp, ce, harness, deadline, deadline, mask=0xFF)
        meta = ce.get("meta")
        if not isinstance(meta, dict) or meta.get("exitCode") is None:
            proj, _mt = _child_meta(rp, run_id)
            if proj is not None:
                ce["meta"] = proj
    # bound the per-parent child map to the children we kept
    for stale in [p for p in kids if p not in keep]:
        del kids[stale]


# ---------- entry point ----------
def for_agents(agents, budget=BUDGET_S, cache_path=None, home=None, now=None):
    """Telemetry wire dicts aligned with `agents` (raw herdr agent dicts);
    None where nothing could be derived. Never raises. Children (subagents)
    get at most CHILD_BUDGET_FRAC of the budget and resume across calls."""
    if not agents:
        return []
    home = home or HOME
    cache_path = cache_path or CACHE_FILE
    now = _time() if now is None else now
    start = _mono()
    deadline = start + budget
    hard = deadline + CATCHUP_GRACE_S
    kids_deadline = start + budget * CHILD_BUDGET_FRAC
    cache = _load_cache(cache_path) if os.path.exists(cache_path) else {}
    out, by_path, touched, keep = [], {}, False, set()
    for a in agents:
        res = None
        try:
            harness, path = _spec(a, home)
            if path is not None and harness in _HANDLERS \
                    and _mono() <= deadline:
                res = by_path.get(path)
                if res is None:
                    try:
                        st = os.stat(path)
                    except OSError:
                        st = None
                    if st is None:
                        cache.pop(path, None)
                        touched = True
                    else:
                        entry = cache.get(path)
                        if not isinstance(entry, dict) \
                                or not isinstance(entry.get("offset"), int) \
                                or not isinstance(entry.get("open_calls"), dict) \
                                or not isinstance(entry.get("trail"), str):
                            entry = _fresh_parent(st.st_size, st.st_mtime)
                        entry.setdefault("children", {})
                        entry.setdefault("child_headers", {})
                        _scan(path, entry, harness, deadline, hard)
                        if harness in ("pi", "claude"):
                            try:
                                if harness == "pi":
                                    cands = _pi_child_candidates(path, entry,
                                                                 home)
                                else:
                                    cands = _cl_child_candidates(path, home)
                                _scan_children(harness, path, entry, home,
                                               min(kids_deadline, deadline),
                                               cands)
                            except Exception:
                                pass            # children never fail the parent
                        res = _emit(harness, entry,
                                    str(a.get("agent_status") or ""), now,
                                    home)
                        by_path[path] = res
                        cache[path] = entry
                        touched = True
                        keep.add(path)
        except Exception:
            res = None                        # telemetry must never fail the map
        out.append(res)
    if touched:
        # drop entries for files that vanished, then cap the cache size
        cache = {p: e for p, e in cache.items()
                 if p in keep or os.path.exists(p)}
        while len(cache) > MAX_CACHE_ENTRIES:
            cache.pop(next(iter(cache)))
        _save_cache(cache_path, cache)
    return out
