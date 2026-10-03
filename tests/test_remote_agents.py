"""Focused UX tests: detach binding surfaces + new-workspace directory default.

Run from the repo root:  python3 -m unittest discover -s tests -v

Herdr-independent: the CLI module is imported against a fake `herdr` stub so
no live server is needed. The detach key grammar asserted here was verified
against `herdr config check` (herdr 0.9.1) and the v0.9.x config grammar:
accepted names include `ctrl+esc`, `ctrl+]`, `alt+d`, `prefix+d`; Herdr's
parser has no home/end/pageup keys in any modifier combination and rejects
raw escape sequences, so the detach set must stay within the accepted set
(otherwise Herdr silently disables the binding and keys leak into sessions).
"""
import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "bin", "remote-agents")
INSTALL = os.path.join(REPO, "install")


def load(path, name):
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod.COLOR = True
    return mod


# Verified-accepted Herdr key names for the detach set (see module docstring).
VERIFIED_DETACH_KEYS = {"prefix+d", "alt+d", "ctrl+esc", "ctrl+]"}


def _fake_herdr(tmp):
    """Stub client that answers the module's protocol probe instantly."""
    fake = os.path.join(tmp, "herdr")
    with open(fake, "w") as f:
        f.write("#!/bin/sh\n"
                "case \"$1\" in\n"
                "  --version) echo 'herdr 0.0.0-test' ;;\n"
                "  status) echo 'status: running';"
                " echo 'endpoint_compatible: yes' ;;\n"
                "  *) printf '' ;;\n"
                "esac\n")
    os.chmod(fake, 0o755)
    return fake


_TMP = tempfile.mkdtemp(prefix="tendril-test-")
_CFG = os.path.join(_TMP, "config")
with open(_CFG, "w") as f:
    f.write("HERDR_BIN=%s\n" % _fake_herdr(_TMP))

# Scope the config override to the import only: the module resolves its Herdr
# client at load time, and later subprocess tests (masthead PTY) must see the
# real environment.
_prev_cfg = os.environ.get("REMOTE_AGENTS_CONFIG")
os.environ["REMOTE_AGENTS_CONFIG"] = _CFG
try:
    MOD = load(CLI, "remote_agents_test")
finally:
    if _prev_cfg is None:
        os.environ.pop("REMOTE_AGENTS_CONFIG", None)
    else:
        os.environ["REMOTE_AGENTS_CONFIG"] = _prev_cfg

INSTALL_SRC = open(INSTALL).read()


def rows_fixture(dirpath, focused=True):
    return [{"id": "w1", "label": "ws", "number": 1, "focused": focused,
             "agents": [], "path": dirpath, "rank": 0}]


class CurrentWsDirTests(unittest.TestCase):
    def test_prefers_focused_workspace_directory(self):
        d = tempfile.mkdtemp(prefix="tendril-cwd-")
        self.assertEqual(MOD.current_ws_dir(rows_fixture(d)), d)

    def test_ignores_missing_directory(self):
        self.assertIsNone(MOD.current_ws_dir(rows_fixture("/no/such/dir")))

    def test_ignores_unfocused_rows(self):
        d = tempfile.mkdtemp(prefix="tendril-cwd-")
        self.assertIsNone(MOD.current_ws_dir(rows_fixture(d, focused=False)))
        self.assertIsNone(MOD.current_ws_dir([]))


class WsCreateTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._orig = MOD.run
        MOD.run = lambda args, timeout=6: (self.calls.append(args),
                                           (0, '{"result":{}}', ""))[1]

    def tearDown(self):
        MOD.run = self._orig

    def test_empty_label_uses_herdr_default_naming(self):
        MOD.ws_create("/tmp", None)
        args = self.calls[0]
        self.assertNotIn("--label", args)
        self.assertIn("--cwd", args)

    def test_explicit_label_is_passed(self):
        MOD.ws_create("/tmp", "myws")
        args = self.calls[0]
        self.assertIn("--label", args)
        self.assertEqual(args[args.index("--label") + 1], "myws")


class NewWorkspaceTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="tendril-new-")
        self.current = d
        self.other = tempfile.mkdtemp(prefix="tendril-other-")
        self.rows = rows_fixture(d)
        self.created = []
        self._ws_create = MOD.ws_create
        MOD.ws_create = lambda cwd, label=None, focus=True: (
            self.created.append((cwd, label)), ({"result": {}}, None))[1]

    def tearDown(self):
        MOD.ws_create = self._ws_create

    def _run(self, inputs):
        import builtins
        it = iter(inputs)
        orig = builtins.input
        builtins.input = lambda prompt="": next(it)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                MOD.new_workspace(self.rows)
            return buf.getvalue()
        finally:
            builtins.input = orig

    def test_enter_inherits_current_workspace_directory(self):
        self._run(["", "", "1", ""])   # auto name, Enter dir, shell, go
        self.assertEqual(self.created, [(self.current, None)])

    def test_explicit_path_override_wins(self):
        self._run(["myws", self.other, "1", ""])
        self.assertEqual(self.created, [(self.other, "myws")])

    def test_bad_override_is_rejected_without_creating(self):
        out = self._run(["", "/no/such/dir/xyz", "1", ""])
        self.assertEqual(self.created, [])
        self.assertIn("no such directory", out)

    def test_falls_back_to_dir_menu_without_focused_workspace(self):
        sentinel = tempfile.mkdtemp(prefix="tendril-menu-")
        self.rows = rows_fixture(self.current, focused=False)
        orig = MOD.ask_dir
        MOD.ask_dir = lambda: sentinel
        try:
            self._run(["x", "1", ""])   # no dir> prompt in this path
        finally:
            MOD.ask_dir = orig
        self.assertEqual(self.created, [(sentinel, "x")])


class QuickShellTests(unittest.TestCase):
    def test_inherits_current_directory(self):
        d = tempfile.mkdtemp(prefix="tendril-shell-")
        created = []
        orig = MOD.ws_create
        MOD.ws_create = lambda cwd, label=None, focus=True: (
            created.append((cwd, label)), ({"result": {}}, None))[1]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                MOD.quick_shell(rows_fixture(d))
        finally:
            MOD.ws_create = orig
        self.assertEqual(created[0][0], d)
        self.assertTrue(created[0][1].startswith("shell-"))

    def test_home_fallback_without_focused_workspace(self):
        created = []
        orig = MOD.ws_create
        MOD.ws_create = lambda cwd, label=None, focus=True: (
            created.append((cwd, label)), ({"result": {}}, None))[1]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                MOD.quick_shell([])
        finally:
            MOD.ws_create = orig
        self.assertEqual(created[0][0], os.path.expanduser("~"))


class DetachHintTests(unittest.TestCase):
    def test_help_screen_lists_primary_and_legacy(self):
        orig = MOD._read_key
        MOD._read_key = lambda: " "
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                MOD.help_screen()
        finally:
            MOD._read_key = orig
        out = buf.getvalue()
        self.assertIn("Ctrl+Esc", out)
        self.assertIn("Ctrl+]", out)
        self.assertIn("detach to REMOTE AGENTS", out)

    def test_attach_hints_primary_and_legacy_and_focuses_first(self):
        runs, attached = [], []
        orig_run, orig_sub = MOD.run, MOD.subprocess
        MOD.run = lambda args, timeout=6: (runs.append(args), (0, "", ""))[1]
        MOD.subprocess = type("S", (), {
            "run": staticmethod(lambda args, check=False: attached.append(args)),
        })
        MOD.save_last = lambda row: None
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                MOD.attach({"id": "w9", "label": "demo"})
        finally:
            MOD.run, MOD.subprocess = orig_run, orig_sub
        out = buf.getvalue()
        self.assertIn("Ctrl+Esc", out)
        self.assertIn("Ctrl+]", out)
        self.assertIn("menu returns here", out)
        # safety behavior preserved: focus the workspace, then attach session
        self.assertEqual(runs[0][1:], ["workspace", "focus", "w9"])
        self.assertTrue(attached and attached[0][1:3] == ["session", "attach"])


class InstallerDetachMergeTests(unittest.TestCase):
    def _embedded(self):
        m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", INSTALL_SRC, re.S)
        self.assertIsNotNone(m, "herdr_detach heredoc not found in install")
        return m.group(1)

    def _run_embedded(self, conf, mode):
        argv = sys.argv
        sys.argv = ["herdr_detach", conf, mode]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exec(self._embedded(), {"__name__": "herdr_detach_test"})
            return buf.getvalue()
        finally:
            sys.argv = argv

    def test_want_list_stays_within_verified_herdr_grammar(self):
        m = re.search(r'^WANT = \[(.*?)\]$', INSTALL_SRC, re.M)
        self.assertIsNotNone(m)
        want = set(re.findall(r'"([^"]+)"', m.group(1)))
        self.assertTrue(want <= VERIFIED_DETACH_KEYS,
                        "unverified key name would be disabled by herdr and "
                        "leak into the session: %s" % (want - VERIFIED_DETACH_KEYS))
        self.assertIn("ctrl+esc", want)   # primary
        self.assertIn("ctrl+]", want)     # legacy retained

    def test_merge_adds_primary_and_keeps_legacy_and_others(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[keys]\nprefix = "ctrl+space"\n'
                    'detach = ["prefix+d"]\nzoom = "prefix+z"\n')
            conf = f.name
        plan = self._run_embedded(conf, "plan")
        self.assertIn("ctrl+esc", plan)
        self.assertIn("ctrl+]", plan)
        self._run_embedded(conf, "apply")
        with open(conf) as f:
            text = f.read()
        self.assertIn('detach = ["prefix+d", "alt+d", "ctrl+esc", "ctrl+]"]', text)
        self.assertIn('zoom = "prefix+z"', text)   # unrelated binding untouched

    def test_merge_skips_conflicting_direct_keys(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[keys]\ndetach = ["prefix+d"]\nfoo = "ctrl+esc"\n')
            conf = f.name
        plan = self._run_embedded(conf, "plan")
        self.assertIn("CONFLICT=ctrl+esc (used by foo)", plan)
        self._run_embedded(conf, "apply")
        with open(conf) as f:
            text = f.read()
        m = re.search(r'^detach = \[(.*?)\]$', text, re.M)
        keys = re.findall(r'"([^"]+)"', m.group(1))
        self.assertNotIn("ctrl+esc", keys)   # never steals a claimed key
        self.assertIn("ctrl+]", keys)        # legacy still added
        self.assertIn('foo = "ctrl+esc"', text)  # decoy binding untouched

    def test_merge_skips_only_the_conflicting_key(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[keys]\ndetach = ["prefix+d"]\nbar = "ctrl+]"\n')
            conf = f.name
        self._run_embedded(conf, "apply")
        with open(conf) as f:
            text = f.read()
        m = re.search(r'^detach = \[(.*?)\]$', text, re.M)
        keys = re.findall(r'"([^"]+)"', m.group(1))
        self.assertNotIn("ctrl+]", keys)     # v0.1.1 guarantee: never steal
        self.assertIn("ctrl+esc", keys)      # primary still added

    def test_installer_keeps_config_check_safety_gate(self):
        self.assertIn("config check", INSTALL_SRC)
        self.assertIn("reload-config", INSTALL_SRC)
        self.assertIn("restoring backup", INSTALL_SRC)


if __name__ == "__main__":
    unittest.main()
