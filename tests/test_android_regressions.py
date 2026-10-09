"""Regressions seen on a real Android (S21 Ultra / Termux) phone.

1. ~/bin/tendril held the `agent` wrapper. Old docs made ~/bin/agent a
   symlink to tendril; copying phone/agent onto ~/bin/agent wrote through
   it, and the wrapper then exec'd itself forever (a silent "hang").
2. The masthead vanished: with the keyboard open and 7+ workspaces, paths
   were kept and took every row, so not even the two-line caption fit.
3. With no alias configured the launcher must fail fast and say why.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_masthead as tm  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TENDRIL = os.path.join(REPO, "phone", "tendril")
AGENT = os.path.join(REPO, "phone", "agent")
SETUP = os.path.join(REPO, "docs", "scripts", "tendril-phone-setup.sh")


def read(path):
    with open(path) as f:
        return f.read()


class LauncherSources(unittest.TestCase):
    def test_launcher_and_wrapper_are_different_files(self):
        self.assertNotEqual(read(TENDRIL), read(AGENT))

    def test_launcher_has_the_real_connect_logic(self):
        src = read(TENDRIL)
        self.assertIn("# tendril — TENDRIL", src)
        for needle in ('mosh "$ALIAS" -- sh -lc', 'exec ssh -t "$ALIAS"',
                       "BatchMode=yes", "attach | resolve | focus | link",
                       "*[!A-Za-z0-9._:-]*"):
            self.assertIn(needle, src)
        self.assertNotIn("compatibility alias", src.splitlines()[1])

    def test_wrapper_stays_a_small_delegating_wrapper(self):
        src = read(AGENT)
        self.assertIn("# agent — compatibility alias", src)
        self.assertIn('exec sh "$here/tendril" "$@"', src)
        self.assertNotIn("mosh", src)
        self.assertLess(len(src.splitlines()), 20)


class WrapperNeverLoops(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tendril phone ")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.bin = os.path.join(self.home, "bin")
        os.makedirs(self.bin)

    def run_sh(self, path, *args):
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin"}
        return subprocess.run(["sh", path] + list(args), env=env,
                              capture_output=True, text=True, timeout=10)

    def test_wrapper_installed_as_tendril_fails_fast(self):
        # the phone's broken state: ~/bin/tendril == phone/agent
        shutil.copy(AGENT, os.path.join(self.bin, "tendril"))
        p = self.run_sh(os.path.join(self.bin, "tendril"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("is the agent wrapper, not the TENDRIL launcher", p.stderr)

    def test_copy_through_legacy_symlink_is_detected(self):
        shutil.copy(TENDRIL, os.path.join(self.bin, "tendril"))
        os.symlink("tendril", os.path.join(self.bin, "agent"))
        shutil.copyfile(AGENT, os.path.join(self.bin, "agent"))  # like scp
        self.assertEqual(read(os.path.join(self.bin, "tendril")), read(AGENT))
        p = self.run_sh(os.path.join(self.bin, "agent"))
        self.assertEqual(p.returncode, 1)

    def test_wrapper_still_delegates_to_the_real_launcher(self):
        shutil.copy(TENDRIL, os.path.join(self.bin, "tendril"))
        shutil.copy(AGENT, os.path.join(self.bin, "agent"))
        p = self.run_sh(os.path.join(self.bin, "agent"), "attach", "x;y")
        self.assertEqual(p.returncode, 2)            # the launcher's own check
        self.assertIn("usage: tendril", p.stdout)


class GuidedSetupCopiesTheRightFiles(unittest.TestCase):
    """Step 6 of the PR #1 phone setup, run against a stubbed scp and a
    phone home that still has the legacy `agent -> tendril` symlink."""

    def test_each_destination_gets_its_own_source(self):
        src = read(SETUP)
        start = src.index('echo; echo "6) Installing')
        end = src.index("chmod 700 ~/bin/tendril ~/bin/agent ~/bin/termux-url-opener")
        step6 = src[start:end]
        home = tempfile.mkdtemp(prefix="tendril phone ")
        self.addCleanup(shutil.rmtree, home, True)
        stubs = os.path.join(home, "stubs")
        os.makedirs(stubs)
        with open(os.path.join(stubs, "scp"), "w") as f:
            # scp -q host:repo/phone/a host:repo/phone/b ... DEST/
            f.write('#!/bin/sh\nshift\nfor a in "$@"; do last=$a; done\n'
                    'for a in "$@"; do [ "$a" = "$last" ] && break\n'
                    '  cp "$TENDRIL_REPO/phone/${a##*/}" "$last"; done\n')
        os.chmod(os.path.join(stubs, "scp"), 0o755)
        bindir = os.path.join(home, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "tendril"), "w") as f:
            f.write("old launcher\n")
        os.symlink("tendril", os.path.join(bindir, "agent"))     # legacy
        script = os.path.join(home, "step6.sh")
        with open(script, "w") as f:
            f.write("set -e\nALIAS=home\nREPO=TENDRIL\n" + step6)
        env = {"HOME": home, "TMPDIR": home, "TENDRIL_REPO": REPO,
               "PATH": stubs + ":/usr/bin:/bin"}
        p = subprocess.run(["bash", script], env=env, capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for name in ("tendril", "agent", "termux-url-opener"):
            dest = os.path.join(bindir, name)
            self.assertFalse(os.path.islink(dest), name)
            self.assertEqual(read(dest), read(os.path.join(REPO, "phone", name)), name)
        self.assertEqual([n for n in os.listdir(home) if n.startswith("tendril-launchers")], [])


class AliasMissingFailsFast(unittest.TestCase):
    def test_no_alias_exits_one_quickly_and_lists_ssh_hosts(self):
        home = tempfile.mkdtemp(prefix="tendril phone ")
        self.addCleanup(shutil.rmtree, home, True)
        os.makedirs(os.path.join(home, ".ssh"))
        with open(os.path.join(home, ".ssh", "config"), "w") as f:
            f.write("Host omarchy\n    HostName omarchy\nHost *\n    ServerAliveInterval 30\n")
        stubs = os.path.join(home, "stubs")
        os.makedirs(stubs)
        log = os.path.join(home, "calls")
        for tool in ("ssh", "mosh"):
            with open(os.path.join(stubs, tool), "w") as f:
                f.write('#!/bin/sh\necho "$0 $*" >> "%s"\nsleep 30\n' % log)
            os.chmod(os.path.join(stubs, tool), 0o755)
        env = {"HOME": home, "PATH": stubs + ":/usr/bin:/bin", "TENDRIL_ALIAS": ""}
        p = subprocess.run(["sh", TENDRIL], env=env, capture_output=True,
                           text=True, timeout=10)
        self.assertEqual(p.returncode, 1)
        self.assertIn("no host alias", p.stdout)
        self.assertIn("hosts in ~/.ssh/config: omarchy", p.stdout)
        self.assertNotIn("*", p.stdout.split("hosts in ~/.ssh/config:")[1].splitlines()[0])
        self.assertFalse(os.path.exists(log))           # never dialled


class PhoneGeometry(unittest.TestCase):
    """S21 Ultra / Termux, portrait, keyboard + extra-keys row open:
    roughly 48-52 columns by 18-24 rows; 7 workspaces, each with a path."""

    def setUp(self):
        self.mod = tm.load(tm.CLI, "ra_android_geometry")

    def brand(self, out):
        plain = tm.SGR.sub("", out)
        return (any(c in out for c in tm.QUADS - {" "})
                or any(ln.strip() == "TENDRIL" for ln in plain.splitlines()))

    def test_keyboard_open_keeps_the_brand_and_fits(self):
        for cols in (48, 52):
            for lines in (18, 19, 20, 21, 22, 23, 24):
                with self.subTest(cols=cols, lines=lines):
                    out = tm.frame(self.mod, cols, lines, 7)
                    body = tm.SGR.sub("", out[len(tm.CLEAR):]).splitlines()
                    self.assertTrue(self.brand(out), "masthead vanished")
                    self.assertLessEqual(len(body) + 1, lines)   # + prompt
                    self.assertLessEqual(max(tm.visible(l) for l in body), cols)
                    self.assertIn("N New", out)                  # Dad's menu

    def test_keyboard_closed_shows_mark_and_paths(self):
        out = tm.frame(self.mod, 52, 45, 7)
        self.assertTrue(any(c in out for c in tm.QUADS - {" "}))
        self.assertIn("project-0", out)

    def test_no_color_never_drops_paths_for_a_mark_it_cannot_show(self):
        self.mod.COLOR = False
        out = tm.frame(self.mod, 48, 22, 7)
        self.assertIn("project-0", out)
        self.assertFalse(self.brand(out))


if __name__ == "__main__":
    unittest.main()
