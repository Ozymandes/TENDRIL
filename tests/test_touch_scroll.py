"""Touch scrolling inside Herdr on Android (v0.2.1).

Root cause (measured end to end, see docs/ANDROID.md "Touch scrolling"):
Herdr requests mouse modes 1000 + 1002 + 1003; Mosh keeps ONE mouse mode
(last wins) and forwards only 1003; Termux implements 1000/1002 only, so
it believed tracking was off and turned swipes into arrow keys. For a
Termux client the attach bridge rewrites 1003 -> 1002 on the way out.

Covers: the pure rewrite (sets, resets, combined params, split safety,
everything else untouched), the real bridge on a real pty with a fake
herdr client (Termux marker on/off), and the launcher only marking real
Termux sessions. Stdlib only; never touches a real Herdr.
"""
import importlib.machinery
import importlib.util
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RA = os.path.join(REPO, "bin", "remote-agents")
LAUNCHER = os.path.join(REPO, "phone", "tendril")

# exactly what `herdr` (0.9.3) writes on attach, trimmed to the mouse part
HERDR_ATTACH = (b"\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l\x1b[?1049h"
                b"\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1004h\x1b[?1006h"
                b"\x1b[?2026hframe\x1b[?2026l")


def load_ra():
    loader = importlib.machinery.SourceFileLoader("ra_touch", RA)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class MouseCompatTranslate(unittest.TestCase):
    def setUp(self):
        self.ra = load_ra()

    def test_1003_becomes_1002_in_sets_and_resets(self):
        tail, out = self.ra._mouse_compat_translate(HERDR_ATTACH)
        self.assertEqual(tail, b"")
        self.assertNotIn(b"1003", out)
        self.assertIn(b"\x1b[?1002h", out)
        self.assertIn(b"\x1b[?1002l", out)
        # every other mode is byte-identical
        for keep in (b"\x1b[?1049h", b"\x1b[?1006h", b"\x1b[?1004h",
                     b"\x1b[?2026h", b"frame"):
            self.assertIn(keep, out)

    def test_combined_parameter_lists(self):
        _, out = self.ra._mouse_compat_translate(b"\x1b[?1000;1003;1006h")
        self.assertEqual(out, b"\x1b[?1000;1002;1006h")

    def test_other_bytes_untouched(self):
        plain = b"seq 1003 \x1b[1003h \x1b[31m1003\x1b[0m"
        self.assertEqual(self.ra._mouse_compat_translate(plain), (b"", plain))

    def test_split_at_every_byte_reassembles_identically(self):
        want = self.ra._mouse_compat_translate(HERDR_ATTACH)[1]
        for i in range(len(HERDR_ATTACH) + 1):
            tail, a = self.ra._mouse_compat_translate(HERDR_ATTACH[:i])
            tail, b = self.ra._mouse_compat_translate(tail + HERDR_ATTACH[i:])
            self.assertEqual(a + b + tail, want, i)


class BridgeOnARealPty(unittest.TestCase):
    """The real _attach_session with a fake herdr client on PATH."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril touch ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.herdr = os.path.join(self.tmp, "herdr")
        with open(self.herdr, "wb") as f:
            f.write(b"#!/bin/sh\nprintf '" +
                    HERDR_ATTACH.replace(b"\x1b", b"\\033") +
                    b"'\nsleep 0.3\n")
        os.chmod(self.herdr, 0o755)

    def run_bridge(self, client):
        code = ("import importlib.machinery as m, importlib.util as u\n"
                "l = m.SourceFileLoader('ra', %r)\n"
                "s = u.spec_from_loader('ra', l); ra = u.module_from_spec(s)\n"
                "l.exec_module(ra)\n"
                "ra.HERDR = %r\n"
                "ra._attach_session()\n") % (RA, self.herdr)
        env = dict(os.environ, TENDRIL_CLIENT=client)
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, "-c", code], env)
        out = b""
        deadline = time.time() + 20
        while time.time() < deadline:
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
        os.waitpid(pid, 0)
        return out

    def test_termux_client_gets_1002_never_1003(self):
        out = self.run_bridge("termux")
        self.assertIn(b"\x1b[?1002h", out)
        self.assertNotIn(b"\x1b[?1003h", out)
        self.assertIn(b"frame", out)

    def test_other_clients_get_herdrs_bytes_verbatim(self):
        out = self.run_bridge("")
        self.assertIn(b"\x1b[?1003h", out)


class LauncherMarksOnlyTermux(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril touch launcher ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)
        self.record = os.path.join(self.tmp, "calls.log")
        for name in ("mosh", "ssh"):
            with open(os.path.join(self.bindir, name), "w") as f:
                f.write('#!/bin/sh\nprintf \'%s %%s\\n\' "$*" >> "$REC"\nexit 0\n'
                        % name)
            os.chmod(os.path.join(self.bindir, name), 0o755)

    def mosh_payload(self, **env):
        e = {"PATH": self.bindir + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
             "HOME": self.tmp, "REC": self.record, "TENDRIL_ALIAS": "testhost"}
        e.update(env)
        if os.path.exists(self.record):
            os.unlink(self.record)
        subprocess.run(["sh", LAUNCHER], env=e, capture_output=True,
                       timeout=60)
        with open(self.record) as f:
            return [ln for ln in f.read().splitlines() if ln.startswith("mosh ")][0]

    def test_termux_version_marks_the_session(self):
        self.assertIn("export TENDRIL_CLIENT=termux;",
                      self.mosh_payload(TERMUX_VERSION="0.118.3"))

    def test_termux_prefix_marks_the_session(self):
        self.assertIn("export TENDRIL_CLIENT=termux;",
                      self.mosh_payload(PREFIX="/data/data/com.termux/files/usr"))

    def test_desktop_clients_are_not_marked(self):
        self.assertNotIn("TENDRIL_CLIENT", self.mosh_payload())


if __name__ == "__main__":
    unittest.main()
