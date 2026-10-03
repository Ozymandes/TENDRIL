"""Interactive selector input, selection, and workspace creation behavior."""
import contextlib
import importlib.util
import io
import os
import pty
import termios
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
    def test_new_workspace_uses_directory_label_and_valid_agent_name(self):
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
                stack.enter_context(mock.patch(
                    "builtins.input", side_effect=AssertionError("no name prompt expected")))
                result = ra.new_workspace()

        self.assertEqual(result, "w-new")
        self.assertEqual(events[0:2], ["dir", "agent"])
        self.assertEqual(events[2], (
            "confirm", f"create workspace @ {ra.short_path(cwd)} + claude?"))
        self.assertEqual(events[3], ("create", cwd, os.path.basename(cwd)))
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


@contextlib.contextmanager
def tempfile_directory():
    import tempfile
    with tempfile.TemporaryDirectory(prefix="Tendril Café ") as path:
        yield path


if __name__ == "__main__":
    unittest.main()
