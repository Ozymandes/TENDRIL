"""LIVE MAP snapshot: `remote-agents snapshot` + phone launcher plumbing.

The document is the read-only mirror TENDRIL Link's map renders (schema v3,
documented in the app's Topology.kt). Contract points under test here:
- the v1 fields unchanged (never cwd/terminal titles/sessions), plus the
  v2 `harness` tag, and the v3 top-level `capabilities` marker;
- status vocabulary collapses to working/blocked/done/idle else "unknown";
- stdout carries the ONE JSON line or NOTHING (reason on stderr, exit 1);
- phone/tendril snapshot dials plain BatchMode SSH, never Mosh, no TTY;
- the whole wire document stays ≤ 64 KB (contract §0) and the optional
  capacity/schedule sections follow the hook tuple contract.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import types
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
            "focused_pane", "workspaces", "capabilities", "health"}
WS_KEYS = {"id", "label", "number", "focused", "agents"}
AGENT_KEYS = {"pane_id", "tab_id", "agent", "status", "focused",
              "harness", "prompt_ready"}


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
        if ra.tendril_telemetry is not None:
            # keep the telemetry cache out of the developer's real ~/.cache
            ctx.append(mock.patch.object(ra.tendril_telemetry, "CACHE_FILE",
                                         os.path.join(tempfile.mkdtemp(
                                             prefix="tendril-snap-cache-"),
                                             "telemetry-v2.json")))
        # the sibling capacity/schedule hooks are other modules' territory:
        # pin them ABSENT (None makes `import` raise) so these tests assert
        # the exact base document regardless of what is installed.
        ctx.append(mock.patch.dict(sys.modules,
                                   {"tendril_capacity": None,
                                    "tendril_schedule": None}))
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

    def test_schema_v3_exact_shape(self):
        self.assertEqual(self.main(["snapshot"]), 0)
        doc = self.doc()
        self.assertEqual(set(doc), DOC_KEYS)
        self.assertEqual(doc["version"], 3)
        self.assertIsInstance(doc["capabilities"], list)
        self.assertIn("subagents", doc["capabilities"])
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
             "status": "working", "focused": True, "harness": "pi",
             "prompt_ready": False},
            {"pane_id": "w15:p2", "tab_id": "w15:t2", "agent": "claude",
             "status": "unknown", "focused": False, "harness": "claude",
             "prompt_ready": False},
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
        self.assertEqual(self.doc()["version"], 3)


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


class DocCap(unittest.TestCase):
    """Contract §0: the whole wire document stays ≤ 64 KB, and shrinking it
    never touches the v1 agent/workspace fields."""

    V1 = ("pane_id", "tab_id", "agent", "status", "focused", "harness")

    def build(self, n):
        ws = {"id": "w1", "label": "lab", "number": 1, "focused": True,
              "agents": []}
        for i in range(n):
            ws["agents"].append({
                "pane_id": "w1:p%d" % i, "tab_id": "w1:t%d" % i,
                "agent": "pi", "status": "working", "focused": i == 0,
                "harness": "pi",
                "model": "m" * 40, "provider": "p" * 40, "effort": "high",
                "started_at": 1, "context_tokens": 1, "output_tokens": 1,
                "cost_usd": 0.0, "tool": "bash", "tool_started_at": 2,
                "activity": "a" * 3000, "trail": "PRTPRTPRTPRT",
                "phase": "thinking",
                "last_event": {"kind": "reply", "at": 3},
                "events": [{"kind": "reply", "at": t} for t in range(12)],
                "children": [{"id": "%08x" % j, "kind": "subagent",
                              "harness": "pi", "status": "done",
                              "name": "worker"} for j in range(8)],
            })
        return {"version": 3, "host": "h", "generated_at": 1,
                "focused_workspace": "w1", "focused_pane": "w1:p0",
                "workspaces": [ws], "capabilities": ["subagents"]}

    def test_oversized_document_is_capped_and_keeps_v1_fields(self):
        doc = self.build(40)
        self.assertGreater(ra._doc_bytes(doc), ra._SNAPSHOT_MAX_BYTES)
        ra._enforce_doc_cap(doc)
        self.assertLessEqual(ra._doc_bytes(doc), ra._SNAPSHOT_MAX_BYTES)
        for key in ("version", "host", "generated_at", "focused_workspace",
                    "focused_pane", "workspaces", "capabilities"):
            self.assertIn(key, doc)
        for ws in doc["workspaces"]:
            self.assertTrue(WS_KEYS <= set(ws))
            for a in ws["agents"]:
                for k in self.V1:
                    self.assertIn(k, a)

    def test_document_under_the_cap_is_untouched(self):
        doc = self.build(2)
        before = json.dumps(doc, sort_keys=True)
        ra._enforce_doc_cap(doc)
        self.assertEqual(json.dumps(doc, sort_keys=True), before)


def _fake_module(name, fn):
    m = types.ModuleType(name)
    m.snapshot_section = fn
    return m


class PromptReady(unittest.TestCase):
    """§7.3: prompt_ready = launchable harness + agent present + idle/done
    (host-computed; the phone derives Busy / Needs input / Not supported
    from status itself)."""

    def test_table(self):
        f = ra._prompt_ready
        self.assertTrue(f("idle", "pi"))
        self.assertTrue(f("done", "claude"))
        self.assertFalse(f("working", "pi"))       # busy
        self.assertFalse(f("blocked", "pi"))       # needs input
        self.assertFalse(f("unknown", "pi"))
        self.assertFalse(f("idle", "tcsh"))        # not a launchable harness


class OptionalSections(unittest.TestCase):
    """Contract §1 hook signature: tendril_capacity.snapshot_section(
    budget_s) and tendril_schedule.snapshot_section() return
    (section_dict | None, capability_string). Absence, failure or an empty
    section omits BOTH the section and the capability."""

    def call(self, cap="absent", sched="absent", health="absent"):
        # NOTE: sibling modules may be importable depending on ambient
        # sys.path (other test files insert REPO/bin), so every case pins
        # ALL module slots explicitly: None makes `import` raise.
        mods = {}
        for key, val in (("tendril_capacity", cap), ("tendril_schedule",
                                                     sched),
                         ("tendril_health", health)):
            if val == "absent" or val is None:
                mods[key] = None            # `import` raises ImportError
            else:
                mods[key] = _fake_module(key, val)
        with mock.patch.dict(sys.modules, mods):
            return ra._optional_sections()

    def test_absent_hooks_omit_sections_and_capabilities(self):
        self.assertEqual(self.call(), ({}, []))
        self.assertEqual(self.call(sched=None), ({}, []))
        self.assertEqual(self.call(cap=None), ({}, []))

    def test_tuple_contract_registers_section_and_capability(self):
        sec = {"generated_at": 1, "providers": []}
        extra, caps = self.call(cap=lambda budget_s=1.0: (sec, "capacity"))
        self.assertEqual(extra, {"capacity": sec})
        self.assertEqual(caps, ["capacity"])

    def test_none_section_registers_nothing(self):
        self.assertEqual(self.call(cap=lambda budget_s=1.0: (None,
                                                             "capacity")),
                         ({}, []))

    def test_empty_section_registers_no_capability(self):
        self.assertEqual(self.call(cap=lambda budget_s=1.0: ({}, "capacity")),
                         ({}, []))

    def test_bare_dict_section_is_tolerated(self):
        sec = {"generated_at": 2, "providers": [{"id": "claude"}]}
        extra, caps = self.call(cap=lambda budget_s=1.0: sec)
        self.assertEqual(extra, {"capacity": sec})
        self.assertEqual(caps, ["capacity"])

    def test_hook_exception_is_swallown_and_does_not_block_the_sibling(self):
        def boom(budget_s=1.0):
            raise RuntimeError("boom")
        sec = {"available": True, "tasks": []}
        extra, caps = self.call(cap=boom,
                                sched=lambda snap=None: (sec, "schedule"))
        self.assertEqual(extra, {"schedule": sec})
        self.assertEqual(caps, ["schedule"])

    def test_snapshot_doc_embeds_hook_sections_and_capabilities(self):
        cap = ({"generated_at": 7, "providers": [{"id": "claude"}]},
               "capacity")
        sched = ({"available": True, "tasks": []}, "schedule")
        health = ({"available": True}, "health")

        def fake_run(args, timeout=6):
            if args[1:] == ["api", "snapshot"]:
                return 0, json.dumps(SNAPSHOT), ""
            return 0, "status: running\n", ""

        with mock.patch.dict(sys.modules, {
                "tendril_capacity": _fake_module("tendril_capacity",
                                                 lambda budget_s=1.0: cap),
                "tendril_schedule": _fake_module("tendril_schedule",
                                                 lambda snap=None: sched),
                "tendril_health": _fake_module("tendril_health",
                                               lambda: health)}), \
             mock.patch.object(ra, "HERDR", "herdr"), \
             mock.patch.object(ra, "HOST_LABEL", "testhost"), \
             mock.patch.object(ra, "run", fake_run):
            doc, code = ra._snapshot_doc()
        self.assertEqual(code, 0)
        self.assertEqual(doc["capacity"]["generated_at"], 7)
        self.assertEqual(doc["schedule"], {"available": True, "tasks": []})
        self.assertEqual(doc["health"], {"available": True})
        self.assertEqual(doc["capabilities"],
                         ["subagents", "capacity", "schedule", "health"])
        self.assertLessEqual(ra._doc_bytes(doc), ra._SNAPSHOT_MAX_BYTES)

    def test_hook_failure_omits_the_section_from_the_doc(self):
        def boom(budget_s=1.0):
            raise RuntimeError("boom")

        def fake_run(args, timeout=6):
            if args[1:] == ["api", "snapshot"]:
                return 0, json.dumps(SNAPSHOT), ""
            return 0, "status: running\n", ""

        with mock.patch.dict(sys.modules, {
                "tendril_capacity": _fake_module("tendril_capacity", boom),
                "tendril_schedule": None,
                "tendril_health": None}), \
             mock.patch.object(ra, "HERDR", "herdr"), \
             mock.patch.object(ra, "HOST_LABEL", "testhost"), \
             mock.patch.object(ra, "run", fake_run):
            doc, code = ra._snapshot_doc()
        self.assertEqual(code, 0)
        self.assertNotIn("capacity", doc)
        self.assertNotIn("schedule", doc)
        self.assertNotIn("health", doc)
        self.assertEqual(doc["capabilities"], ["subagents"])


if __name__ == "__main__":
    unittest.main()
