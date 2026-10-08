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

CLI (for debugging on phone or host):
    tendril_link.py encode <host> <workspace-id> [label] [pane]
    tendril_link.py decode <payload-or-uri>
"""
import base64
import binascii
import json
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
             "       tendril_link.py decode <payload-or-uri>")
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
