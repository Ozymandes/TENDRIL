"""Masthead + launcher tests: asset hygiene, deterministic build, selector
integrity, slogan, and the tendril/agent phone launchers.

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
SLOGAN_CHARS = set(build_masthead.SLOGAN)
GRAY = "\x1b[38;2;112;128;136m"


def load(path, name):
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod.COLOR = True                       # render frames with the module's own
    mod.C_RESET, mod.C_WHITE = "\033[0m", "\033[97m"           # palette
    mod.C_LIME, mod.C_TEAL = mod._PALETTE[0], mod._PALETTE[1]
    mod.C_GRAY, mod.C_RED = mod._PALETTE[2], mod._PALETTE[3]
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
                self.assertLessEqual(set(SGR.sub("", ln)), QUADS | SLOGAN_CHARS)
            plain = SGR.sub("", data).replace(" ", "")
            self.assertEqual(plain.count("THEEDGEISYOURS"), 1, f"{name}: slogan once")

    def test_runtime_embeds_the_committed_assets(self):
        mod = load(CLI, "ra_embed")
        results, ok = build_masthead.build_all(tempfile.mkdtemp(), quiet=True)
        self.assertTrue(ok)
        self.assertEqual([(m, tuple(l), sl) for _, m, l, sl in results], list(mod._MASTHEAD))


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

    def footer_items(self, text):
        lines = SGR.sub("", text[len(CLEAR):]).splitlines()
        dividers = [i for i, line in enumerate(lines) if line.startswith("\u251c")]
        bottom = next(i for i, line in enumerate(lines) if line.startswith("\u2514"))
        return [line[2:-1].rstrip() for line in lines[dividers[-1] + 1:bottom]]

    def test_footer_has_exactly_the_five_supported_actions(self):
        wide = frame(self.new, 48, 50, 0)
        narrow = frame(self.new, 34, 50, 0)
        self.assertEqual(self.footer_items(wide), [
            "N New   I Info   ? Help   R Refresh   Q Quit"])
        self.assertEqual(self.footer_items(narrow), [
            "N New   I Info   ? Help", "R Refresh   Q Quit"])
        for text in (wide, narrow):
            for removed in ("P Project", "S Shell", "L Last", "T TestPush"):
                self.assertNotIn(removed, text)

    def test_selector_is_byte_identical_below_the_masthead(self):
        for cols in (34, 40, 43, 44, 45, 48, 49, 50, 53, 54, 56, 60, 80):
            for n in (0, 1, 3, 6, 7, 10):
                for note in ("", "no remembered workspace yet"):
                    self.masthead_of(cols, n, note=note)

    def test_tiers_and_blank_separator(self):
        box = lambda c: max(34, min(c, 56))                      # noqa: E731
        for cols, arts in ((54, 8), (56, 8), (80, 8), (50, 8), (49, 7), (44, 7), (43, 2), (34, 2)):
            head = self.masthead_of(cols, 7)
            self.assertEqual(len(head), arts + 1, f"{cols} cols")
            self.assertEqual(head[-1], "", "one blank row before the box")
            for ln in head:
                self.assertLessEqual(visible(ln), box(cols), f"{cols}: masthead wider than box")
                self.assertTrue(ln == "" or ln.endswith("\x1b[0m"), "reset before selector")
            if arts == 2:
                self.assertEqual(SGR.sub("", head[0]).strip(), "TENDRIL")
                self.assertEqual(visible(head[0]) - 7, (box(cols) - 7) // 2, "caption centred")

    def test_slogan_sits_quietly_under_the_wordmark(self):
        for cols in (54, 50, 48, 44, 40, 34):
            head = self.masthead_of(cols, 7)
            line = next(ln for ln in head if "E D G E" in ln or "EDGE" in ln)
            self.assertIn(self.new.C_GRAY + "T H E", line, f"{cols}: slogan must be muted gray, tracked")
            self.assertEqual(SGR.sub("", line).replace(" ", "")[-14:], "THEEDGEISYOURS")
            self.assertLess(head.index(line), len(head) - 1, "blank row before the selector")
        # full tier: centred under TENDRIL (not the whole mark), one row of air above
        head = [SGR.sub("", ln) for ln in self.masthead_of(54, 7)]
        s_line = next(ln for ln in head if "E D G E" in ln)
        art = [ln.ljust(54) for ln in head if ln is not s_line]
        ink = [any(r[c] != " " for r in art) for c in range(54)]
        icon_end = ink.index(False, ink.index(True))                   # gap after the icon
        w0 = ink.index(True, icon_end)                                 # wordmark span
        w1 = max(c for c in range(54) if ink[c]) + 1
        s0, s1 = s_line.index("T H E"), len(s_line.rstrip())           # the icon's spine tip shares the row
        self.assertLessEqual(abs((s0 + s1) - (w0 + w1)), 2, "slogan centred under wordmark")
        self.assertFalse(head[head.index(s_line) - 1][w0:].strip(), "air between mark and slogan")

    def test_slogan_dropped_before_the_menu_suffers(self):
        for lines in range(20, 40):
            for n in (7, 10, 13):
                out = frame(self.new, 40, lines, n)[len(CLEAR):]
                body = frame(self.old, 40, lines, n)[len(CLEAR):]
                if body.count("\n") + 1 <= lines:
                    self.assertLessEqual(out.count("\n") + 1, lines)

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

    def test_phone_height_yields_paths_only_when_needed_to_fit(self):
        """Check actual phone geometry instead of requiring path suppression
        at a fixed workspace count: one extra row can preserve paths and art."""
        for n in (8, 11, 13):
            out = frame(self.new, 48, 31, n)[len(CLEAR):]
            self.assertIn("\u2580", out, f"{n} ws: compact mark missing at 48x31")
            self.assertLessEqual(out.count("\n") + 1, 31, f"{n} ws: overflows 48x31")
            if "project-0" not in out:
                self.assertGreater(out.count("\n") + 1 + n, 31,
                                   f"{n} ws: restoring paths should exceed phone height")
            open_out = frame(self.new, 48, 22, n)[len(CLEAR):]
            # Include the input-prompt row, not just the rendered box. Paths
            # must yield even when only the caption (or no mark) can fit.
            self.assertLessEqual(open_out.count("\n") + 1, 22,
                                 f"{n} ws: menu and prompt overflow keyboard-open height")
            self.assertNotIn("project-0", open_out,
                             f"{n} ws: paths must yield to the menu and prompt")

        one_more_row = frame(self.new, 48, 32, 8)[len(CLEAR):]
        self.assertIn("\u2580", one_more_row)
        self.assertIn("project-0", one_more_row)
        self.assertLessEqual(one_more_row.count("\n") + 1, 32)

    def test_path_yield_never_fires_when_there_is_room(self):
        """Generous height: body below the masthead is unchanged."""
        for n in (0, 3, 7, 10, 13):
            new = frame(self.new, 48, 50, n)
            old = frame(self.old, 48, 50, n)
            self.assertTrue(new.endswith(old[len(CLEAR):]), f"{n} ws")

    def test_long_host_label_never_breaks_the_box(self):
        """A prompt-captured TAILSCALE_HOST (old installer bug) must not
        overflow the title row: the label clamps, the suffix survives."""
        self.new.HOST_LABEL = "Tailscale hostname of this machine [horus] horus"
        try:
            out = frame(self.new, 48, 31, 3)[len(CLEAR):]
            for ln in out.split("\n"):
                self.assertLessEqual(visible(ln), 48)
            self.assertIn("REMOTE AGENTS", out)
        finally:
            self.new.HOST_LABEL = load(CLI, "ra_label_reset").HOST_LABEL

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


class Palette(unittest.TestCase):
    """Truecolour only when COLORTERM advertises it; 256-colour fallback
    everywhere else (mosh < 1.4 drops 38;2 sequences entirely, which
    rendered the whole phone UI monochrome)."""

    def env_load(self, name, colorterm):
        keep = os.environ.get("COLORTERM")
        if colorterm is None:
            os.environ.pop("COLORTERM", None)
        else:
            os.environ["COLORTERM"] = colorterm
        try:
            return load(CLI, name)
        finally:
            if keep is None:
                os.environ.pop("COLORTERM", None)
            else:
                os.environ["COLORTERM"] = keep

    def test_truecolour_when_advertised(self):
        tc = self.env_load("ra_palette_tc", "truecolor")
        self.assertEqual(tc._PALETTE[0], "\033[38;2;203;253;117m")
        self.assertEqual(tc._PALETTE[1], "\033[38;2;47;179;196m")

    def test_256_fallback_survives_mosh(self):
        fb = self.env_load("ra_palette_fb", None)
        self.assertEqual(fb._PALETTE,
                         ("\033[38;5;191m", "\033[38;5;73m",
                          "\033[38;5;244m", "\033[38;5;203m"))
        out = frame(fb, 48, 31, 3)
        self.assertIn("\033[38;5;191m", out)
        self.assertNotIn(";2;", out, "no truecolour SGR may reach the frame")


@unittest.skipUnless(origin_main_cli(), "origin/main already contains the masthead")
class AgainstOriginMain(unittest.TestCase):
    @staticmethod
    def without_footer(output):
        lines = SGR.sub("", output[len(CLEAR):]).splitlines()
        dividers = [i for i, line in enumerate(lines) if line.startswith("\u251c")]
        bottom = next(i for i, line in enumerate(lines) if line.startswith("\u2514"))
        return lines[:dividers[-1] + 1] + lines[bottom:]

    def test_selector_body_except_footer_matches_origin_main(self):
        with tempfile.NamedTemporaryFile("w", suffix="-remote-agents", delete=False) as t:
            t.write(origin_main_cli())
        try:
            old, new = load(t.name, "ra_origin_main"), load(CLI, "ra_branch")
            # The footer is intentionally different now. Compare the remaining
            # selector body, including path/masthead fit, against origin/main.
            for cols in (34, 44, 50, 54, 56, 80):
                for n in (0, 3, 7):
                    o, w = frame(old, cols, 36, n), frame(new, cols, 36, n)
                    self.assertEqual(self.without_footer(w), self.without_footer(o),
                                     f"{cols} cols, {n} ws")
                for n in (10, 13):
                    o, w = frame(old, cols, 50, n), frame(new, cols, 50, n)
                    self.assertEqual(self.without_footer(w), self.without_footer(o),
                                     f"{cols} cols, {n} ws @50")
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


class Launcher(unittest.TestCase):
    """phone/tendril is the launcher; `agent` (symlink or wrapper) must behave
    identically. ssh/mosh are stubbed to record what would be run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.log = os.path.join(t, "calls.log")
        self.stubs = os.path.join(t, "stubs")
        self.home_bin = os.path.join(t, "bin")                 # the phone's ~/bin
        os.makedirs(self.stubs)
        os.makedirs(self.home_bin)
        for tool in ("ssh", "mosh"):
            path = os.path.join(self.stubs, tool)
            with open(path, "w") as f:
                f.write(f'#!/bin/sh\necho "{tool} $*" >> "{self.log}"\nexit 0\n')
            os.chmod(path, 0o755)
        shutil.copy(os.path.join(REPO, "phone", "tendril"), os.path.join(self.home_bin, "tendril"))
        os.symlink("tendril", os.path.join(self.home_bin, "agent-link"))
        shutil.copy(os.path.join(REPO, "phone", "agent"), os.path.join(self.home_bin, "agent"))

    def tearDown(self):
        self.tmp.cleanup()

    def launch(self, name, args=(), **env):
        if os.path.exists(self.log):
            os.unlink(self.log)
        e = {"PATH": self.stubs + os.pathsep + os.environ["PATH"], "HOME": self.tmp.name}
        e.update(env)
        r = subprocess.run(["sh", os.path.join(self.home_bin, name), *args],
                           capture_output=True, text=True, env=e, timeout=20)
        calls = read(self.log, "r") if os.path.exists(self.log) else ""
        return r.returncode, calls, r.stdout

    def test_agent_alias_runs_exactly_what_tendril_runs(self):
        for args, env in (((), {"TENDRIL_ALIAS": "omarchy"}), ((), {"AGENT_ALIAS": "omarchy"}),
                          (("omarchy",), {}), (("ssh", "omarchy"), {"TENDRIL_ALIAS": "x"})):
            ref = self.launch("tendril", args, **env)
            self.assertEqual(ref[0], 0)
            self.assertIn("omarchy", ref[1])
            self.assertIn("remote-agents", ref[1])
            for alias in ("agent-link", "agent"):
                self.assertEqual(self.launch(alias, args, **env), ref, f"{alias} {args} {env}")

    def test_tendril_alias_wins_over_legacy_agent_alias(self):
        _, calls, _ = self.launch("tendril", TENDRIL_ALIAS="new", AGENT_ALIAS="old")
        self.assertIn("mosh new", calls)
        self.assertNotIn("old", calls)

    def test_missing_alias_prints_usage(self):
        code, calls, out = self.launch("agent")
        self.assertEqual(code, 1)
        self.assertIn("usage: tendril", out)
        self.assertEqual(calls, "")


if __name__ == "__main__":
    unittest.main()
