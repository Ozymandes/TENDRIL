"""TENDRIL · SYNC — the D11 freshness invalidation event.

The notify watcher's parent-state digest is the host's only push-visible
"the snapshot just changed" signal for live transitions (working/idle/done/
blocked, agents appearing or vanishing). A SYNC is a MIN-priority ntfy
message the phone never surfaces: its receiver answers with one debounced
snapshot refresh (contract §7.6). Rules tested here:

- the digest is order-independent and derives ONLY from (pane_id, status);
- the first sighting is a silent baseline (a watcher restart never pings);
- a digest change sends exactly one SYNC, rate-limited to one per
  SYNC_MIN_INTERVAL; a change inside the window is remembered ("pending")
  and flushed at the boundary;
- a failed send stamps nothing, so the next poll retries;
- NTFY_SYNC=0 (or an unconfigured watcher / allow_push=False) opts out.
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
        self.assertTrue(hn.sync_enabled(CFG))
        self.assertTrue(hn.sync_enabled({}))
        self.assertFalse(hn.sync_enabled({"NTFY_SYNC": "0"}))
        self.assertFalse(hn.sync_enabled({"NTFY_SYNC": " false "}))


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


if __name__ == "__main__":
    unittest.main()
