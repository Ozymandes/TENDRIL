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


if __name__ == "__main__":
    unittest.main()
