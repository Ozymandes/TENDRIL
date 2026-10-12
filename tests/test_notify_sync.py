"""TENDRIL · SYNC — the D11 freshness invalidation event (OPT-IN ONLY).

The notify watcher's parent-state digest is the host's only push-visible
"the snapshot just changed" signal for live transitions (working/idle/done/
blocked, agents appearing or vanishing). A SYNC is a MIN-priority ntfy
message the phone never surfaces: its receiver answers with one debounced
snapshot refresh (contract §7.6). It is OFF BY DEFAULT and only for
SELF-HOSTED ntfy servers: on public ntfy.sh the anonymous daily quota
(~250 msgs/visitor) belongs to the REAL needs-you/finished alerts, and a
busy workspace's state flaps would burn it within the hour. The app's 2 s
foreground cadence covers freshness without any publish. Rules tested:

- the digest is order-independent and derives ONLY from (pane_id, status);
- the first sighting is a silent baseline (a watcher restart never pings);
- with NTFY_SYNC explicitly =1: a digest change sends exactly one SYNC,
  rate-limited to one per SYNC_MIN_INTERVAL; a change inside the window is
  remembered ("pending") and flushed at the boundary; a failed send stamps
  nothing, so the next poll retries;
- with the DEFAULT config a burst of parent-state changes produces ZERO
  extra publishes — byte-identical to the pre-D11 finished/input/nudge set;
- an unconfigured watcher or allow_push=False never publishes either.
"""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "bin")
sys.path.insert(0, BIN)

_loader = importlib.machinery.SourceFileLoader(
    "herdr_notify_sync_test", os.path.join(BIN, "herdr-notify"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
hn = importlib.util.module_from_spec(_spec)
_loader.exec_module(hn)

CFG = {"NTFY_SYNC": "1", "NTFY_TOPIC": "t", "NTFY_URL": "http://ntfy.test"}


def snap(*rows):
    return {"agents": [{"pane_id": p, "agent_status": s} for p, s in rows]}


class SyncDigest(unittest.TestCase):
    def test_digest_is_order_independent(self):
        a = hn.sync_digest(snap(("w1:p1", "idle"), ("w2:p2", "working")))
        b = hn.sync_digest(snap(("w2:p2", "working"), ("w1:p1", "idle")))
        self.assertEqual(a, b)

    def test_digest_sees_status_and_presence_changes(self):
        base = hn.sync_digest(snap(("w1:p1", "idle")))
        self.assertNotEqual(base, hn.sync_digest(snap(("w1:p1", "working"))))
        self.assertNotEqual(base, hn.sync_digest(snap()))
        self.assertNotEqual(base,
                            hn.sync_digest(snap(("w1:p1", "idle"),
                                                ("w1:p2", "idle"))))
        self.assertEqual(base, hn.sync_digest(snap(("w1:p1", "idle"))))

    def test_sync_enabled_env_gate(self):
        # DEFAULT OFF: the public ntfy.sh quota belongs to the real alerts.
        self.assertFalse(hn.sync_enabled({}))
        self.assertFalse(hn.sync_enabled({"NTFY_SYNC": "0"}))
        self.assertFalse(hn.sync_enabled({"NTFY_SYNC": " false "}))
        self.assertFalse(hn.sync_enabled({"NTFY_SYNC": "no"}))
        # Only an explicit opt-in enables it (self-hosted ntfy only).
        for on in ("1", "true", "yes", "on"):
            self.assertTrue(hn.sync_enabled({"NTFY_SYNC": on}), on)


class MaybeSync(unittest.TestCase):
    def setUp(self):
        self.sent = []
        patcher = mock.patch.object(hn, "send",
                                    lambda cfg, title, body, pri, tags,
                                    **kw: self.sent.append((title, pri)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sync = {"sig": None, "last": 0.0, "pending": False}

    def test_first_sighting_is_a_silent_baseline(self):
        hn.maybe_sync(hn.sync_digest(snap(("w1:p1", "idle"))), CFG, True,
                      100.0, self.sync)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.sync["sig"],
                         hn.sync_digest(snap(("w1:p1", "idle"))))

    def test_change_sends_one_min_priority_sync(self):
        hn.maybe_sync(hn.sync_digest(snap(("w1:p1", "idle"))), CFG, True,
                      100.0, self.sync)
        hn.maybe_sync(hn.sync_digest(snap(("w1:p1", "working"))), CFG, True,
                      103.0, self.sync)
        self.assertEqual(self.sent, [("TENDRIL · SYNC", "min")])

    def test_change_inside_the_window_is_pending_then_flushed(self):
        hn.maybe_sync(hn.sync_digest(snap(("w1:p1", "idle"))), CFG, True,
                      100.0, self.sync)
        hn.maybe_sync(hn.sync_digest(snap(("w1:p1", "working"))), CFG, True,
                      101.0, self.sync)          # first change: sends now
        self.assertEqual(self.sent, [("TENDRIL · SYNC", "min")])
        hn.maybe_sync(hn.sync_digest(snap(("w1:p1", "done"))), CFG, True,
                      102.0, self.sync)          # inside 4 s window
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sync["pending"])
        hn.maybe_sync(hn.sync_digest(snap(("w1:p1", "done"))), CFG, True,
                      106.0, self.sync)          # boundary: flush
        self.assertEqual(len(self.sent), 2)
        self.assertFalse(self.sync["pending"])

    def test_failed_send_retries_on_the_next_poll(self):
        with mock.patch.object(hn, "send",
                               side_effect=OSError("ntfy down")):
            hn.maybe_sync(hn.sync_digest(snap(("w1:p1", "idle"))), CFG, True,
                          100.0, self.sync)
            hn.maybe_sync(hn.sync_digest(snap(("w1:p1", "working"))), CFG,
                          True, 103.0, self.sync)
        self.assertEqual(self.sync.get("last"), 0.0)
        hn.maybe_sync(hn.sync_digest(snap(("w1:p1", "working"))), CFG, True,
                      104.0, self.sync)
        self.assertEqual(self.sent, [("TENDRIL · SYNC", "min")])

    def test_opt_out_paths_send_nothing(self):
        for cfg, allow in (({"NTFY_SYNC": "0", "NTFY_TOPIC": "t",
                             "NTFY_URL": "http://x"}, True),
                           (CFG, False),
                           ({}, True)):          # unconfigured watcher
            hn.maybe_sync(hn.sync_digest(snap(("w1:p1", "idle"))), cfg,
                          allow, 100.0, self.sync)
            hn.maybe_sync(hn.sync_digest(snap(("w1:p1", "working"))), cfg,
                          allow, 105.0, self.sync)
        self.assertEqual(self.sent, [])


class PollWiresTheDigest(unittest.TestCase):
    def test_poll_feeds_sync_and_survives_a_snapshot_failure(self):
        sent = []
        with mock.patch.object(hn, "send",
                               lambda cfg, t, b, p, tg, **kw:
                               sent.append(t)), \
             mock.patch.object(hn, "snapshot",
                               return_value=snap(("w1:p1", "idle"))):
            state, sync = {}, {"sig": None, "last": 0.0, "pending": False}
            self.assertTrue(hn.poll(state, CFG, allow_push=True, sync=sync))
            self.assertEqual(sent, [])             # baseline
            self.assertIsNotNone(sync["sig"])
        with mock.patch.object(hn, "snapshot", return_value=None):
            state, sync2 = {}, {"sig": None, "last": 0.0, "pending": False}
            self.assertFalse(hn.poll(state, CFG, allow_push=True, sync=sync2))
            self.assertIsNone(sync2["sig"])        # failure: nothing tracked


def _snap_with(*agents):
    """A herdr snapshot shape poll() understands: top-level agents +"""
    # workspace labels map (the DONE alert composes a title from them).
    agents = [{"pane_id": p, "agent_status": s, "agent": "pi",
               "workspace_id": "w1", "cwd": "/home/u/proj"}
              for p, s in agents]
    return {"agents": agents,
            "workspaces": [{"workspace_id": "w1", "label": "tendril"}]}


class DefaultConfigNeverPublishesSync(unittest.TestCase):
    """The acceptance contract: on ntfy.sh the daily quota belongs to the
    REAL alerts. With the DEFAULT config (NTFY_SYNC absent), a burst of
    parent-state changes produces ZERO extra publishes beyond the
    pre-existing finished/input/nudge set — no SYNC ever leaves the host."""

    def burst_states(self):
        # A busy workspace flap: spawn, work, block, recover, finish, repeat.
        return [
            snap(("w1:p1", "idle")),
            snap(("w1:p1", "working")),
            snap(("w1:p1", "blocked")),
            snap(("w1:p1", "working")),
            snap(("w1:p1", "done")),
            snap(("w1:p1", "idle"), ("w1:p2", "idle")),
            snap(("w1:p1", "working"), ("w1:p2", "working")),
            snap(("w1:p1", "done"), ("w1:p2", "blocked")),
            snap(("w1:p1", "done"), ("w1:p2", "done")),
            snap(("w1:p1", "done")),
        ]

    def test_default_config_zero_sync_publishes_in_a_burst(self):
        sent = []
        with mock.patch.object(hn, "send",
                               lambda cfg, t, b, p, tg, **kw:
                               sent.append(t)):
            state = {}
            sync = {"sig": None, "last": 0.0, "pending": False}
            cfg = {"NTFY_URL": "https://ntfy.sh",          # default:
                   "NTFY_TOPIC": "t"}                      # NO NTFY_SYNC
            now = 1000.0
            for st in self.burst_states() * 3:             # 30 polls
                with mock.patch.object(hn, "snapshot", return_value=st):
                    hn.poll(state, cfg, allow_push=True, sync=sync)
                now += hn.POLL_SECONDS
            # Every publish is a REAL alert (needs input / finished);
            # content-free SYNC pings: ZERO.
            for title in sent:
                self.assertNotIn("SYNC", title)
        self.assertGreater(len(sent), 0)   # the real alerts still fired

    def test_default_config_matches_the_pre_existing_alert_set(self):
        # The same burst WITHOUT the sync bookkeeping (as on the pre-D11
        # watcher) and WITH it must produce the IDENTICAL publish stream.
        def run(with_sync):
            sent = []
            with mock.patch.object(hn, "send",
                                   lambda cfg, t, b, p, tg, **kw:
                                   sent.append((t, p))):
                state = {}
                sync = {"sig": None, "last": 0.0, "pending": False}
                cfg = {"NTFY_URL": "https://ntfy.sh", "NTFY_TOPIC": "t"}
                now = 1000.0
                for st in self.burst_states() * 3:
                    with mock.patch.object(hn, "snapshot", return_value=st):
                        if with_sync:
                            hn.poll(state, cfg, allow_push=True, sync=sync)
                        else:
                            hn.poll(state, cfg, allow_push=True)
                    now += hn.POLL_SECONDS
            return sent

        self.assertEqual(run(with_sync=False), run(with_sync=True))


if __name__ == "__main__":
    unittest.main()
