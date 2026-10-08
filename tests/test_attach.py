"""Direct session addressing: resolve_token, subcommand dispatch, phone launcher."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "bin", "remote-agents")
LAUNCHER = os.path.join(REPO, "phone", "tendril")

sys.path.insert(0, os.path.join(REPO, "bin"))
import tendril_link as tl  # noqa: E402


def load_cli():
    loader = SourceFileLoader("remote_agents_attach", CLI)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ra = load_cli()


def rows_fixture():
    """The shape collect() produces, minus the snapshot plumbing."""
    return [
        {"id": "w15", "label": "tendril", "number": 3, "focused": True,
         "agents": [{"agent": "pi", "agent_status": "working",
                     "pane_id": "w15:p1", "cwd": "/home/u/Projects/tendril"}],
         "path": "/home/u/Projects/tendril", "rank": 0},
        {"id": "wK", "label": "kindle-notes", "number": 4, "focused": False,
         "agents": [{"agent": "claude", "agent_status": "idle",
                     "pane_id": "wK:p1", "cwd": "/home/u/notes"}],
         "path": "/home/u/notes", "rank": 3},
        {"id": "w2", "label": "api", "number": 5, "focused": False,
         "agents": [], "path": "", "rank": 4},
        {"id": "w3", "label": "api", "number": 6, "focused": False,
         "agents": [], "path": "", "rank": 4},
        {"id": "w9", "label": "7", "number": 8, "focused": False,
         "agents": [], "path": "", "rank": 4},
    ]


class ResolveToken(unittest.TestCase):
    def setUp(self):
        self.rows = rows_fixture()

    def resolved(self, token):
        row, failure = ra.resolve_token(self.rows, token)
        return (row["id"] if row else None), failure

    def test_exact_workspace_id_is_canonical(self):
        self.assertEqual(self.resolved("wK"), ("wK", None))

    def test_ids_are_case_sensitive(self):
        self.assertEqual(self.resolved("W15"), (None, ("unknown", [])))
        self.assertEqual(self.resolved("wk"), (None, ("unknown", [])))

    def test_pane_id_resolves_to_parent_workspace(self):
        self.assertEqual(self.resolved("w15:p1"), ("w15", None))
        self.assertEqual(self.resolved("w99:p1"), (None, ("unknown", [])))
        self.assertEqual(self.resolved("w15:"), (None, ("unknown", [])))

    def test_pure_digits_select_the_workspace_number(self):
        self.assertEqual(self.resolved("3"), ("w15", None))
        self.assertEqual(self.resolved("4"), ("wK", None))

    def test_digits_fall_through_to_exact_label_without_number_match(self):
        self.assertEqual(self.resolved("7"), ("w9", None))

    def test_exact_unique_label(self):
        self.assertEqual(self.resolved("tendril"), ("w15", None))

    def test_ambiguous_label_returns_both_matches(self):
        row, failure = ra.resolve_token(self.rows, "api")
        self.assertIsNone(row)
        kind, hits = failure
        self.assertEqual(kind, "ambiguous")
        self.assertEqual([r["id"] for r in hits], ["w2", "w3"])

    def test_unknown_token(self):
        self.assertEqual(self.resolved("nope"), (None, ("unknown", [])))

    def test_empty_token_is_unknown(self):
        self.assertEqual(self.resolved(""), (None, ("unknown", [])))

    def test_oversized_token_is_unknown(self):
        self.assertEqual(self.resolved("w" * 130), (None, ("unknown", [])))


class Dispatch(unittest.TestCase):
    """main() subcommand plumbing with the Herdr client fully faked."""

    def setUp(self):
        self.rows = rows_fixture()
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.attach_mock = mock.Mock()
        self.menu_mock = mock.Mock(return_value=0)
        self.collect_mock = mock.Mock(return_value=self.rows)
        self.run_calls = []

    def fake_run(self, args, timeout=6):
        self.run_calls.append(list(args))
        return 0, "endpoint_compatible: yes\nstatus: running\n", ""

    def main(self, argv, rows="default", herdr="herdr"):
        """Run ra.main(argv) against the fakes; returns the exit code."""
        if rows != "default":
            self.collect_mock.return_value = rows
        ctx = [
            mock.patch.object(ra, "HERDR", herdr),
            mock.patch.object(ra, "HOST_LABEL", "testhost"),
            mock.patch.object(ra, "collect", self.collect_mock),
            mock.patch.object(ra, "attach", self.attach_mock),
            mock.patch.object(ra, "menu_loop", self.menu_mock),
            mock.patch.object(ra, "run", self.fake_run),
            mock.patch.object(ra.sys, "argv", ["remote-agents"] + argv),
            mock.patch.object(ra.sys, "stdout", self.out),
            mock.patch.object(ra.sys, "stderr", self.err),
        ]
        for c in ctx:
            c.start()
        try:
            return ra.main()
        finally:
            for c in reversed(ctx):
                c.stop()

    def test_attach_resolves_and_delegates_to_attach(self):
        self.assertEqual(self.main(["attach", "4"]), 0)
        self.attach_mock.assert_called_once()
        self.assertEqual(self.attach_mock.call_args[0][0]["id"], "wK")
        self.assertIn("wK", self.out.getvalue())

    def test_attach_unknown_token_exits_two(self):
        self.assertEqual(self.main(["attach", "gone"]), 2)
        self.assertFalse(self.attach_mock.called)
        self.assertIn("gone", self.err.getvalue())

    def test_attach_ambiguous_token_exits_three(self):
        self.assertEqual(self.main(["attach", "api"]), 3)
        self.assertFalse(self.attach_mock.called)
        self.assertIn("w2", self.err.getvalue())

    def test_unreachable_server_exits_one(self):
        self.assertEqual(self.main(["attach", "w15"], rows=None), 1)

    def test_missing_herdr_client_exits_one_without_snapshot(self):
        self.assertEqual(self.main(["resolve", "w15"], herdr=None), 1)
        self.assertFalse(self.collect_mock.called)

    def test_resolve_prints_round_trippable_json(self):
        self.assertEqual(self.main(["resolve", "w15"]), 0)
        lines = self.out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        doc = json.loads(lines[0])
        self.assertEqual(doc["host"], "testhost")
        self.assertEqual(doc["workspace_id"], "w15")
        self.assertEqual(doc["label"], "tendril")
        self.assertEqual(doc["number"], 3)
        self.assertEqual(doc["cwd"], "/home/u/Projects/tendril")
        self.assertEqual(doc["agent"], "pi")
        self.assertEqual(doc["agent_status"], "working")
        self.assertEqual(doc["agents"],
                         [{"agent": "pi", "agent_status": "working",
                           "pane_id": "w15:p1"}])
        self.assertTrue(doc["focused"])
        self.assertEqual(doc["herdr_status"], "running")
        self.assertEqual(tl.parse_payload_b64(doc["payload_b64"]),
                         {"host": "testhost", "workspace_id": "w15",
                          "label": "tendril"})
        self.assertEqual(tl.parse_uri(doc["link"])["workspace_id"], "w15")

    def test_resolve_failure_exit_codes_match_attach(self):
        self.assertEqual(self.main(["resolve", "gone"]), 2)
        self.assertEqual(self.main(["resolve", "api"]), 3)

    def test_focus_focuses_headless_and_prints_json(self):
        self.assertEqual(self.main(["focus", "4"]), 0)
        self.assertIn(["herdr", "workspace", "focus", "wK"], self.run_calls)
        doc = json.loads(self.out.getvalue().strip())
        self.assertEqual(doc["workspace_id"], "wK")

    def test_link_prints_exactly_the_uri_line(self):
        self.assertEqual(self.main(["link", "wK"]), 0)
        lines = self.out.getvalue().strip().splitlines()
        self.assertEqual(lines, [tl.uri("testhost", "wK", "kindle-notes")])
        self.assertEqual(tl.parse_uri(lines[0])["workspace_id"], "wK")

    def test_unknown_subcommand_is_a_usage_error(self):
        self.assertEqual(self.main(["bogus", "x"]), 2)
        self.assertIn("usage", self.err.getvalue())

    def test_wrong_arity_is_a_usage_error(self):
        self.assertEqual(self.main(["attach"]), 2)
        self.assertEqual(self.main(["resolve", "w15", "extra"]), 2)
        self.assertIn("usage", self.err.getvalue())

    def test_no_args_still_opens_the_selector(self):
        self.assertEqual(self.main([]), 0)
        self.menu_mock.assert_called_once()
        self.assertEqual(self.menu_mock.call_args[0][0], self.rows)
        self.assertFalse(self.attach_mock.called)

    def test_detect_still_wins_over_everything(self):
        self.assertEqual(self.main(["--detect"]), 0)
        self.assertIn("chosen:", self.out.getvalue())
        self.assertFalse(self.collect_mock.called)


class SnapshotShape(unittest.TestCase):
    """collect() over a realistic 0.9.1 snapshot feeds resolve + links."""

    SNAPSHOT = {
        "result": {"snapshot": {
            "workspaces": [
                {"workspace_id": "w15", "label": "tendril", "number": 1,
                 "focused": True, "active_tab_id": "t1",
                 "agent_status": "working", "pane_count": 2, "tab_count": 2},
                {"workspace_id": "wK", "label": "kindle-notes", "number": 2,
                 "focused": False, "active_tab_id": "t9",
                 "agent_status": "idle", "pane_count": 1, "tab_count": 1},
            ],
            "panes": [
                {"pane_id": "w15:p1", "tab_id": "t1",
                 "cwd": "/home/u/Projects/tendril",
                 "foreground_cwd": "/home/u/Projects/tendril",
                 "agent": "pi", "agent_status": "working",
                 "agent_session": "pi-s1", "terminal_title": "pi"},
                {"pane_id": "wK:p1", "tab_id": "t2", "cwd": "/home/u/notes",
                 "foreground_cwd": "/home/u/notes", "agent": "claude",
                 "agent_status": "idle", "agent_session": "cl-s1",
                 "terminal_title": "claude"},
            ],
            "agents": [
                {"pane_id": "w15:p1", "workspace_id": "w15", "tab_id": "t1",
                 "cwd": "/home/u/Projects/tendril",
                 "foreground_cwd": "/home/u/Projects/tendril",
                 "agent": "pi", "agent_status": "working",
                 "agent_session": "pi-s1", "terminal_title": "pi"},
                {"pane_id": "wK:p1", "workspace_id": "wK", "tab_id": "t2",
                 "cwd": "/home/u/notes", "foreground_cwd": "/home/u/notes",
                 "agent": "claude", "agent_status": "idle",
                 "agent_session": "cl-s1", "terminal_title": "claude"},
            ],
        }},
    }

    def test_collect_rows_resolve_and_link_round_trip(self):
        def fake_run(args, timeout=6):
            if args[-1] == "snapshot":      # `herdr api snapshot` envelope
                return 0, json.dumps(self.SNAPSHOT), ""
            return 0, "status: running\n", ""
        with mock.patch.object(ra, "HOST_LABEL", "testhost"), \
                mock.patch.object(ra, "run", fake_run):
            # no snapshot() patch: the real envelope parsing must produce rows
            rows = ra.collect()
            by_id = {r["id"]: r for r in rows}
            self.assertEqual(by_id["w15"]["label"], "tendril")
            self.assertEqual(by_id["w15"]["path"], "/home/u/Projects/tendril")
            self.assertEqual(by_id["w15"]["rank"], 0)
            self.assertTrue(by_id["w15"]["focused"])
            row, failure = ra.resolve_token(rows, "w15:p1")
            self.assertIsNone(failure)
            self.assertEqual(row["id"], "w15")
            doc = json.loads(ra.session_json(by_id["w15"]))
        self.assertEqual(doc["agent"], "pi")
        self.assertEqual(doc["agent_status"], "working")
        self.assertEqual(doc["herdr_status"], "running")
        self.assertEqual(doc["agents"][0]["pane_id"], "w15:p1")
        self.assertEqual(tl.parse_payload_b64(doc["payload_b64"]),
                         {"host": "testhost", "workspace_id": "w15",
                          "label": "tendril"})
        self.assertEqual(tl.parse_uri(doc["link"])["host"], "testhost")


def _fake_tool(name, body):
    return "#!/bin/sh\n" + body.format(name=name)


class PhoneLauncher(unittest.TestCase):
    """phone/tendril end-to-end against faked ssh/mosh/remote-agents."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril-launcher-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)
        self.record = os.path.join(self.tmp, "calls.log")
        bodies = {
            "ssh": "printf '{name} %s\\n' \"$*\" >> \"$TENDRIL_RECORD\"\nexit 0\n",
            # simulate the remote side: run the command string the launcher built
            "mosh": ("printf '{name} %s\\n' \"$*\" >> \"$TENDRIL_RECORD\"\n"
                     "while [ $# -gt 0 ] && [ \"$1\" != \"--\" ]; do shift; done\n"
                     "[ $# -gt 0 ] && shift\n"
                     "[ \"$1\" = \"sh\" ] && shift\n"
                     "[ \"$1\" = \"-lc\" ] && shift\n"
                     "exec sh -c \"$1\"\n"),
            "remote-agents": "printf '{name} %s\\n' \"$*\" >> \"$TENDRIL_RECORD\"\n",
        }
        for name, body in bodies.items():
            path = os.path.join(self.bindir, name)
            with open(path, "w") as f:
                f.write(_fake_tool(name, body))
            os.chmod(path, 0o755)

    def calls(self):
        try:
            with open(self.record) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def run_launcher(self, args, alias="testhost"):
        env = dict(os.environ)
        env["PATH"] = self.bindir + os.pathsep + env.get("PATH", "")
        env["TENDRIL_RECORD"] = self.record
        env["REMOTE_AGENTS_BIN"] = "remote-agents"
        # isolate HOME so the launcher's ~/.config/tendril/alias lookup can
        # never pick up a real alias file from the developer's home
        env["HOME"] = os.path.join(self.tmp, "isolated home")
        os.makedirs(env["HOME"], exist_ok=True)
        env.pop("XDG_CONFIG_HOME", None)
        if alias is None:
            env.pop("TENDRIL_ALIAS", None)
            env.pop("AGENT_ALIAS", None)
        else:
            env["TENDRIL_ALIAS"] = alias
            env.pop("AGENT_ALIAS", None)
        return subprocess.run(["sh", LAUNCHER] + args, env=env,
                              capture_output=True, text=True, timeout=60)

    def test_no_args_opens_the_selector_via_mosh(self):
        p = self.run_launcher([])
        self.assertEqual(p.returncode, 0)
        self.assertTrue(any(c.startswith("ssh -o ConnectTimeout=5") for c in self.calls()))
        mosh = [c for c in self.calls() if c.startswith("mosh ")]
        self.assertEqual(len(mosh), 1)
        self.assertIn("testhost", mosh[0])
        self.assertNotIn("attach", mosh[0])
        self.assertTrue(any(c.rstrip() == "remote-agents" for c in self.calls()))

    def test_attach_uses_default_alias_and_quotes_the_id(self):
        p = self.run_launcher(["attach", "w15"])
        self.assertEqual(p.returncode, 0)
        mosh = [c for c in self.calls() if c.startswith("mosh ")]
        self.assertEqual(len(mosh), 1)
        self.assertIn("attach 'w15'", mosh[0])
        # the remote shell receives the quoted id and passes it as one argument
        self.assertIn("remote-agents attach w15", self.calls())

    def test_enter_uses_default_alias_quotes_the_id_and_execs_remote(self):
        p = self.run_launcher(["enter", "w15:p1"])
        self.assertEqual(p.returncode, 0)
        mosh = [c for c in self.calls() if c.startswith("mosh ")]
        self.assertEqual(len(mosh), 1)
        # the remote command is `exec remote-agents enter '<id>'`, so the
        # selector's Quit ends the session (no orphan mosh)
        self.assertIn("sh -lc exec remote-agents enter 'w15:p1'", mosh[0])
        # the remote shell receives the quoted id as one argument
        self.assertIn("remote-agents enter w15:p1", self.calls())

    def test_enter_invalid_id_exits_two_without_connecting(self):
        p = self.run_launcher(["enter", "x;rm"])
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])
        self.assertIn("usage", p.stdout + p.stderr)
        p = self.run_launcher(["enter", "w" * 129])
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_explicit_alias_attach_respects_the_alias(self):
        p = self.run_launcher(["otherhost", "attach", "wK"], alias="unused")
        self.assertEqual(p.returncode, 0)
        mosh = [c for c in self.calls() if c.startswith("mosh ")]
        self.assertEqual(len(mosh), 1)
        self.assertIn("otherhost", mosh[0])
        self.assertIn("attach 'wK'", mosh[0])
        self.assertNotIn("unused", mosh[0])

    def test_ssh_subcommand_skips_mosh(self):
        p = self.run_launcher(["ssh", "testhost"])
        self.assertEqual(p.returncode, 0)
        self.assertFalse(any(c.startswith("mosh ") for c in self.calls()))
        self.assertIn("ssh -t testhost remote-agents", self.calls())

    def test_mosh_fallback_lands_on_ssh_with_the_same_command(self):
        mosh_path = os.path.join(self.bindir, "mosh")
        with open(mosh_path, "w") as f:
            f.write("#!/bin/sh\nexit 17\n")     # mosh dies, launcher must fall back
        os.chmod(mosh_path, 0o755)
        p = self.run_launcher(["attach", "w15"])
        self.assertEqual(p.returncode, 0)
        self.assertIn("ssh -t testhost remote-agents attach 'w15'", self.calls())

    def test_invalid_id_exits_two_without_connecting(self):
        p = self.run_launcher(["attach", "x;rm"])
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])
        self.assertIn("usage", p.stdout + p.stderr)

    def test_oversized_id_exits_two(self):
        p = self.run_launcher(["attach", "w" * 129])
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])
        p = self.run_launcher(["attach", "w" * 128])
        self.assertEqual(p.returncode, 0)

    def test_attach_without_alias_exits_one_with_usage(self):
        p = self.run_launcher(["attach", "w15"], alias=None)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.calls(), [])
        self.assertIn("usage", p.stdout + p.stderr)



class EnterLifecycle(unittest.TestCase):
    """remote-agents enter <token> and the exact-pane semantics of
    attach/resolve/focus, against a fully faked herdr client (the same
    fake-herdr harness as Dispatch: every herdr command is recorded)."""

    def setUp(self):
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.run_calls = []
        self.attached = []
        self.renders = []

    # ---- fakes -------------------------------------------------------------

    def snapshot_envelope(self, focused_pane_id="w15:p1"):
        return {"result": {"snapshot": {
            "workspaces": [
                {"workspace_id": "w15", "label": "tendril", "number": 3,
                 "focused": True, "active_tab_id": "w15:t1"},
            ],
            "panes": [
                {"pane_id": "w15:p1", "tab_id": "w15:t1",
                 "workspace_id": "w15", "agent": "pi",
                 "agent_status": "working",
                 "cwd": "/home/u/Projects/tendril",
                 "foreground_cwd": "/home/u/Projects/tendril",
                 "focused": True},
            ],
            "agents": [
                {"pane_id": "w15:p1", "tab_id": "w15:t1",
                 "workspace_id": "w15", "agent": "pi",
                 "agent_status": "working",
                 "cwd": "/home/u/Projects/tendril", "focused": False},
            ],
            "focused_pane_id": focused_pane_id,
            "focused_tab_id": "w15:t1",
        }}}

    def fake_run(self, args, focused_pane_id):
        self.run_calls.append(list(args))
        if args[1:] == ["api", "snapshot"]:
            return 0, json.dumps(self.snapshot_envelope(focused_pane_id)), ""
        if args[1:] == ["status"]:
            return 0, "status: running\nendpoint_compatible: yes\n", ""
        return 0, "", ""                                  # focus commands

    def run_main(self, argv, focused_pane_id="w15:p1", menu=None,
                 menu_return=0, lines=("q",)):
        """Run ra.main(argv) with the herdr client faked and the attach
        bridge recorded; menu=Mock asserts the selector contract directly,
        otherwise the real selector loop runs on patched line input."""
        stdin = mock.Mock()
        stdin.isatty.return_value = False
        ctx = [
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(ra, "HERDR", "herdr"),
            mock.patch.object(ra, "HOST_LABEL", "testhost"),
            mock.patch.object(ra, "run", side_effect=lambda args, timeout=6:
                              self.fake_run(args, focused_pane_id)),
            mock.patch.object(ra, "_attach_session",
                              side_effect=lambda: self.attached.append("attach")),
            mock.patch.object(ra, "save_last", lambda row: None),
            mock.patch.object(ra.sys, "stdin", stdin),
            mock.patch.object(ra.sys, "argv", ["remote-agents"] + argv),
            mock.patch.object(ra.sys, "stdout", self.out),
            mock.patch.object(ra.sys, "stderr", self.err),
        ]
        if menu is not None:
            ctx.append(mock.patch.object(ra, "menu_loop", menu))
        else:
            ctx += [
                mock.patch.object(ra, "render",
                                  side_effect=lambda rows, note, selected:
                                  self.renders.append(note)),
                mock.patch("builtins.input", side_effect=list(lines)),
            ]
        for c in ctx:
            c.start()
        try:
            return ra.main()
        finally:
            for c in reversed(ctx):
                c.stop()

    def focus_sequence(self):
        return [c[1:] for c in self.run_calls
                if len(c) >= 3 and c[1] in ("workspace", "tab", "agent")
                and c[2] == "focus"]

    # ---- enter with a pane target -------------------------------------------

    def test_enter_pane_focuses_exact_tab_and_pane_then_attaches_then_selector(self):
        rc = self.run_main(["enter", "w15:p1"])
        self.assertEqual(rc, 0)
        # exact focus: workspace -> tab -> pane, in that order
        self.assertEqual(self.focus_sequence(),
                         [["workspace", "focus", "w15"],
                          ["tab", "focus", "w15:t1"],
                          ["agent", "focus", "w15:p1"]])
        # the SAME Ctrl+Home-bridged attach the selector uses
        self.assertEqual(self.attached, ["attach"])
        # detach fell into the normal selector loop, once, and quit -> 0
        self.assertEqual(len(self.renders), 1)
        self.assertEqual(self.renders[0], "")    # no stale notice on success

    def test_enter_quit_returns_zero_after_detach(self):
        rc = self.run_main(["enter", "w15:p1"], lines=("q",))
        self.assertEqual(rc, 0)

    def test_enter_verification_failure_never_attaches_and_shows_the_notice(self):
        # the snapshot says a DIFFERENT pane is focused: never attach
        menu = mock.Mock(return_value=0)
        rc = self.run_main(["enter", "w15:p1"],
                           focused_pane_id="w15:t1-fake", menu=menu)
        self.assertEqual(rc, 0)
        self.assertEqual(self.attached, [])
        self.assertEqual(self.focus_sequence(),
                         [["workspace", "focus", "w15"],
                          ["tab", "focus", "w15:t1"],
                          ["agent", "focus", "w15:p1"]])
        menu.assert_called_once()
        self.assertEqual(menu.call_args.kwargs["note"], ra.STALE_NOTE)
        self.assertEqual(menu.call_args.kwargs["preferred_id"], "w15")

    def test_enter_stale_pane_never_attaches_and_preselects_the_parent(self):
        menu = mock.Mock(return_value=0)
        rc = self.run_main(["enter", "w15:p99"], menu=menu)
        self.assertEqual(rc, 0)
        self.assertEqual(self.attached, [])
        self.assertEqual(self.focus_sequence(), [])   # nothing was focused
        menu.assert_called_once()
        self.assertEqual(menu.call_args.kwargs["note"], ra.STALE_NOTE)
        self.assertEqual(menu.call_args.kwargs["preferred_id"], "w15")

    def test_enter_stale_pane_without_a_parent_preselects_nothing(self):
        menu = mock.Mock(return_value=0)
        self.run_main(["enter", "w99:p1"], menu=menu)
        self.assertEqual(self.attached, [])
        menu.assert_called_once()
        self.assertEqual(menu.call_args.kwargs["note"], ra.STALE_NOTE)
        self.assertIsNone(menu.call_args.kwargs["preferred_id"])

    # ---- enter with a workspace token ----------------------------------------

    def test_enter_workspace_token_focuses_attaches_then_selector(self):
        rc = self.run_main(["enter", "w15"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.focus_sequence(),
                         [["workspace", "focus", "w15"]])   # workspace semantics
        self.assertEqual(self.attached, ["attach"])
        self.assertEqual(len(self.renders), 1)

    def test_enter_unknown_workspace_token_exits_two_without_selector(self):
        menu = mock.Mock(return_value=0)
        rc = self.run_main(["enter", "gone"], menu=menu)
        self.assertEqual(rc, 2)
        self.assertEqual(self.attached, [])
        menu.assert_not_called()
        self.assertIn("no session matches 'gone'", self.err.getvalue())

    # ---- attach: one-shot, exact when a pane ----------------------------------

    def test_attach_pane_token_is_one_shot_exact_focus_without_selector(self):
        menu = mock.Mock(return_value=0)
        rc = self.run_main(["attach", "w15:p1"], menu=menu)
        self.assertEqual(rc, 0)
        self.assertEqual(self.focus_sequence(),
                         [["workspace", "focus", "w15"],
                          ["tab", "focus", "w15:t1"],
                          ["agent", "focus", "w15:p1"]])
        self.assertEqual(self.attached, ["attach"])
        menu.assert_not_called()          # one-shot: no selector, returns to shell

    def test_attach_workspace_token_stays_the_plain_one_shot(self):
        menu = mock.Mock(return_value=0)
        rc = self.run_main(["attach", "w15"], menu=menu)
        self.assertEqual(rc, 0)
        self.assertEqual(self.focus_sequence(),
                         [["workspace", "focus", "w15"]])
        self.assertEqual(self.attached, ["attach"])
        menu.assert_not_called()

    def test_attach_stale_pane_exits_two_without_attaching(self):
        menu = mock.Mock(return_value=0)
        rc = self.run_main(["attach", "w15:p99"], menu=menu)
        self.assertEqual(rc, 2)
        self.assertEqual(self.attached, [])
        menu.assert_not_called()
        self.assertIn("no session matches 'w15:p99'", self.err.getvalue())

    # ---- resolve / focus with pane ids -----------------------------------------

    def test_resolve_pane_json_includes_pane_tab_and_agent(self):
        rc = self.run_main(["resolve", "w15:p1"])
        self.assertEqual(rc, 0)
        doc = json.loads(self.out.getvalue().strip())
        self.assertEqual(doc["workspace_id"], "w15")
        self.assertEqual(doc["pane_id"], "w15:p1")
        self.assertEqual(doc["tab_id"], "w15:t1")
        self.assertEqual(doc["agent"], "pi")
        self.assertEqual(doc["agents"],
                         [{"agent": "pi", "agent_status": "working",
                           "pane_id": "w15:p1"}])

    def test_resolve_pane_link_carries_the_pane(self):
        self.run_main(["resolve", "w15:p1"])
        doc = json.loads(self.out.getvalue().strip())
        self.assertEqual(tl.parse_uri(doc["link"]),
                         {"host": "testhost", "workspace_id": "w15",
                          "label": "tendril", "pane": "w15:p1"})

    def test_resolve_stale_pane_exits_two_on_the_not_found_path(self):
        rc = self.run_main(["resolve", "w15:p99"])
        self.assertEqual(rc, 2)
        self.assertIn("no session matches 'w15:p99'", self.err.getvalue())
        self.assertIn("closest:", self.err.getvalue())

    def test_focus_pane_verifies_exact_focus_then_prints_json(self):
        rc = self.run_main(["focus", "w15:p1"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.focus_sequence(),
                         [["workspace", "focus", "w15"],
                          ["tab", "focus", "w15:t1"],
                          ["agent", "focus", "w15:p1"]])
        doc = json.loads(self.out.getvalue().strip())
        self.assertEqual(doc["pane_id"], "w15:p1")
        self.assertEqual(doc["tab_id"], "w15:t1")

    def test_focus_pane_verification_failure_exits_two_without_json(self):
        rc = self.run_main(["focus", "w15:p1"],
                           focused_pane_id="w15:other")
        self.assertEqual(rc, 2)
        self.assertNotIn("{", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()
