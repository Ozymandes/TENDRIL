"""Session titles (DESIGN-FINAL-POLISH §11): bin/tendril_titles.py +
the `remote-agents rename-session` / `titles backfill` verbs + the
snapshot's additive `title`/`title_source` wire fields.

Under test here:
- the deterministic deriver (levels 4-5) against the SHARED fixture
  bin/title-vectors.json — the same vectors the TypeScript extension
  (pi/tendril-session-title.ts) must pass (node parity test below);
- wire sanitization (<= 48, control chars stripped, never empty);
- the FULL precedence chain (manual > native > scheduler > instruction >
  summary > workspace > omit), including: a scheduler routing token
  (`tendril-<id8>`) never passing as a native title, a Herdr agent name's
  `-<hash>` suffix stripped, and the session_info native name read from a
  real JSONL transcript;
- STABILITY: once resolved at levels 3-5 a title is persisted and never
  recomputed (a changed transcript does not move it; only manual does);
- the PANE-REUSE guard: cache entries pin the session's own start — a
  reused pane (new transcript in the same pane id) never inherits;
- CLAUDE CODE panes (agent_session {kind:"id", value:<uuid>}): the
  transcript is located under ~/.claude/projects/<project>/<uuid>.jsonl and
  resolved through the same precedence — a native summary / custom-title
  entry wins (sanitized, latest-in-file), else the first meaningful user
  message derives (isMeta, isSidechain, command-wrapper and tool_result-only
  entries are skipped), else the salient-term summary, else the workspace
  fallback; backfill covers Claude transcripts too (cache-only);
- rename validation (bad pane, blind pane, long title, control chars,
  --clear) and the pending-native queue (TTL, pane filter, consumption);
- backfill: one-shot idempotence, --dry-run writes nothing, named and
  child sessions are skipped, existing JSONL is never appended to;
- snapshot wiring: annotate_document fills agents and children, respects
  the workspace fallback, and _enforce_doc_cap strips titles LAST.

HOME/state is a tempdir; every transcript is written by the tests. No real
herdr, no network, no LLM.
"""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(REPO, "bin")
CLI = os.path.join(BIN, "remote-agents")
VECTORS = os.path.join(BIN, "title-vectors.json")
EXT = os.path.join(REPO, "pi", "tendril-session-title.ts")


def load_mod(name, path):
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


tt = load_mod("tendril_titles_under_test", os.path.join(BIN, "tendril_titles.py"))
ra = load_mod("remote_agents_under_test", CLI)


def _pi_session(ts_epoch, instr, name=None, parent=None, session_id=None):
    """One minimal Pi transcript: header + (session_info?) + user message."""
    hdr_id = session_id or "01a1test00000000000000000000000aa"
    lines = [json.dumps({
        "type": "session", "version": 3, "id": hdr_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                   time.gmtime(ts_epoch)),
        "cwd": "/home/x", **({"parentSession": parent} if parent else {}),
    })]
    if name:
        lines.append(json.dumps({
            "type": "session_info", "id": "si1",
            "parentId": None,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                       time.gmtime(ts_epoch + 1)),
            "name": name,
        }))
    lines.append(json.dumps({
        "type": "message", "id": "m1", "parentId": None,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                   time.gmtime(ts_epoch + 2)),
        "message": {"role": "user", "content": [
            {"type": "text", "text": instr}]},
    }))
    return "\n".join(lines) + "\n"


def _cl_session(ts_epoch, instr):
    """One minimal Claude transcript: a type:user row."""
    return json.dumps({
        "type": "user", "timestamp": time.strftime(
            "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts_epoch)),
        "message": {"role": "user", "content": instr},
    }) + "\n"


def _cl_ts(ts_epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts_epoch))


def _cl_user(ts_epoch, content, uuid="cu", meta=False, side=False):
    """One Claude Code user entry (string or block content)."""
    return {"parentUuid": None, "isSidechain": side, "type": "user",
            "message": {"role": "user", "content": content},
            "uuid": uuid, "timestamp": _cl_ts(ts_epoch), "isMeta": meta,
            "userType": "external", "cwd": "/home/x", "version": "1.0.0"}


class TitlesTestBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="titles-home-")
        self.state = os.path.join(self.home, ".local", "state", "tendril")
        os.makedirs(self.state, mode=0o700, exist_ok=True)
        self.cache = os.path.join(self.state, "session-titles.json")
        self.pending = os.path.join(self.state, "pending-titles.json")
        self._orig = (tt.HOME, tt.STATE_DIR, tt.CACHE_FILE, tt.PENDING_FILE,
                      tt.PI_SESSIONS_DIR)
        tt.HOME = self.home
        tt.STATE_DIR = self.state
        tt.CACHE_FILE = self.cache
        tt.PENDING_FILE = self.pending
        tt.PI_SESSIONS_DIR = os.path.join(self.home, ".pi", "agent", "sessions")
        tt._MEMO.clear()
        tt._child_reads[0] = 0

    def tearDown(self):
        (tt.HOME, tt.STATE_DIR, tt.CACHE_FILE, tt.PENDING_FILE,
         tt.PI_SESSIONS_DIR) = self._orig
        tt._MEMO.clear()
        shutil.rmtree(self.home, ignore_errors=True)

    def write_pi(self, rel, body, mtime=None):
        path = os.path.join(tt.PI_SESSIONS_DIR, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def write_claude(self, uuid, entries, proj="proj-x", mtime=None):
        """One synthetic Claude Code transcript under the temp HOME."""
        path = os.path.join(self.home, ".claude", "projects", proj,
                            uuid + ".jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path


class DeriverVectors(TitlesTestBase):
    """The shared contract: bin/title-vectors.json drives BOTH derivers."""

    def test_shared_vectors_python(self):
        with open(VECTORS, encoding="utf-8") as f:
            fx = json.load(f)
        self.assertGreaterEqual(len(fx["derive"]), 30, "spec: >= 30 vectors")
        for v in fx["derive"]:
            self.assertEqual(tt.derive_title(v["input"]), v["expect"],
                             "vector %s" % v["id"])
        for v in fx["summary"]:
            self.assertEqual(tt.derive_summary(v["input"]), v["expect"],
                             "summary vector %s" % v["id"])

    def test_typescript_parity_via_node(self):
        if shutil.which("node") is None:
            self.skipTest("node not available")
        r = subprocess.run(["node", os.path.join(REPO, "pi",
                                                 "deriver-test.mjs")],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0,
                         "TS deriver parity failed:\n%s%s"
                         % (r.stdout, r.stderr))
        self.assertIn("vectors pass", r.stdout)

    def test_secrets_never_survive(self):
        for blob in ("sk-live-9f4Kj2mN8pQ1zX7wL5rT3vB8yCd0AaE6",
                     "ghp_9f4Kj2mN8pQ1zX7wL5rT3vB8yCd0AaE6",
                     "9f4Kj2mN8pQ1zX7wL5rT3vB8yCd0AaE6",):
            for wrap in ("use %s", "%s", "run with %s now please"):
                title = tt.derive_title(wrap % blob)
                self.assertTrue(title is None or blob not in title,
                                "secret leaked: %s" % blob)
            # credential-shaped long tokens are noise, never title words:
            self.assertIsNone(tt.derive_title(blob))

    def test_cap_and_word_boundary(self):
        t = tt.derive_title("fix the alpha beta gamma delta epsilon zeta eta "
                            "theta iota kappa lambda mu nu xi")
        self.assertIsNotNone(t)
        self.assertLessEqual(len(t), tt.DERIVE_CAP)
        self.assertFalse(t.endswith(" "))      # never a partial word + space


class Sanitize(unittest.TestCase):
    def test_control_chars_stripped_and_capped(self):
        self.assertEqual(tt.sanitize_title("  a\x01b\u202ec "), "abc")
        self.assertEqual(tt.sanitize_title("hello  world"), "hello world")
        self.assertEqual(tt.sanitize_title("x" * 99), "x" * 48)
        self.assertIsNone(tt.sanitize_title("   "))
        self.assertIsNone(tt.sanitize_title(None))
        self.assertIsNone(tt.sanitize_title(42))


class Precedence(TitlesTestBase):
    def _agent(self, path=None, harness="pi", name=None, pane="w9:pA"):
        a = {"pane_id": pane, "agent": harness, "agent_status": "idle",
             "name": name}
        if path:
            a["agent_session"] = {"agent": harness, "kind": "path",
                                  "source": "herdr:pi", "value": path}
        return a

    def test_native_session_info_wins_over_derived(self):
        path = self.write_pi("w/--home-x--/a.jsonl", _pi_session(
            1_800_000_000, "Rewrite the pairing bloom timings",
            name="Pairing bloom polish"))
        title, source = tt.resolve_agent(self._agent(path), "tendril", [],
                                         titles={}, home=self.home)
        self.assertEqual((title, source), ("Pairing bloom polish", "native"))

    def test_scheduler_token_never_native_but_level3_wins(self):
        path = self.write_pi("w/--home-x--/b.jsonl", _pi_session(
            1_800_000_000, "Audit the capacity adapter math"))
        agent = self._agent(path, name="tendril-4be22c0a")
        tasks = [{"pane_id": "w9:pA", "title": "Capacity adapter audit",
                  "state": "RUNNING", "started_at": 1_800_000_500}]
        title, source = tt.resolve_agent(agent, "tendril", tasks,
                                         titles={}, home=self.home)
        self.assertEqual((title, source),
                         ("Capacity adapter audit", "scheduler"))

    def test_scheduler_token_without_task_falls_to_instruction(self):
        path = self.write_pi("w/--home-x--/c.jsonl", _pi_session(
            1_800_000_000, "Audit the capacity adapter math"))
        title, source = tt.resolve_agent(
            self._agent(path, name="tendril-4be22c0a"), "tendril", [],
            titles={}, home=self.home)
        self.assertEqual((title, source),
                         ("Audit the capacity adapter math", "instruction"))

    def test_herdr_name_hash_stripped_is_native(self):
        path = self.write_pi("w/--home-x--/d.jsonl", _pi_session(
            1_800_000_000, "Fix the pairing bloom"))
        title, source = tt.resolve_agent(
            self._agent(path, name="focus-runner-9a1b2c"), "tendril", [],
            titles={}, home=self.home)
        self.assertEqual((title, source), ("Focus runner", "native"))

    def test_role_id_native_never_shown_raw(self):
        """A machine-assigned `<role>#<id8>` session name is a ROLE, never a
        title: humanized, plus the short task label from the session's own
        first instruction (review round 2)."""
        path = self.write_pi("w/--home-x--/role.jsonl", _pi_session(
            1_800_000_000, "Audit the capacity adapter math"))
        title, source = tt.resolve_agent(self._agent(path), "tendril", [],
                                         titles={}, home=self.home)
        # the transcript carries a session_info name backend-scout#2cbfa86a
        # in this variant:
        path2 = self.write_pi("w/--home-x--/role2.jsonl", _pi_session(
            1_800_000_000, "Audit the capacity adapter math",
            name="backend-scout#2cbfa86a"))
        title2, source2 = tt.resolve_agent(self._agent(path2), "tendril", [],
                                           titles={}, home=self.home)
        self.assertEqual(title2, "Backend scout · Audit the capacity adapt")
        self.assertEqual(source2, "instruction")
        self.assertTrue(title is not None and "#" not in title)

    def test_role_only_native_gets_short_label(self):
        """Bare harness role names ("Explore", "Plan", "General purpose") are
        a ROLE, never a title: humanized, plus the first instruction's short
        label when one derives, ≤ 32 chars total (round 3, category 4)."""
        for role in ("Explore", "Plan", "General purpose",
                     "general-purpose"):
            path = self.write_pi(
                "w/--home-x--/roleonly-%s.jsonl" % role.replace(" ", "-"),
                _pi_session(1_800_000_000, "Audit the capacity adapter math",
                            name=role))
            title, source = tt.resolve_agent(self._agent(path), "tendril",
                                             [], titles={}, home=self.home)
            humanized = tt.humanize_role(role)
            self.assertTrue(title.startswith(humanized + " ·"),
                            "%s: %s" % (role, title))
            self.assertLessEqual(len(title), 32)
            self.assertEqual(source, "instruction")
        # no derivable label: the bare humanized role stays
        path = self.write_pi("w/--home-x--/roleonly-bare.jsonl", _pi_session(
            1_800_000_000, "ok", name="Explore"))
        title, source = tt.resolve_agent(self._agent(path), "tendril", [],
                                         titles={}, home=self.home)
        self.assertEqual((title, source), ("Explore", "native"))

    def test_negative_instruction_never_title(self):
        """Negative instructions state a constraint, never a topic: the
        clause is skipped for the next qualifying one, else None (round 3,
        category 1)."""
        self.assertIsNone(tt.derive_title("Do not touch the Tendril"))
        self.assertIsNone(tt.derive_title("Do not restart the audit"))
        self.assertEqual(
            tt.derive_title("Do not touch remote-agents. Rewrite the pane "
                            "registry locks"),
            "Rewrite the pane registry locks")

    def test_manual_beats_everything(self):
        path = self.write_pi("w/--home-x--/e.jsonl", _pi_session(
            1_800_000_000, "Rewrite the pairing bloom timings",
            name="Native name"))
        # the cache is keyed by the transcript's realpath (session_key)
        titles = {os.path.realpath(path): {"title": "Mine", "source": "manual",
                                           "session_start": 1_800_000_000}}
        title, source = tt.resolve_agent(self._agent(path), "tendril", [],
                                         titles=titles, home=self.home)
        self.assertEqual((title, source), ("Mine", "manual"))

    def test_summary_when_instruction_rejects(self):
        path = self.write_pi("w/--home-x--/f.jsonl", "\n".join([
            json.dumps({"type": "session", "version": 3, "id": "x1",
                        "timestamp": "2026-10-01T00:00:00.000Z",
                        "cwd": "/home/x"}),
            json.dumps({"type": "message", "id": "m1", "parentId": None,
                        "timestamp": "2026-10-01T00:00:01.000Z",
                        "message": {"role": "user", "content": "ok"}},
                       ),
            json.dumps({"type": "message", "id": "m2", "parentId": "m1",
                        "timestamp": "2026-10-01T00:01:01.000Z",
                        "message": {"role": "user", "content":
                                    "the CA field density drops near meters"}},
                       ),
        ]) + "\n")
        title, source = tt.resolve_agent(self._agent(path), "tendril", [],
                                         titles={}, home=self.home)
        self.assertEqual(source, "summary")
        self.assertTrue(title)

    def test_workspace_fallback_and_omit(self):
        agent = self._agent(None, harness="pi")
        title, source = tt.resolve_agent(agent, "tendril", [],
                                         titles={}, home=self.home)
        self.assertEqual((title, source), ("tendril · Pi", "workspace"))
        title, source = tt.resolve_agent(agent, "", [], titles={},
                                         home=self.home)
        self.assertEqual((title, source), (None, None))


class Stability(TitlesTestBase):
    def test_instruction_title_persisted_and_never_recomputed(self):
        path = self.write_pi("w/--home-x--/s.jsonl", _pi_session(
            1_800_000_000, "Stabilize the tendril attach handshake"))
        agent = {"pane_id": "w2:p1", "agent": "pi", "agent_status": "idle",
                 "agent_session": {"agent": "pi", "kind": "path",
                                   "source": "herdr:pi", "value": path}}
        titles = {}
        t1, s1 = tt.resolve_agent(agent, "ws", [], titles=titles,
                                  home=self.home)
        self.assertEqual(s1, "instruction")
        self.assertTrue(titles)                      # persisted in memory
        tt._save_cache(titles, self.cache)           # and on disk
        # the transcript grows a DIFFERENT first-looking instruction later:
        # the cached title must not move
        with open(path, "a", encoding="utf-8") as f:
            f.write(_pi_session(1_800_000_000, "totally other words now")
                    .split("\n", 2)[2] + "\n")
        titles2 = tt.load_cache(self.cache)
        t2, s2 = tt.resolve_agent(agent, "ws", [], titles=titles2,
                                  home=self.home)
        self.assertEqual((t2, s2), (t1, "instruction"))

    def test_manual_replaces_persisted_title(self):
        path = self.write_pi("w/--home-x--/s2.jsonl", _pi_session(
            1_800_000_000, "Stabilize the tendril attach handshake"))
        agent = {"pane_id": "w2:p1", "agent": "pi", "agent_status": "idle",
                 "agent_session": {"agent": "pi", "kind": "path",
                                   "source": "herdr:pi", "value": path}}
        titles = {}
        tt.resolve_agent(agent, "ws", [], titles=titles, home=self.home)
        ok, phrase = tt.rename_session("w2:p1", "Chosen label",
                                       agent=agent, home=self.home)
        self.assertTrue(ok)
        titles = tt.load_cache(self.cache)
        t, s = tt.resolve_agent(agent, "ws", [], titles=titles,
                                home=self.home)
        self.assertEqual((t, s), ("Chosen label", "manual"))


class PaneReuseGuard(TitlesTestBase):
    def test_reused_pane_never_inherits(self):
        old_start = 1_800_000_000
        old = self.write_pi("w/--home-x--/old.jsonl",
                            _pi_session(old_start, "Old session subject line"))
        agent = {"pane_id": "w3:p1", "agent": "pi", "agent_status": "idle",
                 "agent_session": {"agent": "pi", "kind": "path",
                                   "source": "herdr:pi", "value": old}}
        titles = {}
        t_old, _ = tt.resolve_agent(agent, "ws", [], titles=titles,
                                    home=self.home)
        tt._save_cache(titles, self.cache)
        # the pane is reused: a NEW transcript (different start) replaces it
        new_start = old_start + 9_000
        new = self.write_pi("w/--home-x--/old.jsonl", _pi_session(
            new_start, "Brand new era of work"), mtime=time.time())
        tt._MEMO.clear()
        titles = tt.load_cache(self.cache)
        agent["agent_session"]["value"] = new
        t_new, s_new = tt.resolve_agent(agent, "ws", [], titles=titles,
                                        home=self.home)
        self.assertNotEqual(t_new, t_old)
        self.assertEqual(s_new, "instruction")

    def test_cache_file_mode_is_0600(self):
        path = self.write_pi("w/--home-x--/perm.jsonl", _pi_session(
            1_800_000_000, "Check the cache file permissions please"))
        agent = {"pane_id": "w4:p1", "agent": "pi", "agent_status": "idle",
                 "agent_session": {"agent": "pi", "kind": "path",
                                   "source": "herdr:pi", "value": path}}
        tt.annotate_document(
            {"workspaces": [{"agents": [dict(agent)]}],
             "schedule": None},
            [{"label": "ws", "agents": [agent]}],
            home=self.home, cache_path=self.cache)
        self.assertEqual(stat.S_IMODE(os.stat(self.cache).st_mode), 0o600)


class ClaudeTitles(TitlesTestBase):
    """Claude Code panes: agent_session {kind:"id", value:<uuid>} — the
    transcript under ~/.claude/projects resolves through the SAME §11
    precedence as a Pi path (spec §11: the precedence applies to every
    harness)."""

    U1 = "11111111-2222-3333-4444-555566667777"
    U2 = "22222222-3333-4444-5555-666677778888"
    U3 = "33333333-4444-5555-6666-777788889999"
    INSTR = "Audit the capacity adapter math"
    T_INSTR = "Audit the capacity adapter math"

    def _agent(self, uuid, pane="w9:pA"):
        return {"pane_id": pane, "agent": "claude", "agent_status": "idle",
                "agent_session": {"agent": "claude", "kind": "id",
                                  "source": "herdr:claude",
                                  "value": uuid}}

    def _user(self, ts, content, uuid="cu", **kw):
        return _cl_user(ts, content, uuid=uuid, **kw)

    def test_native_summary_entry_wins(self):
        self.write_claude(self.U1, [
            self._user(1_800_000_000, self.INSTR, uuid="c1"),
            {"type": "summary", "summary": "Capacity audit session",
             "leafUuid": "leaf-1"},
        ])
        title, source = tt.resolve_agent(self._agent(self.U1), "tendril",
                                         [], titles={}, home=self.home)
        self.assertEqual((title, source), ("Capacity audit session", "native"))

    def test_native_custom_title_latest_in_file_wins(self):
        self.write_claude(self.U1, [
            self._user(1_800_000_000, self.INSTR, uuid="c1"),
            {"type": "ai-title", "aiTitle": "First attempt",
             "sessionId": self.U1},
            {"type": "ai-title", "aiTitle": "Second attempt",
             "sessionId": self.U1},
        ])
        title, source = tt.resolve_agent(self._agent(self.U1), "tendril",
                                         [], titles={}, home=self.home)
        self.assertEqual((title, source), ("Second attempt", "native"))

    def test_native_title_is_sanitized(self):
        self.write_claude(self.U1, [
            self._user(1_800_000_000, self.INSTR, uuid="c1"),
            {"type": "ai-title", "aiTitle": "  bad\x01title\u202e ",
             "sessionId": self.U1},
        ])
        title, source = tt.resolve_agent(self._agent(self.U1), "tendril",
                                         [], titles={}, home=self.home)
        self.assertEqual((title, source), ("badtitle", "native"))

    def test_first_user_message_derives_string_content(self):
        self.write_claude(self.U1, [
            self._user(1_800_000_000, self.INSTR, uuid="c1"),
        ])
        title, source = tt.resolve_agent(self._agent(self.U1), "tendril",
                                         [], titles={}, home=self.home)
        self.assertEqual((title, source), (self.T_INSTR, "instruction"))

    def test_first_user_message_derives_text_block_content(self):
        self.write_claude(self.U1, [
            self._user(1_800_000_000,
                       [{"type": "text", "text": self.INSTR}], uuid="c1"),
        ])
        title, source = tt.resolve_agent(self._agent(self.U1), "tendril",
                                         [], titles={}, home=self.home)
        self.assertEqual((title, source), (self.T_INSTR, "instruction"))

    def test_meta_command_sidechain_toolresult_skipped(self):
        """Only the first MEANINGFUL user message can name the session:
        meta rows, slash-command wrappers, sidechains and tool_result-only
        bodies never become the title."""
        self.write_claude(self.U1, [
            self._user(1_800_000_000, "Earlier unrelated meta content",
                       uuid="c-meta", meta=True),
            self._user(1_800_000_010,
                       "<command-name>/clear</command-name>\n"
                       "<local-command-stdout>cleared</local-command-stdout>",
                       uuid="c-cmd"),
            self._user(1_800_000_020, "Sidechain diversion content here",
                       uuid="c-side", side=True),
            self._user(1_800_000_030,
                       [{"type": "tool_result", "tool_use_id": "t1",
                         "content": "42"}], uuid="c-tool"),
            self._user(1_800_000_040,
                       [{"type": "text", "text": self.INSTR}],
                       uuid="c-real"),
        ])
        title, source = tt.resolve_agent(self._agent(self.U1), "tendril",
                                         [], titles={}, home=self.home)
        self.assertEqual((title, source), (self.T_INSTR, "instruction"))

    def test_uuid_lookup_across_project_dirs(self):
        """The project dir encodes the pane's cwd, unknown here: the
        transcript must be found in WHICHEVER project dir holds it."""
        self.write_claude(self.U1, [
            self._user(1_800_000_000, self.INSTR, uuid="c1"),
        ], proj="-home-seeno-Projects-somewhere-else")
        # an unrelated project dir exists too
        self.write_claude(self.U3, [], proj="-home-seeno-other")
        title, source = tt.resolve_agent(self._agent(self.U1), "tendril",
                                         [], titles={}, home=self.home)
        self.assertEqual((title, source), (self.T_INSTR, "instruction"))

    def test_missing_transcript_falls_to_workspace(self):
        title, source = tt.resolve_agent(self._agent(self.U2), "tendril",
                                         [], titles={}, home=self.home)
        self.assertEqual((title, source), ("tendril · Claude", "workspace"))

    def test_summary_over_first_three_turns_when_instruction_rejects(self):
        self.write_claude(self.U1, [
            self._user(1_800_000_000, "ok", uuid="c1"),
            self._user(1_800_000_100,
                       "the CA field density drops near meters", uuid="c2"),
        ])
        title, source = tt.resolve_agent(self._agent(self.U1), "tendril",
                                         [], titles={}, home=self.home)
        self.assertEqual(source, "summary")
        self.assertTrue(title)

    def test_pane_reuse_guard_and_start_pin(self):
        old_start = 1_800_000_000
        self.write_claude(self.U1, [
            self._user(old_start, "Old session subject line", uuid="c1"),
        ])
        agent = self._agent(self.U1, pane="w9:pA")
        titles = {}
        t_old, s_old = tt.resolve_agent(agent, "Leviathan", [],
                                        titles=titles, home=self.home)
        self.assertEqual(s_old, "instruction")
        tt._save_cache(titles, self.cache)
        # the pane is REUSED: a new Claude session (new UUID) replaces it
        new_start = old_start + 9_000
        self.write_claude(self.U2, [
            self._user(new_start, "Brand new era of work", uuid="c1"),
        ])
        titles = tt.load_cache(self.cache)
        entry = titles[self.U1]
        self.assertEqual(entry.get("session_start"), old_start)
        t_new, s_new = tt.resolve_agent(
            self._agent(self.U2, pane="w9:pA"),
            "Leviathan", [], titles=titles, home=self.home)
        self.assertNotEqual(t_new, t_old)
        self.assertEqual(s_new, "instruction")
        # the OLD session's title never moves, even as its transcript grows
        path = os.path.join(self.home, ".claude", "projects", "proj-x",
                            self.U1 + ".jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(self._user(
                old_start + 5_000, "Totally other words now",
                uuid="c-late")) + "\n")
        tt._MEMO.clear()
        titles = tt.load_cache(self.cache)
        t_again, s_again = tt.resolve_agent(self._agent(self.U1),
                                            "Leviathan", [], titles=titles,
                                            home=self.home)
        self.assertEqual((t_again, s_again), (t_old, "instruction"))

    def test_manual_override_beats_claude_levels(self):
        self.write_claude(self.U1, [
            self._user(1_800_000_000, self.INSTR, uuid="c1"),
            {"type": "ai-title", "aiTitle": "Machine title",
             "sessionId": self.U1},
        ])
        agent = self._agent(self.U1)
        ok, phrase = tt.rename_session("w9:pA", "Chosen by me", agent=agent,
                                       home=self.home)
        self.assertTrue(ok)
        titles = tt.load_cache(self.cache)
        self.assertIn(self.U1, titles)          # keyed by the session UUID
        title, source = tt.resolve_agent(agent, "tendril", [],
                                         titles=titles, home=self.home)
        self.assertEqual((title, source), ("Chosen by me", "manual"))

    def test_annotate_document_wires_claude_row(self):
        self.write_claude(self.U1, [
            self._user(1_800_000_000, self.INSTR, uuid="c1"),
        ])
        raw = self._agent(self.U1, pane="w9:pB")
        doc = {"workspaces": [{"label": "Leviathan", "agents": [
                {"pane_id": "w9:pB", "agent": "claude"}]}],
               "schedule": None}
        tt.annotate_document(doc, [{"label": "Leviathan", "agents": [raw]}],
                             home=self.home, cache_path=self.cache)
        a = doc["workspaces"][0]["agents"][0]
        self.assertEqual((a["title"], a["title_source"]),
                         (self.T_INSTR, "instruction"))
        self.assertLessEqual(len(a["title"]), 48)
        self.assertIn(self.U1, tt.load_cache(self.cache))


class ClaudeBackfill(TitlesTestBase):
    U1 = "aaaa1111-2222-3333-4444-555566667777"
    U2 = "bbbb2222-3333-4444-5555-666677778888"
    U3 = "cccc3333-4444-5555-6666-777788889999"

    def _write(self):
        p1 = None
        return (
            self.write_claude(self.U1, [
                _cl_user(1_800_000_000, "Filler instruction words here",
                         uuid="c1"),
                {"type": "ai-title", "aiTitle": "Native already",
                 "sessionId": self.U1},
            ]),
            self.write_claude(self.U2, [
                _cl_user(1_800_010_000, "Backfill the old sessions tonight",
                         uuid="c1"),
            ]),
            self.write_claude(self.U3, [
                _cl_user(1_800_020_000, "ok", uuid="c1"),
            ]),
        )

    def test_backfill_caches_claude_transcripts_only(self):
        paths = self._write()
        sizes = [os.path.getsize(p) for p in paths]
        rep1 = tt.backfill(dry_run=False, home=self.home,
                           cache_path=self.cache)
        self.assertEqual(rep1["claude"]["scanned"], 3)
        self.assertEqual(rep1["claude"]["skipped"]["native"], 1)
        self.assertEqual(rep1["claude"]["derived"]["instruction"], 1)
        self.assertEqual(rep1["claude"]["skipped"]["underived"], 1)
        titles = tt.load_cache(self.cache)
        self.assertIn(self.U2, titles)              # keyed by the UUID
        self.assertNotIn(self.U1, titles)
        self.assertNotIn(self.U3, titles)
        self.assertEqual(titles[self.U2]["source"], "instruction")
        # transcripts are NEVER touched
        self.assertEqual(sizes, [os.path.getsize(p) for p in paths])
        # idempotent: the second run re-derives nothing
        rep2 = tt.backfill(dry_run=False, home=self.home,
                           cache_path=self.cache)
        self.assertEqual(rep2["claude"]["derived"],
                         {"instruction": 0, "summary": 0})
        self.assertEqual(rep2["claude"]["skipped"]["cached"], 1)

    def test_backfill_dry_run_writes_nothing(self):
        self._write()
        rep = tt.backfill(dry_run=True, home=self.home,
                          cache_path=self.cache)
        self.assertTrue(rep["dry_run"])
        self.assertEqual(rep["claude"]["derived"]["instruction"], 1)
        self.assertFalse(os.path.exists(self.cache))


class Rename(TitlesTestBase):
    def _agent(self, path):
        return {"pane_id": "w5:p1", "agent": "pi", "agent_status": "idle",
                "agent_session": {"agent": "pi", "kind": "path",
                                  "source": "herdr:pi", "value": path}}

    def test_validation(self):
        path = self.write_pi("w/--home-x--/r.jsonl", _pi_session(
            1_800_000_000, "Some anchor instruction for the pane"))
        agent = self._agent(path)
        # bad pane
        ok, why = tt.rename_session("w5", "x", agent=agent, home=self.home)
        self.assertFalse(ok)
        # blind pane (no live row)
        ok, why = tt.rename_session("w5:p1", "x", agent=None, home=self.home)
        self.assertFalse(ok)
        # empty after sanitize
        ok, why = tt.rename_session("w5:p1", "\x01\x02", agent=agent,
                                    home=self.home)
        self.assertFalse(ok)
        # control chars stripped + cap at 48
        ok, phrase = tt.rename_session("w5:p1", "  fix\x01 the\x1b " + "y" * 60,
                                       agent=agent, home=self.home)
        self.assertTrue(ok)
        self.assertEqual(len(phrase), 48)

    def test_clear_removes_only_manual(self):
        path = self.write_pi("w/--home-x--/r2.jsonl", _pi_session(
            1_800_000_000, "Another anchor instruction for clearing"))
        agent = self._agent(path)
        tt.rename_session("w5:p1", "Temp", agent=agent, home=self.home)
        ok, _ = tt.rename_session("w5:p1", None, agent=agent, home=self.home)
        self.assertTrue(ok)
        titles = tt.load_cache(self.cache)
        self.assertNotIn(path, titles)
        # falls back through the chain again
        t, s = tt.resolve_agent(agent, "ws", [], titles=titles,
                                home=self.home)
        self.assertEqual(s, "instruction")

    def test_pending_native_enqueued_for_live_pi(self):
        path = self.write_pi("w/--home-x--/r3.jsonl", _pi_session(
            1_800_000_000, "Anchor instruction three"))
        agent = self._agent(path)
        tt.rename_session("w5:p1", "Live rename", agent=agent, home=self.home)
        got = tt.consume_native("w5:p1")
        self.assertEqual(got, "Live rename")
        self.assertIsNone(tt.consume_native("w5:p1"))   # consumed once


class PendingNative(TitlesTestBase):
    def test_pane_filter_and_ttl(self):
        self.assertTrue(tt.enqueue_native("w6:p1", "Scheduled title"))
        self.assertTrue(tt.enqueue_native("w6:p2", "Other pane"))
        self.assertIsNone(tt.consume_native("w6:p3"))
        self.assertEqual(tt.consume_native("w6:p2"), "Other pane")
        self.assertEqual(tt.consume_native("w6:p1"), "Scheduled title")
        # expired entries are never delivered
        doc = {"schema_version": 1, "entries": [
            {"pane_id": "w6:p4", "title": "Old", "kind": "manual",
             "ts": int(time.time()) - tt.PENDING_TTL_S - 5}]}
        tt._save_json(self.pending, doc)
        self.assertIsNone(tt.consume_native("w6:p4"))

    def test_refuses_bad_input(self):
        self.assertFalse(tt.enqueue_native("w6", "x"))
        self.assertFalse(tt.enqueue_native("w6:p1", ""))
        self.assertFalse(tt.enqueue_native("w6:p1", None))


class Backfill(TitlesTestBase):
    def test_one_shot_idempotent_and_never_appends(self):
        root = "2026-10-01T00-00-00-000Z_aaaa1111-2222-3333-4444-555566667777"
        path = self.write_pi(
            "%s/%s.jsonl" % ("--home-x--", root[:40]),
            _pi_session(1_800_000_000, "Backfill the old sessions tonight"))
        self.assertTrue(os.path.exists(path))
        before_size = os.path.getsize(path)
        named = self.write_pi(
            "--home-x--/2026-10-01T01-00-00-000Z_bbbb2222-3333-4444-5555-666677778888aaa.jsonl",
            _pi_session(1_800_010_000, "Already has a native name",
                        name="Named already"))
        child = self.write_pi(
            "--home-x--/cccc3333-4444-5555-6666-7777888899990000/run-0/session.jsonl",
            _pi_session(1_800_020_000, "Child sessions are skipped",
                        parent="/tmp/parent.jsonl"))
        rep1 = tt.backfill(dry_run=False, home=self.home,
                           cache_path=self.cache)
        self.assertEqual(rep1["derived"]["instruction"], 1)
        self.assertEqual(rep1["skipped"]["named"], 1)
        # child sessions live two levels deep (run dirs) — never scanned,
        # never cached
        self.assertEqual(rep1["skipped"]["child"], 0)
        self.assertNotIn(child, tt.load_cache(self.cache))
        titles = tt.load_cache(self.cache)
        self.assertIn(path, titles)
        self.assertNotIn(named, titles)
        # existing transcripts are NEVER appended to
        self.assertEqual(os.path.getsize(path), before_size)
        rep2 = tt.backfill(dry_run=False, home=self.home,
                           cache_path=self.cache)
        self.assertEqual(rep2["derived"]["instruction"], 0)
        self.assertEqual(rep2["derived"]["summary"], 0)
        self.assertEqual(rep2["skipped"]["cached"], 1)

    def test_dry_run_writes_nothing(self):
        self.write_pi(
            "--home-x--/2026-10-01T02-00-00-000Z_dddd4444-5555-6666-7777-888899990000.jsonl",
            _pi_session(1_800_030_000, "Dry run derives but never writes"))
        rep = tt.backfill(dry_run=True, home=self.home,
                          cache_path=self.cache)
        self.assertTrue(rep["dry_run"])
        self.assertEqual(rep["derived"]["instruction"], 1)
        self.assertFalse(os.path.exists(self.cache))


class SnapshotWiring(TitlesTestBase):
    def test_annotate_document_full_chain(self):
        path = self.write_pi("w/--home-x--/snap.jsonl", _pi_session(
            1_800_000_000, "Annotate the snapshot document correctly"))
        raw = {"pane_id": "w7:p1", "agent": "pi", "agent_status": "working",
               "agent_session": {"agent": "pi", "kind": "path",
                                 "source": "herdr:pi", "value": path}}
        doc = {
            "workspaces": [{"label": "tendril", "agents": [
                {"pane_id": "w7:p1", "agent": "pi"}]}],
            "schedule": {"tasks": []},
        }
        tt.annotate_document(doc, [{"label": "tendril", "agents": [raw]}],
                             home=self.home, cache_path=self.cache)
        a = doc["workspaces"][0]["agents"][0]
        self.assertEqual(a["title"], "Annotate the snapshot document")
        self.assertEqual(a["title_source"], "instruction")
        self.assertLessEqual(len(a["title"]), 48)

    def test_annotate_uses_scheduler_section(self):
        path = self.write_pi("w/--home-x--/snap2.jsonl", _pi_session(
            1_800_000_000, "Whatever this session started with earlier"))
        raw = {"pane_id": "w7:p2", "agent": "pi", "agent_status": "working",
               "name": "tendril-4be22c0a",
               "agent_session": {"agent": "pi", "kind": "path",
                                 "source": "herdr:pi", "value": path}}
        doc = {
            "workspaces": [{"label": "tendril", "agents": [
                {"pane_id": "w7:p2", "agent": "pi"}]}],
            "schedule": {"tasks": [{"pane_id": "w7:p2",
                                    "title": "Scheduler certification",
                                    "state": "RUNNING",
                                    "started_at": 1_800_000_500}]},
        }
        tt.annotate_document(doc, [{"label": "tendril", "agents": [raw]}],
                             home=self.home, cache_path=self.cache)
        a = doc["workspaces"][0]["agents"][0]
        self.assertEqual((a["title"], a["title_source"]),
                         ("Scheduler certification", "scheduler"))

    def test_child_titles(self):
        parent_stem = "eeee5555-6666-7777-8888-99990000aaaa1111"
        child = self.write_pi(
            "--home-x--/%s/ffff6666-7777-8888-9999-00001111aaaa2222/run-0/session.jsonl"
            % parent_stem,
            _pi_session(1_800_040_000, "Research the wire format questions"))
        title, source = tt.child_title(child, "researcher_ffff6666",
                                       home=self.home)
        self.assertEqual(title, "Researcher · Research the wire format")
        self.assertEqual(source, "instruction")
        # role-only children keep the humanized role, never "subagent"
        title, source = tt.child_title(None, "worker_ffff6666", home=self.home)
        self.assertEqual((title, source), ("Worker", "native"))

    def test_doc_cap_strips_titles_last(self):
        doc = {"workspaces": [{"agents": [
            {"pane_id": "w8:p1", "agent": "pi", "model": "m",
             "title": "T", "title_source": "instruction"}]}]}
        # shrink the cap until SOMETHING must go; titles go last
        for cap in (10_000, 1_000, 200, 120):
            ra._SNAPSHOT_MAX_BYTES = cap
            probe = json.loads(json.dumps(doc))
            ra._enforce_doc_cap(probe)
            a = probe["workspaces"][0]["agents"][0]
            self.assertIn("title", a)
        for cap in (60,):
            ra._SNAPSHOT_MAX_BYTES = cap
            probe = json.loads(json.dumps(doc))
            ra._enforce_doc_cap(probe)
            a = probe["workspaces"][0]["agents"][0]
            self.assertNotIn("title", a)
            self.assertNotIn("model", a)
        ra._SNAPSHOT_MAX_BYTES = 64 * 1024


class CliVerbs(TitlesTestBase):
    def test_backfill_cli_dry_run(self):
        self.write_pi(
            "--home-x--/2026-10-01T03-00-00-000Z_aaaabbbb-cccc-dddd-eeee-ffff00001111.jsonl",
            _pi_session(1_800_050_000, "Cli backfill dry run validation"))
        r = subprocess.run([sys.executable, CLI, "titles", "backfill",
                            "--dry-run"], capture_output=True, text=True,
                           timeout=60,
                           env={**os.environ, "HOME": self.home})
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(doc["dry_run"])
        self.assertEqual(doc["derived"]["instruction"], 1)
        self.assertFalse(os.path.exists(self.cache))

    def test_rename_cli_validates(self):
        # no herdr on PATH inside the test env: snapshot() fails → the verb
        # refuses with the designed phrase instead of writing blind
        r = subprocess.run([sys.executable, CLI, "rename-session",
                            "w9:pZ", "Nope"], capture_output=True, text=True,
                           timeout=60,
                           env={**os.environ, "HOME": self.home})
        self.assertIn(r.returncode, (1, 2))
        line = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        if line:
            doc = json.loads(line)
            self.assertFalse(doc["ok"])

    def test_rename_cli_rejects_bad_pane(self):
        r = subprocess.run([sys.executable, CLI, "rename-session",
                            "w9", "x"], capture_output=True, text=True,
                           timeout=60,
                           env={**os.environ, "HOME": self.home})
        self.assertEqual(r.returncode, 2)
        doc = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(doc["error"], "bad pane id")


class PhoneLauncher(unittest.TestCase):
    def test_launcher_passes_shell_checks(self):
        sh = shutil.which("sh")
        r = subprocess.run([sh, "-n", os.path.join(REPO, "phone", "tendril")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_rename_verb_validation(self):
        script = os.path.join(REPO, "phone", "tendril")
        for argv, expect_refuse in (
            (["rename", "w16:pA", "Hello world"], False),
            (["rename", "w16:pA", "--clear"], False),
            (["rename", "w16", "Hello"], True),
            (["rename", "w16:pA", "has 'quote'"], True),
            (["rename", "w16:pA", "x" * 49], True),
        ):
            r = subprocess.run(["/bin/sh", script,
                                "nonexistent-host-x"] + argv,
                               capture_output=True, text=True, timeout=60,
                               env={**os.environ})
            refused = r.returncode == 2 and "usage:" in (r.stdout + r.stderr)
            self.assertEqual(refused, expect_refuse,
                             "argv %r → rc=%s out=%s err=%s"
                             % (argv, r.returncode, r.stdout, r.stderr))


if __name__ == "__main__":
    unittest.main()
