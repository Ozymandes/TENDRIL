"""`tendril --pair`: the phone pairing flow end to end (CLI level).

Runs the real `bin/remote-agents --pair` as a subprocess against a temp
HOME/config (REMOTE_AGENTS_CONFIG + NOTIFY_ENV env overrides), so no real
install, no real Tailscale, and no real qrencode is ever touched:
- `--pair --code` prints exactly one line, the code;
- the full block shows host/reach/code, the ntfy subscribe URL and a
  paste-ready Termux block with values filled in;
- the payload never contains the topic, tokens, paths or commands;
- qrencode is used only when on PATH, and only ever receives the code;
- invalid identity/target/user values: one clear error, no code printed.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "bin", "remote-agents")
sys.path.insert(0, os.path.join(REPO, "bin"))
import tendril_link as tl  # noqa: E402


def load_cli():
    loader = SourceFileLoader("remote_agents_pair", CLI)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


FAKE_QRENCODE = ('printf \'QR %s\\n\' "$*" >> "$TENDRIL_QR_LOG"\n'
                 'echo "fake-qr-block"\nexit 0\n')

CODE = "omarchy.tail1234.ts.net"
HOST = "omarchy"
USER = "seeno"


class PairBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril pair ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.bindir = os.path.join(self.tmp, "stub bin")
        os.makedirs(self.home)
        os.makedirs(self.bindir)
        self.config = os.path.join(self.tmp, "config")
        self.notify = os.path.join(self.tmp, "notify.env")
        self.qr_log = os.path.join(self.tmp, "qr.log")
        self.write_config(f"TENDRIL_HOST={HOST}\nSSH_TARGET={CODE}\n")
        self.write_notify("https://ntfy.sh", "tendril-lPD1ADyhP1iXjUGs")
        with open(os.path.join(self.bindir, "qrencode"), "w") as f:
            f.write("#!/bin/sh\n" + FAKE_QRENCODE)
        os.chmod(os.path.join(self.bindir, "qrencode"), 0o755)
        # deterministic discovery: no real Tailscale on the test PATH
        with open(os.path.join(self.bindir, "tailscale"), "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(self.bindir, "tailscale"), 0o755)

    def write_config(self, body):
        with open(self.config, "w") as f:
            f.write(body)

    def write_notify(self, url, topic):
        with open(self.notify, "w") as f:
            f.write(f"NTFY_URL={url}\nNTFY_TOPIC={topic}\nNTFY_TOKEN=\n")

    def stub_qrencode(self, present=True):
        path = self.env()["PATH"]
        if present:
            return path
        return self.tmp  # a dir without qrencode

    def env(self, **over):
        e = dict(os.environ)
        e["PATH"] = os.pathsep.join([self.bindir, "/usr/bin", "/bin"])
        e["HOME"] = self.home
        e["USER"] = USER
        e["LOGNAME"] = USER
        e["REMOTE_AGENTS_CONFIG"] = self.config
        e["NOTIFY_ENV"] = self.notify
        e["TENDRIL_QR_LOG"] = self.qr_log
        e.update(over)
        return e

    def run_pair(self, *args, qrencode=True, **env_over):
        e = self.env(PATH=self.stub_qrencode(qrencode), **env_over)
        return subprocess.run([sys.executable, CLI] + list(args), env=e,
                              capture_output=True, text=True, timeout=60)

    def seed_provenance(self, source_dir):
        conf_dir = os.path.join(self.home, ".config", "remote-agents")
        os.makedirs(conf_dir, exist_ok=True)
        with open(os.path.join(conf_dir, "install.json"), "w") as f:
            json.dump({"source_dir": source_dir}, f)

    def assertCode(self, out):
        self.assertEqual(out.count("TENDRIL1:"), 1, out)
        return out[out.index("TENDRIL1:"):].split()[0].strip()


class PairCodeOnly(PairBase):
    def test_code_flag_prints_exactly_the_code(self):
        p = self.run_pair("--pair", "--code")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.strip().splitlines(),
                         [tl.pairing_code(HOST, CODE, USER)])
        self.assertEqual(p.stderr, "")

    def test_code_matches_the_link_repo_fixture_construction(self):
        p = self.run_pair("--pair", "--code")
        code = p.stdout.strip()
        self.assertTrue(code.startswith("TENDRIL1:"))
        self.assertNotIn("=", code)
        blob = json.loads(__import__("base64").b64decode(
            code[9:] + "=" * (-(len(code) - 9) % 4),
            altchars=b"-_", validate=True))
        self.assertEqual(blob, {"v": 1, "h": HOST, "s": CODE, "u": USER})

    def test_legacy_omarchy_style_config_pairs_as_omarchy(self):
        self.write_config("TAILSCALE_HOST=OMARCHY\nSSH_ALIAS=omarchy\n")
        p = self.run_pair("--pair", "--code")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        blob = json.loads(__import__("base64").b64decode(
            p.stdout.strip()[9:] + "=" * (-(len(p.stdout.strip()) - 9) % 4),
            altchars=b"-_", validate=True))
        self.assertEqual(blob["h"], "omarchy")       # lowercased legacy value
        self.assertEqual(blob["s"], "omarchy")       # SSH_ALIAS as the target

    def test_extra_arguments_are_a_usage_error(self):
        p = self.run_pair("--pair", "w15")
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage: tendril --pair [--code]", p.stderr)
        self.assertNotIn("TENDRIL1:", p.stdout)


class PairFullBlock(PairBase):
    def test_block_shows_host_reach_code_ntfy_and_termux_steps(self):
        self.seed_provenance(REPO)
        p = self.run_pair("--pair")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("TENDRIL // PHONE PAIRING", out)
        self.assertIn(f"Host     {HOST}", out)
        self.assertIn(f"Reach    {USER}@{CODE}", out)
        self.assertIn(tl.pairing_code(HOST, CODE, USER), out)
        # ntfy: subscribe URL on its own line, read from notify.env
        self.assertIn("ntfy: subscribe to https://ntfy.sh/tendril-lPD1ADyhP1iXjUGs",
                      out)
        # paste-ready Termux block with the real values filled in
        self.assertIn("pkg install -y openssh mosh", out)
        self.assertIn("ssh-copy-id seeno@omarchy.tail1234.ts.net", out)
        self.assertIn("mkdir -p ~/bin ~/.config/tendril", out)
        self.assertIn(f"scp {USER}@{CODE}:{REPO}/phone/tendril ~/bin/tendril", out)
        self.assertIn(f"scp {USER}@{CODE}:{REPO}/phone/termux-url-opener "
                      f"~/bin/termux-url-opener", out)
        self.assertIn("chmod 700 ~/bin/tendril ~/bin/termux-url-opener", out)
        self.assertIn(f"printf '%s\\n' '{USER}@{CODE}' > ~/.config/tendril/alias",
                      out)
        # provenance missing AND no phone/ dir next to the running command
        # -> the scp lines say so instead of guessing
        lone = os.path.join(self.tmp, "lone bin")
        os.makedirs(lone)
        for name in ("remote-agents", "tendril_link.py", "tendril_host.py"):
            shutil.copy(os.path.join(REPO, "bin", name), os.path.join(lone, name))
        shutil.rmtree(os.path.join(self.home, ".config", "remote-agents"))
        e = self.env(PATH=self.stub_qrencode(False))
        p2 = subprocess.run([sys.executable, os.path.join(lone, "remote-agents"),
                             "--pair"], env=e, capture_output=True, text=True,
                            timeout=60)
        self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)
        self.assertIn("no recorded source checkout", p2.stdout)

    def test_payload_never_contains_topic_token_path_or_commands(self):
        self.seed_provenance(REPO)
        p = self.run_pair("--pair", "--code")
        code = p.stdout.strip()
        for needle in ("tendril-lPD1ADyhP1iXjUGs", "ntfy.sh", REPO,
                       "ssh-copy-id", "pkg", "token"):
            self.assertNotIn(needle, code)
        blob = code[9:]
        raw = __import__("base64").b64decode(
            blob + "=" * (-len(blob) % 4), altchars=b"-_").decode()
        self.assertEqual(set(json.loads(raw)), {"h", "s", "u", "v"})

    def test_qrencode_absent_means_no_qr_and_no_failure(self):
        p = self.run_pair("--pair", qrencode=False)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn("█", out)                     # no QR block
        self.assertIn(tl.pairing_code(HOST, CODE, USER), out)

    def test_qrencode_present_receives_the_code_only(self):
        p = self.run_pair("--pair")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(self.qr_log) as f:
            calls = f.read().splitlines()
        self.assertEqual(
            calls, [f"QR -t ansiutf8 {tl.pairing_code(HOST, CODE, USER)}"])
        self.assertIn("fake-qr-block", p.stdout)


class HostLabelWiring(PairBase):
    """remote-agents resolves HOST_LABEL through the shared identity
    resolver — the same value the pairing code and every deep link uses."""

    def test_host_label_uses_the_resolver(self):
        from unittest import mock
        self.write_config("TENDRIL_HOST=box\n")
        with mock.patch.dict(os.environ, self.env()):
            m = load_cli()
        self.assertEqual(m.HOST_LABEL, "box")
        self.write_config("TAILSCALE_HOST=OMARCHY\n")   # legacy uppercase
        with mock.patch.dict(os.environ, self.env()):
            m2 = load_cli()
        self.assertEqual(m2.HOST_LABEL, "omarchy")


class PairRejects(PairBase):
    def test_invalid_ssh_target_errors_without_a_code(self):
        self.write_config(f"TENDRIL_HOST={HOST}\nSSH_TARGET=omarchy tail\n")
        p = self.run_pair("--pair")
        self.assertEqual(p.returncode, 1)
        self.assertIn("cannot pair:", p.stderr)
        self.assertNotIn("TENDRIL1:", p.stdout)

    def test_invalid_user_errors_without_a_code(self):
        p = self.run_pair("--pair", USER="Seeno", LOGNAME="Seeno")
        self.assertEqual(p.returncode, 1)
        self.assertIn("cannot pair:", p.stderr)
        self.assertNotIn("TENDRIL1:", p.stdout)

    def test_ssh_target_with_user_prefix_is_refused(self):
        self.write_config(f"TENDRIL_HOST={HOST}\nSSH_TARGET=seeno@{CODE}\n")
        p = self.run_pair("--pair", "--code")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("TENDRIL1:", p.stdout)


if __name__ == "__main__":
    unittest.main()
