"""Installer ordering: provenance is recorded before anything reads it.

Regression for the install-time traceback: the provenance line used to be
    say "Install provenance (what `tendril --upgrade` reads):"
and the backticks inside double quotes are a live command substitution,
so ./install itself executed tendril --upgrade. On a first install there
was no install.json yet, tendril_upgrade.discover_source raised Refused,
and remote-agents' bare upgrade_main() call printed a full traceback; on
a reinstall the substitution silently started a REAL upgrade mid-install.

Pinned here: a fresh install into an empty fake HOME never executes the
tendril/remote-agents upgrade flow at all (recording stubs on PATH),
still exits 0 with install.json written and no traceback in the output,
ends with the concise INSTALL COMPLETE banner, and no user-facing output
string in the installer ever carries a backtick again. A refused upgrade
through the installed remote-agents prints exactly one guidance line.
"""
import os
import re
import subprocess
import sys
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:  # importable however the suite is invoked
    sys.path.insert(0, TESTS)
from test_darwin_install import INSTALL, FakeDarwin  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Records every invocation, then exits 0: if install ever runs `tendril`
# or `remote-agents` (command substitution or otherwise), the log grows.
RECORDER = 'printf \'%s\\n\' "$*" >> "$TENDRIL_RECORD"\nexit 0\n'

FRESH_ANSWERS = ["", "y", "", ""]


class FreshInstall(FakeDarwin):
    """A first install into an empty fake HOME (no install.json yet),
    with recording tendril/remote-agents stubs ahead on PATH."""

    def setUp(self):
        super().setUp()
        self.record = os.path.join(self.tmp, "upgrade-flow.log")
        for name in ("tendril", "remote-agents"):
            self.stub(name, RECORDER)

    def env(self, **over):
        return super().env(TENDRIL_RECORD=self.record, **over)

    def install(self):
        return self.run_script(INSTALL, [], FRESH_ANSWERS)

    def upgrade_flow_calls(self):
        try:
            with open(self.record) as f:
                return f.read().splitlines()
        except OSError:
            return []  # the stubs were never invoked at all

    def install_json(self):
        return os.path.join(self.home, ".config", "remote-agents",
                            "install.json")

    def test_fresh_install_is_clean_and_never_runs_the_upgrade_flow(self):
        p = self.install()
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("Refused", out)
        self.assertIn("TENDRIL // INSTALL COMPLETE", out)
        self.assertIn("provenance recorded", out)
        self.assertTrue(os.path.isfile(self.install_json()), out)
        self.assertEqual(self.upgrade_flow_calls(), [])

    def test_provenance_is_recorded_before_the_later_sections_read_it(self):
        # The provenance step now lives at the end of "3) Install files":
        # its output line must appear before the sections that follow.
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        prov_at = p.stdout.index("Install provenance")
        for later in ("4) Push notifications", "5) Notification watcher",
                      "TENDRIL // INSTALL COMPLETE"):
            self.assertLess(prov_at, p.stdout.index(later))
        with open(self.install_json()) as f:
            self.assertTrue(f.read().strip())

    def test_refused_upgrade_through_remote_agents_is_one_clean_line(self):
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        os.remove(self.install_json())
        installed = os.path.join(self.home, ".local", "bin", "remote-agents")
        # run OUTSIDE the checkout, with no provenance: discover_source
        # must refuse, and the refusal must arrive as guidance, not a
        # traceback (mirrors tendril_upgrade.main's handling).
        q = subprocess.run([installed, "--upgrade"], cwd=self.tmp,
                           env=self.env(), capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(q.returncode, 1, q.stdout + q.stderr)
        err = q.stderr.splitlines()
        self.assertEqual(len(err), 1, repr(q.stderr))
        self.assertTrue(err[0].startswith("tendril --upgrade:"), q.stderr)
        self.assertNotIn("Traceback", q.stderr)


class InstallerStringsAreSubstitutionFree(unittest.TestCase):
    """Backticks inside double-quoted say/echo/printf strings are live
    command substitutions: `say "see tendril --upgrade"` would RUN it.
    Cheap static guard so the install-time upgrade bug cannot return."""

    SCRIPTS = (
        INSTALL,
        os.path.join(REPO, "uninstall"),
        os.path.join(REPO, "docs", "scripts", "tendril-phone-setup.sh"),
    )
    OUTPUT_STRING = re.compile(r'\b(say|echo|printf)\s+"')

    def test_no_backticks_in_output_strings(self):
        bad = []
        for path in self.SCRIPTS:
            with open(path, encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    if self.OUTPUT_STRING.search(line) and "`" in line:
                        bad.append("%s:%d: %s" % (path, n, line.strip()))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
