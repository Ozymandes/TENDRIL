#!/usr/bin/env python3
"""tendril_health — Herdr health probe, guardian tick, guarded restart.

LINK-CONTRACT-0.2 §6 (binding): tell "host unreachable" from "Herdr
offline", notify exactly once per incident, and offer one safe, VERIFIED
revive. Stdlib only, Python 3.9+. The guardian NEVER restarts Herdr; only
the explicit `remote-agents herdr restart|stop|start` verbs touch the
server lifecycle.

Research evidence (read-only on the live host, 2026-10-10)
----------------------------------------------------------
Probe ("is Herdr running" without ever blocking on a dead server):

    $ herdr status server --json
    {"status":"running","running":true,"version":"0.9.3","protocol":22,
     ...,"socket":"/home/seeno/.config/herdr/herdr.sock"}

This is a client-side read (a local unix-socket connect): it answers
instantly while the server lives and degrades to `running:false` when the
socket is gone. We still bound it with a subprocess timeout (1.5 s; a
timeout parses as "unknown", never a hang). `ps` on the live host shows
the server process is herdr's own headless mode:

    /home/seeno/.local/share/mise/installs/herdr/0.9.3/herdr server

Start (supported headless lifecycle): `herdr --help` documents
"herdr server — Run as headless server"; `herdr server --help` offers only
stop/reload-config/agent-manifests — there is NO "start" subcommand. The
supported start is therefore the bare `herdr server` process, launched so
it survives the SSH session and ties to the USER session:

    systemd-run --user --unit tendril-herdr-<8hex> --collect <herdr> server

(preferred: the unit lives under the user's systemd manager; --collect
removes the unit when the server exits). When systemd-run is missing or
refuses (launchd/macOS, no user bus), we fall back to a setsid-equivalent
detached child: argv-only Popen with start_new_session=True and
stdio->DEVNULL (survives the SSH session; reparented to init). Success is
never assumed: verification is the contract's — `herdr status server
--json` reports running AND `herdr api snapshot` answers, retried within
a 10 s total budget (every subprocess is bounded by the remaining budget).

State machine (guardian tick, ~30 s systemd timer, §6.2)
--------------------------------------------------------
Observations: running | down | stopped (intentional-stop guard set) |
unknown (probe timed out / no client). State file
`~/.local/state/tendril/herdr-health.json` (0600, atomic tmp+fsync+
replace+dir-fsync, flock on every mutation):
`{state, since, last_healthy_at, incident_id, notified_down, notified_up}`
plus the internal `down_streak` (the 2-observation debounce counter).

- running: `last_healthy_at` refreshed; down→running sends at most ONE
  "TENDRIL · HERDR RESTORED" per incident, then the incident closes.
- down: needs 2 CONSECUTIVE down observations before the incident opens;
  the incident opens with exactly one "TENDRIL · HERDR OFFLINE" /
  "Host <h> is reachable, but Herdr stopped." and never a second one.
- stopped (guard set): NO incident, NO notification — an intentional stop
  is silent by design; the incident flags are preserved so a later
  `tendril herdr start` can close a pre-existing incident with one
  RESTORED.
- unknown: changes nothing (a flaky probe must not flap the state).

Notifications go through bin/herdr-notify `send()` exactly like the
scheduler does (loaded lazily by remote-agents; a no-op when ntfy is
unconfigured). Click target is None — the notification opens the app,
never a pane.

`health --json` output carries NO secrets, paths, usernames, other
machines' hostnames, pids, or env (§0/§6.1): enums, timestamps, the
sanitized host label and a sanitized version string only.
"""
import contextlib
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

SCHEMA_VERSION = 1
DEBOUNCE_OBSERVATIONS = 2          # consecutive down observations (≈30 s)
RATE_LIMIT_S = 30.0                # one restart attempt per 30 s (§6.1)
VERIFY_BUDGET_S = 10.0             # restart verification budget (§6.1)
PROBE_TIMEOUT_S = 1.5              # status probe bound (health ≤ 3 s total)
UNIT_PROBE_TIMEOUT_S = 1.2         # systemctl is-active bound
API_TIMEOUT_S = 5.0                # `herdr api snapshot` bound (§6.1)
RESTART_MIN_INTERVAL_S = 30.0

HERDR_OFFLINE_TITLE = "TENDRIL · HERDR OFFLINE"
HERDR_OFFLINE_BODY = "Host %s is reachable, but Herdr stopped."
HERDR_RESTORED_TITLE = "TENDRIL · HERDR RESTORED"
HERDR_RESTORED_BODY = "Herdr is back on host %s."

STATES = ("running", "down", "stopped", "unknown")

_UNIT_SPECS = (("notify", "herdr-notify.service"),
               ("scheduler", "tendril-schedule.timer"),
               ("guardian", "tendril-guardian.timer"))

_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,23}$")

E_GUARD = "Stopped on purpose — start it on the host"
E_RUNNING = "Herdr is already running"
E_SLOW = "Rate limited; wait %d s"
E_NOUP = "Herdr did not come up"
E_START = "could not start Herdr"
E_NOHERDR = "no herdr client found"
E_STOP = "Herdr stop command failed"


# ---------------------------------------------------------------- paths
def paths(home=None):
    """State files under $XDG_STATE_HOME (default ~/.local/state)/tendril:
    (dict) state, lock, guard, stamp — the guard/stamp live beside the
    guardian state file so restart and tick serialize on one flock."""
    if home is None:
        root = os.environ.get("XDG_STATE_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "state")
    else:
        root = os.path.join(home, ".local", "state")
    d = os.path.join(root, "tendril")
    return {"dir": d,
            "state": os.path.join(d, "herdr-health.json"),
            "lock": os.path.join(d, "herdr-health.lock"),
            "guard": os.path.join(d, "herdr-stop-guard"),
            "stamp": os.path.join(d, "herdr-restart-at")}


def _fsync_dir(d):
    try:
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _atomic_write(path, body, mode=0o600):
    """tmp in the same dir → fsync → chmod → os.replace → dir fsync."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".herdr-health-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(d)


@contextlib.contextmanager
def _locked(lock_path):
    d = os.path.dirname(lock_path)
    os.makedirs(d, exist_ok=True)
    # Same symlink refusal as tendril_schedule._locked: a symlinked state
    # dir or lock must not aim the 0700 tightening and the lock write at
    # an attacker-chosen target. O_NOFOLLOW backstops the lstat→open race
    # (swap → ELOOP); O_NONBLOCK is cleared right after so LOCK_EX keeps
    # its normal blocking semantics.
    if stat.S_ISLNK(os.lstat(d).st_mode):
        raise OSError("refusing symlinked health state dir: %s" % d)
    if os.path.lexists(lock_path) \
            and stat.S_ISLNK(os.lstat(lock_path).st_mode):
        raise OSError("refusing symlinked health lock: %s" % lock_path)
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


def _quarantine(path, now):
    """Move a corrupt state file aside (never delete — diagnostics)."""
    with contextlib.suppress(OSError):
        os.replace(path, "%s.corrupt-%d" % (path, int(now * 1000)))


def load_state(path, now=None):
    """(dict, error|None). Missing → fresh empty. Corrupt → quarantined
    aside + error (the guardian recovers to a fresh state file)."""
    now = time.time() if now is None else now
    if not os.path.isfile(path):
        return {}, None
    try:
        with open(path, "rb") as f:
            doc = json.loads(f.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError, OSError):
        _quarantine(path, now)
        return {}, "corrupt state quarantined"
    if not isinstance(doc, dict):
        _quarantine(path, now)
        return {}, "corrupt state quarantined"
    out = {}
    if doc.get("state") in STATES:
        out["state"] = doc["state"]
    for key in ("since", "last_healthy_at"):
        if isinstance(doc.get(key), int) and doc[key] > 0:
            out[key] = doc[key]
    if isinstance(doc.get("incident_id"), str) and \
            re.match(r"^h-[0-9]{8}-[0-9a-f]{8}$", doc["incident_id"]):
        out["incident_id"] = doc["incident_id"]
    for key in ("notified_down", "notified_up"):
        if doc.get(key) is True:
            out[key] = True
    streak = doc.get("down_streak")
    if isinstance(streak, int) and 0 <= streak <= 100000:
        out["down_streak"] = streak
    return out, None


def save_state(path, doc):
    _atomic_write(path, json.dumps(doc, separators=(",", ":")).encode("utf-8"))


# ---------------------------------------------------------------- probes
def run_bounded(args, timeout):
    """(rc, out, err) — subprocess bounded hard; never raises."""
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except (FileNotFoundError, OSError):
        return 127, "", "not found"


def _parse_status(out):
    """(running|None, version|None) from `herdr status server --json`."""
    try:
        doc = json.loads(out)
    except ValueError:
        return None, None
    if not isinstance(doc, dict):
        return None, None
    running = doc.get("running")
    running = True if running is True else False if running is False else None
    version = doc.get("version")
    if not (isinstance(version, str) and _VERSION_RE.match(version)):
        version = None
    return running, version


def probe_herdr(run, herdr, timeout=PROBE_TIMEOUT_S):
    """(state, version): "running"/"down" from the status JSON's own
    `running` boolean; anything else (timeout, garbage, no client) is
    honestly "unknown" with no version. Never blocks past `timeout`."""
    if not herdr:
        return "unknown", None
    rc, out, _err = run([herdr, "status", "server", "--json"], timeout)
    if rc == 124 or rc == 127:
        return "unknown", None
    running, version = _parse_status(out)
    if running is None:
        return "unknown", version
    return ("running" if running else "down"), version


def api_alive(run, herdr, timeout=API_TIMEOUT_S):
    """True when `herdr api snapshot` answers rc 0 within `timeout`."""
    if not herdr:
        return False
    rc, _out, _err = run([herdr, "api", "snapshot"], timeout)
    return rc == 0


# ------------------------------------------------- systemd unit states
def _run_default(args, timeout):
    return run_bounded(args, timeout)


def unit_states(run=None, home=None):
    """{notify|scheduler|guardian: active|inactive|failed|absent}. A unit
    file that is not installed is "absent" (no subprocess); installed
    units are read from ONE bounded `systemctl --user is-active` call.
    Without a systemctl binary (launchd hosts) everything fileless is
    "absent" and installed units read "inactive" (no manager to run them);
    a systemctl timeout degrades installed units to "inactive" rather than
    inventing an active state."""
    run = run or _run_default
    base = home or os.path.expanduser("~")
    unit_dir = os.path.join(base, ".config", "systemd", "user")
    out = {}
    present = []
    for key, unit in _UNIT_SPECS:
        if not os.path.isfile(os.path.join(unit_dir, unit)):
            out[key] = "absent"
        else:
            out[key] = "inactive"          # corrected below when active
            present.append((key, unit))
    if not present:
        return out
    rc, stdout, _err = run(["systemctl", "--user", "is-active"]
                           + [u for _k, u in present],
                           UNIT_PROBE_TIMEOUT_S)
    if rc == 127:                           # no systemctl at all
        return out
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    for (key, _unit), word in zip(present, lines):
        if word in ("active", "failed"):
            out[key] = word
        elif word == "inactive" or word == "":
            out[key] = "inactive"
        # any other surprising word keeps the conservative "inactive"
    return out


# ------------------------------------------------------------ tendril
def install_state(here=None):
    """healthy|degraded — the install files around THIS module are present
    and runnable (remote-agents, herdr-notify, the sibling host modules)."""
    here = here or os.path.dirname(os.path.abspath(__file__))
    needed = ("remote-agents", "herdr-notify", "tendril_schedule.py",
              "tendril_health.py")
    for name in needed:
        path = os.path.join(here, name)
        if not os.path.isfile(path):
            return "degraded"
        if name.endswith(".py"):
            continue
        if not os.access(path, os.X_OK):
            return "degraded"
    return "healthy"


# --------------------------------------------------------- health doc
def health_doc(host=None, herdr=None, run=None, home=None, now=None,
               probe=None, units=None, install=None):
    """The contract §6.1 `health --json` document (one line, no secrets,
    no paths, no pids). Bounded: probe ≤ 1.5 s + unit probe ≤ 1.2 s.
    Reachability of the HOST is deliberately absent — that is the phone's
    classification (§6.3); this doc only describes what the host knows."""
    run = run or _run_default
    now = time.time() if now is None else now
    p = paths(home)
    guard = os.path.isfile(p["guard"])
    if probe is not None:
        state, version = probe()
    else:
        state, version = probe_herdr(run, herdr)
    if guard:
        state = "stopped"                   # intentional stop wins (§6.1)
    herdr_doc = {"state": state}
    st, _err = load_state(p["state"], now)
    for key in ("last_healthy_at", "since"):
        if isinstance(st.get(key), int):
            herdr_doc[key] = st[key]
    if version:
        herdr_doc["version"] = version
    units_doc = units or unit_states(run, home)
    doc = {"version": SCHEMA_VERSION,
           "host": host,
           "generated_at": int(now),
           "herdr": herdr_doc,
           "tendril": {"state": (install if install is not None
                                 else install_state())},
           # §6.1: every component is an OBJECT {"state": ...}
           "notify": {"state": units_doc.get("notify", "absent")},
           "scheduler": {"state": units_doc.get("scheduler", "absent")},
           "guardian": {"state": units_doc.get("guardian", "absent")}}
    return doc


def snapshot_section():
    """(section, capability) for remote-agents `_optional_sections`: the
    phone needs only the `health` capability string (§6.3); the section
    itself stays minimal by design."""
    return {"available": True}, "health"


# ------------------------------------------------------ guardian tick
def _incident_id(epoch):
    stamp = time.strftime("%Y%m%d", time.gmtime(epoch))
    return "h-%s-%s" % (stamp, uuid.uuid4().hex[:8])


def run_tick(host=None, herdr=None, run=None, home=None, now=None,
             probe=None, notifier=None):
    """One guardian observation (contract §6.2). flock-serialized; the
    state file is the truth. Returns a summary dict for the CLI reply.
    NEVER starts, stops or restarts anything."""
    run = run or _run_default
    epoch = time.time() if now is None else now
    p = paths(home)
    with _locked(p["lock"]):
        prev, _err = load_state(p["state"], epoch)
        st = prev.get("state", "unknown")
        if os.path.isfile(p["guard"]):
            cat = "stopped"                 # intentional stop: silent
        elif probe is not None:
            cat = probe()[0]
        else:
            cat, _v = probe_herdr(run, herdr)
        doc = dict(prev)
        notified = []
        if cat == "running":
            doc["down_streak"] = 0
            doc["last_healthy_at"] = int(epoch)
            if (prev.get("notified_down")
                    and not prev.get("notified_up")):
                # an open incident resolves exactly once — including when
                # the user did `tendril herdr stop` + `start` in between
                # (the guard made the interim state "stopped")
                if notifier is not None:
                    notifier("restored", HERDR_RESTORED_TITLE,
                             HERDR_RESTORED_BODY % (host or "host"))
                notified.append("restored")
                # the incident closes with its single RESTORED
                doc.pop("incident_id", None)
                doc["notified_down"] = False
                doc["notified_up"] = False
            if st != "running":
                doc["state"] = "running"
                doc["since"] = int(epoch)
        elif cat == "down":
            streak = int(prev.get("down_streak", 0)) + 1
            doc["down_streak"] = streak
            if st != "down":
                doc["state"] = "down"
                doc["since"] = int(epoch)
            if (streak >= DEBOUNCE_OBSERVATIONS
                    and not prev.get("notified_down")):
                doc["incident_id"] = _incident_id(epoch)
                doc["notified_down"] = True
                if notifier is not None:
                    notifier("offline", HERDR_OFFLINE_TITLE,
                             HERDR_OFFLINE_BODY % (host or "host"))
                notified.append("offline")
        elif cat == "stopped":
            doc["down_streak"] = 0
            if st != "stopped":
                doc["state"] = "stopped"
                doc["since"] = int(epoch)
            # no incident, no notification (§6.2)
        else:                               # unknown: change nothing
            if not prev:
                doc["state"] = "unknown"
                doc["since"] = int(epoch)
        save_state(p["state"], doc)
    return {"state": doc.get("state"),
            "incident_id": doc.get("incident_id"),
            "notified": notified}


# ------------------------------------------------- guarded lifecycle
def make_starter(herdr, run=None, which=None, popen=None):
    """starter() → (ok, phrase). systemd transient user unit first (ties
    the server to the user session), detached child as fallback. Argv
    lists only — never a shell string."""
    run = run or _run_default
    which = which or shutil.which
    popen = popen or subprocess.Popen

    def start():
        if not herdr:
            return False, E_NOHERDR
        if which("systemd-run") and os.environ.get("XDG_RUNTIME_DIR"):
            unit = "tendril-herdr-" + uuid.uuid4().hex[:8]
            rc, _o, _e = run(["systemd-run", "--user", "--unit", unit,
                              "--collect", herdr, "server"], 8)
            if rc == 0:
                return True, None
        try:                                # setsid-equivalent detach
            popen([herdr, "server"], stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  start_new_session=True)
            return True, None
        except OSError:
            return False, E_START
    return start


def make_stopper(herdr, run=None):
    run = run or _run_default

    def stop():
        if not herdr:
            return False
        rc, _o, _e = run([herdr, "server", "stop"], 8)
        return rc == 0
    return stop


def herdr_verb(verb, host=None, herdr=None, run=None, home=None,
               probe=None, api=None, starter=None, stopper=None,
               epoch=None, mono=None, sleep=time.sleep):
    """`restart|stop|start` (contract §6.1) — returns the reply dict.

    restart: guard-set ⇒ refused; already running+answering ⇒ refused;
    rate limited to one attempt per 30 s (flock + timestamp); starts via
    the supported lifecycle and VERIFIES (status running AND
    `herdr api snapshot` answering, ≤ 10 s total). Never touches the guard.
    stop: writes the stop guard FIRST, then stops the server.
    start: clears the guard, then starts + verifies (no rate limit — it is
    the host user's explicit recovery)."""
    run = run or _run_default
    epoch = epoch or time.time
    mono = mono or time.monotonic
    p = paths(home)
    starter = starter or make_starter(herdr, run)
    stopper = stopper or make_stopper(herdr, run)

    def probe_api(deadline):
        """status running AND api snapshot answering, within `deadline`
        (a mono() timestamp); every call is bounded by what's left."""
        while True:
            left = deadline - mono()
            if left <= 0:
                return False
            if probe is not None:
                state = probe()[0]
            else:
                state, _v = probe_herdr(run, herdr, min(PROBE_TIMEOUT_S,
                                                        left))
            if state != "running":
                if mono() >= deadline:
                    return False
                sleep(min(0.5, max(0.0, deadline - mono())))
                continue
            left = deadline - mono()
            if left <= 0:
                return False
            if api is not None:
                return bool(api())
            return api_alive(run, herdr, min(API_TIMEOUT_S, left))

    if verb == "restart":
        if os.path.isfile(p["guard"]):
            return {"ok": False, "error": E_GUARD}
        if probe is not None:
            state = probe()[0]
        else:
            state, _v = probe_herdr(run, herdr)
        if state == "running":
            alive = api() if api is not None else api_alive(run, herdr)
            if alive:
                return {"ok": False, "error": E_RUNNING}
        with _locked(p["lock"]):
            # a concurrent `herdr stop` can land between the up-front
            # check and this lock — re-check under the same flock the
            # stop writes under, so an intentional stop is never
            # resurrected (§6.1 guard semantics)
            if os.path.isfile(p["guard"]):
                return {"ok": False, "error": E_GUARD}
            try:
                with open(p["stamp"]) as f:
                    last = float(f.read().strip() or 0)
            except (OSError, ValueError):
                last = 0.0
            now = epoch()
            if last > 0 and now - last < RATE_LIMIT_S:
                return {"ok": False,
                        "error": E_SLOW % int(RATE_LIMIT_S
                                              - (now - last))}
            _atomic_write(p["stamp"], b"%d" % int(now))
        if os.path.isfile(p["guard"]):     # stop landed while we verified
            return {"ok": False, "error": E_GUARD}
        ok, phrase = starter()
        if not ok:
            return {"ok": False, "error": phrase or E_NOUP}
        if probe_api(mono() + VERIFY_BUDGET_S):
            return {"ok": True, "herdr": {"state": "running"}}
        return {"ok": False, "error": E_NOUP}

    if verb == "start":
        with contextlib.suppress(OSError):
            os.unlink(p["guard"])       # clear the guard FIRST (§6.1)
        if probe is not None:
            state = probe()[0]
        else:
            state, _v = probe_herdr(run, herdr)
        if state == "running":
            alive = api() if api is not None else api_alive(run, herdr)
            if alive:
                return {"ok": True, "herdr": {"state": "running"}}
        ok, phrase = starter()
        if not ok:
            return {"ok": False, "error": phrase or E_NOUP}
        if probe_api(mono() + VERIFY_BUDGET_S):
            return {"ok": True, "herdr": {"state": "running"}}
        return {"ok": False, "error": E_NOUP}

    if verb == "stop":
        # the guard is written BEFORE the stop: a crash in between must
        # still record the user's intent (§6.1 "writes the stop guard,
        # then stops Herdr"). Under the restart flock, so a concurrent
        # restart's guarded re-check sees it before starting anything.
        with _locked(p["lock"]):
            _atomic_write(p["guard"], b"intentional stop\n")
        if stopper():
            return {"ok": True, "herdr": {"state": "stopped"}}
        return {"ok": False, "error": E_STOP}

    return {"ok": False, "error": "unknown verb"}
