"""Herdr health & recovery: bin/tendril_health.py + the guardian (§6).

LINK-CONTRACT-0.2 §6 under test here:
- `health --json`: one bounded JSON line — Herdr state
  (running|down|stopped|unknown), tendril install state, and the
  notify/scheduler/guardian unit states; NO secrets, paths, usernames or
  pids; reachability is deliberately ABSENT (that is the phone's
  classification, §6.3);
- the guardian tick: 2-observation debounce, exactly ONE
  `TENDRIL · HERDR OFFLINE` per incident, at most ONE
  `TENDRIL · HERDR RESTORED` per incident, intentional stop ⇒ no incident
  and no notification, unknown observation changes nothing, corrupt state
  file quarantined and recovered, guardian NEVER restarts anything;
- the recovery verbs: restart refuses the guard ("Stopped on purpose"),
  refuses when already running+answering, rate limits to one attempt per
  30 s (flock + timestamp), starts through the supported headless
  lifecycle (systemd-run user unit first, detached child fallback; argv
  only) and VERIFIES (status running AND `herdr api snapshot` ≤ 10 s);
  stop writes the guard BEFORE stopping; start clears the guard;
- phone verbs `health` / `herdr restart`: exact argv, BatchMode +
  ConnectTimeout=6, every other argument shape refused locally (exit 2,
  NOTHING dialed — injection vectors included);
- guardian unit install idempotent with a fake systemctl; launchd and
  systemctl-less hosts are documented no-ops; --python rendering.

Clock, probe, api, starter, stopper and notifier are fakes; HOME/state is
a tempdir; the CLI end-to-end runs against a fake `herdr` and a fake
`herdr-notify` on PATH (tempdir HOME) — the real server, watcher, systemd
session and ntfy are never touched.
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEALTH = os.path.join(REPO, "bin", "tendril_health.py")
HOST = os.path.join(REPO, "bin", "tendril_host.py")
CLI = os.path.join(REPO, "bin", "remote-agents")
LAUNCHER = os.path.join(REPO, "phone", "tendril")


def load_mod(name, path):
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


th = load_mod("tendril_health_under_test", HEALTH)
thost = load_mod("tendril_host_under_test", HOST)

NOW = 1_800_000_000.0


class FakeClock:
    """One clock for both domains: epoch stamps and the verify deadline."""

    def __init__(self, start=NOW):
        self.t = float(start)

    def epoch(self):
        return self.t

    def mono(self):
        return self.t

    def sleep(self, d):
        self.t += float(d)


def scripted_probe(states, version="0.9.9"):
    """probe() popping canned states (last one repeats)."""
    seq = list(states)

    def probe():
        return (seq.pop(0) if seq else states[-1]), version
    return probe


def rec_notifier():
    calls = []

    def notify(kind, title, body):
        calls.append((kind, title, body))
    return notify, calls


# ---------------------------------------------------------------- guard
def _install_test_guard(home):
    """Failing stubs for every binary that could touch the LIVE herdr
    server, the user systemd manager or the notification watcher —
    prepended to PATH in every test process. A test that ever reaches one
    fails loudly (exit 122) instead of silently mutating the real host:
    the transient tendril-herdr-* units from the first review round must
    never recur."""
    d = os.path.join(home, "test-guard-bin")
    os.makedirs(d, exist_ok=True)
    for name in ("systemd-run", "systemctl", "herdr", "herdr-notify"):
        with open(os.path.join(d, name), "w") as f:
            f.write("#!/bin/sh\n"
                    "echo 'TENDRIL-TEST GUARD: real %s invoked:' \"$@\" >&2\n"
                    "exit 122\n" % name)
        os.chmod(os.path.join(d, name), 0o755)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tendril-health-test-")
        guard_dir = _install_test_guard(self.home)
        patcher = mock.patch.dict(os.environ,
                                  {"HOME": self.home,
                                   "XDG_STATE_HOME": os.path.join(
                                       self.home, ".local", "state"),
                                   "PATH": guard_dir + os.pathsep
                                           + os.environ.get("PATH", "")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.p = th.paths(self.home)

    def touch(self, key):
        """Create an empty state-dir file (the dir may not exist yet)."""
        os.makedirs(os.path.dirname(self.p[key]), exist_ok=True)
        open(self.p[key], "w").close()

    def state(self):
        with open(self.p["state"]) as f:
            return json.load(f)


# ------------------------------------------------------------- probes
class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tendril-health-probe-")
        guard_dir = _install_test_guard(self.home)
        patcher = mock.patch.dict(
            os.environ, {"PATH": guard_dir + os.pathsep
                         + os.environ.get("PATH", "")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def run_fake(self, out, rc=0):
        return lambda args, timeout: (rc, out, "")

    def test_running_json(self):
        state, version = th.probe_herdr(
            self.run_fake('{"status":"running","running":true,'
                          '"version":"0.9.3"}'), "/fake/herdr")
        self.assertEqual((state, version), ("running", "0.9.3"))

    def test_down_json(self):
        state, version = th.probe_herdr(
            self.run_fake('{"status":"stopped","running":false,'
                          '"version":"0.9.3"}'), "/fake/herdr")
        self.assertEqual((state, version), ("down", "0.9.3"))

    def test_timeout_is_unknown_never_blocks(self):
        state, version = th.probe_herdr(
            self.run_fake("", rc=124), "/fake/herdr", timeout=0.01)
        self.assertEqual((state, version), ("unknown", None))

    def test_garbage_is_unknown(self):
        state, _ = th.probe_herdr(self.run_fake("not json"), "/fake/herdr")
        self.assertEqual(state, "unknown")

    def test_bad_version_never_leaves(self):
        state, version = th.probe_herdr(
            self.run_fake('{"running":true,"version":"a b/c $(x)"}'),
            "/fake/herdr")
        self.assertEqual((state, version), ("running", None))

    def test_no_client_is_unknown(self):
        state, version = th.probe_herdr(self.run_fake("", rc=127),
                                        "/fake/herdr")
        self.assertEqual((state, version), ("unknown", None))

    def test_no_client_at_all_is_unknown(self):
        state, version = th.probe_herdr(self.run_fake("", rc=0), None)
        self.assertEqual((state, version), ("unknown", None))

    def test_api_alive(self):
        self.assertTrue(th.api_alive(self.run_fake("{}"), "/fake/herdr"))
        self.assertFalse(th.api_alive(self.run_fake("", rc=124),
                                      "/fake/herdr"))
        self.assertFalse(th.api_alive(self.run_fake("{}"), None))


# --------------------------------------------------------- health doc
class HealthDocTests(Base):
    def test_reachable_and_healthy(self):
        doc = th.health_doc(host="omarchy", herdr="/fake/herdr",
                            run=lambda a, t: (0, '{"running":true,'
                                                '"version":"0.9.3"}', ""),
                            home=self.home, now=NOW,
                            units={"notify": "active",
                                   "scheduler": "active",
                                   "guardian": "active"})
        self.assertEqual(doc["herdr"]["state"], "running")
        self.assertEqual(doc["herdr"]["version"], "0.9.3")
        self.assertEqual(doc["tendril"]["state"], "healthy")
        self.assertEqual(doc["notify"], {"state": "active"})
        self.assertEqual(doc["scheduler"], {"state": "active"})
        self.assertEqual(doc["guardian"], {"state": "active"})
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["generated_at"], int(NOW))

    def test_herdr_dead_reads_down(self):
        doc = th.health_doc(host="omarchy", herdr="/fake/herdr",
                            run=lambda a, t: (0, '{"running":false}', ""),
                            home=self.home, now=NOW,
                            units={"notify": "absent",
                                   "scheduler": "absent",
                                   "guardian": "absent"})
        self.assertEqual(doc["herdr"]["state"], "down")
        self.assertEqual(doc["notify"], {"state": "absent"})
        self.assertNotIn("version", doc["herdr"])   # never fabricate

    def test_components_are_state_objects_from_the_allowed_set(self):
        # §6.1: notify/scheduler/guardian/tendril are OBJECTS {"state": …}
        # with words from their own closed vocabulary — never bare strings.
        allowed = {"tendril": {"healthy", "degraded"},
                   "notify": {"active", "inactive", "failed", "absent"},
                   "scheduler": {"active", "inactive", "absent"},
                   "guardian": {"active", "inactive", "absent"}}
        doc = th.health_doc(host="h", herdr="/fake/herdr",
                            run=lambda a, t: (0, '{"running":true}', ""),
                            home=self.home, now=NOW,
                            units={"notify": "failed",
                                   "scheduler": "inactive",
                                   "guardian": "active"})
        for key, states in allowed.items():
            self.assertIsInstance(doc[key], dict, key)
            self.assertIn(doc[key].get("state"), states, key)
            self.assertEqual(set(doc[key]), {"state"}, key)
        # a missing unit word degrades to the designed "absent"
        doc = th.health_doc(host="h", herdr="/fake/herdr",
                            run=lambda a, t: (0, '{"running":true}', ""),
                            home=self.home, now=NOW,
                            units={})
        for key in ("notify", "scheduler", "guardian"):
            self.assertEqual(doc[key], {"state": "absent"})

    def test_unknown_stays_unknown(self):
        doc = th.health_doc(host="h", herdr="/fake/herdr",
                            run=lambda a, t: (124, "", "timeout"),
                            home=self.home, now=NOW,
                            units={"notify": "inactive",
                                   "scheduler": "absent",
                                   "guardian": "absent"})
        self.assertEqual(doc["herdr"]["state"], "unknown")

    def test_guard_sets_stopped_and_state_file_fills_since(self):
        self.touch("guard")
        th.save_state(self.p["state"],
                      {"state": "stopped", "since": 1791582900,
                       "last_healthy_at": 1791582880})
        doc = th.health_doc(host="h", herdr="/fake/herdr",
                            run=lambda a, t: (0, '{"running":true}', ""),
                            home=self.home, now=NOW,
                            units={"notify": "active", "scheduler": "absent",
                                   "guardian": "absent"})
        self.assertEqual(doc["herdr"]["state"], "stopped")
        self.assertEqual(doc["herdr"]["since"], 1791582900)
        self.assertEqual(doc["herdr"]["last_healthy_at"], 1791582880)

    def test_host_reachability_is_the_phones_concern(self):
        doc = th.health_doc(host="h", herdr="/fake/herdr",
                            run=lambda a, t: (0, '{"running":true}', ""),
                            home=self.home, now=NOW,
                            units={"notify": "active", "scheduler": "active",
                                   "guardian": "inactive"})
        self.assertEqual(set(doc), {"version", "host", "generated_at",
                                    "herdr", "tendril", "notify",
                                    "scheduler", "guardian"})
        self.assertLessEqual(set(doc["herdr"]),
                             {"state", "last_healthy_at", "since",
                              "version"})
        dump = json.dumps(doc)
        for word in ("reachable", "unreachable", "transport", "ssh",
                     "mosh", "latency", "ping"):
            self.assertNotIn(word, dump)

    def test_no_paths_usernames_or_pids(self):
        doc = th.health_doc(host="omarchy", herdr="/fake/herdr",
                            run=lambda a, t: (0, '{"running":true,'
                                                '"version":"0.9.3"}', ""),
                            home=self.home, now=NOW,
                            units={"notify": "active", "scheduler": "active",
                                   "guardian": "active"})

        def walk(node):
            if isinstance(node, dict):
                for v in node.values():
                    yield from walk(v)
            elif isinstance(node, list):
                for v in node:
                    yield from walk(v)
            elif isinstance(node, str):
                yield node
        for value in walk(doc):
            self.assertNotIn("/", value)          # no paths anywhere
            self.assertNotIn(self.home, value)
            self.assertNotIn(str(os.getpid()), value)
        dump = json.dumps(doc)
        import getpass
        self.assertNotIn(self.home, dump)
        with mock.patch.dict(os.environ):
            user = getpass.getuser()
        self.assertNotIn(user, dump)

    def test_unit_states_mapping(self):
        unit_dir = os.path.join(self.home, ".config", "systemd", "user")
        os.makedirs(unit_dir)
        for _key, unit in (("notify", "herdr-notify.service"),
                           ("scheduler", "tendril-schedule.timer"),
                           ("guardian", "tendril-guardian.timer")):
            with open(os.path.join(unit_dir, unit), "w") as f:
                f.write("# unit\n")
        cases = [((0, "active\ninactive\nactive\n", ""),
                  {"notify": "active", "scheduler": "inactive",
                   "guardian": "active"}),
                 ((3, "failed\ninactive\n", ""),
                  {"notify": "failed", "scheduler": "inactive",
                   "guardian": "inactive"}),
                 ((0, "", ""),
                  {"notify": "inactive", "scheduler": "inactive",
                   "guardian": "inactive"})]
        for (rc, out, err), expected in cases:
            states = th.unit_states(run=lambda a, t, _rc=rc, _o=out:
                                    (_rc, _o, ""), home=self.home)
            self.assertEqual(states, expected)

    def test_unit_states_absent_without_files(self):
        states = th.unit_states(run=lambda a, t: (0, "", ""), home=self.home)
        self.assertEqual(states, {"notify": "absent", "scheduler": "absent",
                                  "guardian": "absent"})

    def test_unit_states_without_systemctl_degrade_to_inactive(self):
        unit_dir = os.path.join(self.home, ".config", "systemd", "user")
        os.makedirs(unit_dir)
        with open(os.path.join(unit_dir, "herdr-notify.service"), "w") as f:
            f.write("# unit\n")
        states = th.unit_states(run=lambda a, t: (127, "", "not found"),
                                home=self.home)
        self.assertEqual(states, {"notify": "inactive",
                                  "scheduler": "absent",
                                  "guardian": "absent"})


# ------------------------------------------------------ guardian tick
class TickTests(Base):
    def tick(self, states, notifier=None, guard=False, now=NOW):
        if guard:
            self.touch("guard")
        return th.run_tick(host="omarchy", herdr="/fake/herdr",
                           run=lambda a, t: (0, '{"running":false}', ""),
                           home=self.home, now=now,
                           probe=scripted_probe(states), notifier=notifier)

    def test_debounce_needs_two_down_observations(self):
        notify, calls = rec_notifier()
        s = self.tick(["down"], notify)
        self.assertEqual(s["state"], "down")
        self.assertEqual(calls, [])                       # one is not enough
        s = self.tick(["down"], notify)
        self.assertEqual(calls, [("offline", th.HERDR_OFFLINE_TITLE,
                                  "Host omarchy is reachable, but Herdr "
                                  "stopped.")])
        self.assertTrue(s["incident_id"].startswith("h-"))
        self.assertEqual(self.state()["notified_down"], True)

    def test_exactly_one_offline_per_incident(self):
        notify, calls = rec_notifier()
        self.tick(["down"], notify)
        self.tick(["down"], notify)
        self.tick(["down"], notify)
        self.tick(["down"], notify)
        self.assertEqual(len(calls), 1)

    def test_restored_once_then_incident_closes(self):
        notify, calls = rec_notifier()
        self.tick(["down"], notify)
        self.tick(["down"], notify)
        self.assertEqual(len(calls), 1)
        s = self.tick(["running"], notify)
        self.assertEqual(calls[1][0], "restored")
        self.assertEqual(calls[1][1], th.HERDR_RESTORED_TITLE)
        self.assertEqual(s["state"], "running")
        st = self.state()
        self.assertNotIn("incident_id", st)
        self.assertEqual(st["notified_down"], False)
        # a second running tick: nothing further, last_healthy_at refreshes
        s = self.tick(["running"], notify, now=NOW + 60)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.state()["last_healthy_at"], int(NOW + 60))

    def test_single_down_then_running_never_notifies(self):
        notify, calls = rec_notifier()
        self.tick(["running"], notify)
        self.tick(["down"], notify)       # streak 1: below the debounce
        self.tick(["running"], notify)    # recovers: no incident ever
        self.assertEqual(calls, [])

    def test_intentional_stop_no_incident_no_notification(self):
        notify, calls = rec_notifier()
        s = self.tick(["down"], notify, guard=True)
        self.assertEqual(s["state"], "stopped")
        self.assertEqual(calls, [])
        st = self.state()
        self.assertNotIn("incident_id", st)
        self.assertNotIn("notified_down", st)
        # and it stays silent for as long as the guard is set
        self.tick(["down"], notify, guard=True)
        self.tick(["down"], notify, guard=True)
        self.assertEqual(calls, [])

    def test_stop_during_incident_then_start_restores_once(self):
        notify, calls = rec_notifier()
        self.tick(["down"], notify)
        self.tick(["down"], notify)
        self.assertEqual(len(calls), 1)
        self.tick(["down"], notify, guard=True)   # user stops it on purpose
        self.assertEqual(len(calls), 1)           # still silent
        os.unlink(self.p["guard"])                # tendril herdr start
        self.tick(["running"], notify)
        self.assertEqual([c[0] for c in calls], ["offline", "restored"])
        self.tick(["running"], notify)
        self.assertEqual(len(calls), 2)

    def test_unknown_observation_changes_nothing(self):
        notify, calls = rec_notifier()
        self.tick(["running"], notify)
        before = self.state()
        s = self.tick(["unknown"], notify)
        self.assertEqual(s["state"], "running")
        after = self.state()
        self.assertEqual(before["since"], after["since"])
        self.assertEqual(before["last_healthy_at"], after["last_healthy_at"])
        self.assertEqual(calls, [])

    def test_malformed_state_file_recovered(self):
        notify, calls = rec_notifier()
        os.makedirs(os.path.dirname(self.p["state"]), exist_ok=True)
        with open(self.p["state"], "w") as f:
            f.write('{"state": "down", TRASH')
        s = self.tick(["down"], notify)
        self.assertEqual(s["state"], "down")
        self.assertEqual(calls, [])      # fresh state: streak restarts at 1
        self.tick(["down"], notify)
        self.assertEqual(len(calls), 1)
        quarantined = [n for n in os.listdir(os.path.dirname(self.p["state"]))
                       if n.startswith("herdr-health.json.corrupt-")]
        self.assertEqual(len(quarantined), 1)

    def test_future_schema_fields_are_dropped_safely(self):
        th.save_state(self.p["state"],
                      {"state": "down", "since": "x",
                       "last_healthy_at": -5, "incident_id": "../evil",
                       "notified_down": "yes", "down_streak": 10 ** 9})
        self.tick(["down"])
        st = self.state()
        self.assertEqual(st["down_streak"], 1)     # sanitized, not trusted

    def test_guardian_never_starts_anything(self):
        # the tick takes no starter/stopper: a probe result can only move
        # the state file — assert the summary never claims a start
        s = self.tick(["down"])
        self.assertEqual(sorted(s), ["incident_id", "notified", "state"])


# ------------------------------------------------- guarded lifecycle
class RestartTests(Base):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.api_true = lambda: True

    def verb(self, verb, states, starter=None, stopper=None, api=None):
        started, stop_calls = [], []

        def default_starter():
            started.append(self.clock.t)
            return True, None
        starter = starter or default_starter
        if stopper is None:
            def stopper():
                stop_calls.append(os.path.isfile(self.p["guard"]))
                return True
        if api is None:
            api = lambda: True
        elif isinstance(api, list):
            seq = list(api)
            api = lambda: (seq.pop(0) if seq else seq[-1]) if seq else True
        return th.herdr_verb(verb, host="omarchy", herdr="/fake/herdr",
                             run=lambda a, t: (0, '{"running":false}', ""),
                             home=self.home,
                             probe=scripted_probe(states),
                             api=api,
                             starter=starter, stopper=stopper,
                             epoch=self.clock.epoch, mono=self.clock.mono,
                             sleep=self.clock.sleep), started, stop_calls

    def test_restart_success_is_verified(self):
        doc, started, _ = self.verb("restart", ["down", "down", "running"])
        self.assertEqual(doc, {"ok": True, "herdr": {"state": "running"}})
        self.assertEqual(len(started), 1)

    def test_restart_starter_failure(self):
        doc, started, _ = self.verb("restart", ["down"],
                                    starter=lambda: (False, "boom"))
        self.assertEqual(doc, {"ok": False, "error": "boom"})
        self.assertEqual(started, [])

    def test_restart_verification_failure_after_start(self):
        doc, started, _ = self.verb("restart", ["down"])
        self.assertEqual(doc, {"ok": False, "error": th.E_NOUP})
        self.assertEqual(len(started), 1)
        # retried inside the budget, and the budget is respected
        self.assertLessEqual(self.clock.t - NOW, th.VERIFY_BUDGET_S + 1)
        self.assertGreaterEqual(self.clock.t - NOW, th.VERIFY_BUDGET_S - 2)

    def test_restart_refused_while_intentional_stop(self):
        self.touch("guard")
        doc, started, _ = self.verb("restart", ["down"])
        self.assertEqual(doc["ok"], False)
        self.assertEqual(doc["error"], th.E_GUARD)
        self.assertEqual(started, [])

    def test_restart_refused_when_already_running(self):
        doc, started, _ = self.verb("restart", ["running"])
        self.assertEqual(doc, {"ok": False, "error": th.E_RUNNING})
        self.assertEqual(started, [])

    def test_restart_proceeds_when_running_but_not_answering(self):
        # status says running but the socket is dead: that is exactly the
        # crash the verb exists for — no "already running" refusal
        doc, started, _ = self.verb("restart",
                                    ["running", "down", "running"],
                                    api=[False, True])
        self.assertEqual(doc["ok"], True)
        self.assertEqual(len(started), 1)

    def test_rate_limit_one_attempt_per_30s(self):
        doc, _, _ = self.verb("restart", ["down"])       # fails verify,
        self.assertEqual(doc["ok"], False)               # stamps the attempt
        with open(self.p["stamp"]) as f:
            self.assertEqual(int(f.read()), int(NOW))
        doc, started, _ = self.verb("restart", ["down", "running"])
        self.assertEqual(doc["ok"], False)
        self.assertTrue(doc["error"].startswith("Rate limited"))
        self.assertEqual(started, [])                    # nothing re-started
        # after the window a new attempt goes through
        self.clock.t += 31
        doc, started, _ = self.verb("restart", ["down", "running"])
        self.assertEqual(doc["ok"], True)
        self.assertEqual(len(started), 1)

    def test_stop_writes_guard_then_stops(self):
        order = []

        def stopper():
            order.append(("stopper", os.path.isfile(self.p["guard"])))
            return True
        doc, _, _ = self.verb("stop", ["running"], stopper=stopper)
        self.assertEqual(doc, {"ok": True, "herdr": {"state": "stopped"}})
        # guard existed BEFORE the stopper ran (§6.1 ordering)
        self.assertEqual(order, [("stopper", True)])
        self.assertTrue(os.path.isfile(self.p["guard"]))

    def test_stop_keeps_guard_even_when_stop_fails(self):
        doc, _, _ = self.verb("stop", ["running"],
                              stopper=lambda: False)
        self.assertEqual(doc, {"ok": False, "error": th.E_STOP})
        self.assertTrue(os.path.isfile(self.p["guard"]))

    def test_start_clears_guard_then_starts_and_verifies(self):
        self.touch("guard")
        doc, started, _ = self.verb("start", ["down", "running"])
        self.assertEqual(doc, {"ok": True, "herdr": {"state": "running"}})
        self.assertEqual(len(started), 1)
        self.assertFalse(os.path.isfile(self.p["guard"]))

    def test_start_when_already_running_only_clears_guard(self):
        self.touch("guard")
        doc, started, _ = self.verb("start", ["running"])
        self.assertEqual(doc["ok"], True)
        self.assertEqual(started, [])            # nothing to start
        self.assertFalse(os.path.isfile(self.p["guard"]))

    def test_start_is_not_rate_limited(self):
        # a stop/start cycle inside the window is the host user's explicit
        # recovery: the 30 s lock applies to restart only (§6.1)
        th._atomic_write(self.p["stamp"], b"%d" % int(NOW))
        doc, started, _ = self.verb("start", ["down", "running"])
        self.assertEqual(doc["ok"], True)
        self.assertEqual(len(started), 1)


# ------------------------------------------------------- starter argv
class StarterTests(Base):
    def test_systemd_run_first_with_exact_argv(self):
        calls = []

        def run(args, timeout):
            calls.append(args)
            return 0, "", ""
        starter = th.make_starter("/fake/herdr", run=run,
                                  which=lambda n: n == "systemd-run",
                                  popen=None)
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1"}):
            ok, phrase = starter()
        self.assertEqual((ok, phrase), (True, None))
        argv = calls[0]
        self.assertEqual(argv[:3], ["systemd-run", "--user", "--unit"])
        self.assertRegex(argv[3], r"^tendril-herdr-[0-9a-f]{8}$")
        self.assertEqual(argv[4:], ["--collect", "/fake/herdr", "server"])

    def test_falls_back_to_detached_child_when_unit_refused(self):
        spawned = []

        class FakePopen:
            def __call__(self, args, **kw):
                spawned.append((args, kw))
                return object()

        def run(args, timeout):
            return 1, "", "unit exists"          # systemd-run refuses
        starter = th.make_starter("/fake/herdr", run=run,
                                  which=lambda n: n == "systemd-run",
                                  popen=FakePopen())
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1"}):
            ok, _ = starter()
        self.assertTrue(ok)
        args, kw = spawned[0]
        self.assertEqual(args, ["/fake/herdr", "server"])
        self.assertTrue(kw.get("start_new_session"))
        self.assertIs(kw.get("stdin"), subprocess.DEVNULL)

    def test_detached_child_when_no_systemd(self):
        spawned = []

        class FakePopen:
            def __call__(self, args, **kw):
                spawned.append(args)
                return object()
        starter = th.make_starter("/fake/herdr", run=lambda a, t: (0, "", ""),
                                  which=lambda n: False,
                                  popen=FakePopen())
        ok, _ = starter()
        self.assertTrue(ok)
        self.assertEqual(spawned, [["/fake/herdr", "server"]])

    def test_no_client_is_an_honest_failure(self):
        starter = th.make_starter(None, which=lambda n: False)
        self.assertEqual(starter(), (False, th.E_NOHERDR))

    def test_stopper_argv(self):
        calls = []
        stopper = th.make_stopper("/fake/herdr",
                                  run=lambda a, t: (calls.append(a), 0, "", "")[1:])
        self.assertTrue(stopper())
        self.assertEqual(calls, [["/fake/herdr", "server", "stop"]])


# ------------------------------------------------- phone verbs (§6.3)
SSH_STUB = ('case " $*" in *" true") exit 0 ;; esac\n'
            'printf \'ssh %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'
            'exit "${TENDRIL_SSH_RC:-0}"\n')


class PhoneVerbTests(Base):
    def setUp(self):
        super().setUp()
        self.bindir = os.path.join(self.home, "bin")
        os.makedirs(self.bindir)
        with open(os.path.join(self.bindir, "ssh"), "w") as f:
            f.write("#!/bin/sh\n" + SSH_STUB)
        os.chmod(os.path.join(self.bindir, "ssh"), 0o755)
        self.record = os.path.join(self.home, "record")
        self.env = dict(os.environ)
        self.env.update({"PATH": self.bindir + ":" + self.env["PATH"],
                         "TENDRIL_RECORD": self.record,
                         "TENDRIL_ALIAS": "omarchy",
                         "HOME": self.home})

    def dial(self, *argv):
        r = subprocess.run(["sh", LAUNCHER, *argv], env=self.env,
                           capture_output=True, text=True, timeout=20)
        lines = []
        if os.path.isfile(self.record):
            with open(self.record) as f:
                lines = f.read().splitlines()
        return r.returncode, lines

    def test_health_fixed_remote_command(self):
        rc, lines = self.dial("health")
        self.assertEqual(rc, 0)
        self.assertEqual(lines, ["ssh -o BatchMode=yes -o ConnectTimeout=6 "
                                 "omarchy remote-agents health --json"])

    def test_health_with_explicit_alias(self):
        rc, lines = self.dial("myhost", "health")
        self.assertEqual(rc, 0)
        self.assertEqual(lines, ["ssh -o BatchMode=yes -o ConnectTimeout=6 "
                                 "myhost remote-agents health --json"])

    def test_herdr_restart_fixed_remote_command(self):
        rc, lines = self.dial("herdr", "restart")
        self.assertEqual(rc, 0)
        self.assertEqual(lines, ["ssh -o BatchMode=yes -o ConnectTimeout=6 "
                                 "omarchy remote-agents herdr restart"])

    def test_herdr_restart_with_explicit_alias(self):
        rc, lines = self.dial("myhost", "herdr", "restart")
        self.assertEqual(rc, 0)
        self.assertEqual(lines, ["ssh -o BatchMode=yes -o ConnectTimeout=6 "
                                 "myhost remote-agents herdr restart"])

    def test_refusals_never_dial(self):
        for argv in (("health", "extra"),
                     ("health", "--json"),
                     ("health", "'; rm -rf ~"),
                     ("health", "`boom`"),
                     ("health", "a b"),
                     ("health", "$(x)"),
                     ("herdr",),
                     ("herdr", "stop"),          # host-side only
                     ("herdr", "start"),         # host-side only
                     ("herdr", "restart", "extra"),
                     ("herdr", "status"),
                     ("herdr", "restart'; reboot"),
                     ("myhost", "herdr", "restart", "extra"),
                     ("myhost", "health", "x")):
            rc, lines = self.dial(*argv)
            self.assertEqual(rc, 2, argv)
            self.assertEqual(lines, [], argv)    # NOTHING went on the wire


# --------------------------------------- systemd units + install/refresh
def fake_systemd_setup(test, names):
    pkg = os.path.join(test.home, "pkg")
    os.makedirs(os.path.join(pkg, "systemd"))
    for name in names:
        if name.endswith(".service"):
            body = ("[Service]\nType=oneshot\nExecStart=/usr/bin/python3 "
                    "%h/.local/bin/remote-agents health tick\n")
        else:
            body = ("[Timer]\nOnBootSec=60s\nOnUnitActiveSec=30s\n"
                    "Unit=%s\n" % name.replace(".timer", ".service"))
        with open(os.path.join(pkg, "systemd", name), "w") as f:
            f.write(body)
    test.pkg = pkg
    test.user_dir = os.path.join(test.home, ".config", "systemd", "user")
    test.calls = []
    test.active = "active"

    def run_fake(args, timeout=6, env=None, socket_stdin=False):
        test.calls.append(args)
        if args[-1] == thost.GUARDIAN_TIMER and args[-2:-1] == ["is-active"]:
            return (0 if test.active == "active" else 3), test.active, ""
        return 0, "", ""
    test.run_fake = run_fake


class UnitInstallTests(Base):
    def refresh(self, which_bins=("systemctl",), dry=False, python=None):
        out = io.StringIO()
        rc = thost.refresh_guardian(self.pkg, run=self.run_fake,
                                    dry_run=dry, out=out,
                                    home_dir=self.home,
                                    which=lambda n: n in set(which_bins),
                                    python=python)
        return rc, out.getvalue()

    def setUp(self):
        super().setUp()
        fake_systemd_setup(self, (thost.GUARDIAN_UNIT, thost.GUARDIAN_TIMER))

    def test_install_then_idempotent(self):
        rc, out = self.refresh()
        self.assertEqual(rc, 0)
        with open(os.path.join(self.user_dir, thost.GUARDIAN_UNIT)) as f:
            self.assertIn("health tick", f.read())
        with open(os.path.join(self.user_dir, thost.GUARDIAN_TIMER)) as f:
            self.assertIn("OnUnitActiveSec=30s", f.read())
        self.assertEqual(self.calls,
                         [["systemctl", "--user", "daemon-reload"],
                          ["systemctl", "--user", "enable", "--now",
                           thost.GUARDIAN_TIMER]])
        self.calls = []
        rc, out = self.refresh()
        self.assertEqual(rc, 0)
        self.assertIn("unchanged", out)
        self.assertEqual(self.calls,
                         [["systemctl", "--user", "is-active",
                           thost.GUARDIAN_TIMER]])

    def test_dry_run_writes_nothing(self):
        rc, out = self.refresh(dry=True)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.user_dir))
        self.assertEqual(self.calls, [])
        self.assertIn("[dry-run]", out)

    def test_content_change_triggers_reload(self):
        self.refresh()
        self.calls = []
        with open(os.path.join(self.user_dir, thost.GUARDIAN_TIMER), "a") as f:
            f.write("# drifted\n")
        rc, out = self.refresh()
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls,
                         [["systemctl", "--user", "daemon-reload"],
                          ["systemctl", "--user", "enable", "--now",
                           thost.GUARDIAN_TIMER]])

    def test_inactive_timer_reenabled_without_change(self):
        self.refresh()
        self.calls = []
        self.active = "inactive"
        rc, out = self.refresh()
        self.assertEqual(rc, 0)
        self.assertIn("was inactive - enabled", out)
        self.assertEqual(self.calls,
                         [["systemctl", "--user", "is-active",
                           thost.GUARDIAN_TIMER],
                          ["systemctl", "--user", "enable", "--now",
                           thost.GUARDIAN_TIMER]])

    def test_launchd_host_is_a_noop(self):
        rc, out = self.refresh(which_bins=("launchctl",))
        self.assertEqual((rc, self.calls), (0, []))
        self.assertIn("launchd host", out)

    def test_no_systemctl_is_a_noop(self):
        rc, out = self.refresh(which_bins=())
        self.assertEqual((rc, self.calls), (0, []))
        self.assertIn("no systemctl", out)

    def test_python_rendering_swaps_execstart(self):
        rc, _ = self.refresh(python="/opt/bin/python3.11")
        self.assertEqual(rc, 0)
        with open(os.path.join(self.user_dir, thost.GUARDIAN_UNIT)) as f:
            body = f.read()
        self.assertIn("ExecStart=/opt/bin/python3.11 %h/.local/bin/",
                      body)
        with open(os.path.join(self.user_dir, thost.GUARDIAN_TIMER)) as f:
            self.assertNotIn("ExecStart", f.read())

    def test_missing_pkg_unit_fails_cleanly(self):
        os.unlink(os.path.join(self.pkg, "systemd", thost.GUARDIAN_UNIT))
        rc, out = self.refresh()
        self.assertEqual(rc, 1)
        self.assertIn("cannot read", out)


class RepoUnitShapeTests(unittest.TestCase):
    """The shipped units carry the §6.2 timer contract and the tick."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tendril-health-units-")
        guard_dir = _install_test_guard(self.home)
        patcher = mock.patch.dict(
            os.environ, {"PATH": guard_dir + os.pathsep
                         + os.environ.get("PATH", "")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def test_guardian_service_runs_the_tick(self):
        with open(os.path.join(REPO, "systemd",
                               "tendril-guardian.service")) as f:
            body = f.read()
        self.assertIn("ExecStart=/usr/bin/python3 %h/.local/bin/"
                      "remote-agents health tick", body)
        self.assertIn("NoNewPrivileges=true", body)

    def test_guardian_timer_cadence(self):
        with open(os.path.join(REPO, "systemd",
                               "tendril-guardian.timer")) as f:
            body = f.read()
        self.assertIn("OnBootSec=60s", body)
        self.assertIn("OnUnitActiveSec=30s", body)
        self.assertIn("Unit=tendril-guardian.service", body)


# ----------------------------------------------------- CLI end-to-end
FAKE_HERDR = """#!/bin/sh
# fake herdr: state flips through $HERDR_FAKE_STATE (0=down 1=running)
STATE_FILE="${HERDR_FAKE_STATE}"
running() {
    [ "$(cat "$STATE_FILE" 2>/dev/null)" = 1 ]
}
cmd="$1"; [ "$#" -gt 0 ] && shift
case "$cmd" in
  --version)
      echo "herdr 0.9.9"; exit 0 ;;
  status)
      if [ "$1" = "server" ]; then
          if running; then
              echo '{"status":"running","running":true,"version":"0.9.9"}'
          else
              echo '{"status":"stopped","running":false,"version":"0.9.9"}'
          fi
      else
          # the plain `herdr status` the client detection probes
          echo "client: herdr 0.9.9"
          running && echo "status: running" && echo "endpoint_compatible: yes"
      fi
      exit 0 ;;
  api)
      if [ "$1" = "snapshot" ]; then echo '{}'; exit 0; fi
      exit 64 ;;
  server)
      if [ "$1" = "stop" ]; then echo 0 > "$STATE_FILE"; exit 0; fi
      if [ -n "$HERDR_FAKE_NOSTART" ]; then exit 0; fi
      echo 1 > "$STATE_FILE"                      # the headless daemon
      exit 0 ;;
esac
echo "usage: fake herdr" >&2; exit 64
"""

FAKE_NOTIFY = '''#!/usr/bin/env python3
# fake herdr-notify: same module surface as bin/herdr-notify (§3 wiring)
import json, os
ENV_FILE = os.environ.get("FAKE_NOTIFY_ENV")
RECORD = os.environ.get("FAKE_NOTIFY_RECORD")


def load_env():
    cfg = {}
    try:
        with open(ENV_FILE) as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return cfg


def configured(cfg):
    return bool(cfg.get("NTFY_URL")) and bool(cfg.get("NTFY_TOPIC"))


def send(cfg, title, body, priority, tags, click=None, actions=None):
    with open(RECORD, "a") as f:
        f.write(json.dumps({"title": title, "body": body,
                            "priority": priority, "tags": tags,
                            "click": click}) + "\\n")
    return 0
'''


class CliTests(Base):
    """The real remote-agents verbs against a fake herdr + fake
    herdr-notify on PATH (tempdir HOME)."""

    def setUp(self):
        super().setUp()
        self.bindir = os.path.join(self.home, ".local", "bin")
        os.makedirs(self.bindir)
        self.fake_state = os.path.join(self.home, "fake-herdr-state")
        with open(os.path.join(self.bindir, "herdr"), "w") as f:
            f.write(FAKE_HERDR)
        os.chmod(os.path.join(self.bindir, "herdr"), 0o755)
        with open(os.path.join(self.bindir, "herdr-notify"), "w") as f:
            f.write(FAKE_NOTIFY)
        os.chmod(os.path.join(self.bindir, "herdr-notify"), 0o755)
        # systemd-run exists on real Linux hosts; the CLI tests must never
        # reach the LIVE user manager, so the fake PATH carries a stub that
        # refuses — the starter then takes the detached-Popen fallback,
        # which inherits this process env (the fake's state file variable).
        with open(os.path.join(self.bindir, "systemd-run"), "w") as f:
            f.write("#!/bin/sh\necho 'systemd-run: no user bus' >&2\nexit 1\n")
        os.chmod(os.path.join(self.bindir, "systemd-run"), 0o755)
        self.notify_record = os.path.join(self.home, "notify-record")
        self.notify_env = os.path.join(self.home, "notify.env")
        with open(self.notify_env, "w") as f:
            f.write('NTFY_URL="https://ntfy.sh"\nNTFY_TOPIC="topic-x"\n')
        conf = os.path.join(self.home, ".config", "remote-agents")
        os.makedirs(conf)
        with open(os.path.join(conf, "notify.env"), "w") as f:
            f.write('NTFY_URL="https://ntfy.sh"\nNTFY_TOPIC="topic-x"\n')
        self.env = dict(os.environ)
        self.env.update({"HOME": self.home,
                         "XDG_STATE_HOME": os.path.join(self.home, ".local",
                                                        "state"),
                         "PATH": self.bindir + ":" + self.env["PATH"],
                         "HERDR_FAKE_STATE": self.fake_state,
                         "FAKE_NOTIFY_ENV": self.notify_env,
                         "FAKE_NOTIFY_RECORD": self.notify_record,
                         "TENDRIL_TEST_HERDR_DOWN": ""})

    def cli(self, *argv):
        return subprocess.run([CLI, *argv], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def set_herdr(self, running):
        with open(self.fake_state, "w") as f:
            f.write("1" if running else "0")

    def notifications(self):
        if not os.path.isfile(self.notify_record):
            return []
        with open(self.notify_record) as f:
            return [json.loads(ln) for ln in f.read().splitlines() if ln]

    def test_health_json_one_line_no_paths(self):
        self.set_herdr(True)
        r = self.cli("health", "--json")
        self.assertEqual(r.returncode, 0)
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        doc = json.loads(lines[0])
        self.assertEqual(doc["herdr"]["state"], "running")
        self.assertEqual(doc["herdr"]["version"], "0.9.9")
        for key in ("tendril", "notify", "scheduler", "guardian"):
            self.assertIsInstance(doc[key], dict, key)   # §6.1 objects
            self.assertIn(doc[key].get("state"),
                          {"active", "inactive", "failed", "absent",
                           "healthy", "degraded"}, key)
        self.assertNotIn(self.home, lines[0])
        self.assertNotIn(str(os.getpid()), lines[0])

    def test_health_json_herdr_down(self):
        self.set_herdr(False)
        r = self.cli("health", "--json")
        doc = json.loads(r.stdout.strip())
        self.assertEqual(doc["herdr"]["state"], "down")

    def test_usage_errors(self):
        for argv, code in ((("health",), 2), (("health", "--json", "x"), 2),
                           (("health", "tick", "x"), 2), (("herdr",), 2),
                           (("herdr", "reboot"), 2),
                           (("herdr", "restart", "x"), 2)):
            r = self.cli(*argv)
            self.assertEqual(r.returncode, code, argv)

    def test_tick_end_to_end_debounce_and_notify(self):
        self.set_herdr(False)
        r = self.cli("health", "tick")           # down #1: silent
        self.assertEqual(r.returncode, 0)
        doc = json.loads(r.stdout.strip())
        self.assertEqual(doc["ok"], True)
        self.assertEqual(doc["tick"]["state"], "down")
        self.assertEqual(self.notifications(), [])
        r = self.cli("health", "tick")           # down #2: ONE offline
        doc = json.loads(r.stdout.strip())
        self.assertEqual(doc["tick"]["notified"], ["offline"])
        notes = self.notifications()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["title"], th.HERDR_OFFLINE_TITLE)
        self.assertTrue(notes[0]["body"].startswith("Host "))
        self.assertIsNone(notes[0]["click"])     # the app, never a pane
        r = self.cli("health", "tick")           # down #3: still one
        self.assertEqual(self.notifications(), notes)
        self.set_herdr(True)
        r = self.cli("health", "tick")           # restored once
        notes = self.notifications()
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[1]["title"], th.HERDR_RESTORED_TITLE)
        r = self.cli("health", "tick")           # never a second one
        self.assertEqual(len(self.notifications()), 2)

    def test_herdr_restart_end_to_end(self):
        self.set_herdr(False)
        r = self.cli("herdr", "restart")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.strip()),
                         {"ok": True, "herdr": {"state": "running"}})
        # the fake daemon flipped the state: a re-probe sees running
        r = self.cli("health", "--json")
        self.assertEqual(json.loads(r.stdout.strip())["herdr"]["state"],
                         "running")

    def test_herdr_restart_rate_limited_end_to_end(self):
        self.set_herdr(False)
        env = dict(self.env, HERDR_FAKE_NOSTART="1")
        r = subprocess.run([CLI, "herdr", "restart"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)        # attempt #1 fails + stamps
        r = self.cli("herdr", "restart")         # attempt #2: rate limited
        self.assertEqual(r.returncode, 1)
        self.assertTrue(json.loads(r.stdout.strip())["error"]
                        .startswith("Rate limited"))

    def test_herdr_stop_then_restart_refused_then_start(self):
        self.set_herdr(True)
        r = self.cli("herdr", "stop")
        self.assertEqual(json.loads(r.stdout.strip()),
                         {"ok": True, "herdr": {"state": "stopped"}})
        self.assertTrue(os.path.isfile(self.p["guard"]))
        self.assertEqual(json.loads(r.stdout.strip())["ok"], True)
        # guardian stays silent about an intentional stop
        r = self.cli("health", "tick")
        self.assertEqual(json.loads(r.stdout.strip())["tick"]["state"],
                         "stopped")
        self.assertEqual(self.notifications(), [])
        # the phone's revive is refused by the guard
        r = self.cli("herdr", "restart")
        self.assertEqual(json.loads(r.stdout.strip())["error"], th.E_GUARD)
        # the host user's start clears it and revives
        r = self.cli("herdr", "start")
        self.assertEqual(json.loads(r.stdout.strip()),
                         {"ok": True, "herdr": {"state": "running"}})
        self.assertFalse(os.path.isfile(self.p["guard"]))

    def test_capabilities_include_health(self):
        # _optional_sections registers `health` when the module is present
        loader = SourceFileLoader("remote_agents_health_check", CLI)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        ra = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"HOME": self.home,
                                          "XDG_STATE_HOME": os.path.join(
                                              self.home, ".local", "state")}):
            loader.exec_module(ra)
        extra, caps = ra._optional_sections()
        self.assertIn("health", caps)


if __name__ == "__main__":
    unittest.main()
