"""The real `install`/`uninstall` scripts under a fake Darwin userland.

Stub executables on PATH (uname, launchctl, sysctl, vm_stat, herdr, ssh,
mosh, tailscale) stand in for macOS; recording stubs for systemctl,
journalctl and loginctl fail the run if the Darwin path ever calls a
Linux-only tool. A sitecustomize module patches os.uname so tendril_host
reports a Mac hostname. Everything is written under a temp HOME with a
space in its name; the real home is never touched.

Runs unmodified on Linux CI (no launchctl there, so tendril_host's
backend() picks launchd purely because the stub is on PATH) and on the
GitHub Actions macOS runners.
"""
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(REPO, "install")
UNINSTALL = os.path.join(REPO, "uninstall")
sys.path.insert(0, os.path.join(REPO, "bin"))
import tendril_host as th  # noqa: E402

SITECUSTOMIZE = '''"""Test shim: make os.uname() report the fake Darwin host."""
import os
import posix

if os.environ.get("TENDRIL_FAKE_UNAME"):
    _fields = (
        os.environ.get("TENDRIL_FAKE_SYSNAME", "Darwin"),
        os.environ["TENDRIL_FAKE_UNAME"],
        os.environ.get("TENDRIL_FAKE_RELEASE", "24.0.0"),
        os.environ.get("TENDRIL_FAKE_VERSION",
                       "Darwin Kernel Version 24.0.0: test"),
        os.environ.get("TENDRIL_FAKE_MACHINE", "arm64"),
    )
    os.uname = lambda: posix.uname_result(_fields)
'''

NODENAME = "Janes-MacBook-Pro.local"
HOST = "Janes-MacBook-Pro"

STUBS = {
    "uname": 'case "$1" in\n'
             '    -s) echo Darwin ;;\n'
             '    -m) echo "${TENDRIL_ARCH:-arm64}" ;;\n'
             '    -n) echo "%s" ;;\n'
             '    -r) echo 24.0.0 ;;\n'
             '    *) echo Darwin ;;\n'
             'esac\n' % NODENAME,
    "launchctl": 'printf \'%s\\n\' "$*" >> "$TENDRIL_LAUNCHCTL_LOG"\nexit 0\n',
    "sysctl": 'if [ "$1 $2" = "-n hw.memsize" ]; then\n'
              '    echo 17179869184\n'
              'elif [ "$1 $2" = "-n kern.boottime" ]; then\n'
              '    printf \'%s\\n\' \'{ sec = 1700000000, usec = 0 } '
              'Tue Nov 14 06:13:20 2023 PST\'\n'
              'else\n'
              '    exit 0\n'
              'fi\n',
    "vm_stat": 'cat <<\'EOF\'\n'
               'Mach Virtual Memory Statistics: (page size of 16384 bytes)\n'
               'Pages free: 21894.\n'
               'Pages active: 426591.\n'
               'Pages inactive: 201301.\n'
               'Pages speculative: 51234.\n'
               'Pages wired down: 178230.\n'
               'Pages purgeable: 16.\n'
               'EOF\n',
    "herdr": 'case "$1" in\n'
             '    --version) echo "herdr 0.9.3" ;;\n'
             '    status)\n'
             '        printf \'status: running\\n\'\n'
             '        printf \'endpoint_compatible: yes\\n\'\n'
             '        printf \'private_protocol_compatible: yes\\n\'\n'
             '        printf \'version: 0.9.3\\n\'\n'
             '        printf \'private_protocol: 1\\n\'\n'
             '        ;;\n'
             '    *) exit 0 ;;\n'
             'esac\n',
    "ssh": 'printf \'ssh %s\\n\' "$*" >> "$TENDRIL_TOOL_LOG"\nexit 0\n',
    "mosh": 'printf \'mosh %s\\n\' "$*" >> "$TENDRIL_TOOL_LOG"\nexit 0\n',
    # SHELL points here: tendril_host's remote-path check runs
    # `$SHELL -c probe` (this stub always reports every probe missing),
    # while remote_rc_files() keys off the basename "zsh" (without
    # executing it) to pick ~/.zshenv + ~/.profile. Together that makes
    # step 3b of the installer ask its PATH question on EVERY machine --
    # some /etc/profiles prepend $HOME/.local/bin, others do not.
    "zsh": 'printf \'%s\\n\' "${TENDRIL_MISSING_PROBES:-remote-agents}"\n'
           'exit 0\n',
    "tailscale": 'printf \'tailscale %s\\n\' "$*" >> "$TENDRIL_TOOL_LOG"\n'
                 'exit 1\n',
    # Linux-only tools: the Darwin path must never even try them.
    "systemctl": 'printf \'%s\\n\' "$*" >> "$TENDRIL_LINUX_LOG"\nexit 1\n',
    "journalctl": 'printf \'%s\\n\' "$*" >> "$TENDRIL_LINUX_LOG"\nexit 1\n',
    "loginctl": 'printf \'%s\\n\' "$*" >> "$TENDRIL_LINUX_LOG"\nexit 1\n',
    # Homebrew present so pkg_hint never falls through to another manager.
    "brew": 'exit 1\n',
}


class FakeDarwin(unittest.TestCase):
    arch = "arm64"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril darwin ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "tendril home dir")
        os.makedirs(self.home)
        self.bindir = os.path.join(self.tmp, "stub bin")
        os.makedirs(self.bindir)
        self.sitedir = os.path.join(self.tmp, "pysite")
        os.makedirs(self.sitedir)
        with open(os.path.join(self.sitedir, "sitecustomize.py"), "w") as f:
            f.write(SITECUSTOMIZE)
        self.launchctl_log = os.path.join(self.tmp, "launchctl.log")
        self.linux_log = os.path.join(self.tmp, "linux-only.log")
        self.tool_log = os.path.join(self.tmp, "tools.log")
        for name, body in STUBS.items():
            self.stub(name, body)

    def stub(self, name, body):
        path = os.path.join(self.bindir, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    def env(self, **over):
        e = dict(os.environ)
        e["PATH"] = os.pathsep.join(
            [self.bindir, "/usr/bin", "/bin", e.get("PATH", "")])
        e["HOME"] = self.home
        e["SHELL"] = os.path.join(self.bindir, "zsh")
        e["TENDRIL_MISSING_PROBES"] = "remote-agents mosh-server"
        e["PYTHONPATH"] = self.sitedir
        e["TENDRIL_ARCH"] = self.arch
        e["TENDRIL_FAKE_UNAME"] = NODENAME
        e["TENDRIL_FAKE_MACHINE"] = self.arch
        e["TENDRIL_LAUNCHCTL_LOG"] = self.launchctl_log
        e["TENDRIL_LINUX_LOG"] = self.linux_log
        e["TENDRIL_TOOL_LOG"] = self.tool_log
        for k in ("XDG_CONFIG_HOME", "TENDRIL_ALIAS", "AGENT_ALIAS"):
            e.pop(k, None)
        e.update(over)
        return e

    def run_script(self, script, args, answers, **env_over):
        return subprocess.run(
            ["/bin/sh", script] + list(args), env=self.env(**env_over),
            input="\n".join(answers) + "\n",
            capture_output=True, text=True, timeout=240)

    def log_lines(self, path):
        try:
            with open(path) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def launchctl_calls(self):
        return self.log_lines(self.launchctl_log)

    def linux_calls(self):
        return self.log_lines(self.linux_log)

    def walk_home(self):
        found = []
        for root, dirs, files in os.walk(self.home):
            found.extend(os.path.join(root, d) for d in dirs)
            found.extend(os.path.join(root, f) for f in files)
        return found

    def seed_user_state(self, bashrc=True):
        """An unrelated LaunchAgent and the user's own shell startup lines
        that both install AND uninstall must preserve. bashrc=False is the
        stock macOS zsh account (no ~/.bashrc at all)."""
        agents = os.path.join(self.home, "Library", "LaunchAgents")
        os.makedirs(agents)
        with open(os.path.join(agents, "com.example.other.plist"), "w") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<plist version="1.0"><dict>'
                    '<key>Label</key><string>com.example.other</string>'
                    '</dict></plist>\n')
        with open(os.path.join(self.home, ".profile"), "w") as f:
            f.write("# dotfiles managed by hand\nexport TENDRIL_USER_LINE=1\n")
        if bashrc:
            with open(os.path.join(self.home, ".bashrc"), "w") as f:
                f.write("# bash user line\n")

    def read(self, rel):
        with open(os.path.join(self.home, rel)) as f:
            return f.read()


class DryRunDarwin(FakeDarwin):
    # dry-run asks the config questions and the watcher/keybinding confirms,
    # but step 3b skips the PATH question entirely.
    ANSWERS = ["", "", "", "", "y", "", ""]

    def test_dry_run_writes_nothing_and_shows_the_launchd_plan(self):
        p = self.run_script(INSTALL, ["--dry-run"], self.ANSWERS)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("Darwin arm64", out)
        self.assertIn("(launchd)", out)                       # watcher manager
        self.assertIn("com.tendril.herdr-notify", out)        # plist preview
        self.assertEqual(self.walk_home(), [])                # zero writes
        self.assertEqual(self.linux_calls(), [])              # no Linux tools
        # the offered default is the short mDNS-free hostname
        self.assertIn("Tailscale hostname of this machine [%s]" % HOST, out)
        self.assertIn("SSH alias the phone will use [%s]" % HOST, out)
        self.assertNotIn("Janes-MacBook-Pro.local", out)
        self.assertIn("TAILSCALE_HOST=%s" % HOST, out)        # config preview
        self.assertNotIn("pacman", out)                       # Homebrew hint

    def test_dry_run_never_bootstraps_the_agent(self):
        self.run_script(INSTALL, ["--dry-run"], self.ANSWERS)
        bootstraps = [c for c in self.launchctl_calls()
                      if c.startswith("bootstrap")]
        self.assertEqual(bootstraps, [])


class DryRunDarwinIntel(FakeDarwin):
    arch = "x86_64"

    def test_dry_run_reports_intel_mac(self):
        p = self.run_script(INSTALL, ["--dry-run"], DryRunDarwin.ANSWERS)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("Darwin x86_64", out)
        self.assertEqual(self.walk_home(), [])
        self.assertEqual(self.linux_calls(), [])


class InstallHints(FakeDarwin):
    def test_missing_mosh_hints_homebrew_not_pacman(self):
        if any(os.path.isfile(os.path.join(d, "mosh"))
               for d in ("/usr/bin", "/bin")):
            self.skipTest("a real mosh sits on the base PATH")
        os.remove(os.path.join(self.bindir, "mosh"))
        p = self.run_script(INSTALL, ["--dry-run"], DryRunDarwin.ANSWERS,
                            PATH=os.pathsep.join([self.bindir, "/usr/bin",
                                                  "/bin"]))
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("mosh    : NOT FOUND", out)
        self.assertIn("brew install mosh", out)
        self.assertNotIn("pacman", out)


class HostInstallDarwin(FakeDarwin):
    # roots, hostname, alias, PATH-block y, ntfy n, watcher y, bindings n n
    # (the mosh-server stub keeps the PATH check failing, so run 2 asks
    # the same questions and gets the same answers as run 1)
    FIRST = ["", "", "", "y", "", "y", "", ""]
    SECOND = FIRST

    def test_install_idempotent_then_uninstall(self):
        self.seed_user_state()
        p = self.run_script(INSTALL, [], self.FIRST)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)

        bindir = os.path.join(self.home, ".local", "bin")
        for name in ("remote-agents", "herdr-notify", "tendril_link.py",
                     "tendril_host.py"):
            path = os.path.join(bindir, name)
            self.assertTrue(os.path.isfile(path), name)
            self.assertTrue(os.access(path, os.X_OK), name)
        tendril = os.path.join(bindir, "tendril")
        self.assertTrue(os.path.islink(tendril))
        self.assertEqual(os.readlink(tendril), "remote-agents")

        config = os.path.join(self.home, ".config", "remote-agents", "config")
        self.assertEqual(os.stat(config).st_mode & 0o777, 0o600)

        plist_path = os.path.join(self.home, "Library", "LaunchAgents",
                                  "com.tendril.herdr-notify.plist")
        self.assertTrue(os.path.isfile(plist_path))
        with open(plist_path, "rb") as f:
            doc = plistlib.load(f)
        self.assertEqual(doc["Label"], "com.tendril.herdr-notify")
        self.assertEqual(len(doc["ProgramArguments"]), 2)
        # the spaces in the temp HOME must land in the argv untouched
        self.assertEqual(doc["ProgramArguments"][1],
                         os.path.join(self.home, ".local", "bin",
                                      "herdr-notify"))
        bootstraps = [c for c in self.launchctl_calls()
                      if c.startswith("bootstrap")]
        self.assertEqual(len(bootstraps), 1, self.launchctl_calls())
        self.assertIn(plist_path, bootstraps[0])

        for rc in (".profile", ".zshenv"):
            self.assertEqual(self.read(rc).count(th.BEGIN), 1, rc)
        self.assertIn("export TENDRIL_USER_LINE=1", self.read(".profile"))
        self.assertIn("wrote managed PATH block", out)

        # ---- second run: idempotent -------------------------------
        p2 = self.run_script(INSTALL, [], self.SECOND)
        out2 = p2.stdout + p.stderr
        self.assertEqual(p2.returncode, 0, out2)
        listing = sorted(os.listdir(os.path.join(self.home, "Library",
                                                 "LaunchAgents")))
        self.assertEqual(listing, ["com.example.other.plist",
                                   "com.tendril.herdr-notify.plist"])
        with open(plist_path, "rb") as f:
            self.assertEqual(plistlib.load(f), doc)
        for rc in (".profile", ".zshenv"):
            self.assertEqual(self.read(rc).count(th.BEGIN), 1, rc)
        self.assertEqual(len([c for c in self.launchctl_calls()
                              if c.startswith("bootstrap")]), 2)

        # ---- uninstall --------------------------------------------
        pu = self.run_script(UNINSTALL, [], ["y", "y"])
        outu = pu.stdout + pu.stderr
        self.assertEqual(pu.returncode, 0, outu)
        self.assertFalse(os.path.exists(plist_path))
        self.assertTrue(os.path.isfile(os.path.join(
            self.home, "Library", "LaunchAgents",
            "com.example.other.plist")))
        self.assertTrue(any(c.startswith("bootout")
                            and "com.tendril.herdr-notify" in c
                            for c in self.launchctl_calls()),
                        self.launchctl_calls())
        for name in ("remote-agents", "herdr-notify", "tendril_link.py",
                     "tendril_host.py"):
            self.assertFalse(os.path.exists(os.path.join(bindir, name)), name)
        self.assertFalse(os.path.lexists(tendril))
        for rc in (".profile", ".zshenv"):
            self.assertNotIn(th.BEGIN, self.read(rc))
        self.assertIn("export TENDRIL_USER_LINE=1", self.read(".profile"))
        bashrc = self.read(".bashrc")            # no block was ever added
        self.assertNotIn(th.BEGIN, bashrc)
        self.assertIn("# bash user line", bashrc)
        self.assertFalse(os.path.exists(os.path.join(
            self.home, ".config", "remote-agents")))
        # the whole Darwin cycle never called a Linux-only tool
        self.assertEqual(self.linux_calls(), [])


class ZshOnlyUninstall(FakeDarwin):
    """Stock macOS account: zsh, no ~/.bashrc. Uninstall must still find
    and remove the managed block (regression: a multi-file grep exits 2
    when one listed file is missing, which used to skip the removal)."""

    def test_block_removed_without_a_bashrc(self):
        self.seed_user_state(bashrc=False)
        p = self.run_script(INSTALL, [], HostInstallDarwin.FIRST)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for rc in (".profile", ".zshenv"):
            self.assertEqual(self.read(rc).count(th.BEGIN), 1, rc)
        pu = self.run_script(UNINSTALL, [], ["y", "y"])
        self.assertEqual(pu.returncode, 0, pu.stdout + pu.stderr)
        for rc in (".profile", ".zshenv"):
            self.assertNotIn(th.BEGIN, self.read(rc), rc)
        self.assertIn("export TENDRIL_USER_LINE=1", self.read(".profile"))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".bashrc")))


class UninstallWithoutHelper(FakeDarwin):
    """An uninstall script with no tendril_host.py beside it or installed
    (e.g. a partial copy) must still unload and remove the LaunchAgent
    instead of leaving launchd respawning a deleted watcher."""

    def test_launchagent_removed_by_hand(self):
        lone = os.path.join(self.tmp, "lone copy")
        os.makedirs(lone)
        shutil.copy(UNINSTALL, os.path.join(lone, "uninstall"))
        agents = os.path.join(self.home, "Library", "LaunchAgents")
        os.makedirs(agents)
        plist = os.path.join(agents, "com.tendril.herdr-notify.plist")
        with open(plist, "w") as f:
            f.write("<plist/>\n")
        p = self.run_script(os.path.join(lone, "uninstall"), [], ["n", "n"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(plist))
        self.assertTrue(any(c.startswith("bootout") and "com.tendril.herdr-notify" in c
                            for c in self.launchctl_calls()), self.launchctl_calls())
        self.assertEqual(self.linux_calls(), [])


class DoctorDarwin(FakeDarwin):
    def test_doctor_is_read_only_and_darwin_native(self):
        p = self.run_script(INSTALL, ["--doctor"], [])
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("TENDRIL // SYSTEM CHECK", out)
        self.assertIn("Platform", out)
        self.assertIn("Darwin arm64", out)
        self.assertIn("0.9.3", out)                 # herdr protocol probe
        self.assertEqual(self.linux_calls(), [])
        self.assertEqual(self.walk_home(), [])


class BadOption(FakeDarwin):
    def test_unknown_option_exits_two(self):
        p = self.run_script(INSTALL, ["--bogus"], [])
        self.assertEqual(p.returncode, 2)
        self.assertIn("unknown option", p.stderr)


if __name__ == "__main__":
    unittest.main()
