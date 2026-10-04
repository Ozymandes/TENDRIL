"""Terminal capability model: terminal detection, the detach-bridge gate,
the Blink help variant, palette/colour rules, and layout hardening at
Blink-ish sizes.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import os
import re
import shutil
import sys
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "bin", "remote-agents")


def load_cli():
    loader = SourceFileLoader("remote_agents_terminal_caps", CLI)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ra = load_cli()

SGR = re.compile(r"\x1b\[([0-9;]*)([@-~])")


def visible(line):
    return SGR.sub("", line)


# A realistic two-workspace snapshot: paths, focused row, mixed agents.
SNAPSHOT_ROWS = [
    {"id": "w1", "label": "tendril", "number": 1, "focused": True,
     "path": "/home/user/Projects/tendril", "rank": 0,
     "agents": [{"agent": "pi", "agent_status": "working", "pane_id": "p1"}]},
    {"id": "w2", "label": "api-server", "number": 2, "focused": False,
     "path": "/home/user/Projects/api-server", "rank": 1,
     "agents": [{"agent": "claude", "agent_status": "blocked", "pane_id": "p2"},
                {"agent": "codex", "agent_status": "idle", "pane_id": "p3"}]},
]


class TerminalName(unittest.TestCase):
    """TERMUX_VERSION self-identifies and wins; TENDRIL_TERMINAL is the
    opt-in path (Blink sends no distinguishing env over SSH); junk values
    fall back to generic."""

    def test_detection_matrix(self):
        for env, want in (
                ({"TERMUX_VERSION": "0.118"}, "termux"),
                ({"TERMUX_VERSION": "0.118", "TENDRIL_TERMINAL": "blink"}, "termux"),
                ({"TERMUX_VERSION": "1", "TENDRIL_TERMINAL": "alacritty"}, "termux"),
                ({"TENDRIL_TERMINAL": "Blink"}, "blink"),
                ({"TENDRIL_TERMINAL": "blink"}, "blink"),
                ({"TENDRIL_TERMINAL": "alacritty"}, "alacritty"),  # passes through
                ({"TENDRIL_TERMINAL": "we!rd"}, "generic"),
                ({"TENDRIL_TERMINAL": ""}, "generic"),
                ({}, "generic"),
        ):
            self.assertEqual(ra.terminal_name(env), want, env)

    def test_charset_and_length_guard(self):
        self.assertEqual(ra.terminal_name({"TENDRIL_TERMINAL": "a" * 24}),
                         "a" * 24)
        self.assertEqual(ra.terminal_name({"TENDRIL_TERMINAL": "a" * 25}),
                         "generic")
        self.assertEqual(ra.terminal_name({"TENDRIL_TERMINAL": "Blink OS"}),
                         "generic")

    def test_reads_os_environ_by_default(self):
        with mock.patch.dict(os.environ, {"TENDRIL_TERMINAL": "blink"},
                             clear=True):
            self.assertEqual(ra.terminal_name(), "blink")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ra.terminal_name(), "generic")


class BridgeGate(unittest.TestCase):
    """Ctrl+Home bridge defaults ONLY on Termux — never on Blink or generic
    (Blink's on-screen keyboard cannot produce Ctrl+Home). TENDRIL_DETACH_BRIDGE
    is checked first and wins both ways, exactly as before the refactor."""

    def test_gate_matrix(self):
        for env, want in (
                ({"TERMUX_VERSION": "0.118"}, True),
                ({}, False),
                ({"TENDRIL_TERMINAL": "blink"}, False),
                ({"TENDRIL_TERMINAL": "blink", "TENDRIL_DETACH_BRIDGE": "1"}, True),
                ({"TERMUX_VERSION": "0.118", "TENDRIL_DETACH_BRIDGE": "0"}, False),
                ({"TERMUX_VERSION": "0.118", "TENDRIL_DETACH_BRIDGE": "false"}, False),
                ({"TERMUX_VERSION": "0.118", "TENDRIL_DETACH_BRIDGE": "no"}, False),
                ({"TERMUX_VERSION": "x", "TENDRIL_DETACH_BRIDGE": "off"}, False),
                ({"TERMUX_VERSION": "0.118", "TENDRIL_DETACH_BRIDGE": "1"}, True),
        ):
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(ra._bridge_enabled(), want, env)


class BlinkHelp(unittest.TestCase):
    """help_screen() swaps the Ctrl+Home bridge notes for the Blink block
    only when TENDRIL_TERMINAL=blink; generic and Termux keep the bridge
    notes."""

    def help_text(self, env):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(mock.patch.object(ra, "wait_for_key"))
            buf = io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(buf))
            ra.help_screen()
        return buf.getvalue()

    def test_blink_variant_mentions_blink_and_drops_ctrl_home(self):
        out = self.help_text({"TENDRIL_TERMINAL": "blink"})
        self.assertIn("Blink", out)
        self.assertIn("tap ALT then D", out)
        self.assertIn("TENDRIL_TERMINAL=blink", out)
        self.assertNotIn("Ctrl+Home", out)
        self.assertIn("Detach keeps workspaces and agents.", out)

    def test_generic_and_termux_keep_the_bridge_notes(self):
        for env in ({}, {"TERMUX_VERSION": "0.118"}):
            out = self.help_text(env)
            self.assertIn("Ctrl+Home", out, env)
            self.assertIn("TENDRIL_DETACH_BRIDGE=0", out, env)
            self.assertNotIn("TENDRIL_TERMINAL=blink selects", out, env)
            self.assertIn("Detach keeps workspaces and agents.", out)


class BlinkGeometry(unittest.TestCase):
    """render() stays inside the clamp(34..56) box at Blink-ish sizes and
    never prints a visible line wider than the box; the masthead steps down
    to the caption tier below 44 columns."""

    def render_at(self, cols, lines):
        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                ra.shutil, "get_terminal_size",
                return_value=os.terminal_size((cols, lines))))
            stack.enter_context(mock.patch.object(
                ra, "tailscale_parts", return_value=("ACTIVE", "100.64.0.9")))
            stack.enter_context(mock.patch.object(ra, "COLOR", True))
            stack.enter_context(contextlib.redirect_stdout(buf))
            ra.render([dict(r) for r in SNAPSHOT_ROWS])
        return [visible(ln) for ln in buf.getvalue().splitlines()]

    def test_render_smoke_at_blink_sizes(self):
        for (cols, lines), want_w in (
                ((36, 24), 36), ((44, 24), 44), ((52, 31), 52),
                ((58, 24), 56), ((74, 40), 56), ((100, 30), 56)):
            vis = self.render_at(cols, lines)
            edge = next(ln for ln in vis if ln.startswith("\u250c"))
            self.assertEqual(len(edge), want_w, (cols, lines))
            for ln in vis:
                self.assertLessEqual(len(ln), want_w, (cols, lines, ln))
            joined = "\n".join(vis)
            art = any("\u2588" in ln for ln in vis)   # masthead box-art tier
            if cols >= 44:
                self.assertTrue(art, (cols, lines))
            else:
                self.assertFalse(art, (cols, lines))
                self.assertIn("TENDRIL", joined, (cols, lines))  # caption tier


class Palette(unittest.TestCase):
    """pick_palette()/color_enabled() drive the module's import-time
    choices; behaviour identical to the previous inline expressions."""

    def test_truecolor_when_colorterm_advertises_it(self):
        tc = ra.pick_palette("truecolor")
        self.assertTrue(all(c.startswith("\033[38;2;") for c in tc))
        self.assertEqual(ra.pick_palette("24bit"), tc)
        self.assertEqual(ra.pick_palette("TrueColor"), tc)   # case-insensitive

    def test_256_fallback_without_advertisement(self):
        fb = ("\033[38;5;191m", "\033[38;5;73m",
              "\033[38;5;244m", "\033[38;5;203m")
        self.assertEqual(ra.pick_palette(None), fb)
        self.assertEqual(ra.pick_palette(""), fb)
        self.assertEqual(ra.pick_palette("1"), fb)

    def test_color_enabled_matrix(self):
        self.assertTrue(ra.color_enabled(True, None, "xterm-256color"))
        self.assertFalse(ra.color_enabled(False, None, "xterm-256color"))
        self.assertFalse(ra.color_enabled(True, "", "xterm-256color"))   # NO_COLOR=
        self.assertFalse(ra.color_enabled(True, "1", "xterm-256color"))  # NO_COLOR=1
        self.assertFalse(ra.color_enabled(True, None, "dumb"))

    def test_module_wiring_matches_the_helpers(self):
        self.assertEqual(ra._PALETTE, ra.pick_palette(os.environ.get("COLORTERM")))
        self.assertEqual(
            ra.COLOR,
            ra.color_enabled(sys.stdout.isatty(), os.environ.get("NO_COLOR"),
                             os.environ.get("TERM", "")))


if __name__ == "__main__":
    unittest.main()
