"""Schema v2 telemetry: tendril_telemetry + its wiring into the snapshot.

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
  — only tool names and sanitized file basenames inside `activity`.
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
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
        self.assertNotIn("provider", res)                # not recorded: omitted

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
class SnapshotV2Wiring(unittest.TestCase):
    """End-to-end: `remote-agents snapshot` emits version 2 with telemetry."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="tendril-tel-e2e-")
        self.cache = os.path.join(self.home, ".cache", "telemetry-v1.json")
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

    def test_snapshot_embeds_v2_telemetry(self):
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
               mock.patch.object(ra.sys, "stderr", self.err)]
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
        self.assertEqual(doc["version"], 2)
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
        # codex: harness only
        self.assertEqual(codex["harness"], "codex")
        for k in ("model", "provider", "effort", "started_at",
                  "context_tokens", "output_tokens", "cost_usd", "tool",
                  "tool_started_at", "activity", "last_event", "trail"):
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
        self.assertEqual(doc["version"], 2)
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
