"""RC2 perf: phone/tendril launch trace + healthy-fast-path-first behavior
(docs/LATENCY.md).

Drives the real launcher under `sh` with recording ssh/mosh stubs (same
fake-tools-on-PATH approach as test_phone_verbs / test_attach). Covers:

- trace OFF (the default): NO ~/.cache/tendril/launch.log is written —
  the switch costs one env test and one stat;
- trace ON (trace file OR TENDRIL_TRACE=1): the file is created 0600,
  holds ordered `<epoch_ms> <stage> [rc]` lines, never contains the
  hostname, and rotates at 64 KB;
- the healthy interactive path issues EXACTLY ONE transport invocation
  (mosh) — no synchronous `ssh … true` preflight before it;
- a FAILED transport triggers the key probe + guidance (probe runs after
  the failure, never before; its rc lands in the trace);
- connection reuse: ControlMaster opts on every ssh/mosh invocation,
  while ControlPersist appears ONLY on BatchMode invocations —
  background polling owns the warm master and interactive sessions
  never linger.

HOME is a temp dir; the real home is never touched. Stdlib only.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO, "phone", "tendril")

# RC2 connection reuse (mirrors phone/tendril): see tests/test_phone_verbs.py.
CM = "-o ControlMaster=auto -o ControlPath=~/.ssh/tendril-cm-%C"
BATCH_CM = CM + " -o ControlPersist=120 -o ConnectionAttempts=1"
SESSION_CM = CM + " -o ConnectionAttempts=1 -o ConnectTimeout=6"

# ssh stub: the BatchMode key probe (last arg "true") exits PROBE_RC so a
# test controls whether key auth looks set up; every other call is recorded
# and exits TENDRIL_SSH_RC.
SSH_STUB = ('case " $*" in *" true") exit "${PROBE_RC:-0}" ;; esac\n'
            'printf \'ssh %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'
            'exit "${TENDRIL_SSH_RC:-0}"\n')
# mosh stub: runs the remote payload like the real server would, then exits
# TENDRIL_MOSH_RC (mosh-client does not propagate the remote status).
MOSH_STUB = ('printf \'mosh %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'
             'while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done\n'
             '[ $# -gt 0 ] && shift\n'
             '[ "$1" = "sh" ] && shift\n'
             '[ "$1" = "-lc" ] && shift\n'
             'sh -c "$1"\n'
             'exit "${TENDRIL_MOSH_RC:-0}"\n')
REMOTE_STUB = ('printf \'remote-agents %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'
               'exit 0\n')


class TraceEnv(unittest.TestCase):
    """Temp HOME + stub bin dir; every argv lands in TENDRIL_RECORD."""

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril phone trace ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.bindir = os.path.join(self.tmp, "stub bin")
        os.makedirs(self.home)
        os.makedirs(self.bindir)
        self.record = os.path.join(self.tmp, "calls.log")
        for name, body in (("ssh", SSH_STUB), ("mosh", MOSH_STUB),
                           ("remote-agents", REMOTE_STUB)):
            path = os.path.join(self.bindir, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n" + body)
            os.chmod(path, 0o755)

    def env(self, **over):
        e = dict(os.environ)
        e["PATH"] = os.pathsep.join([self.bindir, "/usr/bin", "/bin"])
        e["HOME"] = self.home                                   # isolated
        e["TENDRIL_RECORD"] = self.record
        for k in ("TENDRIL_ALIAS", "TENDRIL_TRACE", "PROBE_RC",
                  "TENDRIL_SSH_RC", "TENDRIL_MOSH_RC"):
            e.pop(k, None)
        e.update(over)
        return e

    def run_launcher(self, args, **over):
        return subprocess.run(["sh", LAUNCHER] + list(args),
                              env=self.env(**over),
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        try:
            with open(self.record) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def enable_trace(self):
        conf = os.path.join(self.home, ".config", "tendril")
        os.makedirs(conf, exist_ok=True)
        open(os.path.join(conf, "trace"), "w").close()

    def log_path(self):
        return os.path.join(self.home, ".cache", "tendril", "launch.log")

    def log_stages(self):
        """(stages, epoch_ms list) from launch.log, as strings."""
        with open(self.log_path()) as f:
            rows = [ln.split() for ln in f.read().splitlines() if ln.strip()]
        return [r[1] for r in rows], [int(r[0]) for r in rows]


class TraceOff(TraceEnv):
    def test_trace_off_writes_no_log_file(self):
        p = self.run_launcher(["testhost"], TENDRIL_ALIAS="testhost")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.run_launcher(["snapshot"], TENDRIL_ALIAS="testhost")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(self.log_path()))
        # nothing trace-shaped leaks anywhere either
        for line in self.calls():
            self.assertNotIn("launch.log", line)


class TraceOn(TraceEnv):
    def test_env_var_alone_enables_the_trace(self):
        p = self.run_launcher(["testhost", "snapshot"],
                              TENDRIL_ALIAS="testhost", TENDRIL_TRACE="1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        stages, stamps = self.log_stages()
        self.assertEqual(stages, ["start", "args-validated",
                                  "transport-chosen-ssh", "ssh-exec"])
        self.assertEqual(stamps, sorted(stamps))

    def test_healthy_mosh_open_logs_ordered_stages_and_no_hostname(self):
        self.enable_trace()
        p = self.run_launcher([], TENDRIL_ALIAS="secrethost")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        stages, stamps = self.log_stages()
        # after the session: the one nonce-scoped L consume (the stub host
        # answers nothing, so no app open)
        self.assertEqual(stages, ["start", "args-validated",
                                  "transport-chosen-mosh", "mosh-exec",
                                  "mosh-done", "link-consume", "link-none"])
        self.assertEqual(stamps, sorted(stamps))
        # verb names only: no hostname, no remote command, no id anywhere
        with open(self.log_path()) as f:
            body = f.read()
        self.assertNotIn("secrethost", body)
        self.assertNotIn("remote-agents", body)
        # created safely: 0600
        self.assertEqual(oct(os.stat(self.log_path()).st_mode & 0o777),
                         "0o600")

    def test_failed_transport_logs_fallback_then_probe_then_ssh(self):
        self.enable_trace()
        p = self.run_launcher([], TENDRIL_ALIAS="testhost",
                              TENDRIL_MOSH_RC="17")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        stages, _ = self.log_stages()
        self.assertEqual(stages,
                         ["start", "args-validated", "transport-chosen-mosh",
                          "mosh-exec", "fallback", "preflight-start",
                          "preflight-end", "transport-chosen-ssh",
                          "ssh-exec"])
        self.assertIn("mosh failed - falling back to ssh", p.stdout)
        self.assertNotIn("key-auth check failed", p.stdout)   # probe passed

    def test_probe_failure_logs_its_rc_and_prints_the_guidance(self):
        self.enable_trace()
        p = self.run_launcher([], TENDRIL_ALIAS="testhost",
                              TENDRIL_MOSH_RC="17", PROBE_RC="255",
                              TENDRIL_SSH_RC="255")
        self.assertEqual(p.returncode, 255, p.stdout + p.stderr)
        stages, _ = self.log_stages()
        self.assertIn("preflight-end", stages)
        with open(self.log_path()) as f:
            body = f.read()
        self.assertIn("preflight-end 255", body)
        self.assertIn("key-auth check failed for 'testhost'", p.stdout)
        self.assertIn("attempting interactive SSH anyway", p.stdout)


class FastPath(TraceEnv):
    def clear_record(self):
        open(self.record, "w").close()

    def test_healthy_interactive_path_is_exactly_one_transport_invocation(self):
        p = self.run_launcher([], TENDRIL_ALIAS="testhost")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        # ONE interactive transport invocation: exactly one mosh dial, no
        # preflight, no probe. The ONE ssh after the session is not a
        # transport — it is the L-flag consume (mosh drops exit statuses);
        # with the stub answering silence it opens nothing.
        transports = [c for c in self.calls() if c.startswith("mosh ")]
        self.assertEqual(len(transports), 1)
        self.assertTrue(transports[0].startswith("mosh "))
        ssh_calls = [c for c in self.calls() if c.startswith("ssh ")]
        self.assertEqual(len(ssh_calls), 1)
        self.assertIn("--link-open-request", ssh_calls[0])
        self.assertNotIn("key-auth check failed", p.stdout)

    def test_forced_ssh_failure_probes_only_then_degrades(self):
        # ssh -t fails 255 -> ONE probe -> probe fails -> guidance -> the
        # degraded exec ssh -t (which records again and fails 255)
        p = self.run_launcher(["ssh", "testhost"], TENDRIL_ALIAS="testhost",
                              PROBE_RC="1", TENDRIL_SSH_RC="255")
        self.assertEqual(p.returncode, 255, p.stdout + p.stderr)
        ssh_calls = [c for c in self.calls() if c.startswith("ssh ")]
        self.assertEqual(len(ssh_calls), 2)          # ssh -t, then degraded
        self.assertIn("key-auth check failed for 'testhost'", p.stdout)
        for c in ssh_calls:
            self.assertIn("-o ControlMaster=auto", c)   # reuse everywhere
            self.assertNotIn("ControlPersist", c)       # sessions never linger

    def test_controlpersist_is_batchmode_only(self):
        self.clear_record()
        p = self.run_launcher(["snapshot"], TENDRIL_ALIAS="testhost")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        batch = self.calls()[0]
        self.assertIn("-o BatchMode=yes -o ConnectTimeout=6 " + BATCH_CM
                      + " testhost remote-agents snapshot", batch)
        self.clear_record()
        p = self.run_launcher(["ssh", "testhost"], TENDRIL_ALIAS="testhost")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        session = [c for c in self.calls() if c.startswith("ssh -t ")][0]
        self.assertIn("-o ControlMaster=auto", session)
        self.assertNotIn("ControlPersist", session)
        self.clear_record()
        p = self.run_launcher([], TENDRIL_ALIAS="testhost")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        mosh = [c for c in self.calls() if c.startswith("mosh ")][0]
        self.assertIn("--ssh=ssh " + SESSION_CM, mosh)


class Rotation(TraceEnv):
    def test_log_rotates_at_64kb(self):
        self.enable_trace()
        cache = os.path.join(self.home, ".cache", "tendril")
        os.makedirs(cache, exist_ok=True)
        with open(self.log_path(), "w") as f:
            f.write("0 start\n" + "x" * 65536 + "\n")
        p = self.run_launcher(["snapshot"], TENDRIL_ALIAS="testhost")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(os.path.exists(self.log_path() + ".old"))
        stages, _ = self.log_stages()
        self.assertEqual(stages[0], "start")            # fresh small log


if __name__ == "__main__":
    unittest.main()
