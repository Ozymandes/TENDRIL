"""Pairing v3 cross-repo contract + the v0.2.1 install surface.

The S21 found `tendril --pair` QRs rejected as "NOT A TENDRIL CODE": the
host emitted TENDRIL3: while Link only decoded v1/v2. This pins the exact
host serializer output (synthetic secrets) in tests/fixtures/
pairing-v3-contract.json; the Link repo carries a byte-identical copy that
its PairingCode.parse must accept field for field (PairingCodeV3Test).

Also: `tendril install` (host verb) runs setup.sh from the recorded source,
pinning developer checkouts; the phone installer's APK signing-block reader
returns the real certificate digest and refuses garbage.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "bin"))
import tendril_link as tl  # noqa: E402

FIXTURE = os.path.join(REPO, "tests", "fixtures", "pairing-v3-contract.json")
LINK_COPY = os.path.join(os.path.dirname(REPO), "tendril-link", "app", "src",
                         "test", "resources", "pairing-v3-contract.json")


class PairingV3Contract(unittest.TestCase):
    def load(self):
        with open(FIXTURE, encoding="utf-8") as f:
            return json.load(f)

    def test_fixture_is_exactly_what_the_host_serializer_emits(self):
        for case in self.load()["cases"]:
            c = case["fields"]
            code = tl.pairing_code_v3(
                c["host"], c["ssh_target"], c["user"], c["ntfy_url"], c["topic"],
                token=c["token"], enroll_topic=c["enroll_topic"],
                enroll_secret=c["enroll_secret"], hostkey_fp=c["hostkey_fp"])
            self.assertEqual(code, case["code"], case["name"])
            doc = tl.decode_pairing(code)
            self.assertEqual(doc["e"], c["enroll_topic"])
            self.assertEqual(doc["x"], c["enroll_secret"])

    def test_fixture_secrets_are_synthetic(self):
        for case in self.load()["cases"]:
            secret = case["fields"]["enroll_secret"]
            self.assertTrue("SYNTHETIC" in secret or set(secret) == {"B"}, secret)

    def test_codes_are_single_line_printable_qr_safe(self):
        for case in self.load()["cases"]:
            code = case["code"]
            self.assertTrue(code.startswith("TENDRIL3:"))
            self.assertRegex(code, r"^TENDRIL3:[A-Za-z0-9_-]+$")
            self.assertLessEqual(len(code), tl.PAIRING_V3_MAX_LENGTH)

    @unittest.skipUnless(os.path.isfile(LINK_COPY), "tendril-link checkout not beside this repo")
    def test_link_repo_copy_is_byte_identical(self):
        with open(FIXTURE, "rb") as a, open(LINK_COPY, "rb") as b:
            self.assertEqual(a.read(), b.read(),
                             "regenerate both copies together (the Link parser test reads its copy)")


class InstallVerb(unittest.TestCase):
    def run_cli(self, home, *args, extra_env=None):
        env = dict(os.environ, HOME=home)
        env.pop("XDG_STATE_HOME", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run([sys.executable, os.path.join(REPO, "bin", "remote-agents")]
                              + list(args), env=env, capture_output=True, text=True, timeout=60)

    def test_install_runs_setup_from_a_pinned_developer_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            src = os.path.join(tmp, "dev")
            os.makedirs(os.path.join(src, "phone"))
            os.makedirs(os.path.join(home, ".config", "remote-agents"))
            open(os.path.join(src, "phone", "tendril"), "w").close()
            with open(os.path.join(src, "setup.sh"), "w") as f:
                f.write('echo "SRC=$TENDRIL_SRC PINNED=${TENDRIL_SRC_PINNED:-} NOPAIR=${TENDRIL_NO_PAIR:-}"\n')
            with open(os.path.join(home, ".config", "remote-agents", "install.json"), "w") as f:
                json.dump({"source_dir": src}, f)
            p = self.run_cli(home, "install")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.strip(), f"SRC={src} PINNED=1 NOPAIR=")
            p = self.run_cli(home, "install", "--no-pair")
            self.assertEqual(p.stdout.strip(), f"SRC={src} PINNED=1 NOPAIR=1")

    def test_install_rejects_unknown_arguments(self):
        with tempfile.TemporaryDirectory() as home:
            p = self.run_cli(home, "install", "--bogus")
            self.assertEqual(p.returncode, 2)
            self.assertIn("usage: tendril install", p.stderr)


class PhoneApkSigner(unittest.TestCase):
    """phone/setup.sh's apk_signer: the real signing certificate, in sh."""

    def signer(self, apk):
        script = (
            "set -u\n"
            f"eval \"$(sed -n '/^# >>> apk_signer/,/^# <<< apk_signer/p' "
            f"'{os.path.join(REPO, 'phone', 'setup.sh')}')\"\n"
            f"apk_signer '{apk}'\n")
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)

    def test_reads_the_certificate_digest_like_the_host_reader(self):
        sys.path.insert(0, os.path.join(REPO, "tests"))
        import apkfixture
        import tendril_apk
        with tempfile.TemporaryDirectory() as tmp:
            for v_id in (apkfixture.V2_ID, 0xF05368C0):        # v2 and v3 blocks
                for cert in (b"TEST-CERT", b"another certificate " * 40):
                    apk = os.path.join(tmp, "a.apk")
                    with open(apk, "wb") as f:
                        f.write(apkfixture.make_signed_apk(cert=cert, v_id=v_id))
                    p = self.signer(apk)
                    self.assertEqual(p.returncode, 0, p.stderr)
                    self.assertEqual(p.stdout.strip(), apkfixture.signer_of(cert))
                    self.assertEqual([p.stdout.strip()], tendril_apk.signer_sha256s(apk))

    def test_refuses_a_file_without_a_signing_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            junk = os.path.join(tmp, "junk.apk")
            with open(junk, "wb") as f:
                f.write(b"PK" + b"\0" * 200)
            p = self.signer(junk)
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual(p.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
