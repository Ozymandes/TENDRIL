"""Schema v3 telemetry: tendril_telemetry + its wiring into the snapshot.

Contract points under test (TENDRIL Link Topology.kt, AgentTelemetry):
- per-agent facts derived READ-ONLY from pi / claude transcripts; codex and
  unknown harnesses get none;
- NEVER fabricate: a field that cannot be derived reliably is omitted;
- incremental parsing (cache offset advances; appended bytes only), with
  truncation resetting the accumulators;
- over-long (base64 image) lines are mined with bounded windows, fast, and
  still yield the tool name;
- the wall-clock budget skips whole agents, never breaks the snapshot;
- safety gate: pi paths must realpath under $HOME and end .jsonl, claude
  ids must match the uuid shape before globbing;
- privacy: prompt text, command lines and directories never reach the wire
  — only tool names and sanitized file basenames inside `activity`;
- v3 subagent children (pi same-dir/parentSession + pi-subagents run dirs
  + claude sidechains), with meta files whitelist-projected so prompt text
  (the `task` decoy) can never reach the cache or the wire;
- v3 phase classification: an open bash call yields at most the phase word
  ("testing"), never the command; events are ≤ 12, newest last.
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "bin", "remote-agents")
TELE = os.path.join(REPO, "bin", "tendril_telemetry.py")


def _load(name, path):
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


tt = _load("tendril_telemetry_test", TELE)
ra = _load("remote_agents_telemetry_test", CLI)

NOW = 1760000000
UUID_OK = "11111111-2222-4333-8444-555555555555"
UUID_OK2 = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000Z"


def dumps(obj):
    return json.dumps(obj, separators=(",", ":"))


class Base(unittest.TestCase):
    """One fake $HOME with a scratch cache path."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tendril-tel-home-")
        self.cache = os.path.join(self.home, ".cache", "telemetry-v1.json")
        self.addCleanup(shutil.rmtree, self.home, True)

    def write_jsonl(self, path, lines):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("".join(dumps(x) + "\n" for x in lines))
        return path

    def agent(self, harness, session, status="working"):
        return {"agent": harness, "agent_status": status,
                "agent_session": session}

    def agent_sess(self, uuid=UUID_OK):
        return {"agent": "claude", "kind": "id", "source": "herdr:claude",
                "value": uuid}

    def run_tele(self, agents, now=NOW, **kw):
        return tt.for_agents(agents, home=self.home,
                             cache_path=self.cache, now=now, **kw)

    def cache_entry(self, path):
        with open(self.cache) as f:
            return json.load(f)[os.path.realpath(path)]


# ---------- fixtures ----------
def pi_session_line(ts, cwd="/home/u/SECRET_DIR"):
    return {"type": "session", "version": 9, "id": "s1",
            "timestamp": iso(ts), "cwd": cwd}


def pi_model_change(ts, model="glm-test-5", provider="zai"):
    return {"type": "model_change", "id": "m1", "parentId": None,
            "timestamp": iso(ts), "provider": provider, "modelId": model}


def pi_thinking(ts, level="high"):
    return {"type": "thinking_level_change", "id": "t1", "parentId": "m1",
            "timestamp": iso(ts), "thinkingLevel": level}


def pi_user(ts, text):
    return {"type": "message", "id": "u1", "parentId": "m1",
            "timestamp": iso(ts),
            "message": {"role": "user", "timestamp": ts * 1000,
                        "content": [{"type": "text", "text": text}]}}


def pi_assistant(ts, usage, content, model="glm-test-5", provider="zai",
                 level="high"):
    return {"type": "message", "id": "a%d" % ts, "parentId": "u1",
            "timestamp": iso(ts),
            "message": {"role": "assistant", "timestamp": ts * 1000,
                        "model": model, "provider": provider,
                        "thinkingLevel": level, "usage": usage,
                        "content": content}}


def pi_usage(inp, out, cr, cw, total):
    return {"input": inp, "output": out, "cacheRead": cr, "cacheWrite": cw,
            "reasoning": 0, "totalTokens": inp + out,
            "cost": {"input": 0.0, "output": 0.0, "cacheRead": 0.0,
                     "cacheWrite": 0.0, "total": total}}


def pi_tool_call(id_, name, ts, args=None):
    return {"type": "toolCall", "id": id_, "name": name,
            "arguments": args if args is not None else {}}


def pi_tool_result(ts, id_, name, error=False, text="SECRET-RESULT"):
    return {"type": "message", "id": "r%d" % ts, "parentId": "a",
            "timestamp": iso(ts),
            "message": {"role": "toolResult", "toolCallId": id_,
                        "toolName": name, "isError": error,
                        "timestamp": ts * 1000,
                        "content": [{"type": "text", "text": text}]}}


def cl_assistant(ts, model="claude-test-5", usage=None, content=None,
                 effort="high"):
    return {"parentUuid": "x", "isSidechain": False, "type": "assistant",
            "timestamp": iso(ts), "effort": effort, "cwd": "/home/u/SECRET_DIR",
            "message": {"role": "assistant", "model": model,
                        "content": content or [{"type": "text", "text": "hi"}],
                        "usage": usage or {}}}


def cl_tool_use(ts, id_, name, args):
    return cl_assistant(ts, content=[
        {"type": "tool_use", "id": id_, "name": name, "input": args}])


def cl_user_tool_result(ts, id_, error=False):
    return {"parentUuid": "x", "isSidechain": False, "type": "user",
            "timestamp": iso(ts), "cwd": "/home/u/SECRET_DIR",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": id_,
                 "is_error": error, "content": "SECRET-OUTPUT"}]}}


def cl_user_prompt(ts, text="SECRET-PROMPT hello"):
    return {"parentUuid": None, "isSidechain": False, "type": "user",
            "timestamp": iso(ts), "cwd": "/home/u/SECRET_DIR",
            "message": {"role": "user", "content": text}}


def cl_cost_state(ts, total):
    return {"type": "cost-state", "sessionId": UUID_OK,
            "totalCostUSD": total, "totalDuration": 1}


def cl_session_dir(home, uuid):
    d = os.path.join(home, ".claude", "projects", "-home-u-SECRET")
    return d, os.path.join(d, uuid + ".jsonl")


# ---------- pi ----------
class PiSession(Base):
    def session(self):
        return self.write_jsonl(os.path.join(
            self.home, ".pi", "agent", "sessions", "proj", "s1.jsonl"), [
            pi_session_line(NOW - 3600),
            pi_model_change(NOW - 3599),
            pi_thinking(NOW - 3598),
            pi_user(NOW - 3600, "SECRET-PROMPT fix the thing"),
            pi_assistant(NOW - 3500,
                         pi_usage(1000, 50, 900, 100, 0.0123),
                         [pi_tool_call("t1", "edit", NOW - 3500,
                                       {"path": "/home/u/SECRET_DIR/"
                                               "My App.link/Screen.kt",
                                        "content": "SECRET-CONTENT"})]),
            pi_tool_result(NOW - 3400, "t1", "edit"),
            pi_assistant(NOW - 3300, pi_usage(2100, 70, 1900, 0, 0.0221),
                         [{"type": "text", "text": "SECRET-REPLY done"}]),
        ])

    def test_full_derivation(self):
        path = self.session()
        res = self.run_tele([self.agent("pi", {
            "agent": "pi", "kind": "path", "source": "herdr:pi",
            "value": path})])[0]
        self.assertEqual(res["harness"], "pi")
        self.assertEqual(res["model"], "glm-test-5")
        self.assertEqual(res["provider"], "zai")
        self.assertEqual(res["effort"], "high")
        self.assertEqual(res["started_at"], NOW - 3600)
        self.assertEqual(res["context_tokens"], 2100 + 1900)      # latest turn
        self.assertEqual(res["output_tokens"], 50 + 70)           # cumulative
        self.assertEqual(res["cost_usd"], round(0.0123 + 0.0221, 4))
        self.assertEqual(res["last_event"],
                         {"kind": "reply", "at": NOW - 3300})
        self.assertEqual(res["trail"], "PTR")                     # P, tool ok, R
        self.assertEqual(res["activity"], "Thinking")             # working
        self.assertNotIn("tool", res)

    def test_open_tool_and_activity(self):
        path = self.write_jsonl(os.path.join(
            self.home, ".pi", "agent", "sessions", "proj", "s2.jsonl"), [
            pi_session_line(NOW - 120),
            pi_user(NOW - 120, "SECRET-PROMPT go"),
            pi_assistant(NOW - 60, pi_usage(10, 5, 0, 0, 0.0),
                         [pi_tool_call("t9", "edit", NOW - 60,
                                       {"path": "/home/u/SECRET_DIR/x/y.kt"})]),
        ])
        res = self.run_tele([self.agent("pi", {
            "agent": "pi", "kind": "path", "source": "herdr:pi",
            "value": path})])[0]
        self.assertEqual(res["tool"], "edit")
        self.assertEqual(res["tool_started_at"], NOW - 60)
        self.assertEqual(res["activity"], "Editing y.kt")
        self.assertEqual(res["last_event"], {"kind": "prompt",
                                             "at": NOW - 120})
        self.assertEqual(res["trail"], "P")

    def test_stale_open_tool_is_omitted(self):
        path = self.write_jsonl(os.path.join(
            self.home, ".pi", "agent", "sessions", "proj", "s3.jsonl"), [
            pi_session_line(NOW - 7200),
            pi_user(NOW - 7200, "prompt"),
            pi_assistant(NOW - 7000, pi_usage(10, 5, 0, 0, 0.0),
                         [pi_tool_call("t1", "bash", NOW - 7000)]),
        ])
        res = self.run_tele([self.agent("pi", {
            "agent": "pi", "kind": "path", "source": "herdr:pi",
            "value": path})], now=NOW)[0]
        self.assertNotIn("tool", res)
        self.assertNotIn("tool_started_at", res)
        self.assertEqual(res["activity"], "Thinking")

    def test_status_vocabulary_drives_activity(self):
        base = [pi_session_line(NOW - 30),
                pi_user(NOW - 30, "prompt")]
        path = self.write_jsonl(os.path.join(
            self.home, ".pi", "agent", "sessions", "proj", "s4.jsonl"), base)
        sess = {"agent": "pi", "kind": "path", "source": "herdr:pi",
                "value": path}
        for status, phrase in (("blocked", "Waiting for input"),
                               ("idle", "Idle"), ("done", "Finished"),
                               ("working", "Thinking")):
            res = self.run_tele([self.agent("pi", sess, status=status)])[0]
            self.assertEqual(res["activity"], phrase, status)


# ---------- claude ----------
class ClaudeSession(Base):
    def session(self, uuid=UUID_OK):
        _d, path = cl_session_dir(self.home, uuid)
        return self.write_jsonl(path, [
            cl_user_prompt(NOW - 1800),
            cl_tool_use(NOW - 1500, "toolu_1", "Bash",
                        {"command": "curl https://SECRET-HOST.example",
                         "path": "/home/u/SECRET_DIR/x.py"}),
            cl_user_tool_result(NOW - 1400, "toolu_1"),
            cl_assistant(NOW - 1300, usage={
                "input_tokens": 100, "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 30, "output_tokens": 500}),
            cl_cost_state(NOW - 1200, 0.58471),
        ])

    def agent_sess(self, uuid=UUID_OK):
        return {"agent": "claude", "kind": "id", "source": "herdr:claude",
                "value": uuid}

    def test_full_derivation(self):
        self.session()
        res = self.run_tele([self.agent("claude", self.agent_sess())])[0]
        self.assertEqual(res["harness"], "claude")
        self.assertEqual(res["model"], "claude-test-5")
        self.assertEqual(res["effort"], "high")          # recorded, not guessed
        self.assertEqual(res["started_at"], NOW - 1800)
        self.assertEqual(res["context_tokens"], 100 + 20 + 30)
        self.assertEqual(res["output_tokens"], 500)
        self.assertEqual(res["cost_usd"], 0.5847)        # latest cost-state
        self.assertEqual(res["last_event"],
                         {"kind": "reply", "at": NOW - 1300})
        self.assertEqual(res["trail"], "PTR")
        self.assertEqual(res["activity"], "Thinking")
        # §7.1: the vendor is canonical from the harness (Claude Code's
        # only upstream), billing metered (no OAuth credential in the
        # temp home) — a real totalCostUSD exists.
        self.assertEqual(res["provider"], "anthropic")
        self.assertEqual(res["billing"], "metered")
        self.assertNotIn("adapter", res)

    def test_error_tool_result_trails_E(self):
        _d, path = cl_session_dir(self.home, UUID_OK2)
        self.write_jsonl(path, [
            cl_user_prompt(NOW - 60),
            cl_tool_use(NOW - 50, "toolu_e", "Edit", {"path": "keep.kt"}),
            cl_user_tool_result(NOW - 40, "toolu_e", error=True),
        ])
        res = self.run_tele([self.agent("claude",
                                        self.agent_sess(UUID_OK2))])[0]
        self.assertEqual(res["trail"], "PE")
        self.assertEqual(res["last_event"],
                         {"kind": "tool_result", "tool": "Edit",
                          "error": True, "at": NOW - 40})

    def test_open_tool_pending_result(self):
        _d, path = cl_session_dir(self.home, UUID_OK2)
        self.write_jsonl(path, [
            cl_user_prompt(NOW - 30),
            cl_tool_use(NOW - 20, "toolu_o", "Read", {"file_path": "a b.txt"}),
        ])
        res = self.run_tele([self.agent("claude",
                                        self.agent_sess(UUID_OK2))])[0]
        self.assertEqual(res["tool"], "Read")
        self.assertEqual(res["activity"], "Reading a_b.txt")


# ---------- activity phrases ----------
class ActivityPhrases(Base):
    def phrase(self, name, base=None):
        return tt._tool_phrase(name, base)

    def test_verb_table(self):
        self.assertEqual(self.phrase("edit", "s.sh"), "Editing s.sh")
        self.assertEqual(self.phrase("Write", None), "Editing files")
        self.assertEqual(self.phrase("apply_patch", None), "Editing files")
        self.assertEqual(self.phrase("NotebookEdit", "n.ipynb"),
                         "Editing n.ipynb")
        self.assertEqual(self.phrase("read", "a.py"), "Reading a.py")
        self.assertEqual(self.phrase("Read", None), "Reading files")
        self.assertEqual(self.phrase("bash", None), "Running a command")
        self.assertEqual(self.phrase("Bash", None), "Running a command")
        self.assertEqual(self.phrase("grep", None), "Searching the repository")
        self.assertEqual(self.phrase("Glob", None), "Searching the repository")
        self.assertEqual(self.phrase("ls", None), "Searching the repository")
        self.assertEqual(self.phrase("web_search", None),
                         "Researching the web")
        self.assertEqual(self.phrase("WebFetch", None), "Researching the web")
        self.assertEqual(self.phrase("Task", None), "Delegating to a subagent")
        self.assertEqual(self.phrase("subagent_worker", None),
                         "Delegating to a subagent")
        self.assertEqual(self.phrase("TodoWrite", None), "Using TodoWrite")

    def test_basename_is_sanitized_and_capped(self):
        self.assertEqual(tt._sanitize_base("My App (v2).kt"), "My_App__v2_.kt")
        self.assertEqual(len(tt._sanitize_base("x" * 99)), 40)
        self.assertIsNone(tt._sanitize_base("///"))

    def test_unknown_status_omits_activity(self):
        path = self.write_jsonl(os.path.join(
            self.home, ".pi", "agent", "sessions", "proj", "s9.jsonl"),
            [pi_session_line(NOW - 10)])
        res = self.run_tele([self.agent("pi", {
            "agent": "pi", "kind": "path", "source": "herdr:pi",
            "value": path}, status="weird")])[0]
        self.assertNotIn("activity", res)


# ---------- incremental parsing ----------
class Incremental(Base):
    def setUp(self):
        super().setUp()
        self.path = self.write_jsonl(os.path.join(
            self.home, ".pi", "agent", "sessions", "proj", "inc.jsonl"), [
            pi_session_line(NOW - 100),
            pi_user(NOW - 100, "prompt"),
            pi_assistant(NOW - 90, pi_usage(10, 5, 0, 0, 0.1),
                         [{"type": "text", "text": "part1"}]),
        ])
        self.sess = {"agent": "pi", "kind": "path", "source": "herdr:pi",
                     "value": self.path}

    def append(self, lines):
        with open(self.path, "a") as f:
            f.write("".join(dumps(x) + "\n" for x in lines))

    def test_offset_advances_and_only_new_bytes_are_parsed(self):
        first = self.run_tele([self.agent("pi", self.sess)])[0]
        entry = self.cache_entry(self.path)
        self.assertEqual(entry["offset"], os.path.getsize(self.path))
        self.assertEqual(first["output_tokens"], 5)

        real_loads = tt.json.loads
        count = {"n": 0}

        def counting(s, *a, **kw):
            count["n"] += 1
            return real_loads(s, *a, **kw)

        self.append([
            pi_model_change(NOW - 10, model="glm-next"),
            pi_assistant(NOW - 5, pi_usage(20, 7, 0, 0, 0.2),
                         [{"type": "text", "text": "part2"}],
                         model="glm-next"),
        ])
        count["n"] = 0
        with mock.patch.object(tt.json, "loads", counting):
            second = self.run_tele([self.agent("pi", self.sess)])[0]
        # exactly the appended work: cache load + model_change + assistant
        self.assertEqual(count["n"], 3, "only appended bytes may be parsed")
        entry = self.cache_entry(self.path)
        self.assertEqual(entry["offset"], os.path.getsize(self.path))
        self.assertEqual(second["output_tokens"], 5 + 7)
        self.assertEqual(second["cost_usd"], 0.3)
        self.assertEqual(second["model"], "glm-next")
        self.assertEqual(second["started_at"], NOW - 100, "not re-derived")
        self.assertEqual(second["trail"], "PRR")

    def test_partial_final_line_is_not_consumed(self):
        with open(self.path, "a") as f:
            f.write(dumps(pi_assistant(NOW - 1, pi_usage(1, 1, 0, 0, 0.0),
                                       [{"type": "text", "text": "cut"}]))[:20])
        self.run_tele([self.agent("pi", self.sess)])
        entry = self.cache_entry(self.path)
        self.assertEqual(entry["offset"], os.path.getsize(self.path) - 20)
        self.append([])
        self.run_tele([self.agent("pi", self.sess)])  # stays put, no crash

    def test_truncation_resets_accumulators(self):
        self.run_tele([self.agent("pi", self.sess)])
        self.write_jsonl(self.path, [
            pi_session_line(NOW - 3),
            pi_assistant(NOW - 2, pi_usage(4, 2, 0, 0, 0.5),
                         [{"type": "text", "text": "fresh"}]),
        ])
        res = self.run_tele([self.agent("pi", self.sess)])[0]
        self.assertEqual(res["started_at"], NOW - 3)
        self.assertEqual(res["output_tokens"], 2)
        self.assertEqual(res["cost_usd"], 0.5)
        self.assertEqual(res["trail"], "R")


# ---------- over-long lines ----------
class HugeLines(Base):
    def test_pi_image_tool_result_is_mined_not_parsed(self):
        blob = {"type": "text", "text": "SECRET-BASE64 " + "A" * (2 << 20)}
        path = self.write_jsonl(os.path.join(
            self.home, ".pi", "agent", "sessions", "proj", "big.jsonl"), [
            pi_session_line(NOW - 60),
            pi_user(NOW - 60, "prompt"),
            pi_assistant(NOW - 50, pi_usage(10, 5, 0, 0, 0.0),
                         [pi_tool_call("t1", "read", NOW - 50,
                                       {"path": "/home/u/SECRET_DIR/pic.png"})]),
            {"type": "message", "id": "r", "parentId": "a",
             "timestamp": iso(NOW - 40),
             "message": {"role": "toolResult", "toolCallId": "t1",
                         "toolName": "read", "isError": True,
                         "timestamp": NOW * 1000 - 40000,
                         "content": [blob]}},
        ])
        t0 = time.monotonic()
        res = self.run_tele([self.agent("pi", {
            "agent": "pi", "kind": "path", "source": "herdr:pi",
            "value": path})])[0]
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 5.0)
        self.assertGreater(os.path.getsize(path), tt.HUGE_LINE)
        self.assertEqual(res["trail"], "PE")
        self.assertEqual(res["last_event"],
                         {"kind": "tool_result", "tool": "read",
                          "error": True, "at": NOW - 40})

    def test_pi_huge_assistant_usage_window(self):
        huge_text = {"type": "text", "text": "B" * (2 << 20)}
        path = self.write_jsonl(os.path.join(
            self.home, ".pi", "agent", "sessions", "proj", "big2.jsonl"), [
            pi_session_line(NOW - 60),
            pi_user(NOW - 60, "prompt"),
            pi_assistant(NOW - 50, pi_usage(111, 222, 33, 44, 0.9),
                         [huge_text]),
        ])
        res = self.run_tele([self.agent("pi", {
            "agent": "pi", "kind": "path", "source": "herdr:pi",
            "value": path})])[0]
        self.assertEqual(res["output_tokens"], 222)
        self.assertEqual(res["context_tokens"], 111 + 33 + 44)
        self.assertEqual(res["cost_usd"], 0.9)
        self.assertEqual(res["trail"], "PR")

    def test_claude_huge_tool_result_closes_the_call(self):
        _d, path = cl_session_dir(self.home, UUID_OK2)
        blob = "C" * (2 << 20)
        self.write_jsonl(path, [
            cl_user_prompt(NOW - 60),
            cl_tool_use(NOW - 50, "toolu_h", "Bash", {"command": "SECRET"}),
            {"parentUuid": "x", "isSidechain": False, "type": "user",
             "timestamp": iso(NOW - 40), "cwd": "/home/u/SECRET_DIR",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "toolu_h",
                  "is_error": False,
                  "content": [{"type": "image", "data": blob}]}]}},
        ])
        t0 = time.monotonic()
        res = self.run_tele([self.agent("claude",
                                        self.agent_sess(UUID_OK2))])[0]
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertEqual(res["trail"], "PT")
        self.assertEqual(res["last_event"],
                         {"kind": "tool_result", "error": False,
                          "at": NOW - 40})


# ---------- time budget ----------
class TimeBudget(Base):
    def test_budget_skips_whole_agents_and_keeps_their_cache_back(self):
        p1 = self.write_jsonl(os.path.join(
            self.home, ".pi", "agent", "sessions", "proj", "b1.jsonl"),
            [pi_session_line(NOW - 10), pi_user(NOW - 10, "one")])
        p2 = self.write_jsonl(os.path.join(
            self.home, ".pi", "agent", "sessions", "proj", "b2.jsonl"),
            [pi_session_line(NOW - 10), pi_user(NOW - 10, "two")])
        agents = [self.agent("pi", {"agent": "pi", "kind": "path",
                                    "source": "herdr:pi", "value": p1}),
                  self.agent("pi", {"agent": "pi", "kind": "path",
                                    "source": "herdr:pi", "value": p2})]
        clock = iter([0.0, 0.0, 99.0])          # start, agent1 ok, agent2 late

        def fake_mono():
            return next(clock, 99.0)

        with mock.patch.object(tt, "_mono", fake_mono):
            res = tt.for_agents(agents, budget=1.5, home=self.home,
                                cache_path=self.cache, now=NOW)
        self.assertIsInstance(res[0], dict)
        self.assertIsNone(res[1], "past-deadline agent gets NO telemetry")
        with open(self.cache) as f:
            cached = json.load(f)
        self.assertIn(os.path.realpath(p1), cached)
        self.assertNotIn(os.path.realpath(p2), cached)


# ---------- safety gate ----------
class Safety(Base):
    def pi(self, value):
        return self.agent("pi", {"agent": "pi", "kind": "path",
                                 "source": "herdr:pi", "value": value})

    def cl(self, value):
        return self.agent("claude", {"agent": "claude", "kind": "id",
                                     "source": "herdr:claude",
                                     "value": value})

    def test_outside_home_rejected(self):
        outside = tempfile.mkdtemp(prefix="tendril-tel-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        p = self.write_jsonl(os.path.join(outside, "s.jsonl"),
                             [pi_session_line(NOW)])
        self.assertIsNone(self.run_tele([self.pi(p)])[0])

    def test_traversal_and_symlink_escape_rejected(self):
        # '..' noise that resolves back INSIDE home is fine (realpath-normalized)
        p = self.write_jsonl(os.path.join(self.home, "real", "s.jsonl"),
                             [pi_session_line(NOW), pi_user(NOW, "hi")])
        noisy = os.path.join(self.home, "real", "sub", "..", "s.jsonl")
        self.assertEqual(self.run_tele([self.pi(noisy)])[0]["trail"], "P")
        # a symlink that escapes home is rejected
        outside = tempfile.mkdtemp(prefix="tendril-tel-out2-")
        self.addCleanup(shutil.rmtree, outside, True)
        victim = self.write_jsonl(os.path.join(outside, "victim.jsonl"),
                                  [pi_session_line(NOW)])
        link = os.path.join(self.home, "escape.jsonl")
        os.symlink(victim, link)
        self.assertIsNone(self.run_tele([self.pi(link)])[0])

    def test_non_jsonl_directory_and_missing_rejected(self):
        d = os.path.join(self.home, ".pi", "agent", "sessions", "d.jsonl")
        os.makedirs(d)
        self.assertIsNone(self.run_tele([self.pi(d)])[0])
        self.assertIsNone(self.run_tele(
            [self.pi(os.path.join(self.home, "nope.jsonl"))])[0])
        txt = os.path.join(self.home, "notes.txt")
        with open(txt, "w") as f:
            f.write("hi")
        self.assertIsNone(self.run_tele([self.pi(txt)])[0])

    def test_claude_uuid_gate(self):
        self.assertIsNone(self.run_tele([self.cl("../etc/passwd")])[0])
        self.assertIsNone(self.run_tele([self.cl("xyz")])[0])
        self.assertIsNone(self.run_tele(
            [self.cl("G1111111-2222-4333-8444-555555555555")])[0])
        self.assertIsNone(self.run_tele([self.cl(UUID_OK)])[0],
                          "valid uuid with no transcript: omitted")

    def test_bad_session_shapes_rejected(self):
        self.assertIsNone(self.run_tele([self.agent("pi", None)])[0])
        self.assertIsNone(self.run_tele(
            [self.agent("pi", {"kind": "path", "value": ""})])[0])
        self.assertIsNone(self.run_tele(
            [self.agent("pi", {"kind": "id", "value": UUID_OK})])[0])

    def test_codex_gets_no_telemetry(self):
        self.assertIsNone(self.run_tele([self.agent("codex", {
            "agent": "codex", "kind": "id", "source": "herdr:codex",
            "value": "roll-abc"})])[0])

    def test_broken_cache_file_does_not_break_derivation(self):
        p = self.write_jsonl(os.path.join(
            self.home, ".pi", "agent", "sessions", "proj", "c.jsonl"),
            [pi_session_line(NOW - 5), pi_user(NOW - 5, "hi")])
        os.makedirs(os.path.dirname(self.cache), exist_ok=True)
        with open(self.cache, "w") as f:
            f.write("{not json")
        res = self.run_tele([self.pi(p)])[0]
        self.assertEqual(res["trail"], "P")


# ---------- privacy ----------
class Privacy(Base):
    def test_no_content_reaches_the_telemetry_dict(self):
        path = self.write_jsonl(os.path.join(
            self.home, ".pi", "agent", "sessions", "proj", "p.jsonl"), [
            pi_session_line(NOW - 60, cwd="/home/u/SECRET_DIR/deep"),
            pi_user(NOW - 60, "SECRET-PROMPT deploy to SECRET-HOST"),
            pi_assistant(NOW - 50, pi_usage(1, 1, 0, 0, 0.0),
                         [pi_tool_call("t1", "bash", NOW - 50,
                                       {"command": "curl SECRET-HOST",
                                        "path": "/home/u/SECRET_DIR/x.sh"})]),
        ])
        res = self.run_tele([self.agent("pi", {
            "agent": "pi", "kind": "path", "source": "herdr:pi",
            "value": path})])[0]
        text = json.dumps(res)
        for needle in ("SECRET", "deploy", "curl", "/home/u", "deep"):
            self.assertNotIn(needle, text)
        self.assertEqual(res["activity"], "Running a command")
        self.assertNotIn("cwd", res)


# ---------- snapshot wiring ----------
class SnapshotV3Wiring(unittest.TestCase):
    """End-to-end: `remote-agents snapshot` emits version 3 with telemetry
    (v2 keys unchanged, plus phase/events; codex gets none of them)."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tendril-tel-e2e-")
        self.cache = os.path.join(self.home, ".cache", "telemetry-v2.json")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.pi_path = os.path.join(self.home, ".pi", "agent", "sessions",
                                    "proj", "e2e.jsonl")
        os.makedirs(os.path.dirname(self.pi_path))
        with open(self.pi_path, "w") as f:
            f.write("".join(dumps(x) + "\n" for x in [
                pi_session_line(NOW - 120, cwd="/home/u/SECRET_DIR"),
                pi_model_change(NOW - 119),
                pi_user(NOW - 120, "SECRET-PROMPT hi"),
                pi_assistant(NOW - 60, pi_usage(42, 9, 0, 0, 0.111),
                             [{"type": "text", "text": "SECRET-REPLY"}]),
            ]))
        self.out = io.StringIO()
        self.err = io.StringIO()

    def envelope(self):
        return {"result": {"snapshot": {
            "workspaces": [
                {"workspace_id": "w2", "label": "lab", "number": 2,
                 "focused": True, "active_tab_id": "w2:t1"}],
            "panes": [],
            "agents": [
                {"pane_id": "w2:p1", "workspace_id": "w2", "tab_id": "w2:t1",
                 "agent": "pi", "agent_status": "working", "focused": True,
                 "cwd": "/home/u/SECRET_DIR",
                 "agent_session": {"agent": "pi", "kind": "path",
                                   "source": "herdr:pi",
                                   "value": self.pi_path}},
                {"pane_id": "w2:p2", "workspace_id": "w2", "tab_id": "w2:t2",
                 "agent": "codex", "agent_status": "idle", "focused": False,
                 "cwd": "/home/u/SECRET_DIR",
                 "agent_session": {"agent": "codex", "kind": "id",
                                   "source": "herdr:codex",
                                   "value": "roll-xyz"}},
            ],
            "focused_pane_id": "w2:p1",
            "focused_tab_id": "w2:t1",
        }}}

    def test_snapshot_embeds_v3_telemetry(self):
        def fake_run(args, timeout=6):
            if args[1:] == ["api", "snapshot"]:
                return 0, json.dumps(self.envelope()), ""
            return 0, "status: running\n", ""

        ctx = [mock.patch.object(ra, "HERDR", "herdr"),
               mock.patch.object(ra, "HOST_LABEL", "testhost"),
               mock.patch.object(ra, "run", fake_run),
               mock.patch.object(ra.sys, "argv",
                                 ["remote-agents", "snapshot"]),
               mock.patch.object(ra.sys, "stdout", self.out),
               mock.patch.object(ra.sys, "stderr", self.err),
               # sibling hooks are other workers' modules: pin them absent
               mock.patch.dict(sys.modules, {"tendril_capacity": None,
                                             "tendril_schedule": None})]
        if ra.tendril_telemetry is not None:
            ctx += [mock.patch.object(ra.tendril_telemetry, "HOME", self.home),
                    mock.patch.object(ra.tendril_telemetry, "CACHE_FILE",
                                      self.cache)]
        for c in ctx:
            c.start()
        try:
            with mock.patch.object(ra.time, "time", return_value=float(NOW)):
                code = ra.main()
        finally:
            for c in reversed(ctx):
                c.stop()
        self.assertEqual(code, 0)
        doc = json.loads(self.out.getvalue().strip())
        self.assertEqual(doc["version"], 3)
        self.assertIsInstance(doc["capabilities"], list)
        self.assertIn("subagents", doc["capabilities"])
        agents = doc["workspaces"][0]["agents"]
        self.assertEqual(len(agents), 2)
        pi, codex = agents
        # the five v1 fields are unchanged, harness always present
        for a in agents:
            for k in ("pane_id", "tab_id", "agent", "status", "focused",
                      "harness"):
                self.assertIn(k, a)
            self.assertNotIn("cwd", a)
        # pi: real telemetry
        self.assertEqual(pi["harness"], "pi")
        self.assertEqual(pi["model"], "glm-test-5")
        self.assertEqual(pi["provider"], "zai")
        self.assertEqual(pi["effort"], "high")
        self.assertEqual(pi["started_at"], NOW - 120)
        self.assertEqual(pi["context_tokens"], 42)
        self.assertEqual(pi["output_tokens"], 9)
        self.assertEqual(pi["cost_usd"], 0.111)
        self.assertEqual(pi["last_event"], {"kind": "reply", "at": NOW - 60})
        self.assertEqual(pi["trail"], "PR")
        self.assertEqual(pi["activity"], "Thinking")
        # v3: phase + events (no child sessions exist in this fixture)
        self.assertEqual(pi["phase"], "thinking")
        self.assertEqual(pi["events"],
                         [{"kind": "prompt", "at": NOW - 120},
                          {"kind": "reply", "at": NOW - 60}])
        self.assertNotIn("children", pi)
        # codex: harness only
        self.assertEqual(codex["harness"], "codex")
        for k in ("model", "provider", "effort", "started_at",
                  "context_tokens", "output_tokens", "cost_usd", "tool",
                  "tool_started_at", "activity", "last_event", "trail",
                  "phase", "events", "children"):
            self.assertNotIn(k, codex)
        # privacy across the whole wire line
        for needle in ("SECRET", "/home/u", "cwd", "terminal_title",
                       "agent_session"):
            self.assertNotIn(needle, self.out.getvalue())

    def test_snapshot_survives_a_broken_telemetry_module(self):
        def fake_run(args, timeout=6):
            if args[1:] == ["api", "snapshot"]:
                return 0, json.dumps(self.envelope()), ""
            return 0, "status: running\n", ""

        ctx = [mock.patch.object(ra, "HERDR", "herdr"),
               mock.patch.object(ra, "HOST_LABEL", "testhost"),
               mock.patch.object(ra, "run", fake_run),
               mock.patch.object(ra.sys, "argv",
                                 ["remote-agents", "snapshot"]),
               mock.patch.object(ra.sys, "stdout", self.out),
               mock.patch.object(ra.sys, "stderr", self.err),
               mock.patch.dict(sys.modules, {"tendril_capacity": None,
                                             "tendril_schedule": None}),
               mock.patch.object(ra.tendril_telemetry, "for_agents",
                                 side_effect=RuntimeError("boom"))]
        for c in ctx:
            c.start()
        try:
            code = ra.main()
        finally:
            for c in reversed(ctx):
                c.stop()
        self.assertEqual(code, 0)
        doc = json.loads(self.out.getvalue().strip())
        self.assertEqual(doc["version"], 3)
        for a in doc["workspaces"][0]["agents"]:
            self.assertNotIn("model", a)
            self.assertEqual(a["harness"], a["agent"])


if __name__ == "__main__":
    unittest.main()


class SyntheticModelTest(unittest.TestCase):
    """Claude Code writes model "<synthetic>" on interrupt turns; the last
    REAL model must survive."""

    def test_placeholder_models_never_replace_a_real_one(self):
        self.assertTrue(tt._real_model("claude-opus-5"))
        self.assertFalse(tt._real_model("<synthetic>"))
        self.assertFalse(tt._real_model(""))
        self.assertFalse(tt._real_model(None))


# ---------- v3 subagent children (shared fixtures) ----------
def pi_session_header(ts, **kw):
    d = {"type": "session", "version": 9, "id": "s-child",
         "timestamp": iso(ts), "cwd": "/home/u/SECRET_CHILD_CWD"}
    d.update(kw)
    return d


def pi_assistant_stop(ts, usage, stop="end_turn"):
    d = pi_assistant(ts, usage, [{"type": "text", "text": "x"}])
    d["message"]["stopReason"] = stop
    return d


class ChildBase(Base):
    """Fixtures for the three child layouts: same-dir parentSession,
    pi-subagents <stem>/<runId>/run-N/session.jsonl (+ meta artifacts) and
    claude <sessionId>/subagents/agent-*.jsonl."""

    RUN_ID = "9f1e0d0c-1111-4222-8333-444455556666"

    def setUp(self):
        super().setUp()
        self.proj = os.path.join(self.home, ".pi", "agent", "sessions",
                                 "proj")

    def parent(self, name, lines):
        return self.write_jsonl(os.path.join(self.proj, name), lines)

    def run_child(self, parent_path, run_id, lines, mtime=None):
        d = os.path.join(os.path.dirname(parent_path),
                         os.path.basename(parent_path)[:-len(".jsonl")],
                         run_id, "run-0")
        p = self.write_jsonl(os.path.join(d, "session.jsonl"), lines)
        # default: an OLD mtime so _child_last's mtime fallback never masks
        # the transcript's own timestamps in status timing assertions
        os.utime(p, (mtime if mtime is not None else NOW - 100000,) * 2)
        return p

    def meta(self, child_path, run_id, obj):
        d = os.path.join(os.path.dirname(os.path.dirname(child_path)),
                         "subagent-artifacts")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "%s_w_meta.json" % run_id)
        with open(p, "w") as f:
            f.write(dumps(obj))
        return p

    def pi_agent(self, path, status="working"):
        return {"agent": "pi", "agent_status": status,
                "agent_session": {"agent": "pi", "kind": "path",
                                  "source": "herdr:pi", "value": path}}

    def cache_bytes(self):
        with open(self.cache, "rb") as f:
            return f.read()


class PiSameDirChildren(ChildBase):
    def test_header_naming_this_file_attaches_the_child(self):
        parent = self.parent("p1.jsonl", [
            pi_session_line(NOW - 100),
            pi_user(NOW - 100, "SECRET-PROMPT go")])
        self.parent("c1.jsonl", [
            pi_session_header(NOW - 90,
                              parentSession=os.path.realpath(parent),
                              name="worker",
                              id="feedface-1111-4222-8333-444455556666"),
            pi_assistant(NOW - 80, pi_usage(10, 2, 0, 0, 0.0),
                         [{"type": "text", "text": "SECRET-CHILD-REPLY"}]),
            pi_user(NOW - 10, "SECRET-CHILD-PROMPT"),
        ])
        res = self.run_tele([self.pi_agent(parent)])[0]
        kids = res["children"]
        self.assertEqual(len(kids), 1)
        k = kids[0]
        self.assertEqual(k["kind"], "subagent")
        self.assertEqual(k["harness"], "pi")
        self.assertEqual(k["name"], "worker")
        self.assertEqual(k["id"], "feedface")
        self.assertEqual(k["status"], "working")
        self.assertEqual(k["started_at"], NOW - 90)
        self.assertEqual(k["context_tokens"], 10)
        self.assertEqual(k["output_tokens"], 2)
        self.assertNotIn("async", k)             # sync child: key absent
        text = json.dumps(res)
        for needle in ("SECRET-CHILD", "SECRET_CHILD_CWD", "/home/u"):
            self.assertNotIn(needle, text)

    def test_unrelated_sibling_session_is_not_a_child(self):
        parent = self.parent("p1b.jsonl", [
            pi_session_line(NOW - 100), pi_user(NOW - 100, "go")])
        self.parent("c2.jsonl", [
            pi_session_header(NOW - 50,
                              parentSession="/home/u/other-session.jsonl")])
        self.parent("c3.jsonl", [                 # no parentSession at all
            pi_session_header(NOW - 40)])
        res = self.run_tele([self.pi_agent(parent)])[0]
        self.assertNotIn("children", res)


class PiRunChildren(ChildBase):
    def test_run_dir_child_with_meta_artifacts(self):
        parent = self.parent("p2.jsonl", [
            pi_session_line(NOW - 200),
            pi_user(NOW - 200, "SECRET-PROMPT go")])
        child = self.run_child(parent, self.RUN_ID, [
            pi_session_header(NOW - 150, id="run-child"),
            pi_assistant(NOW - 100, pi_usage(1000, 50, 900, 100, 0.05),
                         [{"type": "text", "text": "SECRET-CHILD-REPLY"}]),
        ])
        self.meta(child, self.RUN_ID, {
            "runId": self.RUN_ID, "agent": "worker", "exitCode": 0,
            "model": "meta-model", "label": "w19",
            "task": "SUPER-SECRET-TASK-DECOY",
            "acceptance": "SUPER-SECRET-ACCEPTANCE-DECOY",
            "prompt": "ALSO-SECRET-PROMPT-DECOY",
            "usage": {"input": 1200, "output": 60, "cacheRead": 5,
                      "cost": 0.02, "turns": 3}})
        res = self.run_tele([self.pi_agent(parent)])[0]
        k = res["children"][0]
        self.assertEqual(k["id"], "9f1e0d0c")
        self.assertEqual(k["name"], "worker")
        self.assertEqual(k["status"], "done")
        self.assertEqual(k["async"], True)
        self.assertEqual(k["started_at"], NOW - 150)
        self.assertEqual(k["ended_at"], NOW - 100)
        # transcript facts win; meta only fills gaps
        self.assertEqual(k["context_tokens"], 2000)
        self.assertEqual(k["output_tokens"], 50)
        self.assertEqual(k["cost_usd"], 0.05)
        wire = json.dumps(res)
        cache = self.cache_bytes().decode("utf-8", "replace")
        for where in (wire, cache):
            for needle in ("SUPER-SECRET-TASK-DECOY",
                           "SUPER-SECRET-ACCEPTANCE-DECOY",
                           "ALSO-SECRET-PROMPT-DECOY", "SECRET-CHILD"):
                self.assertNotIn(needle, where)
        self.assertNotIn("meta-model", wire)     # meta model is not emitted

    def test_meta_fills_gaps_when_transcript_has_no_usage(self):
        parent = self.parent("p2b.jsonl", [
            pi_session_line(NOW - 200), pi_user(NOW - 200, "go")])
        child = self.run_child(parent, self.RUN_ID, [
            pi_session_header(NOW - 150, id="run-child")])
        self.meta(child, self.RUN_ID, {
            "runId": self.RUN_ID, "agent": "Explore", "exitCode": 0,
            "usage": {"input": 500, "cacheRead": 50, "output": 40,
                      "cost": 0.01}})
        k = self.run_tele([self.pi_agent(parent)])[0]["children"][0]
        self.assertEqual(k["name"], "Explore")
        self.assertEqual(k["context_tokens"], 550)
        self.assertEqual(k["output_tokens"], 40)
        self.assertEqual(k["cost_usd"], 0.01)


class ClaudeChildren(ChildBase):
    def test_sidechain_children_under_the_session_dir(self):
        _d, path = cl_session_dir(self.home, UUID_OK)
        self.write_jsonl(path, [cl_user_prompt(NOW - 200)])
        sub = os.path.join(os.path.dirname(path), UUID_OK, "subagents")
        self.write_jsonl(os.path.join(sub, "agent-cafe1234bb.jsonl"), [
            cl_user_prompt(NOW - 100),
            cl_tool_use(NOW - 90, "toolu_c1", "Bash",
                        {"command": "echo SECRET-CHILD-COMMAND"}),
        ])
        self.write_jsonl(os.path.join(sub, "notes.jsonl"),
                         [cl_user_prompt(NOW - 50)])   # NOT agent-*: skipped
        res = self.run_tele([self.agent("claude", self.agent_sess())])[0]
        kids = res["children"]
        self.assertEqual(len(kids), 1)
        k = kids[0]
        self.assertEqual(k["id"], "cafe1234")
        self.assertEqual(k["harness"], "claude")
        self.assertEqual(k["status"], "tool")
        self.assertEqual(k["tool"], "Bash")
        self.assertNotIn("SECRET", json.dumps(res))


class CodexNoChildren(ChildBase):
    def test_codex_yields_no_telemetry_and_no_children(self):
        # even with a pi-shaped child tree under the value, codex sessions
        # are never parsed: no telemetry row, hence no children anywhere.
        self.run_child(os.path.join(self.proj, "cx.jsonl"), self.RUN_ID, [
            pi_session_header(NOW - 10)])
        res = self.run_tele([self.agent("codex", {
            "agent": "codex", "kind": "id", "source": "herdr:codex",
            "value": "roll-1"})])[0]
        self.assertIsNone(res)


class ChildStatusMapping(ChildBase):
    """done | failed | working | tool | thinking | unknown — meta exitCode
    wins, then timing rules; nothing is ever guessed."""

    SEQ = [0]

    def scenario(self, lines, meta=None):
        ChildStatusMapping.SEQ[0] += 1
        n = ChildStatusMapping.SEQ[0]
        parent = self.parent("ps%d.jsonl" % n, [
            pi_session_line(NOW - 1000), pi_user(NOW - 1000, "go")])
        rid = "d000000%d-1111-4222-8333-444455556666" % (n % 10)
        child = self.run_child(parent, rid, lines)
        if meta is not None:
            self.meta(child, rid, meta)
        res = self.run_tele([self.pi_agent(parent)])[0]
        self.assertEqual(len(res["children"]), 1)
        return res["children"][0]

    def test_meta_exit_code_decides_done_and_failed(self):
        head = lambda: [pi_session_header(NOW - 100, id="x"),
                        pi_user(NOW - 90, "child prompt")]
        done = self.scenario(head(), meta={"runId": "x", "exitCode": 0})
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["ended_at"], NOW - 90)
        failed = self.scenario(head(), meta={"runId": "x", "exitCode": 1})
        self.assertEqual(failed["status"], "failed")
        self.assertIn("ended_at", failed)

    def test_open_tool_is_tool(self):
        k = self.scenario([
            pi_session_header(NOW - 100, id="x"),
            pi_user(NOW - 40, "child prompt"),
            pi_assistant(NOW - 30, pi_usage(1, 1, 0, 0, 0.0),
                         [pi_tool_call("t1", "bash", NOW - 30)])])
        self.assertEqual(k["status"], "tool")
        self.assertEqual(k["tool"], "bash")
        self.assertEqual(k["activity"], "Running a command")

    def test_fresh_prompt_is_working_fresh_reply_is_thinking(self):
        working = self.scenario([
            pi_session_header(NOW - 100, id="x"),
            pi_assistant(NOW - 90, pi_usage(1, 1, 0, 0, 0.0),
                         [{"type": "text", "text": "x"}]),
            pi_user(NOW - 10, "child prompt")])
        self.assertEqual(working["status"], "working")
        thinking = self.scenario([
            pi_session_header(NOW - 100, id="x"),
            pi_user(NOW - 90, "child prompt"),
            pi_assistant(NOW - 10, pi_usage(1, 1, 0, 0, 0.0),
                         [{"type": "text", "text": "x"}])])
        self.assertEqual(thinking["status"], "thinking")

    def test_quiet_without_terminal_evidence_is_stale(self):
        """§7.2: quiet past the active window with no terminal evidence is
        `stale` — never failed by inference, never `unknown`."""
        k = self.scenario([
            pi_session_header(NOW - 1000, id="x"),
            pi_user(NOW - 1000, "child prompt")])
        self.assertEqual(k["status"], "stale")
        self.assertNotIn("ended_at", k)

    def test_terminal_stop_then_quiet_is_done(self):
        k = self.scenario([
            pi_session_header(NOW - 200, id="x"),
            pi_assistant_stop(NOW - 100, pi_usage(1, 1, 0, 0, 0.0))])
        self.assertEqual(k["status"], "done")
        self.assertEqual(k["ended_at"], NOW - 100)

    def test_error_then_quiet_is_failed(self):
        k = self.scenario([
            pi_session_header(NOW - 200, id="x"),
            pi_user(NOW - 150, "child prompt"),
            pi_assistant(NOW - 120, pi_usage(1, 1, 0, 0, 0.0),
                         [pi_tool_call("t1", "edit", NOW - 120)]),
            pi_tool_result(NOW - 100, "t1", "edit", error=True)])
        self.assertEqual(k["status"], "failed")
        self.assertEqual(k["ended_at"], NOW - 100)


class ChildStatusEdges72(ChildBase):
    """§7.2 edge cases on REAL transcript shapes: an error followed by
    recovery is never failed; quiet history is stale or done; failed
    needs explicit evidence (exitCode, error stop, error-as-final)."""

    SEQ = [0]

    def scenario(self, lines, meta=None):
        ChildStatusEdges72.SEQ[0] += 1
        n = ChildStatusEdges72.SEQ[0]
        parent = self.parent("e%d.jsonl" % n, [
            pi_session_line(NOW - 1000), pi_user(NOW - 1000, "go")])
        rid = "e000000%d-1111-4222-8333-444455556666" % (n % 10)
        child = self.run_child(parent, rid, lines)
        if meta is not None:
            self.meta(child, rid, meta)
        res = self.run_tele([self.pi_agent(parent)])[0]
        self.assertEqual(len(res["children"]), 1)
        return res["children"][0]

    def test_tool_error_then_recovery_then_quiet_is_stale(self):
        k = self.scenario([
            pi_session_header(NOW - 2000, id="x"),
            pi_user(NOW - 2000, "child prompt"),
            pi_assistant(NOW - 1900, pi_usage(1, 1, 0, 0, 0.0),
                         [pi_tool_call("t1", "edit", NOW - 1900)]),
            pi_tool_result(NOW - 1800, "t1", "edit", error=True),
            pi_assistant(NOW - 1700, pi_usage(1, 1, 0, 0, 0.0),
                         [{"type": "text", "text": "recovered"}])])
        self.assertEqual(k["status"], "stale")   # never failed by inference

    def test_error_then_recovery_then_terminal_stop_is_done(self):
        k = self.scenario([
            pi_session_header(NOW - 2000, id="x"),
            pi_user(NOW - 2000, "child prompt"),
            pi_assistant(NOW - 1900, pi_usage(1, 1, 0, 0, 0.0),
                         [pi_tool_call("t1", "edit", NOW - 1900)]),
            pi_tool_result(NOW - 1800, "t1", "edit", error=True),
            pi_assistant_stop(NOW - 1700, pi_usage(1, 1, 0, 0, 0.0))])
        self.assertEqual(k["status"], "done")
        self.assertEqual(k["ended_at"], NOW - 1700)

    def test_error_then_new_open_tool_is_not_failed(self):
        """A tool opened AFTER the error is later work: the child is
        active, never failed."""
        k = self.scenario([
            pi_session_header(NOW - 300, id="x"),
            pi_user(NOW - 250, "child prompt"),
            pi_assistant(NOW - 240, pi_usage(1, 1, 0, 0, 0.0),
                         [pi_tool_call("t1", "bash", NOW - 240)]),
            pi_tool_result(NOW - 200, "t1", "bash", error=True),
            pi_assistant(NOW - 60, pi_usage(1, 1, 0, 0, 0.0),
                         [pi_tool_call("t2", "read", NOW - 60)])])
        self.assertEqual(k["status"], "tool")

    def test_recent_error_within_quiet_grace_is_not_failed_yet(self):
        """The harness may still react in the first CHILD_QUIET_S seconds."""
        k = self.scenario([
            pi_session_header(NOW - 60, id="x"),
            pi_user(NOW - 40, "child prompt"),
            pi_assistant(NOW - 30, pi_usage(1, 1, 0, 0, 0.0),
                         [pi_tool_call("t1", "edit", NOW - 30)]),
            pi_tool_result(NOW - 10, "t1", "edit", error=True)])
        self.assertEqual(k["status"], "working")

    def test_explicit_error_stop_reason_is_failed(self):
        k = self.scenario([
            pi_session_header(NOW - 100, id="x"),
            pi_user(NOW - 90, "child prompt"),
            pi_assistant_stop(NOW - 80, pi_usage(1, 1, 0, 0, 0.0),
                              stop="error")])
        self.assertEqual(k["status"], "failed")

    def test_historical_child_days_old_is_stale(self):
        days = 3 * 86400
        k = self.scenario([
            pi_session_header(NOW - days, id="x"),
            pi_user(NOW - days, "child prompt"),
            pi_assistant(NOW - days + 60, pi_usage(1, 1, 0, 0, 0.0),
                         [{"type": "text", "text": "x"}])])
        self.assertEqual(k["status"], "stale")
        self.assertNotIn("ended_at", k)

    def test_truncated_transcript_is_never_failed(self):
        parent = self.parent("trunc.jsonl", [
            pi_session_line(NOW - 1000), pi_user(NOW - 1000, "go")])
        rid = "f0000001-1111-4222-8333-444455556666"
        child = self.run_child(parent, rid, [
            pi_session_header(NOW - 900, id="x"),
            pi_user(NOW - 800, "child prompt"),
            pi_assistant(NOW - 700, pi_usage(1, 1, 0, 0, 0.0),
                         [pi_tool_call("t1", "edit", NOW - 700)]),
            pi_tool_result(NOW - 600, "t1", "edit", error=True)])
        agent = [self.pi_agent(parent)]
        k = self.run_tele(agent)[0]["children"][0]
        self.assertEqual(k["status"], "failed")   # error is final: evidence
        with open(child, "r+") as f:               # the writer lost its tail
            f.truncate(os.path.getsize(child) // 2)
        k = self.run_tele(agent)[0]["children"][0]
        self.assertIn(k["status"], ("working", "thinking", "stale",
                                    "unknown"))
        self.assertNotEqual(k["status"], "failed")

    def test_claude_historical_child_done_or_stale_never_failed(self):
        day = 3 * 86400
        _d, path = cl_session_dir(self.home, UUID_OK)
        self.write_jsonl(path, [cl_user_prompt(NOW - day)])
        sub = os.path.join(os.path.dirname(path), UUID_OK, "subagents")
        self.write_jsonl(os.path.join(sub, "agent-aaa1aaaa.jsonl"), [
            cl_user_prompt(NOW - day),
            cl_assistant(NOW - day + 60, content=[{"type": "text",
                                                   "text": "x"}])])
        self.write_jsonl(os.path.join(sub, "agent-bbb2bbbb.jsonl"), [
            cl_user_prompt(NOW - day),
            cl_tool_use(NOW - day + 30, "toolu_s", "Edit", {"path": "x.kt"}),
            cl_user_tool_result(NOW - day + 40, "toolu_s", error=True),
            cl_assistant(NOW - day + 50, content=[{"type": "text",
                                                   "text": "recovered"}])])
        # quiet-window assertions need transcript-age mtimes, not "now"
        for n in ("agent-aaa1aaaa.jsonl", "agent-bbb2bbbb.jsonl"):
            p = os.path.join(sub, n)
            os.utime(p, (NOW - day + 120, NOW - day + 120))
        res = self.run_tele([self.agent("claude", self.agent_sess())])[0]
        by_id = {k["id"]: k for k in res["children"]}
        self.assertEqual(by_id["aaa1aaaa"]["status"], "stale")
        self.assertEqual(by_id["aaa1aaaa"].get("harness"), "claude")
        self.assertEqual(by_id["bbb2bbbb"]["status"], "stale")
        for k in res["children"]:              # recovery is never failure
            self.assertNotEqual(k["status"], "failed")


class ChildCap(ChildBase):
    def test_at_most_eight_children_newest_first(self):
        parent = self.parent("cap.jsonl", [
            pi_session_line(NOW - 500), pi_user(NOW - 500, "go")])
        stem = os.path.join(os.path.dirname(parent),
                            os.path.basename(parent)[:-len(".jsonl")])
        for i in range(1, 11):                     # run-1 .. run-10
            rid = "b00000%02d-1111-4222-8333-444455556666" % i
            p = self.write_jsonl(os.path.join(stem, rid, "run-0",
                                              "session.jsonl"),
                                 [pi_session_header(NOW - 400 + i)])
            os.utime(p, (NOW - 100 + i, NOW - 100 + i))
        res = self.run_tele([self.pi_agent(parent)])[0]
        kids = res["children"]
        self.assertEqual(len(kids), 8)             # newest 8 of 10
        self.assertEqual([k["id"] for k in kids],
                         ["b00000%02d" % i for i in range(10, 2, -1)])
        self.assertEqual([k["status"] for k in kids], ["thinking"] * 8)


class ChildIncremental(ChildBase):
    def test_partial_final_child_line_waits_for_the_rest(self):
        parent = self.parent("inc.jsonl", [
            pi_session_line(NOW - 200), pi_user(NOW - 200, "go")])
        rest = dumps(pi_assistant(NOW - 50, pi_usage(7, 9, 0, 0, 0.0),
                                  [{"type": "text", "text": "x"}]))
        child = self.run_child(parent, self.RUN_ID, [
            pi_session_header(NOW - 150, id="x"),
            pi_assistant(NOW - 100, pi_usage(7, 3, 0, 0, 0.0),
                         [{"type": "text", "text": "x"}])])
        with open(child, "a") as f:
            f.write(rest[:20])                     # writer mid-line
        agent = [self.pi_agent(parent)]
        k = self.run_tele(agent)[0]["children"][0]
        self.assertEqual(k["output_tokens"], 3)    # complete lines only
        ce = self.cache_entry(parent)["children"]
        (entry,) = ce.values()
        self.assertEqual(entry["offset"], os.path.getsize(child) - 20)
        with open(child, "a") as f:
            f.write(rest[20:] + "\n")
        k = self.run_tele(agent)[0]["children"][0]
        self.assertEqual(k["output_tokens"], 3 + 9)
        (entry,) = self.cache_entry(parent)["children"].values()
        self.assertEqual(entry["offset"], os.path.getsize(child))

    def test_huge_child_line_is_mined_not_parsed(self):
        blob = {"type": "text", "text": "SECRET-BASE64 " + "A" * (2 << 20)}
        parent = self.parent("huge.jsonl", [
            pi_session_line(NOW - 200), pi_user(NOW - 200, "go")])
        child = self.run_child(parent, self.RUN_ID, [
            pi_session_header(NOW - 150, id="x"),
            pi_user(NOW - 60, "child prompt"),
            pi_assistant(NOW - 50, pi_usage(1, 1, 0, 0, 0.0),
                         [pi_tool_call("t1", "read", NOW - 50,
                                       {"path": "/home/u/SECRET_DIR/p.png"})]),
            {"type": "message", "id": "r", "parentId": "a",
             "timestamp": iso(NOW - 40),
             "message": {"role": "toolResult", "toolCallId": "t1",
                         "toolName": "read", "isError": True,
                         "timestamp": NOW * 1000 - 40000,
                         "content": [blob]}},
        ])
        self.assertGreater(os.path.getsize(child), tt.HUGE_LINE)
        t0 = time.monotonic()
        k = self.run_tele([self.pi_agent(parent)])[0]["children"][0]
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertEqual(k["status"], "failed")    # error result, no meta
        wire = json.dumps(k)
        for needle in ("SECRET-BASE64", "AAAA", "SECRET_DIR"):
            self.assertNotIn(needle, wire)
        self.assertNotIn("SECRET-BASE64",
                         self.cache_bytes().decode("utf-8", "replace"))

    def test_budget_exhaustion_skips_and_resumes_next_call(self):
        parent = self.parent("budget.jsonl", [
            pi_session_line(NOW - 3000), pi_user(NOW - 3000, "go")])
        header = pi_session_header(NOW - 2500, id="x")
        line = dumps(pi_assistant(NOW - 2000, pi_usage(0, 1, 0, 0, 0.0),
                                  [{"type": "text", "text": "x"}]))
        H, L = len(dumps(header)) + 1, len(line) + 1   # + newline byte
        n = 1200
        body = "".join(
            dumps(pi_assistant(NOW - 2000 - i, pi_usage(0, 1, 0, 0, 0.0),
                               [{"type": "text", "text": "x"}])) + "\n"
            for i in range(n))
        child = self.run_child(parent, self.RUN_ID, [header])
        with open(child, "a") as f:
            f.write(body)
        os.utime(child, (NOW - 100000,) * 2)   # the append bumped mtime
        size = os.path.getsize(child)
        ticks = iter([0.1 * (i + 1) for i in range(200)])   # 0.1s per call

        def fake_mono():
            return next(ticks, 9e9)

        agent = [self.pi_agent(parent)]
        kw = dict(budget=1.5, home=self.home, cache_path=self.cache, now=NOW)
        with mock.patch.object(tt, "_mono", fake_mono):
            res1 = tt.for_agents(agent, **kw)[0]
        (entry,) = self.cache_entry(parent)["children"].values()
        self.assertGreater(entry["offset"], 0, "child scan started")
        self.assertLess(entry["offset"], size, "budget stopped it mid-file")
        offset = entry["offset"]
        self.assertEqual((offset - H) % L, 0, "no torn line was consumed")
        k1 = res1["children"][0]
        self.assertEqual(k1["output_tokens"], (offset - H) // L)
        # next call: budget restored — the rest is parsed, nothing re-parsed
        with mock.patch.object(tt, "_mono", lambda: 0.0):
            res2 = tt.for_agents(agent, **kw)[0]
        (entry,) = self.cache_entry(parent)["children"].values()
        self.assertEqual(entry["offset"], size)
        self.assertEqual(res2["children"][0]["output_tokens"], n)
        self.assertEqual(res2["children"][0]["status"], "stale")


class ChildSafety(ChildBase):
    def test_symlink_child_escaping_home_is_never_even_read(self):
        outside = tempfile.mkdtemp(prefix="tendril-child-out-")
        self.addCleanup(shutil.rmtree, outside, True)
        parent = self.parent("p3.jsonl", [
            pi_session_line(NOW - 100), pi_user(NOW - 100, "go")])
        victim = os.path.join(outside, "victim.jsonl")
        with open(victim, "w") as f:
            f.write(dumps(pi_session_header(
                NOW - 90, parentSession=os.path.realpath(parent),
                name="worker")) + "\n")
        os.symlink(victim, os.path.join(self.proj, "esc.jsonl"))
        reads = {"n": 0}
        real_header = tt._pi_header

        def counting(p):
            reads["n"] += 1
            return real_header(p)

        with mock.patch.object(tt, "_pi_header", counting):
            res = self.run_tele([self.pi_agent(parent)])[0]
        self.assertNotIn("children", res)
        self.assertEqual(reads["n"], 0, "outside-home child is never opened")

    def test_parent_session_traversal_that_escapes_is_not_a_child(self):
        parent = self.parent("p4.jsonl", [
            pi_session_line(NOW - 100), pi_user(NOW - 100, "go")])
        self.parent("c4.jsonl", [
            pi_session_header(
                NOW - 90, parentSession=os.path.join(
                    self.home, "elsewhere", "..", "elsewhere.jsonl"))])
        res = self.run_tele([self.pi_agent(parent)])[0]
        self.assertNotIn("children", res)

    def test_claude_bad_uuid_yields_no_children_and_no_error(self):
        _d, path = cl_session_dir(self.home, UUID_OK)
        os.makedirs(os.path.join(os.path.dirname(path), UUID_OK,
                                 "subagents"), exist_ok=True)
        self.write_jsonl(os.path.join(os.path.dirname(path), UUID_OK,
                                      "subagents", "agent-ff01aa.jsonl"),
                         [cl_user_prompt(NOW - 30)])
        for bad in ("../etc/passwd", "xyz", "GGGG-GGGG-GGGG", ""):
            res = self.run_tele([self.agent("claude", {
                "agent": "claude", "kind": "id", "source": "herdr:claude",
                "value": bad})])[0]
            self.assertIsNone(res)


# ---------- v3 phase + events ----------
class PhaseAndEvents(Base):
    def pi(self, path):
        return {"agent": "pi", "agent_status": "working",
                "agent_session": {"agent": "pi", "kind": "path",
                                  "source": "herdr:pi", "value": path}}

    def cache_bytes(self):
        with open(self.cache, "rb") as f:
            return f.read()

    def test_bash_testing_phase_and_the_secret_never_leaks(self):
        path = self.write_jsonl(os.path.join(
            self.home, ".pi", "agent", "sessions", "proj", "ph1.jsonl"), [
            pi_session_line(NOW - 60),
            pi_user(NOW - 60, "SECRET-PROMPT run the tests"),
            pi_assistant(NOW - 50, pi_usage(1, 1, 0, 0, 0.0),
                         [pi_tool_call("t1", "bash", NOW - 50,
                                       {"command": "pytest -k "
                                        "SECRET_TOKEN_abc123 tests/"})]),
        ])
        res = self.run_tele([self.pi(path)])[0]
        self.assertEqual(res["tool"], "bash")
        self.assertEqual(res["phase"], "testing")   # only the word survives
        self.assertEqual(res["activity"], "Running a command")
        wire = json.dumps(res)
        cache = self.cache_bytes().decode("utf-8", "replace")
        for where in (wire, cache):
            for needle in ("SECRET_TOKEN_abc123", "pytest", "SECRET-PROMPT"):
                self.assertNotIn(needle, where)

    def test_plain_bash_command_is_tool_phase(self):
        path = self.write_jsonl(os.path.join(
            self.home, ".pi", "agent", "sessions", "proj", "ph2.jsonl"), [
            pi_session_line(NOW - 60),
            pi_user(NOW - 60, "go"),
            pi_assistant(NOW - 50, pi_usage(1, 1, 0, 0, 0.0),
                         [pi_tool_call("t1", "bash", NOW - 50,
                                       {"command": "curl "
                                        "http://x/SECRET_TOKEN_abc123"})]),
        ])
        res = self.run_tele([self.pi(path)])[0]
        self.assertEqual(res["phase"], "tool")
        self.assertNotIn("SECRET_TOKEN_abc123", json.dumps(res))
        self.assertNotIn("SECRET_TOKEN_abc123",
                         self.cache_bytes().decode("utf-8", "replace"))

    def test_phase_table(self):
        base = os.path.join(self.home, ".pi", "agent", "sessions", "proj")
        cases = [
            ("edit", {"path": "/home/u/SECRET_DIR/x.kt"}, "working",
             "editing"),
            ("read", {"file_path": "/home/u/SECRET_DIR/x.kt"}, "working",
             "reviewing"),
            ("bash", {"command": "ls -la"}, "working", "tool"),
        ]
        for i, (tool, args, status, want) in enumerate(cases):
            path = self.write_jsonl(os.path.join(base, "pt%d.jsonl" % i), [
                pi_session_line(NOW - 60),
                pi_user(NOW - 60, "go"),
                pi_assistant(NOW - 50, pi_usage(1, 1, 0, 0, 0.0),
                             [pi_tool_call("t1", tool, NOW - 50, args)]),
            ])
            res = self.run_tele([self.pi(path)])[0]
            self.assertEqual(res["phase"], want, tool)
        # no open tool: thinking while working, absent otherwise
        path = self.write_jsonl(os.path.join(base, "pt9.jsonl"), [
            pi_session_line(NOW - 60), pi_user(NOW - 60, "go")])
        res = self.run_tele([self.pi(path)])[0]
        self.assertEqual(res["phase"], "thinking")
        blocked = dict(self.pi(path))
        blocked["agent_status"] = "blocked"
        res = self.run_tele([blocked])[0]
        self.assertNotIn("phase", res)

    def test_bash_phase_word_unit_table(self):
        f = tt._bash_phase_word
        bash = lambda cmd: f("bash", {"command": cmd})
        self.assertEqual(bash("pytest -k x"), "testing")
        self.assertEqual(bash("/usr/bin/pytest -q"), "testing")
        self.assertEqual(bash("gradlew check"), "testing")
        self.assertEqual(bash("unittest discover"), "testing")
        self.assertEqual(bash("run_suite.py"), "testing")
        self.assertEqual(bash("npm test"), "testing")
        self.assertEqual(bash("pnpm test --run"), "testing")
        self.assertEqual(bash("yarn test"), "testing")
        self.assertEqual(bash("cargo test --lib"), "testing")
        self.assertEqual(bash("go test ./..."), "testing")
        self.assertEqual(bash("make test"), "testing")
        self.assertEqual(bash("npm run build"), None)
        self.assertEqual(bash("python3 -m pytest"), None)  # argv0 basename
        self.assertEqual(bash("ls -la"), None)
        self.assertEqual(bash("pytest -k 'unbalanced"), "testing")  # shlex
        self.assertEqual(f("bash", {"command": "x" * 5000}), None)
        self.assertEqual(f("bash", {"command": ""}), None)
        self.assertEqual(f("bash", None), None)
        self.assertEqual(f("edit", {"command": "pytest"}), None)

    def test_events_are_capped_at_twelve_newest_last_with_valid_kinds(self):
        lines = [pi_session_line(NOW - 800)]
        ts = NOW - 700
        for _ in range(20):                    # 40 events: P then R pairs
            lines.append(pi_user(ts, "SECRET-PROMPT"))
            lines.append(pi_assistant(ts + 1, pi_usage(1, 1, 0, 0, 0.0),
                                      [{"type": "text", "text": "x"}]))
            ts += 2
        lines.append(pi_assistant(NOW - 60, pi_usage(1, 1, 0, 0, 0.0),
                                  [pi_tool_call("d1", "subagent_worker",
                                                NOW - 60,
                                                {"agent": "Explore"})]))
        lines.append(pi_assistant(NOW - 50, pi_usage(1, 1, 0, 0, 0.0),
                                  [pi_tool_call("t1", "bash", NOW - 50,
                                                {"command": "SECRET-CMD"})]))
        lines.append(pi_tool_result(NOW - 45, "t1", "bash"))
        lines.append(pi_tool_result(NOW - 40, "t1b", "bash", error=True))
        lines.append(pi_assistant(NOW - 30, pi_usage(1, 1, 0, 0, 0.0),
                                  [{"type": "text", "text": "x"}]))
        path = self.write_jsonl(os.path.join(
            self.home, ".pi", "agent", "sessions", "proj", "ev.jsonl"),
            lines)
        res = self.run_tele([self.pi(path)])[0]
        events = res["events"]
        self.assertEqual(len(events), 12)
        valid = {"prompt", "reply", "tool", "error", "subagent"}
        for e in events:
            # §7.2: subagent events may carry name/status; tool events are
            # COMPLETIONS (the open above emits no wire event).
            self.assertTrue(set(e) <= {"kind", "tool", "at", "name",
                                       "status"})
            self.assertIn(e["kind"], valid)
            self.assertIsInstance(e["at"], int)
        ats = [e["at"] for e in events]
        self.assertEqual(ats, sorted(ats))             # newest LAST
        self.assertEqual(events[-1], {"kind": "reply", "at": NOW - 30})
        kinds = {e["kind"] for e in events}
        self.assertTrue({"prompt", "reply", "tool", "error",
                         "subagent"} <= kinds)
        self.assertIn({"kind": "tool", "tool": "bash", "at": NOW - 45},
                      events)                  # a completion, not the open
        self.assertIn({"kind": "error", "tool": "bash", "at": NOW - 40},
                      events)
        self.assertIn({"kind": "subagent", "tool": "subagent_worker",
                       "name": "Explore", "at": NOW - 60}, events)
        self.assertNotIn("SECRET-CMD", json.dumps(res))



class Provenance71(ChildBase):
    """§7.1 provider/adapter/billing/context_window — bridges are never
    providers, bridges are never metered, cost_usd exists only for
    metered billing, and context windows come only from explicit local
    registries (models-store / installed bridge policy), never a table."""

    MODELS_TS = '''// fixture mirroring the installed bridge policy shape
const TWO_HUNDRED_K_CONTEXT = 200_000;
const ONE_M_CONTEXT = 1_000_000;
const MEASURED_ONE_M = new Set([
\t"claude-opus-5-5",
\t"claude-sonnet-5-5",
]);
const PLAN_GATED_ONE_M: Record<string, (settings: LongContextSettings) => boolean> = {
\t"claude-opus-4-6": (settings) => settings.plan === "max" || settings.longContextExtraUsage,
\t"claude-sonnet-4-6": (settings) => settings.longContextExtraUsage,
};
'''

    def setUp(self):
        super().setUp()
        tt._REG_CACHE.clear()
        self.addCleanup(tt._REG_CACHE.clear)
        self.base = self.proj

    def write(self, rel, text):
        p = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    SEQ = [0]

    def session(self, provider, model="glm-test-5", total=0.0):
        # a fresh file per session: production transcripts are append-only,
        # so a rewrite that GROWS in place would only have its tail scanned.
        Provenance71.SEQ[0] += 1
        self.last_path = os.path.join(self.base, "p%d.jsonl"
                                      % Provenance71.SEQ[0])
        return self.write_jsonl(self.last_path, [
            pi_session_line(NOW - 60),
            pi_model_change(NOW - 60, model=model, provider=provider),
            pi_user(NOW - 50, "go"),
            pi_assistant(NOW - 40, pi_usage(10, 5, 0, 0, total),
                         [{"type": "text", "text": "x"}],
                         model=model, provider=provider)])

    def run_one(self):
        return self.run_tele([{ "agent": "pi", "agent_status": "working",
                                "agent_session": {
                                    "agent": "pi", "kind": "path",
                                    "source": "herdr:pi",
                                    "value": self.last_path}}])[0]

    def test_claude_bridge_is_anthropic_via_bridge_never_metered(self):
        self.session("claude-bridge", model="claude-opus-5-5")
        res = self.run_one()
        self.assertEqual(res["provider"], "anthropic")
        self.assertEqual(res["adapter"], "claude-bridge")
        self.assertEqual(res["billing"], "provider_managed")
        self.assertNotIn("cost_usd", res)      # NEVER a bridge cost

    def test_pi_metered_positive_cost_and_unknown_zero_cost(self):
        self.session("zai", total=0.0054)
        res = self.run_one()
        self.assertEqual(res["provider"], "zai")
        self.assertNotIn("adapter", res)
        self.assertEqual(res["billing"], "metered")
        self.assertEqual(res["cost_usd"], 0.0054)
        self.session("zai", total=0.0)
        res = self.run_one()                   # zero cost with tokens
        self.assertEqual(res["billing"], "unknown")
        self.assertNotIn("cost_usd", res)

    def test_optollm_maps_to_nothing_but_cost_stays_honest(self):
        self.session("optollm", model="some-model", total=0.25)
        res = self.run_one()
        self.assertNotIn("provider", res)      # not mapped unambiguously
        self.assertNotIn("adapter", res)
        self.assertEqual(res["billing"], "metered")
        self.assertEqual(res["cost_usd"], 0.25)

    def test_claude_oauth_is_provider_managed_without_cost(self):
        self.write(".claude/.credentials.json",
                   '{"claudeAiOauth": {"accessToken": "x"}}')
        _d, path = cl_session_dir(self.home, UUID_OK2)
        self.write_jsonl(path, [cl_user_prompt(NOW - 60),
                                cl_cost_state(NOW - 50, 0.75)])
        res = self.run_tele([self.agent("claude",
                                        self.agent_sess(UUID_OK2))])[0]
        self.assertEqual(res["provider"], "anthropic")
        self.assertEqual(res["billing"], "provider_managed")
        self.assertNotIn("cost_usd", res)      # subscription route
        self.assertNotIn("x", json.dumps(res))  # no credential values

    def test_claude_without_oauth_is_metered_on_real_cost(self):
        _d, path = cl_session_dir(self.home, UUID_OK2)
        self.write_jsonl(path, [cl_user_prompt(NOW - 60),
                                cl_cost_state(NOW - 50, 0.0)])
        res = self.run_tele([self.agent("claude",
                                        self.agent_sess(UUID_OK2))])[0]
        self.assertEqual(res["billing"], "unknown")
        self.assertNotIn("cost_usd", res)

    def test_context_window_from_models_store_only(self):
        self.write(".pi/agent/models-store.json", json.dumps({
            "zai": {"models": [{"id": "glm-test-5",
                                "contextWindow": 204800}]},
            "openai-codex": {"models": [{"id": "gpt-5.x",
                                         "contextWindow": 272000}]}}))
        self.session("zai")
        res = self.run_one()
        self.assertEqual(res["context_window"], 204800)
        self.assertEqual(res["context_window_source"], "pi_models_store")

    def test_unknown_model_has_no_context_window(self):
        self.write(".pi/agent/models-store.json", json.dumps({
            "zai": {"models": [{"id": "glm-test-5",
                                "contextWindow": 204800}]}}))
        self.session("zai", model="never-seen-model")
        res = self.run_one()
        self.assertNotIn("context_window", res)
        self.assertNotIn("context_window_source", res)

    def test_bridge_window_from_installed_policy(self):
        self.write(".pi/agent/npm/node_modules/pi-claude-bridge/"
                   "src/models.ts", self.MODELS_TS)
        self.write(".pi/agent/claude-bridge.json",
                   '{"askClaude":{"enabled":false},'
                   '"provider":{"plan":"pro"}}')
        self.session("claude-bridge", model="claude-opus-5-5")
        res = self.run_one()
        self.assertEqual(res["context_window"], 1000000)
        self.assertEqual(res["context_window_source"], "bridge_config")
        # unmeasured model: the MEASURED bare-id window, not a placeholder
        self.session("claude-bridge", model="claude-haiku-9")
        self.assertEqual(self.run_one()["context_window"], 200000)
        # plan-gated: opus-4-6 needs max or extra usage
        self.session("claude-bridge", model="claude-opus-4-6")
        self.assertEqual(self.run_one()["context_window"], 200000)
        p = self.write(".pi/agent/claude-bridge.json",
                       '{"provider":{"plan":"max"}}')
        os.utime(p, None)                     # fresh mtime for the registry
        self.assertEqual(self.run_one()["context_window"], 1000000)
        # forceTwoHundredK pins even a measured id
        p = self.write(".pi/agent/claude-bridge.json",
                       '{"provider":{"plan":"max",'
                       '"forceTwoHundredK":["claude-opus-5-5"]}}')
        os.utime(p, None)
        self.session("claude-bridge", model="claude-opus-5-5")
        self.assertEqual(self.run_one()["context_window"], 200000)

    def test_registry_cache_is_mtime_keyed(self):
        self.write(".pi/agent/models-store.json", json.dumps({
            "zai": {"models": [{"id": "glm-test-5",
                                "contextWindow": 204800}]}}))
        self.session("zai")
        self.assertEqual(self.run_one()["context_window"], 204800)
        p = self.write(".pi/agent/models-store.json", json.dumps({
            "zai": {"models": [{"id": "glm-test-5",
                                "contextWindow": 1000000}]}}))
        os.utime(p, (time.time() + 5, time.time() + 5))   # fresh mtime
        self.assertEqual(self.run_one()["context_window"], 1000000)

    def test_child_inherits_parent_route_and_billing(self):
        parent = self.parent("pc.jsonl", [
            pi_session_line(NOW - 200), pi_user(NOW - 200, "go")])
        rid = "c0000001-1111-4222-8333-444455556666"
        child = self.run_child(parent, rid, [
            pi_session_header(NOW - 150, id="x"),
            pi_assistant(NOW - 100, pi_usage(1000, 50, 900, 100, 0.0),
                         [{"type": "text", "text": "x"}],
                         model="claude-opus-5-5",
                         provider="claude-bridge")])
        self.meta(child, rid, {"runId": rid, "agent": "worker",
                               "exitCode": 0})
        k = self.run_tele([self.pi_agent(parent)])[0]["children"][0]
        self.assertEqual(k["provider"], "anthropic")
        self.assertEqual(k["adapter"], "claude-bridge")
        self.assertEqual(k["billing"], "provider_managed")
        self.assertNotIn("cost_usd", k)

    def test_no_registry_files_emit_no_window_keys(self):
        self.session("zai")
        res = self.run_one()
        self.assertNotIn("context_window", res)
        self.assertNotIn("context_window_source", res)


class CacheWrites(Base):
    """Security review: the telemetry cache writer must use a unique
    mkstemp name (a fixed `.tmp` lets two interleaved writers corrupt
    each other), fsync, os.replace and land at 0600."""

    def test_stale_fixed_tmp_never_poisons_writes(self):
        os.makedirs(os.path.dirname(self.cache), exist_ok=True)
        with open(self.cache + ".tmp", "w") as f:
            f.write('{"stale": ')               # leftover from an old writer
        tt._save_cache(self.cache, {"a": 1})
        tt._save_cache(self.cache, {"a": 2})
        with open(self.cache) as f:
            self.assertEqual(json.load(f), {"a": 2})

    def test_concurrent_writers_never_produce_corrupt_json(self):
        docs = [{"writer": i} for i in range(2)]
        errors = []

        def writer(doc):
            try:
                for _ in range(40):
                    tt._save_cache(self.cache, doc)
            except Exception as exc:            # must never raise
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(d,)) for d in docs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with open(self.cache) as f:             # valid JSON, one writer won
            self.assertIn(json.load(f), docs)
        litter = [f for f in os.listdir(os.path.dirname(self.cache))
                  if f.startswith(".telemetry-")]
        self.assertEqual(litter, [])

    def test_cache_written_0600(self):
        tt._save_cache(self.cache, {"a": 1})
        self.assertEqual(oct(os.stat(self.cache).st_mode & 0o777), oct(0o600))
