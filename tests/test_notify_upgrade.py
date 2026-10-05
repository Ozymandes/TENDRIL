"""herdr-notify upgrade: composed alerts, nudges, deep-link plumbing, plugin.

Covers the deterministic composition helpers, the still-working nudge
(fires once per stint), the optional Click/Actions plumbing built on
tendril_link payloads, the local summarizer plugin point, and the poll/wach
loop mechanics (repeat window, pane cleanup, re-baseline after outage).
Fully hermetic: every path that would POST has urlopen mocked.
"""
import contextlib
import importlib.util
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTIFY = os.path.join(REPO, "bin", "herdr-notify")
TENDRIL_LINK = os.path.join(REPO, "bin", "tendril_link.py")


def load_module(name, path):
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


hn = load_module("herdr_notify_under_test", NOTIFY)
tl = load_module("tendril_link_under_test", TENDRIL_LINK)

T0 = 1000.0
CFG_NUDGE = {"NTFY_URL": "https://ntfy.example", "NTFY_TOPIC": "topic",
             "NTFY_WORKING_NUDGE_MINUTES": "45"}
CFG_CLICK = {"NTFY_URL": "https://ntfy.example", "NTFY_TOPIC": "topic",
             "NTFY_CLICK_TEMPLATE":
                 "https://phone.example/open?payload={payload_b64}&host={host}"}


def snap(agents, workspaces=None):
    return {"workspaces": workspaces
            if workspaces is not None else
            [{"workspace_id": "w1", "label": "research"}],
            "agents": agents}


def agent(status="working", pid="w1:p1", kind="pi", ws="w1",
          cwd="/home/op/research"):
    return {"pane_id": pid, "agent_status": status, "agent": kind,
            "workspace_id": ws, "cwd": cwd}


def home_env():
    """Deterministic expanduser(\"~\") -> /home/op for short_cwd."""
    return mock.patch.dict(os.environ, {"HOME": "/home/op"})


def drive_poll(steps, cfg, state=None):
    """Run poll() once per (snapshot, now) step with snapshot/time/urlopen
    mocked. Returns (state, [captured Request objects])."""
    reqs = []

    def fake_urlopen(req, timeout=10):
        reqs.append(req)
        return mock.MagicMock()

    state = {} if state is None else state
    for snapshot, now in steps:
        with home_env(), \
                mock.patch.object(hn, "snapshot", return_value=snapshot), \
                mock.patch.object(hn.time, "time", return_value=now), \
                mock.patch.object(hn.urllib.request, "urlopen",
                                  side_effect=fake_urlopen), \
                mock.patch.dict(hn.CONFIG, {"TAILSCALE_HOST": "testhost"},
                                clear=True), \
                contextlib.redirect_stdout(io.StringIO()):
            hn.poll(state, cfg, allow_push=True)
    return state, reqs


class HumanElapsed(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(hn.human_elapsed(45), "45s")
        self.assertEqual(hn.human_elapsed(59), "59s")
        self.assertEqual(hn.human_elapsed(60), "1m")
        self.assertEqual(hn.human_elapsed(1080), "18m")
        self.assertEqual(hn.human_elapsed(3540), "59m")
        self.assertEqual(hn.human_elapsed(3599), "59m")
        self.assertEqual(hn.human_elapsed(3600), "1h00m")
        self.assertEqual(hn.human_elapsed(3660), "1h01m")
        self.assertEqual(hn.human_elapsed(7320), "2h02m")

    def test_never_negative(self):
        self.assertEqual(hn.human_elapsed(-5), "0s")


class ShortCwd(unittest.TestCase):
    def test_home_collapse(self):
        with home_env():
            self.assertEqual(hn.short_cwd("/home/op/research"), "~/research")
            self.assertEqual(hn.short_cwd("/home/op"), "~")

    def test_long_path_keeps_last_two_components(self):
        with home_env():
            self.assertEqual(
                hn.short_cwd("/home/op/deep/nested/projects/subdir/x"),
                "…/subdir/x")

    def test_short_paths_kept_whole(self):
        with home_env():
            self.assertEqual(hn.short_cwd("~/research"), "~/research")
            self.assertEqual(hn.short_cwd("/usr/bin"), "/usr/bin")

    def test_empty(self):
        self.assertEqual(hn.short_cwd(""), "")
        self.assertEqual(hn.short_cwd(None), "")


class Compose(unittest.TestCase):
    def test_needs_input(self):
        with home_env():
            got = hn.compose("pi", "research", hn.NEEDS_INPUT, T0,
                             "/home/op/research", now=T0 + 1080)
        self.assertEqual(got, ("TENDRIL · research",
                               "PI needs input · 18m · ~/research",
                               "high", "rotating_light"))

    def test_done(self):
        with home_env():
            got = hn.compose("pi", "research", hn.DONE, T0,
                             "/home/op/research", now=T0 + 1320)
        self.assertEqual(got, ("TENDRIL · research",
                               "PI finished · 22m · ~/research",
                               "default", "white_check_mark"))

    def test_nudge(self):
        with home_env():
            got = hn.compose("pi", "research", hn.NUDGE, T0,
                             "/home/op/research", now=T0 + 2820)
        self.assertEqual(got, ("TENDRIL · research",
                               "PI still working · 47m · ~/research",
                               "min", "hourglass"))

    def test_missing_cwd_omits_segment(self):
        got = hn.compose("claude", "ws", hn.NEEDS_INPUT, T0, None, now=T0 + 60)
        self.assertEqual(got[1], "CLAUDE needs input · 1m")

    def test_long_label_truncated_with_ellipsis(self):
        got = hn.compose("pi", "x" * 70, hn.DONE, T0, None, now=T0 + 5)
        self.assertEqual(got[0], "TENDRIL · " + "x" * 60 + "…")

    def test_label_control_chars_sanitized(self):
        got = hn.compose("pi", "re\nsearch\t two", hn.DONE, T0, None,
                         now=T0 + 5)
        self.assertEqual(got[0], "TENDRIL · re search two")
        gone = hn.compose("pi", "\x01\x02\x7f", hn.DONE, T0, None, now=T0 + 5)
        self.assertEqual(gone[0], "TENDRIL · workspace")

    def test_unicode_label_kept(self):
        got = hn.compose("pi", "café–研究", hn.DONE, T0, None, now=T0 + 5)
        self.assertEqual(got[0], "TENDRIL · café–研究")

    def test_kind_normalized(self):
        got = hn.compose("", None, hn.DONE, T0, None, now=T0 + 5)
        self.assertEqual(got[1], "AGENT finished · 5s")

    def test_pure_string_ops_only(self):
        # rendering path must never shell out (no subprocess anywhere)
        for fn in (hn.compose, hn.render_template, hn.short_cwd,
                   hn.human_elapsed, hn.click_headers, hn.link_fields):
            self.assertNotIn("subprocess", inspect.getsource(fn))


class RenderTemplate(unittest.TestCase):
    def fields(self, label="my label"):
        return hn.link_fields("testhost", "w15", label)

    def test_all_placeholders(self):
        out = hn.render_template(
            "{uri}|{payload_b64}|{label_uri}|{host}|{workspace_id}|{label}",
            self.fields())
        self.assertEqual(out, "tendril://host/testhost/workspace/w15"
                              "?label=my%20label"
                              "|eyJoIjoidGVzdGhvc3QiLCJsIjoibXkgbGFiZWwiLCJ2"
                              "IjoxLCJ3IjoidzE1In0"
                              "|my%20label|testhost|w15|my label")

    def test_payload_round_trips(self):
        parsed = tl.parse_payload_b64(self.fields()["payload_b64"])
        self.assertEqual(parsed, {"host": "testhost", "workspace_id": "w15",
                                  "label": "my label"})

    def test_payload_carries_only_host_ws_label(self):
        parsed = tl.parse_payload_b64(self.fields()["payload_b64"])
        self.assertEqual(set(parsed), {"host", "workspace_id", "label"})

    def test_label_uri_percent_encoded(self):
        self.assertEqual(self.fields("my label")["label_uri"], "my%20label")
        self.assertEqual(self.fields("café 研究")["label_uri"],
                         "caf%C3%A9%20%E7%A0%94%E7%A9%B6")
        # comma-safe too, so it cannot break the ntfy Actions short format
        self.assertEqual(self.fields("a,b")["label_uri"], "a%2Cb")

    def test_unknown_placeholder_renders_empty(self):
        self.assertEqual(hn.render_template("x={nope} y", self.fields()), "x= y")
        self.assertEqual(hn.render_template("a{b", self.fields()), "a{b")
        self.assertEqual(hn.render_template(None, self.fields()), "")

    def test_click_headers_view_attach_clear(self):
        url, actions = hn.click_headers(
            {"NTFY_CLICK_TEMPLATE": "https://p/?p={payload_b64}"},
            self.fields())
        self.assertTrue(url.startswith("https://p/?p=ey"))
        self.assertEqual(actions,
                         f"view, Attach, {url}, clear=true")

    def test_actions_disabled_by_zero(self):
        url, actions = hn.click_headers(
            {"NTFY_CLICK_TEMPLATE": "https://p/?p={payload_b64}",
             "NTFY_ACTIONS": "0"}, self.fields())
        self.assertTrue(url)
        self.assertIsNone(actions)

    def test_no_fields_no_click(self):
        self.assertEqual(hn.click_headers(CFG_CLICK, None), (None, None))

    def test_vanished_workspace_skips_link(self):
        self.assertIsNone(hn.link_fields("testhost", "", "research"))
        self.assertIsNone(hn.link_fields("", "w1", "research"))


class PollTransitions(unittest.TestCase):
    def drive(self, steps, cfg, state=None):
        return drive_poll(steps, cfg, state)

    def test_blocked_transition_sends_click_and_actions(self):
        state = {"w1:p1": ("working", T0, 0.0)}
        state, reqs = self.drive([(snap([agent(status="blocked")]), T0 + 8)],
                                 CFG_CLICK, state=state)
        self.assertEqual(len(reqs), 1)
        req = reqs[0]
        payload = tl.payload_b64("testhost", "w1", "research")
        url = f"https://phone.example/open?payload={payload}&host=testhost"
        self.assertEqual(req.get_header("Click"),
                         url.encode("utf-8"))
        self.assertEqual(req.get_header("Actions"),
                         f"view, Attach, {url}, clear=true".encode("utf-8"))
        self.assertEqual(req.get_header("Priority"), "high")
        self.assertEqual(req.get_header("Tags"), "rotating_light")
        self.assertEqual(req.get_header("Title"),
                         "TENDRIL · research".encode("utf-8"))
        self.assertEqual(req.data.decode("utf-8"),
                         "PI needs input · 8s · ~/research")
        self.assertIsNone(req.get_header("Authorization"))
        self.assertEqual(state["w1:p1"], ("blocked", T0 + 8, T0 + 8))

    def test_done_transition_includes_elapsed(self):
        state = {"w1:p1": ("working", T0, 0.0)}
        state, reqs = self.drive([(snap([agent(status="done")]), T0 + 1320)],
                                 CFG_NUDGE, state=state)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].data.decode("utf-8"),
                         "PI finished · 22m · ~/research")
        self.assertEqual(reqs[0].get_header("Priority"), "default")
        self.assertEqual(reqs[0].get_header("Tags"), "white_check_mark")
        self.assertIsNone(reqs[0].get_header("Click"))
        self.assertIsNone(reqs[0].get_header("Actions"))

    def test_meaningless_transitions_stay_silent(self):
        for prev, new in (("idle", "working"), ("blocked", "working"),
                          ("done", "idle"), ("blocked", "idle")):
            state = {"w1:p1": (prev, T0, 0.0)}
            state, reqs = self.drive([(snap([agent(status=new)]), T0 + 8)],
                                     CFG_NUDGE, state=state)
            self.assertEqual(reqs, [], f"{prev}->{new} must not push")
            self.assertEqual(state["w1:p1"], (new, T0 + 8, 0.0))

    def test_repeat_window_suppresses_flaps(self):
        state = {"w1:p1": ("working", T0, T0 + 3)}  # pushed 5s ago
        state, reqs = self.drive([(snap([agent(status="blocked")]), T0 + 8)],
                                 CFG_NUDGE, state=state)
        self.assertEqual(reqs, [])
        self.assertEqual(state["w1:p1"], ("blocked", T0 + 8, T0 + 3))

    def test_pane_gone_cleanup(self):
        state = {"w1:p1": ("idle", T0, 0.0), "w9:p9": ("idle", T0, 0.0)}
        state, reqs = self.drive([(snap([agent(status="idle")]), T0 + 8)],
                                 CFG_NUDGE, state=state)
        self.assertEqual(reqs, [])
        self.assertEqual(list(state), ["w1:p1"])

    def test_vanished_workspace_pushes_without_click(self):
        state = {"w1:p1": ("working", T0, 0.0)}
        state, reqs = self.drive(
            [(snap([agent(status="blocked")], workspaces=[]), T0 + 8)],
            CFG_CLICK, state=state)
        self.assertEqual(len(reqs), 1)  # push still happens...
        self.assertIsNone(reqs[0].get_header("Click"))  # ...without links
        self.assertIsNone(reqs[0].get_header("Actions"))
        self.assertEqual(reqs[0].get_header("Title"),
                         "TENDRIL · w1".encode("utf-8"))

    def test_unconfigured_pushes_nothing(self):
        state = {"w1:p1": ("working", T0, 0.0)}
        state, reqs = self.drive([(snap([agent(status="blocked")]), T0 + 8)],
                                 {"NTFY_TOPIC": "topic"}, state=state)
        self.assertEqual(reqs, [])

    def test_failed_send_keeps_timestamp_for_retry(self):
        state = {"w1:p1": ("working", T0, 0.0)}
        with mock.patch.object(hn, "snapshot",
                               return_value=snap([agent(status="blocked")])), \
                mock.patch.object(hn.time, "time", return_value=T0 + 8), \
                mock.patch.object(hn.urllib.request, "urlopen",
                                  side_effect=OSError("down")), \
                mock.patch.dict(hn.CONFIG, {"TAILSCALE_HOST": "t"},
                                clear=True), \
                contextlib.redirect_stdout(io.StringIO()):
            hn.poll(state, CFG_NUDGE, allow_push=True)
        self.assertEqual(state["w1:p1"], ("blocked", T0 + 8, 0.0))


class Nudge(unittest.TestCase):
    def drive(self, steps, cfg, state=None):
        return drive_poll(steps, cfg, state)

    def test_fires_once_per_stint_at_threshold(self):
        working = snap([agent(status="working")])
        state, reqs = self.drive([
            (working, T0),                      # baseline
            (working, T0 + 44 * 60),            # below threshold: silent
            (working, T0 + 45 * 60 + 8),        # first poll past: fires
            (working, T0 + 45 * 60 + 16),       # still working: no repeat
        ], CFG_NUDGE)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].data.decode("utf-8"),
                         "PI still working · 45m · ~/research")
        self.assertEqual(reqs[0].get_header("Priority"), "min")
        self.assertEqual(reqs[0].get_header("Tags"), "hourglass")
        self.assertEqual(state["w1:p1"],
                         ("working", T0, T0 + 45 * 60 + 8))

    def test_fires_again_on_next_stint(self):
        working = snap([agent(status="working")])
        state = {"w1:p1": ("working", T0, T0 + 45 * 60)}  # nudged stint 1
        state, reqs = self.drive([
            (snap([agent(status="idle")]), T0 + 3600),   # -> done push
            (working, T0 + 3608),                        # new stint: silent
            (working, T0 + 3608 + 45 * 60),              # nudge again
        ], CFG_NUDGE, state=state)
        self.assertEqual(len(reqs), 2)
        self.assertEqual(reqs[0].data.decode("utf-8"),
                         "PI finished · 1h00m · ~/research")
        self.assertEqual(reqs[1].data.decode("utf-8"),
                         "PI still working · 45m · ~/research")

    def test_zero_disables(self):
        cfg = dict(CFG_NUDGE, NTFY_WORKING_NUDGE_MINUTES="0")
        working = snap([agent(status="working")])
        state, reqs = self.drive([
            (working, T0), (working, T0 + 7200)], cfg)
        self.assertEqual(reqs, [])

    def test_below_threshold_never_fires(self):
        working = snap([agent(status="working")])
        state, reqs = self.drive([
            (working, T0), (working, T0 + 44 * 60 + 59)], CFG_NUDGE)
        self.assertEqual(reqs, [])

    def test_junk_config_falls_back_to_default(self):
        self.assertEqual(hn.nudge_minutes({}), 45)
        self.assertEqual(hn.nudge_minutes({"NTFY_WORKING_NUDGE_MINUTES": "x"}),
                         45)
        self.assertEqual(hn.nudge_minutes({"NTFY_WORKING_NUDGE_MINUTES": "0"}),
                         0)


class Rebaseline(unittest.TestCase):
    def test_watch_clears_state_after_outage(self):
        working = snap([agent(status="working")])
        idle = snap([agent(status="idle")])
        reqs = []

        def fake_urlopen(req, timeout=10):
            reqs.append(req)
            return mock.MagicMock()

        with mock.patch.object(hn, "load_env", return_value=dict(CFG_NUDGE)), \
                mock.patch.dict(hn.CONFIG, {"TAILSCALE_HOST": "testhost"},
                                clear=True), \
                mock.patch.object(hn, "snapshot",
                                  side_effect=[working, None, idle]), \
                mock.patch.object(hn.time, "time",
                                  side_effect=[T0, T0 + 16, T0 + 32,
                                               T0 + 48, T0 + 64]), \
                mock.patch.object(hn.time, "sleep",
                                  side_effect=[None, None,
                                               KeyboardInterrupt]), \
                mock.patch.object(hn, "sd_notify", lambda state: None), \
                mock.patch.object(hn.urllib.request, "urlopen",
                                  side_effect=fake_urlopen), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                hn.watch()
        # working baselined, outage hit, idle after recovery re-baselined:
        # the stale working->idle transition must NOT have pushed
        self.assertEqual(reqs, [])


class Send(unittest.TestCase):
    def send_req(self, cfg, title, body, priority, tags, **kw):
        with mock.patch.object(hn.urllib.request, "urlopen") as urlopen, \
                contextlib.redirect_stdout(io.StringIO()):
            hn.send(cfg, title, body, priority, tags, **kw)
        return urlopen.call_args[0][0]

    def test_headers_and_utf8_values(self):
        req = self.send_req({"NTFY_URL": "https://ntfy.example/",
                             "NTFY_TOPIC": "topic"},
                            "TENDRIL · café", "b", "high", "rotating_light",
                            click="https://p/x", actions="view, Attach, x")
        self.assertEqual(req.full_url, "https://ntfy.example/topic")
        self.assertEqual(req.get_header("Title"), "TENDRIL · café".encode())
        self.assertEqual(req.get_header("Priority"), "high")
        self.assertEqual(req.get_header("Tags"), "rotating_light")
        self.assertEqual(req.get_header("Click"), b"https://p/x")
        self.assertEqual(req.get_header("Actions"), b"view, Attach, x")
        self.assertEqual(req.data, b"b")

    def test_no_token_no_authorization_header(self):
        req = self.send_req({"NTFY_URL": "https://ntfy.example",
                             "NTFY_TOPIC": "topic"}, "t", "b", "default", "")
        self.assertIsNone(req.get_header("Authorization"))

    def test_token_sent_as_bearer(self):
        req = self.send_req({"NTFY_URL": "https://ntfy.example",
                             "NTFY_TOPIC": "topic", "NTFY_TOKEN": "tk_x"},
                            "t", "b", "default", "")
        self.assertEqual(req.get_header("Authorization"), "Bearer tk_x")


class SummarizerPlugin(unittest.TestCase):
    INFO = {"workspace_id": "w1", "label": "research", "agent": "PI",
            "status": "working", "elapsed_s": 2820, "cwd": "~/research",
            "host": "testhost"}

    def test_off_by_default_never_spawns(self):
        with mock.patch.object(hn.subprocess, "run") as run:
            self.assertEqual(hn.summarize({}, self.INFO), "")
            self.assertEqual(hn.summarize({"NTFY_SUMMARIZER_BIN": ""},
                                          self.INFO), "")
            self.assertFalse(run.called)

    def test_local_script_detail_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = os.path.join(tmp, "summarize")
            with open(binary, "w") as f:
                f.write("#!/bin/sh\ncat >/dev/null\necho 'phase 2 of 5'\n")
            os.chmod(binary, 0o700)
            detail = hn.summarize({"NTFY_SUMMARIZER_BIN": binary}, self.INFO)
        self.assertEqual(detail, "phase 2 of 5")
        with home_env():
            title, body, prio, tags = hn.compose(
                "PI", "research", hn.NUDGE, T0, "/home/op/research",
                now=T0 + 2820)
        self.assertEqual(body, "PI still working · 47m · ~/research")
        self.assertEqual(body + f" — {detail}",
                         "PI still working · 47m · ~/research — phase 2 of 5")

    def test_multiline_and_overlong_output_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = os.path.join(tmp, "summarize")
            with open(binary, "w") as f:
                f.write("#!/bin/sh\n"
                        "echo line1\n"
                        "echo line2\n"
                        "i=0; while [ $i -lt 80 ]; do printf xxxxxxxxxx; "
                        "i=$((i+1)); done; echo\n")
            os.chmod(binary, 0o700)
            detail = hn.summarize({"NTFY_SUMMARIZER_BIN": binary}, self.INFO)
        self.assertNotIn("\n", detail)
        self.assertLessEqual(len(detail), 200)
        self.assertTrue(detail.startswith("line1 line2 "))

    def test_any_failure_omits(self):
        self.assertEqual(hn.summarize(
            {"NTFY_SUMMARIZER_BIN": "/nonexistent/binary"}, self.INFO), "")
        with tempfile.TemporaryDirectory() as tmp:
            binary = os.path.join(tmp, "summarize")
            with open(binary, "w") as f:
                f.write("#!/bin/sh\nexit 3\n")
            os.chmod(binary, 0o700)
            cfg = {"NTFY_SUMMARIZER_BIN": binary}
            self.assertEqual(hn.summarize(cfg, self.INFO), "")
            os.chmod(binary, 0o600)  # not executable -> omitted
            self.assertEqual(hn.summarize(cfg, self.INFO), "")


class TestFlag(unittest.TestCase):
    """The --test flag: the T flow invokes it and reads the last line."""

    def test_unconfigured_exit_1(self):
        out = io.StringIO()
        with mock.patch.object(hn, "load_env", return_value={}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(hn.test(), 1)
        self.assertIn("push NOT CONFIGURED", out.getvalue())

    def test_configured_sends_with_click_and_keeps_last_line(self):
        cfg = dict(CFG_CLICK)
        out = io.StringIO()
        with mock.patch.object(hn, "load_env", return_value=cfg), \
                mock.patch.dict(hn.CONFIG, {"TAILSCALE_HOST": "testhost"},
                                clear=True), \
                mock.patch.object(hn, "snapshot",
                                  return_value=snap([], workspaces=[
                                      {"workspace_id": "w1",
                                       "label": "research"}])), \
                mock.patch.object(hn, "send") as send, \
                contextlib.redirect_stdout(out):
            self.assertEqual(hn.test(), 0)
        self.assertEqual(out.getvalue().strip().splitlines()[-1],
                         "test push sent - check the phone.")
        self.assertEqual(send.call_args.kwargs["click"],
                         "https://phone.example/open?payload="
                         + tl.payload_b64("testhost", "w1", "research")
                         + "&host=testhost")
        self.assertTrue(send.call_args.kwargs["actions"].startswith(
            "view, Attach, "))


class ZeroLlmDefault(unittest.TestCase):
    def test_no_api_key_or_llm_references_in_source(self):
        src = inspect.getsource(hn)
        for bad in ("API_KEY", "OPENAI", "ANTHROPIC", "GEMINI", "api.openai",
                    "ANTHROPIC_API", "chat/completions"):
            self.assertNotIn(bad, src)
        # the only token-like credential is the documented push token
        self.assertIn("NTFY_TOKEN", src)

    def test_only_known_env_vars_read(self):
        src = inspect.getsource(hn)
        names = set(re.findall(r"os\.environ\.get\(\"([A-Z_]+)\"\)", src))
        self.assertEqual(names, {"REMOTE_AGENTS_CONFIG", "HERDR_BIN",
                                 "NOTIFY_ENV", "NOTIFY_SOCKET"})


class OnceSubprocess(unittest.TestCase):
    def test_once_smoke_end_to_end_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = os.path.join(tmp, "herdr")
            snap_file = os.path.join(tmp, "snap.json")
            with open(snap_file, "w") as f:
                json.dump({"result": {"snapshot":
                          snap([agent(status="working")])}}, f)
            with open(stub, "w") as f:
                f.write("#!/bin/sh\n"
                        "if [ \"$1\" = \"--version\" ]; then"
                        " echo 'herdr 0.9.4'; exit 0; fi\n"
                        "if [ \"$1\" = \"status\" ]; then"
                        " printf 'status: running\\n"
                        "endpoint_compatible: yes\\n'; exit 0; fi\n"
                        "if [ \"$1\" = \"api\" ] && [ \"$2\" = \"snapshot\" ];"
                        " then cat \"$SNAPSHOT_FILE\"; exit 0; fi\n"
                        "exit 1\n")
            os.chmod(stub, 0o755)
            env_file = os.path.join(tmp, "notify.env")
            with open(env_file, "w") as f:
                f.write("# deliberately unconfigured\n")
            cfg_file = os.path.join(tmp, "config")
            with open(cfg_file, "w") as f:
                f.write("# empty\n")
            env = dict(os.environ)
            env.update({"HERDR_BIN": stub, "NOTIFY_ENV": env_file,
                        "REMOTE_AGENTS_CONFIG": cfg_file,
                        "SNAPSHOT_FILE": snap_file})
            env.pop("NOTIFY_SOCKET", None)
            p = subprocess.run([sys.executable, NOTIFY, "--once"], env=env,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("poll OK (baseline logged)", p.stdout)

    def test_test_flag_unconfigured_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = os.path.join(tmp, "notify.env")
            with open(env_file, "w") as f:
                f.write("NTFY_TOPIC=only-topic-no-url\n")
            cfg_file = os.path.join(tmp, "config")
            with open(cfg_file, "w") as f:
                f.write("# empty\n")
            env = dict(os.environ)
            env.update({"NOTIFY_ENV": env_file, "REMOTE_AGENTS_CONFIG":
                        cfg_file})
            env.pop("NOTIFY_SOCKET", None)
            p = subprocess.run([sys.executable, NOTIFY, "--test"], env=env,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, 1)
            self.assertIn("push NOT CONFIGURED", p.stdout)


if __name__ == "__main__":
    unittest.main()
