"""tendril-link payload/URI contract tests: round-trips, strictness,
URL-safety, and the no-secrets guarantee."""
import importlib.util
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "bin"))
import tendril_link as tl  # noqa: E402


class PayloadTests(unittest.TestCase):
    def test_round_trip_with_label(self):
        token = tl.payload_b64("home", "w15", "tendril")
        self.assertEqual(tl.parse_payload_b64(token),
                         {"host": "home", "workspace_id": "w15",
                          "label": "tendril"})

    def test_round_trip_without_label_and_unicode_host(self):
        token = tl.payload_b64("my-host.tsd.net", "wK")
        self.assertEqual(tl.parse_payload_b64(token),
                         {"host": "my-host.tsd.net", "workspace_id": "wK",
                          "label": None})

    def test_payload_is_ascii_url_safe_and_padded_neither(self):
        token = tl.payload_b64("home", "w15", "Tendril Café")
        self.assertTrue(token.isascii())
        self.assertNotIn("=", token)
        self.assertNotIn("+", token)
        self.assertNotIn("/", token)

    def test_uri_round_trip_including_quoting(self):
        u = tl.uri("home", "w15", "spaced label")
        self.assertEqual(u, "tendril://host/home/workspace/w15"
                            "?label=spaced%20label")
        self.assertEqual(tl.parse_uri(u),
                         {"host": "home", "workspace_id": "w15",
                          "label": "spaced label"})

    def test_uri_unicode_label_survives(self):
        u = tl.uri("home", "w15", "Tendril Café")
        self.assertEqual(tl.parse_uri(u)["label"], "Tendril Café")

    def test_describe_forms_agree(self):
        link = tl.describe("home", "w15", "lbl")
        self.assertEqual(tl.parse_payload_b64(link["payload_b64"]),
                         tl.parse_uri(link["uri"]))

    def test_pane_id_shape_is_allowed(self):
        token = tl.payload_b64("home", "w15:p1")
        self.assertEqual(tl.parse_payload_b64(token)["workspace_id"], "w15:p1")


class PayloadStrictnessTests(unittest.TestCase):
    def test_shell_metacharacters_rejected_everywhere(self):
        for bad in ("home;rm", "home&x", "home|x", "ho me", "home$x",
                    "home`x`", "home(x)", "home'x", 'home"x', "home/x",
                    "", "home\nx"):
            with self.assertRaises(tl.LinkError, msg=repr(bad)):
                tl.payload_b64(bad, "w15")
        for bad in ("w15;rm", "w15&", "w15|x", "w15 x", "w15$x", "w15/x",
                    "", "w15\nrm -rf /"):
            with self.assertRaises(tl.LinkError, msg=repr(bad)):
                tl.payload_b64("home", bad)

    def test_oversized_payload_rejected(self):
        with self.assertRaises(tl.LinkError):
            tl.parse_payload_b64("A" * 600)

    def test_unknown_fields_rejected(self):
        doc = {"v": 1, "h": "home", "w": "w15", "cmd": "rm -rf /"}
        blob = json.dumps(doc, separators=(",", ":"), sort_keys=True)
        import base64
        token = base64.urlsafe_b64encode(blob.encode()).decode().rstrip("=")
        with self.assertRaises(tl.LinkError):
            tl.parse_payload_b64(token)

    def test_wrong_version_rejected(self):
        doc = {"v": 2, "h": "home", "w": "w15"}
        import base64
        blob = json.dumps(doc, separators=(",", ":"), sort_keys=True)
        token = base64.urlsafe_b64encode(blob.encode()).decode().rstrip("=")
        with self.assertRaises(tl.LinkError):
            tl.parse_payload_b64(token)

    def test_garbage_rejected(self):
        for junk in ("", "!!!", "////", "eyJub3RfanNvbg", "eyJ2IjoxfQ"):
            with self.assertRaises(tl.LinkError, msg=junk):
                tl.parse_payload_b64(junk)

    def test_uri_strictness(self):
        for bad in ("https://host/home/workspace/w15",
                    "tendril://other/home/workspace/w15",
                    "tendril://host/home/tab/w15",
                    "tendril://host/home/workspace/w15?cmd=x",
                    "tendril://host/home/workspace/w15?label=a&label=b",
                    "tendril://host//workspace/w15",
                    "not a uri"):
            with self.assertRaises(tl.LinkError, msg=bad):
                tl.parse_uri(bad)

    def test_label_is_sanitized_not_rejected(self):
        self.assertEqual(tl.clean_label("  spaced\r\nout\x07 "), "spaced out")
        self.assertEqual(tl.clean_label("x" * 500), "x" * tl.MAX_LABEL)
        self.assertIsNone(tl.clean_label("   "))
        self.assertIsNone(tl.clean_label(None))

    def test_decode_cli(self):
        token = tl.payload_b64("home", "w15")
        rc = tl.main(["tendril-link", "decode", token])
        self.assertEqual(rc, 0)
        self.assertEqual(tl.main(["tendril-link", "decode", "!!!"]), 2)
        self.assertEqual(tl.main(["tendril-link", "nope"]), 2)


class PaneContract(unittest.TestCase):
    """Contract v1 additive pane query: optional, strictly validated, and
    invisible to every workspace-only link that predates it."""

    @staticmethod
    def _encode(doc):
        import base64
        blob = json.dumps(doc, separators=(",", ":"), sort_keys=True)
        return base64.urlsafe_b64encode(blob.encode()).decode().rstrip("=")

    # ---- payload ---------------------------------------------------------

    def test_payload_round_trip_with_pane_and_label(self):
        token = tl.payload_b64("home", "w15", "tendril", "w15:p3")
        self.assertEqual(tl.parse_payload_b64(token),
                         {"host": "home", "workspace_id": "w15",
                          "label": "tendril", "pane": "w15:p3"})

    def test_payload_round_trip_with_pane_without_label(self):
        token = tl.payload_b64("home", "w15", None, "w15:p1")
        self.assertEqual(tl.parse_payload_b64(token),
                         {"host": "home", "workspace_id": "w15",
                          "label": None, "pane": "w15:p1"})

    def test_workspace_only_payload_is_byte_identical_to_the_old_contract(self):
        token = tl.payload_b64("home", "w15", "tendril")
        self.assertEqual(tl.parse_payload_b64(token),
                         {"host": "home", "workspace_id": "w15",
                          "label": "tendril"})
        pad = "=" * (-len(token) % 4)
        import base64
        doc = json.loads(base64.b64decode(token + pad,
                                          altchars=b"-_").decode())
        self.assertEqual(set(doc), {"v", "h", "w", "l"})   # no p field

    def test_payload_pane_field_carries_only_p(self):
        token = tl.payload_b64("home", "w15", None, "w15:p3")
        pad = "=" * (-len(token) % 4)
        import base64
        doc = json.loads(base64.b64decode(token + pad,
                                          altchars=b"-_").decode())
        self.assertEqual(set(doc), {"v", "h", "w", "p"})

    def test_payload_rejects_hostile_and_stale_panes(self):
        for doc in ({"v": 1, "h": "home", "w": "w15", "p": "w17:p3"},   # cross-workspace
                    {"v": 1, "h": "home", "w": "w15", "p": "p3"},       # no prefix
                    {"v": 1, "h": "home", "w": "w15", "p": "w15"},      # bare workspace
                    {"v": 1, "h": "home", "w": "w15", "p": "w15:p3:x"}, # second ':'
                    {"v": 1, "h": "home", "w": "w15", "p": "w15:p%33"}, # percent-encoded
                    {"v": 1, "h": "home", "w": "w15", "p": ""},         # empty
                    {"v": 1, "h": "home", "w": "w15", "p": None}):      # null
            with self.assertRaises(tl.LinkError, msg=repr(doc)):
                tl.parse_payload_b64(self._encode(doc))

    # ---- uri -------------------------------------------------------------

    def test_uri_pane_precedes_label_and_round_trips(self):
        u = tl.uri("home", "w15", "my proj", "w15:p3")
        self.assertEqual(u, "tendril://host/home/workspace/w15"
                            "?pane=w15:p3&label=my%20proj")
        self.assertEqual(tl.parse_uri(u),
                         {"host": "home", "workspace_id": "w15",
                          "label": "my proj", "pane": "w15:p3"})

    def test_uri_pane_only(self):
        u = tl.uri("home", "w15", None, "w15:p1")
        self.assertEqual(u, "tendril://host/home/workspace/w15"
                            "?pane=w15:p1")
        self.assertEqual(tl.parse_uri(u),
                         {"host": "home", "workspace_id": "w15",
                          "label": None, "pane": "w15:p1"})

    def test_uri_pane_and_label_accepted_in_any_order(self):
        a = tl.parse_uri("tendril://host/home/workspace/w15"
                         "?pane=w15%3Ap1&label=x")
        b = tl.parse_uri("tendril://host/home/workspace/w15"
                         "?label=x&pane=w15%3Ap1")
        self.assertEqual(a, b)
        self.assertEqual(a["pane"], "w15:p1")

    def test_uri_rejects_duplicate_unknown_empty_and_foreign_pane(self):
        for bad in ("tendril://host/home/workspace/w15"
                    "?pane=w15%3Ap1&pane=w15%3Ap2",
                    "tendril://host/home/workspace/w15"
                    "?pane=w15%3Ap1&label=a&label=b",
                    "tendril://host/home/workspace/w15?pane=w15%3Ap1&cmd=x",
                    "tendril://host/home/workspace/w15?pane=",
                    "tendril://host/home/workspace/w15?pane=w16%3Ap1",
                    "tendril://host/home/workspace/w15?pane=p1"):
            with self.assertRaises(tl.LinkError, msg=bad):
                tl.parse_uri(bad)

    def test_uri_with_pane_is_never_percent_encoding_the_pane(self):
        # a literal '%' in a pane is rejected; the ':' travels as %3A and
        # decodes back to the plain id
        with self.assertRaises(tl.LinkError):
            tl.uri("home", "w15", None, "w15:p3%20x")
        self.assertEqual(tl.parse_uri("tendril://host/home/workspace/w15"
                                      "?pane=w15%3Ap3")["pane"], "w15:p3")

    # ---- validation matrix -------------------------------------------------

    def test_pane_strictness(self):
        for bad in ("w17:p3",      # cross-workspace
                    "p3",          # no workspace prefix
                    "w15",         # bare workspace: no ':'
                    "w15:p3:x",    # second ':'
                    "w15:p%33",    # percent-encoded -> literal % rejected
                    "w15:",        # empty suffix
                    "w15::",       # empty suffix, extra colon
                    ":p1",         # empty prefix
                    "w15:p3 ",     # space
                    ""):
            with self.assertRaises(tl.LinkError, msg=repr(bad)):
                tl.payload_b64("home", "w15", None, bad)

    # ---- describe / target -------------------------------------------------

    def test_describe_exposes_pane_and_target(self):
        link = tl.describe("home", "w15", "lbl", "w15:p3")
        self.assertEqual(link["pane"], "w15:p3")
        self.assertEqual(link["target"], "w15:p3")
        self.assertEqual(tl.parse_uri(link["uri"])["pane"], "w15:p3")
        self.assertEqual(tl.parse_payload_b64(link["payload_b64"])["pane"],
                         "w15:p3")

    def test_describe_without_pane_keeps_the_old_shape_and_uri(self):
        link = tl.describe("home", "w15", "lbl")
        self.assertIsNone(link["pane"])
        self.assertEqual(link["target"], "w15")
        self.assertNotIn("pane=", link["uri"])
        self.assertEqual(link["uri"], tl.uri("home", "w15", "lbl"))

    def test_target_helper_prefers_the_pane(self):
        self.assertEqual(tl.target("w15", "w15:p3"), "w15:p3")
        self.assertEqual(tl.target("w15"), "w15")
        self.assertEqual(tl.target("w15", None), "w15")

    def test_describe_forms_agree_with_pane(self):
        link = tl.describe("home", "w15", "lbl", "w15:p3")
        self.assertEqual(tl.parse_payload_b64(link["payload_b64"]),
                         tl.parse_uri(link["uri"]))

    def test_decode_cli_reports_pane(self):
        token = tl.payload_b64("home", "w15", None, "w15:p3")
        self.assertEqual(tl.main(["tendril-link", "decode", token]), 0)
        self.assertEqual(
            tl.main(["tendril-link", "decode",
                     tl.uri("home", "w15", None, "w15:p3")]), 0)


class NoSecretsTests(unittest.TestCase):
    def test_payload_carries_only_the_whitelisted_fields(self):
        import base64
        token = tl.payload_b64("home", "w15", "lbl", "w15:p3")
        pad = "=" * (-len(token) % 4)
        doc = json.loads(base64.b64decode(token + pad,
                                          altchars=b"-_").decode())
        self.assertEqual(set(doc), {"v", "h", "w", "l", "p"})

    def test_control_chars_never_survive_into_payload(self):
        token = tl.payload_b64("home", "w15", "a\r\nb\x1b[31mred")
        parsed = tl.parse_payload_b64(token)
        self.assertNotIn("\r", parsed["label"])
        self.assertNotIn("\n", parsed["label"])
        self.assertNotIn("\x1b", parsed["label"])


if __name__ == "__main__":
    unittest.main()
