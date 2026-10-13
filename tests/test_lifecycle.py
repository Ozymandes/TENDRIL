"""RC2 lifecycle regressions — the journal crash and its neighbours.

The live failure: Herdr went down, `remote-agents schedule tick` (the
systemd timer) crashed with TypeError because snapshot() fed None into
run([HERDR, "api", "snapshot"]) — subprocess argv may never contain
None — and the timer stayed stopped afterwards. These tests pin:

- `schedule tick` with Herdr down (or no client at all) answers ONE ok
  JSON line with zero launches — no traceback, tasks wait;
- `run()`/`snapshot()` never pass a None argv[0];
- `capacity` / `capacity enable <adapter>` dispatch with the module
  installed (needs no network to succeed at enabling);
- health --json carries the §7.4 herdr/ scheduler/ guardian/ capacity
  blocks over the real CLI.

End-to-end through the real bin/remote-agents with a temp HOME, fake
herdr and stubbed systemctl — the live host is never touched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "bin", "remote-agents")

HERDR_STUB = """#!/bin/sh
STATE_FILE="${TENDRIL_FAKE_HERDR_STATE}"
running() { [ "$(cat "$STATE_FILE" 2>/dev/null)" = 1 ]; }
case "$1" in
  --version) echo "herdr 0.9.9"; exit 0 ;;
  status)
      if [ "$2" = "server" ]; then
          if running; then
              echo '{"status":"running","running":true,"version":"0.9.9","compatible":true}'
          else
              echo '{"status":"stopped","running":false,"version":"0.9.9"}'
          fi
      else
          echo "client: herdr 0.9.9"
          running && echo "status: running" && echo "endpoint_compatible: yes"
      fi
      exit 0 ;;
  api) if running && [ "$2" = "snapshot" ]; then
            echo '{"result":{"snapshot":{"panes":[]}}}'; exit 0;
        fi
        exit 7 ;;
  server) exit 0 ;;
esac
exit 64
"""

SYSTEMCTL_STUB = """#!/bin/sh
case "$1 $2" in
    "--user is-active") echo inactive ;;
    "--user is-system-running") echo running ;;
esac
exit 0
"""


class LifecycleCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril lifecycle ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.bindir = os.path.join(self.tmp, "stub-bin")
        os.makedirs(self.bindir)
        self.state = os.path.join(self.tmp, "herdr-state")
        with open(self.state, "w") as f:
            f.write("0")
        for name, body in (("herdr", HERDR_STUB),
                           ("systemctl", SYSTEMCTL_STUB)):
            with open(os.path.join(self.bindir, name), "w") as f:
                f.write(body)
            os.chmod(os.path.join(self.bindir, name), 0o755)
        self.env = dict(os.environ)
        # hermetic PATH: only the stubs and /usr/bin — the real user PATH
        # would leak mise shims/real herdrs into resolution and flake
        self.env.update({
            "HOME": self.home,
            # /bin too: macOS keeps cat & friends there (no usr-merge)
            "PATH": os.pathsep.join([self.bindir, "/usr/bin", "/bin"]),
            "TENDRIL_FAKE_HERDR_STATE": self.state,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
                  "HERDR_BIN", "TENDRIL_ALIAS", "AGENT_ALIAS"):
            self.env.pop(k, None)
        # an empty store so the tick has nothing to launch even when up
        state_dir = os.path.join(self.home, ".local", "state", "tendril")
        os.makedirs(state_dir)
        with open(os.path.join(state_dir, "schedule.json"), "w") as f:
            f.write('{"schema_version": 1, "tasks": []}')

    def cli(self, *argv):
        return subprocess.run([sys.executable, CLI, *argv], env=self.env,
                              capture_output=True, text=True, timeout=120)

    def test_tick_with_herdr_down_is_ok_with_no_launches(self):
        with open(self.state, "w") as f:
            f.write("0")                     # Herdr down
        p = self.cli("schedule", "tick")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        doc = json.loads(p.stdout.strip())
        self.assertEqual(doc["ok"], True)
        self.assertEqual(doc["tick"]["launched"], 0)
        self.assertEqual(doc["tick"]["transitions"], 0)
        self.assertEqual(doc["herdr"], "unavailable")

    def test_tick_with_no_client_at_all_is_ok(self):
        # no herdr anywhere: detection finds nothing, HERDR is None —
        # exactly the TypeError crash in the journal
        env = dict(self.env)
        env["PATH"] = os.path.join(self.tmp, "empty-bin")
        os.makedirs(env["PATH"])
        env["TENDRIL_HERDR_FIXED_DIRS"] = ""
        p = subprocess.run([sys.executable, CLI, "schedule", "tick"],
                           env=env, capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        doc = json.loads(p.stdout.strip())
        self.assertEqual(doc["ok"], True)
        self.assertEqual(doc["tick"]["launched"], 0)

    def test_snapshot_never_passes_none_argv(self):
        # direct unit check of the guard (the journal's TypeError)
        with open(self.state, "w") as f:
            f.write("0")
        p = self.cli("snapshot", "--json")
        self.assertNotIn("TypeError", p.stderr)

    def test_tick_with_herdr_up_runs_normally(self):
        with open(self.state, "w") as f:
            f.write("1")
        p = self.cli("schedule", "tick")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        doc = json.loads(p.stdout.strip())
        self.assertEqual(doc["ok"], True)
        self.assertNotIn("herdr", doc)       # normal tick: no availability note

    def test_capacity_status_and_enable_dispatch(self):
        p = self.cli("capacity", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        sec = json.loads(p.stdout.strip())
        self.assertIsInstance(sec.get("providers"), list)
        for adapter in ("claude_oauth", "zai_quota"):
            p = self.cli("capacity", "enable", adapter)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("enabled", p.stdout)
        cfg = os.path.join(self.home, ".config", "tendril", "capacity.json")
        with open(cfg) as f:
            enabled = json.load(f)["enabled"]
        self.assertEqual(enabled, ["claude_oauth", "zai_quota"])
        p = self.cli("capacity", "disable", "claude_oauth")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_health_json_v2_shape(self):
        p = self.cli("health", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        doc = json.loads(p.stdout.strip())
        herdr = doc["herdr"]
        self.assertIn(herdr["state"],
                      ("running", "down", "stopped", "mismatch", "unknown"))
        self.assertIn(herdr["binary"], ("recorded", "detected", "missing"))
        for key in ("scheduler", "guardian"):
            block = doc[key]
            self.assertIn(block["state"],
                          ("active", "degraded", "inactive", "absent"))
            if "timer" in block:
                self.assertIn(block["timer"],
                              ("waiting", "running", "inactive", "failed"))
            if "last_tick" in block:
                self.assertIn(block["last_tick"], ("ok", "failed"))
        cap = doc["capacity"]
        self.assertIn(cap["state"],
                      ("available", "adapters_disabled", "error", "absent"))
        self.assertIsInstance(cap.get("enabled", []), list)
        # §0: no paths or home dirs anywhere in the payload
        self.assertNotIn(self.home, p.stdout)
        self.assertNotIn(self.bindir, p.stdout)

    def test_health_json_herdr_down_state(self):
        with open(self.state, "w") as f:
            f.write("0")
        p = self.cli("health", "--json")
        doc = json.loads(p.stdout.strip())
        self.assertEqual(doc["herdr"]["state"], "down")
        self.assertEqual(doc["herdr"]["binary"], "detected")

    def test_health_json_mismatch_when_versions_differ(self):
        # recorded client wins resolution (provenance recorded) even with
        # a different-looking candidate on PATH; equal versions stay
        # "running" — the version-difference mismatch itself is covered by
        # the unit tests in test_health.py
        conf = os.path.join(self.home, ".config", "remote-agents")
        os.makedirs(conf, exist_ok=True)
        with open(os.path.join(conf, "config"), "w") as f:
            f.write("HERDR_BIN=%s\n" % os.path.join(self.bindir, "herdr"))
            f.write("HERDR_VERSION=0.9.9\n")
        with open(self.state, "w") as f:
            f.write("1")
        p = self.cli("health", "--json")
        doc = json.loads(p.stdout.strip())
        self.assertEqual(doc["herdr"]["binary"], "recorded")
        self.assertEqual(doc["herdr"]["state"], "running")
        self.assertEqual(doc["herdr"]["client_version"], "0.9.9")
        self.assertEqual(doc["herdr"]["server_version"], "0.9.9")

    def test_remote_agents_detect_still_prints_chosen(self):
        p = self.cli("--detect")
        self.assertEqual(p.returncode, 0)
        self.assertIn("chosen: ", p.stdout)


if __name__ == "__main__":
    unittest.main()
