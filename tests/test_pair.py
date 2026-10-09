"""`tendril --pair`: the phone pairing flow end to end (CLI level).

Runs the real `bin/remote-agents --pair` as a subprocess against a temp
HOME/config (REMOTE_AGENTS_CONFIG + NOTIFY_ENV env overrides), so no real
install, no real Tailscale, and no real qrencode is ever touched:
- `--pair --code` prints exactly one line: the contract v2 code (TENDRIL2:);
- the default block shows host/reach/link/notify, the optional QR and the
  pairing code as one unwrapped line — no ntfy subscribe URL (Link
  subscribes itself), no scp, no wall of steps;
- `--pair --manual` prints the one-command new-phone bootstrap;
- notify.env rides inside the code: NTFY_URL + NTFY_TOPIC + a subscribe
  token (NTFY_SUBSCRIBE_TOKEN preferred over NTFY_TOKEN). A bad ntfy value
  never blocks pairing: v2 without n/t/k, NOTIFY shown not configured;
- the ntfy token is only ever inside the encoded payload, never printed;
- qrencode is used only when on PATH (and only ever receives the code),
  and the QR is suppressed when the terminal is too narrow;
- invalid identity/target/user values: one clear error, no code printed.

The pure codec side (pairing_code_v2 / decode_pairing, exact bytes,
grammars) is pinned against the binding contract and the Link decoder.
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

# A fixed QR block wide enough to overflow a 40-column terminal (with the
# 2-space indent) — for the too-narrow suppression path.
WIDE_QRENCODE = ('echo ' + 'x' * 60 + '\nexit 0\n')

CODE = "omarchy.tail1234.ts.net"
HOST = "omarchy"
USER = "seeno"
TOPIC = "tendril-lPD1ADyhP1iXjUGs"
TOKEN = "tk_" + "a" * 29            # ntfy shape: tk_ + 29 alnum
URL = "https://ntfy.sh"

# Pinned contract bytes: v1 unchanged, v2 bare, v2 with n/t/k (sorted
# compact JSON, base64url, no padding).
V1_CODE = ("TENDRIL1:eyJoIjoib21hcmNoeSIsInMiOiJvbWFyY2h5LnRhaWwxMjM0LnRzLm5l"
           "dCIsInUiOiJzZWVubyIsInYiOjF9")
V2_CODE = ("TENDRIL2:eyJoIjoib21hcmNoeSIsInMiOiJvbWFyY2h5LnRhaWwxMjM0LnRzLm5l"
           "dCIsInUiOiJzZWVubyIsInYiOjJ9")
V2_NTK_CODE = ("TENDRIL2:eyJoIjoib21hcmNoeSIsImsiOiJ0a19hYWFhYWFhYWFhYWFhYWF"
               "hYWFhYWFhYWFhYWFhYSIsIm4iOiJodHRwczovL250Znkuc2giLCJzIjoib21h"
               "cmNoeS50YWlsMTIzNC50cy5uZXQiLCJ0IjoidGVuZHJpbC1sUEQxQUR5aFAx"
               "aVhqVUdzIiwidSI6InNlZW5vIiwidiI6Mn0")


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
        self.write_notify(URL, TOPIC)
        self.stub_tool("qrencode", "#!/bin/sh\n" + FAKE_QRENCODE)
        self.stub_tool("mosh")
        # deterministic discovery: no real Tailscale on the test PATH
        self.stub_tool("tailscale", "#!/bin/sh\nexit 1\n")

    def write_config(self, body):
        with open(self.config, "w") as f:
            f.write(body)

    def write_notify(self, url, topic, subscribe="", legacy=""):
        with open(self.notify, "w") as f:
            f.write(f"NTFY_URL={url}\nNTFY_TOPIC={topic}\n"
                    f"NTFY_SUBSCRIBE_TOKEN={subscribe}\nNTFY_TOKEN={legacy}\n")

    def stub_tool(self, name, body="#!/bin/sh\nexit 0\n"):
        path = os.path.join(self.bindir, name)
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, 0o755)
        return path

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
        path = env_over.pop("PATH", None)
        if path is None:
            path = self.env()["PATH"] if qrencode else self.tmp  # dir without it
        e = self.env(PATH=path, **env_over)
        return subprocess.run([sys.executable, CLI] + list(args), env=e,
                              capture_output=True, text=True, timeout=60)

    def assertPairCode(self, out):
        for prefix in ("TENDRIL2:", "TENDRIL1:"):
            if prefix in out:
                self.assertEqual(out.count(prefix), 1, out)
                return out[out.index(prefix):].split()[0].strip()
        self.fail(f"no pairing code in output:\n{out}")


class PairCodeOnly(PairBase):
    def test_code_flag_prints_exactly_the_code(self):
        p = self.run_pair("--pair", "--code")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.strip().splitlines(),
                         [tl.pairing_code_v2(HOST, CODE, USER,
                                             ntfy_url=URL, topic=TOPIC)])
        self.assertEqual(p.stderr, "")

    def test_code_matches_the_link_repo_fixture_construction(self):
        p = self.run_pair("--pair", "--code")
        code = p.stdout.strip()
        self.assertTrue(code.startswith("TENDRIL2:"))
        self.assertNotIn("=", code)
        blob = json.loads(__import__("base64").b64decode(
            code[9:] + "=" * (-(len(code) - 9) % 4),
            altchars=b"-_", validate=True))
        self.assertEqual(blob, {"v": 2, "h": HOST, "s": CODE, "u": USER,
                                "n": URL, "t": TOPIC})

    def test_code_and_manual_flags_are_exclusive(self):
        p = self.run_pair("--pair", "--code", "--manual")
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage: tendril --pair [--code | --manual]", p.stderr)

    def test_legacy_omarchy_style_config_pairs_as_omarchy(self):
        self.write_config("TAILSCALE_HOST=OMARCHY\nSSH_ALIAS=omarchy\n")
        p = self.run_pair("--pair", "--code")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        doc = tl.decode_pairing(p.stdout.strip())
        self.assertEqual(doc["h"], "omarchy")       # lowercased legacy value
        self.assertEqual(doc["s"], "omarchy")       # SSH_ALIAS as the target

    def test_extra_arguments_are_a_usage_error(self):
        p = self.run_pair("--pair", "w15")
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage: tendril --pair [--code | --manual]", p.stderr)
        self.assertNotIn("TENDRIL1:", p.stdout)
        self.assertNotIn("TENDRIL2:", p.stdout)


class PairV2Codec(unittest.TestCase):
    """Pure codec: exact contract bytes, round trips, reject-not-sanitize
    — the host side of the byte identity the Link repo pins."""

    def test_v1_exact_bytes_unchanged(self):
        self.assertEqual(tl.pairing_code(HOST, CODE, USER), V1_CODE)

    def test_v2_exact_bytes_bare(self):
        self.assertEqual(tl.pairing_code_v2(HOST, CODE, USER), V2_CODE)

    def test_v2_exact_bytes_with_ntfy(self):
        self.assertEqual(tl.pairing_code_v2(HOST, CODE, USER, ntfy_url=URL,
                                            topic=TOPIC, token=TOKEN),
                         V2_NTK_CODE)

    def test_round_trip_v1(self):
        self.assertEqual(tl.decode_pairing(V1_CODE),
                         {"v": 1, "h": HOST, "s": CODE, "u": USER})

    def test_round_trip_v2_token_unredacted(self):
        self.assertEqual(tl.decode_pairing(V2_NTK_CODE),
                         {"v": 2, "h": HOST, "s": CODE, "u": USER,
                          "n": URL, "t": TOPIC, "k": TOKEN})

    def test_decode_rejects_out_of_grammar(self):
        b64 = __import__("base64").urlsafe_b64encode
        flat = lambda b: "TENDRIL2:" + b64(b).decode().rstrip("=")
        bad = {
            "empty": "",
            "no prefix": f"{HOST}:{CODE}",
            "unknown version": V2_NTK_CODE.replace("TENDRIL2:", "TENDRIL3:"),
            "padding": V1_CODE + "=",
            "v mismatch": V2_CODE.replace("TENDRIL2:", "TENDRIL1:"),
            "not base64": "TENDRIL1:!!!",
            "whitespace json": flat(b'{"h": "a", "v":2}'),
            "duplicate key": flat(b'{"v":2,"h":"a","h":"b","s":"a","u":"a"}'),
            "unknown key": flat(b'{"v":2,"h":"a","s":"a","u":"a","z":"b"}'),
            "missing keys": flat(b'{"v":2,"h":"a"}'),
            "float v": flat(b'{"v":2.0,"h":"a","s":"a","u":"a"}'),
            "uppercase host": flat(b'{"v":2,"h":"OMARCHY","s":"a","u":"a"}'),
            "k without t": flat(b'{"v":2,"h":"a","s":"a","u":"a",'
                                b'"n":"https://ntfy.sh","k":"' + TOKEN.encode() + b'"}'),
            "n without t": flat(b'{"v":2,"h":"a","s":"a","u":"a",'
                                b'"n":"https://ntfy.sh"}'),
            "oversized": "TENDRIL2:" + "A" * 1020,
        }
        for name, code in bad.items():
            with self.subTest(case=name):
                with self.assertRaises(tl.LinkError):
                    tl.decode_pairing(code)

    def test_ntfy_url_grammar_table(self):
        ok = ["https://ntfy.sh", "https://ntfy.example.com:443",
              "http://omarchy.tail1234.ts.net", "http://omarchy.tail1234.ts.net:80",
              "http://100.64.0.1", "http://100.127.255.254",
              "http://127.0.0.1", "http://localhost", "http://localhost:8080"]
        reject = ["http://ntfy.example.com",                       # plain http
                  "http://100.63.255.255", "http://100.128.0.0",   # outside /10
                  "https://ntfy.sh/path", "https://ntfy.sh?all=1",  # path/query
                  "https://me@ntfy.sh", "https://ntfy.sh/",         # userinfo/slash
                  "https://ntfy.sh:port", "https://",               # port shape
                  "ftp://ntfy.sh", ""]
        for url in ok:
            with self.subTest(url=url):
                code = tl.pairing_code_v2(HOST, CODE, USER, ntfy_url=url,
                                          topic="t")
                self.assertEqual(tl.decode_pairing(code)["n"], url)
        for url in reject:
            with self.subTest(url=url):
                with self.assertRaises(tl.LinkError):
                    tl.pairing_code_v2(HOST, CODE, USER, ntfy_url=url,
                                       topic="t")

    def test_topic_and_token_grammar_table(self):
        for value, expect_ok in (("a", True), ("A0_-", True),
                                 ("t" * 64, True), ("t" * 65, False),
                                 ("", False), ("bad space", False),
                                 ("bad/slash", False), ("t\u00f6pic", False)):
            with self.subTest(topic=value):
                try:
                    tl.pairing_code_v2(HOST, CODE, USER, ntfy_url=URL,
                                       topic=value)
                except tl.LinkError:
                    self.assertFalse(expect_ok, value)
                else:
                    self.assertTrue(expect_ok, value)
        for value, expect_ok in ((TOKEN, True), ("k" * 128, True),
                                 ("k" * 129, False), ("no spaces", False),
                                 ("k/slash", False)):
            with self.subTest(token=value):
                try:
                    tl.pairing_code_v2(HOST, CODE, USER, ntfy_url=URL,
                                       topic="t", token=value)
                except tl.LinkError:
                    self.assertFalse(expect_ok, value)
                else:
                    self.assertTrue(expect_ok, value)

    def test_n_and_t_together_k_requires_both(self):
        with self.assertRaises(tl.LinkError):      # n without t
            tl.pairing_code_v2(HOST, CODE, USER, ntfy_url=URL)
        with self.assertRaises(tl.LinkError):      # t without n
            tl.pairing_code_v2(HOST, CODE, USER, topic="t")
        doc = tl.decode_pairing(tl.pairing_code_v2(
            HOST, CODE, USER, ntfy_url=URL, topic="t", token=TOKEN))
        self.assertEqual(doc["k"], TOKEN)

    def test_v2_oversized_code_rejected(self):
        # every value inside its own grammar, yet the whole code overflows
        # the contract cap — the cap is the gate that binds.
        with self.assertRaises(tl.LinkError) as ctx:
            tl.pairing_code_v2(HOST, "a" * 253, USER,
                               ntfy_url="https://" + "b" * 253,
                               topic="t" * 64, token="k" * 128)
        self.assertIn("oversized", str(ctx.exception))

    def test_v2_at_grammar_maximum_still_fits_1024(self):
        code = tl.pairing_code_v2(HOST, "a" * 253, USER,
                                  ntfy_url="https://" + "b" * 253,
                                  topic="t" * 64, token="k" * 116)
        self.assertLessEqual(len(code), tl.PAIRING_V2_MAX_LENGTH)
        self.assertEqual(tl.decode_pairing(code)["k"], "k" * 116)


class PairNotifyWiring(PairBase):
    def test_subscribe_token_preferred_over_legacy(self):
        legacy = "tk_" + "b" * 29
        self.write_notify(URL, TOPIC, subscribe=TOKEN, legacy=legacy)
        p = self.run_pair("--pair", "--code")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(tl.decode_pairing(p.stdout.strip())["k"], TOKEN)

    def test_no_token_means_no_k_key(self):
        p = self.run_pair("--pair", "--code")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("k", tl.decode_pairing(p.stdout.strip()))

    def test_topic_rides_byte_exact_inside_the_code(self):
        p = self.run_pair("--pair", "--code")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(tl.decode_pairing(p.stdout.strip())["t"], TOPIC)

    def test_bad_ntfy_url_pairs_without_ntfy_keys(self):
        self.write_notify("http://ntfy.example.com", TOPIC, legacy=TOKEN)
        p = self.run_pair("--pair")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)       # never crash, never block
        self.assertNotIn("cannot pair", out)
        doc = tl.decode_pairing(self.assertPairCode(out))
        for key in ("n", "t", "k"):
            self.assertNotIn(key, doc)
        self.assertIn("not configured", out)
        self.assertIn("run ./install", out)

    def test_missing_notify_env_pairs_without_ntfy_keys(self):
        os.remove(self.notify)
        p = self.run_pair("--pair")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        doc = tl.decode_pairing(self.assertPairCode(p.stdout))
        self.assertEqual(set(doc), {"v", "h", "s", "u"})
        self.assertIn("not configured", p.stdout)

    def test_token_never_printed_outside_the_code(self):
        self.write_notify(URL, TOPIC, subscribe=TOKEN)
        for args in (["--pair"], ["--pair", "--manual"], ["--pair", "--code"]):
            with self.subTest(args=args):
                p = self.run_pair(*args)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotIn(TOKEN, p.stdout)
                self.assertNotIn(TOKEN, p.stderr)


class PairPrettyBlock(PairBase):
    def test_default_layout_at_80_columns(self):
        p = self.run_pair("--pair", COLUMNS="80")
        out = p.stdout
        self.assertEqual(p.returncode, 0, out + p.stderr)
        code = self.assertPairCode(out)
        self.assertNotIn("\x1b", out)            # piped stdout: colour off
        lines = out.splitlines()
        self.assertEqual(lines[0], "TENDRIL // PAIR DEVICE")
        self.assertIn(f"  HOST       {HOST}", lines)
        self.assertIn("  REACH      Tailscale + Mosh", lines)
        self.assertIn("  LINK       ready", lines)
        self.assertIn("  NOTIFY     ready", lines)
        self.assertIn("  Scan with TENDRIL Link", lines)
        self.assertIn("  or paste the pairing code", lines)
        self.assertIn("  Pairing code", lines)
        self.assertEqual([ln for ln in lines if "TENDRIL2:" in ln],
                         [f"  {code}"])          # one unwrapped line
        self.assertIn("  New phone?  tendril --pair --manual", lines)
        # secrets and dead weight stay out of the default block
        self.assertNotIn("scp ", out)
        self.assertNotIn("~/.ssh", out)
        self.assertNotIn("BEGIN OPENSSH", out)
        self.assertNotIn(TOPIC, out)             # rides only inside the code
        self.assertNotIn("ntfy: subscribe", out) # Link subscribes itself

    def test_qrencode_receives_only_the_code(self):
        p = self.run_pair("--pair", COLUMNS="80")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(self.qr_log) as f:
            calls = f.read().splitlines()
        self.assertEqual(
            calls, [f"QR -t ansiutf8 -m 1 "
                    f"{tl.pairing_code_v2(HOST, CODE, USER, ntfy_url=URL, topic=TOPIC)}"])
        self.assertIn("fake-qr-block", p.stdout)

    def test_qrencode_absent_means_no_qr_and_no_failure(self):
        p = self.run_pair("--pair", qrencode=False, COLUMNS="80")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn("fake-qr-block", out)         # no QR block
        self.assertPairCode(out)

    def test_lines_fit_narrow_terminals(self):
        for width in (40, 80, 120):
            with self.subTest(width=width):
                p = self.run_pair("--pair", COLUMNS=str(width))
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                code = self.assertPairCode(p.stdout)
                for ln in p.stdout.splitlines():
                    if code in ln:
                        continue          # the code line is copy-paste critical
                    self.assertLessEqual(len(ln), width, ln)

    def test_qr_suppressed_when_terminal_too_narrow(self):
        self.stub_tool("qrencode", "#!/bin/sh\n" + WIDE_QRENCODE)
        p = self.run_pair("--pair", COLUMNS="40")
        out = p.stdout
        self.assertEqual(p.returncode, 0, out + p.stderr)
        self.assertIn("  (terminal too narrow for QR — widen it or use the code)",
                      out)
        self.assertNotIn("x" * 60, out)

    def test_reach_variants(self):
        p = self.run_pair("--pair", PATH=self.tmp)      # .ts.net, no mosh
        self.assertIn("  REACH      Tailscale + SSH", p.stdout)
        self.write_config(f"TENDRIL_HOST={HOST}\nSSH_TARGET=omarchy.lan\n")
        p = self.run_pair("--pair", PATH=self.tmp)      # no tailscale, no mosh
        self.assertIn("  REACH      SSH", p.stdout)

    def test_reach_composition(self):
        from unittest import mock
        with mock.patch.dict(os.environ, self.env()):
            m = load_cli()
        down = mock.patch.object(m.tendril_host, "tailscale_identity",
                                 return_value=("", ""))
        up = mock.patch.object(m.tendril_host, "tailscale_identity",
                               return_value=("omarchy", "omarchy.tail1234.ts.net"))
        for mosh, ts_target, lan in ((None, "Tailscale + SSH", "SSH"),
                                     ("/usr/bin/mosh", "Tailscale + Mosh", "SSH")):
            with self.subTest(mosh=mosh):
                with mock.patch.object(m.shutil, "which", lambda n, _m=mosh: _m), up:
                    self.assertEqual(m._pair_reach(CODE), ts_target)
                with mock.patch.object(m.shutil, "which", lambda n, _m=mosh: _m), down:
                    self.assertEqual(m._pair_reach("omarchy.lan"), lan)
                with mock.patch.object(m.shutil, "which", lambda n, _m=mosh: _m), up:
                    self.assertEqual(m._pair_reach("omarchy.lan"), ts_target)


class PairManualMode(PairBase):
    def test_manual_prints_the_one_bootstrap_command(self):
        p = self.run_pair("--pair", "--manual")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn("\x1b", out)
        self.assertIn("TENDRIL // PAIR DEVICE", out)
        reach = f"{USER}@{CODE}"
        self.assertIn(
            "pkg install -y openssh mosh && "
            "{ [ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N '' "
            "-f ~/.ssh/id_ed25519 -C termux; } && "
            f"ssh-copy-id {reach} && "
            f"ssh {reach} remote-agents --phone-script setup "
            '> "$PREFIX/tmp/tendril-setup.sh" && sh "$PREFIX/tmp/tendril-setup.sh"',
            out)
        self.assertIn("ssh-copy-id", out)
        self.assertIn("remote-agents --phone-script setup", out)
        self.assertIn("F-Droid", out)
        # no giant wall, no code, no scp
        self.assertNotIn("scp ", out)
        self.assertNotIn("TENDRIL2:", out)
        self.assertNotIn("mkdir -p ~/bin", out)
        self.assertNotIn("chmod 700", out)
        self.assertNotIn("hash -r", out)

    def test_manual_explains_the_password_prompt(self):
        p = self.run_pair("--pair", "--manual")
        self.assertIn("asks for your host password once", p.stdout)

    def test_manual_hints_the_upgrade_path_for_already_set_up_phones(self):
        p = self.run_pair("--pair", "--manual")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Already set up?  in Termux:  tendril --upgrade",
                      p.stdout)
        lines = p.stdout.rstrip("\n").splitlines()
        self.assertEqual(lines[-1],
                         "  Already set up?  in Termux:  tendril --upgrade")


class PairColour(PairBase):
    def test_piped_output_has_no_escape_codes(self):
        p = self.run_pair("--pair")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("\x1b", p.stdout)

    def test_color_enabled_rules(self):
        m = load_cli()
        self.assertTrue(m.color_enabled(True, None, "xterm-256color"))
        self.assertFalse(m.color_enabled(True, "1", "xterm-256color"))
        self.assertFalse(m.color_enabled(True, None, "dumb"))
        self.assertFalse(m.color_enabled(False, None, "xterm-256color"))

    def test_no_color_env_disables_module_colour(self):
        from unittest import mock
        with mock.patch.dict(os.environ, dict(self.env(), NO_COLOR="1")):
            m = load_cli()
        self.assertFalse(m.COLOR)


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
        self.assertNotIn("TENDRIL2:", p.stdout)

    def test_invalid_user_errors_without_a_code(self):
        p = self.run_pair("--pair", USER="Seeno", LOGNAME="Seeno")
        self.assertEqual(p.returncode, 1)
        self.assertIn("cannot pair:", p.stderr)
        self.assertNotIn("TENDRIL1:", p.stdout)
        self.assertNotIn("TENDRIL2:", p.stdout)

    def test_ssh_target_with_user_prefix_is_refused(self):
        self.write_config(f"TENDRIL_HOST={HOST}\nSSH_TARGET=seeno@{CODE}\n")
        p = self.run_pair("--pair", "--code")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("TENDRIL1:", p.stdout)
        self.assertNotIn("TENDRIL2:", p.stdout)


if __name__ == "__main__":
    unittest.main()
