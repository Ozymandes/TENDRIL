"""phone/tendril fixed verbs + the selector L key (LINK-CONTRACT-0.2 §0/§4).

Drives the real launcher under `sh` with stub ssh/mosh/am/termux-am/
remote-agents on PATH (the same fake-ssh-on-PATH approach as
test_client_install), recording every argv in TENDRIL_RECORD. Covers:

- the fixed verbs' exact ssh argv/remote strings: snapshot, capacity,
  schedule show|cancel|add (BatchMode, ConnectTimeout=6, single-quoted id,
  contract regexes enforced locally BEFORE anything is dialed);
- every injection vector (quotes, $(), backticks, spaces, newline, ';',
  oversize, uppercase hex, wrong id shape) is refused locally with
  NOTHING on the wire;
- `tendril link` with no args runs exactly
  `am start -n app.tendril.link/.MainActivity` locally and nothing else;
  extra args are refused; `tendril link <id>` keeps the host deep-link
  verb (one plain ssh round trip);
- the selector's L key (LINK_OPEN_EXIT = 75): over ssh the launcher
  turns it into `am start`; over mosh the status is lost (mosh-client
  does not propagate it), so the host selector's own one-line hint is
  all the user gets and `am` never runs; selector exit 0 never runs am.
- the selector run directly on the HOST prints the same hint and exits
  75 — honest, never a silent exit.

HOME is a temp dir; the real home is never touched. Stdlib only.
"""
import importlib.util
import io
import contextlib
import os
import shutil
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO, "phone", "tendril")
RA = os.path.join(REPO, "bin", "remote-agents")

GOOD_ID = "t-20261010-a1b2c3d4"
GOOD_PAYLOAD = "QXJjaGl0ZWN0dXJlLWF1ZGl0LXBsYW5uZWQ"      # base64url chars only
HINT = ("L opens TENDRIL Link on the phone — "
        "if nothing opened, run: tendril link")
AM_LINE = "am start -n app.tendril.link/.MainActivity"

# ssh stub: the BatchMode key probe (last arg "true") always succeeds so a
# test can reach the interactive ssh; every other call exits TENDRIL_SSH_RC.
SSH_STUB = ('case " $*" in *" true") exit 0 ;; esac\n'
            'printf \'ssh %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'
            'exit "${TENDRIL_SSH_RC:-0}"\n')
# mosh stub: runs the remote payload like the real server would, then exits
# TENDRIL_MOSH_RC (default 0) — mosh-client does NOT propagate the remote
# exit status, and this stub encodes exactly that.
MOSH_STUB = ('printf \'mosh %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'
             'while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done\n'
             '[ $# -gt 0 ] && shift\n'
             '[ "$1" = "sh" ] && shift\n'
             '[ "$1" = "-lc" ] && shift\n'
             'sh -c "$1"\n'
             'exit "${TENDRIL_MOSH_RC:-0}"\n')
# remote-agents stub: records argv, optionally prints a line (the host
# selector's L hint) and exits TENDRIL_REMOTE_RC.
REMOTE_STUB = ('printf \'remote-agents %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'
               '[ -n "${TENDRIL_REMOTE_MSG:-}" ] '
               '&& printf \'%s\\n\' "$TENDRIL_REMOTE_MSG"\n'
               'exit "${TENDRIL_REMOTE_RC:-0}"\n')
AM_STUB = 'printf \'am %s\\n\' "$*" >> "$TENDRIL_RECORD"\nexit 0\n'
TERMUX_AM_STUB = ('printf \'termux-am %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'
                  'exit 0\n')


def load_ra():
    """The real bin/remote-agents as a module (selector-L unit checks)."""
    loader = SourceFileLoader("remote_agents_phone_verbs", RA)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class LauncherEnv(unittest.TestCase):
    """Temp HOME + stub bin dir; every argv lands in TENDRIL_RECORD."""

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril phone verbs ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "phone home")
        self.bindir = os.path.join(self.tmp, "stub bin")
        os.makedirs(self.home)
        os.makedirs(self.bindir)
        self.record = os.path.join(self.tmp, "calls.log")
        for name, body in (("ssh", SSH_STUB), ("mosh", MOSH_STUB),
                           ("remote-agents", REMOTE_STUB), ("am", AM_STUB),
                           ("termux-am", TERMUX_AM_STUB)):
            self.stub(name, body)

    def stub(self, name, body):
        path = os.path.join(self.bindir, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)

    def env(self, **over):
        e = dict(os.environ)
        e["PATH"] = os.pathsep.join([self.bindir, "/usr/bin", "/bin"])
        e["HOME"] = self.home                                   # isolated
        e["TENDRIL_RECORD"] = self.record
        for k in ("TENDRIL_ALIAS", "AGENT_ALIAS", "TENDRIL_SSH_RC",
                  "TENDRIL_MOSH_RC", "TENDRIL_REMOTE_RC",
                  "TENDRIL_REMOTE_MSG"):
            e.pop(k, None)
        e.update(over)
        return e

    def run_launcher(self, args, **over):
        return subprocess.run(["sh", LAUNCHER] + list(args),
                              env=self.env(**over),
                              capture_output=True, text=True, timeout=60)

    def clear(self):
        open(self.record, "w").close()

    def calls(self):
        try:
            with open(self.record) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def am_calls(self):
        return [c for c in self.calls() if c.startswith(("am ", "termux-am "))]

    def ssh_calls(self):
        return [c for c in self.calls() if c.startswith("ssh ")]


class FixedVerbs(LauncherEnv):
    """snapshot / capacity / schedule: exact argv, local validation."""

    def test_snapshot_is_one_batchmode_ssh_with_a_fixed_command(self):
        p = self.run_launcher(["testhost", "snapshot"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["ssh -o BatchMode=yes -o ConnectTimeout=6 "
                          "testhost remote-agents snapshot"])

    def test_snapshot_extra_arg_is_refused_without_dialing(self):
        p = self.run_launcher(["snapshot", "--evil"])
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])
        self.assertIn("usage", p.stdout + p.stderr)

    def test_snapshot_explicit_alias_extra_arg_is_refused(self):
        p = self.run_launcher(["testhost", "snapshot", "--evil"])
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])
        self.assertIn("usage", p.stdout + p.stderr)

    def test_capacity_is_one_batchmode_ssh_with_a_fixed_command(self):
        p = self.run_launcher(["testhost", "capacity"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["ssh -o BatchMode=yes -o ConnectTimeout=6 "
                          "testhost remote-agents capacity --json"])

    def test_capacity_extra_arg_is_refused_without_dialing(self):
        p = self.run_launcher(["testhost", "capacity", "--json"])
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])
        self.assertIn("usage", p.stdout + p.stderr)

    def test_schedule_show_quotes_the_id_over_batchmode_ssh(self):
        p = self.run_launcher(["testhost", "schedule", "show", GOOD_ID])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["ssh -o BatchMode=yes -o ConnectTimeout=6 "
                          "testhost remote-agents schedule show "
                          "'%s'" % GOOD_ID])

    def test_schedule_cancel_quotes_the_id_over_batchmode_ssh(self):
        p = self.run_launcher(["testhost", "schedule", "cancel", GOOD_ID])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["ssh -o BatchMode=yes -o ConnectTimeout=6 "
                          "testhost remote-agents schedule cancel "
                          "'%s'" % GOOD_ID])

    def test_schedule_add_quotes_the_payload_over_batchmode_ssh(self):
        p = self.run_launcher(["testhost", "schedule", "add", GOOD_PAYLOAD])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["ssh -o BatchMode=yes -o ConnectTimeout=6 "
                          "testhost remote-agents schedule add "
                          "'%s'" % GOOD_PAYLOAD])

    def test_schedule_add_takes_the_full_contract_length(self):
        p = self.run_launcher(["testhost", "schedule", "add", "b" * 16384])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("schedule add '%s'" % ("b" * 16384), self.calls()[0])
        self.clear()
        p = self.run_launcher(["testhost", "schedule", "add", "b" * 16385])
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])          # oversize never dialed

    def test_default_alias_form_matches_the_explicit_alias_form(self):
        for args in (["snapshot"], ["capacity"],
                     ["schedule", "show", GOOD_ID],
                     ["schedule", "add", GOOD_PAYLOAD]):
            self.clear()
            self.run_launcher(list(args), TENDRIL_ALIAS="testhost")
            explicit = self.calls()
            self.clear()
            self.run_launcher(["testhost"] + list(args))
            self.assertEqual(self.calls(), explicit, args)


class FixedVerbValidation(LauncherEnv):
    """Every injection vector is refused locally: nothing is ever dialed."""

    def refused(self, args):
        p = self.run_launcher(["testhost"] + list(args))
        self.assertEqual(p.returncode, 2, args)
        self.assertEqual(self.calls(), [], args)     # NOTHING reached ssh
        self.assertIn("usage", p.stdout + p.stderr, args)

    def test_show_cancel_reject_every_injection_vector(self):
        bad_ids = (
            "t-20261010-a1b2c3d'; rm -rf /tmp",      # quote + semicolon
            "t-20261010-a1b2c3d4; rm -rf /tmp",      # semicolon
            "$(id)",                                 # command substitution
            "`id`",                                  # backticks
            "t-20261010 a1b2c3d4",                   # spaces
            "t-20261010-a1b2c3d4\nrm -rf /tmp",      # newline
            "x" * 200,                               # oversize
            "t-20261010-A1B2C3D4",                   # uppercase hex
            "20261010-a1b2c3d4",                     # missing t- prefix
            "t-2026-1010-a1b2c3d4",                  # wrong date shape
            "t-20261010-a1b2c3d",                    # short hex
            "x",                                     # not an id at all
            "",                                      # empty
        )
        for sub in ("show", "cancel"):
            for bad in bad_ids:
                self.refused(["schedule", sub, bad])

    def test_add_reject_every_injection_vector(self):
        bad_payloads = (
            "a'; rm -rf /tmp",                       # quote + semicolon
            "a;b",                                   # semicolon
            "$(id)",                                 # command substitution
            "`id`",                                  # backticks
            "a b",                                   # space
            "a\nb",                                  # newline
            "a" + '"' + "b",                         # double quote
            "x" * 16385,                             # oversize
            "",                                      # empty
        )
        for bad in bad_payloads:
            self.refused(["schedule", "add", bad])

    def test_schedule_rejects_unknown_subcommands_and_missing_args(self):
        self.refused(["schedule", "delete", GOOD_ID])
        self.refused(["schedule", "frobnicate"])
        self.refused(["schedule", "show"])           # sub without its id
        self.refused(["schedule"])                   # sub without anything
        self.refused(["schedule", "show", GOOD_ID, "extra"])


class LinkVerb(LauncherEnv):
    """`tendril link`: local app launch vs the host deep-link verb."""

    def test_no_args_runs_am_start_exactly_and_nothing_else(self):
        p = self.run_launcher(["link"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(), [AM_LINE])     # am, and ONLY am
        self.assertEqual(self.ssh_calls(), [])        # local only: no ssh

    def test_no_args_needs_no_alias(self):
        p = self.run_launcher(["link"], TENDRIL_ALIAS="")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(), [AM_LINE])

    def test_termux_am_is_the_fallback_component(self):
        os.remove(os.path.join(self.bindir, "am"))
        p = self.run_launcher(["link"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["termux-am start -n app.tendril.link/.MainActivity"])

    def test_no_am_at_all_fails_honestly_without_ssh(self):
        os.remove(os.path.join(self.bindir, "am"))
        os.remove(os.path.join(self.bindir, "termux-am"))
        p = self.run_launcher(["link"])
        self.assertEqual(p.returncode, 1)
        self.assertIn("'am' not found", p.stdout + p.stderr)
        self.assertEqual(self.calls(), [])

    def test_two_or_more_args_are_refused(self):
        for args in (["link", "x", "y"], ["link", "a", "b", "c"]):
            p = self.run_launcher(args)
            self.assertEqual(p.returncode, 2, args)
            self.assertEqual(self.calls(), [], args)  # no am, no ssh
            self.assertIn("usage", p.stdout + p.stderr)

    def test_link_with_id_keeps_the_host_deep_link_verb(self):
        p = self.run_launcher(["testhost", "link", "w3"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["ssh testhost remote-agents link 'w3'"])

    def test_link_with_id_via_default_alias(self):
        p = self.run_launcher(["link", "w3"], TENDRIL_ALIAS="testhost")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["ssh testhost remote-agents link 'w3'"])

    def test_link_bad_id_is_refused_locally(self):
        for bad in ("bad id", "", "x;y", "$(id)", "`id`", "'",
                    "w" * 129, "w15\nrm"):
            p = self.run_launcher(["link", bad])
            self.assertEqual(p.returncode, 2, bad)
            self.assertEqual(self.calls(), [], bad)
            out = p.stdout + p.stderr
            self.assertIn("usage: tendril [<alias>] link <session-id>", out)
            self.assertIn("A-Z a-z 0-9 . _ : -", out)

    def test_link_with_id_still_refuses_a_third_argument(self):
        p = self.run_launcher(["link", "w3", "extra"])
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])


class SelectorLKey(LauncherEnv):
    """The selector's L key (75): am over ssh, hint over mosh/host."""

    def test_over_ssh_exit_75_runs_am(self):
        p = self.run_launcher(["ssh", "testhost"], TENDRIL_SSH_RC="75",
                              TENDRIL_REMOTE_MSG=HINT, TENDRIL_REMOTE_RC="75")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.am_calls(), [AM_LINE])

    def test_over_ssh_exit_0_never_runs_am(self):
        p = self.run_launcher(["ssh", "testhost"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.am_calls(), [])

    def test_over_ssh_other_statuses_pass_through_without_am(self):
        p = self.run_launcher(["ssh", "testhost"], TENDRIL_SSH_RC="5")
        self.assertEqual(p.returncode, 5)
        self.assertEqual(self.am_calls(), [])

    def test_over_mosh_the_hint_is_printed_and_am_never_runs(self):
        # The host selector prints the hint and exits 75; mosh-client does
        # not propagate the status (the stub encodes that), so the launcher
        # exits 0 with the hint on the terminal and no `am` anywhere.
        p = self.run_launcher(["testhost"], TENDRIL_REMOTE_MSG=HINT,
                              TENDRIL_REMOTE_RC="75")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("run: tendril link", p.stdout)
        self.assertEqual(self.am_calls(), [])
        mosh = [c for c in self.calls() if c.startswith("mosh ")]
        self.assertEqual(len(mosh), 1)
        self.assertEqual(self.ssh_calls(), [])       # no ssh fallback here

    def test_over_mosh_exit_0_is_quiet(self):
        p = self.run_launcher(["testhost"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("tendril link", p.stdout)
        self.assertEqual(self.am_calls(), [])

    def test_mosh_own_failure_still_falls_back_to_ssh(self):
        p = self.run_launcher(["testhost"], TENDRIL_MOSH_RC="17")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("mosh failed - falling back to ssh", p.stdout)
        self.assertTrue(any(c.startswith("mosh ") for c in self.calls()))
        self.assertIn("ssh -t testhost remote-agents", self.calls())
        self.assertEqual(self.am_calls(), [])

    def test_selector_l_on_the_host_prints_the_hint_and_exits_75(self):
        ra = load_ra()
        out = io.StringIO()
        with mock.patch.object(ra, "read_menu_action",
                               return_value=("line", "l")):
            with contextlib.redirect_stdout(out):
                rc = ra.menu_loop([], tty_mode=True)
        self.assertEqual(rc, ra.LINK_OPEN_EXIT)
        self.assertEqual(rc, 75)
        self.assertIn("L opens TENDRIL Link", out.getvalue())
        self.assertIn("run: tendril link", out.getvalue())

    def test_selector_quit_on_the_host_prints_no_hint(self):
        ra = load_ra()
        out = io.StringIO()
        with mock.patch.object(ra, "read_menu_action",
                               return_value=("line", "q")):
            with contextlib.redirect_stdout(out):
                rc = ra.menu_loop([], tty_mode=True)
        self.assertEqual(rc, 0)
        self.assertNotIn("tendril link", out.getvalue())


class LauncherHygiene(LauncherEnv):
    def test_launcher_parses_as_posix_sh(self):
        # shellcheck is not installed on this machine; `sh -n` is the
        # syntax gate we CAN run (full POSIX lint is manual + CI's job).
        p = subprocess.run(["sh", "-n", LAUNCHER], capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main()
