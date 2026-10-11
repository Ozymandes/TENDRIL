"""Scheduler: bin/tendril_schedule.py + the `remote-agents schedule` verbs.

LINK-CONTRACT-0.2 §3 under test here:
- storage ~/.local/state/tendril/schedule.json (dir 0700, file 0600),
  flock on every mutation, atomic tmp+fsync+replace+dir-fsync;
- corrupt store → quarantined aside, tick refuses; schema mismatch → refuse;
- EVERY state-machine row (add/QUEUED/WAITING_FOR_RESET/
  WAITING_FOR_CAPACITY/READY/RUNNING/DONE/FAILED/CANCELLED, cancel rules,
  terminal immutability, duplicate add);
- RUNNING + launch_token persisted BEFORE the launcher runs (the contract's
  fsync point), crash-in-between → FAILED `interrupted`, never relaunched;
- notify exactly once per transition; prompt text only via `schedule show`;
- unit install/refresh idempotent with a fake systemctl; launchd no-op;
- two concurrent tick subprocesses → exactly one launch.

Clock, launcher, snapshot, capacity and notifier are fakes; HOME/state is a
tempdir; the end-to-end CLI tick runs against a fake `herdr` on PATH — no
real herdr server, workspace, tab or agent is ever touched. The store on
disk is the truth: tick() reloads it, so tests always assert on the stored
row (Base.row), never on a stale in-memory copy.
"""
import base64
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from importlib.machinery import SourceFileLoader

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHED = os.path.join(REPO, "bin", "tendril_schedule.py")
HOST = os.path.join(REPO, "bin", "tendril_host.py")
CLI = os.path.join(REPO, "bin", "remote-agents")


def load_mod(name, path):
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ts = load_mod("tendril_schedule_under_test", SCHED)
th = load_mod("tendril_host_under_test", HOST)

NOW = 1_800_000_000.0            # pure-engine tests inject this clock
DUE = 1_799_999_000              # an `at` already due (relative to NOW)
FUTURE = 1_800_100_000           # an `at` still ahead
RESET_AT = 1_799_999_500
AT_PAST = int(time.time()) - 3600        # CLI/ticker ticks run the real clock
AT_FUTURE = int(time.time()) + 3600


def b64(doc):
    return base64.urlsafe_b64encode(
        json.dumps(doc).encode("utf-8")).decode("ascii").rstrip("=")


def payload(**over):
    doc = {"title": "Architecture audit",
           "prompt": "Audit the architecture and report findings",
           "workspace": "w15", "harness": "pi",
           "trigger": {"kind": "at", "at": DUE}}
    doc.update(over)
    return doc


def cap_of(state="ready", resets_at=None, provider="claude"):
    """capacity(provider) fake in the tendril_capacity.provider_state shape."""
    windows = []
    if resets_at is not None:
        windows = [{"kind": "session", "used_pct": 50.0,
                    "resets_at": resets_at, "window_min": 300}]

    def capacity(pid):
        if pid != provider:
            return None
        return {"id": pid, "state": state, "windows": windows,
                "observed_at": int(NOW)}
    return capacity


def rec_launcher(ok=True, pane="w15:p9", err=None, log=None):
    def launch(task):
        if log is not None:
            log.append(task["id"])
        if ok:
            return True, pane, None
        return False, None, err
    return launch


def rec_notifier():
    seen = []

    def notify(kind, task):
        seen.append((kind, task["id"]))
    return notify, seen


def snap_of(panes):
    return {"workspaces": [], "panes": panes}


def pane(pid, status, agent="pi"):
    return {"pane_id": pid, "agent": agent, "agent_status": status,
            "workspace_id": "w15"}


def installed_harness(harness, home):
    """Make `harness` 'installed' for the module's canonical-dir check."""
    d = os.path.join(home, ".local", "bin")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, harness)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n")
    os.chmod(path, 0o755)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tendril-sched-test-")
        self.addCleanup(self._tmp.cleanup)
        self.home = self._tmp.name
        self.dir, self.store, self.lock = ts.store_paths(self.home)

    # -- helpers -----------------------------------------------------------
    def add(self, now=NOW, capacity=None, **over):
        doc = payload(**over)
        task, err = ts.decode_add(b64(doc), now,
                                  capacity=capacity or cap_of(),
                                  which=lambda k: True, home=self.home)
        self.assertIsNone(err)
        saved, err = ts.add_task(self.store, task, now)
        self.assertIsNone(err)
        return saved

    def row(self, ref):
        """The stored row for a task (the disk copy is the truth; tick()
        works on freshly loaded dicts, so references go stale)."""
        rid = ref["id"] if isinstance(ref, dict) else ref
        for t in self.load()["tasks"]:
            if t["id"] == rid:
                return t
        self.fail("task %s vanished from the store" % rid)

    def mark(self, ref, **fields):
        """Persist field changes onto the stored row."""
        rid = ref["id"] if isinstance(ref, dict) else ref
        store = self.load()
        for t in store["tasks"]:
            if t["id"] == rid:
                t.update(fields)
        ts.save_store(store)

    def load(self):
        store, err = ts.load_store(self.store)
        self.assertIsNone(err)
        return store

    def ondisk(self):
        with open(self.store, "rb") as f:
            return json.loads(f.read().decode("utf-8"))

    def tick(self, snap=None, capacity=None, launcher=None, notifier=None,
             now=NOW, started_at_of=None):
        store = self.load()
        return ts.tick(store, now, snap, capacity, launcher, notifier,
                       started_at_of=started_at_of)

    def with_timer(self):
        d = os.path.join(self.home, ".config", "systemd", "user")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "tendril-schedule.timer"), "w").close()


class StoreTests(Base):
    def test_missing_store_loads_fresh(self):
        store, err = ts.load_store(self.store)
        self.assertIsNone(err)
        self.assertEqual(store["schema_version"], 1)
        self.assertEqual(store["tasks"], [])

    def test_save_tightens_dir_and_file_permissions(self):
        task = self.add()
        os.chmod(self.dir, 0o755)                    # sloppy by hand
        with open(self.store, "w") as f:
            f.write("{}")
        os.chmod(self.store, 0o644)
        ts.save_store(ts.Store(self.store, [task]))
        self.assertEqual(oct(os.stat(self.dir).st_mode & 0o777), oct(0o700))
        self.assertEqual(oct(os.stat(self.store).st_mode & 0o777),
                         oct(0o600))

    def test_corrupt_store_quarantined_and_tick_refused(self):
        os.makedirs(self.dir)
        with open(self.store, "w") as f:
            f.write('{"schema_version":1,"tasks":[oops')
        store, err = ts.load_store(self.store, now=NOW)
        self.assertIsNone(store)
        self.assertEqual(err, ts.E_CORRUPT)
        self.assertFalse(os.path.exists(self.store))
        quarantined = [f for f in os.listdir(self.dir)
                       if f.startswith("schedule.json.corrupt-")]
        self.assertEqual(len(quarantined), 1)
        # a fresh corrupt file also refuses the flock'd tick entry point
        with open(self.store, "w") as f:
            f.write('{"schema_version":1,"tasks":[oops')
        summary, err = ts.run_tick(self.store, now=NOW)
        self.assertIsNone(summary)
        self.assertEqual(err, ts.E_CORRUPT)

    def test_schema_mismatch_refused_not_quarantined(self):
        os.makedirs(self.dir)
        with open(self.store, "w") as f:
            f.write('{"schema_version":2,"tasks":[]}')
        store, err = ts.load_store(self.store, now=NOW)
        self.assertIsNone(store)
        self.assertEqual(err, "schedule store schema 2 not supported")
        self.assertTrue(os.path.exists(self.store))     # NOT quarantined

    def test_save_leaves_no_tmp_litter(self):
        ts.save_store(ts.Store(self.store, [self.add()]))
        self.assertEqual([f for f in os.listdir(self.dir)
                          if f.startswith(".schedule-")], [])

    def test_malformed_task_rows_dropped_alone(self):
        good = self.add()
        doc = self.ondisk()
        doc["tasks"] = [{"id": 7, "state": "WEIRD"}, "junk", 7,
                        doc["tasks"][0]]
        os.makedirs(self.dir, exist_ok=True)
        with open(self.store, "w") as f:
            json.dump(doc, f)
        store, err = ts.load_store(self.store)
        self.assertIsNone(err)
        self.assertEqual([t["id"] for t in store["tasks"]], [good["id"]])


class LockHardeningTests(Base):
    """Security review: a symlinked lock or symlinked state dir must be
    refused with a clear error before any chmod/write, and the symlink
    target left untouched."""

    def test_symlinked_lock_refused_target_untouched(self):
        os.makedirs(self.dir)
        victim = os.path.join(self.home, "victim.lock")
        with open(victim, "w") as f:
            f.write("do not touch")
        os.chmod(victim, 0o644)
        os.symlink(victim, self.store + ".lock")
        with self.assertRaises(OSError) as cm:
            with ts._locked(self.store + ".lock"):
                pass
        self.assertIn("symlink", str(cm.exception))
        self.assertTrue(os.path.islink(self.store + ".lock"))
        with open(victim) as f:
            self.assertEqual(f.read(), "do not touch")
        # the target was never chmod'ed either
        self.assertEqual(oct(os.stat(victim).st_mode & 0o777), oct(0o644))

    def test_symlinked_state_dir_refused(self):
        real = os.path.join(self.home, "real-state")
        os.makedirs(os.path.dirname(self.dir))
        os.makedirs(real)
        os.symlink(real, self.dir)
        with self.assertRaises(OSError) as cm:
            with ts._locked(self.store + ".lock"):
                pass
        self.assertIn("symlink", str(cm.exception))
        self.assertEqual(os.listdir(real), [])     # nothing written through
        self.assertTrue(os.path.islink(self.dir))

    def test_plain_lock_still_locks(self):
        os.makedirs(self.dir)
        with ts._locked(self.store + ".lock"):
            self.assertTrue(os.path.exists(self.store + ".lock"))
        # lock file remains for the next flock cycle and is a plain file
        self.assertTrue(os.path.isfile(self.store + ".lock"))


class AddTests(Base):
    def test_add_at_task_is_queued(self):
        task = self.add()
        self.assertRegex(task["id"], ts.ID_RE)
        self.assertEqual(task["state"], ts.QUEUED)
        self.assertEqual(task["due_at"], DUE)
        self.assertEqual(task["created_at"], int(NOW))
        self.assertIsNone(task["started_at"])
        self.assertIsNone(task["finished_at"])
        self.assertTrue(task["fingerprint"])
        self.assertEqual(task["notified"], {})
        self.assertEqual(self.row(task)["id"], task["id"])

    def test_add_on_reset_with_known_reset_waits_for_reset(self):
        task = self.add(capacity=cap_of("exhausted", RESET_AT),
                        trigger={"kind": "on_reset", "provider": "claude"})
        row = self.row(task)
        self.assertEqual(row["state"], ts.WAITING_FOR_RESET)
        self.assertEqual(row["resets_at"], RESET_AT)
        self.assertEqual(row["due_at"], RESET_AT)

    def test_add_on_reset_unknown_reset_waits_for_capacity(self):
        task = self.add(capacity=cap_of("ready", None),
                        trigger={"kind": "on_reset", "provider": "claude"})
        self.assertEqual(self.row(task)["state"], ts.WAITING_FOR_CAPACITY)

    def test_add_when_available_waits_for_capacity(self):
        task = self.add(trigger={"kind": "when_available",
                                 "provider": "claude"})
        self.assertEqual(self.row(task)["state"], ts.WAITING_FOR_CAPACITY)

    def test_bad_payloads(self):
        not_json = base64.urlsafe_b64encode(b"not json at all") \
            .decode("ascii").rstrip("=")
        not_obj = base64.urlsafe_b64encode(b"[1]").decode("ascii")
        cases = [
            ("malformed payload", "!!not-b64!!"),
            ("malformed payload", "x" * 16385),               # oversize
            ("malformed payload", "abc$"),                # bad charset
            ("payload is not valid base64url", "abcde"),  # bad padding
            ("payload is not valid JSON", not_json),
            ("payload must be a JSON object", not_obj),
            ("unexpected field in payload", b64(payload(note="hello"))),
            ("title must be 1..80 characters", b64(payload(title="y" * 81))),
            ("title must be 1..80 characters", b64(payload(title=""))),
            ("prompt must be 1..8000 characters",
             b64(payload(prompt="z" * 8001))),
            ("prompt must be 1..8000 characters", b64(payload(prompt=""))),
            ("invalid workspace id", b64(payload(workspace="research"))),
            ("invalid workspace id", b64(payload(workspace=""))),
            ("harness is not launchable", b64(payload(harness="bash"))),
            ("harness is not installed", b64(payload(harness="pi"))),
            ("invalid trigger", b64(payload(trigger={"kind": "cron"}))),
            ("invalid trigger", b64(payload(trigger={"kind": "at"}))),
            ("invalid trigger", b64(payload(trigger={"kind": "on_reset"}))),
            ("invalid trigger",
             b64(payload(trigger={"kind": "when_available",
                                  "provider": "CLAUDE"}))),
            ("invalid trigger",
             b64(payload(trigger={"kind": "at", "at": DUE,
                                  "provider": "claude", "extra": 1}))),
        ]
        for want, blob in cases:
            with self.subTest(want=want):
                kw = {"which": lambda k: None, "home": self.home} \
                    if want == "harness is not installed" \
                    else {"home": self.home}
                task, err = ts.decode_add(blob, NOW, **kw)
                self.assertIsNone(task)
                self.assertEqual(err, want)

    def test_harness_must_also_be_installed(self):
        task, err = ts.decode_add(b64(payload()), NOW,
                                  which=lambda k: None, home=self.home)
        self.assertIsNone(task)
        self.assertEqual(err, "harness is not installed")
        installed_harness("pi", self.home)       # canonical-dir fallback
        task, err = ts.decode_add(b64(payload()), NOW,
                                  which=lambda k: None, home=self.home)
        self.assertIsNone(err)
        self.assertEqual(task["harness"], "pi")

    def test_duplicate_fingerprint_within_60s_refused(self):
        first = self.add()
        dup, err = ts.decode_add(b64(payload()), NOW + 5,
                                 which=lambda k: True, home=self.home)
        self.assertIsNone(err)
        _, err = ts.add_task(self.store, dup, NOW + 5)
        self.assertEqual(err, ts.E_DUPLICATE)
        # past the window the same content is a legitimate new task
        _, err = ts.add_task(self.store, dup, NOW + 61)
        self.assertIsNone(err)
        self.assertEqual(len(self.load()["tasks"]), 2)
        self.assertNotEqual(first["id"], self.load()["tasks"][1]["id"])

    def test_same_content_same_second_same_id_refused(self):
        self.add()
        dup, err = ts.decode_add(b64(payload()), NOW,
                                 which=lambda k: True, home=self.home)
        self.assertIsNone(err)
        _, err = ts.add_task(self.store, dup, NOW)
        self.assertEqual(err, ts.E_DUPLICATE)

    def test_add_to_corrupt_store_refused(self):
        self.add()
        with open(self.store, "w") as f:
            f.write("garbage")
        task, err = ts.decode_add(b64(payload(prompt="second")), NOW + 5,
                                  which=lambda k: True, home=self.home)
        self.assertIsNone(err)
        _, err = ts.add_task(self.store, task, NOW + 5)
        self.assertEqual(err, ts.E_CORRUPT)


class CancelTests(Base):
    def test_cancel_queued(self):
        task = self.add()
        done, err = ts.cancel_task(self.store, task["id"], NOW + 10)
        self.assertIsNone(err)
        self.assertEqual(done["state"], ts.CANCELLED)
        self.assertEqual(done["finished_at"], int(NOW + 10))
        self.assertEqual(self.row(task)["state"], ts.CANCELLED)

    def test_cancel_running_refused_with_contract_phrase(self):
        task = self.add()
        self.mark(task, state=ts.RUNNING)
        _, err = ts.cancel_task(self.store, task["id"], NOW)
        self.assertEqual(err, "running — open the session to stop it")
        self.assertEqual(self.row(task)["state"], ts.RUNNING)

    def test_cancel_terminal_refused(self):
        for state in (ts.DONE, ts.FAILED, ts.CANCELLED):
            with self.subTest(state=state):
                task = self.add(prompt="x" + state)
                self.mark(task, state=state)
                _, err = ts.cancel_task(self.store, task["id"], NOW)
                self.assertEqual(err, ts.E_FINISHED)
                self.assertEqual(self.row(task)["state"], state)

    def test_cancel_unknown_id(self):
        _, err = ts.cancel_task(self.store, "t-20261010-00000000", NOW)
        self.assertEqual(err, ts.E_NO_TASK)


class TickTests(Base):
    """Every contract §3 state-machine row."""

    def test_due_at_task_launches(self):
        task = self.add()
        self.assertEqual(task["state"], ts.QUEUED)
        launches = []
        notify, seen = rec_notifier()
        events = self.tick(launcher=rec_launcher(log=launches), notifier=notify)
        task = self.row(task)
        self.assertEqual(task["state"], ts.RUNNING)
        self.assertEqual(launches, [task["id"]])
        self.assertEqual(task["pane_id"], "w15:p9")
        self.assertEqual(task["started_at"], int(NOW))
        self.assertTrue(task["launch_token"])
        self.assertEqual([e["to"] for e in events], [ts.READY, ts.RUNNING])
        self.assertEqual([k for k, _ in seen], ["started"])

    def test_due_at_with_exhausted_provider_waits_for_capacity(self):
        task = self.add(trigger={"kind": "at", "at": DUE,
                                 "provider": "claude"})
        launches = []
        notify, seen = rec_notifier()
        self.tick(capacity=cap_of("exhausted", RESET_AT),
                  launcher=rec_launcher(log=launches), notifier=notify)
        row = self.row(task)
        self.assertEqual(row["state"], ts.WAITING_FOR_CAPACITY)
        self.assertEqual(launches, [])
        # due but exhausted = the one TASK WAITING case; the provider
        # reported, so no reason
        self.assertEqual(seen, [("waiting", task["id"])])
        self.assertIsNone(row.get("reason"))

    def test_future_at_stays_queued(self):
        task = self.add(trigger={"kind": "at", "at": FUTURE})
        events = self.tick(launcher=rec_launcher())
        self.assertEqual(self.row(task)["state"], ts.QUEUED)
        self.assertEqual(events, [])

    def test_waiting_reset_becomes_ready_and_launches(self):
        task = self.add(capacity=cap_of("ready", RESET_AT),
                        trigger={"kind": "on_reset", "provider": "claude"})
        self.assertEqual(task["state"], ts.WAITING_FOR_RESET)
        launches = []
        self.tick(capacity=cap_of("ready", RESET_AT), now=RESET_AT + 1,
                  launcher=rec_launcher(log=launches))
        self.assertEqual(self.row(task)["state"], ts.RUNNING)
        self.assertEqual(launches, [task["id"]])

    def test_waiting_reset_not_ready_before_resets_at(self):
        task = self.add(capacity=cap_of("ready", RESET_AT),
                        trigger={"kind": "on_reset", "provider": "claude"})
        self.tick(capacity=cap_of("ready", RESET_AT), now=RESET_AT - 1,
                  launcher=rec_launcher())
        self.assertEqual(self.row(task)["state"], ts.WAITING_FOR_RESET)

    def test_waiting_reset_stays_while_exhausted(self):
        task = self.add(capacity=cap_of("exhausted", RESET_AT),
                        trigger={"kind": "on_reset", "provider": "claude"})
        self.tick(capacity=cap_of("exhausted", RESET_AT), now=RESET_AT + 1,
                  launcher=rec_launcher())
        self.assertEqual(self.row(task)["state"], ts.WAITING_FOR_RESET)

    def test_waiting_reset_ready_with_no_fresh_sample(self):
        # clock past resets_at + no evidence of a newer 429 → READY
        task = self.add(capacity=cap_of("exhausted", RESET_AT),
                        trigger={"kind": "on_reset", "provider": "claude"})
        self.tick(capacity=lambda pid: None, now=RESET_AT + 1,
                  launcher=rec_launcher())
        self.assertEqual(self.row(task)["state"], ts.RUNNING)

    def test_waiting_reset_refreshes_resets_at_when_later(self):
        task = self.add(capacity=cap_of("exhausted", RESET_AT),
                        trigger={"kind": "on_reset", "provider": "claude"})
        later = RESET_AT + 500
        events = self.tick(capacity=cap_of("exhausted", later),
                           now=RESET_AT + 1, launcher=rec_launcher())
        row = self.row(task)
        self.assertEqual(row["state"], ts.WAITING_FOR_RESET)
        self.assertEqual(row["resets_at"], later)
        self.assertEqual(row["due_at"], later)
        self.assertEqual(events, [])

    def test_waiting_capacity_promotes_on_healthy_states(self):
        for state in ("ready", "pressure", "critical"):
            with self.subTest(state=state):
                task = self.add(prompt="wa-" + state,
                                trigger={"kind": "when_available",
                                         "provider": "claude"})
                self.tick(capacity=cap_of(state), launcher=rec_launcher())
                self.assertEqual(self.row(task)["state"], ts.RUNNING)

    def test_waiting_capacity_stays_unhealthy(self):
        for state in ("exhausted", "unknown"):
            with self.subTest(state=state):
                task = self.add(prompt="wa-" + state,
                                trigger={"kind": "when_available",
                                         "provider": "claude"})
                self.tick(capacity=cap_of(state), launcher=rec_launcher())
                self.assertEqual(self.row(task)["state"],
                                 ts.WAITING_FOR_CAPACITY)
        task = self.add(prompt="no sample",
                        trigger={"kind": "when_available",
                                 "provider": "claude"})
        self.tick(capacity=lambda pid: None, launcher=rec_launcher())
        self.assertEqual(self.row(task)["state"], ts.WAITING_FOR_CAPACITY)

    def test_running_done_when_pane_idle_or_done(self):
        for status in ("idle", "done"):
            with self.subTest(status=status):
                task = self.add(prompt="p-" + status)
                self.tick(launcher=rec_launcher(), snap=None)
                self.tick(snap=snap_of([pane("w15:p9", status)]))
                row = self.row(task)
                self.assertEqual(row["state"], ts.DONE)
                self.assertEqual(row["finished_at"], int(NOW))
                self.assertIsNone(row["failure"])

    def test_running_stays_while_working_or_blocked(self):
        for status in ("working", "blocked", "unknown"):
            with self.subTest(status=status):
                task = self.add(prompt="w-" + status)
                self.tick(launcher=rec_launcher(), snap=None)
                events = self.tick(snap=snap_of([pane("w15:p9", status)]))
                self.assertEqual(self.row(task)["state"], ts.RUNNING)
                self.assertEqual(events, [])

    def test_pane_gone_is_done_session_ended(self):
        task = self.add()
        self.tick(launcher=rec_launcher(), snap=None)
        events = self.tick(snap=snap_of([]))
        row = self.row(task)
        self.assertEqual(row["state"], ts.DONE)
        self.assertEqual(row["failure"], "session ended")

    def test_running_without_pane_after_crash_fails_interrupted(self):
        task = self.add()
        self.tick(launcher=rec_launcher(), snap=None)
        self.mark(task, pane_id=None)      # the crash lost the pane record
        events = self.tick(snap=snap_of([pane("w15:p1", "working")]))
        row = self.row(task)
        self.assertEqual(row["state"], ts.FAILED)
        self.assertEqual(row["failure"], "interrupted")
        self.assertEqual([e["to"] for e in events], [ts.FAILED])

    def test_running_without_snapshot_is_left_alone(self):
        task = self.add()
        self.tick(launcher=rec_launcher(), snap=None)
        events = self.tick(snap=None)
        self.assertEqual(self.row(task)["state"], ts.RUNNING)
        self.assertEqual(events, [])

    def test_terminal_states_are_immutable(self):
        ids = []
        for state in (ts.DONE, ts.FAILED, ts.CANCELLED):
            t = self.add(prompt="t-" + state)
            self.mark(t, state=state)
            ids.append(t["id"])
        launches = []
        notify, seen = rec_notifier()
        events = self.tick(snap=snap_of([]), capacity=cap_of("ready"),
                           launcher=rec_launcher(log=launches), notifier=notify)
        self.assertEqual(events, [])
        self.assertEqual(launches, [])
        self.assertEqual(seen, [])
        self.assertEqual([self.row(i)["state"] for i in ids],
                         [ts.DONE, ts.FAILED, ts.CANCELLED])

    def test_running_plus_token_persisted_before_launch(self):
        self.add()
        seen = {}

        def launch(t):
            disk = self.ondisk()["tasks"][0]
            seen["state"] = disk["state"]
            seen["token"] = disk["launch_token"]
            return True, "w15:p9", None

        self.tick(launcher=launch)
        self.assertEqual(seen["state"], ts.RUNNING)
        self.assertTrue(seen["token"])
        self.assertEqual(self.ondisk()["tasks"][0]["pane_id"], "w15:p9")

    def test_launch_error_phrases(self):
        for err, want in (("workspace missing", "workspace missing"),
                          ("prompt rejected", "prompt rejected"),
                          ("launch refused", "launch refused"),
                          ("herdr exploded", "launch refused")):
            with self.subTest(err=err):
                task = self.add(prompt="e-" + err)
                self.tick(launcher=rec_launcher(ok=False, err=err))
                row = self.row(task)
                self.assertEqual(row["state"], ts.FAILED)
                self.assertEqual(row["failure"], want)
                self.assertEqual(row["finished_at"], int(NOW))

    def test_invalid_pane_from_launcher_is_launch_refused(self):
        task = self.add()
        self.tick(launcher=rec_launcher(ok=True, pane="not a pane"))
        row = self.row(task)
        self.assertEqual(row["state"], ts.FAILED)
        self.assertEqual(row["failure"], "launch refused")

    def test_launcher_exception_cannot_crash_tick(self):
        task = self.add()

        def boom(t):
            raise RuntimeError("boom")
        self.tick(launcher=boom)
        row = self.row(task)
        self.assertEqual(row["state"], ts.FAILED)
        self.assertEqual(row["failure"], "launch refused")


class NotifyTests(Base):
    def test_started_exactly_once(self):
        task = self.add()
        notify, seen = rec_notifier()
        self.tick(launcher=rec_launcher(), notifier=notify)
        self.tick(snap=snap_of([pane("w15:p9", "working")]), notifier=notify)
        self.assertEqual(seen, [("started", task["id"])])
        self.assertEqual(self.row(task)["notified"], {"started": True})

    def test_failed_exactly_once(self):
        task = self.add()
        notify, seen = rec_notifier()
        self.tick(launcher=rec_launcher(ok=False, err="workspace missing"),
                  notifier=notify)
        self.assertEqual(seen, [("failed", task["id"])])
        self.tick(launcher=rec_launcher(), notifier=notify)
        self.assertEqual(seen, [("failed", task["id"])])
        self.assertEqual(self.row(task)["notified"], {"failed": True})

    def test_add_never_pushes_waiting(self):
        # the user just created the task on the phone: an add must not
        # echo it back as a push (Opus revision, contract §3)
        notify, seen = rec_notifier()
        task, err = ts.decode_add(
            b64(payload(trigger={"kind": "on_reset", "provider": "claude"})),
            NOW, capacity=cap_of("exhausted", RESET_AT),
            which=lambda k: True, home=self.home)
        self.assertIsNone(err)
        _, err = ts.add_task(self.store, task, NOW)
        self.assertIsNone(err)
        self.assertEqual(seen, [])
        self.assertEqual(self.ondisk()["tasks"][0]["notified"], {})

    def test_waiting_exactly_once_when_at_due_but_exhausted(self):
        task = self.add(trigger={"kind": "at", "at": DUE,
                                 "provider": "claude"})
        notify, seen = rec_notifier()
        events = self.tick(capacity=cap_of("exhausted", RESET_AT),
                           launcher=rec_launcher(), notifier=notify)
        self.assertEqual(self.row(task)["state"], ts.WAITING_FOR_CAPACITY)
        self.assertEqual(seen, [("waiting", task["id"])])
        self.assertEqual([e["to"] for e in events],
                         [ts.WAITING_FOR_CAPACITY])
        # still exhausted: no duplicate push, no movement
        self.tick(capacity=cap_of("exhausted", RESET_AT),
                  launcher=rec_launcher(), notifier=notify)
        self.assertEqual(seen, [("waiting", task["id"])])
        # recovery: exactly one started, never a second waiting
        self.tick(capacity=cap_of("ready"), launcher=rec_launcher(),
                  notifier=notify, now=NOW + 5)
        self.assertEqual(self.row(task)["state"], ts.RUNNING)
        self.assertEqual(seen, [("waiting", task["id"]),
                                ("started", task["id"])])
        self.assertEqual(self.row(task)["notified"],
                         {"waiting": True, "started": True})


class ReasonTests(Base):
    """`reason`: WAITING_FOR_CAPACITY + provider reports nothing."""

    def wfc(self, provider="claude"):
        return self.add(prompt="wfc-" + provider[:8],
                        trigger={"kind": "when_available",
                                 "provider": provider})

    def test_reason_when_provider_silent(self):
        task = self.wfc()
        self.tick(capacity=lambda pid: None)
        row = self.row(task)
        self.assertEqual(row["state"], ts.WAITING_FOR_CAPACITY)
        self.assertEqual(row["reason"],
                         "claude does not report capacity to this host")

    def test_reason_when_provider_state_unknown(self):
        task = self.wfc()
        self.tick(capacity=cap_of("unknown"))
        self.assertEqual(self.row(task)["reason"],
                         "claude does not report capacity to this host")

    def test_no_reason_while_provider_reports_exhausted(self):
        task = self.wfc()
        self.tick(capacity=cap_of("exhausted"))
        self.assertIsNone(self.row(task).get("reason"))

    def test_no_reason_at_add(self):
        self.assertIsNone(self.row(self.wfc()).get("reason"))

    def test_reason_cleared_on_recovery(self):
        task = self.wfc()
        self.tick(capacity=lambda pid: None)
        self.assertEqual(self.row(task)["reason"],
                         "claude does not report capacity to this host")
        self.tick(capacity=cap_of("ready"), launcher=rec_launcher())
        row = self.row(task)
        self.assertEqual(row["state"], ts.RUNNING)
        self.assertNotIn("reason", row)

    def test_reason_cleared_once_exhaustion_reported(self):
        task = self.wfc()
        self.tick(capacity=lambda pid: None)
        self.tick(capacity=cap_of("exhausted"))
        self.assertIsNone(self.row(task).get("reason"))

    def test_reason_stays_short_for_long_provider_ids(self):
        task = self.wfc(provider="abcdefghijklmnopqrstuvwxyz012345")
        self.tick(capacity=lambda pid: None)
        reason = self.row(task)["reason"]
        self.assertTrue(reason.endswith(
            " does not report capacity to this host"), reason)
        self.assertLessEqual(len(reason), 60)

    def test_reason_in_section_only_while_waiting(self):
        task = self.wfc()
        self.tick(capacity=lambda pid: None)
        self.with_timer()
        rows = ts.snapshot_section(home=self.home)[0]["tasks"]
        self.assertEqual(rows[0]["reason"],
                         "claude does not report capacity to this host")
        self.tick(capacity=cap_of("ready"), launcher=rec_launcher())
        rows = ts.snapshot_section(home=self.home)[0]["tasks"]
        self.assertNotIn("reason", rows[0])


class BodyTests(unittest.TestCase):
    """remote-agents' notification body ('<title> · waiting for
    <Provider> capacity'). Loaded hermetically: HERDR_BIN points at a
    nonexistent path and PATH is stripped, so detection never reaches a
    real herdr client."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="tendril-sched-body-")
        config = os.path.join(cls._tmp.name, "config")
        with open(config, "w") as f:
            f.write("HERDR_BIN=/nonexistent/tendril-test-herdr\n")
        cls._saved = {k: os.environ.get(k) for k in
                      ("HOME", "PATH", "REMOTE_AGENTS_CONFIG")}
        os.environ["HOME"] = cls._tmp.name
        os.environ["PATH"] = "/nonexistent:/usr/bin:/bin"
        os.environ["REMOTE_AGENTS_CONFIG"] = config
        try:
            cls.ra = load_mod("remote_agents_body_under_test", CLI)
        finally:
            for k, v in cls._saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_waiting_body_names_provider(self):
        task = {"title": "Architecture audit",
                "trigger": {"kind": "at", "at": DUE, "provider": "claude"}}
        self.assertEqual(self.ra._schedule_body("waiting", task),
                         "Architecture audit · waiting for Claude capacity")

    def test_started_and_failed_bodies_are_the_title(self):
        task = {"title": "T", "trigger": {"provider": "claude"}}
        self.assertEqual(self.ra._schedule_body("started", task), "T")
        self.assertEqual(self.ra._schedule_body("failed", task), "T")


class SectionTests(Base):
    def finished(self, n, start=1_700_000_000):
        out = []
        for i in range(n):
            t = self.add(prompt="f%d" % i, now=start + i)
            self.mark(t, state=ts.DONE, finished_at=start + i)
            out.append(t)
        return out

    def test_section_fresh_host_with_timer(self):
        self.with_timer()
        section, cap = ts.snapshot_section(home=self.home)
        self.assertEqual(cap, "schedule")
        self.assertTrue(section["available"])
        self.assertEqual(section["tasks"], [])

    def test_section_unavailable_without_timer(self):
        section, cap = ts.snapshot_section(home=self.home)
        self.assertEqual(cap, "schedule")
        self.assertFalse(section["available"])

    def test_section_unavailable_when_store_corrupt(self):
        self.with_timer()
        os.makedirs(self.dir)
        with open(self.store, "w") as f:
            f.write("{corrupt")
        section, _ = ts.snapshot_section(home=self.home)
        self.assertFalse(section["available"])
        self.assertEqual(section["tasks"], [])
        # section reads never quarantine — that is the tick's job
        self.assertTrue(os.path.exists(self.store))

    def test_section_excludes_prompt_and_internals(self):
        task = self.add()
        self.tick(launcher=rec_launcher())
        self.with_timer()
        section, _ = ts.snapshot_section(home=self.home)
        row = section["tasks"][0]
        self.assertEqual(row["id"], task["id"])
        for banned in ("prompt", "fingerprint", "launch_token", "notified",
                       "resets_at"):
            self.assertNotIn(banned, row)
        self.assertEqual(row["state"], ts.RUNNING)
        self.assertEqual(row["pane_id"], "w15:p9")

    def test_section_caps_30_tasks_all_live_plus_newest_terminal(self):
        self.finished(25)
        live = [self.add(prompt="live%d" % i, now=NOW + i) for i in range(10)]
        self.with_timer()
        rows = ts.snapshot_section(home=self.home)[0]["tasks"]
        self.assertLessEqual(len(rows), 30)
        self.assertEqual([t["id"] for t in rows if t["state"] != ts.DONE],
                         [t["id"] for t in reversed(live)])  # ALL, newest first
        self.assertEqual(len(rows), 30)                 # + newest 20 done
        done_ids = [t["id"] for t in rows if t["state"] == ts.DONE]
        by_newest = sorted(self.load()["tasks"],
                           key=lambda t: t.get("finished_at") or 0,
                           reverse=True)
        self.assertEqual(done_ids, [t["id"] for t in by_newest[:20]])

    def test_more_than_30_non_terminal_capped_newest(self):
        for i in range(35):
            self.add(prompt="n%d" % i, now=NOW + i, title="T%d" % i)
        self.with_timer()
        rows = ts.snapshot_section(home=self.home)[0]["tasks"]
        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[0]["title"], "T34")       # newest first


TICKER = textwrap.dedent("""
    import json, os, sys, time
    sys.path.insert(0, os.environ["REPO"] + "/bin")
    import tendril_schedule as ts
    store_path, snap_json = sys.argv[1], sys.argv[2]
    die = os.environ.get("DIE_IN_LAUNCHER") == "1"
    def launcher(task):
        if die:
            os._exit(9)                    # crash AFTER tick's persist step
        with open(os.environ["MARKER"], "a") as f:
            f.write(task["id"] + "\\n")
        time.sleep(0.2)
        return True, "w15:p9", None
    snap = json.loads(snap_json) if snap_json != "none" else None
    summary, err = ts.run_tick(store_path, now=time.time(), snap=snap,
                               capacity=None, launcher=launcher,
                               notifier=lambda k, t: None)
    print(err or "ok")
""")


class SubprocessTickTests(Base):
    def setUp(self):
        super().setUp()
        self.marker = os.path.join(self.home, "launches.log")
        self.ticker = os.path.join(self.home, "ticker.py")
        with open(self.ticker, "w") as f:
            f.write(TICKER)
        installed_harness("pi", self.home)

    def _env(self, **over):
        env = dict(os.environ, REPO=REPO, MARKER=self.marker,
                   XDG_STATE_HOME=os.path.join(self.home, ".local", "state"))
        env.update(over)
        return env

    def add_due_now(self, **over):
        """A task due on the REAL clock (the ticker subprocess injects
        time.time(), not the fixture clock)."""
        return self.add(trigger={"kind": "at", "at": AT_PAST}, **over)

    def test_concurrent_ticks_launch_exactly_once(self):
        self.add_due_now()
        procs = [subprocess.Popen(
            [sys.executable, "-u", self.ticker, self.store, "none"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self._env()) for _ in range(2)]
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
        with open(self.marker) as f:
            self.assertEqual(f.read().count("\n"), 1)
        disk = self.ondisk()["tasks"][0]
        self.assertEqual(disk["state"], ts.RUNNING)
        self.assertEqual(disk["pane_id"], "w15:p9")

    def test_crash_between_persist_and_launch_fails_interrupted(self):
        self.add_due_now()
        r = subprocess.run([sys.executable, "-u", self.ticker, self.store,
                            "none"], capture_output=True, text=True,
                           timeout=60, env=self._env(DIE_IN_LAUNCHER="1"))
        self.assertEqual(r.returncode, 9)
        disk = self.ondisk()["tasks"][0]
        self.assertEqual(disk["state"], ts.RUNNING)
        self.assertTrue(disk["launch_token"])
        self.assertFalse(os.path.exists(self.marker))    # never launched
        # next tick with no such pane: FAILED interrupted, never relaunched
        self.tick(snap=snap_of([pane("w15:p1", "working")]))
        disk = self.ondisk()["tasks"][0]
        self.assertEqual(disk["state"], ts.FAILED)
        self.assertEqual(disk["failure"], "interrupted")
        self.assertFalse(os.path.exists(self.marker))
        self.tick(snap=snap_of([]))                      # still terminal
        self.assertEqual(self.ondisk()["tasks"][0]["state"], ts.FAILED)

    def test_tick_refuses_when_store_corrupt(self):
        os.makedirs(self.dir)
        with open(self.store, "w") as f:
            f.write("{")
        r = subprocess.run([sys.executable, "-u", self.ticker, self.store,
                            "none"], capture_output=True, text=True,
                           timeout=60, env=self._env())
        self.assertEqual(r.returncode, 0)
        self.assertIn("corrupt", r.stdout)


class FakeHerdrCliTests(Base):
    """End-to-end `remote-agents schedule …` against a fake `herdr`."""

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.home, "bin")
        os.makedirs(self.bin, exist_ok=True)
        self.herdr_log = os.path.join(self.home, "herdr-calls.log")
        self.snap_file = os.path.join(self.home, "snap.json")
        self.fail_at = os.path.join(self.home, "fail-at")   # empty = healthy
        with open(self.snap_file, "w") as f:
            json.dump({"result": {"snapshot": {
                "workspaces": [{"workspace_id": "w15", "label": "research",
                                "number": 15, "focused": False}],
                "panes": [pane("w15:p9", "working")],
                "agents": []}}}, f)
        fake = os.path.join(self.bin, "herdr")
        with open(fake, "w") as f:
            f.write("""#!/bin/sh
for _a in "$@"; do printf '%s\t' "$_a" >> "$HERDR_LOG"; done
printf '\n' >> "$HERDR_LOG"
case "$1" in
    --version) echo "herdr 9.9.9-fake"; exit 0 ;;
esac
case "$1,$2" in
    status,*) echo "server: stopped"; exit 0 ;;
    api,snapshot) cat "$HERDR_SNAP"; exit 0 ;;
esac
if [ -s "$HERDR_FAIL" ]; then
    case "$(cat "$HERDR_FAIL"),$1,$2" in
        *,tab,create|*,agent,start|*,agent,prompt) exit 1 ;;
    esac
fi
case "$1,$2" in
    tab,create)
        printf '%s%s\n' '{"id":"x","result":{"pane":' \\
            '{"pane_id":"w15:p9","tab_id":"w15:t9"}}}'
        exit 0 ;;
esac
exit 0
""")
        os.chmod(fake, 0o755)
        installed_harness("pi", self.home)   # canonical-dir install
        config = os.path.join(self.home, "config")
        with open(config, "w") as f:
            f.write("HERDR_BIN=%s\n" % fake)
        self.env = dict(os.environ,
                        HOME=self.home,
                        PATH=self.bin + ":/usr/bin:/bin",
                        XDG_STATE_HOME=os.path.join(self.home, ".local",
                                                    "state"),
                        REMOTE_AGENTS_CONFIG=config,
                        HERDR_LOG=self.herdr_log,
                        HERDR_SNAP=self.snap_file,
                        HERDR_FAIL=self.fail_at,
                        PYTHONDONTWRITEBYTECODE="1")

    def cli(self, *args):
        return subprocess.run([sys.executable, "-u", CLI, "schedule", *args],
                              capture_output=True, text=True, env=self.env,
                              timeout=60)

    def test_cli_add_reply_store_and_duplicate(self):
        r = self.cli("add", b64(payload(trigger={"kind": "at",
                                                 "at": AT_FUTURE})))
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["task"]["state"], "QUEUED")
        self.assertNotIn("prompt", doc["task"])
        self.assertEqual(oct(os.stat(self.store).st_mode & 0o777),
                         oct(0o600))
        r2 = self.cli("add", b64(payload(trigger={"kind": "at",
                                                  "at": AT_FUTURE})))
        self.assertEqual(r2.returncode, 1)
        self.assertEqual(json.loads(r2.stdout),
                         {"ok": False, "error": "duplicate task"})

    def test_cli_add_validation_error_is_one_json_line(self):
        r = self.cli("add", "!!!bad!!!")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout),
                         {"ok": False, "error": "malformed payload"})
        r = self.cli("add", b64(payload(workspace="nope")))
        self.assertEqual(json.loads(r.stdout)["error"],
                         "invalid workspace id")

    def test_cli_list_and_show_prompt_visibility(self):
        added = json.loads(
            self.cli("add", b64(payload(trigger={"kind": "at",
                                                 "at": AT_FUTURE})))
            .stdout)["task"]
        listed = json.loads(self.cli("list").stdout)
        self.assertTrue(listed["ok"])
        self.assertEqual(listed["tasks"][0]["id"], added["id"])
        self.assertNotIn("prompt", listed["tasks"][0])
        listed2 = json.loads(self.cli("list", "--json").stdout)
        self.assertTrue(listed2["ok"])
        shown = json.loads(self.cli("show", added["id"]).stdout)
        self.assertTrue(shown["ok"])
        self.assertEqual(shown["task"]["prompt"],
                         "Audit the architecture and report findings")
        bad = self.cli("show", "t-2026-abcdef")
        self.assertEqual(json.loads(bad.stdout),
                         {"ok": False, "error": "invalid task id"})
        self.assertEqual(bad.returncode, 1)

    def test_cli_existing_target_dispatches_agent_prompt_only(self):
        # the snapshot the fake herdr serves must contain the pane the
        # task targets (add-time lookup) and its agent idle
        with open(self.snap_file, "w") as f:
            json.dump({"result": {"snapshot": {
                "workspaces": [{"workspace_id": "w15", "label": "r",
                                "number": 15, "focused": False}],
                "panes": [pane("w15:p5", "idle")],
                "agents": [pane("w15:p5", "idle")]}}}, f)
        doc = payload(harness=None,
                      target={"kind": "existing", "pane": "w15:p5",
                              "harness": "pi",
                              "agent_started_at": AT_PAST},
                      trigger={"kind": "at", "at": AT_PAST})
        doc.pop("harness", None)
        added = json.loads(self.cli("add", b64(doc)).stdout)
        self.assertTrue(added["ok"], added)
        self.assertEqual(added["task"]["target"], "existing")
        self.assertEqual(added["task"]["state"], "QUEUED")
        r = self.cli("tick")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["tick"]["launched"], 1)
        with open(self.herdr_log) as f:      # skip the --version probe
            calls = [ln.rstrip("\n").rstrip("\t").split("\t")
                     for ln in f if ln.startswith(("tab\t", "agent\t"))]
        # §7.3: the herdr agent-prompt argv ONLY — never a new pane,
        # never agent start
        self.assertEqual(calls,
                         [["agent", "prompt", "w15:p5",
                           "Audit the architecture and report findings",
                           "--wait", "--timeout", "15000"]])
        disk = self.ondisk()["tasks"][0]
        self.assertEqual(disk["state"], "RUNNING")
        self.assertEqual(disk["pane_id"], "w15:p5")

    def test_cli_tick_launches_through_full_herdr_argv_sequence(self):
        added = json.loads(
            self.cli("add", b64(payload(trigger={"kind": "at",
                                                "at": AT_PAST})))
            .stdout)["task"]
        self.assertNotIn("prompt", added)
        r = self.cli("tick")
        self.assertEqual(r.returncode, 0, r.stderr)
        reply = json.loads(r.stdout)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["tick"]["launched"], 1)
        with open(self.herdr_log) as f:      # skip the --version/status
            calls = [ln.rstrip("\n").rstrip("\t").split("\t")   # probes, tab-delimited
                     for ln in f if ln.startswith(("tab\t", "agent\t"))]
        name = "tendril-" + added["id"].split("-")[-1]
        self.assertEqual(calls[0],
                         ["tab", "create", "--workspace", "w15",
                          "--label", name, "--no-focus"])
        self.assertEqual(calls[1],
                         ["agent", "start", name, "--kind", "pi",
                          "--pane", "w15:p9", "--timeout", "30000"])
        self.assertEqual(calls[2],
                         ["agent", "prompt", "w15:p9",
                          "Audit the architecture and report findings",
                          "--wait", "--timeout", "15000"])
        disk = self.ondisk()["tasks"][0]
        self.assertEqual(disk["state"], "RUNNING")
        self.assertEqual(disk["pane_id"], "w15:p9")
        # a second tick: RUNNING is never relaunched
        r2 = self.cli("tick")
        self.assertEqual(json.loads(r2.stdout)["tick"]["launched"], 0)
        with open(self.herdr_log) as f:
            self.assertEqual(
                len([ln for ln in f
                     if ln.startswith(("tab\t", "agent\t"))]), 3)

    def test_cli_tick_reports_contract_failure_phrase(self):
        self.cli("add", b64(payload(trigger={"kind": "at",
                                             "at": AT_PAST})))
        with open(self.fail_at, "w") as f:
            f.write("tab\n")
        r = self.cli("tick")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["tick"]["launched"], 1)
        disk = self.ondisk()["tasks"][0]
        self.assertEqual(disk["state"], "FAILED")
        self.assertEqual(disk["failure"], "workspace missing")

    def test_cli_cancel_running_refused(self):
        added = json.loads(
            self.cli("add", b64(payload(trigger={"kind": "at",
                                                 "at": AT_PAST})))
            .stdout)["task"]
        self.cli("tick")
        r = self.cli("cancel", added["id"])
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout),
                         {"ok": False,
                          "error": "running — open the session to stop it"})
        r = self.cli("cancel", "t-2026-abcdef")
        self.assertEqual(json.loads(r.stdout),
                         {"ok": False, "error": "invalid task id"})

    def test_cli_usage_errors(self):
        r = self.cli("bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: remote-agents schedule", r.stderr)
        r = self.cli("tick", "extra")
        self.assertEqual(r.returncode, 2)


class UnitInstallTests(Base):
    """tendril_host.refresh_schedule with a fake systemctl — the real user
    session is never touched."""

    def setUp(self):
        super().setUp()
        self.pkg = os.path.join(self.home, "pkg")
        os.makedirs(os.path.join(self.pkg, "systemd"))
        for name in (th.SCHEDULE_UNIT, th.SCHEDULE_TIMER):
            with open(os.path.join(self.pkg, "systemd", name), "w") as f:
                f.write("# %s body\n" % name)
        self.user_dir = os.path.join(self.home, ".config", "systemd", "user")
        self.calls = []
        self.active = "active"

    def run_fake(self, args, timeout=6, env=None, socket_stdin=False):
        self.calls.append(args)
        if args[-1] == th.SCHEDULE_TIMER and args[-2] == "is-active":
            return (0 if self.active == "active" else 3), self.active, ""
        return 0, "", ""

    def refresh(self, which_bins=("systemctl",), dry=False, python=None):
        out = io.StringIO()
        rc = th.refresh_schedule(self.pkg, run=self.run_fake, dry_run=dry,
                                 out=out, home_dir=self.home,
                                 which=lambda n: n in set(which_bins),
                                 python=python)
        return rc, out.getvalue()

    def test_install_then_idempotent(self):
        rc, out = self.refresh()
        self.assertEqual(rc, 0)
        for name in (th.SCHEDULE_UNIT, th.SCHEDULE_TIMER):
            self.assertIn("installed %s" % name, out)
            with open(os.path.join(self.user_dir, name)) as f:
                self.assertEqual(f.read(), "# %s body\n" % name)
        self.assertEqual(self.calls,
                         [["systemctl", "--user", "daemon-reload"],
                          ["systemctl", "--user", "enable", "--now",
                           th.SCHEDULE_TIMER],
                          ["systemctl", "--user", "is-active",
                           th.SCHEDULE_TIMER]])   # state report (§7.4)
        self.calls = []
        rc, out = self.refresh()
        self.assertEqual(rc, 0)
        self.assertIn("unchanged", out)
        # healthy install: one read-only is-active probe, nothing else
        self.assertEqual(self.calls,
                         [["systemctl", "--user", "is-active",
                           th.SCHEDULE_TIMER]])

    def test_dry_run_writes_nothing(self):
        rc, out = self.refresh(dry=True)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.user_dir))
        self.assertEqual(self.calls, [])
        self.assertIn("[dry-run]", out)

    def test_content_change_triggers_reload(self):
        self.refresh()
        self.calls = []
        with open(os.path.join(self.user_dir, th.SCHEDULE_TIMER), "a") as f:
            f.write("# drifted\n")
        rc, out = self.refresh()
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls,
                         [["systemctl", "--user", "daemon-reload"],
                          ["systemctl", "--user", "enable", "--now",
                           th.SCHEDULE_TIMER],
                          ["systemctl", "--user", "is-active",
                           th.SCHEDULE_TIMER]])

    def test_inactive_timer_reenabled_without_change(self):
        self.refresh()
        self.calls = []
        self.active = "inactive"
        rc, out = self.refresh()
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls,
                         [["systemctl", "--user", "is-active",
                           th.SCHEDULE_TIMER],
                          ["systemctl", "--user", "enable", "--now",
                           th.SCHEDULE_TIMER],
                          ["systemctl", "--user", "is-active",
                           th.SCHEDULE_TIMER]])
        self.assertIn("was inactive", out)

    def test_launchd_host_is_documented_noop(self):
        rc, out = self.refresh(which_bins=("launchctl",))
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.user_dir))
        self.assertEqual(self.calls, [])
        self.assertIn("launchd host", out)
        self.assertIn("docs/MACOS.md", out)

    def test_launchd_wins_even_when_a_systemctl_spy_exists(self):
        # the darwin-sim install tests put a recording systemctl stub on
        # PATH; launchd must still win (backend()'s precedence)
        rc, out = self.refresh(which_bins=("launchctl", "systemctl"))
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.user_dir))
        self.assertEqual(self.calls, [])
        self.assertIn("launchd host", out)

    def test_no_systemctl_noop(self):
        rc, out = self.refresh(which_bins=())
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.user_dir))
        self.assertEqual(self.calls, [])

    def test_missing_source_unit_fails_cleanly(self):
        os.remove(os.path.join(self.pkg, "systemd", th.SCHEDULE_UNIT))
        rc, out = self.refresh()
        self.assertEqual(rc, 1)
        self.assertIn("cannot read", out)

    def test_python_interpreter_is_templated_into_the_unit(self):
        """Security review: the tick unit must run the install's resolved
        $PYTHON (like the watcher unit does), not a hardcoded
        /usr/bin/python3; the template keeps the refresh idempotent."""
        with open(os.path.join(self.pkg, "systemd", th.SCHEDULE_UNIT),
                  "w") as f:
            f.write("[Service]\nType=oneshot\n"
                    "ExecStart=/usr/bin/python3 %h/.local/bin/remote-agents"
                    " schedule tick\n")
        rc, out = self.refresh(python="/opt/py/bin/python3")
        self.assertEqual(rc, 0)
        with open(os.path.join(self.user_dir, th.SCHEDULE_UNIT)) as f:
            body = f.read()
        self.assertIn("ExecStart=/opt/py/bin/python3 %h/.local/bin"
                      "/remote-agents schedule tick", body)
        self.assertNotIn("/usr/bin/python3", body)
        # the timer has no ExecStart and passes through untouched
        with open(os.path.join(self.user_dir, th.SCHEDULE_TIMER)) as f:
            self.assertEqual(f.read(), "# %s body\n" % th.SCHEDULE_TIMER)
        # same python again → unchanged (the rendered body is compared)
        self.calls = []
        rc, out = self.refresh(python="/opt/py/bin/python3")
        self.assertEqual(rc, 0)
        self.assertIn("unchanged", out)


class UnitFileTests(unittest.TestCase):
    def test_timer_directives_per_contract(self):
        with open(os.path.join(REPO, "systemd",
                               "tendril-schedule.timer")) as f:
            body = f.read()
        self.assertIn("OnBootSec=45s", body)
        self.assertIn("OnUnitActiveSec=30s", body)
        self.assertIn("Persistent=true", body)
        self.assertIn("AccuracySec=5s", body)
        self.assertIn("Unit=tendril-schedule.service", body)
        self.assertIn("WantedBy=timers.target", body)

    def test_service_is_oneshot_tick(self):
        with open(os.path.join(REPO, "systemd",
                               "tendril-schedule.service")) as f:
            body = f.read()
        self.assertIn("Type=oneshot", body)
        self.assertIn("remote-agents schedule tick", body)
        # security-review hardening (safe: herdr's socket lives in
        # ~/.config/herdr, sd_notify's under $XDG_RUNTIME_DIR — neither
        # is /tmp; the tick's writes go to ~/.cache + ~/.local/state)
        self.assertIn("NoNewPrivileges=true", body)
        self.assertIn("PrivateTmp=true", body)


class ExistingTargetTests(Base):
    """§7.3: target existing{pane, harness, agent_started_at?} — strict
    schema at add, pane/harness/started_at re-validation at launch, the
    busy/blocked retry bound, and pane-reuse detection."""

    PANE = "w15:p5"
    STARTED = NOW - 3600

    def lookup_of(self, row=None):
        rows = {self.PANE: row} if row is not None else {}
        def lookup(pane):
            return rows.get(pane)
        return lookup

    def live_row(self, status="idle", agent="pi", started=None):
        return {"pane_id": self.PANE, "agent": agent,
                "agent_status": status, "workspace_id": "w15",
                "started_at": self.STARTED if started is None else started}

    def target(self, **over):
        t = {"kind": "existing", "pane": self.PANE, "harness": "pi",
             "agent_started_at": self.STARTED}
        t.update(over)
        return t

    def add_existing(self, target=None, lookup=None, **over):
        doc = payload(harness=None, target=self.target() if target is None
                      else target, **over)
        doc.pop("harness", None)               # §7.3: harness rides on target
        task, err = ts.decode_add(b64(doc), NOW, capacity=cap_of(),
                                  which=lambda k: True, home=self.home,
                                  lookup=self.lookup_of(self.live_row())
                                  if lookup is None else lookup)
        return task, err

    def test_existing_target_happy_path_launches_into_the_pane(self):
        task, err = self.add_existing()
        self.assertIsNone(err)
        self.assertEqual(task["target"], self.target())
        saved, err = ts.add_task(self.store, task, NOW)
        self.assertIsNone(err)
        log = []
        ev = self.tick(snap=snap_of([self.live_row()]),
                       launcher=rec_launcher(pane=self.PANE, log=log))
        row = self.row(saved)
        self.assertEqual(row["state"], ts.RUNNING)
        self.assertEqual(row["pane_id"], self.PANE)
        self.assertEqual(log, [row["id"]])     # dispatched, not re-created
        self.assertNotIn("target_wait_since", row)

    def test_target_kind_is_echoed_and_prompt_never_leaks(self):
        task, _ = self.add_existing()
        saved, _ = ts.add_task(self.store, task, NOW)
        doc = ts.project(self.row(saved))
        self.assertEqual(doc["target"], "existing")
        self.assertNotIn("prompt", doc)
        self.assertNotIn("pane", doc["target"] if "target" in doc
                         else doc)
        plain, _ = ts.decode_add(b64(payload()), NOW, which=lambda k: True,
                                 home=self.home)
        self.assertEqual(ts.project(plain)["target"], "new")

    def test_busy_defers_then_fails_session_busy_after_bound(self):
        task, _ = self.add_existing()
        saved, _ = ts.add_task(self.store, task, NOW)
        log = []
        ev = self.tick(snap=snap_of([self.live_row("working")]),
                       launcher=rec_launcher(pane=self.PANE, log=log))
        row = self.row(saved)
        self.assertEqual(row["state"], ts.READY)   # stays READY, no launch
        self.assertEqual(log, [])
        self.assertEqual(row["target_wait_since"], int(NOW))
        # retried each tick within the bound
        self.tick(snap=snap_of([self.live_row("working")]), now=NOW + 600,
                  launcher=rec_launcher(pane=self.PANE, log=log))
        self.assertEqual(self.row(saved)["state"], ts.READY)
        self.assertEqual(log, [])
        # past 15 min still working → FAILED session busy
        self.tick(snap=snap_of([self.live_row("working")]), now=NOW + 901,
                  launcher=rec_launcher(pane=self.PANE, log=log))
        row = self.row(saved)
        self.assertEqual(row["state"], ts.FAILED)
        self.assertEqual(row["failure"], "session busy")

    def test_blocked_never_dispatches_then_fails_session_needs_input(self):
        task, _ = self.add_existing()
        saved, _ = ts.add_task(self.store, task, NOW)
        log = []
        self.tick(snap=snap_of([self.live_row("blocked")]),
                  launcher=rec_launcher(pane=self.PANE, log=log))
        self.assertEqual(self.row(saved)["state"], ts.READY)
        self.tick(snap=snap_of([self.live_row("blocked")]), now=NOW + 901,
                  launcher=rec_launcher(pane=self.PANE, log=log))
        row = self.row(saved)
        self.assertEqual(row["state"], ts.FAILED)
        self.assertEqual(row["failure"], "session needs input")
        self.assertEqual(log, [])

    def test_pane_reuse_started_at_guard_fails_session_changed(self):
        task, _ = self.add_existing()
        saved, _ = ts.add_task(self.store, task, NOW)
        # the pane now hosts a NEWER agent (pane id reused)
        self.tick(snap=snap_of([self.live_row("idle",
                                              started=self.STARTED + 5)]),
                  launcher=rec_launcher(pane=self.PANE),
                  started_at_of=lambda row: row.get("started_at"))
        row = self.row(saved)
        self.assertEqual(row["state"], ts.FAILED)
        self.assertEqual(row["failure"], "session changed")

    def test_pane_gone_fails_session_changed(self):
        task, _ = self.add_existing()
        saved, _ = ts.add_task(self.store, task, NOW)
        self.tick(snap=snap_of([]), launcher=rec_launcher(pane=self.PANE))
        row = self.row(saved)
        self.assertEqual(row["state"], ts.FAILED)
        self.assertEqual(row["failure"], "session changed")

    def test_harness_change_at_launch_fails_session_changed(self):
        task, _ = self.add_existing()
        saved, _ = ts.add_task(self.store, task, NOW)
        self.tick(snap=snap_of([self.live_row("idle", agent="claude")]),
                  launcher=rec_launcher(pane=self.PANE))
        self.assertEqual(self.row(saved)["state"], ts.FAILED)
        self.assertEqual(self.row(saved)["failure"], "session changed")

    def test_malformed_targets_rejected(self):
        bad = ["no", 17, {}, {"kind": "other"},
               {"kind": "new", "pane": "w15:p5"},
               {"kind": "existing", "pane": "p5", "harness": "pi"},
               {"kind": "existing", "pane": "w15:p5; rm", "harness": "pi"},
               {"kind": "existing", "pane": "w15:p5", "harness": "tcsh"},
               {"kind": "existing", "pane": "w15:p5", "harness": "pi",
                "agent_started_at": 12},
               {"kind": "existing", "pane": "w15:p5", "harness": "pi",
                "agent_started_at": True},
               {"kind": "existing", "pane": "w15:p5", "harness": "pi",
                "extra": 1},
               ]
        for t in bad:
            _, err = self.add_existing(target=t)
            self.assertEqual(err, ts.E_TARGET, t)
        # a launchable harness that the pane does not host: schema-valid,
        # refused by the live lookup
        _, err = self.add_existing(
            target={"kind": "existing", "pane": self.PANE,
                    "harness": "codex"})
        self.assertEqual(err, ts.E_HARNESS_MISMATCH)
        # a top-level harness that contradicts the target harness
        doc = payload(harness="pi", target=self.target(harness="claude"))
        _, err = ts.decode_add(b64(doc), NOW, which=lambda k: True,
                               home=self.home,
                               lookup=self.lookup_of(self.live_row()))
        self.assertEqual(err, ts.E_TARGET)

    def test_add_time_validation_needs_a_live_lookup(self):
        _, err = self.add_existing(lookup=self.lookup_of(None))
        self.assertEqual(err, ts.E_PANE)
        _, err = self.add_existing(
            lookup=self.lookup_of(self.live_row(agent="claude")))
        self.assertEqual(err, ts.E_HARNESS_MISMATCH)
        other_ws = self.live_row()
        other_ws["workspace_id"] = "w16"
        _, err = self.add_existing(lookup=self.lookup_of(other_ws))
        self.assertEqual(err, ts.E_PANE)
        _, err = self.add_existing(
            lookup=self.lookup_of(self.live_row(started=self.STARTED + 1)))
        self.assertEqual(err, ts.E_SESSION_CHANGED)
        # no lookup injected at all: nothing verifiable → refused
        doc = payload(target=self.target())
        doc.pop("harness", None)
        _, err = ts.decode_add(b64(doc), NOW, which=lambda k: True,
                               home=self.home, lookup=None)
        self.assertEqual(err, ts.E_PANE)

    def test_duplicate_fingerprint_distinguishes_targets(self):
        task, _ = self.add_existing()
        plain, _ = ts.decode_add(b64(payload()), NOW, which=lambda k: True,
                                 home=self.home)
        self.assertNotEqual(task["fingerprint"], plain["fingerprint"])


class ReconcileTests(Base):
    """§7.3 snapshot-side reconcile: RUNNING → DONE on idle/done after
    dispatch or pane gone; non-blocking flock skips when the tick holds
    it; never launches, never fails."""

    def add(self, now=NOW, **over):
        """add with a permissive live lookup (the pane is believed to
        exist; guards are exercised in ExistingTargetTests)."""
        doc = payload(**over)

        def lookup(p):
            return {"agent": doc.get("harness") or "pi",
                    "workspace_id": doc.get("workspace", "w15"),
                    "status": "idle", "started_at": None}
        task, err = ts.decode_add(b64(doc), now, capacity=cap_of(),
                                  which=lambda k: True, home=self.home,
                                  lookup=lookup)
        self.assertIsNone(err)
        saved, err = ts.add_task(self.store, task, now)
        self.assertIsNone(err)
        return saved

    def test_running_done_when_pane_idle_and_when_gone(self):
        self.with_timer()
        saved = self.add(target={"kind": "existing", "pane": "w15:p5",
                                 "harness": "pi"})
        self.tick(snap=snap_of([pane("w15:p5", "idle")]),
                  launcher=rec_launcher(pane="w15:p5"))
        self.assertEqual(self.row(saved)["state"], ts.RUNNING)
        n = ts.reconcile(self.store, NOW + 30,
                         snap_of([pane("w15:p5", "idle")]))
        self.assertEqual(n, 1)
        row = self.row(saved)
        self.assertEqual(row["state"], ts.DONE)
        self.assertIsNone(row.get("failure"))
        # pane gone after a fresh dispatch → DONE session ended
        saved2 = self.add(now=NOW + 60,
                          target={"kind": "existing", "pane": "w15:p6",
                                  "harness": "pi"})
        self.tick(snap=snap_of([pane("w15:p6", "idle")]), now=NOW + 60,
                  launcher=rec_launcher(pane="w15:p6"))
        self.assertEqual(self.row(saved2)["state"], ts.RUNNING)
        n = ts.reconcile(self.store, NOW + 90, snap_of([]))
        self.assertEqual(n, 1)
        row = self.row(saved2)
        self.assertEqual(row["state"], ts.DONE)
        self.assertEqual(row["failure"], "session ended")

    def test_never_launched_running_is_left_for_the_tick(self):
        self.with_timer()
        saved = Base.add(self)
        self.mark(saved, state=ts.RUNNING, pane_id=None)   # crash window
        n = ts.reconcile(self.store, NOW + 30, snap_of([]))
        self.assertEqual(n, 0)
        self.assertEqual(self.row(saved)["state"], ts.RUNNING)

    def test_lock_busy_skips_whole(self):
        self.with_timer()
        saved = self.add(target={"kind": "existing", "pane": "w15:p5",
                                 "harness": "pi"})
        self.tick(snap=snap_of([pane("w15:p5", "idle")]),
                  launcher=rec_launcher(pane="w15:p5"))
        with ts._locked(self.store + ".lock"):     # the tick holds it
            n = ts.reconcile(self.store, NOW + 30,
                             snap_of([pane("w15:p5", "idle")]))
        self.assertEqual(n, 0)
        self.assertEqual(self.row(saved)["state"], ts.RUNNING)

    def test_snapshot_section_reconciles_when_given_snap(self):
        self.with_timer()
        saved = self.add(target={"kind": "existing", "pane": "w15:p5",
                                 "harness": "pi"})
        self.tick(snap=snap_of([pane("w15:p5", "idle")]),
                  launcher=rec_launcher(pane="w15:p5"))
        sec, cap = ts.snapshot_section(now=NOW + 30, home=self.home,
                                       snap=snap_of([pane("w15:p5",
                                                          "idle")]))
        self.assertEqual(cap, "schedule")
        row = self.row(saved)
        self.assertEqual(row["state"], ts.DONE)
        done = [t for t in sec["tasks"] if t["id"] == row["id"]]
        self.assertEqual(done[0]["state"], ts.DONE)


if __name__ == "__main__":
    unittest.main()
