"""Contract §1/§2 capacity: tendril_capacity + the remote-agents verb.

Under test (docs/LINK-CONTRACT-0.2.md, all fixtures synthetic):
- token accounting EXCLUDES cache reads on every provider — claude:
  input+output+cache_creation; codex: input−cached+output; pi:
  input+output+cacheWrite — and the definition lives in the module
  docstring (the 67M-tokens-a-day smoke bug);
- state thresholds ready<70 / pressure 70–89 / critical 90–97 / exhausted
  ≥98 or an active 429 with future resets_at; unknown outside the
  staleness bound (local 6 h, network 30 min);
- providers appear only with 7-day activity or a trusted window;
  tokens_by_day always carries all 7 days, oldest first, host local dates;
- network adapters are opt-in (config file), Bearer-only, 10 s timeout,
  schema-projected, 300 s min interval, doubling backoff, and a 401 is
  never retried until the credential file or the enabled config changes;
- secrets: the fake token planted beside the real credential subtree never
  reaches output, cache or exception text (assertions avoid embedding the
  marker so even a failure cannot print it);
- urllib is mocked in every network test — NO test touches the network;
- budget behaviour: snapshot_section serves the cache or scans within its
  budget; progress is incremental across calls.
"""
import contextlib
import fcntl
import http.server
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from importlib.machinery import SourceFileLoader
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "bin", "remote-agents")
CAP = os.path.join(REPO, "bin", "tendril_capacity.py")
FIX = os.path.join(REPO, "tests", "fixtures", "capacity")


def _load(name, path):
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


tc = _load("tendril_capacity_test", CAP)

NOW = 1760000000.0
MARKER = "FAKE-SECRET-TOKEN-h2r"          # synthetic credential marker
DECOY_EMAIL = "decoy-claude@example.test"


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000Z"


def no_leak(self, marker, haystack, what):
    """Assert absence WITHOUT risking the haystack in a failure message."""
    if isinstance(haystack, bytes):
        haystack = haystack.decode("utf-8", "replace")
    ok = marker not in haystack
    if not ok:
        # name the file/stream, never its contents
        raise AssertionError("marker leaked into %s" % what)


class Base(unittest.TestCase):
    """One fake $HOME plus a scratch cache path, per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril-capacity-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.cache = os.path.join(self.tmp, "cache", "capacity.json")
        os.makedirs(self.home)

    # ----- fixture plumbing -------------------------------------------
    def subs(self):
        return {
            "@T0@": iso(NOW), "@T1@": iso(NOW - 86400),
            "@T2@": iso(NOW - 2 * 86400), "@T3@": iso(NOW - 3 * 86400),
            "@T4@": iso(NOW - 4 * 86400), "@T5@": iso(NOW - 5 * 86400),
            "@T6@": iso(NOW - 6 * 86400),
            "@TNOW@": iso(NOW), "@TNOW2@": iso(NOW + 60),
            "@RESET@": str(int(NOW + 3600)),
            "@RESET2@": str(int(NOW + 8 * 86400)),
            "@RESET_ISO@": iso(NOW + 3600),
            "@RESET2_ISO@": iso(NOW + 8 * 86400),
            "@STALE_ISO@": iso(NOW - 7 * 3600),
            "@STALE_RESET@": str(int(NOW - 3600)),
            "@YDAY_ISO@": iso(NOW - 86400),
            "@MS0@": str(int(NOW * 1000)),
            "@MS1@": str(int((NOW - 86400) * 1000)),
        }

    def fixture(self, name):
        with open(os.path.join(FIX, name), encoding="utf-8") as f:
            text = f.read()
        for k, v in self.subs().items():
            text = text.replace(k, v)
        return text

    def put(self, rel, content, binary=False):
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb" if binary else "w") as f:
            f.write(content)
        return path

    def put_fixture(self, rel, name):
        return self.put(rel, self.fixture(name))

    def cred(self, which):
        """Install one credential fixture (with decoy subtrees)."""
        names = {"claude": ("credentials_claude.json",
                            ".claude/.credentials.json"),
                 "codex": ("credentials_codex.json", ".codex/auth.json"),
                 "pi": ("credentials_pi.json", ".pi/agent/auth.json")}
        name, rel = names[which]
        return self.put_fixture(rel, name)

    def enable(self, *aids):
        for aid in aids:
            self.assertTrue(tc.set_enabled(aid, True, home=self.home))

    # ----- drivers ------------------------------------------------------
    def collect(self, refresh=False, budget_s=30.0, now=NOW):
        return tc.collect(refresh=refresh, budget_s=budget_s, now=now,
                          home=self.home, cache_path=self.cache)

    def prov(self, pid, now=NOW):
        sec = self.collect(now=now)
        for rec in sec.get("providers", []):
            if rec.get("id") == pid:
                return rec
        return None


class _FakeResp:
    def __init__(self, body, url=""):
        self.body = body if isinstance(body, bytes) else body.encode()
        self.url = url

    def read(self, n=-1):
        return self.body if n < 0 else self.body[:n]

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urllib(body=None, code=200, exc=None):
    """Build an opener.open stand-in; records (url, headers, timeout)
    per call. Patches tendril_capacity._OPENER.open — the no-redirect
    opener _http_get_json actually uses."""
    calls = []

    def handler(req, timeout=None):                     # noqa: ARG001
        calls.append((req.get_full_url(),
                      {k.lower(): v for k, v in req.header_items()},
                      timeout))
        if exc is not None:
            raise exc
        if code != 200:
            raise urllib.error.HTTPError(req.get_full_url(), code,
                                         "fake", None, None)
        return _FakeResp(body, req.get_full_url())

    handler.calls = calls
    return handler


# ---------- token accounting: cache reads are NEVER counted ----------
class TestTokenAccounting(Base):
    def test_docstring_documents_the_definition(self):
        with open(CAP, encoding="utf-8") as f:
            doc = f.read().split('"""', 2)[1]
        self.assertIn("cache_read_input_tokens → cache_read", doc)
        self.assertIn("input_tokens - cached_input_tokens", doc)
        self.assertIn("input + output + cacheWrite", doc)
        self.assertIn("CACHE READS", doc)
        self.assertIn("by_model_window_days", doc)
        with open(CAP, encoding="utf-8") as f:
            self.assertIn("ACC_VERSION", f.read())

    def test_claude_week_cache_read_excluded(self):
        self.put_fixture(".claude/projects/p1/s1.jsonl", "claude_week.jsonl")
        rec = self.prov("claude")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["tokens_today"], 1600)   # 1000+500+100
        self.assertEqual(rec["state"], "unknown")
        self.assertEqual(rec["source"], "claude_local")
        days = rec["tokens_by_day"]
        self.assertEqual(len(days), 7)                # stable chart: all 7
        self.assertEqual([d["tokens"] for d in days], [1600] * 7)
        self.assertEqual(days[-1]["date"], tc._local_date(NOW))
        self.assertEqual(days[0]["date"], tc._local_date(NOW - 6 * 86400))
        # headline tokens = input+output+cache_write; cache reads ride
        # SEPARATELY, labelled (900000 today) — never inside the headline
        self.assertEqual(days[-1],
                         {"date": tc._local_date(NOW), "tokens": 1600,
                          "input": 1000, "output": 500, "cache_write": 100,
                          "cache_read": 900000})
        self.assertEqual(rec["by_model"],
                         [{"model": "claude-opus-4-8", "tokens": 11200,
                           "input": 7000, "output": 3500,
                           "cache_write": 700, "cache_read": 4800000}])
        self.assertEqual(rec["by_model_window_days"], 7)
        self.assertEqual(rec["days_source"],
                         ["local_transcripts:claude_code"])
        self.assertEqual(rec["by_model_source"],
                         ["local_transcripts:claude_code"])
        headline = json.dumps(
            {"tokens": [d["tokens"] for d in days],
             "model_tokens": [m["tokens"] for m in rec["by_model"]]})
        no_leak(self, "900000", headline, "headline token fields")
        no_leak(self, "decoy prompt text", json.dumps(rec), "wire record")

    def test_codex_full_input_minus_cached_plus_output(self):
        self.put_fixture(".codex/sessions/2025/10/rollout-a.jsonl",
                         "codex_full.jsonl")
        rec = self.prov("codex")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["tokens_today"], 4600)   # 3800 + 800
        self.assertEqual(rec["state"], "critical")    # worst 93.0
        self.assertEqual(rec["plan"], "plus")
        self.assertEqual(rec["source"], "codex_rollout")
        kinds = {w["kind"]: w for w in rec["windows"]}
        self.assertEqual(kinds["session"]["used_pct"], 93.0)
        self.assertEqual(kinds["session"]["resets_at"], int(NOW + 3600))
        self.assertEqual(kinds["session"]["window_min"], 300)
        self.assertEqual(kinds["weekly"]["used_pct"], 42.5)
        self.assertEqual(kinds["weekly"]["resets_at"], int(NOW + 8 * 86400))
        blob = json.dumps(rec)
        for total in ("12800", "19100", "6300"):
            no_leak(self, total, blob, "wire record")  # totals never counted

    def test_codex_secondary_null_is_normal(self):
        self.put_fixture(".codex/sessions/2025/10/rollout-b.jsonl",
                         "codex_secondary_null.jsonl")
        rec = self.prov("codex")
        self.assertEqual(rec["tokens_today"], 300)    # 4000-3900+200
        self.assertEqual([w["kind"] for w in rec["windows"]], ["session"])
        self.assertEqual(rec["state"], "ready")
        self.assertEqual(rec["plan"], "pro")

    def test_codex_legacy_resets_in_seconds_and_delta(self):
        self.put_fixture(".codex/sessions/2025/10/rollout-c.jsonl",
                         "codex_legacy.jsonl")
        rec = self.prov("codex")
        self.assertEqual(rec["tokens_today"], 2500)   # 4000-2000 + 500
        session = {w["kind"]: w for w in rec["windows"]}["session"]
        self.assertEqual(session["resets_at"], int(NOW + 1800))
        self.assertEqual(session["used_pct"], 10.0)
        self.assertEqual(rec["state"], "ready")
        blob = json.dumps(rec)
        no_leak(self, "5400", blob, "wire record")
        no_leak(self, "9900", blob, "wire record")

    def test_codex_stale_window_unknown(self):
        self.put_fixture(".codex/sessions/2025/10/rollout-d.jsonl",
                         "codex_stale.jsonl")
        rec = self.prov("codex")
        self.assertIsNotNone(rec)                     # recent tokens exist
        self.assertEqual(rec["state"], "unknown")     # window is 7 h old
        self.assertNotIn("windows", rec)              # never fabricate
        self.assertEqual(rec["source"], "codex_rollout")
        days = {d["date"]: d["tokens"] for d in rec["tokens_by_day"]}
        self.assertEqual(len(rec["tokens_by_day"]), 7)
        self.assertEqual(sum(days.values()), 350)     # 150 + 200, both counted
        today = tc._local_date(NOW)
        self.assertEqual(days[today],
                         150 if tc._local_date(NOW - 7 * 3600) == today
                         else 0)

    def test_pi_two_providers_two_days(self):
        self.put_fixture(".pi/agent/sessions/s1/chunk.jsonl", "pi_two.jsonl")
        sec = self.collect()
        ids = [p["id"] for p in sec["providers"]]
        self.assertEqual(ids, ["zai", "google"])      # fixed first, then rest
        zai = sec["providers"][0]
        self.assertEqual(zai["label"], "Z.AI")
        self.assertEqual(zai["harness"], "pi")
        self.assertEqual(zai["tokens_today"], 170)    # 100+50+20
        self.assertEqual(zai["tokens_by_day"][-2]["tokens"], 290)
        self.assertEqual(zai["tokens_by_day"][-1]["tokens"], 170)
        self.assertEqual(zai["cost_today_usd"], 0.25)
        self.assertEqual(zai["by_model"],
                         [{"model": "glm-4.7", "tokens": 460,
                           "input": 300, "output": 110, "cache_write": 50,
                           "cache_read": 90000}])
        self.assertEqual(zai["today_by_model"],
                         [{"model": "glm-4.7", "tokens": 170,
                           "input": 100, "output": 50, "cache_write": 20,
                           "cache_read": 50000}])
        google = sec["providers"][1]
        self.assertEqual(google["tokens_today"], 600)
        self.assertNotIn("cost_today_usd", google)
        blob = json.dumps(sec)
        for cached in ("40300", "50180"):
            no_leak(self, cached, blob, "section")    # totalTokens ignored
        no_leak(self, "decoy user text", blob, "section")

    def test_only_last_seven_days_appear(self):
        old = json.dumps({
            "timestamp": iso(NOW - 10 * 86400), "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 5000,
                                     "cached_input_tokens": 0,
                                     "output_tokens": 5000}}}},
            separators=(",", ":")) + "\n"
        self.put(".codex/sessions/2025/10/rollout-old.jsonl", old)
        self.assertIsNone(self.prov("codex"))         # 10 days old: absent
        self.assertIsNone(self.prov("codex", now=NOW - 3 * 86400))
        rec = self.prov("codex", now=NOW - 4 * 86400)  # 6 days before that now
        self.assertIsNotNone(rec)
        self.assertEqual(rec["tokens_by_day"][0]["tokens"], 10000)

    def test_claude_huge_line_regex_path(self):
        line = json.dumps({
            "type": "assistant", "timestamp": iso(NOW),
            "message": {"model": "claude-opus-4-8", "usage": {
                "input_tokens": 7, "output_tokens": 3,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 123456}}},
            separators=(",", ":")).encode() + b" " * (tc.HUGE_LINE + 4096)
        self.put(".claude/projects/p9/big.jsonl",
                 line + b"\n", binary=True)
        rec = self.prov("claude")
        self.assertEqual(rec["tokens_today"], 12)     # cache read excluded
        # …and labelled separately on the wire, never folded in
        self.assertEqual(rec["tokens_by_day"][-1]["cache_read"], 123456)
        self.assertEqual(rec["by_model"][0]["cache_read"], 123456)
        headline = json.dumps({"tokens": rec["tokens_today"],
                               "m": rec["by_model"][0]["tokens"]})
        no_leak(self, "123456", headline, "headline token fields")

    def test_claude_huge_429_ms_reset_normalized(self):
        ms = int((NOW + 3600) * 1000)
        line = json.dumps({
            "type": "x", "apiErrorStatus": 429, "error": "rate_limit",
            "timestamp": iso(NOW),
            "message": {"quotaLimits": {"resetsAt": ms,
                                        "rateLimitType": "seven_day"}}},
            separators=(",", ":")).encode() + b" " * (tc.HUGE_LINE + 16)
        self.put(".claude/projects/p9/big429.jsonl",
                 line + b"\n", binary=True)
        rec = self.prov("claude")
        self.assertEqual(rec["state"], "exhausted")
        self.assertEqual(rec["windows"],
                         [{"kind": "weekly", "resets_at": int(NOW + 3600)}])


# ---------- 429 / thresholds / staleness ----------
# ---------- §12 by-model windowing, provenance, harness split ----------
class TestByModelWindowAndProvenance(Base):
    """by_model speaks for EXACTLY the same 7 host-local days as
    tokens_by_day; cache reads are labelled separately; every list
    carries its source; multi-harness providers split by harness; the
    cache's per-model buckets stay bounded."""

    def cache_with(self, files, net=None):
        cache = {"acc_ver": tc.ACC_VERSION, "files": files}
        if net:
            cache["net"] = net
        return cache

    def cache_with_mday(self, mday, days=None, kind="claude", extra=None):
        today = tc._local_date(NOW)
        files = {
            "/h/x.jsonl": {"kind": kind, "last_ts": NOW,
                           "days": days if days is not None else
                           {today: {"tok": 10, "in": 10}},
                           "mday": mday},
        }
        if kind == "codex":
            files["/h/x.jsonl"]["limit"] = None
            files["/h/x.jsonl"]["prev"] = None
        files.update(extra or {})
        return self.cache_with(files)

    def models(self, cache):
        provs = {p["id"]: p for p in
                 tc._assemble(cache, NOW, enabled=[])["providers"]}
        return provs

    def test_stale_model_outside_the_window_is_excluded(self):
        """A model the source recorded only on day-8 must not appear as
        current — the September-models bug."""
        today = tc._local_date(NOW)
        day8 = tc._local_date(NOW - 8 * 86400)
        provs = self.models(self.cache_with_mday({
            "claude-fable-5": {day8: {"tok": 999, "in": 999}},
            "claude-opus-5-5": {today: {"tok": 40, "in": 40}},
        }))
        claude = provs["claude"]
        self.assertEqual([m["model"] for m in claude["by_model"]],
                         ["claude-opus-5-5"])
        self.assertEqual(claude["by_model_window_days"], 7)

    def test_window_is_seven_local_days_ending_today(self):
        """The boundary is the local calendar: day-6 in, day-7 out —
        a bucket one hour either side of local midnight lands on one
        local date, and that date decides."""
        in6 = tc._local_date(NOW - 6 * 86400)      # oldest in-window day
        out7 = tc._local_date(NOW - 7 * 86400)     # one day older: outside
        self.assertEqual(len({in6, out7, tc._local_date(NOW)}), 3)
        provs = self.models(self.cache_with_mday({
            "m-in": {in6: {"tok": 6, "in": 6}},
            "m-out": {out7: {"tok": 7, "in": 7}},
            "m-today": {tc._local_date(NOW): {"tok": 1, "in": 1}},
        }))
        names = [m["model"] for m in provs["claude"]["by_model"]]
        self.assertEqual(names, ["m-in", "m-today"])   # sorted by tokens

    def test_today_by_model_covers_today_only(self):
        today = tc._local_date(NOW)
        yday = tc._local_date(NOW - 86400)
        provs = self.models(self.cache_with_mday({
            "m-now": {today: {"tok": 5, "in": 5}},
            "m-past": {yday: {"tok": 9, "in": 9}},
        }, days={today: {"tok": 5, "in": 5},
                 yday: {"tok": 9, "in": 9}}))
        claude = provs["claude"]
        self.assertEqual([m["model"] for m in claude["by_model"]],
                         ["m-past", "m-now"])
        self.assertEqual([m["model"] for m in claude["today_by_model"]],
                         ["m-now"])
        self.assertEqual(claude["tokens_today"], 5)

    def test_cache_read_separate_on_every_list(self):
        line = json.dumps({
            "type": "message", "message": {
                "role": "assistant", "provider": "zai",
                "model": "glm-5.3-flash", "timestamp": NOW * 1000,
                "usage": {"input": 100, "output": 50, "cacheWrite": 20,
                          "cacheRead": 777777}}},
            separators=(",", ":")).encode() + b"\n"
        self.put(".pi/agent/sessions/s9/chunk.jsonl", line.decode())
        rec = self.prov("zai")
        self.assertEqual(rec["tokens_today"], 170)    # reads never counted
        self.assertEqual(rec["tokens_by_day"][-1]["cache_read"], 777777)
        self.assertEqual(rec["by_model"][0],
                         {"model": "glm-5.3-flash", "tokens": 170,
                          "input": 100, "output": 50, "cache_write": 20,
                          "cache_read": 777777})
        self.assertEqual(rec["today_by_model"][0]["cache_read"], 777777)
        headline = json.dumps({"t": rec["tokens_today"],
                               "m": [m["tokens"] for m in rec["by_model"]]})
        no_leak(self, "777777", headline, "headline token fields")

    def test_codex_cached_input_is_labelled_cache_read(self):
        line = json.dumps({
            "timestamp": iso(NOW), "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 4000,
                                     "cached_input_tokens": 3900,
                                     "output_tokens": 200}}}},
            separators=(",", ":")).encode() + b"\n"
        self.put(".codex/sessions/2025/10/rollout-cr.jsonl", line.decode())
        rec = self.prov("codex")
        self.assertEqual(rec["tokens_today"], 300)    # 4000-3900+200
        self.assertEqual(rec["tokens_by_day"][-1]["cache_read"], 3900)
        # codex rollouts carry no model attribution: §12 sentence, not a list
        self.assertIsNone(rec["by_model"])
        self.assertEqual(rec["by_model_unavailable_reason"],
                         "no_model_attribution")
        self.assertEqual(rec["by_model_window_days"], 7)

    def test_by_harness_splits_multi_harness_providers(self):
        today = tc._local_date(NOW)
        yday = tc._local_date(NOW - 86400)
        cache = self.cache_with({
            "/h/.claude/c.jsonl": {"kind": "claude", "last_ts": NOW,
                "days": {today: {"tok": 50, "in": 50},
                         yday: {"tok": 70, "in": 70}}, "mday": {}},
            "/h/.pi/b.jsonl": {"kind": "pi", "last_ts": NOW, "prov": {
                "claude-bridge": {"days": {today: {"tok": 100,
                                                 "in": 100}},
                                  "mday": {}}}}})
        claude = self.models(cache)["claude"]
        self.assertEqual(claude["by_harness"],
                         {"claude_code": 120, "pi": 100})
        self.assertEqual(sum(claude["by_harness"].values()),
                         sum(d["tokens"] for d in claude["tokens_by_day"]))
        self.assertEqual(claude["days_source"],
                         ["local_transcripts:claude_code",
                          "local_transcripts:pi"])
        self.assertEqual(claude["by_model_source"],
                         claude["days_source"])

    def test_single_harness_never_carries_by_harness(self):
        self.put_fixture(".pi/agent/sessions/s1/chunk.jsonl", "pi_two.jsonl")
        rec = self.prov("zai")
        self.assertNotIn("by_harness", rec)
        self.assertEqual(rec["days_source"], ["local_transcripts:pi"])

    def test_provider_api_windows_are_never_summed_with_transcripts(self):
        self.put_fixture(".claude/projects/p1/s1.jsonl", "claude_week.jsonl")
        self.cred("claude")
        self.enable("claude_oauth")
        fake = fake_urllib(body=self.fixture("oauth_iso.json"))
        with mock.patch.object(tc._OPENER, "open", fake):
            net = self.prov("claude")
        no_net = self.prov("claude")
        # the usage API adds windows (and plan) — never tokens
        self.assertIn("windows", net)
        self.assertEqual(net["tokens_today"], no_net["tokens_today"])
        self.assertEqual(net["tokens_by_day"], no_net["tokens_by_day"])
        self.assertEqual(net["by_model"], no_net["by_model"])
        self.assertEqual(net.get("by_harness"), no_net.get("by_harness"))
        if "by_harness" in net:
            self.assertEqual(sum(net["by_harness"].values()),
                             sum(d["tokens"]
                                 for d in net["tokens_by_day"]))

    def test_mday_buckets_pruned_past_35_days(self):
        today = tc._local_date(NOW)
        old40 = tc._local_date(NOW - 40 * 86400)
        old34 = tc._local_date(NOW - 34 * 86400)
        cache = self.cache_with_mday({
            "m-gone": {old40: {"tok": 1, "in": 1}},
            "m-kept": {old34: {"tok": 2, "in": 2},
                       today: {"tok": 3, "in": 3}},
        })
        tc._prune_cache_mdays(cache, NOW)
        md = cache["files"]["/h/x.jsonl"]["mday"]
        self.assertNotIn("m-gone", md)          # 40 days: bounded growth
        self.assertIn("m-kept", md)
        # a full collect() prunes the persisted cache too (scratch path)
        self.put_fixture(".claude/projects/p1/s1.jsonl", "claude_week.jsonl")
        stale = {"acc_ver": tc.ACC_VERSION, "scanned_at": 0, "files": {
            "/h/.claude/projects/p1/s1.jsonl": {
                "kind": "claude", "size": 1, "mtime": 0.0, "offset": 0,
                "last_ts": 0, "days": {}, "r429": None,
                "mday": {"sep-only-model": {old40: {"tok": 5,
                                                    "in": 5}}}}}}
        os.makedirs(os.path.dirname(self.cache), exist_ok=True)
        with open(self.cache, "w") as f:
            json.dump(stale, f)
        self.collect(refresh=True, budget_s=30)
        with open(self.cache) as f:
            persisted = json.load(f)
        entry = persisted["files"]["/h/.claude/projects/p1/s1.jsonl"]
        self.assertNotIn("sep-only-model", entry["mday"])

    def test_pre_v4_cache_rebuilds_clean(self):
        old = {"acc_ver": 3, "scanned_at": NOW, "files": {
            "/h/.pi/x.jsonl": {"kind": "pi", "last_ts": NOW, "prov": {
                "zai": {"days": {tc._local_date(NOW): {"tok": 9}},
                        "mday": {"glm-4.7": {tc._local_date(NOW): 9}}}}}}}
        self.assertNotEqual(tc.ACC_VERSION, 3)
        cache = old
        if cache.get("acc_ver") != tc.ACC_VERSION:     # collect()'s rule
            cache = {"acc_ver": tc.ACC_VERSION}
            cache["scanned_at"] = 0
        self.assertIsNone(cache.get("files"))          # numbers never mix


class TestStates(Base):
    def _w(self, pct, age=0.0, kind="session", net=False):
        return {"kind": kind, "used_pct": pct, "observed_at": NOW - age,
                "net": net}

    def test_thresholds(self):
        for pct, want in ((0.0, "ready"), (69.9, "ready"), (70.0, "pressure"),
                          (89.0, "pressure"), (89.9, "pressure"),
                          (90.0, "critical"), (97.0, "critical"),
                          (97.9, "critical"), (98.0, "exhausted"),
                          (100.0, "exhausted")):
            state, _ = tc._state_for([self._w(pct)], None, NOW)
            self.assertEqual(state, want, pct)

    def test_worst_window_wins(self):
        state, driver = tc._state_for(
            [self._w(10.0), self._w(96.0, kind="weekly")], None, NOW)
        self.assertEqual(state, "critical")
        self.assertEqual(driver["used_pct"], 96.0)

    def test_stale_windows_are_not_trusted(self):
        state, driver = tc._state_for([self._w(99.0, age=tc.LOCAL_STALE_S + 1)],
                                      None, NOW)
        self.assertEqual(state, "unknown")
        self.assertIsNone(driver)
        state, _ = tc._state_for(
            [self._w(99.0, age=tc.NET_STALE_S - 5, net=True)], None, NOW)
        self.assertEqual(state, "exhausted")          # network bound 30 min

    def test_claude_429_future_reset_exhausts(self):
        self.put_fixture(".claude/projects/p1/r429.jsonl", "claude_429.jsonl")
        rec = self.prov("claude")
        self.assertEqual(rec["state"], "exhausted")
        self.assertEqual(rec["windows"],
                         [{"kind": "session", "resets_at": int(NOW + 3600)}])
        self.assertEqual(rec["observed_at"], int(NOW))
        self.assertEqual(rec["source"], "claude_local")

    def test_claude_429_past_reset_not_exhausted(self):
        text = self.fixture("claude_429.jsonl").replace(
            '"resetsAt":%d' % (NOW + 3600), '"resetsAt":%d' % (NOW - 60))
        self.put(".claude/projects/p1/r429.jsonl", text)
        rec = self.prov("claude")
        self.assertIsNotNone(rec)
        self.assertNotEqual(rec["state"], "exhausted")
        self.assertNotIn("windows", rec)

    def test_provider_state_prefers_429_reset(self):
        self.put_fixture(".claude/projects/p1/r429.jsonl", "claude_429.jsonl")
        self.put_fixture(".claude/projects/p1/w.jsonl", "claude_week.jsonl")
        st = tc.provider_state("claude", now=NOW, home=self.home,
                               cache_path=self.cache)
        self.assertEqual(st["id"], "claude")
        self.assertEqual(st["state"], "exhausted")
        self.assertEqual(st["resets_at"], int(NOW + 3600))
        self.assertEqual(st["source"], "claude_local")

    def test_provider_state_codex_worst_window_reset(self):
        self.put_fixture(".codex/sessions/2025/10/rollout-a.jsonl",
                         "codex_full.jsonl")
        st = tc.provider_state("codex", now=NOW, home=self.home,
                               cache_path=self.cache)
        self.assertEqual(st["state"], "critical")
        self.assertEqual(st["resets_at"], int(NOW + 3600))

    def test_provider_state_unknown_provider(self):
        self.assertIsNone(tc.provider_state("nosuch", now=NOW,
                                            home=self.home,
                                            cache_path=self.cache))


# ---------- appearance, cache, incremental scans ----------
class TestScanAndCache(Base):
    def test_empty_and_garbage_inputs(self):
        self.put(".claude/projects/p/e1.jsonl", "")
        self.put(".codex/sessions/2025/10/rollout-e.jsonl", "")
        self.put(".pi/agent/sessions/s/e2.jsonl", "")
        self.put(".claude/projects/p/e2.jsonl", "not json at all\n")
        self.put(".codex/sessions/2025/10/rollout-f.jsonl",
                 '{"type":"event_msg","payload":{"type":"token_count"\n')
        self.assertEqual(self.collect()["providers"], [])

    def test_unknown_keys_and_types_ignored(self):
        weird = {
            "type": "assistant", "timestamp": iso(NOW), "weird": {"a": 1},
            "message": {"model": "m", "usage": {
                "input_tokens": 5, "output_tokens": 5,
                "future_field": {"x": "y"}, "weird_ratio": "nan"}}}
        self.put(".claude/projects/p/w.jsonl",
                 json.dumps(weird, separators=(",", ":")) + "\n")
        rec = self.prov("claude")
        self.assertEqual(rec["tokens_today"], 10)

    def test_truncated_tail_line_waits_for_the_rest(self):
        full = json.dumps({
            "type": "assistant", "timestamp": iso(NOW + 120),
            "message": {"model": "m", "usage": {
                "input_tokens": 11, "output_tokens": 7}}},
            separators=(",", ":"))
        path = self.put(".claude/projects/p/t.jsonl",
                        json.dumps({
                            "type": "assistant", "timestamp": iso(NOW),
                            "message": {"model": "m", "usage": {
                                "input_tokens": 3, "output_tokens": 4}}},
                            separators=(",", ":")) + "\n")
        self.assertEqual(self.prov("claude")["tokens_today"], 7)
        with open(path, "a") as f:
            f.write(full[:40])                        # partial line, no \n
        self.assertEqual(self.prov("claude", now=NOW + 61)["tokens_today"], 7)
        with open(path, "a") as f:
            f.write(full[40:] + "\n")                 # now complete
        self.assertEqual(self.prov("claude", now=NOW + 122)["tokens_today"],
                         25)                          # 7 + 18

    def test_truncation_resets_the_accumulators(self):
        body = json.dumps({
            "type": "assistant", "timestamp": iso(NOW),
            "message": {"model": "m", "usage": {
                "input_tokens": 30, "output_tokens": 10}}},
            separators=(",", ":")) + "\n"
        path = self.put(".claude/projects/p/x.jsonl", body * 2)
        self.assertEqual(self.prov("claude")["tokens_today"], 80)
        with open(path, "w") as f:                    # rotated to one line
            f.write(body)
        rec = self.prov("claude", now=NOW + 61)
        self.assertEqual(rec["tokens_today"], 40)

    def test_local_min_interval_gates_rescans(self):
        path = self.put(".claude/projects/p/m.jsonl", json.dumps({
            "type": "assistant", "timestamp": iso(NOW),
            "message": {"model": "m", "usage": {
                "input_tokens": 5, "output_tokens": 5}}},
            separators=(",", ":")) + "\n")
        self.assertEqual(self.prov("claude")["tokens_today"], 10)
        with open(path, "a") as f:                    # appended too soon
            f.write(json.dumps({
                "type": "assistant", "timestamp": iso(NOW + 1),
                "message": {"model": "m", "usage": {
                    "input_tokens": 5, "output_tokens": 5}}},
                separators=(",", ":")) + "\n")
        self.assertEqual(self.prov("claude", now=NOW + 30)["tokens_today"], 10)
        self.assertEqual(self.prov("claude", now=NOW + 61)["tokens_today"], 20)

    def test_budget_returns_fast_and_advances(self):
        line = json.dumps({
            "type": "assistant", "timestamp": iso(NOW),
            "message": {"model": "m", "usage": {
                "input_tokens": 2, "output_tokens": 1}}},
            separators=(",", ":")) + "\n"
        path = self.put(".claude/projects/p/huge.jsonl", line * 40000)
        t0 = time.monotonic()
        sec = self.collect(budget_s=0.15)
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertIn("providers", sec)
        entry = tc._load_cache(self.cache)["files"][
            os.path.realpath(path)]
        self.assertLess(entry["offset"], os.path.getsize(path))
        seen = [entry["offset"]]
        for i in range(1, 12):
            self.collect(budget_s=0.15, now=NOW + i * 61)
            seen.append(tc._load_cache(self.cache)["files"][
                os.path.realpath(path)]["offset"])
        self.assertEqual(seen, sorted(seen))          # progress is monotonic
        self.assertGreater(seen[-1], seen[0])

    def test_snapshot_section_serves_fresh_cache_tuple(self):
        section = {"generated_at": int(NOW) - 5, "providers": [{"id": "x"}]}
        tc._save_cache({"acc_ver": tc.ACC_VERSION, "built_at": NOW,
                        "section": section}, self.cache)
        sec, cap = tc.snapshot_section(now=NOW + 1, home=self.home,
                                       cache_path=self.cache)
        self.assertEqual(cap, "capacity")
        self.assertEqual(sec, section)

    def test_snapshot_section_rebuilds_when_stale_or_old_version(self):
        tc._save_cache({"built_at": NOW - 3600,
                        "section": {"generated_at": 1, "providers": []}},
                       self.cache)
        sec, cap = tc.snapshot_section(now=NOW, home=self.home,
                                       cache_path=self.cache)
        self.assertEqual((bool(sec), cap), (True, "capacity"))
        self.assertEqual(sec["providers"], [])        # empty home: nothing
        tc._save_cache({"built_at": NOW,             # pre-ACC_VERSION cache
                        "section": {"generated_at": 1,
                                    "providers": [{"tokens_today": 10 ** 9}]}},
                       self.cache)
        sec, _ = tc.snapshot_section(now=NOW + 1, home=self.home,
                                     cache_path=self.cache)
        self.assertEqual(sec["providers"], [])        # definition change wins

    def test_snapshot_section_never_overruns_its_budget(self):
        line = json.dumps({
            "type": "assistant", "timestamp": iso(NOW),
            "message": {"model": "m", "usage": {
                "input_tokens": 2, "output_tokens": 1}}},
            separators=(",", ":")) + "\n"
        self.put(".claude/projects/p/huge.jsonl", line * 40000)
        t0 = time.monotonic()
        sec, cap = tc.snapshot_section(budget_s=0.2, now=NOW, home=self.home,
                                       cache_path=self.cache)
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertEqual(cap, "capacity")
        self.assertIsInstance(sec, dict)


class TestCacheWrites(Base):
    """Security review: the cache/config writers must use a unique
    mkstemp name (a fixed `.tmp` lets two interleaved writers corrupt
    each other), fsync, os.replace and land at 0600."""

    def test_stale_fixed_tmp_never_poisons_writes(self):
        os.makedirs(os.path.dirname(self.cache), exist_ok=True)
        with open(self.cache + ".tmp", "w") as f:
            f.write('{"stale": ')               # leftover from an old writer
        tc._save_cache({"built_at": NOW}, self.cache)
        tc._save_cache({"built_at": NOW, "n": 2}, self.cache)
        self.assertEqual(tc._load_cache(self.cache),
                         {"built_at": NOW, "n": 2})

    def test_concurrent_writers_never_produce_corrupt_json(self):
        import threading
        docs = [{"writer": i, "built_at": NOW} for i in range(2)]
        errors = []

        def writer(doc):
            try:
                for _ in range(40):
                    tc._save_cache(doc, self.cache)
            except Exception as exc:            # must never raise
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(d,)) for d in docs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        loaded = tc._load_cache(self.cache)     # _load_cache: {} on garbage
        self.assertIn(loaded, docs)
        litter = [f for f in os.listdir(os.path.dirname(self.cache))
                  if f.startswith(".capacity-")]
        self.assertEqual(litter, [])

    def test_cache_and_config_written_0600(self):
        tc._save_cache({"built_at": NOW}, self.cache)
        self.assertEqual(oct(os.stat(self.cache).st_mode & 0o777),
                         oct(0o600))
        self.assertTrue(tc.set_enabled("claude_oauth", True,
                                       home=self.home))
        cfg = os.path.join(self.home, ".config", "tendril", "capacity.json")
        self.assertEqual(oct(os.stat(cfg).st_mode & 0o777), oct(0o600))
        with open(cfg) as f:
            self.assertEqual(json.load(f), {"enabled": ["claude_oauth"]})
        self.assertFalse(os.path.exists(cfg + ".tmp"))


# ---------- network adapters (mocked; NO real network) ----------
class TestNetwork(Base):
    def setUp(self):
        super().setUp()
        # the home-derived cache path, so set_enabled()'s 401-revival sees
        # the same net state the collects write
        self.cache = os.path.join(self.home, ".cache", "tendril",
                                  "capacity.json")

    def _enabled_home(self, adapter="claude_oauth", cred="claude"):
        self.cred(cred)
        self.enable(adapter)

    def test_no_fetch_without_optin(self):
        fake = fake_urllib(body="{}")
        with mock.patch.object(tc._OPENER, "open", fake):
            self.collect(refresh=True, budget_s=30)
        self.assertEqual(fake.calls, [])

    def test_no_fetch_below_network_budget(self):
        self._enabled_home()
        fake = fake_urllib(body="{}")
        with mock.patch.object(tc._OPENER, "open", fake):
            self.collect(refresh=False, budget_s=5.0)   # 5 < 11 s
        self.assertEqual(fake.calls, [])

    def test_redirect_refused_bearer_never_resent(self):
        """Security review: a 302 must not let urllib re-send the
        Authorization header to the redirect target. The no-redirect
        opener classifies the redirect as "net" and makes no second
        request at all — verified against a real local server."""
        hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                if self.path == "/":
                    self.send_response(302)
                    self.send_header("Location", "/final")
                    self.end_headers()
                else:
                    body = b"{\"steal\": true}"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)          # LIFO: shutdown, then close
        url = "http://127.0.0.1:%d/" % srv.server_port
        kind, data = tc._http_get_json(url, "Bearer-goes-here", None)
        self.assertEqual((kind, data), ("net", None))
        self.assertEqual(hits, ["/"], "no request may follow the redirect")

    def test_redirect_responses_are_classified_net(self):
        """The opener raises HTTPError for a refused 3xx; every redirect
        class must come back as "net", never "http" (retryable) or data."""
        for code in (301, 302, 303, 307, 308):
            fake = fake_urllib(code=code)
            with mock.patch.object(tc._OPENER, "open", fake):
                kind, data = tc._http_get_json(
                    "https://capacity.example.test/u", "tok", None)
            self.assertEqual((kind, data), ("net", None), "code %d" % code)

    def test_oauth_iso_fetch(self):
        self._enabled_home()
        fake = fake_urllib(body=self.fixture("oauth_iso.json"))
        with mock.patch.object(tc._OPENER, "open", fake):
            rec = self.prov("claude")
        self.assertEqual(len(fake.calls), 1)
        url, headers, timeout = fake.calls[0]
        self.assertTrue(url.startswith("https://api.anthropic.com/"))
        self.assertEqual(timeout, 10)
        auth = headers.get("authorization", "")
        self.assertTrue(auth.startswith("Bearer "))
        self.assertGreater(len(auth), 12)
        self.assertEqual(rec["state"], "critical")     # 0.91 → 91.0
        self.assertEqual(rec["source"], "claude_oauth")
        self.assertEqual(rec["plan"], "max")           # subscriptionType
        kinds = {w["kind"]: w for w in rec["windows"]}
        self.assertEqual(kinds["session"]["used_pct"], 91.0)
        self.assertEqual(kinds["session"]["resets_at"], int(NOW + 3600))
        self.assertEqual(kinds["session"]["window_min"], 300)
        self.assertEqual(kinds["weekly"]["used_pct"], 42.0)
        self.assertEqual(kinds["weekly"]["resets_at"], int(NOW + 8 * 86400))
        no_leak(self, MARKER, json.dumps(rec), "wire record")

    def test_oauth_epoch_resets_exhausted(self):
        self._enabled_home()
        fake = fake_urllib(body=self.fixture("oauth_epoch.json"))
        with mock.patch.object(tc._OPENER, "open", fake):
            rec = self.prov("claude")
        self.assertEqual(rec["state"], "exhausted")    # 99.5 ≥ 98
        kinds = {w["kind"]: w for w in rec["windows"]}
        self.assertEqual(kinds["session"]["resets_at"], int(NOW + 3600))

    def test_wham_fetch(self):
        self._enabled_home("codex_wham", "codex")
        fake = fake_urllib(body=self.fixture("wham.json"))
        with mock.patch.object(tc._OPENER, "open", fake):
            rec = self.prov("codex")
        self.assertEqual(rec["source"], "codex_wham")
        self.assertEqual(rec["state"], "ready")        # worst 50.0
        kinds = {w["kind"]: w for w in rec["windows"]}
        self.assertEqual(kinds["session"]["used_pct"], 12.5)
        self.assertEqual(kinds["session"]["resets_at"], int(NOW + 3600))
        self.assertEqual(kinds["weekly"]["used_pct"], 50.0)
        self.assertEqual(kinds["weekly"]["resets_at"], int(NOW + 86400))
        blob = json.dumps(rec)
        no_leak(self, MARKER, blob, "wire record")
        no_leak(self, "decoy-owner", blob, "wire record")

    def test_zai_buckets_and_utilization_forms(self):
        self._enabled_home("zai_quota", "pi")
        fake = fake_urllib(body=self.fixture("zai_buckets.json"))
        with mock.patch.object(tc._OPENER, "open", fake):
            rec = self.prov("zai")
        self.assertEqual(rec["source"], "zai_quota")
        self.assertEqual(rec["harness"], "pi")
        kinds = {w["kind"]: w for w in rec["windows"]}
        self.assertEqual(kinds["session"]["used_pct"], 91.0)
        self.assertEqual(kinds["weekly"]["used_pct"], 55.0)
        self.assertEqual(kinds["daily"]["used_pct"], 30.0)  # 30/(30+70)
        self.assertEqual(rec["state"], "critical")
        # direct-form fallback
        fake = fake_urllib(body=self.fixture("zai_forms.json"))
        with mock.patch.object(tc._OPENER, "open", fake):
            rec = self.prov("zai", now=NOW + tc.NET_MIN_S + 1)
        kinds = {w["kind"]: w for w in rec["windows"]}
        self.assertEqual(kinds["session"]["used_pct"], 25.0)
        self.assertEqual(kinds["weekly"]["used_pct"], 80.0)
        self.assertEqual(rec["state"], "pressure")
        no_leak(self, MARKER, json.dumps(rec), "wire record")
        no_leak(self, "decoy-user", json.dumps(rec), "wire record")

    def test_missing_credential_is_nocred_no_fetch(self):
        self.enable("claude_oauth")                   # no cred file at all
        fake = fake_urllib(body="{}")
        with mock.patch.object(tc._OPENER, "open", fake):
            self.collect(refresh=True, budget_s=30)
        self.assertEqual(fake.calls, [])
        st = tc._load_cache(self.cache)["net"]["claude_oauth"]
        self.assertEqual(st["fail"], "nocred")

    def test_401_marks_auth_and_never_retries_until_change(self):
        self._enabled_home()
        fake = fake_urllib(code=401)
        with mock.patch.object(tc._OPENER, "open", fake):
            self.collect(refresh=True, budget_s=30)
        self.assertEqual(len(fake.calls), 1)
        with mock.patch.object(tc._OPENER, "open", fake):
            self.collect(refresh=True, budget_s=30, now=NOW + 99999)
        self.assertEqual(len(fake.calls), 1)          # dead until it changes
        # a config change (disable + enable) revives it
        self.assertTrue(tc.set_enabled("claude_oauth", False, home=self.home))
        self.assertTrue(tc.set_enabled("claude_oauth", True, home=self.home))
        with mock.patch.object(tc._OPENER, "open", fake):
            self.collect(refresh=True, budget_s=30, now=NOW + 199999)
        self.assertEqual(len(fake.calls), 2)

    def test_401_retries_after_credential_file_changes(self):
        cred_path = self.cred("claude")
        self.enable("claude_oauth")
        fake = fake_urllib(code=401)
        with mock.patch.object(tc._OPENER, "open", fake):
            self.collect(refresh=True, budget_s=30)
        self.assertEqual(len(fake.calls), 1)
        with open(cred_path, "w") as f:
            f.write(self.fixture("credentials_claude.json").replace(
                "h2r-claude", "h2r-claude-2"))
        os.utime(cred_path, (time.time() + 5, time.time() + 5))
        with mock.patch.object(tc._OPENER, "open", fake):
            self.collect(refresh=True, budget_s=30, now=NOW + 9999)
        self.assertEqual(len(fake.calls), 2)

    def test_429_backoff_doubles(self):
        self._enabled_home()
        fake = fake_urllib(code=429)
        with mock.patch.object(tc._OPENER, "open", fake):
            self.collect(refresh=True, budget_s=30)          # fail 1
        st = tc._load_cache(self.cache)["net"]["claude_oauth"]
        self.assertEqual(st["fail"], "rate")
        self.assertEqual(st["backoff"], 600)                 # 2 × 300
        with mock.patch.object(tc._OPENER, "open", fake):
            self.collect(refresh=True, budget_s=30, now=NOW + 100)
        self.assertEqual(len(fake.calls), 1)                 # still backing off
        with mock.patch.object(tc._OPENER, "open", fake):
            self.collect(refresh=True, budget_s=30, now=NOW + 700)
        self.assertEqual(len(fake.calls), 2)                 # fail 2
        st = tc._load_cache(self.cache)["net"]["claude_oauth"]
        self.assertEqual(st["backoff"], 1200)

    def test_shape_failure_then_interval_gate(self):
        self._enabled_home()
        fake = fake_urllib(body="this is not json")
        with mock.patch.object(tc._OPENER, "open", fake):
            rec = self.prov("claude")
        self.assertIsNone(rec)                         # no data ⇒ absent
        st = tc._load_cache(self.cache)["net"]["claude_oauth"]
        self.assertEqual(st["fail"], "shape")
        self.assertEqual(st["backoff"], 600)           # 2 × 300 s
        with mock.patch.object(tc._OPENER, "open", fake):
            self.collect(refresh=True, budget_s=30, now=NOW + 60)
        self.assertEqual(len(fake.calls), 1)           # still backing off
        fake2 = fake_urllib(body=self.fixture("oauth_iso.json"))
        with mock.patch.object(tc._OPENER, "open", fake2):
            self.collect(refresh=True, budget_s=30, now=NOW + 700)
        self.assertEqual(len(fake2.calls), 1)          # backoff elapsed: retry
        with mock.patch.object(tc._OPENER, "open", fake2):
            self.collect(refresh=True, budget_s=30, now=NOW + 900)
        self.assertEqual(len(fake2.calls), 1)          # 200 s < 300 s interval
        with mock.patch.object(tc._OPENER, "open", fake2):
            rec = self.prov("claude", now=NOW + 1100)
        self.assertEqual(len(fake2.calls), 2)          # due again
        self.assertEqual(rec["state"], "critical")

    def test_exception_text_never_reaches_the_cache(self):
        self._enabled_home()
        boom = RuntimeError("urllib exploded: " + MARKER)
        fake = fake_urllib(exc=boom)
        with mock.patch.object(tc._OPENER, "open", fake):
            self.collect(refresh=True, budget_s=30)
        st = tc._load_cache(self.cache)["net"]["claude_oauth"]
        self.assertEqual(st["fail"], "net")
        no_leak(self, MARKER, self._cache_bytes(), "cache file")

    def _cache_bytes(self):
        with open(self.cache, "rb") as f:
            return f.read()

    def test_secrets_never_reach_cache_or_section(self):
        self._enabled_home()
        fake = fake_urllib(body=self.fixture("oauth_iso.json"))
        with mock.patch.object(tc._OPENER, "open", fake):
            sec = self.collect(refresh=True, budget_s=30)
        no_leak(self, MARKER, json.dumps(sec), "section")
        cached = self._cache_bytes()
        no_leak(self, MARKER, cached, "cache file")
        no_leak(self, DECOY_EMAIL, json.dumps(sec), "section")
        no_leak(self, DECOY_EMAIL, cached, "cache file")

    def test_junk_config_enables_nothing(self):
        self.cred("claude")
        self.put(".config/tendril/capacity.json",
                 self.fixture("config_junk.json"))
        fake = fake_urllib(body="{}")
        with mock.patch.object(tc._OPENER, "open", fake):
            self.collect(refresh=True, budget_s=30)
        self.assertEqual(fake.calls, [])
        self.assertEqual(tc.enabled_ids(self.home), [])


# ---------- config round-trip ----------
class TestConfig(Base):
    def test_enable_disable_roundtrip(self):
        self.assertEqual(tc.enabled_ids(self.home), [])
        self.assertTrue(tc.set_enabled("zai_quota", True, home=self.home))
        self.assertEqual(tc.enabled_ids(self.home), ["zai_quota"])
        self.assertTrue(
            tc.set_enabled("claude_oauth", True, home=self.home))
        self.assertEqual(tc.enabled_ids(self.home),
                         ["claude_oauth", "zai_quota"])   # stable order
        self.assertTrue(tc.set_enabled("zai_quota", False, home=self.home))
        self.assertEqual(tc.enabled_ids(self.home), ["claude_oauth"])
        path = os.path.join(self.home, ".config", "tendril",
                            "capacity.json")
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertEqual(
            oct(os.stat(os.path.dirname(path)).st_mode & 0o777), "0o700")

    def test_invalid_ids_refused(self):
        for bad in ("claude", "bogus", "", "claude_oauth ", "../../etc"):
            self.assertFalse(tc.set_enabled(bad, True, home=self.home))
        self.assertEqual(tc.enabled_ids(self.home), [])


# ---------- scheduler-tick background refresh (contract §2 defect fix) ----------
class TestBackgroundRefresh(Base):
    """background_refresh — the tick's capacity keeper: returns when no
    adapter is enabled; otherwise one bounded collect() whose budget covers
    a single worst-case 10 s fetch. The existing per-adapter gates (300 s
    min interval, 2× backoff, never-retry-401, nocred) decide whether the
    network is actually hit; a busy lock is a skip; a blow-up never
    propagates and releases the lock. urllib is mocked throughout."""

    def setUp(self):
        super().setUp()
        # the home-derived cache path, exactly what background_refresh and
        # set_enabled's 401-revival use
        self.cache = os.path.join(self.home, ".cache", "tendril",
                                  "capacity.json")

    def seed(self, age=None, adapter="claude_oauth", cred="claude"):
        """Enable the adapter (credential installed) and optionally plant a
        stale net sample observed `age` seconds ago."""
        if cred:
            self.cred(cred)
        self.enable(adapter)
        if age is not None:
            st = tc._fresh_net_state()
            st["observed_at"] = NOW - age
            tc._save_cache({"acc_ver": tc.ACC_VERSION,
                            "net": {adapter: st}}, self.cache)

    def test_no_enabled_adapter_never_fetches(self):
        fake = fake_urllib(body="{}")
        with mock.patch.object(tc._OPENER, "open", fake):
            self.assertFalse(tc.background_refresh(now=NOW, home=self.home))
        self.assertEqual(fake.calls, [])
        self.assertFalse(os.path.exists(self.cache))   # not even a write

    def test_stale_sample_is_fetched_exactly_once(self):
        self.seed(age=tc.NET_MIN_S + 1)
        fake = fake_urllib(body=self.fixture("oauth_iso.json"))
        with mock.patch.object(tc._OPENER, "open", fake):
            self.assertTrue(tc.background_refresh(now=NOW, home=self.home))
        self.assertEqual(len(fake.calls), 1)
        url, headers, timeout = fake.calls[0]
        self.assertTrue(url.startswith("https://api.anthropic.com/"))
        self.assertEqual(timeout, 10)
        self.assertTrue(headers.get("authorization", "").startswith("Bearer "))
        # the fresh section is in the cache for the phone's next snapshot
        recs = tc._load_cache(self.cache).get("section", {}).get("providers")
        self.assertEqual(recs[0]["source"], "claude_oauth")
        self.assertEqual(recs[0]["state"], "critical")

    def test_min_interval_governs_consecutive_ticks(self):
        self.seed(age=tc.NET_MIN_S + 1)
        fake = fake_urllib(body=self.fixture("oauth_iso.json"))
        with mock.patch.object(tc._OPENER, "open", fake):
            tc.background_refresh(now=NOW, home=self.home)
            self.assertEqual(len(fake.calls), 1)        # stale: fetched
            tc.background_refresh(now=NOW + 30, home=self.home)
            self.assertEqual(len(fake.calls), 1)        # 30 s < 300 s: skip
            tc.background_refresh(now=NOW + 60, home=self.home)
            self.assertEqual(len(fake.calls), 1)        # 60 s < 300 s: skip
            tc.background_refresh(now=NOW + 301, home=self.home)
            self.assertEqual(len(fake.calls), 2)        # due again: fetched

    def test_scan_gate_does_not_starve_the_network_phase(self):
        """The defect itself: snapshot collects (1 s budget) keep the
        cache freshly scanned, so a refresh nested behind the 60 s local
        scan gate could never reach a due adapter — 30 min later both
        adapters were unknown/no_data. The network phase must run on its
        own budget/interval gates."""
        self.seed(age=tc.NET_MIN_S + 1)
        fake = fake_urllib(body=self.fixture("oauth_iso.json"))
        with mock.patch.object(tc._OPENER, "open", fake):
            tc.collect(budget_s=1.0, now=NOW - 31, home=self.home,
                       cache_path=self.cache)   # snapshot-like fresh scan
            self.assertEqual(fake.calls, [])     # 1 s budget: never fetches
            tc.background_refresh(now=NOW, home=self.home)
            self.assertEqual(len(fake.calls), 1)  # tick still reaches it

    def test_401_is_never_retried_by_background_refresh(self):
        self.seed()
        fake = fake_urllib(code=401)
        with mock.patch.object(tc._OPENER, "open", fake):
            tc.background_refresh(now=NOW, home=self.home)
            self.assertEqual(len(fake.calls), 1)
            tc.background_refresh(now=NOW + 90000, home=self.home)
            self.assertEqual(len(fake.calls), 1)   # dead until it changes

    def test_nocred_records_the_word_and_never_fetches(self):
        self.seed(cred=None)                       # enabled, no credential
        fake = fake_urllib(body="{}")
        with mock.patch.object(tc._OPENER, "open", fake):
            tc.background_refresh(now=NOW, home=self.home)
        self.assertEqual(fake.calls, [])
        st = tc._load_cache(self.cache)["net"]["claude_oauth"]
        self.assertEqual(st["fail"], "nocred")

    def test_busy_lock_skips_never_waits(self):
        self.seed(age=tc.NET_MIN_S + 1)
        fake = fake_urllib(body="{}")
        lock = os.path.join(self.home, ".cache", "tendril",
                            "capacity-refresh.lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)             # an overlapping tick
        try:
            with mock.patch.object(tc._OPENER, "open", fake):
                self.assertFalse(tc.background_refresh(
                    now=NOW, home=self.home, lock_path=lock))
            self.assertEqual(fake.calls, [])       # skipped, not waited on
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        with mock.patch.object(tc._OPENER, "open", fake):
            self.assertTrue(tc.background_refresh(now=NOW, home=self.home))
        self.assertEqual(len(fake.calls), 1)       # released: next tick works

    def test_refresh_exception_never_propagates_and_releases_the_lock(self):
        self.seed()
        with mock.patch.object(tc, "collect",
                               side_effect=RuntimeError("boom")):
            self.assertTrue(tc.background_refresh(now=NOW, home=self.home))
        with mock.patch.object(tc, "collect",
                               side_effect=RuntimeError("boom")) as m:
            tc.background_refresh(now=NOW + 1, home=self.home)
        self.assertEqual(m.call_count, 1)          # lock was not leaked

    def test_net_phase_deadline_gate_bounds_a_multi_adapter_pass(self):
        """The ≤ 12 s wall bound: an adapter's fetch only starts while a
        full 10 s timeout still fits the pass budget."""
        self.seed()
        cache = {"acc_ver": tc.ACC_VERSION, "net": {}}
        fake = fake_urllib(body=self.fixture("oauth_iso.json"))
        with mock.patch.object(tc._OPENER, "open", fake):
            tc._net_phase(cache, NOW, True, self.home,
                          net_deadline=tc._mono() - 1)
            self.assertEqual(fake.calls, [])       # out of budget: none
            tc._net_phase(cache, NOW, True, self.home,
                          net_deadline=tc._mono() + tc.NET_TIMEOUT_S + 1.0)
            self.assertEqual(len(fake.calls), 1)   # a full timeout fits

    def test_background_budget_covers_one_fetch_and_stays_bounded(self):
        self.assertGreaterEqual(tc.BG_BUDGET_S, tc.NET_BUDGET_MIN_S)
        self.assertLessEqual(tc.BG_BUDGET_S, 12.0)

    def test_adapter_reported_and_first_sample_after_enable(self):
        self.seed()
        self.assertFalse(tc.adapter_reported("claude_oauth", home=self.home))
        self.assertFalse(tc.adapter_reported("bogus", home=self.home))
        fake = fake_urllib(body=self.fixture("oauth_iso.json"))
        with mock.patch.object(tc._OPENER, "open", fake):
            tc.background_refresh(now=NOW, home=self.home)
        self.assertTrue(tc.adapter_reported("claude_oauth", home=self.home))


# ---------- human status + the remote-agents verb ----------
class TestCliAndStatus(Base):
    def _env(self):
        env = dict(os.environ)
        env["HOME"] = self.home
        return env

    def _cli(self, *args):
        return subprocess.run(
            [sys.executable, CLI, "capacity", *args], env=self._env(),
            capture_output=True, text=True, timeout=60)

    def test_status_text_table_and_no_secrets(self):
        self.put_fixture(".claude/projects/p1/s1.jsonl", "claude_week.jsonl")
        sec = self.collect()
        text = tc.status_text(sec, now=NOW, enabled=["claude_oauth"])
        self.assertIn("claude", text)
        self.assertIn("unknown", text)
        self.assertIn("1.6K", text)
        self.assertIn("claude_oauth", text)
        no_leak(self, MARKER, text, "status output")
        self.assertEqual(tc.status_text({"providers": []}, now=NOW),
                         "no provider usage found on this host")

    def _live_claude_line(self, tokens_in=1000, tokens_out=500):
        # CLI subprocesses run with the real wall clock, so the transcript
        # line must carry a real-time timestamp to land in the 7-day window
        line = json.dumps({
            "type": "assistant",
            "timestamp": iso(time.time()),
            "message": {"model": "claude-opus-4-8", "usage": {
                "input_tokens": tokens_in, "output_tokens": tokens_out,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 900000}}},
            separators=(",", ":"))
        return line + "\n"

    def test_cli_json_one_line_no_secrets(self):
        self.put(".claude/projects/p1/s1.jsonl", self._live_claude_line())
        self.cred("claude")
        self.put(".config/tendril/capacity.json", '{"enabled":[]}')
        r = self._cli("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        sec = json.loads(lines[0])
        self.assertEqual(sec["providers"][0]["id"], "claude")
        self.assertEqual(sec["providers"][0]["tokens_today"], 1600)
        no_leak(self, MARKER, r.stdout, "cli stdout")
        no_leak(self, MARKER, r.stderr, "cli stderr")

    def test_cli_enable_disable_and_validation(self):
        r = self._cli("enable", "bogus_adapter")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown adapter", r.stderr)
        r = self._cli("enable", "zai_quota")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(tc.enabled_ids(self.home), ["zai_quota"])
        cfg = os.path.join(self.home, ".config", "tendril", "capacity.json")
        with open(cfg, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"enabled": ["zai_quota"]})
        r = self._cli("disable", "zai_quota")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(tc.enabled_ids(self.home), [])
        r = self._cli("frobnicate")
        self.assertEqual(r.returncode, 2)

    def test_cli_status(self):
        r = self._cli("status")
        self.assertEqual(r.returncode, 0)
        self.assertIn("no provider usage found on this host", r.stdout)
        self.put(".claude/projects/p1/s1.jsonl", self._live_claude_line())
        # drop the cache the first status run wrote: the 60 s local min
        # interval would otherwise serve it unchanged
        cachef = os.path.join(self.home, ".cache", "tendril",
                              "capacity.json")
        if os.path.exists(cachef):
            os.unlink(cachef)
        r = self._cli("status")
        self.assertEqual(r.returncode, 0)
        self.assertIn("claude", r.stdout)
        self.assertIn("1.6K", r.stdout)
        no_leak(self, MARKER, r.stdout, "cli stdout")

    def test_cli_missing_module(self):
        env = self._env()
        env["TENDRIL_TEST_HIDE"] = "1"
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.argv=['remote-agents','capacity','--json'];\n"
             "import importlib.machinery, importlib.util, json;\n"
             "loader=importlib.machinery.SourceFileLoader('ra', %r);\n"
             "spec=importlib.util.spec_from_loader('ra', loader);\n"
             "ra=importlib.util.module_from_spec(spec);\n"
             "sys.modules['tendril_capacity']=None;\n"
             "loader.exec_module(ra);\n"
             "sys.exit(ra.main())" % CLI],
            env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("missing next to remote-agents", r.stderr)
        no_leak(self, MARKER, r.stdout + r.stderr, "cli output")


class TestReasonsAndHealth(Base):
    """§7.5: unknown providers say WHY (reason + enable id); §7.1: zero
    cost is a provider-managed/unknown route, never a 0.0 charge; the
    health module's read-only section hook.
    These drive _assemble/health_section directly (the scanner-produced
    cache shape), so no fixture transcript churn is needed."""

    def cache_with(self, files, net=None):
        cache = {"acc_ver": tc.ACC_VERSION, "files": files}
        if net:
            cache["net"] = net
        return cache

    def prov(self, pid, cache, enabled=()):
        sec = tc._assemble(cache, NOW, enabled=list(enabled))
        for rec in sec.get("providers", []):
            if rec.get("id") == pid:
                return rec
        self.fail("provider %s missing" % pid)

    def claude_unknown(self, net=None):
        return self.cache_with({
            "/h/.claude/s.jsonl": {"kind": "claude", "last_ts": NOW,
                                   "days": {tc._local_date(NOW):
                                            {"tok": 50}}, "mday": {}}},
            net=net)

    def test_disabled_adapter_reason_and_enable_id(self):
        rec = self.prov("claude", self.claude_unknown())
        self.assertEqual(rec["state"], "unknown")
        self.assertEqual(rec["reason"], "adapter_disabled")
        self.assertEqual(rec["enable"], "claude_oauth")

    def test_adapter_failures_map_to_reasons(self):
        rec = self.prov("claude", self.claude_unknown(
            {"claude_oauth": {"fail": "auth"}}), enabled=("claude_oauth",))
        self.assertEqual(rec["reason"], "auth_error")
        self.assertNotIn("enable", rec)
        rec = self.prov("claude", self.claude_unknown(
            {"claude_oauth": {"fail": "net"}}), enabled=("claude_oauth",))
        self.assertEqual(rec["reason"], "network_error")
        rec = self.prov("claude", self.claude_unknown(),
                        enabled=("claude_oauth",))
        self.assertEqual(rec["reason"], "no_data")

    def test_provider_without_adapter_is_feature_missing(self):
        cache = self.cache_with({
            "/h/.pi/g.jsonl": {"kind": "pi", "last_ts": NOW, "prov": {
                "google": {"days": {tc._local_date(NOW):
                                    {"tok": 10}}, "mday": {}}}}})
        rec = self.prov("google", cache)
        self.assertEqual(rec["reason"], "feature_missing")
        self.assertNotIn("enable", rec)

    def test_zero_cost_today_is_omitted_never_zero(self):
        today, yday = tc._local_date(NOW), tc._local_date(NOW - 86400)
        cache = self.cache_with({
            "/h/.pi/z.jsonl": {"kind": "pi", "last_ts": NOW, "prov": {
                "zai": {"days": {yday: {"tok": 100, "cost": 0.5},
                                 today: {"tok": 10, "cost": 0.0}},
                        "mday": {}}}}})
        rec = self.prov("zai", cache)
        self.assertNotIn("cost_today_usd", rec)   # §7.1: never a fake 0.0

    def test_positive_cost_today_still_emitted(self):
        today = tc._local_date(NOW)
        cache = self.cache_with({
            "/h/.pi/z.jsonl": {"kind": "pi", "last_ts": NOW, "prov": {
                "zai": {"days": {today: {"tok": 100, "cost": 0.25}},
                        "mday": {}}}}})
        rec = self.prov("zai", cache, enabled=("zai_quota",))
        self.assertEqual(rec["cost_today_usd"], 0.25)

    def test_health_section_states(self):
        # no cache at all → absent
        self.assertEqual(tc.health_section(home=self.home,
                                           cache_path=self.cache),
                         {"state": "absent", "enabled": []})
        # a usable cached section → available (and read-only: no file made)
        cache = self.claude_unknown()
        cache["section"] = tc._assemble(cache, NOW, enabled=[])
        os.makedirs(os.path.dirname(self.cache), exist_ok=True)
        tc._save_cache(cache, self.cache)
        before = os.stat(self.cache).st_mtime_ns
        doc = tc.health_section(home=self.home, cache_path=self.cache)
        self.assertEqual(doc, {"state": "adapters_disabled",
                               "enabled": []})
        self.assertEqual(os.stat(self.cache).st_mtime_ns, before)
        # one healthy provider → available
        cache["section"]["providers"][0]["state"] = "ready"
        tc._save_cache(cache, self.cache)
        doc = tc.health_section(home=self.home, cache_path=self.cache)
        self.assertEqual(doc["state"], "available")
        # enabled adapter flips the designed state even with all-unknown
        with mock.patch.object(tc, "enabled_ids", return_value=["zai_quota"]):
            doc = tc.health_section(home=self.home, cache_path=self.cache)
        self.assertEqual(doc, {"state": "available",
                               "enabled": ["zai_quota"]})
        # unreadable cache is an error, never a raise
        with mock.patch.object(tc, "_load_cache",
                               side_effect=RuntimeError("boom")):
            doc = tc.health_section(home=self.home, cache_path=self.cache)
        self.assertEqual(doc, {"state": "error", "enabled": []})

    def test_enable_is_idempotent(self):
        for _ in range(2):
            self.assertTrue(tc.set_enabled("claude_oauth", True,
                                           home=self.home))
            self.assertTrue(tc.set_enabled("zai_quota", True,
                                           home=self.home))
        with open(os.path.join(self.home, ".config", "tendril",
                               "capacity.json")) as f:
            self.assertEqual(json.load(f),
                             {"enabled": ["claude_oauth", "zai_quota"]})
        self.assertEqual(tc.enabled_ids(self.home),
                         ["claude_oauth", "zai_quota"])

    def bridge_cache(self, extra_files=None):
        """A pi-via-claude-bridge day (tokens, cost 0.0 — never metered)
        plus a claude_local day with real tokens, and an optollm route."""
        today = tc._local_date(NOW)
        files = {
            "/h/.pi/b.jsonl": {"kind": "pi", "last_ts": NOW, "prov": {
                "claude-bridge": {"days": {today: {"tok": 100,
                                                   "cost": 0.0}},
                                  "mday": {"claude-opus-5-5":
                                           {today: 100}}}}},
            "/h/.claude/c.jsonl": {"kind": "claude", "last_ts": NOW,
                                   "days": {today: {"tok": 50}},
                                   "mday": {}},
            "/h/.pi/o.jsonl": {"kind": "pi", "last_ts": NOW, "prov": {
                "optollm": {"days": {today: {"tok": 10}}, "mday": {}}}},
        }
        files.update(extra_files or {})
        return self.cache_with(files)

    def ids(self, cache, enabled=()):
        return [p["id"] for p in
                tc._assemble(cache, NOW, enabled=list(enabled))
                ["providers"]]

    def test_bridge_tokens_fold_into_claude(self):
        cache = self.bridge_cache()
        provs = {p["id"]: p for p in
                 tc._assemble(cache, NOW, enabled=[])["providers"]}
        self.assertNotIn("claude-bridge", provs)   # §7.1: never a provider
        claude = provs["claude"]
        self.assertEqual(claude["tokens_today"], 150)   # 100 bridge + 50 local
        self.assertEqual(claude["harness"], "claude")   # rollout wins
        # provider_managed route: no 0.0 cost, and it creates no windows
        self.assertNotIn("cost_today_usd", claude)
        self.assertEqual(claude["state"], "unknown")
        self.assertNotIn("windows", claude)
        # bridged model usage lands in the upstream provider's by_model
        models = {m["model"]: m["tokens"] for m in claude["by_model"]}
        self.assertEqual(models["claude-opus-5-5"], 100)

    def test_bridge_only_host_still_gets_the_claude_entry(self):
        cache = self.bridge_cache(extra_files={
            "/h/.pi/b.jsonl": {"kind": "pi", "last_ts": NOW, "prov": {
                "claude-bridge": {"days": {tc._local_date(NOW):
                                           {"tok": 100}}, "mday": {}}}},
            "/h/.pi/o.jsonl": {"kind": "pi", "last_ts": NOW, "prov": {
                "optollm": {"days": {tc._local_date(NOW):
                                     {"tok": 10}}, "mday": {}}}},
        })
        ids = self.ids(cache)
        self.assertIn("claude", ids)               # pi data alone suffices
        self.assertNotIn("claude-bridge", ids)

    def test_no_provider_id_equals_an_adapter_or_route_name(self):
        ids = self.ids(self.bridge_cache())
        for bad in ("claude-bridge", "openai-codex", "optollm",
                    "claude_oauth", "codex_wham", "zai_quota"):
            self.assertNotIn(bad, ids)

    def test_unmapped_route_is_omitted_entirely(self):
        ids = self.ids(self.bridge_cache())
        self.assertNotIn("optollm", ids)          # no adapter → no provider

    def test_openai_codex_route_folds_into_codex(self):
        cache = self.cache_with({
            "/h/.pi/oc.jsonl": {"kind": "pi", "last_ts": NOW, "prov": {
                "openai-codex": {"days": {tc._local_date(NOW):
                                          {"tok": 70}}, "mday": {}}}}})
        provs = {p["id"]: p for p in
                 tc._assemble(cache, NOW, enabled=[])["providers"]}
        self.assertNotIn("openai-codex", provs)
        self.assertEqual(provs["codex"]["tokens_today"], 70)
        self.assertEqual(provs["codex"]["reason"], "adapter_disabled")
        self.assertEqual(provs["codex"]["enable"], "codex_wham")

    def test_scan_folds_route_pids_at_ingest(self):
        entry = tc._fresh_entry("pi")
        line = json.dumps({
            "type": "message",
            "timestamp": iso(NOW),
            "message": {"role": "assistant", "provider": "claude-bridge",
                        "model": "claude-opus-5-5",
                        "timestamp": NOW * 1000,
                        "usage": {"input": 30, "output": 4,
                                  "cacheWrite": 0,
                                  "cost": {"total": 0.0}}}},
            separators=(",", ":")).encode()
        tc._pi_line(line, entry, NOW)
        self.assertEqual(list(entry["prov"]), ["claude"])
        opto = json.dumps({
            "type": "message",
            "timestamp": iso(NOW),
            "message": {"role": "assistant", "provider": "optollm",
                        "model": "x", "timestamp": NOW * 1000,
                        "usage": {"input": 30, "output": 4,
                                  "cacheWrite": 0,
                                  "cost": {"total": 0.0}}}},
            separators=(",", ":")).encode()
        tc._pi_line(opto, entry, NOW)
        self.assertEqual(list(entry["prov"]), ["claude"])   # nowhere

    def test_acc_version_bump_invalidates_pre_attribution_caches(self):
        # an old cache (raw claude-bridge keys, acc_ver 2) starts clean
        old = {"acc_ver": 2, "scanned_at": 0, "files": {
            "/h/.pi/x.jsonl": {"kind": "pi", "last_ts": NOW, "prov": {
                "claude-bridge": {"days": {tc._local_date(NOW):
                                           {"tok": 9}},
                                  "mday": {}}}}}}
        self.assertNotEqual(tc.ACC_VERSION, 2)
        cache = old
        if cache.get("acc_ver") != tc.ACC_VERSION:     # collect()'s rule
            cache = {"acc_ver": tc.ACC_VERSION}
        self.assertEqual(cache.get("files"), None)


# ---------- remote-agents wiring: the tick and enable verbs ----------
class TestRemoteAgentsWiring(unittest.TestCase):
    """The verbs call tendril_capacity.background_refresh. The real
    remote-agents module and the real bin/tendril_capacity +
    bin/tendril_schedule are loaded here (sys.modules names), with the
    refresh hooks patched at their own names — so only the wiring is
    under test and no test ever touches the network or the real HOME:
    the store lives in a tempdir, the launcher is a stub, and set_enabled
    is stubbed (its round-trip is covered above and in test_schedule)."""

    @classmethod
    def setUpClass(cls):
        cls.bin_dir = os.path.join(REPO, "bin")
        sys.path.insert(0, cls.bin_dir)
        cls.ra = _load("remote_agents_capacity_wiring", CLI)
        import tendril_capacity
        import tendril_schedule
        cls.capmod = tendril_capacity
        cls.tsmod = tendril_schedule

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(cls.bin_dir)
        sys.modules.pop("tendril_capacity", None)
        sys.modules.pop("tendril_schedule", None)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tendril-capwiring-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "state", "tendril",
                                  "schedule.json")

    # ----- drivers ------------------------------------------------------
    def tick(self, tasks=(), bg_raises=None):
        seq = []

        def launcher_log(task):
            seq.append("launch")
            return True, "w15:p9", None

        def bg():
            seq.append("refresh")
            if bg_raises is not None:
                raise bg_raises

        with mock.patch.object(self.tsmod, "store_paths",
                               return_value=(os.path.dirname(self.store),
                                             self.store,
                                             self.store + ".lock")), \
             mock.patch.object(self.ra, "snapshot",
                               return_value={"panes": [], "agents": [],
                                             "workspaces": []}), \
             mock.patch.object(self.ra, "_schedule_launcher",
                               return_value=launcher_log), \
             mock.patch.object(self.ra, "_schedule_notifier",
                               return_value=None), \
             mock.patch.object(self.capmod, "background_refresh",
                               side_effect=bg) as bg_mock:
            if tasks:
                os.makedirs(os.path.dirname(self.store), exist_ok=True)
                with open(self.store, "w") as f:
                    json.dump({"schema_version": 1,
                               "tasks": list(tasks)}, f)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = self.ra._schedule_cmd(["tick"])
        return rc, out.getvalue(), seq, bg_mock

    def queued_task(self):
        now = time.time()
        return {"id": "t-2026-wiring1", "title": "wiring",
                "prompt": "the secret prompt text",
                "workspace": "w15", "workspace_label": "research",
                "harness": "pi", "trigger": {"kind": "at", "at": now - 10},
                "state": "QUEUED", "created_at": now - 20,
                "due_at": now - 10, "fingerprint": "fp"}

    def enable(self, reported=False, bg_raises=None, verb="enable",
               adapter="zai_quota"):
        calls = []

        def bg():
            calls.append(adapter)
            if bg_raises is not None:
                raise bg_raises
            return True

        with mock.patch.object(self.capmod, "set_enabled",
                               return_value=True) as se, \
             mock.patch.object(self.capmod, "background_refresh",
                               side_effect=bg), \
             mock.patch.object(self.capmod, "adapter_reported",
                               return_value=reported):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = self.ra._capacity_cmd([verb, adapter])
        return rc, out.getvalue(), calls, se

    # ----- tests --------------------------------------------------------
    def test_tick_refreshes_after_the_scheduling_work(self):
        rc, out, seq, bg = self.tick()
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertTrue(doc["ok"])
        self.assertEqual(bg.call_count, 1)     # exactly one refresh per tick
        self.assertEqual(seq, ["refresh"])

    def test_launch_precedes_refresh_and_failure_never_eats_the_tick(self):
        rc, out, seq, _ = self.tick(tasks=[self.queued_task()],
                                    bg_raises=RuntimeError("refresh blew up"))
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertTrue(doc["ok"])                      # tick not failed
        self.assertEqual(doc["tick"]["launched"], 1)    # launch succeeded
        self.assertEqual(seq, ["launch", "refresh"])    # launches FIRST
        with open(self.store) as f:
            row = json.load(f)["tasks"][0]
        self.assertEqual(row["state"], "RUNNING")       # persisted RUNNING
        self.assertEqual(row["pane_id"], "w15:p9")
        self.assertNotIn("the secret prompt text", out)  # reply stays clean

    def test_tick_survives_a_refresh_that_raises_before_any_launch(self):
        rc, out, seq, _ = self.tick(bg_raises=OSError("no route to host"))
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["ok"])
        self.assertEqual(seq, ["refresh"])

    def test_enable_prints_result_first_one_refresh_then_honest_line(self):
        rc, out, calls, se = self.enable(reported=False)
        self.assertEqual(rc, 0)                          # enabling succeeded
        self.assertEqual(calls, ["zai_quota"])           # exactly one refresh
        self.assertEqual(se.call_args, mock.call("zai_quota", True))
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(lines[0], "zai_quota enabled")  # result FIRST
        self.assertEqual(lines[1],
                         "enabled; first sample will arrive within 5 min")

    def test_enable_with_a_landed_sample_prints_nothing_more(self):
        rc, out, calls, _ = self.enable(reported=True)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["zai_quota"])
        self.assertEqual(out.splitlines(), ["zai_quota enabled"])

    def test_enable_succeeds_offline_with_the_honest_line(self):
        rc, out, calls, _ = self.enable(
            bg_raises=OSError("network is unreachable"))
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["zai_quota"])
        self.assertIn("zai_quota enabled", out)
        self.assertIn("first sample will arrive within 5 min", out)

    def test_disable_does_not_refresh(self):
        rc, out, calls, _ = self.enable(verb="disable")
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])                      # no fetch on disable
        self.assertEqual(out.splitlines(), ["zai_quota disabled"])


if __name__ == "__main__":
    unittest.main()
