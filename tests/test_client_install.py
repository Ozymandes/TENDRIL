"""`install --client` and the INSTALLED TENDRIL client launcher.

Runs the real `install --client` under `sh` with stub ssh/mosh/remote-agents
on PATH (recording their argv), then drives the launcher that was installed
into the temp HOME — both via `sh` and directly as an executable. Covers the
attach path (BatchMode key check, Mosh preference, SSH fallback) and the
resolve/focus/link verbs (one plain SSH round trip, no key check, no Mosh).
HOME is a temp dir with a space in its name; the real home is never touched.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(REPO, "install")
UNINSTALL = os.path.join(REPO, "uninstall")
PHONE = os.path.join(REPO, "phone", "tendril")

SSH_STUB = ('printf \'ssh %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'
            'exit "${TENDRIL_SSH_RC:-0}"\n')
# simulate the remote side: run the command string the launcher built
MOSH_STUB = ('printf \'mosh %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'
             'while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done\n'
             '[ $# -gt 0 ] && shift\n'
             '[ "$1" = "sh" ] && shift\n'
             '[ "$1" = "-lc" ] && shift\n'
             'exec sh -c "$1"\n')
REMOTE_STUB = 'printf \'remote-agents %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'


class ClientEnv(unittest.TestCase):
    """Temp HOME + stub bin dir shared by installer and launcher tests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril client ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "tendril home dir")
        os.makedirs(self.home)
        self.bindir = os.path.join(self.tmp, "stub bin")
        os.makedirs(self.bindir)
        for name, body in (("ssh", SSH_STUB), ("mosh", MOSH_STUB),
                           ("remote-agents", REMOTE_STUB)):
            path = os.path.join(self.bindir, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n" + body)
            os.chmod(path, 0o755)
        self.record = os.path.join(self.tmp, "calls.log")

    def env(self, **over):
        e = dict(os.environ)
        e["PATH"] = os.pathsep.join([self.bindir, "/usr/bin", "/bin"])
        e["HOME"] = self.home
        e["TENDRIL_RECORD"] = self.record
        for k in ("XDG_CONFIG_HOME", "TENDRIL_ALIAS", "AGENT_ALIAS",
                  "TENDRIL_SSH_RC", "SHELL"):
            e.pop(k, None)
        e.update(over)
        return e

    def client(self, args=("--client",), answers=("janes-mac",), **over):
        return subprocess.run(["/bin/sh", INSTALL] + list(args),
                              env=self.env(**over),
                              input="\n".join(answers) + "\n",
                              capture_output=True, text=True, timeout=120)

    def uninstall(self, answers=()):
        return subprocess.run(["/bin/sh", UNINSTALL], env=self.env(),
                              input="\n".join(answers) + "\n",
                              capture_output=True, text=True, timeout=120)

    def launcher(self, path, args, mode="sh", **over):
        argv = ([path] + list(args) if mode == "exec"
                else ["/bin/sh", path] + list(args))
        return subprocess.run(argv, env=self.env(**over),
                              capture_output=True, text=True, timeout=120)

    def calls(self):
        try:
            with open(self.record) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def clear_calls(self):
        try:
            os.remove(self.record)
        except OSError:
            pass

    def tendril_bin(self, name="tendril"):
        return os.path.join(self.home, ".local", "bin", name)

    def installed(self):
        with open(self.tendril_bin()) as f:
            return f.read()

    def alias_file(self):
        return os.path.join(self.home, ".config", "tendril", "alias")

    def walk_home(self):
        found = []
        for root, dirs, files in os.walk(self.home):
            found.extend(os.path.join(root, d) for d in dirs)
            found.extend(os.path.join(root, f) for f in files)
        return found


class ClientInstall(ClientEnv):
    def test_client_installs_launcher_and_alias(self):
        p = self.client(answers=("janes-mac",), TENDRIL_SSH_RC="1")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        phone = open(PHONE).read()
        expected = "#!/bin/sh\n" + phone.split("\n", 1)[1]
        self.assertEqual(self.installed(), expected)
        self.assertTrue(os.access(self.tendril_bin(), os.X_OK))
        with open(self.alias_file()) as f:
            self.assertEqual(f.read(), "janes-mac\n")
        # BatchMode key check failed: guidance, then a hard failure exit
        self.assertIn("key login to janes-mac failed", out)

    def test_key_check_success_is_reported(self):
        p = self.client(answers=("janes-mac",), TENDRIL_SSH_RC="0")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("key login to janes-mac: ok", out)

    def test_invalid_alias_input_is_rejected(self):
        for bad in ("bad;name", "-oProxyCommand=x"):
            p = self.client(answers=(bad,))
            out = p.stdout + p.stderr
            self.assertEqual(p.returncode, 1, (bad, out))
            self.assertIn("no valid alias given", out)
        self.assertEqual(self.walk_home(), [])

    def test_dry_run_writes_nothing(self):
        p = self.client(args=("--client", "--dry-run"))
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("dry-run", out)
        self.assertEqual(self.walk_home(), [])

    def test_host_symlink_becomes_tendril_remote(self):
        bindir = os.path.join(self.home, ".local", "bin")
        os.makedirs(bindir)
        os.symlink("remote-agents", self.tendril_bin())
        p = self.client()
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertTrue(os.path.islink(self.tendril_bin()))
        self.assertEqual(os.readlink(self.tendril_bin()), "remote-agents")
        self.assertTrue(os.path.isfile(self.tendril_bin("tendril-remote")))
        self.assertTrue(self.installed_name_starts_with_sh("tendril-remote"))
        with open(self.alias_file()) as f:
            self.assertEqual(f.read(), "janes-mac\n")

    def installed_name_starts_with_sh(self, name):
        with open(self.tendril_bin(name)) as f:
            return f.readline().rstrip("\n") == "#!/bin/sh"

    def test_foreign_tendril_file_is_kept(self):
        bindir = os.path.join(self.home, ".local", "bin")
        os.makedirs(bindir)
        foreign = "#!/bin/sh\necho foreign tool\n"
        with open(self.tendril_bin(), "w") as f:
            f.write(foreign)
        os.chmod(self.tendril_bin(), 0o755)
        p = self.client()
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        with open(self.tendril_bin()) as f:
            self.assertEqual(f.read(), foreign)          # byte-for-byte
        self.assertTrue(os.path.isfile(self.tendril_bin("tendril-remote")))
        self.assertIn("tendril-remote", out)


class InstalledLauncher(ClientEnv):
    def setUp(self):
        super().setUp()
        p = self.client(answers=("testhost",))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.clear_calls()

    def test_alias_file_used_when_env_unset(self):
        p = self.launcher(self.tendril_bin(), [])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        calls = self.calls()
        self.assertEqual(calls[0],
                         "ssh -o ConnectTimeout=5 -o BatchMode=yes "
                         "testhost true")
        mosh = [c for c in calls if c.startswith("mosh ")]
        self.assertEqual(len(mosh), 1)
        self.assertIn("testhost", mosh[0])
        self.assertNotIn("attach", mosh[0])
        self.assertTrue(any(c.rstrip() == "remote-agents" for c in calls))

    def test_tendril_alias_env_overrides_the_file(self):
        p = self.launcher(self.tendril_bin(), [], TENDRIL_ALIAS="otherhost")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        mosh = [c for c in self.calls() if c.startswith("mosh ")]
        self.assertEqual(len(mosh), 1)
        self.assertIn("otherhost", mosh[0])
        self.assertNotIn("testhost", mosh[0])

    def test_attach_matches_the_phone_launcher_exactly(self):
        installed = self.launcher(self.tendril_bin(), ["attach", "w15"])
        self.assertEqual(installed.returncode, 0)
        installed_calls = self.calls()
        self.clear_calls()
        phone = self.launcher(PHONE, ["attach", "w15"],
                              TENDRIL_ALIAS="testhost")
        self.assertEqual(phone.returncode, 0)
        self.assertEqual(self.calls(), installed_calls)
        mosh = [c for c in installed_calls if c.startswith("mosh ")]
        self.assertEqual(len(mosh), 1)
        self.assertIn("attach 'w15'", mosh[0])
        self.assertIn("remote-agents attach w15", installed_calls)

    def test_installed_launcher_runs_directly_as_an_executable(self):
        p = self.launcher(self.tendril_bin(), ["attach", "w15"], mode="exec")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        mosh = [c for c in self.calls() if c.startswith("mosh ")]
        self.assertEqual(len(mosh), 1)
        self.assertIn("attach 'w15'", mosh[0])

    def test_batchmode_failure_falls_back_to_interactive_ssh(self):
        p = self.launcher(self.tendril_bin(), [], TENDRIL_SSH_RC="1")
        # the launcher execs the interactive ssh, so its rc propagates
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("key-auth check failed for 'testhost'", p.stdout)
        calls = self.calls()
        self.assertFalse(any(c.startswith("mosh ") for c in calls))
        self.assertIn("ssh -t testhost remote-agents", calls)

    # ---- resolve / focus / link: one plain SSH round trip -------------
    def test_focus_is_one_plain_ssh_without_key_check_or_mosh(self):
        p = self.launcher(self.tendril_bin(), ["focus", "w15"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["ssh testhost remote-agents focus 'w15'"])

    def test_link_with_explicit_alias(self):
        p = self.launcher(self.tendril_bin(), ["otherhost", "link", "w3"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["ssh otherhost remote-agents link 'w3'"])

    def test_resolve_accepts_pane_ids(self):
        p = self.launcher(self.tendril_bin(), ["testhost", "resolve",
                                               "w15:p1"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["ssh testhost remote-agents resolve 'w15:p1'"])

    def test_direct_verbs_match_the_phone_launcher_exactly(self):
        for args in (["focus", "w15"], ["otherhost", "link", "w3"]):
            self.clear_calls()
            installed = self.launcher(self.tendril_bin(), list(args))
            self.assertEqual(installed.returncode, 0)
            installed_calls = self.calls()
            self.clear_calls()
            phone = self.launcher(PHONE, list(args), TENDRIL_ALIAS="testhost")
            self.assertEqual(phone.returncode, 0)
            self.assertEqual(self.calls(), installed_calls, args)

    def test_resolve_bad_id_exits_two_without_ssh(self):
        for args in (["resolve", "x;y"], ["link", "bad id"],
                     ["focus", "w15!"], ["attach", ""], ["link", ""]):
            p = self.launcher(self.tendril_bin(), args)
            self.assertEqual(p.returncode, 2, args)
            self.assertEqual(self.calls(), [])
            out = p.stdout + p.stderr
            self.assertIn("usage: tendril [<alias>] %s <session-id>"
                          % args[0], out)
            self.assertIn("A-Z a-z 0-9 . _ : -", out)

    def test_evil_alias_is_rejected_for_direct_verbs(self):
        with open(self.alias_file(), "w") as f:
            f.write("-oProxyCommand=evil\n")
        p = self.launcher(self.tendril_bin(), ["resolve", "w15"])
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])
        self.assertIn("not an SSH alias", p.stdout + p.stderr)
        p = self.launcher(self.tendril_bin(), ["focus", "w15"],
                          TENDRIL_ALIAS="-oProxyCommand=evil")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])


class ClientUninstall(ClientEnv):
    def test_uninstall_removes_client_launcher_and_alias(self):
        p = self.client()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        pu = self.uninstall()
        out = pu.stdout + pu.stderr
        self.assertEqual(pu.returncode, 0, out)
        self.assertFalse(os.path.exists(self.tendril_bin()))
        self.assertFalse(os.path.exists(self.alias_file()))
        self.assertFalse(os.path.exists(os.path.dirname(self.alias_file())))
        self.assertIn("removed:", out)

    def test_uninstall_never_touches_a_foreign_tendril(self):
        bindir = os.path.join(self.home, ".local", "bin")
        os.makedirs(bindir)
        foreign = "#!/bin/sh\necho foreign tool\n"
        with open(self.tendril_bin(), "w") as f:
            f.write(foreign)
        os.chmod(self.tendril_bin(), 0o755)
        p = self.client()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        pu = self.uninstall()
        self.assertEqual(pu.returncode, 0, pu.stdout + pu.stderr)
        with open(self.tendril_bin()) as f:
            self.assertEqual(f.read(), foreign)          # untouched
        self.assertFalse(os.path.exists(self.tendril_bin("tendril-remote")))
        self.assertFalse(os.path.exists(self.alias_file()))


if __name__ == "__main__":
    unittest.main()
