"""LIVE MAP snapshot: `remote-agents snapshot` + phone launcher plumbing.

The document is the read-only mirror TENDRIL Link's map renders (schema v1,
documented in the app's Topology.kt). Contract points under test here:
- exactly the v1 fields, nothing else — never cwd/terminal titles/sessions;
- status vocabulary collapses to working/blocked/done/idle else "unknown";
- stdout carries the ONE JSON line or NOTHING (reason on stderr, exit 1);
- phone/tendril snapshot dials plain BatchMode SSH, never Mosh, no TTY.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "bin", "remote-agents")
LAUNCHER = os.path.join(REPO, "phone", "tendril")


def load_cli():
    loader = SourceFileLoader("remote_agents_snapshot", CLI)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ra = load_cli()

AGENTS = [
    {"pane_id": "w15:p1", "workspace_id": "w15", "tab_id": "w15:t1",
     "agent": "pi", "agent_status": "working", "focused": True,
     "cwd": "/home/u/Projects/tendril", "foreground_cwd": "/home/u/secret",
     "agent_session": "pi-s1", "terminal_title": "pi secret title"},
    {"pane_id": "w15:p2", "workspace_id": "w15", "tab_id": "w15:t2",
     "agent": "claude", "agent_status": "weird-status", "focused": False,
     "cwd": "/home/u/more-secret", "agent_session": "cl-s1",
     "terminal_title": "claude secret title"},
]

SNAPSHOT = {"result": {"snapshot": {
    "workspaces": [
        {"workspace_id": "w15", "label": "tendril", "number": 3,
         "focused": True, "active_tab_id": "w15:t1"},
        {"workspace_id": "wK", "label": "kindle-notes", "number": 9,
         "focused": False, "active_tab_id": "wK:t1",
         "agent_status": "idle", "agent_session": "ws-session",
         "terminal_title": "ws title", "cwd": "/home/u/ws-secret"},
    ],
    "panes": [
        {"pane_id": "w15:p1", "tab_id": "w15:t1",
         "cwd": "/home/u/pane-secret", "foreground_cwd": "/home/u/pane-secret",
         "agent": "pi", "agent_status": "working",
         "agent_session": "pane-session", "terminal_title": "pane title"},
    ],
    "agents": AGENTS,
    "focused_pane_id": "w15:p1",
    "focused_tab_id": "w15:t1",
}}}

DOC_KEYS = {"version", "host", "generated_at", "focused_workspace",
            "focused_pane", "workspaces"}
WS_KEYS = {"id", "label", "number", "focused", "agents"}
AGENT_KEYS = {"pane_id", "tab_id", "agent", "status", "focused"}


class SnapshotCommand(unittest.TestCase):
    """main(["snapshot"]) with the herdr client fully faked."""

    def setUp(self):
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.run_calls = []

    def fake_run(self, snapshot_envelope):
        def run(args, timeout=6):
            self.run_calls.append(list(args))
            if args[1:] == ["api", "snapshot"]:
                if snapshot_envelope is None:
                    return 1, "", "herdr: connection refused"
                return 0, json.dumps(snapshot_envelope), ""
            return 0, "status: running\nendpoint_compatible: yes\n", ""
        return run

    def main(self, argv, envelope=SNAPSHOT, herdr="herdr"):
        ctx = [
            mock.patch.object(ra, "HERDR", herdr),
            mock.patch.object(ra, "HOST_LABEL", "testhost"),
            mock.patch.object(ra, "run", self.fake_run(envelope)),
            mock.patch.object(ra.sys, "argv", ["remote-agents"] + argv),
            mock.patch.object(ra.sys, "stdout", self.out),
            mock.patch.object(ra.sys, "stderr", self.err),
        ]
        for c in ctx:
            c.start()
        try:
            with mock.patch.object(ra.time, "time", return_value=1760000000.4):
                return ra.main()
        finally:
            for c in reversed(ctx):
                c.stop()

    def doc(self):
        lines = self.out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1, "exactly ONE line of output")
        return json.loads(lines[0])

    def test_schema_v1_exact_shape(self):
        self.assertEqual(self.main(["snapshot"]), 0)
        doc = self.doc()
        self.assertEqual(set(doc), DOC_KEYS)
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["host"], "testhost")
        self.assertEqual(doc["generated_at"], 1760000000)
        self.assertIsInstance(doc["generated_at"], int)
        self.assertEqual(doc["focused_workspace"], "w15")
        self.assertEqual(doc["focused_pane"], "w15:p1")
        self.assertEqual(len(doc["workspaces"]), 2)
        for ws in doc["workspaces"]:
            self.assertEqual(set(ws), WS_KEYS)
            for a in ws["agents"]:
                self.assertEqual(set(a), AGENT_KEYS)

    def test_agent_fields_are_projected_and_status_vocabulary(self):
        self.assertEqual(self.main(["snapshot"]), 0)
        agents = self.doc()["workspaces"][0]["agents"]
        self.assertEqual(agents, [
            {"pane_id": "w15:p1", "tab_id": "w15:t1", "agent": "pi",
             "status": "working", "focused": True},
            {"pane_id": "w15:p2", "tab_id": "w15:t2", "agent": "claude",
             "status": "unknown", "focused": False},
        ])

    def test_never_leaks_cwd_titles_or_sessions(self):
        self.assertEqual(self.main(["snapshot"]), 0)
        text = self.out.getvalue()
        for needle in ("cwd", "terminal_title", "agent_session",
                       "/home/u", "secret", "title"):
            self.assertNotIn(needle, text)

    def test_workspaces_in_selector_order(self):
        self.assertEqual(self.main(["snapshot"]), 0)
        doc = self.doc()
        with mock.patch.object(ra, "run", self.fake_run(SNAPSHOT)):
            rows = ra.collect()
        self.assertEqual([w["id"] for w in doc["workspaces"]],
                         [r["id"] for r in rows])

    def test_herdr_down_exits_one_with_empty_stdout(self):
        self.assertEqual(self.main(["snapshot"], envelope=None), 1)
        self.assertEqual(self.out.getvalue(), "")
        self.assertIn("cannot reach the Herdr server", self.err.getvalue())

    def test_missing_herdr_client_exits_one_with_empty_stdout(self):
        self.assertEqual(self.main(["snapshot"], herdr=None), 1)
        self.assertEqual(self.out.getvalue(), "")
        self.assertIn("no compatible herdr", self.err.getvalue())

    def test_json_flag_is_accepted_and_identical(self):
        self.assertEqual(self.main(["snapshot", "--json"]), 0)
        first = self.out.getvalue()
        self.out.seek(0)
        self.out.truncate()
        self.assertEqual(self.main(["snapshot"]), 0)
        self.assertEqual(json.loads(first), json.loads(self.out.getvalue()))

    def test_extra_arg_is_a_usage_error(self):
        self.assertEqual(self.main(["snapshot", "w15"]), 2)
        self.assertIn("usage", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_snapshot_runs_headless_without_selector(self):
        # snapshot is a mode word, not a session-token position: it never
        # opens the selector, and the herdr snapshot call is what feeds it.
        self.assertEqual(self.main(["snapshot"]), 0)
        self.assertIn(["herdr", "api", "snapshot"], self.run_calls)
        self.assertEqual(self.doc()["version"], 1)


class SnapshotLauncher(unittest.TestCase):
    """phone/tendril snapshot end-to-end against a recording ssh stub."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril-snapshot-launcher-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)
        self.record = os.path.join(self.tmp, "calls.log")
        ssh = ("#!/bin/sh\n"
               "printf 'ssh %s\\n' \"$*\" >> \"$TENDRIL_RECORD\"\n"
               "echo 'SNAPSHOT-JSON-LINE'\n")
        with open(os.path.join(self.bindir, "ssh"), "w") as f:
            f.write(ssh)
        os.chmod(os.path.join(self.bindir, "ssh"), 0o755)

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

    def test_snapshot_dials_default_alias_over_plain_batchmode_ssh(self):
        p = self.run_launcher(["snapshot"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.calls(), [
            "ssh -o BatchMode=yes -o ConnectTimeout=6 testhost remote-agents snapshot",
        ])
        self.assertIn("SNAPSHOT-JSON-LINE", p.stdout)

    def test_explicit_alias_snapshot_respects_the_alias(self):
        p = self.run_launcher(["otherhost", "snapshot"], alias="unused")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.calls(), [
            "ssh -o BatchMode=yes -o ConnectTimeout=6 otherhost remote-agents snapshot",
        ])
        self.assertNotIn("unused", " ".join(self.calls()))

    def test_snapshot_never_uses_mosh_or_a_tty(self):
        self.run_launcher(["snapshot"])
        joined = " ".join(self.calls())
        self.assertNotIn("mosh", joined)
        self.assertNotIn("-t", joined.split("ssh ")[1].split(" testhost")[0])

    def test_snapshot_without_alias_fails_fast(self):
        p = self.run_launcher(["snapshot"], alias=None)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.calls(), [])
        self.assertIn("usage", p.stdout + p.stderr)

    def test_snapshot_rejects_dash_alias_without_connecting(self):
        p = self.run_launcher(["-x", "snapshot"])
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.calls(), [])
        self.assertIn("not an SSH alias", p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
