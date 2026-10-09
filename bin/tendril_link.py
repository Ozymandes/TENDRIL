#!/usr/bin/env python3
"""tendril-link — canonical deep-link payloads for TENDRIL sessions.

A TENDRIL session is an addressable object. The canonical address is the
Herdr `workspace_id` (e.g. ``w15``) on a named host; an optional exact
`pane_id` (``w15:p3``, the emitting agent's pane) narrows the address to
one pane inside that workspace. This module is the single definition of
how that address is serialized for transport between machines (ntfy click
URLs, Termux url-opener shares, iOS Shortcuts input, shell arguments).

Three equivalent wire forms:

  payload (URL-safe, fits any URL query/header value):
      <base64url of compact JSON>          e.g.eyJ2IjoxLCJoIjoiaG9tZSIsInciOiJ3MTUifQ

  URI (human-inspectable):
      tendril://host/<host-id>/workspace/<workspace-id>[?pane=<pane-id>][&label=<label>]

  parsed dict: {"host": str, "workspace_id": str, "label": str|None
                [, "pane": str]}

The payload carries ONLY the host id, the workspace id, an optional human
label and an optional exact pane id. Never keys, tokens, topics, shell
text or free-form commands. Hosts and ids are validated against strict
regexes; labels are control-char stripped, whitespace-collapsed and
length-capped. Decoding rejects unknown fields, wrong versions, oversized
input and non-canonical encodings, so a payload is safe to log, safe to
embed in a URL and safe to treat as data (not code) on the far side.

Import path: lives next to remote-agents / herdr-notify in ~/.local/bin.

The pane field is additive (LINK_VERSION stays 1): workspace-only links
are byte-identical to the original contract, and a link without a pane
parses to exactly the original three-key dict.

Also the canonical host identity (documented in
docs/ANDROID.md, "Pair your phone"): one value, TENDRIL_HOST in the host
config, is what every deep link, notification and pairing code calls this
machine. Readers resolve it here so remote-agents, herdr-notify and the
pairing flow cannot drift:

    host_identity(config): config TENDRIL_HOST -> legacy TAILSCALE_HOST
    (lowercased) -> short hostname (lowercased); anything that cannot match
    HOST_RE is sanitized (lowercase, [a-z0-9._-], runs of junk -> '-'),
    never rewritten on disk.

And the phone pairing code encoders, the exact inverse of the TENDRIL Link
app decoder (app/.../core/PairingCode.kt). Two contract versions:

  v1  TENDRIL1:<base64url, no padding, of compact JSON with the sorted
      keys {"h","s","u","v":1}> — kept for compatibility; still decodes.

  v2  TENDRIL2:<base64url, no padding, of compact JSON with the sorted
      keys {"v":2,"h","s","u"} plus the optional ntfy keys n (server
      base URL), t (topic) and k (read-only subscribe token)>.  n and t
      appear together or not at all, k requires n+t, the whole code is
      <= 1024 chars, and every value is grammar-validated (reject, never
      sanitize). Host `--pair` emits v2 — with n/t/k when notify.env is
      usable, without them (and the UI shows notify "not configured")
      when it is not. The token k travels ONLY inside the encoded
      payload; nothing prints it separately.

`decode_pairing` strictly decodes either version (the same reject-not-
sanitize grammar as the Link decoder); the CLI prints the result with k
redacted. The developer test in the Link repo pins byte identity both
ways.

CLI (for debugging on phone or host):
    tendril_link.py encode <host> <workspace-id> [label] [pane]
    tendril_link.py decode <payload-or-uri>
    tendril_link.py decode-pair <TENDRIL1:/TENDRIL2: pairing code>
"""
import base64
import binascii
import json
import os
import re
import sys
import unicodedata
from urllib.parse import quote, unquote, urlsplit

SCHEME = "tendril"
LINK_VERSION = 1
MAX_LABEL = 120
MAX_PAYLOAD_CHARS = 512

# Hosts: hostname-shaped (Tailscale MagicDNS names, nodenames, ssh aliases).
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
# Herdr workspace ids ("w15", "wK") and pane ids ("w15:p1").
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# ---- canonical host identity (IDENTITY-PAIRING.md) -----------------------
# The ssh target and the ssh user of a pairing code: separate grammar,
# because "what to call it" != "how to reach it".
SSH_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
PAIR_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
PAIRING_PREFIX = "TENDRIL1:"
PAIRING_MAX_LENGTH = 512
PAIRING_PREFIX_V2 = "TENDRIL2:"
PAIRING_V2_MAX_LENGTH = 1024

# Contract "pairing code v2" grammars for the optional ntfy keys. n: https
# always; http only for .ts.net / 100.64.0.0/10 / localhost hosts (checked
# in code, not here). No path, no query, no userinfo, no trailing slash —
# the anchored regex below rejects all of those by construction.
NTFY_URL_RE = re.compile(r"^(https?)://([A-Za-z0-9.-]{1,253})(?::([0-9]{1,5}))?$")
NTFY_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
NTFY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
NTFY_REDACTED = "<redacted>"


class LinkError(ValueError):
    """Malformed host, id, label, pane, payload or URI. Never a security event."""


def clean_label(label):
    """Label-safe text: NFC, control chars dropped, whitespace collapsed,
    capped at MAX_LABEL. Returns None when nothing remains."""
    if label is None:
        return None
    text = unicodedata.normalize("NFC", str(label))
    text = re.sub(r"[\x00-\x1f\x7f\s]+", " ", text).strip()
    return text[:MAX_LABEL].strip() or None


def sanitize_host(name):
    """Force a hostname-shaped id: lowercase, keep [a-z0-9._-], collapse
    every run of anything else into one '-', trim leading/trailing '-'.
    Deterministic cleanup for odd nodenames ("My Host!" -> "my-host");
    HOST_RE stays the gate — this only makes more values passable."""
    text = re.sub(r"[^a-z0-9._-]+", "-", str(name or "").strip().lower())
    return text.strip("-.")


def host_identity(config=None, nodename=None):
    """The canonical TENDRIL host id (binding contract, readers side):

    config TENDRIL_HOST -> legacy TAILSCALE_HOST (lowercased) -> short
    hostname (lowercased).

    A candidate that cannot match HOST_RE is sanitized and used only if
    the sanitized form passes; otherwise the next source is tried, ending
    at the sanitized nodename. Uppercase legacy values (OMARCHY) resolve
    to omarchy. Never mutates config; never touches the network.
    """
    cfg = config if isinstance(config, dict) else {}
    if nodename is None:
        nodename = os.uname().nodename
    short = str(nodename or "").split(".", 1)[0].strip()
    for key in ("TENDRIL_HOST", "TAILSCALE_HOST"):
        value = str(cfg.get(key) or "").strip().lower()
        if not value:
            continue
        if HOST_RE.match(value):
            return value
        value = sanitize_host(value)
        if value and HOST_RE.match(value):
            return value
    value = short.lower()
    if HOST_RE.match(value):
        return value
    value = sanitize_host(short)
    return value if value and HOST_RE.match(value) else "host"


# ---- phone pairing code (TENDRIL Link, PairingCode.kt inverse) -----------

def pairing_code(host, ssh_target, user):
    """The exact bytes the TENDRIL Link decoder accepts:

    TENDRIL1:<base64url, no padding, of compact JSON with the sorted keys
    {"h","s","u","v":1}> — h the canonical host id (already lowercase;
    the decoder rejects uppercase), s the ssh target (DNS grammar), u the
    ssh user. Payload carries nothing else: no keys, tokens, topics,
    paths or commands. Raises LinkError on any invalid input; callers
    print the error and no code.
    """
    host = str(host or "")
    ssh_target = str(ssh_target or "")
    user = str(user or "")
    if not host or host != host.lower() or not HOST_RE.match(host):
        raise LinkError(f"pairing host invalid (lowercase hostname expected): {host!r}")
    if not SSH_TARGET_RE.match(ssh_target):
        raise LinkError(f"pairing ssh target invalid (hostname/alias expected): {ssh_target!r}")
    if not PAIR_USER_RE.match(user):
        raise LinkError(f"pairing user invalid (lowercase ssh username expected): {user!r}")
    blob = json.dumps({"v": 1, "h": host, "s": ssh_target, "u": user},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    code = PAIRING_PREFIX + base64.urlsafe_b64encode(
        blob.encode("utf-8")).decode("ascii").rstrip("=")
    if len(code) > PAIRING_MAX_LENGTH:
        raise LinkError(f"pairing code oversized ({len(code)} > {PAIRING_MAX_LENGTH} chars)")
    return code


def _pair_identity(host, ssh_target, user):
    """Identity validation shared by v1/v2: same grammar, same messages,
    reject-not-sanitize. Returns the normalized triple."""
    host = str(host or "")
    ssh_target = str(ssh_target or "")
    user = str(user or "")
    if not host or host != host.lower() or not HOST_RE.match(host):
        raise LinkError(f"pairing host invalid (lowercase hostname expected): {host!r}")
    if not SSH_TARGET_RE.match(ssh_target):
        raise LinkError(f"pairing ssh target invalid (hostname/alias expected): {ssh_target!r}")
    if not PAIR_USER_RE.match(user):
        raise LinkError(f"pairing user invalid (lowercase ssh username expected): {user!r}")
    return host, ssh_target, user


def _valid_ntfy_url(url):
    """The n key's grammar: https always; http only when the host ends in
    .ts.net or is an IPv4 in 100.64.0.0/10 or 127.0.0.1/localhost. The
    NTFY_URL_RE match already rules out path, query, userinfo and any
    trailing slash."""
    m = NTFY_URL_RE.match(url) if isinstance(url, str) else None
    if not m:
        return False
    scheme, host = m.group(1), m.group(2)
    if scheme == "https":
        return True
    host = host.lower()
    if host.endswith(".ts.net") or host in ("localhost", "127.0.0.1"):
        return True
    parts = host.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(p) for p in parts]
    except ValueError:                      # junk like 100.64.0.x1
        return False
    return (octets[0] == 100 and 64 <= octets[1] <= 127
            and all(0 <= o <= 255 for o in octets))


def pairing_code_v2(host, ssh_target, user, ntfy_url=None, topic=None,
                    token=None):
    """The contract "pairing code v2", the exact bytes the TENDRIL Link
    decoder accepts:

    TENDRIL2:<base64url, no padding, of compact JSON with the sorted keys
    {"v":2,"h","s","u"} plus n/t/k when the ntfy config is provided>.
    Same h/s/u grammar and error style as v1. n and t appear together or
    not at all; k requires n+t; every value is validated, never
    sanitized; the whole code is <= 1024 chars. Raises LinkError on any
    invalid input; callers print the error and no code. The token rides
    only inside the encoded payload — callers never print it separately.
    """
    host, ssh_target, user = _pair_identity(host, ssh_target, user)
    url = ntfy_url or None
    topic = topic or None
    token = token or None
    if url is not None and not _valid_ntfy_url(url):
        raise LinkError(f"pairing ntfy url invalid (https://host[:port] only): {url!r}")
    if (url is None) != (topic is None):
        raise LinkError("pairing ntfy url and topic appear together or not at all")
    doc = {"v": 2, "h": host, "s": ssh_target, "u": user}
    if url is not None:
        if not isinstance(topic, str) or not NTFY_TOPIC_RE.match(topic):
            raise LinkError(f"pairing ntfy topic invalid: {topic!r}")
        doc["n"] = url
        doc["t"] = topic
        if token is not None:
            if not NTFY_TOKEN_RE.match(token):
                raise LinkError("pairing ntfy token invalid")
            doc["k"] = token
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    code = PAIRING_PREFIX_V2 + base64.urlsafe_b64encode(
        blob.encode("utf-8")).decode("ascii").rstrip("=")
    if len(code) > PAIRING_V2_MAX_LENGTH:
        raise LinkError(
            f"pairing code oversized ({len(code)} > {PAIRING_V2_MAX_LENGTH} chars)")
    return code


# ---- pairing decode (strict, v1 and v2; the Link decoder's twin) ---------

_PAIRING_KEYS = ("v", "h", "s", "u", "n", "t", "k")
_PAIRING_B64_RE = re.compile(r"[A-Za-z0-9_-]+")


def _scan_pairing_string(text, start):
    """Strict flat-JSON string: `"..."` with no escapes and printable
    ASCII only (0x20-0x7E). Returns (value, index past the closing
    quote), or (None, start) — the Link decoder's shape."""
    if start >= len(text) or text[start] != '"':
        return None, start
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == '"':
            return text[start + 1:i], i + 1
        if ch == "\\" or not 0x20 <= ord(ch) <= 0x7E:
            return None, start
        i += 1
    return None, start                          # unterminated


def _scan_pairing_number(text, start):
    """Strict JSON number token (no leading zeros, canonical fraction and
    exponent). Returns (token, index past it), or (None, start)."""
    i = start
    if i < len(text) and text[i] == "-":
        i += 1
    if i >= len(text) or not "0" <= text[i] <= "9":
        return None, start
    i += 1 if text[i] == "0" else 0
    if text[i - 1] != "0":
        while i < len(text) and "0" <= text[i] <= "9":
            i += 1
    if i < len(text) and text[i] == ".":
        i += 1
        if i >= len(text) or not "0" <= text[i] <= "9":
            return None, start
        while i < len(text) and "0" <= text[i] <= "9":
            i += 1
    if i < len(text) and text[i] in "eE":
        i += 1
        if i < len(text) and text[i] in "+-":
            i += 1
        if i >= len(text) or not "0" <= text[i] <= "9":
            return None, start
        while i < len(text) and "0" <= text[i] <= "9":
            i += 1
    return text[start:i], i


def _scan_pairing_json(text):
    """Strict flat pairing JSON, mirroring the Link decoder's hand-rolled
    parser: compact separators only (no whitespace), no escapes, no
    nesting, each key at most once, any order, input fully consumed.
    Returns the payload dict with "v" kept as the raw number token
    (validated by the caller). Raises LinkError on any deviation."""
    bad = LinkError("pairing payload is not valid pairing JSON")
    if not text or text[0] != "{":
        raise bad
    i = 1
    doc = {}
    if i < len(text) and text[i] == "}":
        i += 1
    else:
        while True:
            key, i = _scan_pairing_string(text, i)
            if key is None:
                raise bad
            if key not in _PAIRING_KEYS or key in doc:
                raise LinkError("pairing payload has unknown or duplicate keys")
            if i >= len(text) or text[i] != ":":
                raise bad
            i += 1
            if key == "v":
                token, i = _scan_pairing_number(text, i)
                if token is None:
                    raise bad
                doc["v"] = token
            else:
                value, i = _scan_pairing_string(text, i)
                if value is None:
                    raise bad
                doc[key] = value
            if i >= len(text):
                raise bad
            if text[i] == ",":
                i += 1
            elif text[i] == "}":
                i += 1
                break
            else:
                raise bad
    if i != len(text):
        raise bad
    return doc


def decode_pairing(code):
    """Strict decode of a TENDRIL1:/TENDRIL2: pairing code — the inverse
    of pairing_code()/pairing_code_v2() and the Link decoder's twin:
    canonical unpadded base64url, printable-ASCII bytes, strict flat JSON
    and every value in grammar (reject, never sanitize). Returns the flat
    payload dict: {"v", "h", "s", "u"} plus "n", "t", "k" on v2 — k
    UNREDACTED; anything that displays it must redact (NTFY_REDACTED).
    Raises LinkError on anything out of grammar. URIs and bare payloads
    are not pairing codes."""
    text = str(code or "").strip()
    if text.startswith(PAIRING_PREFIX_V2):
        prefix, version, max_len = PAIRING_PREFIX_V2, 2, PAIRING_V2_MAX_LENGTH
    elif text.startswith(PAIRING_PREFIX):
        prefix, version, max_len = PAIRING_PREFIX, 1, PAIRING_MAX_LENGTH
    else:
        raise LinkError("not a pairing code (expected TENDRIL1: or TENDRIL2:)")
    if len(text) > max_len:
        raise LinkError(f"pairing code oversized ({len(text)} > {max_len} chars)")
    payload = text[len(prefix):]
    if (not payload or "=" in payload or len(payload) % 4 == 1
            or not _PAIRING_B64_RE.fullmatch(payload)):
        raise LinkError("pairing payload is not canonical base64url")
    raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    try:
        text_json = raw.decode("ascii")
    except UnicodeDecodeError:
        raise LinkError("pairing payload is not printable ASCII") from None
    if any(not 0x20 <= ord(ch) <= 0x7E for ch in text_json):
        raise LinkError("pairing payload is not printable ASCII")
    doc = _scan_pairing_json(text_json)
    if doc.get("v") != str(version):
        raise LinkError(
            f"pairing version mismatch: prefix {prefix!r} but v is {doc.get('v')!r}")
    host, target_, user = doc.get("h"), doc.get("s"), doc.get("u")
    if not isinstance(host, str) or host != host.lower() or not HOST_RE.match(host):
        raise LinkError("pairing payload host invalid")
    if not isinstance(target_, str) or not SSH_TARGET_RE.match(target_):
        raise LinkError("pairing payload ssh target invalid")
    if not isinstance(user, str) or not PAIR_USER_RE.match(user):
        raise LinkError("pairing payload user invalid")
    out = {"v": version, "h": host, "s": target_, "u": user}
    if version == 2:
        url, topic, token = doc.get("n"), doc.get("t"), doc.get("k")
        if (url is None) != (topic is None):
            raise LinkError("pairing payload n/t must appear together")
        if url is not None:
            if not _valid_ntfy_url(url):
                raise LinkError("pairing payload ntfy url invalid")
            if not NTFY_TOPIC_RE.match(topic):
                raise LinkError("pairing payload ntfy topic invalid")
            out["n"], out["t"] = url, topic
        if token is not None:
            if url is None:
                raise LinkError("pairing payload k requires n+t")
            if not NTFY_TOKEN_RE.match(token):
                raise LinkError("pairing payload ntfy token invalid")
            out["k"] = token
    return out


def _valid_pane(workspace_id, pane):
    """A pane id is '<workspace-id>:<suffix>': the id charset, the parent
    workspace prefix, exactly one ':' in total, and never a '%' (pane ids
    are never percent-encoded; a literal % means someone pre-encoded it)."""
    if not pane or "%" in pane or pane.count(":") != 1:
        return False
    if not ID_RE.match(pane):
        return False
    head, suffix = pane.split(":", 1)
    return head == workspace_id and bool(suffix)


def _validated(host, workspace_id, label=None, pane=None):
    host = str(host)
    workspace_id = str(workspace_id)
    if not HOST_RE.match(host):
        raise LinkError(f"invalid host: {host!r}")
    if not ID_RE.match(workspace_id):
        raise LinkError(f"invalid workspace id: {workspace_id!r}")
    if pane is not None:
        pane = str(pane)
        if not _valid_pane(workspace_id, pane):
            raise LinkError(f"invalid pane id for {workspace_id}: {pane!r}")
    return host, workspace_id, clean_label(label), pane


def target(workspace_id, pane=None):
    """Effective deep-link target: the exact pane id when present, else the
    workspace id — the carrier path segment (https://…/.tendril/<target>)."""
    return pane if pane else workspace_id


def payload_b64(host, workspace_id, label=None, pane=None):
    """Compact URL-safe payload (no '=' padding, ASCII only)."""
    host, workspace_id, label, pane = _validated(host, workspace_id,
                                                 label, pane)
    doc = {"v": LINK_VERSION, "h": host, "w": workspace_id}
    if label:
        doc["l"] = label
    if pane:
        doc["p"] = pane
    blob = json.dumps(doc, separators=(",", ":"), sort_keys=True,
                      ensure_ascii=True)
    return base64.urlsafe_b64encode(blob.encode("utf-8")).decode("ascii").rstrip("=")


def parse_payload_b64(token):
    """Strict decode. Returns {"host", "workspace_id", "label"} — plus
    "pane" only when the payload carries one, so old links decode to
    exactly the original dict. Raises LinkError on anything that is not
    exactly what payload_b64 produces."""
    if not isinstance(token, str) or not token:
        raise LinkError("payload missing")
    if len(token) > MAX_PAYLOAD_CHARS:
        raise LinkError("payload oversized")
    pad = "=" * (-len(token) % 4)
    try:
        raw = base64.b64decode(token + pad, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise LinkError("payload is not base64url") from None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LinkError("payload is not valid JSON") from None
    if not isinstance(doc, dict) or set(doc) - {"v", "h", "w", "l", "p"}:
        raise LinkError("payload has unknown fields")
    if doc.get("v") != LINK_VERSION:
        raise LinkError(f"unsupported payload version: {doc.get('v')!r}")
    host, ws = doc.get("h"), doc.get("w")
    if not isinstance(host, str) or not HOST_RE.match(host):
        raise LinkError("payload host invalid")
    if not isinstance(ws, str) or not ID_RE.match(ws):
        raise LinkError("payload workspace id invalid")
    label = doc.get("l")
    if label is not None and not isinstance(label, str):
        raise LinkError("payload label invalid")
    pane = None
    if "p" in doc:                      # present but null/garbage -> reject
        pane = doc["p"]
        if not isinstance(pane, str) or not _valid_pane(ws, pane):
            raise LinkError("payload pane id invalid")
    parsed = {"host": host, "workspace_id": ws, "label": clean_label(label)}
    if pane is not None:
        parsed["pane"] = pane
    return parsed


def uri(host, workspace_id, label=None, pane=None):
    """Human-inspectable canonical URI:
    tendril://host/<host-id>/workspace/<workspace-id>[?pane=<pane-id>][&label=<label>]

    The pane query comes first so the exact target reads before the
    cosmetic label."""
    host, workspace_id, label, pane = _validated(host, workspace_id,
                                                 label, pane)
    out = f"{SCHEME}://host/{quote(host, safe='')}/workspace/{quote(workspace_id, safe=':')}"
    query = []
    if pane:
        query.append("pane=" + quote(pane, safe=":"))
    if label:
        query.append("label=" + quote(label, safe=""))
    if query:
        out += "?" + "&".join(query)
    return out


def parse_uri(value):
    """Inverse of uri(). Strict: scheme must be tendril, netloc 'host',
    path /<host-id>/workspace/<id>, optional ?pane=/?label= in any order,
    at most one of each, nothing else. The result gains "pane" only when
    the URI carries one, so old links parse to the original dict."""
    if not isinstance(value, str):
        raise LinkError("uri missing")
    parts = urlsplit(value.strip())
    if parts.scheme != SCHEME:
        raise LinkError(f"not a {SCHEME}:// uri")
    if parts.netloc != "host":
        raise LinkError("uri netloc must be 'host'")
    segments = parts.path.split("/")
    if len(segments) != 4 or segments[0] != "" or segments[2] != "workspace":
        raise LinkError("uri path must be /<host>/workspace/<id>")
    label = None
    pane = None
    for key, val in (kv.split("=", 1) for kv in parts.query.split("&") if kv):
        if key == "label":
            if label is not None:
                raise LinkError("duplicate label parameter")
            label = unquote(val)
        elif key == "pane":
            if pane is not None:
                raise LinkError("duplicate pane parameter")
            pane = unquote(val)
        else:
            raise LinkError(f"unknown uri parameter: {key!r}")
    parsed = {"host": unquote(segments[1]),
              "workspace_id": unquote(segments[3]),
              "label": clean_label(label)}
    _validated(parsed["host"], parsed["workspace_id"], None, pane)
    if pane is not None:
        parsed["pane"] = pane
    return parsed


def describe(host, workspace_id, label=None, pane=None):
    """All wire forms at once (what notifications embed). `pane` carries
    the emitting agent's exact pane when known; `target` is the effective
    carrier path token (pane id, else workspace id)."""
    host, workspace_id, label, pane = _validated(host, workspace_id,
                                                 label, pane)
    return {
        "host": host,
        "workspace_id": workspace_id,
        "label": label,
        "pane": pane,
        "target": target(workspace_id, pane),
        "uri": uri(host, workspace_id, label, pane),
        "payload_b64": payload_b64(host, workspace_id, label, pane),
    }


def main(argv):
    usage = ("usage: tendril_link.py encode <host> <workspace-id> [label] [pane]\n"
             "       tendril_link.py decode <payload-or-uri>\n"
             "       tendril_link.py decode-pair <TENDRIL1:/TENDRIL2: code>")
    if len(argv) >= 4 and argv[1] == "encode":
        try:
            link = describe(argv[2], argv[3],
                            argv[4] if len(argv) > 4 else None,
                            argv[5] if len(argv) > 5 else None)
        except LinkError as exc:
            print(f"link error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(link, indent=2))
        return 0
    if len(argv) == 3 and argv[1] == "decode-pair":
        try:
            doc = decode_pairing(argv[2])
        except LinkError as exc:
            print(f"link error: {exc}", file=sys.stderr)
            return 2
        if "k" in doc:
            doc["k"] = NTFY_REDACTED          # never display the token
        print(json.dumps(doc, indent=2))
        return 0
    if len(argv) == 3 and argv[1] == "decode":
        token = argv[2]
        try:
            parsed = (parse_uri(token) if "://" in token
                      else parse_payload_b64(token))
        except LinkError as exc:
            print(f"link error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(parsed, indent=2))
        return 0
    print(usage, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
