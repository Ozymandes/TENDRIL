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


class NoSecretsTests(unittest.TestCase):
    def test_payload_carries_only_the_three_fields(self):
        import base64
        token = tl.payload_b64("home", "w15", "lbl")
        pad = "=" * (-len(token) % 4)
        doc = json.loads(base64.b64decode(token + pad,
                                          altchars=b"-_").decode())
        self.assertEqual(set(doc), {"v", "h", "w", "l"})

    def test_control_chars_never_survive_into_payload(self):
        token = tl.payload_b64("home", "w15", "a\r\nb\x1b[31mred")
        parsed = tl.parse_payload_b64(token)
        self.assertNotIn("\r", parsed["label"])
        self.assertNotIn("\n", parsed["label"])
        self.assertNotIn("\x1b", parsed["label"])


if __name__ == "__main__":
    unittest.main()
