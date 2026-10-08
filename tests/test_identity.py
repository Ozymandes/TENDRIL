"""Canonical host identity + pairing-code encoder (IDENTITY-PAIRING.md).

The resolver (tendril_link.host_identity) is the single definition every
reader shares: config TENDRIL_HOST -> legacy TAILSCALE_HOST (lowercased)
-> short hostname (lowercased), HOST_RE-gated, sanitized never rewritten.
The encoder (tendril_link.pairing_code) must produce EXACTLY the bytes the
TENDRIL Link app decoder (PairingCode.kt) accepts: `TENDRIL1:` + unpadded
base64url of compact JSON with the sorted keys {"h","s","u","v":1}. The
fixture string below is copied from the Link repo's PairingCodeTest
(codeFor(h="omarchy", s="omarchy.tailnet.ts.net", u="seeno")) to pin byte
identity across the two repositories.
"""
import base64
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "bin"))

import tendril_link as tl  # noqa: E402

# PairingCodeTest.codeFor() with its default arguments, built EXACTLY the
# way the host emits codes (compact JSON, ascii-sorted keys, unpadded
# base64url) — the literal both repos must agree on forever.
LINK_FIXTURE = ("TENDRIL1:eyJoIjoib21hcmNoeSIsInMiOiJvbWFyY2h5LnRhaWxuZXQu"
                "dHMubmV0IiwidSI6InNlZW5vIiwidiI6MX0")


def kotlin_style_code(h, s, u):
    """The Link test's construction, verbatim semantics:
    Base64.getUrlEncoder().withoutPadding() over the f-string JSON."""
    blob = '{"h":"%s","s":"%s","u":"%s","v":1}' % (h, s, u)
    return "TENDRIL1:" + base64.urlsafe_b64encode(
        blob.encode("utf-8")).decode("ascii").rstrip("=")


class HostIdentity(unittest.TestCase):
    def test_tendril_host_wins_over_legacy_and_nodename(self):
        cfg = {"TENDRIL_HOST": "box", "TAILSCALE_HOST": "legacy"}
        self.assertEqual(tl.host_identity(cfg, nodename="other"), "box")

    def test_legacy_tailscale_host_omarchy(self):
        self.assertEqual(tl.host_identity({"TAILSCALE_HOST": "omarchy"},
                                          nodename="whatever"), "omarchy")

    def test_uppercase_legacy_value_is_lowercased(self):
        self.assertEqual(tl.host_identity({"TAILSCALE_HOST": "OMARCHY"},
                                          nodename="whatever"), "omarchy")
        self.assertEqual(tl.host_identity({"TENDRIL_HOST": "OMARCHY"},
                                          nodename="whatever"), "omarchy")

    def test_empty_config_falls_back_to_hostname(self):
        self.assertEqual(tl.host_identity({}, nodename="Omarchy.local"),
                         "omarchy")
        self.assertEqual(tl.host_identity(None, nodename="Boxy"), "boxy")
        # macOS mDNS suffix and domains are dropped, not sanitized in
        nodename = "Janes-MacBook-Pro.local"
        self.assertEqual(tl.host_identity({}, nodename=nodename),
                         "janes-macbook-pro")

    def test_invalid_chars_are_sanitized(self):
        self.assertEqual(tl.host_identity({}, nodename="my host!"), "my-host")
        self.assertEqual(tl.host_identity({"TAILSCALE_HOST": "a b//c"},
                                          nodename="x"), "a-b-c")

    def test_unsanitizable_value_falls_through(self):
        # a value that sanitizes to nothing must not win...
        self.assertEqual(tl.host_identity({"TAILSCALE_HOST": "!!!"},
                                          nodename="omarchy"), "omarchy")
        # ...and a nodename with nothing left still yields a valid label
        self.assertEqual(tl.host_identity({}, nodename="???"), "host")

    def test_resolver_output_always_parses_as_a_link_host(self):
        for cfg, node in (({}, "omarchy"), ({"TAILSCALE_HOST": "OMARCHY"}, "x"),
                          ({}, "My Strange_Node.local")):
            host = tl.host_identity(cfg, nodename=node)
            self.assertTrue(tl.HOST_RE.match(host), host)
            parsed = tl.parse_payload_b64(
                tl.payload_b64(host, "w1"))
            self.assertEqual(parsed["host"], host)


class PairingCodeBytes(unittest.TestCase):
    def test_exact_bytes_match_the_link_repo_fixture(self):
        self.assertEqual(tl.pairing_code("omarchy", "omarchy.tailnet.ts.net",
                                         "seeno"), LINK_FIXTURE)

    def test_matches_the_kotlin_construction_for_other_values(self):
        for h, s, u in (("a", "b.c", "_x"),
                        ("a" + "b" * 62, "a" + "b" * 120 + "." + "c" * 131,
                         "u" + "n" * 30 + "_"),
                        ("omarchy", "omarchy.tail1234.ts.net", "seeno")):
            self.assertEqual(tl.pairing_code(h, s, u), kotlin_style_code(h, s, u))

    def test_shape_is_prefix_unpadded_base64url_compact_sorted_json(self):
        code = tl.pairing_code("omarchy", "omarchy.tail1234.ts.net", "seeno")
        self.assertTrue(code.startswith("TENDRIL1:"))
        self.assertNotIn("=", code)
        self.assertLessEqual(len(code), tl.PAIRING_MAX_LENGTH)
        pad = "=" * (-len(code[len("TENDRIL1:"):]) % 4)
        blob = base64.b64decode(code[9:] + pad, altchars=b"-_", validate=True)
        self.assertEqual(json.loads(blob),
                         {"v": 1, "h": "omarchy",
                          "s": "omarchy.tail1234.ts.net", "u": "seeno"})
        self.assertEqual(
            blob, b'{"h":"omarchy","s":"omarchy.tail1234.ts.net",'
                  b'"u":"seeno","v":1}')  # sorted keys, compact separators

    def test_payload_never_carries_secrets_topics_paths_or_commands(self):
        code = tl.pairing_code("omarchy", "omarchy.tail1234.ts.net", "seeno")
        secretish = ("tendril-lPD1ADyhP1iXjUGs", "tok_secret", "/home/seeno",
                     "ssh-copy-id", "pkg install", "NTFY_TOPIC", "https://")
        for needle in secretish:
            self.assertNotIn(needle, code)
        pad = "=" * (-(len(code) - 9) % 4)
        blob = base64.b64decode(code[9:] + pad, altchars=b"-_").decode()
        doc = json.loads(blob)
        self.assertEqual(set(doc), {"h", "s", "u", "v"})
        for needle in secretish:
            self.assertNotIn(needle, blob)

    def test_malformed_inputs_are_refused_not_rescued(self):
        bad = [
            ("OMARCHY", "omarchy.tail1234.ts.net", "seeno"),   # uppercase host
            ("omarchy", "omarchy tail", "seeno"),              # space in target
            ("omarchy", "omarchy.tail1234.ts.net", "Seeno"),   # uppercase user
            ("omarchy", "omarchy.tail1234.ts.net", "seeno@x"), # '@' in user
            ("", "omarchy.tail1234.ts.net", "seeno"),
            ("omarchy", "", "seeno"),
            ("-lead", "omarchy.tail1234.ts.net", "seeno"),     # leading '-'
            ("omarchy", "-lead.example", "seeno"),
        ]
        for h, s, u in bad:
            with self.assertRaises(tl.LinkError, msg=(h, s, u)):
                tl.pairing_code(h, s, u)

    def test_oversized_code_is_refused(self):
        target = "a" * 240 + "." + "b" * 10 + "." + "c" * 10   # > 253 grammar
        with self.assertRaises(tl.LinkError):
            tl.pairing_code("h", target, "u")
        # grammar-valid but long enough to blow the 512-char code ceiling
        target = "a" + "b" * 120 + "." + "c" * 131
        host = "a" + "b" * 62
        code = kotlin_style_code(host, target, "u" + "n" * 30 + "_")
        self.assertLessEqual(len(tl.pairing_code(host, target,
                                                 "u" + "n" * 30 + "_")), 512)
        self.assertLessEqual(len(code), 512)


class HerdrNotifyWiring(unittest.TestCase):
    """Both herdr-notify host sites go through the shared resolver."""

    def test_both_host_sites_use_the_resolver(self):
        with open(os.path.join(REPO, "bin", "herdr-notify"),
                  encoding="utf-8") as f:
            source = f.read()
        self.assertEqual(source.count('tendril_link.host_identity(CONFIG)'), 2)
        self.assertNotIn('CONFIG.get("TAILSCALE_HOST") or os.uname().nodename',
                         source)


class DecoderRoundTripAgainstLinkGrammar(unittest.TestCase):
    """Mirror of the decisive PairingCodeTest acceptance rows: whatever the
    encoder emits must land in the decoder's Ok branch (hand-rolled here to
    the same grammar; the Kotlin suite is the executable reference)."""

    def decode_like_link(self, raw):
        """Returns (ok, value-or-reason) following PairingCode.parse order."""
        if raw is None:
            return False, "EMPTY"
        code = raw.strip()
        if not code:
            return False, "EMPTY"
        if len(code) > 512:
            return False, "OVERSIZED"
        if not code.startswith("TENDRIL1:"):
            return False, "BAD_PREFIX"
        payload = code[9:]
        if not payload or "=" in payload or len(payload) % 4 == 1:
            return False, "BAD_ENCODING"
        try:
            blob = base64.b64decode(payload + "=" * (-len(payload) % 4),
                                    altchars=b"-_", validate=True)
        except Exception:
            return False, "BAD_ENCODING"
        try:
            text = blob.decode("ascii")
            if any(not (0x20 <= ord(c) <= 0x7E) for c in text):
                return False, "BAD_ENCODING"
            doc = json.loads(text)
        except (UnicodeDecodeError, ValueError):
            return False, "BAD_JSON"
        if not isinstance(doc, dict) or set(doc) != {"h", "s", "u", "v"}:
            return False, "BAD_JSON"
        if doc["v"] != 1 or isinstance(doc["v"], bool):
            return False, "BAD_VALUE"
        h, s, u = doc["h"], doc["s"], doc["u"]
        if not (isinstance(h, str) and isinstance(s, str) and isinstance(u, str)):
            return False, "BAD_JSON"
        if h != h.lower() or not tl.HOST_RE.match(h):
            return False, "BAD_VALUE"
        if not tl.SSH_TARGET_RE.match(s):
            return False, "BAD_VALUE"
        if not tl.PAIR_USER_RE.match(u):
            return False, "BAD_VALUE"
        return True, (h, s, u)

    def test_every_emitted_code_decodes(self):
        for h, s, u in (("omarchy", "omarchy.tail1234.ts.net", "seeno"),
                        ("a", "b.c", "_x"),
                        ("a" + "b" * 62, "a" + "b" * 120 + "." + "c" * 131,
                         "u" + "n" * 30 + "_")):
            ok, value = self.decode_like_link(tl.pairing_code(h, s, u))
            self.assertTrue(ok, value)
            self.assertEqual(value, (h, s, u))
        # whitespace around the code is trimmed, decoder-side
        ok, value = self.decode_like_link(
            "  " + tl.pairing_code("omarchy", "omarchy.tail1234.ts.net",
                                   "seeno") + "\n")
        self.assertTrue(ok)
        self.assertEqual(value[0], "omarchy")


if __name__ == "__main__":
    unittest.main()
