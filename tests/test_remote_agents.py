"""Focused UX tests: detach binding surfaces + new-workspace directory default.

Run from the repo root:  python3 -m unittest discover -s tests -v

Herdr-independent: the CLI module is imported against a fake `herdr` stub so
no live server is needed. Two key facts are pinned here:

- Detach grammar: Herdr accepts `ctrl+]`, `alt+d`, `prefix+d` (verified
  against `herdr config check`, herdr 0.9.1, and the v0.9.x config docs).
  Herdr's parser has no home/end/pageup keys in any modifier combination and
  rejects raw escape sequences — a rejected binding would be disabled and
  its key would leak into the session.
- Ctrl+Home therefore cannot be a Herdr config binding. Termux encodes the
  combo as `ESC [ 1 ; 5 H` (termux-app KeyHandler.getCode +
  transformForModifiers), and remote-agents bridges exactly those bytes to
  Herdr's existing Alt+D detach while forwarding everything else — plain
  HOME included — byte-for-byte.
"""
import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import time
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
# No phone keyboard remapping happens anywhere: the existing Termux CTRL and
# HOME buttons work together, and the bridge translates only their combo.
VERIFIED_DETACH_KEYS = {"prefix+d", "alt+d", "ctrl+]"}


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
    def test_help_screen_lists_ctrl_home_primary_and_compat(self):
        orig = MOD._read_key
        MOD._read_key = lambda: " "
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                MOD.help_screen()
        finally:
            MOD._read_key = orig
        out = buf.getvalue()
        self.assertIn("Ctrl+Home", out)
        self.assertIn("detach to REMOTE AGENTS", out)
        self.assertIn("Alt+D", out)
        self.assertIn("Ctrl+]", out)
        self.assertNotIn("\u2302", out)          # no extra-key button

    def test_attach_hints_ctrl_home_and_bridges_session(self):
        runs, bridged = [], []
        orig_run, orig_bridge = MOD.run, MOD._attach_session
        MOD.run = lambda args, timeout=6: (runs.append(args), (0, "", ""))[1]
        MOD._attach_session = lambda: bridged.append(True)
        MOD.save_last = lambda row: None
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                MOD.attach({"id": "w9", "label": "demo"})
        finally:
            MOD.run, MOD._attach_session = orig_run, orig_bridge
        out = buf.getvalue()
        self.assertIn("Ctrl+Home", out)
        self.assertIn("Alt+D", out)
        self.assertIn("Ctrl+]", out)
        self.assertIn("menu returns here", out)
        self.assertNotIn("\u2302", out)
        # safety behavior preserved: focus the workspace, then attach session
        self.assertEqual(runs[0][1:], ["workspace", "focus", "w9"])
        self.assertEqual(bridged, [True])

    def test_attach_session_falls_back_without_tty(self):
        attached = []
        orig_sub, orig_stdin = MOD.subprocess, sys.stdin
        MOD.subprocess = type("S", (), {
            "run": staticmethod(lambda args, check=False: attached.append(args)),
        })
        r, w = os.pipe()
        sys.stdin = os.fdopen(r, "rb")
        try:
            MOD._attach_session()
        finally:
            sys.stdin = orig_stdin
            MOD.subprocess = orig_sub
            os.close(w)
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
        self.assertNotIn("ctrl+esc", want)  # no invented host-side bindings
        self.assertIn("ctrl+]", want)       # original single-byte detach kept

    def test_merge_adds_alt_d_and_keeps_legacy_and_others(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[keys]\nprefix = "ctrl+space"\n'
                    'detach = ["prefix+d"]\nzoom = "prefix+z"\n')
            conf = f.name
        plan = self._run_embedded(conf, "plan")
        self.assertIn("ctrl+]", plan)
        self.assertIn("alt+d", plan)
        self._run_embedded(conf, "apply")
        with open(conf) as f:
            text = f.read()
        self.assertIn('detach = ["prefix+d", "alt+d", "ctrl+]"]', text)
        self.assertIn('zoom = "prefix+z"', text)   # unrelated binding untouched

    def test_merge_token_match_ignores_alt_down(self):
        # "alt+d" must not substring-match "alt+down" and cry conflict
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[keys]\ndetach = ["prefix+d"]\nfoo = "alt+down"\n')
            conf = f.name
        plan = self._run_embedded(conf, "plan")
        self.assertEqual(plan.splitlines()[0], "CONFLICT=")
        self._run_embedded(conf, "apply")
        with open(conf) as f:
            text = f.read()
        self.assertIn('detach = ["prefix+d", "alt+d", "ctrl+]"]', text)

    def test_merge_skips_conflicting_direct_keys(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[keys]\ndetach = ["prefix+d"]\nbar = "alt+d"\n')
            conf = f.name
        plan = self._run_embedded(conf, "plan")
        self.assertIn("CONFLICT=alt+d (used by bar)", plan)
        self._run_embedded(conf, "apply")
        with open(conf) as f:
            text = f.read()
        m = re.search(r'^detach = \[(.*?)\]$', text, re.M)
        keys = re.findall(r'"([^"]+)"', m.group(1))
        self.assertNotIn("alt+d", keys)      # never steals a claimed key
        self.assertIn("ctrl+]", keys)        # legacy still added
        self.assertIn('bar = "alt+d"', text)  # decoy binding untouched

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
        self.assertIn("alt+d", keys)         # desktop alternative still added

    def test_installer_keeps_config_check_safety_gate(self):
        self.assertIn("config check", INSTALL_SRC)
        self.assertIn("reload-config", INSTALL_SRC)
        self.assertIn("restoring backup", INSTALL_SRC)


class CtrlHomeBridgeTests(unittest.TestCase):
    """CTRL+HOME -> ESC[1;5H -> rewritten to ESC d (Herdr detach).
    Verified Termux encoding: termux-app KeyHandler.getCode,
    KEYCODE_MOVE_HOME + transformForModifiers(KEYMOD_CTRL) -> 1;5H."""

    def test_ctrl_home_becomes_detach(self):
        tail, out = MOD._detach_translate(b"\x1b[1;5H")
        self.assertEqual(out, b"\x1bd")
        self.assertEqual(tail, b"")

    def test_plain_home_passes_through(self):
        for home in (b"\x1b[H", b"\x1bOH"):     # normal + cursor-app mode
            tail, out = MOD._detach_translate(home)
            self.assertEqual(out, home)          # byte-identical
            self.assertEqual(tail, b"")

    def test_ctrl_end_and_other_sequences_pass_through(self):
        for seq in (b"\x1b[1;5F", b"\x1b[1;5D", b"\x1b[5~", b"\x1b[A"):
            tail, out = MOD._detach_translate(seq)
            self.assertEqual(out, seq)
            self.assertEqual(tail, b"")

    def test_split_across_reads_still_translates(self):
        tail, out = MOD._detach_translate(b"ls\r\x1b[1;")
        self.assertEqual(out, b"ls\r")
        tail, out = MOD._detach_translate(tail + b"5H pwd\r")
        self.assertEqual(out, b"\x1bd pwd\r")
        self.assertEqual(tail, b"")

    def test_ambiguous_tail_is_forwarded_when_cancelled(self):
        tail, out = MOD._detach_translate(b"\x1b[1;")
        self.assertEqual(out, b"")
        self.assertEqual(tail, b"\x1b[1;")
        tail, out = MOD._detach_translate(tail + b"A")   # not ctrl+home
        self.assertEqual(out, b"\x1b[1;A")
        self.assertEqual(tail, b"")

    def test_multiple_sequences_in_one_stream(self):
        tail, out = MOD._detach_translate(b"a\x1b[1;5Hb\x1b[1;5Hc")
        self.assertEqual(out, b"a\x1bdb\x1bdc")
        self.assertEqual(tail, b"")

    def test_pty_roundtrip_home_kept_ctrl_home_detaches(self):
        """Real PTY round-trip: HOME alone reaches Herdr unchanged;
        Ctrl+Home reaches it as the ESC d detach."""
        import pty
        import select as sel
        tmp = tempfile.mkdtemp(prefix="tendril-bridge-")
        stub = os.path.join(tmp, "stub-herdr")
        with open(stub, "w") as f:
            f.write(
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
                "        if b'\\x1bd' in buf:\n"
                "            break\n"
                "finally:\n"
                "    termios.tcsetattr(0, termios.TCSADRAIN, old)\n"
                "sys.stdout.write('GOT:' + buf.hex())\n"
                "sys.stdout.flush()\n")
        os.chmod(stub, 0o755)
        driver = os.path.join(tmp, "driver.py")
        with open(driver, "w") as f:
            f.write(
                "from importlib.machinery import SourceFileLoader\n"
                "import importlib.util\n"
                "mod = importlib.util.module_from_spec(\n"
                "    importlib.util.spec_from_loader(\n"
                "        'ra', SourceFileLoader('ra', %r)))\n" % CLI +
                "SourceFileLoader('ra', %r).exec_module(mod)\n" % CLI +
                "mod.HERDR = %r\n" % stub +
                "mod._attach_session()\n")
        pid, master = pty.fork()
        if pid == 0:
            os.execvp(sys.executable, [sys.executable, driver])
            os._exit(127)
        out = b""
        try:
            time.sleep(0.8)                    # let the bridge go raw
            os.write(master, b"\x1b[H")         # HOME alone
            time.sleep(0.3)
            os.write(master, b"\x1b[1;5H")      # CTRL+HOME
            deadline = time.time() + 10
            while time.time() < deadline:
                r, _, _ = sel.select([master], [], [], 0.4)
                if not r:
                    continue
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
                if b"GOT:" in out and b"1b64" in out.split(b"GOT:")[-1]:
                    break
        finally:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            try:
                os.close(master)
            except OSError:
                pass
        self.assertGreater(out.find(b"GOT:"), -1, out[-400:])
        got = out.split(b"GOT:", 1)[1][:20]
        self.assertEqual(got, b"1b5b481b64")   # ESC[H then ESC d


class ExtraKeyRevertTests(unittest.TestCase):
    """Guard: the ⌂ extra-key experiment stays reverted."""

    def test_no_helper_script(self):
        self.assertFalse(os.path.exists(os.path.join(REPO, "phone",
                                                     "tendril-keys")))

    def test_installer_has_no_button_logic(self):
        self.assertNotIn("tendril-keys", INSTALL_SRC)
        self.assertNotIn("\u2302", INSTALL_SRC)

    def test_selector_has_no_button_hints_or_termux_writes(self):
        src = open(CLI).read()
        self.assertNotIn("\u2302", src)
        self.assertNotIn("termux.properties", src)


if __name__ == "__main__":
    unittest.main()
