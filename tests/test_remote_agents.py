"""Interactive selector input, selection, and workspace creation behavior."""
import contextlib
import importlib.util
import io
import os
import pty
import shutil
import signal
import subprocess
import sys
import termios
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "bin", "remote-agents")


def load_cli():
    loader = SourceFileLoader("remote_agents_interaction", CLI)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ra = load_cli()


class KeyReader(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = pty.openpty()

    def tearDown(self):
        os.close(self.master)
        os.close(self.slave)

    def read(self, data):
        with ra.raw_terminal(self.slave):
            os.write(self.master, data)
            return ra.read_key(self.slave)

    def test_csi_ss3_and_modified_arrow_sequences(self):
        self.assertEqual(self.read(bytes([27]) + b"[A"), "up")
        self.assertEqual(self.read(bytes([27]) + b"OB"), "down")
        self.assertEqual(self.read(bytes([27]) + b"[1;5A"), "up")
        self.assertEqual(self.read(bytes([27]) + b"[1;2B"), "down")

    def test_bare_escape_is_bounded_and_not_an_eof(self):
        self.assertEqual(self.read(bytes([27])), "escape")

    def test_terminal_modes_restore_after_success_and_exception(self):
        original = termios.tcgetattr(self.slave)
        with ra.raw_terminal(self.slave):
            self.assertNotEqual(termios.tcgetattr(self.slave), original)
        self.assertEqual(termios.tcgetattr(self.slave), original)

        with self.assertRaisesRegex(RuntimeError, "boom"):
            with ra.raw_terminal(self.slave):
                raise RuntimeError("boom")
        self.assertEqual(termios.tcgetattr(self.slave), original)

    def test_numeric_input_supports_multi_digit_and_backspace_and_restores_tty(self):
        original = termios.tcgetattr(self.slave)
        output = io.StringIO()
        with mock.patch.object(ra.sys, "stdout", output):
            os.write(self.master, b"123" + bytes([127]) + b"4" + bytes([13]))
            self.assertEqual(ra.read_menu_action(True, self.slave), ("line", "124"))
        self.assertEqual(termios.tcgetattr(self.slave), original)

    def test_enter_and_command_keys_are_distinct_actions(self):
        with mock.patch.object(ra.sys, "stdout", io.StringIO()):
            os.write(self.master, bytes([13]))
            self.assertEqual(ra.read_menu_action(True, self.slave), ("enter", ""))
            os.write(self.master, b"N" + bytes([13]))
            self.assertEqual(ra.read_menu_action(True, self.slave), ("line", "N"))

    def test_non_tty_keeps_line_input(self):
        with mock.patch("builtins.input", return_value="12") as read_line:
            self.assertEqual(ra.read_menu_action(False), ("line", "12"))
        read_line.assert_called_once_with("> ")


class Selection(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"id": "w1", "focused": False},
            {"id": "w2", "focused": True},
            {"id": "w3", "focused": False},
        ]

    def test_initial_selection_prefers_focused_workspace(self):
        self.assertEqual(ra.selection_index(self.rows), 1)

    def test_refresh_preserves_selected_id_then_falls_back_sensibly(self):
        reordered = [self.rows[2], self.rows[0], self.rows[1]]
        self.assertEqual(ra.selection_index(reordered, "w3", 0), 0)
        removed = [{"id": "w1", "focused": False},
                   {"id": "w2", "focused": True}]
        self.assertEqual(ra.selection_index(removed, "gone", 1), 1)
        self.assertEqual(ra.selection_index([self.rows[0]], "gone", 2), 0)

    def test_navigation_clamps_and_empty_lists_are_safe(self):
        self.assertEqual(ra.move_selection(self.rows, 0, -1), 0)
        self.assertEqual(ra.move_selection(self.rows, 2, 1), 2)
        self.assertIsNone(ra.selection_index([]))
        self.assertIsNone(ra.move_selection([], None, 1))

    def test_number_selection_is_multi_digit_and_bounds_checked(self):
        self.assertEqual(ra.workspace_number("12", 12), 11)
        self.assertIsNone(ra.workspace_number("0", 12))
        self.assertIsNone(ra.workspace_number("13", 12))
        self.assertIsNone(ra.workspace_number("9" * 5000, 12))

    def test_selected_row_has_a_plain_visible_marker(self):
        menu_rows = [dict(row, label=f"workspace-{i}", agents=[], path="", rank=3)
                     for i, row in enumerate(self.rows)]
        buf = io.StringIO()
        with mock.patch.object(ra, "tailscale_parts", return_value=("DOWN", "")):
            with contextlib.redirect_stdout(buf):
                ra.render(menu_rows, selected=1)
        self.assertIn("> 2", buf.getvalue())

    def test_enter_attaches_the_selected_row_after_navigation(self):
        menu_rows = [dict(row, label=f"workspace-{i}", agents=[], path="", rank=3)
                     for i, row in enumerate(self.rows)]
        attached = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(ra, "render"))
            stack.enter_context(mock.patch.object(ra, "read_menu_action", side_effect=[
                ("navigate", "down"), ("enter", ""), ("line", "q")]))
            stack.enter_context(mock.patch.object(
                ra, "attach", side_effect=lambda row: attached.append(row["id"])))
            stack.enter_context(mock.patch.object(ra, "collect", return_value=menu_rows))
            self.assertEqual(ra.menu_loop(menu_rows, tty_mode=True, fd=0), 0)
        self.assertEqual(attached, ["w3"])

    def test_enter_and_numeric_selection_are_safe_for_empty_lists(self):
        notes = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                ra, "render", side_effect=lambda rows, note, selected: notes.append(note)))
            stack.enter_context(mock.patch.object(ra, "read_menu_action", side_effect=[
                ("enter", ""), ("line", "12"), ("line", "q")]))
            attach = stack.enter_context(mock.patch.object(ra, "attach"))
            stack.enter_context(mock.patch.object(ra, "collect", return_value=[]))
            self.assertEqual(ra.menu_loop([], tty_mode=False), 0)
        attach.assert_not_called()
        self.assertEqual(notes, ["", "no workspaces to attach", "no workspaces to attach"])


class WorkspaceCreation(unittest.TestCase):
    def test_new_workspace_without_usable_cwd_falls_back_to_dir_menu(self):
        events = []
        created = {"result": {
            "workspace": {"workspace_id": "w-new", "label": "Café API"},
            "root_pane": {"pane_id": "p-new"},
        }}
        with tempfile_directory() as cwd:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    ra, "ask_dir", side_effect=lambda: events.append("dir") or cwd))
                stack.enter_context(mock.patch.object(
                    ra, "ask_agent", side_effect=lambda: events.append("agent") or "claude"))
                stack.enter_context(mock.patch.object(
                    ra, "confirm", side_effect=lambda prompt: events.append(("confirm", prompt)) or True))
                stack.enter_context(mock.patch.object(
                    ra, "ws_create", side_effect=lambda path, label=None:
                    events.append(("create", path, label)) or (created, None)))
                stack.enter_context(mock.patch.object(
                    ra, "start_agent", side_effect=lambda *args:
                    events.append(("start",) + args) or (True, "")))
                stack.enter_context(mock.patch.object(
                    ra, "collect", return_value=[{"id": "w-new", "label": "Café API"}]))
                stack.enter_context(mock.patch.object(
                    ra, "attach", side_effect=lambda row: events.append(("attach", row["id"]))))
                stack.enter_context(mock.patch("builtins.input", return_value="cli"))
                result = ra.new_workspace([], None)

        self.assertEqual(result, "w-new")
        self.assertEqual(events[0:2], ["dir", "agent"])
        self.assertEqual(events[2], (
            "confirm", f"create 'cli' @ {ra.short_path(cwd)} + claude?"))
        self.assertEqual(events[3], ("create", cwd, "cli"))
        expected_name = ra.agent_name_for_workspace("Café API", cwd, "w-new")
        self.assertEqual(events[4], ("start", expected_name, "claude", "p-new"))
        self.assertEqual(events[5], ("attach", "w-new"))

    def test_project_launcher_uses_workspace_id_for_agent_name(self):
        with tempfile_directory() as root:
            project = os.path.join(root, "Project")
            os.mkdir(project)
            created = {"result": {
                "workspace": {"workspace_id": "w-project", "label": "Project"},
                "root_pane": {"pane_id": "p-project"},
            }}
            started = []
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(ra, "PROJECT_ROOTS", [root]))
                stack.enter_context(mock.patch.object(ra, "collect", side_effect=[[], []]))
                stack.enter_context(mock.patch.object(ra, "ask_agent", return_value="claude"))
                stack.enter_context(mock.patch.object(ra, "confirm", return_value=True))
                stack.enter_context(mock.patch.object(
                    ra, "ws_create", return_value=(created, None)))
                stack.enter_context(mock.patch.object(
                    ra, "start_agent", side_effect=lambda *args:
                    started.append(args) or (True, "")))
                stack.enter_context(mock.patch("builtins.input", return_value="1"))
                self.assertEqual(ra.project_launcher(), "w-project")
            self.assertEqual(started, [(
                ra.agent_name_for_workspace("Project", project, "w-project"),
                "claude", "p-project")])

    def test_ws_create_omits_optional_label_but_preserves_explicit_callsite_labels(self):
        with mock.patch.object(ra, "run", return_value=(0, "{}", "")) as run:
            ra.ws_create("/tmp/project", None, focus=False)
            self.assertEqual(run.call_args.args[0], [ra.HERDR, "workspace", "create",
                                                    "--cwd", "/tmp/project", "--no-focus"])
            ra.ws_create("/tmp/project", "shell-123", focus=True)
            self.assertEqual(run.call_args.args[0], [ra.HERDR, "workspace", "create",
                                                    "--cwd", "/tmp/project", "--label",
                                                    "shell-123", "--focus"])

    def test_agent_name_is_slugged_to_herdr_name_constraints(self):
        for label in ("Workspace with spaces", "42 project", "東京", "x" * 80):
            name = ra.agent_name_for_workspace(label, "/tmp/fallback")
            self.assertRegex(name, r"^[a-z][a-z0-9_-]{0,31}$")

    def test_agent_names_for_different_workspaces_are_unique_and_valid(self):
        first = ra.agent_name_for_workspace("shared label", "/one/project", "w1")
        second = ra.agent_name_for_workspace("shared-label", "/two/project", "w2")
        self.assertNotEqual(first, second)
        for name in (first, second):
            self.assertRegex(name, r"^[a-z][a-z0-9_-]{0,31}$")


class DirectoryInheritance(unittest.TestCase):
    """N/S flows default to the selected/focused workspace's directory.
    Ported from local ee54eeb, reconciled with origin/main's derived-label
    and agent-name safety (origin/main always asked the full dir menu)."""

    def test_current_ws_dir_prefers_selected_then_focused(self):
        with tempfile_directory() as sel_dir, tempfile_directory() as foc_dir:
            rows = [{"id": "w1", "focused": True, "path": foc_dir},
                    {"id": "w2", "focused": False, "path": sel_dir}]
            self.assertEqual(ra.current_ws_dir(rows, 1), sel_dir)
            self.assertEqual(ra.current_ws_dir(rows, None), foc_dir)
            self.assertIsNone(ra.current_ws_dir(
                [{"id": "w", "focused": False, "path": "/definitely/not/here"}], 0))
            self.assertIsNone(ra.current_ws_dir([], None))

    def test_enter_accepts_inherited_directory_and_empty_name_auto_labels(self):
        events = []
        created = {"result": {
            "workspace": {"workspace_id": "w-auto", "label": "inherit"},
            "root_pane": {"pane_id": "p-auto"},
        }}
        with tempfile_directory() as cwd:
            rows = [{"id": "w0", "focused": True, "path": cwd}]
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(ra, "ask_agent", return_value="shell"))
                stack.enter_context(mock.patch.object(
                    ra, "confirm", side_effect=lambda p: events.append(("confirm", p)) or True))
                stack.enter_context(mock.patch.object(
                    ra, "ws_create", side_effect=lambda path, label=None:
                    events.append(("create", path, label)) or (created, None)))
                stack.enter_context(mock.patch.object(
                    ra, "collect", return_value=rows + [{"id": "w-auto", "label": "inherit"}]))
                stack.enter_context(mock.patch.object(
                    ra, "attach", side_effect=lambda row: events.append(("attach", row["id"]))))
                stack.enter_context(mock.patch("builtins.input", side_effect=["", ""]))
                result = ra.new_workspace(rows, 0)

        self.assertEqual(result, "w-auto")
        self.assertEqual(events[0], (
            "confirm", f"create '{os.path.basename(cwd)} (auto)' "
                        f"@ {ra.short_path(cwd)} + shell?"))
        self.assertEqual(events[1], ("create", cwd, os.path.basename(cwd)))
        self.assertEqual(events[2], ("attach", "w-auto"))

    def test_explicit_absolute_path_overrides_inherited_default(self):
        created = {"result": {
            "workspace": {"workspace_id": "w2"},
            "root_pane": {"pane_id": "p2"},
        }}
        with tempfile_directory() as inherited, tempfile_directory() as override:
            rows = [{"id": "w0", "focused": True, "path": inherited}]
            creates = []
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(ra, "ask_agent", return_value="shell"))
                stack.enter_context(mock.patch.object(ra, "confirm", return_value=True))
                stack.enter_context(mock.patch.object(
                    ra, "ws_create", side_effect=lambda path, label=None:
                    creates.append((path, label)) or (created, None)))
                stack.enter_context(mock.patch.object(ra, "collect", return_value=rows))
                stack.enter_context(mock.patch.object(ra, "attach"))
                stack.enter_context(mock.patch(
                    "builtins.input", side_effect=["feat", override]))
                ra.new_workspace(rows, 0)
        self.assertEqual(creates, [(override, "feat")])

    def test_bad_override_cancels_without_creating(self):
        with tempfile_directory() as inherited:
            rows = [{"id": "w0", "focused": True, "path": inherited}]
            with contextlib.ExitStack() as stack:
                ws_create = stack.enter_context(mock.patch.object(ra, "ws_create"))
                ask_agent = stack.enter_context(mock.patch.object(ra, "ask_agent"))
                stack.enter_context(mock.patch(
                    "builtins.input", side_effect=["x", "relative/path"]))
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertIsNone(ra.new_workspace(rows, 0))
        self.assertIn("no such directory", buf.getvalue())
        ws_create.assert_not_called()
        ask_agent.assert_not_called()

    def test_quick_shell_inherits_selected_cwd_else_home(self):
        created = {"result": {"workspace": {"workspace_id": "s1"}}}
        with tempfile_directory() as cwd:
            rows = [{"id": "w0", "focused": True, "path": cwd}]
            creates = []
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    ra, "ws_create", side_effect=lambda path, label=None:
                    creates.append((path, label)) or (created, None)))
                stack.enter_context(mock.patch.object(ra, "collect", return_value=rows))
                stack.enter_context(mock.patch.object(ra, "attach"))
                ra.quick_shell(rows, 0)
                ra.quick_shell([], None)
        self.assertEqual(creates[0][0], cwd)
        self.assertEqual(creates[1][0], os.path.expanduser("~"))
        for path, label in creates:
            self.assertRegex(label, r"^shell-\d{6}$")

    def test_menu_passes_selection_into_n_and_s_flows(self):
        rows = [{"id": "w1", "focused": True}]
        calls = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(ra, "render"))
            stack.enter_context(mock.patch.object(
                ra, "read_menu_action",
                side_effect=[("line", "n"), ("line", "s"), ("line", "q")]))
            stack.enter_context(mock.patch.object(
                ra, "new_workspace",
                side_effect=lambda r, s: calls.append(("n", r, s))))
            stack.enter_context(mock.patch.object(
                ra, "quick_shell",
                side_effect=lambda r, s: calls.append(("s", r, s))))
            stack.enter_context(mock.patch.object(ra, "collect", return_value=rows))
            ra.menu_loop(rows, tty_mode=False)
        self.assertEqual(calls, [("n", rows, 0), ("s", rows, 0)])


class CtrlHomeBridge(unittest.TestCase):
    """CTRL+HOME -> ESC[1;5H -> rewritten to ESC d (Herdr's Alt+D detach).
    Verified Termux encoding: termux-app KeyHandler.getCode,
    KEYCODE_MOVE_HOME + transformForModifiers(KEYMOD_CTRL) -> 1;5H.
    Everything else must pass through byte-for-byte."""

    def test_ctrl_home_becomes_detach(self):
        tail, out = ra._detach_translate(b"\x1b[1;5H")
        self.assertEqual(out, b"\x1bd")
        self.assertEqual(tail, b"")

    def test_plain_home_passes_through(self):
        for home in (b"\x1b[H", b"\x1bOH"):     # normal + cursor-app mode
            tail, out = ra._detach_translate(home)
            self.assertEqual(out, home)          # byte-identical
            self.assertEqual(tail, b"")

    def test_ctrl_end_and_other_sequences_pass_through(self):
        for seq in (b"\x1b[1;5F", b"\x1b[1;5D", b"\x1b[5~", b"\x1b[A"):
            tail, out = ra._detach_translate(seq)
            self.assertEqual(out, seq)
            self.assertEqual(tail, b"")

    def test_ctrl_close_bracket_and_alt_d_pass_through(self):
        for seq in (b"\x1d", b"\x1bd", b"\x1b[27;5u"):
            tail, out = ra._detach_translate(seq)
            self.assertEqual(out, seq)
            self.assertEqual(tail, b"")

    def test_split_across_reads_still_translates(self):
        tail, out = ra._detach_translate(b"ls\r\x1b[1;")
        self.assertEqual(out, b"ls\r")
        tail, out = ra._detach_translate(tail + b"5H pwd\r")
        self.assertEqual(out, b"\x1bd pwd\r")
        self.assertEqual(tail, b"")

    def test_ambiguous_tail_is_forwarded_when_cancelled(self):
        tail, out = ra._detach_translate(b"\x1b[1;")
        self.assertEqual(out, b"")
        self.assertEqual(tail, b"\x1b[1;")
        tail, out = ra._detach_translate(tail + b"A")   # not ctrl+home
        self.assertEqual(out, b"\x1b[1;A")
        self.assertEqual(tail, b"")

    def test_multiple_sequences_in_one_stream(self):
        tail, out = ra._detach_translate(b"a\x1b[1;5Hb\x1b[1;5Hc")
        self.assertEqual(out, b"a\x1bdb\x1bdc")
        self.assertEqual(tail, b"")

    # ---- real-PTY harness: driver runs _attach_session with our pty slave

    def _bridge_session(self, stub_body):
        """Start `python driver.py` with stdin/stdout on a fresh pty.
        Returns (process, master, slave, tmpdir)."""
        tmp = tempfile.mkdtemp(prefix="tendril-bridge-")
        stub = os.path.join(tmp, "stub-herdr")
        with open(stub, "w") as f:
            f.write(stub_body)
        os.chmod(stub, 0o755)
        driver_path = os.path.join(tmp, "driver.py")
        with open(driver_path, "w") as f:
            f.write(
                "from importlib.machinery import SourceFileLoader\n"
                "import importlib.util\n"
                "mod = importlib.util.module_from_spec(\n"
                "    importlib.util.spec_from_loader(\n"
                "        'ra', SourceFileLoader('ra', %r)))\n" % CLI +
                "SourceFileLoader('ra', %r).exec_module(mod)\n" % CLI +
                "mod.HERDR = %r\n" % stub +
                "mod._attach_session()\n")
        master, slave = pty.openpty()
        orig = termios.tcgetattr(slave)   # before the driver can go raw
        proc = subprocess.Popen(
            [sys.executable, driver_path], stdin=slave, stdout=slave,
            stderr=slave, close_fds=True)
        return proc, master, slave, orig, tmp

    def _read_until(self, master, predicate, timeout=8.0):
        import select as sel
        out = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = sel.select([master], [], [], 0.3)
            if not r:
                if predicate(out):
                    break
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if predicate(out):
                break
        return out

    ECHO_STUB = (
        "#!/usr/bin/env python3\n"
        "import os, sys, termios, tty\n"
        "old = termios.tcgetattr(0)\n"
        "tty.setraw(0)\n"
        "buf = b''\n"
        "try:\n"
        "    while True:\n"
        "        d = os.read(0, 1024)\n"
        "        if not d:\n"
        "            break\n"
        "        buf += d\n"
        "        sys.stdout.write(d.hex())\n"
        "        sys.stdout.flush()\n"
        "        if b'\\x1bd' in buf:\n"   # detach key reached the session: exit
        "            break\n"
        "finally:\n"
        "    termios.tcsetattr(0, termios.TCSADRAIN, old)\n")

    def test_pty_roundtrip_home_kept_ctrl_home_detaches_and_tty_restored(self):
        """HOME alone reaches the session unchanged; Ctrl+Home reaches it as
        the ESC d detach; the session exits, the user tty is restored and the
        bridge exits 0."""
        proc, master, slave, orig, tmp = self._bridge_session(self.ECHO_STUB)
        self.addCleanup(self._cleanup, proc, master, slave, tmp)
        time.sleep(0.8)                      # let the bridge go raw
        os.write(master, b"\x1b[H")          # HOME alone
        time.sleep(0.3)
        os.write(master, b"\x1b[1;5H")       # CTRL+HOME -> ESC d
        out = self._read_until(master, lambda b: b"1b5b481b64" in b)
        self.assertIn(b"1b5b48", out)        # HOME forwarded verbatim
        self.assertIn(b"1b5b481b64", out)    # ...then ESC d (the detach)
        self.assertEqual(proc.wait(10), 0)   # session exit reaped cleanly
        self.assertEqual(termios.tcgetattr(slave), orig)

    def test_held_partial_sequence_flushes_when_input_goes_idle(self):
        """A cancelled combo must not hold bytes until the next keystroke:
        after the select timeout the held prefix is forwarded as-is."""
        proc, master, slave, orig, tmp = self._bridge_session(self.ECHO_STUB)
        self.addCleanup(self._cleanup, proc, master, slave, tmp)
        time.sleep(0.8)
        os.write(master, b"\x1b[1;")         # partial: could still complete
        seen = self._read_until(master, lambda b: b"1b5b313b" in b, timeout=4.0)
        self.assertIn(b"1b5b313b", seen)     # forwarded by the idle flush
        os.write(master, b"A")
        out = self._read_until(master, lambda b: b"41" in b, timeout=4.0)
        self.assertIn(b"41", out)
        os.close(master)                     # hangup: bridge must not hang
        self.assertIsNotNone(proc.wait(10))

    def test_master_close_exits_promptly(self):
        proc, master, slave, orig, tmp = self._bridge_session(self.ECHO_STUB)
        self.addCleanup(self._cleanup, proc, master, slave, tmp)
        time.sleep(0.8)
        os.close(master)                     # hangup: bridge must restore + exit
        self.assertIsNotNone(proc.wait(10))

    def test_resize_propagates_to_the_session(self):
        import fcntl
        import struct
        stub = (
            "#!/usr/bin/env python3\n"
            "import fcntl, os, signal, struct, sys, termios, tty\n"
            "def report(*_):\n"
            "    s = fcntl.ioctl(0, termios.TIOCGWINSZ, b'\\0'*8)\n"
            "    rows, cols = struct.unpack('HH', s[:4])\n"
            "    sys.stdout.write('SIZE:%dx%d\\n' % (rows, cols))\n"
            "    sys.stdout.flush()\n"
            "signal.signal(signal.SIGWINCH, report)\n"
            "report()\n"
            "tty.setraw(0)\n"
            "try:\n"
            "    while True:\n"
            "        if not os.read(0, 256):\n"
            "            break\n"
            "finally:\n"
            "    pass\n")
        proc, master, slave, orig, tmp = self._bridge_session(stub)
        self.addCleanup(self._cleanup, proc, master, slave, tmp)
        time.sleep(0.8)
        fcntl.ioctl(master, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 41, 97, 0, 0))
        os.kill(proc.pid, signal.SIGWINCH)
        out = self._read_until(master, lambda b: b"SIZE:41x97" in b, timeout=5.0)
        self.assertIn(b"SIZE:41x97", out)

    @staticmethod
    def _cleanup(proc, master, slave, tmp):
        for fd in (master, slave):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            proc.terminate()
            proc.wait(5)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


class DetachGate(unittest.TestCase):
    """The bridge is a TENDRIL convenience: automatic on Termux, opt-in
    elsewhere, TENDRIL_DETACH_BRIDGE=0 disables it everywhere."""

    ENV_KEYS = ("TERMUX_VERSION", "TENDRIL_DETACH_BRIDGE")

    def test_gate_matrix(self):
        saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        try:
            for env, want in (
                    ({"TERMUX_VERSION": "0.118"}, True),
                    ({}, False),
                    ({"TERMUX_VERSION": "0.118", "TENDRIL_DETACH_BRIDGE": "0"}, False),
                    ({"TENDRIL_DETACH_BRIDGE": "1"}, True),
                    ({"TERMUX_VERSION": "x", "TENDRIL_DETACH_BRIDGE": "off"}, False),
            ):
                for k in self.ENV_KEYS:
                    os.environ.pop(k, None)
                os.environ.update(env)
                self.assertEqual(ra._bridge_enabled(), want, env)
        finally:
            for k in self.ENV_KEYS:
                os.environ.pop(k, None)
                if saved[k] is not None:
                    os.environ[k] = saved[k]

    def test_attach_hint_and_attach_path_follow_the_gate(self):
        row = {"id": "w9", "label": "demo"}
        for enabled in (True, False):
            with contextlib.ExitStack() as stack:
                # Sessions launched inside Herdr set HERDR_ENV=1; the test
                # must exercise the launcher paths, not that passthrough.
                stack.enter_context(mock.patch.dict(os.environ))
                os.environ.pop("HERDR_ENV", None)
                stack.enter_context(mock.patch.object(
                    ra, "_bridge_enabled", return_value=enabled))
                run = stack.enter_context(mock.patch.object(ra, "run"))
                bridge = stack.enter_context(mock.patch.object(ra, "_attach_session"))
                sub = stack.enter_context(mock.patch.object(ra.subprocess, "run"))
                stack.enter_context(mock.patch.object(ra, "save_last"))
                buf = io.StringIO()
                stack.enter_context(contextlib.redirect_stdout(buf))
                ra.attach(row)
            self.assertEqual(bridge.called, enabled)
            self.assertEqual(sub.called, not enabled)
            self.assertIn("Ctrl+Home" if enabled else "Alt+D", buf.getvalue())
            self.assertIn("menu returns here", buf.getvalue())

    def test_attach_session_falls_back_without_tty(self):
        attached = []
        orig_stdin = sys.stdin
        r, w = os.pipe()
        sys.stdin = os.fdopen(r, "rb")
        with mock.patch.object(
                ra.subprocess, "run",
                staticmethod(lambda args, check=False: attached.append(args))):
            try:
                ra._attach_session()
            finally:
                sys.stdin = orig_stdin
                os.close(w)
        self.assertTrue(attached and attached[0][1:3] == ["session", "attach"])


@contextlib.contextmanager
def tempfile_directory():
    import tempfile
    with tempfile.TemporaryDirectory(prefix="Tendril Café ") as path:
        yield path


if __name__ == "__main__":
    unittest.main()
