#!/usr/bin/env python3
"""tendril_schedule — the TENDRIL host scheduler (LINK-CONTRACT-0.2 §3/§4).

State machine (every row of contract §3 is implemented in tick()):

    add ──► QUEUED (`at`) · WAITING_FOR_RESET (`on_reset`, resets_at known)
        └─► WAITING_FOR_CAPACITY (`when_available`, or `on_reset` unknown)
    clock ≥ at, provider not exhausted            QUEUED              → READY
    clock ≥ at, provider exhausted                QUEUED              → WAITING_FOR_CAPACITY
    clock ≥ resets_at and fresh sample not
    exhausted (or no 429 newer than resets_at)    WAITING_FOR_RESET   → READY
    provider ∈ {ready, pressure, critical}        WAITING_FOR_CAPACITY → READY
    RUNNING + launch_token persisted (fsync)
    BEFORE launching                              READY               → RUNNING
    launch error                                  RUNNING             → FAILED
    RUNNING, never launched, no pane (crash)      RUNNING             → FAILED `interrupted` (never relaunched)
    pane reports done/idle after prompt           RUNNING             → DONE
    pane disappears                               RUNNING             → DONE `session ended`
    cancel (QUEUED/WAITING_*/READY)                                   → CANCELLED
    cancel RUNNING                                                    → refused
    DONE / FAILED / CANCELLED                     immutable

Storage: ~/.local/state/tendril/schedule.json (dir 0700, file 0600),
{"schema_version":1,"tasks":[…]}. Every mutation holds flock on
schedule.json.lock; every save is atomic tmp+fsync+os.replace+dir-fsync.
A corrupt store is quarantined aside and the tick refuses with a clear
error; a wrong schema_version refuses without quarantining.

tick(store, now, snap, capacity, launcher, notifier) is pure in the
testable sense: time, snapshot, capacity, launcher and notifier are all
injected. It DOES persist through the injected store (the contract's
"writes RUNNING + launch_token (fsync) then launches" ordering), so the
crash window between those two steps is real and testable.

The systemd user timer (systemd/tendril-schedule.timer →
tendril-schedule.service → `remote-agents schedule tick`) drives ticks
every 30 s; there is no systemd on macOS, where the tick is a documented
no-op unless run by hand (docs/MACOS.md).

Stdlib only; Python 3.9+.
"""
import base64
import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid

SCHEMA_VERSION = 1

QUEUED = "QUEUED"
WAITING_FOR_RESET = "WAITING_FOR_RESET"
WAITING_FOR_CAPACITY = "WAITING_FOR_CAPACITY"
READY = "READY"
RUNNING = "RUNNING"
DONE = "DONE"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
TERMINAL = (DONE, FAILED, CANCELLED)

TRIGGER_KINDS = ("at", "on_reset", "when_available")

# contract §4: the wire forms are fixed; validate BEFORE decoding
PAYLOAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,16384}$")
ID_RE = re.compile(r"^t-[0-9]{8}-[a-f0-9]{8}$")
WORKSPACE_RE = re.compile(r"^w[A-Za-z0-9_-]{1,31}$")
PANE_RE = re.compile(r"^[A-Za-z0-9]+:[A-Za-z0-9]+$")
PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

TITLE_MAX = 80
PROMPT_MIN = 1
PROMPT_MAX = 8000
FINGERPRINT_WINDOW = 60.0        # seconds (contract: duplicate add within 60 s)
SECTION_TASKS_MAX = 30           # snapshot §1: ≤ 30 tasks
SECTION_TERMINAL_MAX = 20        # …all non-terminal + newest 20 terminal

# launchable = herdr agent start --kind values (verified live);
# "installed" additionally requires the harness executable on PATH.
HARNESSES = ("pi", "claude", "codex", "gemini", "cursor", "devin", "agy",
             "cline", "omp", "mastracode", "opencode", "copilot", "kimi",
             "kiro", "droid", "amp", "grok", "hermes", "kilo", "qodercli",
             "qwen", "letta", "maki", "muse")

# one-line JSON replies; only `schedule show` ever returns prompt text
E_MALFORMED = "malformed payload"
E_B64 = "payload is not valid base64url"
E_JSON = "payload is not valid JSON"
E_OBJECT = "payload must be a JSON object"
E_FIELDS = "unexpected field in payload"
E_TITLE = "title must be 1..80 characters"
E_PROMPT = "prompt must be 1..8000 characters"
E_WORKSPACE = "invalid workspace id"
E_HARNESS = "harness is not launchable"
E_INSTALLED = "harness is not installed"
E_TRIGGER = "invalid trigger"
E_DUPLICATE = "duplicate task"
E_NO_TASK = "no such task"
E_FINISHED = "already finished"
E_RUNNING = "running — open the session to stop it"
E_ID = "invalid task id"
E_CORRUPT = "schedule store corrupt - quarantined; refusing to tick"

# FAILED phrases (contract §3) — everything else collapses to the generic one
LAUNCH_PHRASES = ("launch refused", "workspace missing", "prompt rejected")


class Store(dict):
    """The loaded schedule document. `path` rides on the instance, never
    inside the JSON keys, so the on-disk shape stays exactly
    {"schema_version":1,"tasks":[…]}."""

    def __init__(self, path, tasks=None):
        super().__init__()
        self["schema_version"] = SCHEMA_VERSION
        self["tasks"] = [] if tasks is None else tasks
        self.path = path


# ---------------------------------------------------------------- paths
def store_paths(home=None):
    """(state_dir, schedule.json, schedule.json.lock) under
    $XDG_STATE_HOME (default ~/.local/state)."""
    if home is None:
        root = os.environ.get("XDG_STATE_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "state")
    else:
        root = os.path.join(home, ".local", "state")
    d = os.path.join(root, "tendril")
    return d, os.path.join(d, "schedule.json"), os.path.join(d, "schedule.json.lock")


def timer_installed(home=None):
    """True when the user systemd timer unit is installed (the tick's
    driver; macOS hosts have none — the documented no-op)."""
    base = home or os.path.expanduser("~")
    return os.path.isfile(os.path.join(
        base, ".config", "systemd", "user", "tendril-schedule.timer"))


# ---------------------------------------------------------------- store I/O
def _fsync_dir(d):
    try:
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def save_store(store):
    """Atomic persist: tmp in the same dir → fsync → chmod 0600 →
    os.replace → dir fsync. Creates the state dir 0700 if needed."""
    d = os.path.dirname(store.path)
    os.makedirs(d, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    body = json.dumps({"schema_version": store.get("schema_version"),
                       "tasks": store.get("tasks", [])},
                      separators=(",", ":")).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".schedule-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, store.path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(d)


def _quarantine(path, now):
    """Move a corrupt store aside (never delete — the user's tasks)."""
    with contextlib.suppress(OSError):
        os.replace(path, "%s.corrupt-%d" % (path, int(now * 1000)))


def load_store(path, now=None, quarantine=True):
    """(Store|None, error|None). Missing file → a fresh empty store.
    Corrupt JSON/shape → quarantined aside + clear error (refuse to tick).
    Wrong schema_version → refused WITHOUT quarantining (it is not
    garbage, it is a newer writer). Structurally broken single tasks are
    dropped alone; the store never blocks on one bad row."""
    now = time.time() if now is None else now
    if not os.path.isfile(path):
        return Store(path), None
    try:
        with open(path, "rb") as f:
            raw = f.read()
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, OSError):
        if quarantine:
            _quarantine(path, now)
        return None, E_CORRUPT
    if not isinstance(doc, dict) or not isinstance(doc.get("tasks"), list):
        if quarantine:
            _quarantine(path, now)
        return None, E_CORRUPT
    if doc.get("schema_version") != SCHEMA_VERSION:
        return None, "schedule store schema %s not supported" % (
            doc.get("schema_version"),)
    tasks = []
    for t in doc["tasks"]:
        if (isinstance(t, dict) and isinstance(t.get("id"), str)
                and isinstance(t.get("state"), str)):
            tasks.append(t)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return Store(path, tasks), None


@contextlib.contextmanager
def _locked(lock_path):
    d = os.path.dirname(lock_path)
    os.makedirs(d, exist_ok=True)
    # Symlink refusal before chmod/open: a symlinked state dir or lock
    # would point the 0700 tightening and the lock write at an attacker's
    # target. O_NOFOLLOW backstops the lstat→open race (symlink swap →
    # ELOOP instead of silently locking the target); O_NONBLOCK keeps a
    # weirdly-held fd from half-blocking the open, and is cleared right
    # after so LOCK_EX below keeps its normal blocking semantics.
    if stat.S_ISLNK(os.lstat(d).st_mode):
        raise OSError("refusing symlinked schedule state dir: %s" % d)
    if os.path.lexists(lock_path) \
            and stat.S_ISLNK(os.lstat(lock_path).st_mode):
        raise OSError("refusing symlinked schedule lock: %s" % lock_path)
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                 | os.O_NONBLOCK, 0o600)
    try:
        os.set_blocking(fd, True)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- validation
def _installed(kind, which=None, home=None):
    """The harness executable must exist: PATH first, then the canonical
    install dirs SSH sessions may not have on PATH."""
    which = which or shutil.which
    if which(kind):
        return True
    base = home or os.path.expanduser("~")
    for d in (os.path.join(base, ".local", "bin"), "/usr/local/bin",
              "/opt/homebrew/bin"):
        if os.path.isfile(os.path.join(d, kind)):
            return True
    return False


def _valid_trigger(raw):
    """Strict trigger schema: {kind: at, at: epoch[, provider]} or
    {kind: on_reset|when_available, provider} — nothing else."""
    if not isinstance(raw, dict) or raw.get("kind") not in TRIGGER_KINDS:
        return None, E_TRIGGER
    kind = raw["kind"]
    if kind == "at":
        at = raw.get("at")
        if (isinstance(at, bool) or not isinstance(at, (int, float))
                or not 10 ** 8 <= at <= 4 * 10 ** 9):
            return None, E_TRIGGER
        out = {"kind": "at", "at": int(at)}
        if "provider" in raw:
            p = raw["provider"]
            if not isinstance(p, str) or not PROVIDER_RE.match(p):
                return None, E_TRIGGER
            out["provider"] = p
    else:
        p = raw.get("provider")
        if not isinstance(p, str) or not PROVIDER_RE.match(p):
            return None, E_TRIGGER
        out = {"kind": kind, "provider": p}
    if set(raw) - set(out):
        return None, E_TRIGGER
    return out, None


def fingerprint_of(title, prompt, workspace, harness, trigger):
    canon = json.dumps([title, prompt, workspace, harness, trigger],
                       sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def new_id(fingerprint, now):
    day = time.strftime("%Y%m%d", time.gmtime(now))
    digest = hashlib.sha256(
        (fingerprint + str(int(now))).encode("utf-8")).hexdigest()[:8]
    return "t-%s-%s" % (day, digest)


def decode_add(payload, now, capacity=None, which=None, home=None):
    """(task|None, error|None). Validates the b64url regex BEFORE decoding,
    then strict JSON fields (contract §4). `capacity` (provider_id →
    provider_state|None) resolves a known resets_at for on_reset adds."""
    if not isinstance(payload, str) or not PAYLOAD_RE.match(payload):
        return None, E_MALFORMED
    try:
        raw = base64.urlsafe_b64decode(
            payload + "=" * (-len(payload) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None, E_B64
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, E_JSON
    if not isinstance(doc, dict):
        return None, E_OBJECT
    if set(doc) - {"title", "prompt", "workspace", "harness", "trigger"}:
        return None, E_FIELDS
    title = doc.get("title")
    if not isinstance(title, str) or not 1 <= len(title) <= TITLE_MAX:
        return None, E_TITLE
    prompt = doc.get("prompt")
    if (not isinstance(prompt, str)
            or not PROMPT_MIN <= len(prompt) <= PROMPT_MAX):
        return None, E_PROMPT
    workspace = doc.get("workspace")
    if not isinstance(workspace, str) or not WORKSPACE_RE.match(workspace):
        return None, E_WORKSPACE
    harness = doc.get("harness")
    if not isinstance(harness, str) or harness not in HARNESSES:
        return None, E_HARNESS
    if not _installed(harness, which=which, home=home):
        return None, E_INSTALLED
    trigger, err = _valid_trigger(doc.get("trigger"))
    if err:
        return None, err

    fp = fingerprint_of(title, prompt, workspace, harness, trigger)
    task = {
        "id": new_id(fp, now),
        "title": title,
        "prompt": prompt,
        "workspace": workspace,
        "workspace_label": "",
        "harness": harness,
        "trigger": trigger,
        "state": None,
        "due_at": None,
        "created_at": int(now),
        "started_at": None,
        "finished_at": None,
        "pane_id": None,
        "failure": None,
        # internal (never projected into the snapshot): fingerprint,
        # launch_token, notified, resets_at
        "fingerprint": fp,
        "launch_token": None,
        "notified": {},
        "resets_at": None,
    }
    if trigger["kind"] == "at":
        task["state"] = QUEUED
        task["due_at"] = trigger["at"]
    elif trigger["kind"] == "when_available":
        task["state"] = WAITING_FOR_CAPACITY
    else:                                   # on_reset
        resets = _later_reset(_capacity_of(capacity, trigger["provider"]))
        if isinstance(resets, int):
            task["state"] = WAITING_FOR_RESET
            task["resets_at"] = resets
            task["due_at"] = resets
        else:
            task["state"] = WAITING_FOR_CAPACITY
    return task, None


# ---------------------------------------------------------------- mutations
def add_task(path, task, now):
    """flock { load → duplicate checks (same id, or same fingerprint
    within 60 s) → append → save } → (task|None, error|None). No push on
    add: the user just created the task on the phone — TASK WAITING only
    fires later, from tick(), when an `at` task comes due but the provider
    is exhausted."""
    with _locked(path + ".lock"):
        store, err = load_store(path, now=now)
        if err:
            return None, err
        for t in store["tasks"]:
            if t.get("id") == task["id"]:
                return None, E_DUPLICATE
            if (t.get("fingerprint") == task["fingerprint"]
                    and now - (t.get("created_at") or 0) < FINGERPRINT_WINDOW):
                return None, E_DUPLICATE
        store["tasks"].append(task)
        save_store(store)
    return task, None


def cancel_task(path, task_id, now):
    """(cancelled|None, error|None). RUNNING is refused per contract;
    terminal states are immutable."""
    with _locked(path + ".lock"):
        store, err = load_store(path, now=now)
        if err:
            return None, err
        task = _find(store["tasks"], task_id)
        if task is None:
            return None, E_NO_TASK
        if task["state"] == RUNNING:
            return None, E_RUNNING
        if task["state"] in TERMINAL:
            return None, E_FINISHED
        task["state"] = CANCELLED
        task["finished_at"] = int(now)
        save_store(store)
    return task, None


def get_task(path, task_id):
    """(full task|None, error|None) — the only reader that returns the
    prompt (schedule show)."""
    with _locked(path + ".lock"):
        store, err = load_store(path)
        if err:
            return None, err
        task = _find(store["tasks"], task_id)
    if task is None:
        return None, E_NO_TASK
    return task, None


def list_tasks(path):
    """(tasks, error|None) — raw rows; callers project (never the prompt)."""
    with _locked(path + ".lock"):
        store, err = load_store(path)
        if err:
            return None, err
        tasks = list(store["tasks"])
    tasks.sort(key=_recency_key, reverse=True)
    return tasks, None


def _find(tasks, task_id):
    for t in tasks:
        if t.get("id") == task_id:
            return t
    return None


def _recency_key(t):
    live = 0 if t.get("state") in TERMINAL else 1
    return (live, t.get("created_at") or 0, t.get("finished_at") or 0)


# ---------------------------------------------------------------- tick
def launch_name(task):
    """The herdr agent/tab name for a task: deterministic from its id."""
    return "tendril-" + str(task.get("id", "")).split("-")[-1]


def _capacity_of(capacity, provider):
    if capacity is None or not provider:
        return None
    try:
        entry = capacity(provider)
    except Exception:
        return None
    return entry if isinstance(entry, dict) else None


def _later_reset(entry):
    """Max resets_at across the provider's windows, or None."""
    if not entry:
        return None
    out = None
    for w in entry.get("windows") or []:
        r = w.get("resets_at") if isinstance(w, dict) else None
        if isinstance(r, int) and not isinstance(r, bool):
            out = r if out is None else max(out, r)
    return out


def _notify(notifier, task, kind):
    """TASK STARTED / TASK WAITING / TASK FAILED — exactly once per
    transition, gated on the persisted notified set."""
    if notifier is None:
        return
    slot = task.setdefault("notified", {})
    if slot.get(kind):
        return
    slot[kind] = True
    try:
        notifier(kind, task)
    except Exception:
        pass                                  # a push must never fail a tick


def _transition(task, old, new, now, failure=None):
    task["state"] = new
    if failure is not None:
        task["failure"] = failure
    if new == RUNNING:
        task["started_at"] = int(now)
    if new in TERMINAL:
        task["finished_at"] = int(now)
    return {"id": task["id"], "from": old, "to": new, "at": int(now)}


def _queued_tick(task, now, capacity):
    trig = task.get("trigger") or {}
    at = trig.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or now < at:
        return None
    entry = _capacity_of(capacity, trig.get("provider"))
    if entry and entry.get("state") == "exhausted":
        return _transition(task, task["state"], WAITING_FOR_CAPACITY, now)
    return _transition(task, task["state"], READY, now)


def _reset_tick(task, now, capacity):
    entry = _capacity_of(capacity, (task.get("trigger") or {}).get("provider"))
    resets = task.get("resets_at")
    late = _later_reset(entry)
    if isinstance(late, int) and isinstance(resets, int) and late > resets:
        task["resets_at"] = late              # refresh if the reset moved later
        if (task.get("trigger") or {}).get("kind") == "on_reset":
            task["due_at"] = late
        resets = late
    if not isinstance(resets, int) or now < resets:
        return None
    if entry and entry.get("state") == "exhausted":
        return None
    # fresh sample not exhausted, or no evidence of a newer 429 → READY
    return _transition(task, task["state"], READY, now)


def _no_capacity_reason(provider):
    """Why a WAITING_FOR_CAPACITY task is not moving (safe phrase ≤ 60)."""
    return "%s does not report capacity to this host" % (provider or "")[:16]


def _capacity_tick(task, now, capacity):
    entry = _capacity_of(capacity, (task.get("trigger") or {}).get("provider"))
    if entry and entry.get("state") in ("ready", "pressure", "critical"):
        task.pop("reason", None)              # moving again
        return _transition(task, task["state"], READY, now)
    if entry and entry.get("state") == "exhausted":
        task.pop("reason", None)              # reported: exhaustion says why
        return None
    # no sample or unknown state → carry the reason for the phone's
    # "why isn't this moving" line; cleared as soon as a sample arrives
    provider = (task.get("trigger") or {}).get("provider") or ""
    task["reason"] = _no_capacity_reason(provider) if provider else None
    return None


def _running_tick(task, now, snap):
    """Crash/finish dispositions for RUNNING tasks. No snapshot → no
    evidence, leave alone. A pane the launcher never recorded and that
    nothing can match is a crash → FAILED `interrupted` (never
    relaunched). A recorded pane that is gone → DONE `session ended`."""
    if not isinstance(snap, dict):
        return None
    panes = [p for p in (snap.get("panes") or []) if isinstance(p, dict)]
    pane_id = task.get("pane_id")
    if pane_id:
        for p in panes:
            if p.get("pane_id") == pane_id:
                if p.get("agent_status") in ("done", "idle"):
                    return _transition(task, task["state"], DONE, now)
                return None                   # working/blocked/unknown: still going
        return _transition(task, task["state"], DONE, now, failure="session ended")
    return _transition(task, task["state"], FAILED, now, failure="interrupted")


def tick(store, now, snap, capacity, launcher, notifier):
    """One scheduler pass over the loaded store. Transitions first, then a
    single atomic persist (READY tasks go to disk as RUNNING + launch_token
    — contract's fsync-before-launch point), and only then the launches.
    Returns the list of transition events. Caller must hold the store's
    flock (run_tick does)."""
    events = []
    launches = []
    for task in store.get("tasks", []):
        if not isinstance(task, dict):
            continue
        st = task.get("state")
        if st in TERMINAL:
            continue                          # terminal states are immutable
        if st == RUNNING:
            ev = _running_tick(task, now, snap)
        elif st == QUEUED:
            ev = _queued_tick(task, now, capacity)
        elif st == WAITING_FOR_RESET:
            ev = _reset_tick(task, now, capacity)
        elif st == WAITING_FOR_CAPACITY:
            ev = _capacity_tick(task, now, capacity)
        else:
            ev = None
        if ev:
            ev["from"] = st
            events.append(ev)
            if st == QUEUED and task["state"] == WAITING_FOR_CAPACITY:
                # TASK WAITING: the `at` task came due but the provider is
                # exhausted — exactly once per task via the notified gate.
                # (Adding a task never pushes: the user just created it.)
                _notify(notifier, task, "waiting")
        if task.get("state") == READY:
            task.pop("reason", None)          # no longer stuck on capacity
            task["launch_token"] = uuid.uuid4().hex
            events.append(_transition(task, READY, RUNNING, now))
            launches.append(task)
    save_store(store)                         # RUNNING + tokens on disk FIRST
    for task in launches:
        ok, pane_id, err = _launch_one(launcher, task)
        if ok:
            task["pane_id"] = pane_id
            _notify(notifier, task, "started")
        else:
            task["state"] = FAILED
            task["failure"] = err
            task["finished_at"] = int(now)
            events.append({"id": task["id"], "from": RUNNING, "to": FAILED,
                           "at": int(now), "failure": err})
            _notify(notifier, task, "failed")
        save_store(store)
    return events


def _launch_one(launcher, task):
    """(ok, pane_id, safe_failure_phrase). The launcher is host-supplied
    (remote-agents' argv sequence); it must never be able to crash the
    tick, and only contract phrases survive."""
    if launcher is None:
        return False, None, "launch refused"
    try:
        ok, pane_id, err = launcher(task)
    except Exception:
        return False, None, "launch refused"
    if ok:
        if isinstance(pane_id, str) and PANE_RE.match(pane_id):
            return True, pane_id, None
        return False, None, "launch refused"
    return False, None, (err if err in LAUNCH_PHRASES else "launch refused")


def run_tick(path, now=None, snap=None, capacity=None, launcher=None,
             notifier=None):
    """The flock-serialized entry point (one tick at a time per host).
    (summary|None, error|None); a corrupt/quarantined store refuses here."""
    now = time.time() if now is None else now
    with _locked(path + ".lock"):
        store, err = load_store(path, now=now)
        if err:
            return None, err
        events = tick(store, now, snap, capacity, launcher, notifier)
    return {"tick": {"at": int(now), "launched": sum(
        1 for e in events if e["to"] == RUNNING), "transitions": len(events)},
        "events": events}, None


# ---------------------------------------------------------------- snapshot
def project(task):
    """Contract §1 task shape for snapshot/list/add replies: never the
    prompt, fingerprint, launch_token or notified set. `reason` rides
    only on a WAITING_FOR_CAPACITY task whose provider reports nothing
    (the phone's 'why isn't this moving' line)."""
    out = {}
    for k in ("id", "title", "workspace", "workspace_label", "harness",
              "trigger", "state", "due_at", "created_at", "started_at",
              "finished_at", "pane_id", "failure"):
        v = task.get(k)
        if v is None:
            continue
        out[k] = copy.deepcopy(v) if isinstance(v, dict) else v
    if task.get("state") == WAITING_FOR_CAPACITY and task.get("reason"):
        out["reason"] = task["reason"]
    return out


def _section_tasks(tasks):
    live = [t for t in tasks if t.get("state") not in TERMINAL]
    done = [t for t in tasks if t.get("state") in TERMINAL]
    live.sort(key=lambda t: t.get("created_at") or 0, reverse=True)
    done.sort(key=lambda t: t.get("finished_at")
              or t.get("created_at") or 0, reverse=True)
    out = [project(t) for t in live[:SECTION_TASKS_MAX]]
    for t in done[:SECTION_TERMINAL_MAX]:
        if len(out) >= SECTION_TASKS_MAX:
            break
        out.append(project(t))
    return out


def snapshot_section(now=None, home=None):
    """(section|None, "schedule") for the snapshot assembler — never
    raises, never contains prompt text. available is False when the tick
    driver (the user timer) is missing or the store refuses."""
    try:
        now = time.time() if now is None else now
        _, path, _ = store_paths(home)
        tasks, err = [], None
        if os.path.isfile(path):
            store, err = load_store(path, now=now, quarantine=False)
            if err is None:
                tasks = store["tasks"]
        ok = err is None and timer_installed(home)
        return {"available": ok, "tasks": [] if err else _section_tasks(tasks)
                }, "schedule"
    except Exception:
        return None, "schedule"
