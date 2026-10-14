"""One-shot phone enrollment (v0.2.1): host session/proof/authorized_keys,
the ntfy poll, the guardian hook, and the phone launcher's `tendril enroll`
verb — including the cross-implementation contract (the shell launcher's
sha256 proof must verify in the host's Python) and the host-key pin.

Never touches the real ~/.ssh: HOME / XDG_STATE_HOME are temp dirs, ssh,
ssh-keyscan and curl are stubs. Stdlib only.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(REPO, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)
import tendril_enroll as te  # noqa: E402

LAUNCHER = os.path.join(REPO, "phone", "tendril")


def keypair(tmp):
    """A real ed25519 public key line (ssh-keygen when present, else a
    well-formed synthetic one)."""
    if shutil.which("ssh-keygen"):
        path = os.path.join(tmp, "k")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t",
                        "-f", path], check=True, stdin=subprocess.DEVNULL)
        with open(path + ".pub") as f:
            return f.read().strip()
    blob = (b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + os.urandom(32))
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


class FakeNtfy:
    """urlopen stand-in serving the ntfy poll stream."""

    def __init__(self, messages=(), error=None):
        self.messages = list(messages)
        self.error = error
        self.urls = []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        if self.error:
            raise self.error
        body = "".join(json.dumps({"event": "message", "message": m}) + "\n"
                       for m in self.messages)
        resp = mock.MagicMock()
        resp.read.return_value = body.encode()
        resp.__enter__.return_value = resp
        return resp


class EnrollEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril enroll ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        p = mock.patch.dict(os.environ, {"HOME": self.home,
                                         "XDG_STATE_HOME": os.path.join(self.home, "state")})
        p.start()
        self.addCleanup(p.stop)
        self.key = keypair(self.tmp)

    def message(self, session, key=None, label="SM-G998B", secret=None):
        key_type, b64 = (key or self.key).split()[:2]
        return "tendril-enroll-1 %s %s %s %s" % (
            key_type, b64, label, te.proof_for(secret or session["secret"], key_type, b64))

    def authorized(self):
        try:
            with open(os.path.join(self.home, ".ssh", "authorized_keys")) as f:
                return f.read()
        except OSError:
            return ""


class Session(EnrollEnv):
    def test_session_is_random_private_and_bounded(self):
        a = te.start("https://ntfy.sh")
        path = te.state_path()
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertRegex(a["topic"], r"^tendril-enroll-[A-Za-z0-9_-]{24}$")
        self.assertGreaterEqual(len(a["secret"]), 32)
        self.assertEqual(a["expires"] - a["created"], te.ENROLL_TTL_S)
        b = te.start("https://ntfy.sh")
        self.assertNotEqual((a["topic"], a["secret"]), (b["topic"], b["secret"]))
        self.assertEqual(te.pending()["topic"], b["topic"])   # newest QR only

    def test_expired_session_is_gone(self):
        s = te.start("https://ntfy.sh", now=1000)
        self.assertIsNone(te.pending(now=1000 + te.ENROLL_TTL_S + 1))
        self.assertFalse(os.path.exists(te.state_path()))
        self.assertEqual(te.process_once(s, now=1000 + te.ENROLL_TTL_S + 1)["state"],
                         "expired")


class Proof(EnrollEnv):
    def test_valid_message_verifies(self):
        s = te.start("https://ntfy.sh")
        key_line, label = te.verify_message(self.message(s), s["secret"])
        self.assertEqual(key_line, " ".join(self.key.split()[:2]))
        self.assertEqual(label, "SM-G998B")

    def test_forgeries_are_rejected(self):
        s = te.start("https://ntfy.sh")
        good = self.message(s)
        bad = {
            "wrong secret": self.message(s, secret="W" * 32),
            "appended junk": good + " ; rm -rf ~",
            "newline smuggle": good.replace(" SM-G998B ", " SM\nX "),
            "rsa key": good.replace("ssh-ed25519", "ssh-rsa", 1),
            "label with space": self.message(s, label="a b"),
            "label too long": self.message(s, label="x" * 33),
            "bad proof chars": good[:-64] + "Z" * 64,
            "empty": "",
        }
        for name, text in bad.items():
            with self.subTest(name):
                self.assertIsNone(te.verify_message(text, s["secret"]))

    def test_fingerprint_matches_openssh(self):
        if not shutil.which("ssh-keygen"):
            self.skipTest("ssh-keygen not installed")
        pub = os.path.join(self.tmp, "k.pub")
        ref = subprocess.run(["ssh-keygen", "-lf", pub], capture_output=True,
                             text=True).stdout.split()[1]
        self.assertEqual(te.fingerprint_of_pubkey_line(self.key), ref)

    def test_host_key_override_and_absence(self):
        pub = os.path.join(self.tmp, "host.pub")
        with open(pub, "w") as f:
            f.write(self.key + "\n")
        with mock.patch.dict(os.environ, {"TENDRIL_HOSTKEY_PUB": pub}):
            self.assertEqual(te.host_key_fingerprint(),
                             te.fingerprint_of_pubkey_line(self.key))
        with mock.patch.dict(os.environ, {"TENDRIL_HOSTKEY_PUB": pub + ".none"}):
            self.assertIsNone(te.host_key_fingerprint())


class Process(EnrollEnv):
    def test_enrolls_once_installs_the_key_and_consumes_the_session(self):
        s = te.start("https://ntfy.sh")
        ntfy = FakeNtfy(["noise", self.message(s, secret="X" * 32), self.message(s)])
        res = te.process_once(s, opener=ntfy)
        self.assertEqual(res["state"], "enrolled")
        self.assertEqual(res["label"], "SM-G998B")
        self.assertIn(" ".join(self.key.split()[:2]) + " tendril-phone:SM-G998B:",
                      self.authorized())
        self.assertEqual(oct(os.stat(os.path.join(self.home, ".ssh")).st_mode & 0o777),
                         "0o700")
        self.assertEqual(oct(os.stat(os.path.join(self.home, ".ssh",
                                                  "authorized_keys")).st_mode & 0o777),
                         "0o600")
        self.assertIsNone(te.pending())                     # one phone per QR
        self.assertIn(s["topic"], ntfy.urls[0])
        self.assertIn("poll=1", ntfy.urls[0])

    def test_existing_keys_are_kept_and_never_duplicated(self):
        ssh_dir = os.path.join(self.home, ".ssh")
        os.makedirs(ssh_dir)
        with open(os.path.join(ssh_dir, "authorized_keys"), "w") as f:
            f.write("ssh-ed25519 AAAAexisting laptop\n")
        for _ in range(2):
            s = te.start("https://ntfy.sh")
            te.process_once(s, opener=FakeNtfy([self.message(s)]))
        body = self.authorized()
        self.assertIn("ssh-ed25519 AAAAexisting laptop\n", body)
        self.assertEqual(body.count(self.key.split()[1]), 1)

    def test_no_valid_message_keeps_waiting(self):
        s = te.start("https://ntfy.sh")
        self.assertEqual(te.process_once(s, opener=FakeNtfy(["hello"]))["state"], "waiting")
        self.assertIsNotNone(te.pending())
        self.assertEqual(self.authorized(), "")

    def test_unreachable_server_is_reported_not_fatal(self):
        s = te.start("https://ntfy.sh")
        res = te.process_once(s, opener=FakeNtfy(error=OSError("down")))
        self.assertEqual(res["state"], "unreachable")
        self.assertIsNotNone(te.pending())

    def test_guardian_tick_is_silent_without_a_session(self):
        self.assertEqual(te.tick(), {"state": "none"})


# ---------------------------------------------------------------- the phone side
STUB_SSH = """#!/bin/sh
# succeed only once the host "accepted" the key (marker file)
printf 'ssh %s\\n' "$*" >> "$REC"
[ -f "$ACCEPTED" ] && exit 0
exit 255
"""
STUB_KEYSCAN = """#!/bin/sh
printf 'ssh-keyscan %s\\n' "$*" >> "$REC"
cat "$HOSTKEY_LINE_FILE"
"""
STUB_CURL = """#!/bin/sh
# record the published body, then let the "host" accept the key
printf 'curl %s\\n' "$*" >> "$REC"
while [ $# -gt 0 ]; do
    case $1 in -d) shift; printf '%s' "$1" > "$PUBLISHED" ;; esac
    shift
done
: > "$ACCEPTED"
"""


class PhoneVerb(EnrollEnv):
    """The real phone/tendril `enroll` verb with stub network tools."""

    def setUp(self):
        super().setUp()
        if not shutil.which("ssh-keygen"):
            self.skipTest("ssh-keygen not installed")
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)
        for name, body in (("ssh", STUB_SSH), ("ssh-keyscan", STUB_KEYSCAN),
                           ("curl", STUB_CURL)):
            with open(os.path.join(self.bindir, name), "w") as f:
                f.write(body)
            os.chmod(os.path.join(self.bindir, name), 0o755)
        # the "host" key the keyscan returns, and its fingerprint for the QR
        hostkey_dir = os.path.join(self.tmp, "hk")
        os.makedirs(hostkey_dir)
        self.host_line = "omarchy.tail1234.ts.net " + keypair(hostkey_dir)
        self.host_fp = te.fingerprint_of_pubkey_line(self.host_line.split(" ", 1)[1])
        self.files = {k: os.path.join(self.tmp, k) for k in
                      ("rec", "published", "accepted", "hostline")}
        with open(self.files["hostline"], "w") as f:
            f.write(self.host_line + "\n")

    def enroll(self, *args):
        ssh_keygen_dir = os.path.dirname(shutil.which("ssh-keygen"))
        env = {"HOME": self.home, "PATH": os.pathsep.join(
                   [self.bindir, ssh_keygen_dir, "/usr/bin", "/bin"]),
               "REC": self.files["rec"], "PUBLISHED": self.files["published"],
               "ACCEPTED": self.files["accepted"],
               "HOSTKEY_LINE_FILE": self.files["hostline"]}
        return subprocess.run(["sh", LAUNCHER, "enroll", *args], env=env,
                              capture_output=True, text=True, timeout=120)

    def test_shell_proof_verifies_in_the_host_python(self):
        s = te.start("https://ntfy.sh")
        p = self.enroll("seeno@omarchy.tail1234.ts.net", "https://ntfy.sh",
                        s["topic"], s["secret"], self.host_fp)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.strip().splitlines()[-1], "ENROLL ok")
        with open(self.files["published"]) as f:
            body = f.read()
        hit = te.verify_message(body, s["secret"])
        self.assertIsNotNone(hit, body)                    # sh <-> python agree
        with open(os.path.join(self.home, ".ssh", "id_ed25519.pub")) as f:
            self.assertEqual(hit[0], " ".join(f.read().split()[:2]))
        # the pinned host key and the alias were written
        with open(os.path.join(self.home, ".ssh", "known_hosts")) as f:
            self.assertIn(self.host_line, f.read())
        with open(os.path.join(self.home, ".config", "tendril", "alias")) as f:
            self.assertEqual(f.read().strip(), "seeno@omarchy.tail1234.ts.net")
        with open(self.files["rec"]) as f:
            calls = f.read()
        self.assertIn("ntfy.sh/" + s["topic"], calls)
        self.assertIn("StrictHostKeyChecking=yes", calls)

    def test_host_key_mismatch_refuses_before_publishing_anything(self):
        s = te.start("https://ntfy.sh")
        other = te.fingerprint_of_pubkey_line(self.key)     # not the host's key
        p = self.enroll("seeno@omarchy.tail1234.ts.net", "https://ntfy.sh",
                        s["topic"], s["secret"], other)
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("ENROLL hostkey", p.stdout)
        self.assertFalse(os.path.exists(self.files["published"]))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "tendril", "alias")))

    def test_already_enrolled_publishes_nothing(self):
        open(self.files["accepted"], "w").close()
        s = te.start("https://ntfy.sh")
        p = self.enroll("seeno@omarchy.tail1234.ts.net", "https://ntfy.sh",
                        s["topic"], s["secret"], self.host_fp)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(self.files["published"]))

    def test_every_argument_is_validated_before_anything_runs(self):
        s = {"topic": "tendril-enroll-" + "a" * 24, "secret": "b" * 32}
        good = ["seeno@omarchy.tail1234.ts.net", "https://ntfy.sh",
                s["topic"], s["secret"], self.host_fp]
        bad = {
            0: ["seeno@host;rm", "host", "-oProxyCommand=x", "a b@c"],
            1: ["file:///etc", "https://ntfy.sh/path", "javascript:x", "https://a b"],
            2: ["tendril-enroll-short", "other-topic-aaaaaaaaaaaaaaaaaaaa", "$(id)"],
            3: ["short", "has space " + "a" * 30, "a" * 65],
            4: ["SHA256:tooShort", "MD5:" + "a" * 43, "$(id)"],
        }
        for pos, values in bad.items():
            for v in values:
                with self.subTest(pos=pos, value=v):
                    args = list(good)
                    args[pos] = v
                    p = self.enroll(*args)
                    self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                    self.assertIn("ENROLL usage", p.stdout)
                    self.assertFalse(os.path.exists(self.files["rec"]),
                                     "nothing may run on a bad argument")

    def test_wrong_arity_is_usage(self):
        p = self.enroll("seeno@host")
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
