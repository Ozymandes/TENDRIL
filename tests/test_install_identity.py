"""Installer host-identity discovery and prompts (IDENTITY-PAIRING.md).

Fresh installs discover the identity (config > Tailscale MagicDNS name >
short hostname), show one summary with one confirmation, and ask a name
ONLY when Tailscale and the hostname disagree. `--yes` and non-tty runs
take every default without a single question. Reinstalls keep the
configured values and never ask about identity; `./install --upgrade`
keeps config byte-identical (the pre-existing invariant).

Non-interactive runs use the FakeDarwin userland from test_darwin_install
(piped stdin). The interactive prompt counts are proven through a real
pty: a pipe would skip the questions entirely by design, which is exactly
the behavior under test.
"""
import os
import pty
import select
import subprocess
import sys
import time
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)
from test_darwin_install import FakeDarwin, INSTALL, HOST, NODENAME  # noqa: E402

TS_STUB = '''if [ -n "$TENDRIL_FAKE_TS" ]; then
    printf '{"Self":{"HostName":"%s","DNSName":"%s"}}\\n' \\
        "${TENDRIL_FAKE_TS_SHORT:-}" "${TENDRIL_FAKE_TS_FQDN:-}"
    exit 0
fi
exit 1
'''

LOWER = HOST.lower()                    # janes-macbook-pro
TS_NAME = "omarchy"
TS_FQDN = "omarchy.tail1234.ts.net."
NAME_PROMPT = "Host identity (the name phones and the Link app see)"
CONFIRM = "Use this host?"

config_path = lambda self: os.path.join(  # noqa: E731
    self.home, ".config", "remote-agents", "config")


def read_config(self):
    with open(config_path(self)) as f:
        return dict(ln.split("=", 1) for ln in
                    f.read().splitlines() if "=" in ln)


class IdentityDarwin(FakeDarwin):
    """Same fake Mac, but the tailscale stub speaks status --json when
    TENDRIL_FAKE_TS is set (exported per run through env kwargs)."""

    def setUp(self):
        super().setUp()
        self.stub("tailscale", TS_STUB)

    # ---- piped (non-tty): zero questions -----------------------------
    def run_pipe(self, args, answers=(), **env_over):
        return self.run_script(INSTALL, list(args), list(answers), **env_over)

    # ---- pty (interactive): the questions really happen ---------------
    # Answers are prompt-synced: each is written only after its prompt was
    # observed in the output, so the run is deterministic and the prompt
    # counts in the transcript are exact.
    def run_pty(self, args, waits, timeout=90, **env_over):
        """waits: [(prompt_snippet, answer_line), ...] in question order."""
        env = self.env(**env_over)
        master, slave = pty.openpty()
        out_r, out_w = os.pipe()
        proc = subprocess.Popen(["/bin/sh", INSTALL] + list(args), env=env,
                                stdin=slave, stdout=out_w,
                                stderr=subprocess.STDOUT)
        os.close(slave)
        os.close(out_w)
        buf = b""
        deadline = time.monotonic() + timeout
        pending = list(waits)
        try:
            while True:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    proc.kill()
                    proc.wait()
                    raise AssertionError("pty install run timed out")
                ready, _, _ = select.select([out_r], [], [],
                                            min(remain, 5))
                if not ready:
                    if proc.poll() is not None:
                        continue        # drain the last bytes below
                    continue
                data = os.read(out_r, 65536)
                if not data:
                    break               # installer closed its stdout
                buf += data
                text = buf.decode("utf-8", "replace")
                while pending and pending[0][0] in text:
                    os.write(master, pending[0][1].encode() + b"\n")
                    pending.pop(0)
                if not pending and master is not None:
                    os.close(master)     # EOF: later questions take defaults
                    master = None
                if proc.poll() is not None and not pending:
                    break
            while True:                 # drain to EOF
                data = os.read(out_r, 65536)
                if not data:
                    break
                buf += data
            proc.wait(timeout=60)
        finally:
            for fd in ([master] if master is not None else []) + [out_r]:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if pending:
            raise AssertionError("prompt never appeared: %r"
                                 % pending[0][0])
        return proc.returncode, buf.decode("utf-8", "replace")


class FreshHostnameNoTailscale(IdentityDarwin):
    def test_non_tty_takes_defaults_with_zero_questions(self):
        p = self.run_pipe([])
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn(NAME_PROMPT, out)      # zero name prompts
        self.assertNotIn(CONFIRM, out)          # and no question on a pipe
        self.assertIn("(--yes / non-interactive: accepted as shown)", out)
        cfg = read_config(self)
        self.assertEqual(cfg["TENDRIL_HOST"], LOWER)
        self.assertEqual(cfg["TAILSCALE_HOST"], LOWER)
        self.assertEqual(cfg["SSH_TARGET"], LOWER)   # no FQDN: plain hostname
        self.assertEqual(cfg["SSH_ALIAS"], LOWER)
        self.assertEqual(cfg["PROJECT_ROOTS"],
                         f"{self.home}/Projects:{self.home}/src"
                         f":{self.home}/dev:{self.home}/code")

    def test_interactive_asks_exactly_one_confirmation(self):
        rc, out = self.run_pty([], [(CONFIRM, "")])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count(NAME_PROMPT), 0)   # unambiguous: no ask
        self.assertEqual(out.count(CONFIRM), 1)       # ...one summary confirm
        cfg = read_config(self)
        self.assertEqual(cfg["TENDRIL_HOST"], LOWER)


class FreshTailscaleSameName(IdentityDarwin):
    def test_same_name_is_unambiguous_one_confirmation(self):
        rc, out = self.run_pty([], [(CONFIRM, "")],
                               TENDRIL_FAKE_TS="1",
                               TENDRIL_FAKE_TS_SHORT=HOST,
                               TENDRIL_FAKE_TS_FQDN=HOST.lower() + ".tail1234.ts.net.")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count(NAME_PROMPT), 0)
        self.assertEqual(out.count(CONFIRM), 1)
        self.assertIn("Tailscale ✓  janes-macbook-pro.tail1234.ts.net", out)
        cfg = read_config(self)
        self.assertEqual(cfg["TENDRIL_HOST"], LOWER)
        self.assertEqual(cfg["SSH_TARGET"], "janes-macbook-pro.tail1234.ts.net")


class FreshTailscaleDiffers(IdentityDarwin):
    def test_interactive_asks_exactly_one_name_then_confirms(self):
        rc, out = self.run_pty([], [(NAME_PROMPT, ""), (CONFIRM, "")],
                               TENDRIL_FAKE_TS="1",
                               TENDRIL_FAKE_TS_SHORT=TS_NAME,
                               TENDRIL_FAKE_TS_FQDN=TS_FQDN)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count(NAME_PROMPT), 1)   # the ONE ambiguity ask
        self.assertEqual(out.count(CONFIRM), 1)
        cfg = read_config(self)
        self.assertEqual(cfg["TENDRIL_HOST"], TS_NAME)     # default taken
        self.assertEqual(cfg["SSH_TARGET"], "omarchy.tail1234.ts.net")

    def test_yes_flag_asks_nothing_and_takes_the_tailscale_name(self):
        p = self.run_pipe(["--yes"],
                          TENDRIL_FAKE_TS="1",
                          TENDRIL_FAKE_TS_SHORT=TS_NAME,
                          TENDRIL_FAKE_TS_FQDN=TS_FQDN)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn(NAME_PROMPT, out)
        self.assertNotIn(CONFIRM, out)
        cfg = read_config(self)
        self.assertEqual(cfg["TENDRIL_HOST"], TS_NAME)
        self.assertEqual(cfg["SSH_TARGET"], "omarchy.tail1234.ts.net")

    def test_non_tty_without_yes_also_takes_defaults(self):
        p = self.run_pipe([], TENDRIL_FAKE_TS="1",
                          TENDRIL_FAKE_TS_SHORT=TS_NAME,
                          TENDRIL_FAKE_TS_FQDN=TS_FQDN)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn(CONFIRM, out)
        self.assertEqual(read_config(self)["TENDRIL_HOST"], TS_NAME)

    def test_pty_default_answer_keeps_the_tailscale_name(self):
        rc, out = self.run_pty([], [(NAME_PROMPT, ""), (CONFIRM, "")],
                               TENDRIL_FAKE_TS="1",
                               TENDRIL_FAKE_TS_SHORT=TS_NAME,
                               TENDRIL_FAKE_TS_FQDN=TS_FQDN)
        self.assertEqual(rc, 0, out)
        self.assertEqual(read_config(self)["TENDRIL_HOST"], TS_NAME)


class ReinstallKeepsIdentity(IdentityDarwin):
    def test_existing_config_no_identity_prompts_values_preserved(self):
        conf_dir = os.path.dirname(config_path(self))
        os.makedirs(conf_dir)
        before = (f"TAILSCALE_HOST={TS_NAME}\nSSH_ALIAS={TS_NAME}\n"
                  f"PROJECT_ROOTS=/data/proj\nHERDR_BIN=\n")
        with open(config_path(self), "w") as f:
            f.write(before)
        p = self.run_pipe([])                  # no tailscale: nothing to prefer
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn(NAME_PROMPT, out)      # configured: never asked
        self.assertNotIn(CONFIRM, out)
        cfg = read_config(self)
        self.assertEqual(cfg["TAILSCALE_HOST"], TS_NAME)   # preserved
        self.assertEqual(cfg["SSH_ALIAS"], TS_NAME)        # preserved
        self.assertEqual(cfg["PROJECT_ROOTS"], "/data/proj")
        self.assertEqual(cfg["TENDRIL_HOST"], TS_NAME)     # canonical key added
        self.assertEqual(cfg["SSH_TARGET"], TS_NAME)       # alias kept as target

    def test_reinstall_with_live_tailscale_prefers_the_fqdn_target(self):
        conf_dir = os.path.dirname(config_path(self))
        os.makedirs(conf_dir)
        with open(config_path(self), "w") as f:
            f.write(f"TAILSCALE_HOST={TS_NAME}\nSSH_ALIAS={TS_NAME}\n")
        p = self.run_pipe([], TENDRIL_FAKE_TS="1",
                          TENDRIL_FAKE_TS_SHORT=TS_NAME,
                          TENDRIL_FAKE_TS_FQDN=TS_FQDN)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn(NAME_PROMPT, out)
        cfg = read_config(self)
        self.assertEqual(cfg["TENDRIL_HOST"], TS_NAME)     # identity untouched
        self.assertEqual(cfg["SSH_TARGET"],
                         "omarchy.tail1234.ts.net")        # FQDN wins

    def test_uppercase_legacy_config_normalizes_to_lowercase(self):
        conf_dir = os.path.dirname(config_path(self))
        os.makedirs(conf_dir)
        with open(config_path(self), "w") as f:
            f.write("TAILSCALE_HOST=OMARCHY\nSSH_ALIAS=OMARCHY\n")
        p = self.run_pipe([])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        cfg = read_config(self)
        self.assertEqual(cfg["TENDRIL_HOST"], TS_NAME)
        self.assertEqual(cfg["TAILSCALE_HOST"], TS_NAME)


class UpgradePreservesIdentity(IdentityDarwin):
    def test_upgrade_mode_keeps_config_byte_identical(self):
        conf_dir = os.path.dirname(config_path(self))
        os.makedirs(conf_dir)
        body = (f"TENDRIL_HOST={TS_NAME}\nTAILSCALE_HOST={TS_NAME}\n"
                f"SSH_TARGET=omarchy.tail1234.ts.net\nSSH_ALIAS={TS_NAME}\n"
                f"PROJECT_ROOTS=/data/proj\nHERDR_BIN=\n")
        with open(config_path(self), "w") as f:
            f.write(body)
        notify = os.path.join(conf_dir, "notify.env")
        with open(notify, "w") as f:
            f.write("NTFY_URL=https://ntfy.sh\nNTFY_TOPIC=tendril-lPD1ADyhP1iXjUGs\n")
        p = self.run_pipe(["--upgrade"])
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        with open(config_path(self)) as f:
            self.assertEqual(f.read(), body)      # byte-identical
        with open(notify) as f:
            self.assertEqual(f.read(),
                             "NTFY_URL=https://ntfy.sh\n"
                             "NTFY_TOPIC=tendril-lPD1ADyhP1iXjUGs\n")


if __name__ == "__main__":
    unittest.main()
