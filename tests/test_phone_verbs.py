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
import base64
import importlib.util
import json
import io
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO, "phone", "tendril")
RA = os.path.join(REPO, "bin", "remote-agents")

GOOD_ID = "t-20261010-a1b2c3d4"
GOOD_PAYLOAD = "QXJjaGl0ZWN0dXJlLWF1ZGl0LXBsYW5uZWQ"      # base64url chars only

# RC2 connection reuse (phone/tendril builds these from CM_OPTS /
# BATCH_CM_OPTS / SESSION_CM_OPTS): every launcher ssh multiplexes over
# one control socket per destination. ControlPersist is BatchMode-only
# (background verbs own the warm master; interactive sessions never
# linger), and the interactive/plain set carries the bounded
# ConnectTimeout the old preflight used to provide.
CM = "-o ControlMaster=auto -o ControlPath=~/.ssh/tendril-cm-%C"
BATCH_CM = CM + " -o ControlPersist=120 -o ConnectionAttempts=1"
SESSION_CM = CM + " -o ConnectionAttempts=1 -o ConnectTimeout=6"
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
# ssh stub that ANSWERS the link-open flag request (the real mosh-path
# fix): the consume call prints `open` when TENDRIL_FLAG_OPEN is set;
# every other call records and exits 0 (the key probe included).
FLAG_SSH_STUB = (
    'printf \'ssh %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'
    'case " $*" in *" --link-open-request"*) '
    '[ -n "${TENDRIL_FLAG_OPEN:-}" ] && printf \'open\\n\' ;; esac\n'
    'exit 0\n')
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
                          + BATCH_CM +
                          " testhost remote-agents snapshot"])

    def test_snapshot_extra_arg_is_refused_without_dialing(self):
        p = self.run_launcher(["snapshot", "--evil"])
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])
        self.assertIn("usage", p.stdout + p.stderr)

    def test_snapshot_etag_is_forwarded_single_quoted(self):
        p = self.run_launcher(["testhost", "snapshot", "0123456789abcdef"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["ssh -o BatchMode=yes -o ConnectTimeout=6 "
                          + BATCH_CM +
                          " testhost remote-agents snapshot"
                          " --if-none-match '0123456789abcdef'"])

    def test_snapshot_malformed_etag_is_refused_without_dialing(self):
        for bad in ("ZZTOOLONG99999", "0123456789ABCDEF",
                    "0123456789abcdef0", "a; b"):
            p = self.run_launcher(["testhost", "snapshot", bad])
            self.assertEqual(p.returncode, 2, bad)
            self.assertEqual(self.calls(), [], bad)
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
                          + BATCH_CM +
                          " testhost remote-agents capacity --json"])

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
                          + BATCH_CM + " testhost remote-agents schedule show "
                          "'%s'" % GOOD_ID])

    def test_schedule_cancel_quotes_the_id_over_batchmode_ssh(self):
        p = self.run_launcher(["testhost", "schedule", "cancel", GOOD_ID])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["ssh -o BatchMode=yes -o ConnectTimeout=6 "
                          + BATCH_CM + " testhost remote-agents schedule cancel "
                          "'%s'" % GOOD_ID])

    def test_schedule_add_quotes_the_payload_over_batchmode_ssh(self):
        p = self.run_launcher(["testhost", "schedule", "add", GOOD_PAYLOAD])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["ssh -o BatchMode=yes -o ConnectTimeout=6 "
                          + BATCH_CM + " testhost remote-agents schedule add "
                          "'%s'" % GOOD_PAYLOAD])

    # ---- §7.3: strict target schema on the launcher side ------------

    def b64(self, doc):
        import base64
        return base64.urlsafe_b64encode(
            json.dumps(doc).encode()).decode().rstrip("=")

    def new_target_payload(self, target):
        return self.b64({"title": "t", "prompt": "p", "workspace": "w15",
                         "harness": "pi",
                         "trigger": {"kind": "at", "at": 1791590000},
                         "target": target})

    def test_target_new_form_dials(self):
        p = self.run_launcher(["testhost", "schedule", "add",
                               self.new_target_payload({"kind": "new"})])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_target_existing_form_dials(self):
        for t in ({"kind": "existing", "pane": "w16:p5", "harness": "pi"},
                  {"kind": "existing", "pane": "w9:pB", "harness": "claude",
                   "agent_started_at": 1791580000}):
            open(self.record, "w").close()      # one dial per case
            p = self.run_launcher(["testhost", "schedule", "add",
                                   self.new_target_payload(t)])
            self.assertEqual(p.returncode, 0, (t, p.stdout + p.stderr))
            self.assertEqual(len(self.calls()), 1)

    def test_malformed_targets_refused_with_nothing_on_the_wire(self):
        bad = [
            {"kind": "other"},
            {"kind": "existing"},
            {"kind": "existing", "pane": "w16:p5; rm -rf", "harness": "pi"},
            {"kind": "existing", "pane": "p5", "harness": "pi"},
            {"kind": "existing", "pane": "w16:p5", "harness": "PI"},
            {"kind": "existing", "pane": "w16:p5", "harness": "pi",
             "agent_started_at": "soon"},
            {"kind": "existing", "pane": "w16:p5", "harness": "pi",
             "extra": 1},
            "existing",
            {"kind": "new", "pane": "w16:p5"},
        ]
        for t in bad:
            p = self.run_launcher(["testhost", "schedule", "add",
                                   self.new_target_payload(t)])
            self.assertEqual(p.returncode, 2, t)
            self.assertEqual(self.calls(), [], t)   # NOTHING dialed
            self.assertIn("usage", p.stdout + p.stderr)

    def test_truncated_target_object_is_refused(self):
        import base64
        raw = '{"title":"t","prompt":"p","workspace":"w15",'
        raw += '"harness":"pi","trigger":{"kind":"at","at":1},'
        raw += '"target":{"kind":'
        payload = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        p = self.run_launcher(["testhost", "schedule", "add", payload])
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])

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
                         ["ssh " + SESSION_CM + " testhost remote-agents link 'w3'"])

    def test_link_with_id_via_default_alias(self):
        p = self.run_launcher(["link", "w3"], TENDRIL_ALIAS="testhost")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["ssh " + SESSION_CM + " testhost remote-agents link 'w3'"])

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


    def test_link_manifest_is_one_batchmode_ssh_with_a_fixed_command(self):
        p = self.run_launcher(["testhost", "link-manifest"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(),
                         ["ssh -o BatchMode=yes -o ConnectTimeout=6 "
                          + BATCH_CM +
                          " testhost remote-agents --phone-manifest"])

    def test_link_manifest_extra_arg_is_refused_without_dialing(self):
        p = self.run_launcher(["link-manifest", "--evil"])
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])
        self.assertIn("usage", p.stdout + p.stderr)


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
        # consumes the host-side L flag with one BatchMode ssh; the stub
        # answers nothing, so no `am` anywhere — the hint is the UX.
        p = self.run_launcher(["testhost"], TENDRIL_REMOTE_MSG=HINT,
                              TENDRIL_REMOTE_RC="75")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("run: tendril link", p.stdout)
        self.assertEqual(self.am_calls(), [])
        mosh = [c for c in self.calls() if c.startswith("mosh ")]
        self.assertEqual(len(mosh), 1)
        # no ssh FALLBACK here — the only ssh is the flag consume, which
        # (stub answers nothing) opens nothing: the hint is the UX.
        consume = [c for c in self.ssh_calls() if "--link-open-request" in c]
        self.assertEqual(len(consume), 1)

    def test_over_mosh_the_flag_opens_the_app(self):
        # The REAL mosh fix (v0.2.x): the selector leaves a host-side flag;
        # the launcher consumes it with one BatchMode ssh and runs the fixed
        # am start when the host answers `open`.
        self.stub("ssh", FLAG_SSH_STUB)
        p = self.run_launcher(["testhost"], TENDRIL_REMOTE_MSG=HINT,
                              TENDRIL_REMOTE_RC="75", TENDRIL_FLAG_OPEN="1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.am_calls(), [AM_LINE])

    def test_over_mosh_no_flag_opens_nothing(self):
        self.stub("ssh", FLAG_SSH_STUB)
        p = self.run_launcher(["testhost"], TENDRIL_REMOTE_MSG=HINT,
                              TENDRIL_REMOTE_RC="75")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.am_calls(), [])

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
        # the probe ran (its rc 0: key auth fine) and only THEN the fallback
        self.assertIn("ssh -t " + SESSION_CM + " testhost remote-agents",
                      self.calls())
        self.assertEqual(self.am_calls(), [])

    def test_selector_l_on_the_host_prints_the_hint_and_exits_75(self):
        ra = load_ra()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            with mock.patch.object(ra, "read_menu_action",
                                   return_value=("line", "l")):
                with contextlib.redirect_stdout(out):
                    rc = ra.menu_loop([], tty_mode=True)
        self.assertEqual(rc, ra.LINK_OPEN_EXIT)
        self.assertEqual(rc, 75)
        self.assertIn("L opens TENDRIL Link", out.getvalue())
        self.assertIn("run: tendril link", out.getvalue())

    NONCE = "0123456789abcdef"

    def request_path(self, nonce=None):
        return os.path.join(self.tmp, ".local", "state", "tendril",
                            "link-open", nonce or self.NONCE)

    def press(self, ra, key, **env):
        base = {"HOME": self.tmp}
        base.update(env)
        with mock.patch.dict(os.environ, base):
            for k in ("SSH_CONNECTION", "TENDRIL_LINK_NONCE"):
                if k not in env:
                    os.environ.pop(k, None)
            with mock.patch.object(ra, "read_menu_action",
                                   return_value=("line", key)):
                with contextlib.redirect_stdout(io.StringIO()):
                    return ra.menu_loop([], tty_mode=True)

    def test_selector_l_over_mosh_writes_the_request_despite_ssh_connection(self):
        # PHYSICAL S21 ROOT CAUSE (build 158): mosh-server inherits
        # SSH_CONNECTION from its ssh bootstrap (verified on this host:
        # `mosh omarchy -- env` shows SSH_CONNECTION=...), so the old
        # "SSH_CONNECTION set => ssh transport" gate skipped the flag on
        # EVERY mosh session and L behaved exactly like Q.
        ra = load_ra()
        rc = self.press(ra, "l", SSH_CONNECTION="100.78.155.82 34062 100.78.155.82 22",
                        TENDRIL_LINK_NONCE=self.NONCE)
        self.assertEqual(rc, ra.LINK_OPEN_EXIT)
        with open(self.request_path()) as f:
            self.assertEqual(f.read().strip(), "open")

    def test_selector_l_without_a_nonce_writes_nothing(self):
        # host desktop / ssh transport / old launcher: no nonce, no request
        # (nothing anyone could consume later); exit 75 still rides ssh.
        ra = load_ra()
        for env in ({}, {"SSH_CONNECTION": "1 2 3 4"}):
            self.assertEqual(self.press(ra, "L", **env), ra.LINK_OPEN_EXIT)
        d = os.path.join(self.tmp, ".local", "state", "tendril", "link-open")
        self.assertEqual(os.listdir(d) if os.path.isdir(d) else [], [])

    def test_selector_l_malformed_nonce_writes_nothing(self):
        ra = load_ra()
        for bad in ("../../x", "ABCDEF0123456789", "0123", "0123456789abcdef;rm"):
            self.assertEqual(self.press(ra, "l", TENDRIL_LINK_NONCE=bad),
                             ra.LINK_OPEN_EXIT)
        d = os.path.join(self.tmp, ".local", "state", "tendril", "link-open")
        self.assertEqual(os.listdir(d) if os.path.isdir(d) else [], [])

    def test_selector_q_writes_no_request(self):
        ra = load_ra()
        for key in ("q", "Q"):
            self.assertEqual(self.press(ra, key, TENDRIL_LINK_NONCE=self.NONCE), 0)
        self.assertFalse(os.path.exists(self.request_path()))

    def test_link_open_request_consumes_once_for_its_own_nonce_only(self):
        ra = load_ra()
        self.press(ra, "l", TENDRIL_LINK_NONCE=self.NONCE)
        other = "fedcba9876543210"
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ra._link_open_request(other), 0)
            self.assertEqual(out.getvalue(), "")        # not its request
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ra._link_open_request(None), 0)
            self.assertEqual(out.getvalue(), "")        # no nonce, nothing
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ra._link_open_request(self.NONCE), 0)
            self.assertEqual(out.getvalue(), "open\n")
            self.assertFalse(os.path.exists(self.request_path()))  # consumed
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ra._link_open_request(self.NONCE), 0)
            self.assertEqual(out.getvalue(), "")        # consume-once

    def test_unconsumed_requests_are_swept_after_the_ttl(self):
        ra = load_ra()
        self.press(ra, "l", TENDRIL_LINK_NONCE=self.NONCE)
        old = os.path.getmtime(self.request_path()) - ra.LINK_REQUEST_TTL_S - 5
        os.utime(self.request_path(), (old, old))
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            with contextlib.redirect_stdout(io.StringIO()):
                ra._link_open_request("fedcba9876543210")
        self.assertFalse(os.path.exists(self.request_path()))

    def test_cli_verb_takes_the_nonce_argument(self):
        ra = load_ra()
        self.press(ra, "l", TENDRIL_LINK_NONCE=self.NONCE)
        p = subprocess.run([sys.executable, RA, "--link-open-request", self.NONCE],
                           env=dict(os.environ, HOME=self.tmp),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual((p.returncode, p.stdout), (0, "open\n"), p.stderr)

    def test_selector_quit_on_the_host_prints_no_hint(self):
        ra = load_ra()
        out = io.StringIO()
        with mock.patch.object(ra, "read_menu_action",
                               return_value=("line", "q")):
            with contextlib.redirect_stdout(out):
                rc = ra.menu_loop([], tty_mode=True)
        self.assertEqual(rc, 0)
        self.assertNotIn("tendril link", out.getvalue())

    def test_selector_l_and_l_behave_identically(self):
        for key in ("l", "L"):
            ra = load_ra()
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"HOME": self.tmp}):
                os.environ.pop("SSH_CONNECTION", None)
                with mock.patch.object(ra, "read_menu_action",
                                       return_value=("line", key)):
                    with contextlib.redirect_stdout(out):
                        rc = ra.menu_loop([], tty_mode=True)
            self.assertEqual(rc, 75)
            self.assertIn("L opens TENDRIL Link", out.getvalue())


# The REAL round trip of the selector's L key over mosh, across processes:
# the real phone launcher -> a mosh stub that, like real mosh-server,
# hands the session an inherited SSH_CONNECTION -> the REAL remote-agents
# selector (only the keypress is injected) -> the launcher's post-session
# consume over an ssh stub that runs the REAL `remote-agents
# --link-open-request <nonce>` -> am. Nothing in between is mocked.
REAL_MOSH_STUB = (
    'printf \'mosh %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'
    'while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done\n'
    '[ $# -gt 0 ] && shift\n'
    '[ "$1" = "sh" ] && shift\n'
    '[ "$1" = "-lc" ] && shift\n'
    'SSH_CONNECTION="100.124.189.87 42108 100.78.155.82 22" sh -c "$1"\n'
    'exit 0\n')
REAL_SSH_STUB = (
    'printf \'ssh %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'
    'for last; do :; done\n'
    'case "$last" in *"--link-open-request"*) sh -c "$last" ;; esac\n'
    'exit 0\n')
SELECTOR_SIM = r"""
import os, sys
from importlib.machinery import SourceFileLoader
import importlib.util
if "--link-open-request" in sys.argv:
    os.execv(sys.executable, [sys.executable, os.environ["TENDRIL_RA"]] + sys.argv[1:])
loader = SourceFileLoader("ra_sim", os.environ["TENDRIL_RA"])
spec = importlib.util.spec_from_loader(loader.name, loader)
ra = importlib.util.module_from_spec(spec)
loader.exec_module(ra)
ra.read_menu_action = lambda *a, **k: ("line", os.environ["TENDRIL_KEY"])
sys.exit(ra.menu_loop([], tty_mode=True))
"""


class SelectorLRoundTrip(LauncherEnv):
    """L/l open TENDRIL Link exactly once over mosh; Q/q never do."""

    def setUp(self):
        super().setUp()
        sim = os.path.join(self.tmp, "selector_sim.py")
        with open(sim, "w") as f:
            f.write(SELECTOR_SIM)
        self.stub("mosh", REAL_MOSH_STUB)
        self.stub("ssh", REAL_SSH_STUB)
        self.stub("remote-agents",
                  'exec "%s" "%s" "$@"\n' % (sys.executable, sim))

    def round_trip(self, key):
        self.clear()
        p = self.run_launcher(["testhost"], TENDRIL_KEY=key, TENDRIL_RA=RA)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def requests_left(self):
        d = os.path.join(self.home, ".local", "state", "tendril", "link-open")
        return [n for n in os.listdir(d)] if os.path.isdir(d) else []

    def test_l_and_L_open_link_exactly_once(self):
        for key in ("l", "L"):
            p = self.round_trip(key)
            self.assertEqual(self.am_calls(), [AM_LINE], key + p.stdout)
            # one mosh session, one consume, carrying the SAME nonce
            mosh = [c for c in self.calls() if c.startswith("mosh ")]
            consume = [c for c in self.ssh_calls() if "--link-open-request" in c]
            self.assertEqual((len(mosh), len(consume)), (1, 1))
            nonce = consume[0].rsplit(" ", 1)[1]
            self.assertRegex(nonce, r"^[0-9a-f]{16}$")
            self.assertIn("TENDRIL_LINK_NONCE=%s;" % nonce, mosh[0])
            self.assertEqual(self.requests_left(), [])     # consumed, no stale

    def test_q_and_Q_never_open_link_and_leave_nothing(self):
        for key in ("q", "Q"):
            self.round_trip(key)
            self.assertEqual(self.am_calls(), [], key)
            self.assertEqual(self.requests_left(), [])

    def test_a_q_after_an_l_never_reopens(self):
        self.round_trip("l")
        self.round_trip("q")
        self.assertEqual(self.am_calls(), [])           # only the q launch logged

    def test_the_selector_hint_is_shown_in_band(self):
        p = self.round_trip("l")
        self.assertIn("L opens TENDRIL Link", p.stdout)


class LauncherHygiene(LauncherEnv):
    def test_launcher_parses_as_posix_sh(self):
        # shellcheck is not installed on this machine; `sh -n` is the
        # syntax gate we CAN run (full POSIX lint is manual + CI's job).
        p = subprocess.run(["sh", "-n", LAUNCHER], capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main()
