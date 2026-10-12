"""LIVE integration: the D10 scheduler launcher against REAL Herdr (§13).

Runs the actual `_schedule_launcher` from bin/remote-agents — real herdr
subprocesses, real pane, real pi agent — against a workspace this test
created and closes afterwards. Skipped cleanly (never a failure) whenever
a real Herdr server or the pi harness is not available, so `run_suite.py`
stays green on machines without them.

What it proves (the §13 chain, launcher → pane → lifecycle):
  1. an existing-session task dispatches `agent prompt` into the EXACT
     pre-existing pane (no new pane, no agent start);
  2. the launch acknowledgment returns ok while the turn may still be
     running (the D10 fix: rc 0 is herdr's pickup proof, not a 15 s
     completion race);
  3. the prompt text arrives in that pane and pi answers it;
  4. the store-side RUNNING → DONE machinery finishes the task off the
     live snapshot once the pane settles idle/done.

Stdlib + the repo's own modules only; never touches user panes.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "bin", "remote-agents")
SCHED = os.path.join(REPO, "bin", "tendril_schedule.py")

MARKER = "TENDRIL-D10-LIVE-OK"
REPLY_WAIT_S = 150          # pi answering a one-line prompt
IDLE_WAIT_S = 60            # pane settling back to idle/done afterwards


def load_mod(name, path):
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _herdr_bin():
    """The §7.4 resolver's answer, else PATH — None when unusable."""
    try:
        cfg_path = os.path.expanduser("~/.config/remote-agents/config")
        cfg = {}
        if os.path.isfile(cfg_path):
            with open(cfg_path) as f:
                for ln in f:
                    ln = ln.strip()
                    if ln and not ln.startswith("#") and "=" in ln:
                        k, v = ln.split("=", 1)
                        cfg[k.strip()] = v.strip().strip('"').strip("'")
        resolved = load_mod("tendril_herdr_live", os.path.join(
            REPO, "bin", "tendril_herdr.py")).resolve(config=cfg,
                                                      env=os.environ)
        if resolved.get("bin"):
            return resolved["bin"]
    except Exception:
        pass
    return shutil.which("herdr")


def _server_running(herdr):
    try:
        p = subprocess.run([herdr, "api", "snapshot"], capture_output=True,
                           text=True, timeout=10)
        return p.returncode == 0 and '"snapshot"' in p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


HERDR = _herdr_bin()
LIVE = (HERDR is not None and _server_running(HERDR)
        and (shutil.which("pi")
             or os.path.isfile(os.path.expanduser("~/.local/bin/pi"))))


@unittest.skipUnless(LIVE, "live Herdr server + pi not available")
class ScheduleLiveHerdrTests(unittest.TestCase):
    """One dedicated workspace, one pi pane, closed again afterwards."""

    def setUp(self):
        self.ra = load_mod("remote_agents_live", CLI)
        self.ts = load_mod("tendril_schedule_live", SCHED)
        self.tmp = tempfile.mkdtemp(prefix="tendril-d10-live-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        tag = "d%d" % (os.getpid() % 100000)
        self.ws_label = "tendril-%s-live" % tag
        self.agent_name = "tendril-%s-live" % tag
        self._herdr = lambda *argv: self._run(HERDR, list(argv), 40)

    # -- herdr plumbing ----------------------------------------------------
    def _run(self, bin, argv, timeout):
        p = subprocess.run([bin] + argv, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    def _json_out(self, rc, out, _err=None):
        self.assertEqual(rc, 0, out)
        return json.loads(out)["result"]

    def _agent_row(self, pane_id):
        rc, out, _ = self._herdr("api", "snapshot")
        self.assertEqual(rc, 0)
        snap = json.loads(out)["result"]["snapshot"]
        for a in snap.get("agents", []):
            if a.get("pane_id") == pane_id:
                return a
        return None

    def _wait_status(self, pane_id, statuses, bound):
        deadline = time.time() + bound
        last = None
        while time.time() < deadline:
            row = self._agent_row(pane_id)
            last = (row or {}).get("agent_status")
            if last in statuses:
                return last
            time.sleep(2)
        self.fail("pane %s never reached %s (last: %r)"
                  % (pane_id, statuses, last))

    # -- the live chain ----------------------------------------------------
    def test_existing_session_task_dispatches_finishes_and_reconciles(self):
        # 1) a DEDICATED workspace + tab, then a real idle pi in it
        created = self._json_out(*self._herdr(
            "workspace", "create", "--label", self.ws_label))
        ws_id = created["workspace"]["workspace_id"]
        self.addCleanup(self._close_ws, ws_id)
        tab = self._json_out(*self._herdr(
            "tab", "create", "--workspace", ws_id,
            "--cwd", self.tmp, "--label", self.ws_label,
            "--no-focus"))
        pane_id = tab["root_pane"]["pane_id"]
        self._json_out(*self._herdr(
            "agent", "start", self.agent_name, "--kind", "pi",
            "--pane", pane_id, "--timeout", "30000"))
        self.assertEqual(self._agent_row(pane_id)["agent_status"], "idle")

        # pane inventory BEFORE the dispatch: the cert workspace holds
        # exactly the panes we created — the task must not add one
        rc, out, _ = self._herdr("api", "snapshot")
        before = [p["pane_id"] for p in
                  json.loads(out)["result"]["snapshot"]["panes"]]

        # 2) the REAL launcher, existing-session target, as the tick calls it
        task = {"id": "t-live-d10", "title": "d10 live",
                "prompt": "Reply with exactly: " + MARKER,
                "workspace": ws_id, "harness": "pi", "pane_id": None,
                "target": {"kind": "existing", "pane": pane_id,
                           "harness": "pi"}}
        real_run, real_herdr = self.ra.run, self.ra.HERDR
        self.ra.HERDR = HERDR
        try:
            launch = self.ra._schedule_launcher()
            ok, got_pane, err = launch(task)
        finally:
            self.ra.run, self.ra.HERDR = real_run, real_herdr
        self.assertTrue(ok, "launcher refused: %r" % (err,))
        self.assertEqual(got_pane, pane_id)     # dispatched INTO the pane

        # 3) no new pane anywhere; the prompt arrives in THAT pane; pi answers
        rc, out, _ = self._herdr("api", "snapshot")
        after = [p["pane_id"] for p in
                 json.loads(out)["result"]["snapshot"]["panes"]]
        self.assertEqual(after, before)
        self._wait_reply(pane_id)

        # 4) the store-side lifecycle: RUNNING → (pane idle) → DONE
        _, store_path, _ = self.ts.store_paths(self.tmp)
        now = time.time()
        row = {"id": "t-live-d10", "title": "d10 live", "prompt": "x",
               "workspace": ws_id, "workspace_label": "", "harness": "pi",
               "trigger": {"kind": "at", "at": int(now)},
               "target": {"kind": "existing", "pane": pane_id,
                          "harness": "pi"},
               "state": "RUNNING", "due_at": int(now),
               "created_at": int(now), "started_at": int(now),
               "finished_at": None, "pane_id": pane_id, "failure": None,
               "fingerprint": "x", "launch_token": "x", "notified": {},
               "resets_at": None}
        self.ts.save_store(self.ts.Store(store_path, [row]))
        self._wait_status(pane_id, ("idle", "done"), IDLE_WAIT_S)
        snap = self.ra.snapshot()
        self.assertIsNotNone(snap)
        n = self.ts.reconcile(store_path, time.time(), snap)
        self.assertEqual(n, 1)
        store, err = self.ts.load_store(store_path)
        self.assertIsNone(err)
        self.assertEqual(store["tasks"][0]["state"], "DONE")

    def _wait_reply(self, pane_id):
        deadline = time.time() + REPLY_WAIT_S
        while time.time() < deadline:
            rc, out, _ = self._run(HERDR, ["agent", "read", pane_id,
                                           "--source", "recent-unwrapped",
                                           "--lines", "80"], 15)
            if rc == 0 and MARKER in out:
                return
            time.sleep(3)
        self.fail("prompt never produced %r in pane %s" % (MARKER, pane_id))

    def _close_ws(self, ws_id):
        # the workspace (and its pi) are ours; closing is the cleanup.
        # A close can transiently refuse while the agent is mid-turn —
        # wait it out and retry once so a stranded workspace is impossible.
        for attempt in range(2):
            rc, out, _ = self._run(HERDR, ["workspace", "close", ws_id], 30)
            if rc == 0:
                return
            self._wait_status_quiet(ws_id)
        self.fail("could not close live-test workspace %s" % ws_id)

    def _wait_status_quiet(self, ws_id):
        time.sleep(5)


if __name__ == "__main__":
    unittest.main()
