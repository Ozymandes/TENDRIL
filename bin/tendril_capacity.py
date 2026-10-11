#!/usr/bin/env python3
"""tendril_capacity — provider capacity section for the TENDRIL Link snapshot.

Derives, READ-ONLY, per-provider rate-limit state and token usage from the
harnesses' own local transcripts (Claude Code JSONL, Codex rollouts, pi
sessions) plus — only when the operator opts in — three network usage
endpoints. Output shape is contract §1 `capacity.providers[]`; state
thresholds are exactly: ready < 70 · pressure 70–89 · critical 90–97 ·
exhausted ≥ 98 or an active 429 with resets_at in the future · unknown when
no trusted window sample is within the adapter's staleness bound (local
transcripts 6 h, network 30 min).

Token accounting — billable-equivalent tokens per provider; CACHE READS
ARE NEVER COUNTED (counting them inflates a heavy day by orders of
magnitude — the 67M-tokens-a-day smoke bug):
- claude (JSONL message.usage): input_tokens + output_tokens +
  cache_creation_input_tokens — cache_read_input_tokens EXCLUDED;
- codex (rollout token_count): input_tokens - cached_input_tokens +
  output_tokens (cumulative totals are differenced field-wise);
- pi (message.usage): input + output + cacheWrite — cacheRead EXCLUDED;
  reasoning is part of output where reported separately, never added.
A usage object that lacks the fields its definition needs is skipped,
never approximated.

Privacy contract (structural, like tendril_telemetry):
- transcript CONTENT is never retained: no prompts, no commands, no paths in
  output — only token counts, model ids, day dates and window numbers;
- network adapters read ONLY the one credential subtree they need, keep it in
  memory for the single GET, and never write it anywhere; failures are stored
  as a classification word ("auth"/"rate"/"http"/"net"/"shape"/
  "nocred"), never as an
  exception text, and no response body is ever read on error;
- nothing secret is ever cached: the cache holds paths, numbers, dates and
  model names — nothing else.

Never fabricate: a field that cannot be derived reliably is omitted (the key
is absent). Providers with no data on this host do not appear at all.

Performance: transcripts are scanned INCREMENTALLY — a cache
(~/.cache/tendril/capacity.json, 0600, atomic) stores per-file accumulators
and a byte offset, so each call reads only appended bytes. A wall-clock
budget covers all files per call; files past it advance on the next call.
The last assembled section is cached too, so `snapshot_section()` serves it
instantly while refresh work continues in the background of later calls.

Network adapters (`claude_oauth`, `codex_wham`, `zai_quota`) are default OFF;
`tendril capacity enable <id>` opts in (~/.config/tendril/capacity.json).
GET only, Bearer only, 10 s timeout, single attempt, response schema-
projected to numbers before storage, min interval 300 s, 2× backoff on
failures, never retried after 401 until the credential file or the enabled
config changes.

Stdlib only; every failure mode degrades to "no data" — never an error.
"""
import glob
import json
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------- tunables (module-level so tests can patch) ----------
HOME = os.path.realpath(os.path.expanduser("~"))
CACHE_DIR = os.path.expanduser("~/.cache/tendril")
CACHE_FILE = os.path.join(CACHE_DIR, "capacity.json")
CONFIG_DIR = os.path.expanduser("~/.config/tendril")
CONFIG_FILE = os.path.join(CONFIG_DIR, "capacity.json")
BUDGET_S = 1.0              # snapshot_section default wall-clock budget
REFRESH_BUDGET_S = 30.0     # CLI --refresh: generous, cold backfills fit
CATCHUP_GRACE_S = 8.5       # extra allowance for one cold full backfill
LOCAL_MIN_S = 60            # min interval between local transcript scans
NET_MIN_S = 300             # min interval between network checks
NET_BUDGET_MIN_S = 11.0     # budget must cover a 10 s fetch to allow network
NET_TIMEOUT_S = 10          # per-request hard timeout
BACKOFF_BASE_S = NET_MIN_S  # first failure waits one min interval
BACKOFF_CAP_S = 6 * 3600    # ... doubling, capped
LOCAL_STALE_S = 6 * 3600    # local window sample older than this: unknown
NET_STALE_S = 1800          # network window sample older than this: unknown
DAY_KEEP = 8                # day buckets kept (7 emitted, host local dates)
MAX_CACHE_ENTRIES = 1024
MAX_FILES = 4096
HUGE_LINE = 256 * 1024      # above this: regex windows instead of json.loads
HEAD_BYTES = 512
WINDOW_BYTES = 4096
MAX_READ_BODY = 1024 * 1024
MAX_CRED_BYTES = 256 * 1024

PRESSURE_PCT, CRITICAL_PCT, EXHAUSTED_PCT = 70.0, 90.0, 98.0

# bumped whenever token accounting or window semantics change (the cache
# holds per-file accumulators; a definition change must not be mixed with
# numbers accumulated under the old definition)
ACC_VERSION = 3               # 3: §7.1 route attribution in pi scans

# fixed provider ids (contract §1) + the pi providers seen on this host that
# are not zai become their own provider ids (tokens only, state unknown
# unless windows are known).
FIXED_PROVIDERS = {"claude": "Claude", "codex": "Codex", "zai": "Z.AI"}

# opt-in network adapters: id -> (endpoint, credential file, key path,
# extra headers, parser). Default OFF; enabled via CONFIG_FILE.
CLAUDE_OAUTH_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_WHAM_URL = "https://chatgpt.com/backend-api/wham/usage"
ZAI_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
NET_IDS = ("claude_oauth", "codex_wham", "zai_quota")
_NET_SPECS = {
    "claude_oauth": {
        "cred": ".claude/.credentials.json",
        "path": ("claudeAiOauth", "accessToken"),
        "plan": ("claudeAiOauth", "subscriptionType"),
        "headers": {"anthropic-beta": "oauth-2025-04-20"},
    },
    "codex_wham": {
        "cred": ".codex/auth.json",
        "path": ("tokens", "access_token"),
        "headers": {},
    },
    "zai_quota": {
        "cred": ".pi/agent/auth.json",
        "path": ("zai", "key"),
        "headers": {},
    },
}


def _time():
    return time.time()


def _mono():
    return time.monotonic()


def _num(v):
    """int/float (bool excluded) else None."""
    return v if type(v) in (int, float) else None


def _iso_to_epoch(s):
    """ISO-8601 → epoch seconds; naive strings are treated as UTC. Accepts
    the bytes that the bounded regex windows return."""
    if isinstance(s, (bytes, bytearray)):
        s = bytes(s).decode("ascii", "replace")
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


def _any_ts(v):
    """Epoch seconds from an epoch-s / epoch-ms / ISO-8601 value."""
    n = _num(v)
    if n is not None:
        n = float(n)
        if n >= 1e12:
            n /= 1000.0                       # milliseconds
        return n if 1e8 < n < 4e9 else None
    return _iso_to_epoch(v)


def _resets_at(v, base):
    """resets_at as epoch seconds: epoch s/ms, ISO-8601, or seconds-from-now."""
    n = _num(v)
    if n is None:
        return _iso_to_epoch(v) if isinstance(v, str) else None
    n = float(n)
    if n >= 1e12:
        n /= 1000.0
    if 1e8 < n < 4e9:
        return int(n)
    if 0 <= n <= 60 * 86400:
        return int(base + n)                  # resets_in_seconds form
    return None


def _pct(v):
    """Utilization number → percent (reference semantics: a float in (0,1]
    that is not integral is a fraction; integers stay as-is)."""
    n = _num(v)
    if n is None or not (n >= 0) or n > 100:
        return None
    if n <= 1:
        return float(n) if float(n).is_integer() else round(n * 100.0, 1)
    return round(float(n), 1)


def _local_date(ts):
    """Host local date (contract: tokens_by_day uses host local dates)."""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _sanitize_id(name):
    """Provider id for the wire: [a-z0-9._-], ≤ 32, never empty."""
    out = re.sub(r"[^a-z0-9._-]+", "-", str(name).lower()).strip("-")[:32]
    return out.strip("._-") or None


def _sanitize_token(name, cap=120):
    """Short wire-safe token (model ids, plan names)."""
    if not isinstance(name, str):
        return None
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:cap].strip("-.")
    return out or None


# ---------- per-day accumulators ----------
def _day_add(days, ts, tokens, cost=None):
    """Bucket tokens (and pi cost) under the host-local date of ts."""
    if ts is None or not tokens:
        return
    date = _local_date(ts)
    b = days.setdefault(date, {"tok": 0})
    b["tok"] += int(tokens)
    if cost is not None:
        b["cost"] = b.get("cost", 0.0) + float(cost)


def _model_add(mday, model, ts, tokens):
    if not model or not tokens or ts is None:
        return
    per = mday.setdefault(model, {})
    date = _local_date(ts)
    per[date] = per.get(date, 0) + int(tokens)
    if len(per) > DAY_KEEP + 2:               # bounded even if never pruned
        for old in sorted(per)[:-DAY_KEEP]:
            per.pop(old, None)


def _prune_days(days, today):
    for date in [d for d in days if d > today]:
        days.pop(date, None)


# ---------- claude transcript (projects/*/<uuid>.jsonl) ----------
_RE_CL_IN = re.compile(rb'"input_tokens":(\d+)')
_RE_CL_OUT = re.compile(rb'"output_tokens":(\d+)')
_RE_CL_CW = re.compile(rb'"cache_creation_input_tokens":(\d+)')
_RE_CL_MODEL = re.compile(rb'"model":"([^"\\]{1,120})"')
_RE_CL_RESETSAT = re.compile(rb'"resetsAt":(\d{9,13})')
_RE_CL_RLTYPE = re.compile(rb'"rateLimitType":"([a-z0-9_]{1,40})"')
_RE_CL_STATUS = re.compile(rb'"apiErrorStatus":(\d{3})')

_RL_KIND = {"five_hour": "session", "seven_day": "weekly",
            "seven_day_opus": "weekly", "seven_day_opus_1x": "weekly"}


def _cl_usage_numbers(u):
    """claude billable tokens: input + output + cache_creation — cache
    reads are EXCLUDED (module docstring)."""
    vals = [_num(u.get("input_tokens")), _num(u.get("output_tokens")),
            _num(u.get("cache_creation_input_tokens"))]
    if all(v is None for v in vals):
        return None
    return sum(int(v or 0) for v in vals)


def _cl_note_429(d, entry, ts, now):
    """apiErrorStatus 429 (or a rate_limit error) + quotaLimits → the
    active-rate-limit record. Type-agnostic: error events need not be
    assistant lines. quotaLimits sits under message or at top level.
    Keep the newest one only."""
    err = d.get("error")
    status = _num(d.get("apiErrorStatus"))
    if status != 429 and not (err == "rate_limit"
                              or (isinstance(err, str)
                                  and "rate_limit" in err)):
        return
    if ts is None:
        ts = _iso_to_epoch(d.get("timestamp"))
    q = d.get("quotaLimits")
    if not isinstance(q, dict):
        m = d.get("message")
        q = m.get("quotaLimits") if isinstance(m, dict) else None
    if not isinstance(q, dict):
        return
    resets = _any_ts(q.get("resetsAt"))
    if resets is None:
        return
    kind = _RL_KIND.get(q.get("rateLimitType"))
    seen = ts if ts is not None else now
    cur = entry.get("r429")
    if cur is None or (seen or 0) >= (cur.get("observed_at") or 0):
        entry["r429"] = {"resets_at": int(resets), "kind": kind,
                         "observed_at": seen}


def _cl_obj(d, entry, now):
    if d.get("type") != "assistant":
        return
    ts = _iso_to_epoch(d.get("timestamp"))
    m = d.get("message")
    if not isinstance(m, dict):
        m = {}
    u = m.get("usage")
    if isinstance(u, dict):
        tokens = _cl_usage_numbers(u)
        if tokens:
            entry["last_ts"] = max(entry.get("last_ts") or 0, ts or 0)
            _day_add(entry["days"], ts, tokens)
            _model_add(entry["mday"], m.get("model"), ts, tokens)


def _cl_huge(raw, entry, now):
    """Over-long (image-bearing) line: bounded regex windows only."""
    ts = _iso_to_epoch(_rx_win(raw, _RE_ISO_TS, HEAD_BYTES))
    if b'"usage"' in raw:
        win = _at_win(raw, b'"usage"')
        vals = [int(m.group(1)) if m else 0 for m in (
            _RE_CL_OUT.search(win), _RE_CL_IN.search(win),
            _RE_CL_CW.search(win))]          # cache reads excluded
        tokens = sum(vals)
        if tokens:
            entry["last_ts"] = max(entry.get("last_ts") or 0, ts or 0)
            _day_add(entry["days"], ts, tokens)
            mm = _RE_CL_MODEL.search(raw[:HEAD_BYTES])
            _model_add(entry["mday"],
                       mm.group(1).decode("utf-8", "replace") if mm else None,
                       ts, tokens)
    if b'"quotaLimits"' in raw:
        win = _at_win(raw, b'"quotaLimits"')
        resets = _RE_CL_RESETSAT.search(win)
        status = _RE_CL_STATUS.search(raw[:HEAD_BYTES]) \
            or _RE_CL_STATUS.search(raw[-HEAD_BYTES:])
        rltype = _RE_CL_RLTYPE.search(win)
        if resets and status and int(status.group(1)) == 429:
            r = _any_ts(int(resets.group(1)))     # s or ms → epoch seconds
            if r is not None:
                seen = ts if ts is not None else now
                cur = entry.get("r429")
                if cur is None or (seen or 0) >= (cur.get("observed_at") or 0):
                    entry["r429"] = {
                        "resets_at": int(r),
                        "kind": _RL_KIND.get(
                            rltype.group(1).decode("utf-8", "replace")
                        ) if rltype else None,
                        "observed_at": seen}


def _cl_line(raw, entry, now):
    if len(raw) > HUGE_LINE:
        _cl_huge(raw, entry, now)
        return
    if not (b'"type":"assistant"' in raw or b'"type": "assistant"' in raw
            or b'"quotaLimits"' in raw or b'"apiErrorStatus"' in raw):
        return
    try:
        d = json.loads(raw)
    except Exception:
        return
    if isinstance(d, dict):
        _cl_obj(d, entry, now)            # tokens: assistant lines only
        _cl_note_429(d, entry, None, now)  # 429: any line shape


# ---------- codex rollout (sessions/YYYY/MM/DD/rollout-*.jsonl) ----------
_RE_CX_IN = re.compile(rb'"input_tokens":(\d{1,12})')
_RE_CX_CACHED = re.compile(rb'"cached_input_tokens":(\d{1,12})')
_RE_CX_OUT = re.compile(rb'"output_tokens":(\d{1,12})')
_RE_CX_USED = re.compile(rb'"used_percent":(\d{1,3}(?:\.\d+)?)')
_RE_CX_RESET = re.compile(rb'"resets_at":("?[^,"}]{1,40}"?)')
_RE_CX_RESET_IN = re.compile(rb'"resets_in_seconds":(\d{1,9})')
_RE_CX_WMIN = re.compile(rb'"window_minutes":(\d{1,6})')
_RE_CX_PLAN = re.compile(rb'"plan_type":"([A-Za-z0-9._-]{1,32})"')


def _cx_turn(u):
    """codex billable tokens for one API call: input − cached + output
    (cached input is a cache READ — excluded, module docstring)."""
    i = _num(u.get("input_tokens"))
    c = _num(u.get("cached_input_tokens"))
    o = _num(u.get("output_tokens"))
    if i is None and o is None:
        return None
    return max(0, int(i or 0) - int(c or 0)) + int(o or 0)


def _cx_window(rec, now, ts, entry):
    """rate_limits.primary/secondary → trusted window sample (newest wins)."""
    if not isinstance(rec, dict):
        return
    out = {}
    for slot, kind in (("primary", "session"), ("secondary", "weekly")):
        w = rec.get(slot)
        if not isinstance(w, dict):
            continue                          # null secondary is normal
        used = _pct(w.get("used_percent"))
        if used is None:
            used = _pct(w.get("utilization"))
        resets = _resets_at(w.get("resets_at"), ts if ts is not None else now)
        if resets is None:
            rin = _num(w.get("resets_in_seconds"))
            if rin is not None and 0 <= rin <= 60 * 86400:
                resets = int((ts if ts is not None else now) + rin)
        wmin = _num(w.get("window_minutes"))
        entry_w = {"kind": kind}
        if used is not None:
            entry_w["used_pct"] = used
        if resets is not None:
            entry_w["resets_at"] = int(resets)
        if wmin is not None and 0 < wmin <= 60 * 24 * 31:
            entry_w["window_min"] = int(wmin)
        out[kind] = entry_w
    if not out:
        return
    seen = ts if ts is not None else now
    cur = entry.get("limit") if isinstance(entry.get("limit"), dict) else None
    if cur is None or seen >= (cur.get("observed_at") or 0):
        plan = _sanitize_token(rec.get("plan_type"), 16)
        if plan:
            plan = plan.lower()
        entry["limit"] = {"windows": out, "observed_at": seen, "plan": plan}


def _cx_obj(d, entry, now):
    if d.get("type") != "event_msg":
        return
    p = d.get("payload")
    if not isinstance(p, dict) or p.get("type") != "token_count":
        return
    ts = _iso_to_epoch(d.get("timestamp"))
    info = p.get("info")
    if isinstance(info, dict):
        tokens = None
        last = info.get("last_token_usage")
        total = info.get("total_token_usage")
        if isinstance(last, dict):
            tokens = _cx_turn(last)       # this event's call: bucket directly
        elif isinstance(total, dict):
            cur = {"in": int(_num(total.get("input_tokens")) or 0),
                   "cached": int(_num(total.get("cached_input_tokens")) or 0),
                   "out": int(_num(total.get("output_tokens")) or 0)}
            prev = entry.get("prev") if isinstance(entry.get("prev"), dict) \
                else None
            if prev is not None:          # field-wise delta of the totals
                d_in = cur["in"] - prev["in"]
                d_c = cur["cached"] - prev["cached"]
                d_out = cur["out"] - prev["out"]
                if 0 <= d_in < 10 ** 9 and 0 <= d_c <= d_in \
                        and 0 <= d_out < 10 ** 9:
                    tokens = d_in - d_c + d_out
            entry["prev"] = cur           # first sight only seeds the delta
        if tokens:
            entry["last_ts"] = max(entry.get("last_ts") or 0, ts or 0)
            _day_add(entry["days"], ts, tokens)
    _cx_window(p.get("rate_limits"), now, ts, entry)


def _cx_huge(raw, entry, now):
    ts = _iso_to_epoch(_rx_win(raw, _RE_ISO_TS, HEAD_BYTES))
    if b'"last_token_usage"' in raw:
        win = _at_win(raw, b'"last_token_usage"')
        mi, mc, mo = (_RE_CX_IN.search(win), _RE_CX_CACHED.search(win),
                      _RE_CX_OUT.search(win))
        if mi is not None or mo is not None:
            tokens = max(0, int(mi.group(1)) - int(mc.group(1) if mc else 0)) \
                + int(mo.group(1) if mo else 0)
            if tokens:
                entry["last_ts"] = max(entry.get("last_ts") or 0, ts or 0)
                _day_add(entry["days"], ts, tokens)
    if b'"rate_limits"' in raw:
        at = raw.find(b'"rate_limits"')
        win = raw[at:at + 2 * WINDOW_BYTES]
        used = _RE_CX_USED.search(win)
        reset = _RE_CX_RESET.search(win) or _RE_CX_RESET_IN.search(win)
        wmin = _RE_CX_WMIN.search(win)
        if used:
            seen = ts if ts is not None else now
            resets = None
            if reset:
                val = reset.group(1).strip(b'"')
                if b"resets_in_seconds" in reset.re.pattern.encode():
                    resets = int((seen or now) + int(val)) if val.isdigit() \
                        else None
                else:
                    resets = _resets_at(
                        val.decode("utf-8", "replace"), seen or now)
            w = {"kind": "session", "used_pct": float(used.group(1))}
            if resets:
                w["resets_at"] = int(resets)
            if wmin:
                w["window_min"] = int(wmin.group(1))
            cur = entry.get("limit") \
                if isinstance(entry.get("limit"), dict) else None
            if cur is None or seen >= (cur.get("observed_at") or 0):
                pm = _RE_CX_PLAN.search(win)
                plan = pm.group(1).decode("utf-8", "replace").lower() if pm \
                    else None
                entry["limit"] = {"windows": {"session": w},
                                  "observed_at": seen, "plan": plan}


def _cx_line(raw, entry, now):
    if len(raw) > HUGE_LINE:
        _cx_huge(raw, entry, now)
        return
    if b'"token_count"' in raw:
        try:
            d = json.loads(raw)
        except Exception:
            return
        if isinstance(d, dict):
            _cx_obj(d, entry, now)


# ---------- pi session (sessions/**/*.jsonl) ----------
_RE_PI_IN = re.compile(rb'"input":(\d+)')
_RE_PI_OUT = re.compile(rb'"output":(\d+)')
_RE_PI_CW = re.compile(rb'"cacheWrite":(\d+)')
_RE_PI_PROV = re.compile(rb'"provider":"([^"\\]{1,64})"')
_RE_PI_MODEL = re.compile(rb'"model":"([^"\\]{1,120})"')
_RE_PI_MS_TS = re.compile(rb'"timestamp":(\d{10,16})')
_RE_ISO_TS = re.compile(rb'"timestamp":"([0-9T:.+\-Z]{10,40})"')


_PI_PID_OF = {
    # §7.1: a bridge/route name is NEVER a provider. Pi-local usage is
    # attributed to the upstream provider's capacity entry, so its
    # adapters (claude_oauth, codex_wham, zai_quota) answer for it. A
    # gateway route with no upstream of its own (optollm) maps to None:
    # those tokens are attributed nowhere rather than becoming a fake
    # provider with reason feature_missing.
    "claude-bridge": "claude",
    "openai-codex": "codex",
    "optollm": None,
}


def _capacity_pid(pid):
    """Canonical capacity provider id for a pi transcript provider token
    (already sanitized). Bridges/routes fold into the upstream provider;
    unknown tokens stay themselves (zai, google, …)."""
    return _PI_PID_OF.get(pid, pid)


def _pi_prov_new(entry, pid):
    prov = entry["prov"]
    agg = prov.get(pid)
    if not isinstance(agg, dict) or not isinstance(agg.get("days"), dict) \
            or not isinstance(agg.get("mday"), dict):
        agg = {"days": {}, "mday": {}, "cost_known": False}
        prov[pid] = agg
    return agg


def _pi_line(raw, entry, now):
    if len(raw) > HUGE_LINE:
        ts_ms = _RE_PI_MS_TS.search(raw[:HEAD_BYTES])
        ts = float(ts_ms.group(1)) / 1000.0 if ts_ms else None
        if b'"usage"' in raw:
            at = raw.find(b'"usage"')
            win = raw[at:at + WINDOW_BYTES]
            nums = [_RE_PI_IN.search(win), _RE_PI_OUT.search(win),
                    _RE_PI_CW.search(win)]   # input + output + cacheWrite
            total = sum(int(m.group(1)) for m in nums if m) \
                if any(nums) else 0
            if total:
                pm = _RE_PI_PROV.search(raw[:HEAD_BYTES])
                mm = _RE_PI_MODEL.search(raw[:HEAD_BYTES])
                pid = _capacity_pid(_sanitize_id(
                    pm.group(1).decode("utf-8", "replace"))) if pm else None
                if pid:
                    agg = _pi_prov_new(entry, pid)
                    entry["last_ts"] = max(entry.get("last_ts") or 0,
                                           ts or 0)
                    _day_add(agg["days"], ts, total)
                    _model_add(agg["mday"],
                               mm.group(1).decode("utf-8", "replace") if mm
                               else None, ts, total)
        return
    if b'"usage"' not in raw:
        return
    try:
        d = json.loads(raw)
    except Exception:
        return
    if not isinstance(d, dict) or d.get("type") != "message":
        return
    m = d.get("message")
    if not isinstance(m, dict) or m.get("role") != "assistant":
        return
    u = m.get("usage")
    if not isinstance(u, dict):
        return
    pid = _sanitize_id(m.get("provider")) if isinstance(m.get("provider"),
                                                        str) else None
    if pid is None:
        return
    pid = _capacity_pid(pid)
    if pid is None:
        return                              # route without a provider: skip
    ts = _any_ts(m.get("timestamp")) or _any_ts(d.get("timestamp"))
    parts = [_num(u.get("input")), _num(u.get("output")),
             _num(u.get("cacheWrite"))]     # input + output + cacheWrite
    if all(v is None for v in parts):
        return                              # not derivable: skip, never guess
    total = int(parts[0] or 0) + int(parts[1] or 0) + int(parts[2] or 0)
    c = u.get("cost")
    cost = _num(c.get("total")) if isinstance(c, dict) else None
    agg = _pi_prov_new(entry, pid)
    if cost is not None:
        agg["cost_known"] = True
    if total > 0:
        entry["last_ts"] = max(entry.get("last_ts") or 0, ts or 0)
        _day_add(agg["days"], ts, total, cost)
        _model_add(agg["mday"], m.get("model"), ts, total)


def _rx_win(raw, rx, cap):
    m = rx.search(raw[:cap])
    return m.group(1) if m else None


def _at_win(raw, marker):
    at = raw.find(marker)
    return raw[at:at + WINDOW_BYTES]


_HANDLERS = {"claude": _cl_line, "codex": _cx_line, "pi": _pi_line}


# ---------- incremental scan driver ----------
def _fresh_entry(kind):
    base = {"kind": kind, "size": 0, "mtime": 0.0, "offset": 0,
            "last_ts": 0, "days": {}, "mday": {}}
    if kind == "claude":
        base["r429"] = None
    elif kind == "codex":
        base["limit"] = None
        base["prev"] = None
    elif kind == "pi":
        base["prov"] = {}
    return base


def _valid_entry(entry, kind):
    if not isinstance(entry, dict) or entry.get("kind") != kind:
        return False
    if not isinstance(entry.get("offset"), int) \
            or not isinstance(entry.get("days"), dict):
        return False
    if kind == "pi" and not isinstance(entry.get("prov"), dict):
        return False
    return True


def _scan(path, entry, kind, deadline, hard_deadline, now):
    """Read only bytes past entry["offset"]; update accumulators in place.
    Truncation/rotation resets the entry. A partial final line is left for
    the next call; progress is persisted even when the budget ends mid-file."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    size, mtime = st.st_size, st.st_mtime
    if size < entry.get("size", 0) or entry["offset"] > size:
        entry.clear()
        entry.update(_fresh_entry(kind))
    offset = entry["offset"]
    entry["size"], entry["mtime"] = size, mtime
    if offset == size:
        return False
    handle = _HANDLERS.get(kind)
    if handle is None:
        return False
    limit = hard_deadline if offset == 0 else deadline
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
                    handle(raw, entry, now)
                except Exception:
                    pass                     # one bad line never kills a scan
                pos += len(raw)
                if (seen & 0xFF) == 0 and _mono() > limit:
                    break                    # budget out; offset keeps progress
                    # (checked every 256 lines: one big JSONL line can cost
                    # ~1 ms, so a sparser stride overshoots the budget)
    except OSError:
        return False
    entry["offset"] = pos
    return True


def _discover(home):
    """(realpath, kind) for every transcript worth scanning, hottest first."""
    pats = (
        (os.path.join(home, ".claude", "projects", "*", "*.jsonl"), "claude"),
        (os.path.join(home, ".codex", "sessions", "**", "rollout-*.jsonl"),
         "codex"),
        (os.path.join(home, ".pi", "agent", "sessions", "**", "*.jsonl"),
         "pi"),
    )
    out = []
    for pat, kind in pats:
        try:
            hits = glob.glob(pat, recursive=True)
        except Exception:
            hits = []
        for p in hits:
            rp = os.path.realpath(p)
            try:
                st = os.stat(rp)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                out.append((st.st_mtime, rp, kind))
    out.sort(reverse=True)
    seen = set()
    files = []
    for mtime, rp, kind in out:
        if rp in seen:
            continue
        seen.add(rp)
        files.append((rp, kind))
        if len(files) >= MAX_FILES:
            break
    return files


def _scan_all(cache, deadline, hard_deadline, now, home):
    touched = False
    entries = cache.setdefault("files", {})
    for rp, kind in _discover(home):
        entry = entries.get(rp)
        if not _valid_entry(entry, kind):
            entry = _fresh_entry(kind)
        if _mono() > deadline:
            entries[rp] = entry               # keep its accumulators anyway
            continue
        try:
            _scan(rp, entry, kind, deadline, hard_deadline, now)
        except Exception:
            continue
        entries[rp] = entry
        touched = True
    while len(entries) > MAX_CACHE_ENTRIES:
        entries.pop(next(iter(entries)))
    return touched


# ---------- network adapters (opt-in; tolerant parse; never raises) ----------
def _read_credential(home, spec):
    """The ONE subtree needed: (token, plan). Everything else in the file is
    dropped immediately; nothing is ever logged or cached."""
    path = os.path.join(home, spec["cred"])
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_CRED_BYTES)
    except OSError:
        return None, None, None
    try:
        d = json.loads(raw)
    except Exception:
        return None, None, None
    mtime = None
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        pass
    node = d
    for key in spec["path"]:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            break
    token = node if isinstance(node, str) and 8 <= len(node) <= 8192 else None
    plan = None
    if spec.get("plan"):
        p = d
        for key in spec["plan"]:
            p = p.get(key) if isinstance(p, dict) else None
            if p is None:
                break
        if isinstance(p, str) and 0 < len(p) <= 32:
            plan = re.sub(r"[^a-z0-9._-]+", "", p.lower())[:16] or None
    return token, plan, mtime


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would re-send the Authorization
    header to the redirect target. Returning None makes the opener raise
    HTTPError for the 3xx itself — no second request is ever made."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http_get_json(url, token, extra_headers, timeout=NET_TIMEOUT_S):
    """One GET, Bearer only. Returns (kind, data): kind is "ok", "auth"
    (401), "rate" (429), "http", "net" or "shape". NEVER raises; error
    bodies are never read and never stored — no secret can leak.
    Redirects are refused ("net"): the opener never re-issues the request,
    so the Bearer header can never reach a redirect target."""
    headers = {"Authorization": "Bearer " + token,
               "User-Agent": "tendril-capacity/0.2"}
    headers.update(extra_headers or {})
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            if resp.geturl() != url:            # rewritten underneath us
                return "net", None
            body = resp.read(MAX_READ_BODY)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "auth", None
        if e.code == 429:
            return "rate", None
        if e.code in (301, 302, 303, 307, 308):
            return "net", None                  # redirect refused
        return "http", None
    except Exception:
        return "net", None
    try:
        data = json.loads(body)
    except Exception:
        return "shape", None
    return "ok", data


def _limit_from_dict(d, now):
    """One limit object → {used_pct, resets_at} (tolerant key names)."""
    used = _pct(d.get("utilization")) if isinstance(d, dict) else None
    if used is None:
        used = _pct(d.get("used_percent")) if isinstance(d, dict) else None
    if used is None:
        used = _pct(d.get("percentage")) if isinstance(d, dict) else None
    if used is None and isinstance(d, dict):
        cur, rem = _num(d.get("currentValue")), _num(d.get("remaining"))
        if cur is not None and rem is not None and cur + rem > 0:
            used = round(cur / (cur + rem) * 100.0, 1)
    resets = _resets_at(d.get("resets_at"), now) if isinstance(d, dict) \
        else None
    if used is None and resets is None:
        return None
    out = {}
    if used is not None:
        out["used_pct"] = used
    if resets is not None:
        out["resets_at"] = int(resets)
    return out


_SESSION_TYPES = ("TIME_LIMIT", "SESSION_LIMIT", "REQUEST_LIMIT", "RPM_LIMIT",
                  "RPD_LIMIT")
_WEEKLY_TYPES = ("WEEK_LIMIT", "WEEKLY_LIMIT", "TPM_LIMIT")
_DAILY_TYPES = ("DAILY_LIMIT", "DAY_LIMIT")


def _parse_claude_oauth(data, now):
    """five_hour/seven_day utilization; resets_at ISO or epoch (shape partly
    unverified upstream — every field individually validated)."""
    windows = []
    for key, kind, wmin in (("five_hour", "session", 300),
                            ("seven_day", "weekly", 10080)):
        node = data.get(key) if isinstance(data, dict) else None
        if not isinstance(node, dict):
            continue
        w = _limit_from_dict(node, now)
        if w is None:
            continue
        w["kind"] = kind
        w.setdefault("window_min", wmin)
        windows.append(w)
    return windows


def _parse_codex_wham(data, now):
    rl = data.get("rate_limit") if isinstance(data, dict) else None
    if not isinstance(rl, dict):
        return []
    windows = []
    for key, kind in (("primary_window", "session"),
                      ("secondary_window", "weekly")):
        node = rl.get(key)
        if not isinstance(node, dict):
            continue
        w = _limit_from_dict(node, now)
        if w is None:
            continue
        rin = _num(node.get("reset_after_seconds"))
        if "resets_at" not in w and rin is not None \
                and 0 <= rin <= 60 * 86400:
            w["resets_at"] = int(now + rin)
        w["kind"] = kind
        windows.append(w)
    return windows


def _parse_zai_quota(data, now):
    """limits[] by type (bucket string forms included), or direct
    session/weekly fields — mirrors the reference usage extension."""
    limits = None
    if isinstance(data, dict):
        for cand in (data.get("data"), data.get("quota")):
            if isinstance(cand, dict) and isinstance(cand.get("limits"), list):
                limits = cand["limits"]
                break
        if limits is None and isinstance(data.get("limits"), list):
            limits = data["limits"]
    windows = []
    if isinstance(limits, list):
        by_kind = {}

        def bucket_type(t):
            t = str(t or "").upper()
            if t in _SESSION_TYPES:
                return "session"
            if t in _WEEKLY_TYPES:
                return "weekly"
            if t in _DAILY_TYPES:
                return "daily"
            if "TOKEN" in t:
                return "weekly"
            return None

        for item in limits:
            if not isinstance(item, dict):
                continue
            kind = bucket_type(item.get("type"))
            if kind is None:
                continue
            w = _limit_from_dict(item, now)
            alt = _limit_from_dict({"currentValue": item.get("used"),
                                    "remaining": item.get("remaining")},
                                   now)
            if alt:                            # merge the used/remaining form
                w = dict(w or {})
                if "used_pct" not in w:
                    w.update(alt)
            if not w:
                continue
            w["kind"] = kind
            by_kind.setdefault(kind, w)
        windows = list(by_kind.values())
    if windows:
        return windows
    # direct-shape fallbacks (session/weekly candidates)
    session = weekly = None
    for cand in (data if isinstance(data, dict) else {},
                 data.get("data") if isinstance(data, dict) else None):
        if not isinstance(cand, dict):
            continue
        for key in ("session", "sessionPercent", "session_percent"):
            node = cand.get(key)
            if isinstance(node, dict):
                session = session or _limit_from_dict(node, now)
            else:
                w = _pct(node)
                session = session or ({"used_pct": w} if w is not None
                                      else None)
        fh = cand.get("five_hour")
        if isinstance(fh, dict):
            session = session or _limit_from_dict(fh, now)
        for key in ("weekly", "weeklyPercent", "weekly_percent"):
            node = cand.get(key)
            if isinstance(node, dict):
                weekly = weekly or _limit_from_dict(node, now)
            else:
                w = _pct(node)
                weekly = weekly or ({"used_pct": w} if w is not None
                                    else None)
        sd = cand.get("seven_day")
        if isinstance(sd, dict):
            weekly = weekly or _limit_from_dict(sd, now)
    for w, kind in ((session, "session"), (weekly, "weekly")):
        if w:
            w["kind"] = kind
            windows.append(w)
    return windows


_PARSERS = {"claude_oauth": _parse_claude_oauth,
            "codex_wham": _parse_codex_wham,
            "zai_quota": _parse_zai_quota}


def _fresh_net_state():
    return {"windows": [], "observed_at": 0, "fail": None,
            "next_ok_at": 0, "backoff": BACKOFF_BASE_S, "cred_mtime": None}


def _valid_net(st):
    return isinstance(st, dict) and isinstance(st.get("windows"), list) \
        and (st.get("next_ok_at") is None
             or isinstance(st.get("next_ok_at"), (int, float))) \
        and isinstance(st.get("backoff"), (int, float))


def _net_phase(cache, now, allow_network, home):
    """Attempt each enabled, due network adapter at most once. 401 marks the
    adapter failed until the credential file changes (or it is re-enabled);
    other failures double the backoff, capped."""
    if not allow_network:
        return
    enabled = set(enabled_ids(home))
    states = cache.setdefault("net", {})
    for aid in NET_IDS:
        if aid not in enabled:
            continue
        st = states.get(aid)
        if not _valid_net(st):
            st = _fresh_net_state()
        spec = _NET_SPECS[aid]
        token, plan, mtime = _read_credential(home, spec)
        if token is None:
            st["fail"], st["windows"], st["observed_at"] = "nocred", [], 0
            states[aid] = st
            continue
        if st.get("fail") == "auth" \
                and st.get("cred_mtime") is not None \
                and mtime is not None and mtime == st.get("cred_mtime"):
            states[aid] = st
            continue                          # 401: never retry until it changes
        nxt = st.get("next_ok_at")
        if nxt is not None and now < nxt:
            states[aid] = st
            continue                          # backing off (None = dead 401)
        if st.get("observed_at") \
                and now - st["observed_at"] < NET_MIN_S:
            states[aid] = st
            continue
        st["cred_mtime"] = mtime
        kind, data = _http_get_json(_endpoint(aid), token, spec["headers"])
        if kind == "ok":
            windows = []
            try:
                windows = _PARSERS[aid](data, now) or []
            except Exception:
                windows = []
            if windows:
                st["windows"] = windows
                st["observed_at"] = now
                st["fail"] = None
                st["backoff"] = BACKOFF_BASE_S
                st["next_ok_at"] = 0
                if plan:
                    st["plan"] = plan
            else:
                _net_fail(st, now, "shape")
        else:
            _net_fail(st, now, kind)
        states[aid] = st


def _net_fail(st, now, kind):
    """Record a failure: 401 never retries until the credential file or the
    enabled config changes (next_ok_at None); everything else doubles the
    stored backoff, capped."""
    st["fail"] = kind
    if kind == "auth":
        st["next_ok_at"] = None               # None = wait for a config change
    else:
        st["backoff"] = min(max(BACKOFF_BASE_S,
                                (st.get("backoff") or BACKOFF_BASE_S) * 2),
                            BACKOFF_CAP_S)
        st["next_ok_at"] = now + st["backoff"]


def _endpoint(aid):
    return {"claude_oauth": CLAUDE_OAUTH_URL, "codex_wham": CODEX_WHAM_URL,
            "zai_quota": ZAI_QUOTA_URL}[aid]


# ---------- enabled-adapters config ----------
def enabled_ids(home=None):
    """Validated list from CONFIG_FILE; garbage is dropped, never raised."""
    path = CONFIG_FILE if home is None else os.path.join(
        home, ".config", "tendril", "capacity.json")
    try:
        with open(path, "rb") as f:
            d = json.loads(f.read(65536))
    except Exception:
        return []
    out = []
    if isinstance(d, dict):
        for v in d.get("enabled", []):
            if v in NET_IDS and v not in out:
                out.append(v)
    return out


def set_enabled(adapter_id, on, home=None):
    """`tendril capacity enable|disable <adapter>` — validated, atomic 0600.
    Enabling also clears a recorded 401 so the next check can retry."""
    if adapter_id not in NET_IDS:
        return False
    path = CONFIG_FILE if home is None else os.path.join(
        home, ".config", "tendril", "capacity.json")
    cur = set(enabled_ids(home))
    if on:
        cur.add(adapter_id)
    else:
        cur.discard(adapter_id)
    ordered = [a for a in NET_IDS if a in cur]
    d = os.path.dirname(path)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        body = json.dumps({"enabled": ordered},
                          separators=(",", ":")).encode("utf-8")
        _atomic_write(path, body)
    except OSError:
        return False
    if on:
        try:
            cache = _load_cache(CACHE_FILE if home is None else
                                os.path.join(home, ".cache", "tendril",
                                             "capacity.json"))
            st = cache.get("net", {}).get(adapter_id)
            if _valid_net(st) and st.get("fail") == "auth":
                st["fail"] = None
                st["cred_mtime"] = None
                _save_cache(cache, CACHE_FILE if home is None else
                            os.path.join(home, ".cache", "tendril",
                                         "capacity.json"))
        except Exception:
            pass
    return True


# ---------- assembly ----------
def _window_trusted(w, now):
    if not isinstance(w, dict) or not isinstance(w.get("observed_at"),
                                                (int, float)):
        return False
    bound = NET_STALE_S if w.get("net") else LOCAL_STALE_S
    return now - w["observed_at"] <= bound


def _group_new(pid, harness):
    return {"harness": harness, "days": {}, "mday": {}, "cost_known": False,
            "windows": [], "plan": None, "last_ts": 0}


def _merge_group_days(group, agg):
    for date, b in (agg.get("days") or {}).items():
        if not isinstance(b, dict):
            continue
        g = group["days"].setdefault(date, {"tok": 0})
        g["tok"] += int(b.get("tok") or 0)
        if "cost" in b:
            g["cost"] = g.get("cost", 0.0) + float(b["cost"])
            group["cost_known"] = True
    for model, per in (agg.get("mday") or {}).items():
        gm = group["mday"].setdefault(model, {})
        for date, tok in (per or {}).items():
            gm[date] = gm.get(date, 0) + int(tok or 0)


def _state_for(windows, r429, now):
    """Contract thresholds on the worst trusted window; an active 429 with a
    future resets_at exhausts outright. Returns (state, source_driver)."""
    trusted = [w for w in windows if _window_trusted(w, now)]
    worst = None
    for w in trusted:
        if w.get("used_pct") is not None \
                and (worst is None
                     or w["used_pct"] > worst["used_pct"]):
            worst = w
    exhausted_429 = r429 is not None \
        and (now - (r429.get("observed_at") or 0)) <= LOCAL_STALE_S \
        and (r429.get("resets_at") or 0) > now
    if worst is not None and worst["used_pct"] >= EXHAUSTED_PCT:
        return "exhausted", worst
    if exhausted_429:
        return "exhausted", "r429"
    if worst is None:
        return "unknown", None
    if worst["used_pct"] >= CRITICAL_PCT:
        return "critical", worst
    if worst["used_pct"] >= PRESSURE_PCT:
        return "pressure", worst
    return "ready", worst


def _assemble(cache, now, enabled=None):
    today = _local_date(now)
    groups = {}
    entries = cache.get("files") or {}
    # fixed-kind files (claude/codex rollouts) seed their groups first so
    # the harness of a shared provider id (pi usage folded into it) is
    # deterministic — and so the "claude" group exists even when the
    # ONLY claude data is pi-via-bridge usage.
    for rp, e in sorted(entries.items(),
                        key=lambda kv: kv[1].get("kind") == "pi"
                        if isinstance(kv[1], dict) else False):
        if not isinstance(e, dict):
            continue
        kind = e.get("kind")
        if kind == "pi":
            provs = e.get("prov")
            if not isinstance(provs, dict):
                continue
            for raw_pid, agg in provs.items():
                if not isinstance(agg, dict):
                    continue
                pid = _capacity_pid(raw_pid)
                if pid is None:
                    continue                  # route without a provider
                g = groups.setdefault(pid, _group_new(pid, "pi"))
                _merge_group_days(g, agg)
                g["last_ts"] = max(g["last_ts"], e.get("last_ts") or 0)
        elif kind in ("claude", "codex"):
            pid = kind
            harness = "claude" if kind == "claude" else "codex"
            g = groups.setdefault(pid, _group_new(pid, harness))
            _merge_group_days(g, e)
            g["last_ts"] = max(g["last_ts"], e.get("last_ts") or 0)
            if kind == "codex" and isinstance(e.get("limit"), dict):
                lim = e["limit"]
                for w in (lim.get("windows") or {}).values():
                    if isinstance(w, dict):
                        g["windows"].append(dict(
                            w, observed_at=lim.get("observed_at"),
                            net=False, source="codex_rollout"))
                if lim.get("plan"):
                    g["plan"] = lim.get("plan")
            if kind == "claude" and isinstance(e.get("r429"), dict):
                g["r429"] = dict(e["r429"])
                g.setdefault("r429s", []).append(dict(e["r429"]))
    # newest 429 across files wins (active-rate-limit is provider-wide)
    r429_by_group = {}
    for rp, e in entries.items():
        if isinstance(e, dict) and e.get("kind") == "claude" \
                and isinstance(e.get("r429"), dict):
            cur = r429_by_group.get("claude")
            if cur is None or (e["r429"].get("observed_at") or 0) >= \
                    (cur.get("observed_at") or 0):
                r429_by_group["claude"] = e["r429"]
    # network windows + plans
    net = cache.get("net") or {}
    net_map = {"claude_oauth": ("claude", "claude_oauth"),
               "codex_wham": ("codex", "codex_wham"),
               "zai_quota": ("zai", "zai_quota")}
    for aid, (pid, source) in net_map.items():
        st = net.get(aid)
        if not _valid_net(st) or not st.get("observed_at"):
            continue
        g = groups.setdefault(pid, _group_new(
            pid, "pi" if pid == "zai" else pid))
        for w in st.get("windows") or []:
            if isinstance(w, dict):
                g["windows"].append(dict(
                    w, observed_at=st.get("observed_at"), net=True,
                    source=aid))
        if st.get("plan") and not g["plan"]:
            g["plan"] = st.get("plan")
    # project to wire records
    providers = []
    for pid in sorted(groups, key=lambda p: (p not in FIXED_PROVIDERS, p)):
        g = groups[pid]
        _prune_days(g["days"], today)
        r429 = r429_by_group.get(pid) if pid == "claude" else None
        state, driver = _state_for(g["windows"], r429, now)
        day_keys = [_local_date(now - i * 86400) for i in range(7)]
        has_tokens = any(g["days"].get(d, {}).get("tok") for d in day_keys)
        has_windows = bool([w for w in g["windows"]
                            if _window_trusted(w, now)])
        if not has_tokens and not has_windows and state == "unknown" \
                and not r429:
            continue                          # nothing to say about it
        rec = {"id": pid, "label": FIXED_PROVIDERS.get(pid, pid),
               "harness": g["harness"], "state": state}
        # windows: trusted ones, plus the active-429 window (no used_pct —
        # never fabricated) when it exhausts the provider
        out_windows = []
        for w in sorted(g["windows"],
                        key=lambda w: ({"session": 0, "weekly": 1,
                                        "daily": 2}.get(w.get("kind"), 3),)):
            if not _window_trusted(w, now):
                continue
            wr = {"kind": w.get("kind") if w.get("kind") in
                  ("session", "weekly", "daily") else None}
            if wr["kind"] is None:
                continue
            if w.get("used_pct") is not None:
                wr["used_pct"] = w["used_pct"]
            if _num(w.get("resets_at")):
                wr["resets_at"] = int(w["resets_at"])
            if _num(w.get("window_min")):
                wr["window_min"] = int(w["window_min"])
            out_windows.append(wr)
        if state == "exhausted" and driver == "r429" and r429:
            wr = {"kind": r429.get("kind") or "session"}
            wr["resets_at"] = int(r429["resets_at"])
            out_windows.append(wr)
        if out_windows:
            rec["windows"] = out_windows[:4]
        observed = g["last_ts"]
        if driver is not None and driver != "r429":
            observed = max(observed or 0, driver.get("observed_at") or 0)
        elif driver == "r429" and r429:
            observed = max(observed or 0, r429.get("observed_at") or 0)
        elif has_windows:
            observed = max([observed or 0] +
                           [w.get("observed_at") or 0
                            for w in g["windows"]
                            if _window_trusted(w, now)])
        if observed:
            rec["observed_at"] = int(observed)
        if state == "unknown":
            rec["source"] = {"claude": "claude_local",
                             "codex": "codex_rollout"}.get(pid, "pi_local")
        elif driver == "r429":
            rec["source"] = "claude_local"
        else:
            rec["source"] = driver.get("source")
        if has_tokens:
            days = [{"date": d, "tokens": int(g["days"].get(d, {})
                                              .get("tok", 0))}
                    for d in day_keys]        # all 7 days: stable chart
            days.reverse()                    # oldest first
            rec["tokens_by_day"] = days
            rec["tokens_today"] = days[-1]["tokens"]
        models = []
        for model, per in g["mday"].items():
            tok = sum(t for d, t in per.items() if d <= today)
            if tok > 0:
                models.append({"model": _sanitize_token(model) or model,
                               "tokens": int(tok)})
        if models:
            models.sort(key=lambda m: -m["tokens"])
            rec["by_model"] = models[:5]
        if g["cost_known"]:
            cost = float(g["days"].get(today, {}).get("cost", 0.0))
            # §7.1 billing: a zero total is a provider-managed (or unknown)
            # route, never a metered charge — omit instead of emitting 0.0.
            if cost > 0:
                rec["cost_today_usd"] = round(cost, 4)
        if g["plan"]:
            rec["plan"] = g["plan"]
        if state == "unknown":
            # §7.5: say WHY the state is unknown, and hand the user the
            # exact adapter id when enabling it would fix it.
            reason, enable = _unknown_reason(pid, cache.get("net"), enabled)
            rec["reason"] = reason
            if enable:
                rec["enable"] = enable
        providers.append(rec)
    return {"generated_at": int(now), "providers": providers}


_ADAPTER_OF = {"claude": "claude_oauth", "codex": "codex_wham",
              "zai": "zai_quota"}


def _unknown_reason(pid, net, enabled):
    """(reason, enable|None) for a provider whose state is unknown
    (§7.5): a disabled adapter → adapter_disabled + the id to enable; an
    enabled adapter's recorded 401 → auth_error; network failures →
    network_error; an adapter with no usable sample → no_data; a
    provider with no measurement path on this host → feature_missing."""
    aid = _ADAPTER_OF.get(pid)
    if aid is None:
        return "feature_missing", None
    if aid not in (enabled or []):
        return "adapter_disabled", aid
    fail = ((net or {}).get(aid) or {}).get("fail")
    if fail == "auth":
        return "auth_error", None
    if fail == "net":
        return "network_error", None
    return "no_data", None


# ---------- cache ----------
def _load_cache(path):
    try:
        with open(path, "rb") as f:
            d = json.loads(f.read(8 * 1024 * 1024))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _fsync_dir(d):
    try:
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _atomic_write(path, body):
    """Atomic persist (mirrors tendril_schedule.save_store): a unique
    mkstemp name in the target dir → write+fsync → chmod 0600 →
    os.replace → dir fsync. A fixed tmp name would let two concurrent
    writers interleave into the same file and corrupt it."""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".capacity-", dir=d)
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
    _fsync_dir(d)


def _save_cache(cache, path=None):
    path = path or CACHE_FILE
    try:
        d = os.path.dirname(path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        _atomic_write(path, json.dumps(cache,
                                       separators=(",", ":")).encode("utf-8"))
    except OSError:
        pass


# ---------- public API ----------
def collect(refresh=False, budget_s=None, now=None, home=None,
            cache_path=None):
    """Build the capacity section. Local transcripts are rescanned when the
    60 s min interval has passed (or refresh=True); enabled network adapters
    are checked only when the budget can cover a 10 s fetch (CLI --refresh,
    scheduler ticks) and only when due (300 s min interval / backoff).
    Never raises."""
    home = home or HOME
    cache_path = cache_path or CACHE_FILE
    now = _time() if now is None else now
    budget_s = REFRESH_BUDGET_S if refresh and budget_s is None \
        else (budget_s if budget_s is not None else BUDGET_S)
    deadline = _mono() + max(0.0, budget_s)
    hard = deadline + (CATCHUP_GRACE_S if refresh else 0.0)
    # the catch-up grace exists for CLI --refresh only: snapshot_section
    # never overruns its budget (contract: < 1.5 s on the snapshot path)
    allow_network = budget_s >= NET_BUDGET_MIN_S
    cache = _load_cache(cache_path)
    if cache.get("acc_ver") != ACC_VERSION:
        cache = {"acc_ver": ACC_VERSION}   # definition changed: start clean
        cache["scanned_at"] = 0
    scanned_at = _num(cache.get("scanned_at")) or 0
    if refresh or now - scanned_at >= LOCAL_MIN_S:
        _scan_all(cache, deadline, hard, now, home)
        cache["scanned_at"] = now
        try:
            _net_phase(cache, now, allow_network, home)
        except Exception:
            pass
    section = _assemble(cache, now, enabled=enabled_ids(home))
    cache["section"] = section
    cache["built_at"] = now
    _save_cache(cache, cache_path)
    return section


def snapshot_section(budget_s=BUDGET_S, now=None, home=None,
                     cache_path=None):
    """(section|None, "capacity") for the snapshot assembler. Serves the
    cached section when fresh; otherwise refreshes within the budget (files
    past the deadline advance on later calls). Never raises."""
    try:
        now = _time() if now is None else now
        cache = _load_cache(cache_path or CACHE_FILE)
        built_at = _num(cache.get("built_at")) or 0
        if cache.get("section") and cache.get("acc_ver") == ACC_VERSION \
                and now - built_at < LOCAL_MIN_S:
            return cache["section"], "capacity"
        return collect(budget_s=budget_s, now=now, home=home,
                       cache_path=cache_path), "capacity"
    except Exception:
        return None, "capacity"


def health_section(now=None, home=None, cache_path=None):
    """Read-only capacity summary for the health module (contract 0.2
    §7.4): {"state": "available|adapters_disabled|error|absent",
    "enabled": [<network adapter ids>]}. Signature (h1 calls this):
    health_section(now=None, home=None, cache_path=None) → dict; NEVER
    raises, NEVER scans transcripts, NEVER touches the network, NEVER
    writes: it serves the cached section only, so a bounded health probe
    stays bounded. States: absent = no cached section (capacity has not
    reported yet); adapters_disabled = a section exists but every
    provider is unknown while no network adapter is enabled (the phone's
    'enable secure host reporting' state); available = usable data (or
    at least one adapter enabled); error = the cache could not be read."""
    try:
        enabled = enabled_ids(home)
        path = cache_path or (CACHE_FILE if home is None else
                              os.path.join(home, ".cache", "tendril",
                                           "capacity.json"))
        cache = _load_cache(path)
        section = cache.get("section") \
            if cache.get("acc_ver") == ACC_VERSION else None
        providers = section.get("providers") \
            if isinstance(section, dict) else None
        if not isinstance(providers, list) or not providers:
            return {"state": "absent", "enabled": enabled}
        if not enabled and all(
                isinstance(p, dict) and p.get("state") == "unknown"
                for p in providers):
            return {"state": "adapters_disabled", "enabled": enabled}
        return {"state": "available", "enabled": enabled}
    except Exception:
        return {"state": "error", "enabled": []}


def provider_state(provider_id, budget_s=NET_BUDGET_MIN_S, now=None,
                   home=None, cache_path=None):
    """Fresh-enough record for the scheduler (hook for tendril_schedule):
    {"id","state","windows","resets_at","observed_at","source"} or None
    when the provider has no data. `resets_at` is the reset of the
    state-driving window — for an active-429 exhaustion that is the 429's
    own reset (its window carries no used_pct). May block up to budget_s
    (default 11 s: local rescan plus, when due, one network check). Never
    raises."""
    try:
        pid = _sanitize_id(provider_id)
        if not pid:
            return None
        section = collect(budget_s=budget_s, now=now, home=home,
                          cache_path=cache_path)
        for rec in section.get("providers", []):
            if rec.get("id") != pid:
                continue
            out = {"id": pid, "state": rec.get("state", "unknown"),
                   "windows": rec.get("windows", []),
                   "observed_at": rec.get("observed_at"),
                   "source": rec.get("source")}
            worst, four29 = None, None
            for w in rec.get("windows", []):
                if w.get("resets_at") is None:
                    continue
                if w.get("used_pct") is None:
                    # the active-429 window (used_pct is never fabricated):
                    # it drives the state whenever it exhausts
                    if (w.get("resets_at") or 0) > (now or _time()):
                        four29 = four29 or w
                    continue
                if worst is None or w["used_pct"] >= worst["used_pct"]:
                    worst = w
            if out.get("state") == "exhausted" and four29 is not None:
                worst = four29
            out["resets_at"] = worst["resets_at"] if worst else None
            return out
        return None
    except Exception:
        return None


# ---------- human status (no secrets exist in these records) ----------
def _human_tokens(n):
    n = float(n)
    if n >= 1e9:
        return "%.2fG" % (n / 1e9)
    if n >= 1e6:
        return "%.2fM" % (n / 1e6)
    if n >= 1e3:
        return "%.1fK" % (n / 1e3)
    return "%d" % int(n)


def _human_age(seconds):
    if seconds < 0:
        seconds = 0
    if seconds < 90:
        return "%ds" % int(seconds)
    if seconds < 5400:
        return "%dm" % int(seconds / 60)
    if seconds < 129600:
        return "%.1fh" % (seconds / 3600.0)
    return "%.1fd" % (seconds / 86400.0)


def status_text(section, now=None, enabled=None):
    """Human table for `tendril capacity status`. Derived only from the
    projected section — it cannot contain secrets."""
    now = _time() if now is None else now
    providers = (section or {}).get("providers") or []
    if not providers:
        return "no provider usage found on this host"
    gen = _num((section or {}).get("generated_at")) or now
    lines = ["provider   state       windows                      "
             "today      plan",
             "-" * 74]
    for p in providers:
        wins = []
        for w in p.get("windows", []):
            bit = w.get("kind", "?")
            if w.get("used_pct") is not None:
                bit += " %.0f%%" % w["used_pct"]
            elif w.get("resets_at") and w["resets_at"] > now:
                bit += " reset " + _human_age(w["resets_at"] - now)
            wins.append(bit)
        today = _human_tokens(p["tokens_today"]) \
            if isinstance(p.get("tokens_today"), int) else "-"
        age = _human_age(now - p["observed_at"]) \
            if p.get("observed_at") else "?"
        lines.append("%-10s %-11s %-28s %-10s %-8s  (seen %s ago)" % (
            p.get("id", "?"), p.get("state", "?"),
            " · ".join(wins) or "-", today, p.get("plan") or "-", age))
    lines.append("observed snapshot %s ago; network adapters: %s" % (
        _human_age(now - gen),
        ", ".join(enabled_ids() if enabled is None else enabled)
        or "none enabled (tendril capacity enable <id>)"))
    return "\n".join(lines)
