"""Masthead tests: asset hygiene, deterministic build, selector integrity.

Run from the repo root:  python3 -m unittest discover -s tests -v
The PTY test needs a reachable Herdr server and is skipped otherwise.
"""
import contextlib
import importlib.util
import io
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "bin", "remote-agents")
ASSETS = os.path.join(REPO, "assets", "masthead")
sys.path.insert(0, ASSETS)
import build_masthead  # noqa: E402

CLEAR = "\033[H\033[2J"
SGR = re.compile(r"\x1b\[([0-9;]*)([@-~])")
QUADS = set(build_masthead.GLYPH.values())


def load(path, name):
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod.COLOR = True
    mod.C_RESET, mod.C_LIME = "\033[0m", "\033[38;2;203;253;117m"
    mod.C_TEAL, mod.C_GRAY = "\033[38;2;47;179;196m", "\033[38;2;112;128;136m"
    mod.C_WHITE, mod.C_RED = "\033[97m", "\033[38;2;232;92;111m"
    mod.tailscale_parts = lambda: ("ACTIVE", "100.64.0.1")
    return mod


def rows(n):
    return [{"id": f"w{i}", "label": f"workspace-{i}", "number": i, "focused": i == 0,
             "agents": [{"agent": "claude", "agent_status": "idle"}],
             "path": f"/home/u/project-{i}", "rank": 3} for i in range(n)]


def frame(mod, cols, lines, n, note=""):
    real = shutil.get_terminal_size
    shutil.get_terminal_size = lambda fallback=(52, 24): os.terminal_size((cols, lines))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod.render(rows(n), note)
        return buf.getvalue()
    finally:
        shutil.get_terminal_size = real


def visible(s):
    return len(SGR.sub("", s))


def read(path, mode="rb"):
    with open(path, mode) as f:
        return f.read()


class Assets(unittest.TestCase):
    def test_build_is_deterministic_and_matches_committed_assets(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            build_masthead.build_all(a, quiet=True)
            build_masthead.build_all(b, quiet=True)
            for name, *_ in build_masthead.TIERS:
                fa = read(os.path.join(a, f"tendril_{name}.ans"))
                fb = read(os.path.join(b, f"tendril_{name}.ans"))
                fc = read(os.path.join(ASSETS, f"tendril_{name}.ans"))
                self.assertEqual(fa, fb, f"{name}: build not deterministic")
                self.assertEqual(fa, fc, f"{name}: committed asset is stale")

    def test_asset_hygiene(self):
        for name, _, budget, *_ in build_masthead.TIERS:
            data = read(os.path.join(ASSETS, f"tendril_{name}.ans"), "r")
            seqs = SGR.findall(data)
            self.assertEqual({f for _, f in seqs}, {"m"}, "only SGR, no cursor motion")
            self.assertFalse([p for p, _ in seqs if "48" in p.split(";")], "no background colour")
            for ln in data.rstrip("\n").split("\n"):
                self.assertTrue(ln.endswith("\x1b[0m"), "every line resets")
                self.assertLessEqual(visible(ln), budget)
                self.assertLessEqual(set(SGR.sub("", ln)), QUADS)

    def test_runtime_embeds_the_committed_assets(self):
        mod = load(CLI, "ra_embed")
        results, ok = build_masthead.build_all(tempfile.mkdtemp(), quiet=True)
        self.assertTrue(ok)
        self.assertEqual([(m, tuple(l)) for _, m, l in results], [(m, a) for m, a in mod._MASTHEAD])


class Selector(unittest.TestCase):
    """The masthead is only ever prepended: the selector below it must be
    byte-identical to what the same CLI prints with the masthead switched off."""

    def setUp(self):
        self.new = load(CLI, "ra_branch")
        self.old = load(CLI, "ra_baseline")
        self.old.masthead = lambda width, body: []

    def masthead_of(self, cols, n, lines=36, note=""):
        old, new = frame(self.old, cols, lines, n, note), frame(self.new, cols, lines, n, note)
        self.assertTrue(new.startswith(CLEAR) and old.startswith(CLEAR))
        body = old[len(CLEAR):]
        self.assertTrue(new.endswith(body), f"{cols}x{lines}/{n}: selector changed")
        head = new[len(CLEAR):len(new) - len(body)]
        return head.split("\n")[:-1] if head else []

    def test_selector_is_byte_identical_below_the_masthead(self):
        for cols in (34, 40, 43, 44, 45, 48, 49, 50, 53, 54, 56, 60, 80):
            for n in (0, 1, 3, 6, 7, 10):
                for note in ("", "no remembered workspace yet"):
                    self.masthead_of(cols, n, note=note)

    def test_tiers_and_blank_separator(self):
        box = lambda c: max(34, min(c, 56))                      # noqa: E731
        for cols, arts in ((54, 8), (56, 8), (80, 8), (50, 8), (49, 6), (44, 6), (43, 1), (34, 1)):
            head = self.masthead_of(cols, 7)
            self.assertEqual(len(head), arts + 1, f"{cols} cols")
            self.assertEqual(head[-1], "", "one blank row before the box")
            for ln in head:
                self.assertLessEqual(visible(ln), box(cols), f"{cols}: masthead wider than box")
                self.assertTrue(ln == "" or ln.endswith("\x1b[0m"), "reset before selector")
            if arts == 1:
                self.assertEqual(SGR.sub("", head[0]).strip(), "TENDRIL")
                self.assertEqual(visible(head[0]) - 7, (box(cols) - 7) // 2, "caption centred")

    def test_nothing_wraps_at_phone_width(self):
        for n in (0, 3, 7, 10):
            out = frame(self.new, 54, 36, n)[len(CLEAR):]
            for ln in out.split("\n"):
                self.assertLessEqual(visible(ln), 54)

    def test_selector_always_fits_vertically(self):
        for lines in (20, 24, 30, 36, 50):
            for n in range(0, 16):
                out = frame(self.new, 54, lines, n)[len(CLEAR):]
                body = frame(self.old, 54, lines, n)[len(CLEAR):]
                if body.count("\n") + 1 <= lines:                # + input prompt line
                    self.assertLessEqual(out.count("\n") + 1, lines, f"{lines} rows, {n} ws")

    def test_color_off_prints_no_masthead(self):
        for mod in (self.new, self.old):
            mod.COLOR = False
            mod.C_RESET = mod.C_LIME = mod.C_TEAL = mod.C_GRAY = mod.C_WHITE = mod.C_RED = ""
        for cols in (40, 54):
            self.assertEqual(frame(self.new, cols, 36, 7), frame(self.old, cols, 36, 7))


def origin_main_cli():
    """origin/main's remote-agents while it predates the masthead, else None."""
    if not shutil.which("git"):
        return None
    src = subprocess.run(["git", "-C", REPO, "show", "origin/main:bin/remote-agents"],
                         capture_output=True, text=True)
    if src.returncode or "def masthead(" in src.stdout:
        return None
    return src.stdout


@unittest.skipUnless(origin_main_cli(), "origin/main already contains the masthead")
class AgainstOriginMain(unittest.TestCase):
    def test_selector_is_byte_identical_to_origin_main(self):
        with tempfile.NamedTemporaryFile("w", suffix="-remote-agents", delete=False) as t:
            t.write(origin_main_cli())
        try:
            old, new = load(t.name, "ra_origin_main"), load(CLI, "ra_branch")
            for cols in (34, 44, 50, 54, 56, 80):
                for n in (0, 3, 7, 10):
                    o, w = frame(old, cols, 36, n), frame(new, cols, 36, n)
                    self.assertTrue(w.endswith(o[len(CLEAR):]), f"{cols} cols, {n} ws")
        finally:
            os.unlink(t.name)


def pty_run(argv, env_extra, cols=54, lines=36, keys=b"q\n", timeout=20):
    """Run argv on a real pseudo-terminal of cols x lines, answer the first
    '> ' prompt with `keys`, return (output, exit code or None if killed)."""
    import fcntl
    import struct
    import termios
    env = dict(os.environ, TERM="xterm-256color")
    env.pop("NO_COLOR", None)
    env.update(env_extra)
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(argv[0], argv, env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", lines, cols, 0, 0))
    out, sent, code, end = b"", False, None, time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.3)
        if r:
            try:
                out += os.read(fd, 65536)
            except OSError:                                       # child closed the pty
                pass
        if not sent and b"> " in out:
            os.write(fd, keys)
            sent = True
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            code = os.waitstatus_to_exitcode(status)
            break
    if code is None:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    os.close(fd)
    return out.decode("utf-8", "replace"), code


def herdr_reachable():
    r = subprocess.run([sys.executable, CLI, "--detect"], capture_output=True, text=True)
    return "chosen: /" in r.stdout


@unittest.skipUnless(herdr_reachable(), "no Herdr server/client here")
class Pty(unittest.TestCase):
    def test_real_startup_renders_masthead_and_quits_cleanly(self):
        out, code = pty_run([sys.executable, CLI], {})
        self.assertEqual(code, 0, out[-300:])
        self.assertIn("\u2580", out)                               # quadrant art present
        self.assertIn("REMOTE AGENTS", out)
        self.assertIn("\u250c", out)
        self.assertIn("\u2518", out)
        head = out.split(CLEAR, 1)[1].split("\u250c", 1)[0]
        self.assertLessEqual(max(visible(l) for l in head.split("\r\n")), 54)

    def test_no_color_startup_has_no_masthead(self):
        out, code = pty_run([sys.executable, CLI], {"NO_COLOR": "1"})
        self.assertEqual(code, 0)
        self.assertNotIn("\u2580", out)
        self.assertIn("REMOTE AGENTS", out)


if __name__ == "__main__":
    unittest.main()
