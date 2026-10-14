"""`tendril install` on the phone — the installer SOURCE contract (v0.2.1).

Drives the real launcher under `sh` with stub ssh/curl on PATH (the same
fake-tools-on-PATH approach as test_phone_verbs), recording every argv in
TENDRIL_RECORD. Covers the v0.2.1 install-source fix:

- HOST FIRST: a phone that knows its host (TENDRIL_ALIAS / AGENT_ALIAS /
  ~/.config/tendril/alias) fetches the installer from THAT host over its
  key auth (`remote-agents --phone-script setup`) and runs it — the host
  is the authority on what the phone should run, published release or
  RC. GitHub is NEVER consulted behind a reachable host's back;
- an unreachable paired host fails with one clear line, exit 1, and no
  silent fallback to a different trust source;
- an alias with a leading '-' is refused locally (exit 2, no wire);
- no host yet: the public bootstrap downloads exactly ONE NAMED FILE —
  `.../phone/setup.sh` — honouring TENDRIL_RAW / TENDRIL_REF the same
  way phone/setup.sh does (TENDRIL_RAW includes the /phone component);
- THE PERMANENT REGRESSION: the launcher may never construct or request
  a raw.githubusercontent URL that ends in a directory (`/phone`) —
  checked functionally (the recorded curl argv) AND at the source level
  (every raw URL literal names a file);
- the served script runs as a file (never piped), its exit code passes
  through, and a successful INTERACTIVE install opens TENDRIL Link
  (the headless branch — stdin not a tty — never does).

HOME is a temp dir; the real home is never touched. Stdlib only.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO, "phone", "tendril")

DEFAULT_URL = ("https://raw.githubusercontent.com/Ozymandes/TENDRIL/"
               "main/phone/setup.sh")

# ssh stub: records argv; when the call is the installer fetch it prints
# $TENDRIL_SERVED (the host-served script a test stages) and exits
# TENDRIL_SSH_RC.
SSH_STUB = ('printf \'ssh %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'
            'case " $*" in *" --phone-script"*) '
            '[ -n "${TENDRIL_SERVED:-}" ] && cat "$TENDRIL_SERVED" ;; esac\n'
            'exit "${TENDRIL_SSH_RC:-0}"\n')
# curl stub: records argv; honours `-o dest` by copying $TENDRIL_SERVED
# into it (the "downloaded installer"), exits TENDRIL_CURL_RC.
CURL_STUB = ('printf \'curl %s\\n\' "$*" >> "$TENDRIL_RECORD"\n'
             'prev=""\n'
             'for a in "$@"; do\n'
             '    if [ "$prev" = "-o" ] && [ -n "${TENDRIL_SERVED:-}" ]; then\n'
             '        cat "$TENDRIL_SERVED" > "$a"\n'
             '    fi\n'
             '    prev="$a"\n'
             'done\n'
             'exit "${TENDRIL_CURL_RC:-0}"\n')

# A served installer that proves it RAN (as a file, not a pipe) and can
# fail on demand: writes the marker, exits TENDRIL_SERVED_RC.
SERVED = ('printf served > "$TENDRIL_HOME/marker"\n'
          'exit "${TENDRIL_SERVED_RC:-0}"\n')


class InstallEnv(unittest.TestCase):
    """Temp HOME + Termux-shaped PREFIX + stub ssh/curl on PATH."""

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril phone install ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "phone home")
        self.bindir = os.path.join(self.tmp, "stub bin")
        self.prefix = os.path.join(self.tmp, "com.termux", "files", "usr")
        os.makedirs(os.path.join(self.prefix, "tmp"))
        os.makedirs(self.home)
        os.makedirs(self.bindir)
        self.record = os.path.join(self.tmp, "calls.log")
        self.served = os.path.join(self.tmp, "served.sh")
        with open(self.served, "w") as f:
            f.write(SERVED)
        for name, body in (("ssh", SSH_STUB), ("curl", CURL_STUB)):
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
        e["PREFIX"] = self.prefix
        e["TENDRIL_RECORD"] = self.record
        e["TENDRIL_SERVED"] = self.served
        e["TENDRIL_HOME"] = self.home
        for k in ("TENDRIL_ALIAS", "AGENT_ALIAS", "TENDRIL_SSH_RC",
                  "TENDRIL_CURL_RC", "TENDRIL_SERVED_RC", "TENDRIL_RAW",
                  "TENDRIL_REF"):
            e.pop(k, None)
        e.update(over)
        return e

    def set_alias_file(self, alias):
        d = os.path.join(self.home, ".config", "tendril")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "alias"), "w") as f:
            f.write(alias + "\n")

    def run_install(self, **over):
        return subprocess.run(
            ["sh", LAUNCHER, "install"],
            env=self.env(**over), capture_output=True, text=True,
            stdin=subprocess.DEVNULL,            # headless: never a tty
            timeout=60)

    def calls(self):
        try:
            with open(self.record) as f:
                return f.read()
        except OSError:
            return ""


class HostFirst(InstallEnv):

    def test_a_paired_phone_installs_from_its_host(self):
        self.set_alias_file("omarchy")
        r = self.run_install()
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("ssh -o BatchMode=yes -o ConnectTimeout=8 omarchy "
                      "remote-agents --phone-script setup", self.calls())
        # The served script RAN (as a downloaded file, not a pipe).
        self.assertEqual("served",
                         open(os.path.join(self.home, "marker")).read())

    def test_the_environment_alias_wins_over_the_file(self):
        self.set_alias_file("from-file")
        r = self.run_install(TENDRIL_ALIAS="from-env")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn(" from-env remote-agents", self.calls())
        self.assertNotIn(" from-file remote-agents", self.calls())

    def test_github_is_never_consulted_behind_the_host(self):
        self.set_alias_file("omarchy")
        self.run_install()
        self.assertNotIn("githubusercontent", self.calls())
        self.assertNotIn("curl ", self.calls())

    def test_an_unreachable_host_fails_without_a_github_fallback(self):
        self.set_alias_file("omarchy")
        r = self.run_install(TENDRIL_SSH_RC="7")
        self.assertEqual(1, r.returncode)
        self.assertIn("could not fetch the installer from omarchy",
                      r.stdout + r.stderr)
        self.assertNotIn("githubusercontent", self.calls())

    def test_a_leading_dash_alias_is_refused_locally(self):
        self.set_alias_file("-evil")
        r = self.run_install()
        self.assertEqual(2, r.returncode)
        self.assertIn("not an SSH alias", r.stdout + r.stderr)
        self.assertEqual("", self.calls())


class PublicBootstrap(InstallEnv):

    def test_without_a_host_the_named_public_file_is_downloaded(self):
        r = self.run_install()
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn(DEFAULT_URL, self.calls())
        # Still the served installer, executed.
        self.assertEqual("served",
                         open(os.path.join(self.home, "marker")).read())

    def test_tendril_ref_picks_the_release_branch_still_named_file(self):
        r = self.run_install(TENDRIL_REF="release/v0.2.1")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn(
            "https://raw.githubusercontent.com/Ozymandes/TENDRIL/"
            "release/v0.2.1/phone/setup.sh", self.calls())
    def test_tendril_raw_override_keeps_setup_sh_semantics(self):
        # TENDRIL_RAW is the base INCLUDING /phone (same contract as the
        # setup.sh fetch loop) - the launcher must not append it twice.
        raw = "https://mirror.example.com/raw/Ozymandes/TENDRIL/main/phone"
        r = self.run_install(TENDRIL_RAW=raw)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn(f"{raw}/setup.sh", self.calls())

    def test_a_failed_download_fails_with_the_full_url(self):
        r = self.run_install(TENDRIL_CURL_RC="22")
        self.assertEqual(1, r.returncode)
        self.assertIn(DEFAULT_URL, r.stdout + r.stderr)


class Passthrough(InstallEnv):

    def test_the_served_scripts_exit_code_passes_through(self):
        self.set_alias_file("omarchy")
        r = self.run_install(TENDRIL_SERVED_RC="3")
        self.assertEqual(3, r.returncode)

    def test_headless_success_never_opens_the_app(self):
        # stdin is not a tty under the test runner: the headless branch
        # (RUN_COMMAND from the Link app) must stay quiet on the screen.
        self.set_alias_file("omarchy")
        r = self.run_install()
        self.assertEqual(0, r.returncode)
        self.assertNotIn("am start", self.calls())


class NeverADirectoryUrl(InstallEnv):
    """THE permanent regression: a raw URL ending in /phone is a 404."""

    def test_the_recorded_curl_url_always_names_a_file(self):
        self.run_install()                       # no host: the public path
        for m in re.finditer(r"^curl (.*)$", self.calls(), re.M):
            url = m.group(1).split()[-1]
            self.assertFalse(url.rstrip("/").endswith("/phone"),
                             f"directory raw URL constructed: {url}")
            self.assertTrue(url.endswith(".sh"),
                            f"raw URL does not name a file: {url}")

    def test_no_source_literal_is_a_raw_directory_url(self):
        with open(LAUNCHER, encoding="utf-8") as f:
            src = f.read()
        for raw in re.findall(
                r"https://raw\.githubusercontent\.com[^\s\"')]*", src):
            literal = re.sub(r"\$\{[^}]*\}", "", raw)   # expand templates
            self.assertFalse(literal.rstrip("/").endswith("/phone"),
                             f"directory raw URL in the launcher: {raw}")
            self.assertTrue(literal.rstrip("/").endswith(".sh"),
                            f"raw URL literal does not name a file: {raw}")


if __name__ == "__main__":
    unittest.main()
