"""The host phone channel (binding contract §2/§3): manifest, asset
whitelist, --link-stage staging, and the generated Termux scripts.

Black-box where it matters: --phone-asset / --link-stage run through
remote-agents; the generated setup/upgrade scripts run for real under
`sh` in a fake HOME with a recording ssh stub that serves --phone-asset
from the snapshot, plus stub cmd / termux-open / termux-reload-settings.
HOME and XDG_DATA_HOME always point into temp dirs, so the real home and
the real installation are never touched. An unchanged phone must make
ZERO asset fetches; every fetched byte is sha256-verified before it
replaces anything.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(REPO, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)
import tendril_phone as tp  # noqa: E402

REMOTE_AGENTS = os.path.join(BIN, "remote-agents")
LAUNCHER = os.path.join(REPO, "phone", "tendril")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)
from apkfixture import make_signed_apk, signer_of  # noqa: E402

# a real APK-shaped zip with a real (v2-layout) signing block: --link-stage
# reads the ACTUAL signer since v0.2.1, so fixtures must carry one
CERT = b"TENDRIL-TEST-CERT"
APK_BYTES = make_signed_apk(CERT, payload=b"TENDRIL-LINK-APK-BYTES-" * 8)
SIGNER = signer_of(CERT)
OTHER_CERT = b"SOMEONE-ELSES-CERT"
RELEASE_CERT = b"TENDRIL-RELEASE-CERT"
RELEASE_SIGNER = signer_of(RELEASE_CERT)
LAUNCHERS = ("tendril", "termux-url-opener", "agent")

LAUNCHER_BODIES = {
    "tendril": "#!/bin/sh\necho tendril-launcher\n",
    "termux-url-opener": "#!/bin/sh\necho url-opener\n",
    "agent": "#!/bin/sh\necho agent-wrapper\n",
}

SNAPSHOT_LINE = ("phone files not installed on this host - "
                 "run ./install (or tendril --upgrade) first")


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def make_doc(apk=APK_BYTES, **over):
    doc = {"schema": 1, "package": "app.tendril.link", "versionCode": 42,
           "versionName": "0.2.0", "build": "abc1234",
           "sha256": sha_bytes(apk), "size": len(apk),
           "signer_sha256": SIGNER}
    doc.update(over)
    return doc


class PhoneEnv(unittest.TestCase):
    """Fake HOME + fake XDG_DATA_HOME snapshot + stub executables."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril phone-channel ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.data = os.path.join(self.tmp, "data")
        self.stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(self.home)
        os.makedirs(self.stubs)
        self.ssh_log = os.path.join(self.tmp, "ssh.log")
        self.open_log = os.path.join(self.tmp, "open.log")
        self.cmd_log = os.path.join(self.tmp, "cmd.log")
        self.reload_log = os.path.join(self.tmp, "reload.log")
        self.am_log = os.path.join(self.tmp, "am.log")
        self.vc_file = os.path.join(self.tmp, "vc.count")

    # -- paths ------------------------------------------------------------
    def snap(self):
        return os.path.join(self.data, "tendril", "phone")

    def link(self):
        return os.path.join(self.data, "tendril", "link")

    # -- fixture builders ---------------------------------------------------
    def seed_snapshot(self):
        os.makedirs(self.snap(), exist_ok=True)
        for name, body in LAUNCHER_BODIES.items():
            with open(os.path.join(self.snap(), name), "w") as f:
                f.write(body)

    def stage(self, doc_over=None, apk=APK_BYTES):
        """A valid staged state in the fake link dir (as --link-stage
        would leave it)."""
        os.makedirs(self.link(), exist_ok=True)
        doc = make_doc(apk=apk, **(doc_over or {}))
        with open(os.path.join(self.link(), "tendril-link.apk"), "wb") as f:
            f.write(apk)
        with open(os.path.join(self.link(), "tendril-link.json"), "w") as f:
            json.dump(doc, f)
        return doc

    def seed_provenance(self, version="9.9.9-test", host="testhost"):
        conf = os.path.join(self.home, ".config", "remote-agents")
        os.makedirs(conf, exist_ok=True)
        with open(os.path.join(conf, "config"), "w") as f:
            f.write("TENDRIL_HOST=%s\nSSH_TARGET=host.example\n" % host)
        with open(os.path.join(conf, "install.json"), "w") as f:
            json.dump({"version": version, "source_dir": REPO}, f)

    def install_launchers(self, bodies=None):
        """Phone-side ~/bin state; None body = skip that launcher."""
        bodies = bodies if bodies is not None else LAUNCHER_BODIES
        os.makedirs(os.path.join(self.home, "bin"), exist_ok=True)
        for name, body in bodies.items():
            if body is None:
                continue
            path = os.path.join(self.home, "bin", name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, 0o700)

    def record_link_sha(self, doc):
        d = os.path.join(self.home, ".config", "tendril")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "link-apk.sha256"), "w") as f:
            f.write(doc["sha256"] + "\n")

    # -- stubs ------------------------------------------------------------
    def stub(self, name, body):
        path = os.path.join(self.stubs, name)
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, 0o755)

    def stub_channel_tools(self):
        self.stub("ssh", SSH_STUB)
        self.stub("termux-open", OPEN_STUB)
        self.stub("cmd", CMD_STUB)
        self.stub("pm", PM_STUB)
        self.stub("am", AM_STUB)
        self.stub("termux-reload-settings", RELOAD_STUB)

    # -- environment --------------------------------------------------------
    def env(self, **over):
        e = dict(os.environ)
        e["HOME"] = self.home
        e["XDG_DATA_HOME"] = self.data
        e["PATH"] = os.pathsep.join([self.stubs, "/usr/bin", "/bin"])
        e["TMPDIR"] = self.tmp
        e["TENDRIL_SNAP"] = self.data
        e["TENDRIL_SSH_LOG"] = self.ssh_log
        e["TENDRIL_OPEN_LOG"] = self.open_log
        e["TENDRIL_CMD_LOG"] = self.cmd_log
        e["TENDRIL_RELOAD_LOG"] = self.reload_log
        e["TENDRIL_VC_FILE"] = self.vc_file
        e["TENDRIL_AM_LOG"] = self.am_log
        for k in ("TENDRIL_ALIAS", "AGENT_ALIAS", "PREFIX", "TENDRIL_FAKE_VC",
                  "TENDRIL_FAKE_VC2", "TENDRIL_FAKE_PM", "TENDRIL_FAKE_CMD",
                  "TENDRIL_CORRUPT", "TENDRIL_SSH_FAIL"):
            e.pop(k, None)
        e.update(over)
        return e

    def call_tp(self, fn, *args):
        """Run a tendril_phone function in-process against the fake env."""
        with mock.patch.dict(os.environ, {"HOME": self.home,
                                          "XDG_DATA_HOME": self.data}):
            return fn(*args)

    def run_cli(self, *args, env=None, bytes_out=False):
        p = subprocess.run([sys.executable, REMOTE_AGENTS] + list(args),
                           capture_output=True, env=env or self.env(),
                           timeout=90)
        if bytes_out:
            return p
        return p.returncode, p.stdout.decode(), p.stderr.decode()

    def run_generated(self, kind, stdin="", tty=False, **env_over):
        """Run the generated script under sh. tty=True gives it a REAL
        terminal on stdin (the user typing `tendril --upgrade` in Termux),
        with [stdin] pre-typed; tty=False is a pipe (the Link app's
        background RUN_COMMAND) — the script must never prompt there."""
        script = self.call_tp(tp.phone_script, kind)
        self.assertTrue(script)
        path = os.path.join(self.tmp, "generated-%s.sh" % kind)
        with open(path, "w") as f:
            f.write(script)
        # the fake phone dials via TENDRIL_ALIAS (the ssh stub logs it);
        # setup runs see a brand-new phone: no alias anywhere
        env_over.setdefault("TENDRIL_ALIAS", "stubhost" if kind == "upgrade"
                            else "")
        if not tty:
            return subprocess.run(["sh", path], input=stdin, text=True,
                                  capture_output=True,
                                  env=self.env(**env_over), timeout=120)
        import pty
        master, slave = pty.openpty()
        try:
            if stdin:
                os.write(master, stdin.encode())
            return subprocess.run(["sh", path], stdin=slave, text=True,
                                  capture_output=True,
                                  env=self.env(**env_over), timeout=120)
        finally:
            os.close(slave)
            os.close(master)

    def log_lines(self, path):
        try:
            with open(path) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def asset_calls(self):
        return [ln for ln in self.log_lines(self.ssh_log)
                if "--phone-asset" in ln]


SSH_STUB = """#!/bin/sh
{ printf 'ssh'; for a in "$@"; do printf ' <%s>' "$a"; done; printf '\\n'; } >> "$TENDRIL_SSH_LOG"
if [ -n "${TENDRIL_SSH_FAIL:-}" ]; then exit 1; fi
name=""
case "$*" in
    *"--phone-asset link-apk"*) name="link-apk" ;;
    *"--phone-asset tendril"*) name="tendril" ;;
    *"--phone-asset termux-url-opener"*) name="termux-url-opener" ;;
    *"--phone-asset agent"*) name="agent" ;;
esac
if [ -n "${TENDRIL_CORRUPT:-}" ] && [ "$name" = "$TENDRIL_CORRUPT" ]; then
    printf 'corrupted-bytes\\n'
    exit 0
fi
case "$name" in
    link-apk) cat "$TENDRIL_SNAP/tendril/link/tendril-link.apk" ;;
    tendril|termux-url-opener|agent) cat "$TENDRIL_SNAP/tendril/phone/$name" ;;
    *) exit 3 ;;
esac
"""

OPEN_STUB = """#!/bin/sh
{ printf 'termux-open'; for a in "$@"; do printf ' <%s>' "$a"; done; printf '\\n'; } >> "$TENDRIL_OPEN_LOG"
exit 0
"""

RELOAD_STUB = """#!/bin/sh
printf 'termux-reload-settings\\n' >> "$TENDRIL_RELOAD_LOG"
exit 0
"""

AM_STUB = """#!/bin/sh
{ printf 'am'; for a in "$@"; do printf ' <%s>' "$a"; done; printf '\\n'; } >> "$TENDRIL_AM_LOG"
exit 0
"""

PM_STUB = """#!/bin/sh
# Android package-manager reality, faked (TENDRIL_FAKE_PM):
#   1 (default) = present  -> `pm path` prints the package: line, exit 0
#   0           = absent   -> exit 0, prints nothing (a CLEAN empty answer)
#   fail        = FAILED   -> stderr 'Failed transaction (2147483646)', exit 1
#                            (the real Samsung+Termux shape)
#   garbage     = FAILED   -> stdout 'Failed transaction (2147483646)', exit 0
#                            (unparseable output - must be UNKNOWN too)
if [ "$1" = "path" ] && [ "$2" = "app.tendril.link" ]; then
    case "${TENDRIL_FAKE_PM:-1}" in
        1) printf 'package:/data/app/~~fake~~/app.tendril.link/base.apk\\n' ;;
        0) : ;;
        fail) printf 'Failed transaction (2147483646)\\n' >&2; exit 1 ;;
        garbage) printf 'Failed transaction (2147483646)\\n' ;;
    esac
fi
exit 0
"""

CMD_STUB = """#!/bin/sh
{ printf 'cmd'; for a in "$@"; do printf ' <%s>' "$a"; done; printf '\\n'; } >> "$TENDRIL_CMD_LOG"
if [ "${TENDRIL_FAKE_CMD:-}" = "fail" ]; then
    printf 'SecurityException: INTERACT_ACROSS_USERS\\n' >&2
    exit 7
fi
case "$*" in
    *"list packages"*)
        _n=$(cat "$TENDRIL_VC_FILE" 2>/dev/null)
        case $_n in ''|*[!0-9]*) _n=0 ;; esac
        _n=$((_n + 1))
        printf '%s' "$_n" > "$TENDRIL_VC_FILE"
        if [ "$_n" -eq 1 ] || [ -z "${TENDRIL_FAKE_VC2:-}" ]; then
            echo "package:app.tendril.link versionCode:${TENDRIL_FAKE_VC:-}"
        else
            echo "package:app.tendril.link versionCode:$TENDRIL_FAKE_VC2"
        fi
        ;;
esac
exit 0
"""



class Manifest(PhoneEnv):
    """tendril --phone-manifest: exact sh-parsable format."""

    def expected_manifest(self, doc=None, snap=True):
        lines = ["tendril-phone-manifest 1", "host testhost",
                 "version 9.9.9-test"]
        if snap:
            for name in LAUNCHERS:
                lines.append("file %s %s"
                             % (name, sha(os.path.join(self.snap(), name))))
        if doc is not None:
            lines.append("apk %d %s %s %d %s"
                         % (doc["versionCode"], doc["versionName"],
                            doc["sha256"], doc["size"], doc["signer_sha256"]))
        lines.append("end")
        return "\n".join(lines) + "\n"

    def test_manifest_format_exact(self):
        self.seed_snapshot()
        self.seed_provenance()
        self.assertEqual(self.call_tp(tp.manifest_text),
                         self.expected_manifest())

    def test_apk_line_absent_when_not_staged(self):
        self.seed_snapshot()
        self.seed_provenance()
        text = self.call_tp(tp.manifest_text)
        self.assertNotIn("apk ", text)
        self.assertTrue(text.endswith("end\n"))

    def test_apk_line_present_after_stage(self):
        self.seed_snapshot()
        self.seed_provenance()
        doc = self.stage()
        self.assertEqual(self.call_tp(tp.manifest_text),
                         self.expected_manifest(doc=doc))

    def test_missing_snapshot_file_is_refused_at_the_cli(self):
        # a partial snapshot must never surface as a manifest with holes:
        # the channel refuses with one line and an empty stdout
        self.seed_snapshot()
        self.seed_provenance()
        os.remove(os.path.join(self.snap(), "agent"))
        rc, out, err = self.run_cli("--phone-manifest")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, SNAPSHOT_LINE + "\n")

    def test_broken_staged_state_reads_as_not_staged(self):
        self.seed_snapshot()
        self.seed_provenance()
        self.stage(doc_over={"sha256": "0" * 64})    # json lies about the apk
        self.assertNotIn("apk ", self.call_tp(tp.manifest_text))

    def test_values_never_contain_spaces(self):
        self.seed_snapshot()
        self.seed_provenance()
        self.stage()
        for ln in self.call_tp(tp.manifest_text).splitlines():
            parts = ln.split(" ")
            self.assertTrue(all(parts), ln)
            if ln.split(":")[0] in ("host", "version"):
                self.assertEqual(len(parts), 2, ln)


class SnapshotGuard(PhoneEnv):
    """A missing or incomplete snapshot refuses the channel: exit 1, one
    stderr line, nothing on stdout (never all-zero placeholder hashes)."""

    def test_missing_snapshot_dir_refuses_the_manifest(self):
        rc, out, err = self.run_cli("--phone-manifest")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, SNAPSHOT_LINE + "\n")

    def test_missing_snapshot_dir_refuses_both_script_kinds(self):
        for kind in ("setup", "upgrade"):
            rc, out, err = self.run_cli("--phone-script", kind)
            self.assertEqual(rc, 1, kind)
            self.assertEqual(out, "", kind)
            self.assertEqual(err, SNAPSHOT_LINE + "\n", kind)

    def test_each_missing_launcher_refuses(self):
        for name in LAUNCHERS:
            self.seed_snapshot()
            os.remove(os.path.join(self.snap(), name))
            rc, out, err = self.run_cli("--phone-manifest")
            self.assertEqual(rc, 1, name)
            self.assertEqual(out, "", name)
            self.assertEqual(err, SNAPSHOT_LINE + "\n", name)
            shutil.rmtree(self.snap())

    def test_incomplete_snapshot_refuses_the_script(self):
        self.seed_snapshot()
        self.seed_provenance()
        os.remove(os.path.join(self.snap(), "termux-url-opener"))
        rc, out, err = self.run_cli("--phone-script", "upgrade")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, SNAPSHOT_LINE + "\n")

    def test_complete_snapshot_still_serves(self):
        self.seed_snapshot()
        self.seed_provenance()
        rc, out, err = self.run_cli("--phone-manifest")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith("tendril-phone-manifest 1\n"), out)
        self.assertIn("file tendril ", out)

    def test_missing_asset_file_exits_one_silently(self):
        self.seed_snapshot()
        os.remove(os.path.join(self.snap(), "tendril"))
        p = self.run_cli("--phone-asset", "tendril", bytes_out=True)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout, b"")
        self.assertIn("snapshot", p.stderr.decode())


class AssetDispatch(PhoneEnv):
    """tendril --phone-asset through remote-agents: raw bytes or exit 2."""

    def test_valid_names_are_byte_identical(self):
        self.seed_snapshot()
        for name in LAUNCHERS:
            rc, out, err = self.run_cli("--phone-asset", name, bytes_out=False)
            p = self.run_cli("--phone-asset", name, bytes_out=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(os.path.join(self.snap(), name), "rb") as f:
                self.assertEqual(p.stdout, f.read(), name)

    def test_link_apk_needs_a_staged_apk(self):
        self.seed_snapshot()
        rc, out, err = self.run_cli("--phone-asset", "link-apk")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("snapshot", err)

    def test_link_apk_after_stage(self):
        self.seed_snapshot()
        self.stage()
        p = self.run_cli("--phone-asset", "link-apk", bytes_out=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, APK_BYTES)

    def test_whitelist_refusals_exit_two_with_empty_stdout(self):
        self.seed_snapshot()
        for bad in ("../x", "link-apk/../..", "", "unknown", "/etc/passwd",
                    "tendril ", "tendril\n"):
            p = self.run_cli("--phone-asset", bad, bytes_out=True)
            self.assertEqual(p.returncode, 2, repr(bad))
            self.assertEqual(p.stdout, b"", repr(bad))

    def test_missing_value_is_usage(self):
        p = self.run_cli("--phone-asset", bytes_out=True)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, b"")

    def test_two_phone_flags_is_usage(self):
        p = self.run_cli("--phone-manifest", "--phone-asset", "tendril",
                         bytes_out=True)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, b"")

    def test_missing_module_one_clear_line_exit_one(self):
        lone = os.path.join(self.tmp, "lone")
        os.makedirs(lone)
        shutil.copy2(REMOTE_AGENTS, os.path.join(lone, "remote-agents"))
        p = subprocess.run(
            [sys.executable, os.path.join(lone, "remote-agents"),
             "--phone-manifest"],
            capture_output=True, text=True, env=self.env(), timeout=60)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout, "")
        self.assertIn("tendril_phone.py is missing", p.stderr)


class LinkStage(PhoneEnv):
    """tendril --link-stage: strict contract §3 validation, atomic copy."""

    def pkg(self, doc_over=None, apk=APK_BYTES, name="pkg"):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "tendril-link.apk"), "wb") as f:
            f.write(apk)
        doc = make_doc(apk=apk, **(doc_over or {}))
        with open(os.path.join(d, "tendril-link.json"), "w") as f:
            json.dump(doc, f)
        return d, doc

    def test_happy_path_dir_form(self):
        d, doc = self.pkg()
        rc, out, err = self.run_cli("--link-stage", d)
        self.assertEqual(rc, 0, err)
        self.assertIn("TENDRIL // LINK STAGED", out)
        self.assertIn("  version   %s" % doc["versionName"], out)
        self.assertIn("  build     %s" % doc["build"], out)
        self.assertIn("  sha256    %s" % doc["sha256"][:12], out)
        with open(os.path.join(self.link(), "tendril-link.apk"), "rb") as f:
            self.assertEqual(f.read(), APK_BYTES)
        with open(os.path.join(self.link(), "signer.sha256")) as f:
            self.assertEqual(f.read().strip(), SIGNER)
        self.assertEqual(self.call_tp(tp.manifest_text).count("apk 42 "), 1)

    def test_happy_path_json_form(self):
        d, _ = self.pkg()
        rc, out, err = self.run_cli("--link-stage",
                                    os.path.join(d, "tendril-link.json"))
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isfile(
            os.path.join(self.link(), "tendril-link.apk")))

    def test_equal_versioncode_restages(self):
        d, _ = self.pkg()
        self.assertEqual(self.run_cli("--link-stage", d)[0], 0)
        d2, _ = self.pkg(doc_over={"build": "def5678"}, name="pkg2")
        rc, _, err = self.run_cli("--link-stage", d2)
        self.assertEqual(rc, 0, err)
        with open(os.path.join(self.link(), "tendril-link.json")) as f:
            self.assertEqual(json.load(f)["build"], "def5678")

    def test_refusals(self):
        cases = [
            ("sha mismatch", {"sha256": "0" * 64}, None),
            ("size mismatch", {"size": 999}, None),
            ("bad package", {"package": "com.other.app"}, None),
            ("bad schema", {"schema": 2}, None),
            ("versionCode zero", {"versionCode": 0}, None),
            ("versionCode negative", {"versionCode": -1}, None),
            ("versionCode string", {"versionCode": "42"}, None),
            ("versionName space", {"versionName": "0.2.0 x"}, None),
            ("missing key", {}, "signer_sha256"),
        ]
        for label, over, drop in cases:
            with self.subTest(label):
                d = os.path.join(self.tmp, "pkg-" + label.replace(" ", "-"))
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "tendril-link.apk"), "wb") as f:
                    f.write(APK_BYTES)
                full = make_doc(**over)
                if drop:
                    full = {k: v for k, v in full.items() if k != drop}
                with open(os.path.join(d, "tendril-link.json"), "w") as f:
                    json.dump(full, f)
                rc, out, err = self.run_cli("--link-stage", d)
                self.assertNotEqual(rc, 0, label)
                self.assertNotIn("LINK STAGED", out)
                self.assertIn("refusing to stage", err)
                self.assertFalse(os.path.exists(self.link()),
                                 "a refusal must write nothing")

    def test_extra_key_refused(self):
        d, _ = self.pkg(doc_over={"surprise": True})
        rc, _, err = self.run_cli("--link-stage", d)
        self.assertNotEqual(rc, 0)
        self.assertIn("unknown key", err)
        self.assertFalse(os.path.exists(self.link()))

    def test_malformed_json_refused(self):
        d = os.path.join(self.tmp, "pkg-bad-json")
        os.makedirs(d)
        with open(os.path.join(d, "tendril-link.apk"), "wb") as f:
            f.write(APK_BYTES)
        with open(os.path.join(d, "tendril-link.json"), "w") as f:
            f.write("{oops")
        rc, _, err = self.run_cli("--link-stage", d)
        self.assertNotEqual(rc, 0)
        self.assertIn("valid JSON", err)
        self.assertFalse(os.path.exists(self.link()))

    def test_signer_change_refused(self):
        d, _ = self.pkg()
        self.assertEqual(self.run_cli("--link-stage", d)[0], 0)
        other = make_signed_apk(OTHER_CERT)
        d2, _ = self.pkg(doc_over={"signer_sha256": signer_of(OTHER_CERT)},
                         apk=other, name="pkg2")
        rc, out, err = self.run_cli("--link-stage", d2)
        self.assertNotEqual(rc, 0)
        self.assertIn("signing key changed", err)
        self.assertIn("Android would refuse an in-place update", err)
        with open(os.path.join(self.link(), "tendril-link.json")) as f:
            self.assertEqual(json.load(f)["signer_sha256"], SIGNER)

    def test_manifest_lying_about_the_signer_is_refused(self):
        # v0.2.1: the manifest names SIGNER, but the APK is signed by another
        # key - the signing block decides, never the manifest
        other = make_signed_apk(OTHER_CERT)
        d, _ = self.pkg(apk=other)               # make_doc keeps SIGNER
        rc, out, err = self.run_cli("--link-stage", d)
        self.assertNotEqual(rc, 0)
        self.assertIn("the APK is signed by", err)
        self.assertFalse(os.path.exists(self.link()))

    def test_unsigned_or_non_apk_bytes_are_refused(self):
        d, _ = self.pkg(apk=b"not an apk at all")
        rc, _, err = self.run_cli("--link-stage", d)
        self.assertNotEqual(rc, 0)
        self.assertIn("cannot read the APK signer", err)
        self.assertFalse(os.path.exists(self.link()))

    def test_rotation_to_the_release_key_is_the_one_allowed_change(self):
        from unittest import mock
        d, _ = self.pkg()
        self.assertEqual(self.run_cli("--link-stage", d)[0], 0)   # dev pin
        release_cert = b"TENDRIL-RELEASE-CERT"
        rel = make_signed_apk(release_cert, payload=b"release build")
        d2, _ = self.pkg(doc_over={"signer_sha256": signer_of(release_cert),
                                   "versionCode": 43},
                         apk=rel, name="pkg2")
        with mock.patch.object(tp, "LINK_RELEASE_SIGNER", signer_of(release_cert)):
            rc = self.call_tp(tp.link_stage, d2)
            self.assertEqual(rc, 0)
            with open(os.path.join(self.link(), "signer.sha256")) as f:
                self.assertEqual(f.read().strip(), signer_of(release_cert))
            # ...and once on the release key, nothing else is ever accepted
            d3, _ = self.pkg(apk=make_signed_apk(OTHER_CERT),
                             doc_over={"signer_sha256": signer_of(OTHER_CERT),
                                       "versionCode": 44}, name="pkg3")
            self.assertNotEqual(self.call_tp(tp.link_stage, d3), 0)

    def test_versioncode_downgrade_refused(self):
        d, _ = self.pkg()
        self.assertEqual(self.run_cli("--link-stage", d)[0], 0)
        d2, _ = self.pkg(doc_over={"versionCode": 41}, name="pkg2")
        rc, _, err = self.run_cli("--link-stage", d2)
        self.assertNotEqual(rc, 0)
        self.assertIn("versionCode 41 is lower than the staged 42", err)

    def test_missing_apk_refused(self):
        d = os.path.join(self.tmp, "pkg-no-apk")
        os.makedirs(d)
        with open(os.path.join(d, "tendril-link.json"), "w") as f:
            json.dump(make_doc(), f)
        rc, _, err = self.run_cli("--link-stage", d)
        self.assertNotEqual(rc, 0)
        self.assertIn("tendril-link.apk", err)


class LinkFetch(PhoneEnv):
    """tendril --link-fetch: the OFFICIAL release path — pinned signer
    only, everything re-verified by link_stage. Runs against a fake
    opener; no test ever touches the network."""

    def fake_release(self, apk=None, doc_over=None):
        """A fake opener serving tendril-link.json + tendril-link.apk
        exactly as the GitHub release would: the APK is signed by the
        release key and the manifest names that signer."""
        if apk is None:
            apk = make_signed_apk(RELEASE_CERT, payload=b"release build")
        doc = make_doc(apk=apk,
                       **{"signer_sha256": RELEASE_SIGNER,
                          **(doc_over or {})})

        class Resp:
            def __init__(self, body):
                self.body = body

            def read(self, limit=-1):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        served = []

        def opener(req, timeout=60):
            name = req.full_url.rsplit("/", 1)[-1]
            served.append(name)
            if name == "tendril-link.json":
                return Resp(json.dumps(doc).encode())
            if name == "tendril-link.apk":
                return Resp(apk)
            raise OSError("404 " + name)

        opener.served = served
        return opener, doc

    def fetch(self, opener, tag=None):
        import io
        out = io.StringIO()
        rc = self.call_tp(lambda: tp.link_fetch(tag, opener=opener, out=out))
        return rc, out.getvalue()

    def pinned(self):
        return mock.patch.object(tp, "LINK_RELEASE_SIGNER", RELEASE_SIGNER)

    def test_unpinned_refuses_before_any_network(self):
        # the pre-release truth: LINK_RELEASE_SIGNER == "" — nothing is
        # fetched, nothing is staged
        opener, _ = self.fake_release()
        with mock.patch.object(tp, "LINK_RELEASE_SIGNER", ""):
            rc, _ = self.fetch(opener)
        self.assertEqual(rc, 1)
        self.assertEqual(opener.served, [])          # never even looked
        self.assertFalse(os.path.exists(self.link()))

    def test_bad_tag_is_a_usage_error(self):
        opener, _ = self.fake_release()
        with self.pinned():
            rc, _ = self.fetch(opener, tag="../evil")
        self.assertEqual(rc, 2)
        self.assertEqual(opener.served, [])

    def test_manifest_naming_another_signer_is_refused(self):
        opener, doc = self.fake_release(
            apk=make_signed_apk(OTHER_CERT),
            doc_over={"signer_sha256": signer_of(OTHER_CERT)})
        with self.pinned():
            rc, _ = self.fetch(opener)
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.link()))

    def test_implausible_size_refuses_before_the_download(self):
        opener, _ = self.fake_release(doc_over={"size": 500 * 1024 * 1024})
        with self.pinned():
            rc, _ = self.fetch(opener)
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.link()))

    def test_happy_path_stages_the_release(self):
        opener, doc = self.fake_release()
        with self.pinned():
            rc, out = self.fetch(opener)
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.link(), "tendril-link.json")) as f:
            staged = json.load(f)
        self.assertEqual(staged["sha256"], doc["sha256"])
        self.assertEqual(staged["signer_sha256"], RELEASE_SIGNER)
        with open(os.path.join(self.link(), "signer.sha256")) as f:
            self.assertEqual(f.read().strip(), RELEASE_SIGNER)

    def test_already_staged_is_a_no_op(self):
        opener, doc = self.fake_release()
        with self.pinned():
            rc, _ = self.fetch(opener)
            self.assertEqual(rc, 0)
            opener.served.clear()
            rc, out = self.fetch(opener)
        self.assertEqual(rc, 0)
        self.assertIn("already staged", out)

    def test_lying_bytes_are_refused_by_link_stage(self):
        garbage = b"not an apk at all"
        opener, _ = self.fake_release(
            apk=garbage,
            doc_over={"sha256": sha_bytes(garbage), "size": len(garbage)})
        with self.pinned():
            rc, _ = self.fetch(opener)
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.link()))


class GeneratedScriptBasics(PhoneEnv):
    """The generated script: auditable, sh -n clean, values embedded."""

    def test_sh_n_passes_for_both_kinds(self):
        self.seed_snapshot()
        self.stage()
        for kind in ("setup", "upgrade"):
            script = self.call_tp(tp.phone_script, kind)
            p = subprocess.run(["sh", "-n"], input=script, text=True,
                               capture_output=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_shellcheck_when_available(self):
        if not shutil.which("shellcheck"):
            self.skipTest("shellcheck not installed")
        self.seed_snapshot()
        self.stage()
        for kind in ("setup", "upgrade"):
            script = self.call_tp(tp.phone_script, kind)
            path = os.path.join(self.tmp, "sc-%s.sh" % kind)
            with open(path, "w") as f:
                f.write(script)
            p = subprocess.run(["shellcheck", "-s", "sh", path],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0,
                             "%s:\n%s" % (kind, p.stdout + p.stderr))

    def test_upgrade_embeds_manifest_values(self):
        self.seed_snapshot()
        doc = self.stage()
        self.seed_provenance()
        script = self.call_tp(tp.phone_script, "upgrade")
        for name in LAUNCHERS:
            self.assertIn("SHA_%s='%s'"
                          % (name.upper().replace("-", "_"), sha(
                              os.path.join(self.snap(), name))), script)
        self.assertIn("APK_CODE='42'", script)
        self.assertIn("APK_NAME='0.2.0'", script)
        self.assertIn("APK_SHA='%s'" % doc["sha256"], script)
        self.assertIn("APK_SIZE='%d'" % len(APK_BYTES), script)
        self.assertIn('MODE=\'upgrade\'', script)
        self.assertIn('ssh -o BatchMode=yes "$ALIAS" '
                      '"remote-agents --phone-asset', script)

    def test_setup_embeds_reach_and_never_touches_ssh(self):
        import getpass
        self.seed_snapshot()
        self.seed_provenance()
        script = self.call_tp(tp.phone_script, "setup")
        self.assertIn("REACH='%s@host.example'" % getpass.getuser(), script)
        self.assertNotIn(".ssh", script.replace("~/.ssh", "")
                         .replace("$HOME/.ssh", ""))
        self.assertIn("allow-external-apps = true", script)
        self.assertIn('MODE=\'setup\'', script)

    def test_bad_kind_refused(self):
        with self.assertRaises(ValueError):
            self.call_tp(tp.phone_script, "both")


class UpgradeScriptRun(PhoneEnv):
    """The generated upgrade script, run for real under sh."""

    def fresh_phone(self, launchers=None, recorded=True, fake_vc="42"):
        self.seed_snapshot()
        doc = self.stage()
        self.stub_channel_tools()
        self.install_launchers(bodies=launchers)
        if recorded:
            self.record_link_sha(doc)
        return doc

    def test_unchanged_phone_zero_fetches(self):
        self.fresh_phone()
        p = self.run_generated("upgrade", stdin="", TENDRIL_FAKE_VC="42")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(self.asset_calls(), [])
        for row in ("  launcher       current", "  url handler    current",
                    "  agent          current", "  TENDRIL Link   current"):
            self.assertIn(row, p.stdout)
        self.assertNotIn("opening Android installer", out)

    def test_changed_launcher_replaced_atomically(self):
        self.fresh_phone(launchers=dict(LAUNCHER_BODIES, tendril="# stale\n"))
        alias = os.path.join(self.home, ".config", "tendril", "alias")
        os.makedirs(os.path.dirname(alias), exist_ok=True)
        with open(alias, "w") as f:
            f.write("keepme@host\n")
        sshdir = os.path.join(self.home, ".ssh")
        os.makedirs(sshdir)
        with open(os.path.join(sshdir, "id_ed25519"), "w") as f:
            f.write("PRIVATE\n")
        props = os.path.join(self.home, ".termux", "termux.properties")
        os.makedirs(os.path.dirname(props), exist_ok=True)
        with open(props, "w") as f:
            f.write("use-black-ui = true\n")

        p = self.run_generated("upgrade", stdin="", TENDRIL_FAKE_VC="42")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        tendril = os.path.join(self.home, "bin", "tendril")
        self.assertEqual(sha(tendril),
                         sha(os.path.join(self.snap(), "tendril")))
        self.assertEqual(stat.S_IMODE(os.stat(tendril).st_mode), 0o700)
        self.assertEqual(sorted(os.listdir(os.path.join(self.home, "bin"))),
                         sorted(LAUNCHERS))          # no .new.$$ leftovers
        with open(alias) as f:
            self.assertEqual(f.read(), "keepme@host\n")
        with open(os.path.join(sshdir, "id_ed25519")) as f:
            self.assertEqual(f.read(), "PRIVATE\n")
        with open(props) as f:
            self.assertEqual(f.read(), "use-black-ui = true\n")
        self.assertIn("  launcher       updated", p.stdout)
        calls = self.asset_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("remote-agents --phone-asset tendril", calls[0])
        self.assertIn("<-o> <BatchMode=yes>", calls[0])

    def test_staged_apk_downloaded_verified_and_opened(self):
        # An OLDER installed build: the staged APK downloads, verifies,
        # opens and the versionCode is confirmed after the user's Enter
        # (presence itself is `pm path` reality, never the recorded sha).
        self.fresh_phone(recorded=False)
        p = self.run_generated("upgrade", stdin="\n", tty=True,
                               TENDRIL_FAKE_VC="5", TENDRIL_FAKE_VC2="42")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        apk = os.path.join(self.home, ".cache", "tendril",
                           "tendril-link-0.2.0.apk")
        with open(apk, "rb") as f:
            self.assertEqual(f.read(), APK_BYTES)
        opens = self.log_lines(self.open_log)
        self.assertEqual(len(opens), 1)
        self.assertIn("<--content-type>", opens[0])
        self.assertIn("<application/vnd.android.package-archive>", opens[0])
        self.assertIn(apk, opens[0])
        self.assertIn("Tap Update, then press Enter here", p.stdout)
        self.assertIn("recorded (versionCode 42 installed)", p.stdout)
        rec = os.path.join(self.home, ".config", "tendril",
                           "link-apk.sha256")
        with open(rec) as f:
            self.assertEqual(f.read().strip(), sha_bytes(APK_BYTES))
        self.assertEqual(stat.S_IMODE(os.stat(rec).st_mode), 0o600)
        self.assertIn("  TENDRIL Link   update ready", p.stdout)
        self.assertIn("  opening Android installer...", p.stdout)
        # the APK open only works with allow-external-apps: enabled + reloaded
        props = os.path.join(self.home, ".termux", "termux.properties")
        with open(props) as f:
            self.assertEqual(f.read(), "allow-external-apps = true\n")
        self.assertEqual(self.log_lines(self.reload_log),
                         ["termux-reload-settings"])

    # ---- HEADLESS: the Link app's background RUN_COMMAND (stdin = pipe) ----

    def test_headless_parks_the_verified_apk_and_hands_off_to_the_app(self):
        # Physical S21 failure (build 158): from the app the script ran
        # termux-open from the background (Android suppresses the activity
        # start) and then blocked on `read`. Headless now: verify, park,
        # print the handoff, exit — no installer from Termux, no prompt.
        doc = self.fresh_phone(recorded=False)
        p = self.run_generated("upgrade", stdin="", TENDRIL_FAKE_VC="5")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        apk = os.path.join(self.home, ".cache", "tendril",
                           "tendril-link-0.2.0.apk")
        with open(apk, "rb") as f:
            self.assertEqual(f.read(), APK_BYTES)
        self.assertEqual(self.log_lines(self.open_log), [])      # no termux-open
        self.assertNotIn("Tap Update", out)                       # no prompt
        self.assertNotIn("opening Android installer", out)
        self.assertIn("  TENDRIL Link   update ready", p.stdout)
        self.assertIn("  APK saved: %s" % apk, p.stdout)
        self.assertIn("  APK verified: %d %s %d %s" % (
            doc["versionCode"], doc["sha256"], doc["size"],
            doc["signer_sha256"]), p.stdout)
        self.assertIn("  handoff: app", p.stdout)
        # nothing recorded: only a confirmed install may record the sha
        self.assertFalse(os.path.exists(os.path.join(
            self.home, ".config", "tendril", "link-apk.sha256")))

    def test_headless_exits_while_stdin_is_held_open_like_termux_appshell(self):
        # Termux AppShell keeps a background command's stdin OPEN until the
        # process exits; a `read` anywhere would hang until the app's
        # deadline. The run must finish with stdin never closed.
        self.fresh_phone(recorded=False)
        script = self.call_tp(tp.phone_script, "upgrade")
        path = os.path.join(self.tmp, "generated-held.sh")
        with open(path, "w") as f:
            f.write(script)
        proc = subprocess.Popen(
            ["sh", path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            env=self.env(TENDRIL_ALIAS="stubhost", TENDRIL_FAKE_VC="5"))
        try:
            rc = proc.wait(timeout=60)                # stdin still open
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("the headless upgrade blocked with stdin held open")
        finally:
            proc.stdin.close()
        out = proc.stdout.read()
        proc.stdout.close()
        proc.stderr.close()
        self.assertEqual(rc, 0, out)
        self.assertIn("  handoff: app", out)

    def test_headless_current_phone_downloads_nothing(self):
        self.fresh_phone(recorded=False)
        p = self.run_generated("upgrade", stdin="", TENDRIL_FAKE_VC="42")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("  TENDRIL Link   current", p.stdout)
        self.assertNotIn("handoff", p.stdout)
        self.assertNotIn("APK saved", p.stdout)

    def test_headless_corrupt_download_refuses_without_handoff(self):
        self.fresh_phone(recorded=False)
        p = self.run_generated("upgrade", stdin="", TENDRIL_FAKE_VC="5",
                               TENDRIL_CORRUPT="link-apk")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 1, out)
        self.assertIn("  TENDRIL Link   download failed", p.stdout)
        self.assertNotIn("handoff", out)
        self.assertNotIn("APK verified", out)

    def test_staged_apk_visible_but_still_old_is_not_recorded(self):
        self.fresh_phone(recorded=False)
        p = self.run_generated("upgrade", stdin="\n", tty=True,
                               TENDRIL_FAKE_VC="5")
        out = p.stdout + p.stderr
        # NOT a success: the update did not complete, and nothing was
        # launched (the package presence was never confirmed)
        self.assertEqual(p.returncode, 1, out)
        self.assertIn("not installed yet - run  tendril --upgrade  again",
                      p.stdout)
        self.assertFalse(os.path.exists(
            os.path.join(self.home, ".config", "tendril",
                         "link-apk.sha256")))
        self.assertEqual(self.log_lines(self.am_log), [])

    def test_staged_apk_versioncode_verified_after_enter(self):
        self.fresh_phone(recorded=False)
        p = self.run_generated("upgrade", stdin="\n", tty=True,
                               TENDRIL_FAKE_VC="5", TENDRIL_FAKE_VC2="42")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("recorded (versionCode 42 installed)", p.stdout)
        with open(os.path.join(self.home, ".config", "tendril",
                               "link-apk.sha256")) as f:
            self.assertEqual(f.read().strip(), sha_bytes(APK_BYTES))
        # a CONFIRMED install may launch the exact MainActivity
        self.assertEqual(self.log_lines(self.am_log),
                         ["am <start> <-n> <app.tendril.link/.MainActivity>"])

    def test_upgrade_enables_external_apps_only_when_opening(self):
        # without a pending APK the upgrade must not touch ~/.termux at all
        self.fresh_phone()
        p = self.run_generated("upgrade", stdin="", TENDRIL_FAKE_VC="42")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(
            os.path.join(self.home, ".termux")))
        self.assertEqual(self.log_lines(self.reload_log), [])
        # with a pending APK a false line is flipped in place, first
        props = os.path.join(self.home, ".termux", "termux.properties")
        os.makedirs(os.path.dirname(props), exist_ok=True)
        with open(props, "w") as f:
            f.write("use-black-ui = true\nallow-external-apps = false\n")
        os.remove(os.path.join(self.home, ".config", "tendril",
                               "link-apk.sha256"))          # force pending
        os.remove(self.vc_file)                  # fresh cmd call counter
        p = self.run_generated("upgrade", stdin="\n", tty=True,
                               TENDRIL_FAKE_VC="5", TENDRIL_FAKE_VC2="42")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        with open(props) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines, ["use-black-ui = true",
                                 "allow-external-apps = true"])
        self.assertEqual(len(self.log_lines(self.reload_log)), 1)

    def test_staged_apk_current_when_versioncode_matches(self):
        self.fresh_phone(recorded=False)
        p = self.run_generated("upgrade", stdin="", TENDRIL_FAKE_VC="42")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.asset_calls(), [])
        self.assertIn("  TENDRIL Link   current", p.stdout)
        self.assertFalse(os.path.exists(
            os.path.join(self.home, ".cache", "tendril")))

    def test_recorded_sha_is_never_install_evidence_when_package_absent(self):
        # THE physical regression (v0.2.1 RC): the app was manually
        # uninstalled, the recorded sha from the PAST install matched the
        # staged manifest, and `tendril install` said "current" and then
        # failed to launch a package that was not there. Android package
        # reality (pm path) decides - the recorded sha never overrides it.
        self.fresh_phone(recorded=True)
        p = self.run_generated("upgrade", stdin="", TENDRIL_FAKE_VC="",
                               TENDRIL_FAKE_PM="0")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("  TENDRIL Link   not installed - installer ready",
                      p.stdout)
        self.assertNotIn("  TENDRIL Link   current", out)
        # the APK was re-fetched (no cached copy) and handed off
        self.assertIn("  handoff: app", out)

    def test_cached_apk_is_reused_when_the_package_is_absent(self):
        # A cached copy passes the same sha256+size check and is handed to
        # Android's installer WITHOUT a re-download - the manual-uninstall
        # recovery is exactly `tendril install`, zero re-download.
        doc = self.fresh_phone(recorded=True)
        cache = os.path.join(self.home, ".cache", "tendril")
        os.makedirs(cache, exist_ok=True)
        parked = os.path.join(cache, "tendril-link-%s.apk" % doc["versionName"])
        with open(parked, "wb") as f:
            f.write(APK_BYTES)
        calls_before = len(self.asset_calls())
        p = self.run_generated("upgrade", stdin="", TENDRIL_FAKE_VC="",
                               TENDRIL_FAKE_PM="0")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(len(self.asset_calls()), calls_before)   # no re-download
        self.assertIn("  APK saved: %s" % parked, p.stdout)
        self.assertIn("  APK verified: %d %s %d %s" % (
            doc["versionCode"], doc["sha256"], doc["size"],
            doc["signer_sha256"]), p.stdout)
        self.assertIn("  handoff: app", out)

    def test_newer_installed_build_is_never_downgraded(self):
        # the phone runs a NEWER production build than the host has staged:
        # no download, no installer, one honest row, exit 0
        self.fresh_phone(recorded=False)
        p = self.run_generated("upgrade", stdin="", TENDRIL_FAKE_VC="99")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("  TENDRIL Link   phone has 99 > staged 42 - kept",
                      p.stdout)
        self.assertEqual(
            [c for c in self.asset_calls() if "link-apk" in c], [])
        self.assertEqual(self.log_lines(self.open_log), [])
        self.assertNotIn("handoff", out)

    def test_present_but_unreadable_version_is_left_alone(self):
        # pm path proves PRESENCE while the versionCode listing cannot
        # answer: never reinstall over an unknown version from Termux
        self.fresh_phone(recorded=False)
        p = self.run_generated("upgrade", stdin="", TENDRIL_FAKE_VC="")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("  TENDRIL Link   installed", p.stdout)
        self.assertEqual(
            [c for c in self.asset_calls() if "link-apk" in c], [])
        self.assertNotIn("handoff", out)

    # ---- probe FAILURE is UNKNOWN - never installed, never absent ------

    def test_failed_probes_are_unknown_and_change_nothing(self):
        # The real S21 shape: `pm path` dies with "Failed transaction
        # (2147483646)" and `cmd` dies with INTERACT_ACROSS_USERS.
        # installed-vs-absent is UNKNOWABLE: no download, no installer,
        # no launch, one truthful row + guidance, non-zero exit.
        self.fresh_phone(recorded=False)
        p = self.run_generated("upgrade", stdin="",
                               TENDRIL_FAKE_PM="fail", TENDRIL_FAKE_CMD="fail")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 1, out)
        self.assertIn("  TENDRIL Link   state unknown - Android probes refused",
                      p.stdout)
        self.assertIn("installed-vs-absent is UNKNOWN", out)
        self.assertEqual(
            [c for c in self.asset_calls() if "link-apk" in c], [])
        self.assertEqual(self.log_lines(self.open_log), [])
        self.assertNotIn("handoff", out)

    def test_unparseable_probe_output_is_unknown_never_installed(self):
        # THE false-positive shape: the transaction error text lands on
        # STDOUT and exit 0 - non-empty garbage must never count as
        # presence evidence (the old code read it as "installed").
        self.fresh_phone(recorded=False)
        p = self.run_generated("upgrade", stdin="",
                               TENDRIL_FAKE_PM="garbage", TENDRIL_FAKE_CMD="fail")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 1, out)
        self.assertIn("  TENDRIL Link   state unknown - Android probes refused",
                      p.stdout)
        self.assertNotIn("  TENDRIL Link   installed", out)
        self.assertNotIn("  TENDRIL Link   current", out)
        self.assertEqual(self.log_lines(self.am_log), [])

    def test_unknown_with_a_cached_apk_points_at_it(self):
        # UNKNOWN cannot act, but it can be honest about the one safe
        # manual path: the already-verified cached APK.
        doc = self.fresh_phone(recorded=False)
        cache = os.path.join(self.home, ".cache", "tendril")
        os.makedirs(cache, exist_ok=True)
        parked = os.path.join(cache, "tendril-link-%s.apk" % doc["versionName"])
        with open(parked, "wb") as f:
            f.write(APK_BYTES)
        p = self.run_generated("upgrade", stdin="",
                               TENDRIL_FAKE_PM="fail", TENDRIL_FAKE_CMD="fail")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 1, out)
        self.assertIn("termux-open", out)
        self.assertIn(parked, out)
        self.assertEqual(self.log_lines(self.open_log), [])   # not opened FOR you

    def test_one_clean_absent_answer_outweighs_failed_others(self):
        # `pm path` ran CLEAN (exit 0, empty) - that alone is absence
        # evidence, even when the cmd probes are denied; the fallback
        # download then opens the installer (the safe direction).
        self.fresh_phone(recorded=False)
        p = self.run_generated("upgrade", stdin="",
                               TENDRIL_FAKE_PM="0", TENDRIL_FAKE_CMD="fail")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("  TENDRIL Link   not installed - installer ready",
                      p.stdout)
        self.assertIn("  handoff: app", out)

    def test_link_not_staged_row(self):
        self.seed_snapshot()
        self.stub_channel_tools()
        self.install_launchers()
        p = self.run_generated("upgrade", stdin="")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("  TENDRIL Link   not staged", p.stdout)
        self.assertNotIn("opening Android installer", out)

    def test_corrupted_asset_refused_and_old_file_kept(self):
        self.fresh_phone(launchers=dict(LAUNCHER_BODIES, tendril="# stale\n"))
        tendril = os.path.join(self.home, "bin", "tendril")
        p = self.run_generated("upgrade", stdin="", TENDRIL_CORRUPT="tendril",
                               TENDRIL_FAKE_VC="42")
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("download failed the sha256 check", out)
        with open(tendril) as f:
            self.assertEqual(f.read(), "# stale\n")
        self.assertEqual(sorted(os.listdir(os.path.join(self.home, "bin"))),
                         sorted(LAUNCHERS))

    def test_ssh_failure_one_clear_line_old_file_kept(self):
        self.fresh_phone(launchers=dict(LAUNCHER_BODIES, agent="# old\n"))
        agent = os.path.join(self.home, "bin", "agent")
        p = self.run_generated("upgrade", stdin="", TENDRIL_SSH_FAIL="1",
                               TENDRIL_FAKE_VC="42")
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("could not fetch from", out)
        self.assertIn("kept the old file", out)
        with open(agent) as f:
            self.assertEqual(f.read(), "# old\n")

    def test_no_alias_fails_fast_with_one_line(self):
        self.fresh_phone()
        p = self.run_generated("upgrade", stdin="", TENDRIL_ALIAS="",
                               AGENT_ALIAS="", TENDRIL_FAKE_VC="42")
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no host alias", out)
        self.assertNotIn("  launcher", p.stdout)
        self.assertEqual(self.asset_calls(), [])


class SetupScriptRun(PhoneEnv):
    """The generated setup script, run for real under sh."""

    def host_ready(self):
        self.seed_snapshot()
        self.seed_provenance()
        self.stub_channel_tools()

    def test_setup_installs_everything(self):
        import getpass
        self.host_ready()
        p = self.run_generated("setup", stdin="")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        for name in LAUNCHERS:
            path = os.path.join(self.home, "bin", name)
            self.assertEqual(sha(path), sha(os.path.join(self.snap(), name)),
                             name)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)
        alias = os.path.join(self.home, ".config", "tendril", "alias")
        with open(alias) as f:
            self.assertEqual(f.read(),
                             "%s@host.example\n" % getpass.getuser())
        self.assertEqual(stat.S_IMODE(os.stat(alias).st_mode), 0o600)
        props = os.path.join(self.home, ".termux", "termux.properties")
        with open(props) as f:
            self.assertEqual(f.read(),
                             "extra-keys = [['ESC','/',{key: '-', popup: '|'},"
                             "'HOME','UP','END','PGUP','KEYBOARD'], "
                             "['TAB','CTRL','ALT','LEFT','DOWN','RIGHT','PGDN']]\n"
                             "allow-external-apps = true\n")
        self.assertEqual(self.log_lines(self.reload_log),
                         ["termux-reload-settings"])
        bashrc = os.path.join(self.home, ".bashrc")
        with open(bashrc) as f:
            body = f.read()
        self.assertEqual(body.count('export PATH="$HOME/bin:$PATH"'), 1)
        self.assertIn("PHONE // SETUP", p.stdout)
        self.assertIn("LINK READY", p.stdout)

    def test_setup_installs_staged_link_apk(self):
        # a brand-new phone has NO app.tendril.link package (pm path
        # answers nothing): the staged APK downloads, verifies and opens
        self.host_ready()
        self.stage()
        p = self.run_generated("setup", stdin="\n", tty=True,
                               TENDRIL_FAKE_VC="", TENDRIL_FAKE_PM="0")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("  TENDRIL Link   not installed - installer ready",
                      p.stdout)
        opens = self.log_lines(self.open_log)
        self.assertEqual(len(opens), 1)
        self.assertIn("<application/vnd.android.package-archive>", opens[0])
        self.assertIn("LINK READY", p.stdout)
        self.assertNotIn("staged - run", out)

    def test_setup_without_staged_apk_says_not_staged(self):
        self.host_ready()
        p = self.run_generated("setup", stdin="")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("  TENDRIL Link   not staged", p.stdout)
        self.assertEqual(self.log_lines(self.open_log), [])
        self.assertIn("open the TENDRIL Link app", p.stdout)
        self.assertEqual(len(self.asset_calls()), 3)

    def test_setup_idempotent_on_second_run(self):
        self.host_ready()
        self.assertEqual(self.run_generated("setup", stdin="").returncode, 0)
        alias = os.path.join(self.home, ".config", "tendril", "alias")
        with open(alias) as f:
            first_alias = f.read()
        props = os.path.join(self.home, ".termux", "termux.properties")
        with open(props) as f:
            first_props = f.read()
        bashrc = os.path.join(self.home, ".bashrc")
        with open(bashrc) as f:
            first_bashrc = f.read()
        os.remove(self.ssh_log)             # count only the SECOND run
        p = self.run_generated("setup", stdin="")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(self.asset_calls(), [])        # nothing to fetch
        for row in ("  launcher       current", "  url handler    current",
                    "  agent          current"):
            self.assertIn(row, p.stdout)
        with open(alias) as f:
            self.assertEqual(f.read(), first_alias)
        with open(props) as f:
            self.assertEqual(f.read(), first_props)
        with open(bashrc) as f:
            self.assertEqual(f.read(), first_bashrc)
        self.assertEqual(self.log_lines(self.reload_log),
                         ["termux-reload-settings"])   # not reloaded again
        self.assertIn("LINK READY", p.stdout)

    def test_setup_keeps_existing_alias(self):
        self.host_ready()
        alias = os.path.join(self.home, ".config", "tendril", "alias")
        os.makedirs(os.path.dirname(alias), exist_ok=True)
        with open(alias, "w") as f:
            f.write("custom\n")
        p = self.run_generated("setup", stdin="")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(alias) as f:
            self.assertEqual(f.read(), "custom\n")
        self.assertIn("  alias          kept (custom)", p.stdout)

    def test_setup_flips_a_false_line_only(self):
        self.host_ready()
        props = os.path.join(self.home, ".termux", "termux.properties")
        os.makedirs(os.path.dirname(props), exist_ok=True)
        with open(props, "w") as f:
            f.write("use-black-ui = true\n"
                    "allow-external-apps = false\n"
                    "# allow-external-apps = false (kept as a comment)\n")
        p = self.run_generated("setup", stdin="")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(props) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], "use-black-ui = true")
        self.assertEqual(lines[1], "allow-external-apps = true")
        self.assertEqual(lines[2],
                         "# allow-external-apps = false (kept as a comment)")
        # the keyboard toggle: the user had no extra-keys layout, so the
        # default row + KEYBOARD lands AFTER their untouched lines
        self.assertTrue(lines[3].startswith("extra-keys = [["), lines[3])
        self.assertIn("'KEYBOARD'", lines[3])
        self.assertEqual(len(lines), 4)
        self.assertEqual(len(self.log_lines(self.reload_log)), 1)

    def test_setup_preserves_a_custom_extra_keys_layout(self):
        # a user's own extra-keys layout is THEIRS: the keyboard toggle is
        # only guaranteed when no extra-keys line exists at all
        self.host_ready()
        props = os.path.join(self.home, ".termux", "termux.properties")
        os.makedirs(os.path.dirname(props), exist_ok=True)
        with open(props, "w") as f:
            f.write("allow-external-apps = false\nextra-keys = [[CTRL]]\n")
        p = self.run_generated("setup", stdin="")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(props) as f:
            self.assertEqual(f.read(),
                             "allow-external-apps = true\nextra-keys = [[CTRL]]\n")

    def test_setup_extra_keys_is_idempotent(self):
        self.host_ready()
        self.assertEqual(self.run_generated("setup", stdin="").returncode, 0)
        props = os.path.join(self.home, ".termux", "termux.properties")
        with open(props) as f:
            first = f.read()
        self.assertEqual(first.count("extra-keys"), 1)
        self.assertIn("'KEYBOARD'", first)
        reloads_after_run1 = len(self.log_lines(self.reload_log))
        self.assertEqual(self.run_generated("setup", stdin="").returncode, 0)
        with open(props) as f:
            self.assertEqual(f.read(), first)          # byte-identical
        # nothing left to reload on the second run
        self.assertEqual(len(self.log_lines(self.reload_log)),
                         reloads_after_run1)

    def test_setup_never_touches_ssh(self):
        self.host_ready()
        sshdir = os.path.join(self.home, ".ssh")
        os.makedirs(sshdir)
        key = os.path.join(sshdir, "id_ed25519")
        with open(key, "w") as f:
            f.write("PRIVATE KEY STAYS\n")
        before = sorted(os.listdir(sshdir))
        p = self.run_generated("setup", stdin="")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(key) as f:
            self.assertEqual(f.read(), "PRIVATE KEY STAYS\n")
        self.assertEqual(sorted(os.listdir(sshdir)), before)

    def test_setup_ssh_failure_fails_without_link_ready(self):
        self.host_ready()
        p = self.run_generated("setup", stdin="", TENDRIL_SSH_FAIL="1")
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("could not fetch from", out)
        self.assertNotIn("LINK READY", out)


class PhoneLauncherFlags(PhoneEnv):
    """phone/tendril --upgrade / --version, handled before alias parsing."""

    def launcher_env(self, **over):
        e = self.env(**over)
        return e

    def stub_launcher_ssh(self):
        self.stub("ssh", """#!/bin/sh
{ printf 'ssh'; for a in "$@"; do printf ' <%s>' "$a"; done; printf '\\n'; } >> "$TENDRIL_SSH_LOG"
case "$*" in
    *--phone-script*) printf '#!/bin/sh\\necho SCRIPT-RAN\\n' ;;
    *) exit 0 ;;
esac
""")

    def run_launcher(self, *args, **env_over):
        return subprocess.run(["sh", LAUNCHER] + list(args), text=True,
                              input="", capture_output=True,
                              env=self.launcher_env(**env_over), timeout=60)

    def test_upgrade_dials_batchmode_and_runs_the_script(self):
        self.stub_launcher_ssh()
        p = self.run_launcher("--upgrade", TENDRIL_ALIAS="stubhost",
                              TMPDIR=self.tmp)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("SCRIPT-RAN", p.stdout)
        calls = self.log_lines(self.ssh_log)
        self.assertEqual(len(calls), 1)
        self.assertIn("<-o> <BatchMode=yes>", calls[0])
        self.assertIn("<stubhost>", calls[0])
        self.assertIn("<remote-agents --phone-script upgrade>", calls[0])

    def test_upgrade_is_never_mistaken_for_an_alias(self):
        self.stub_launcher_ssh()
        # no alias anywhere: the flag must yield the no-alias guidance,
        # never an ssh attempt to an alias literally named "--upgrade"
        p = self.run_launcher("--upgrade", TMPDIR=self.tmp)
        self.assertEqual(p.returncode, 1)
        self.assertIn("no host alias", p.stdout + p.stderr)
        self.assertFalse(os.path.exists(self.ssh_log))

    def test_version_prints_launcher_sha12(self):
        p = self.run_launcher("--version")
        self.assertEqual(p.returncode, 0, p.stderr)
        want = "tendril-phone %s" % sha_bytes(
            open(LAUNCHER, "rb").read())[:12]
        self.assertEqual(p.stdout.strip(), want)

    def test_version_beats_alias_position_too(self):
        # `tendril --version` with no alias at all still prints the id
        p = self.run_launcher("--version")
        self.assertEqual(p.returncode, 0)
        self.assertTrue(p.stdout.startswith("tendril-phone "))


if __name__ == "__main__":
    unittest.main()
