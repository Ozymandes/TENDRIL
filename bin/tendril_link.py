#!/usr/bin/env python3
"""tendril-link — canonical deep-link payloads for TENDRIL sessions.

A TENDRIL session is an addressable object. The canonical address is the
Herdr `workspace_id` (e.g. ``w15``) on a named host; panes (``w15:p1``)
resolve to their parent workspace. This module is the single definition of
how that address is serialized for transport between machines (ntfy click
URLs, Termux url-opener shares, iOS Shortcuts input, shell arguments).

Three equivalent wire forms:

  payload (URL-safe, fits any URL query/header value):
      <base64url of compact JSON>          e.g.eyJ2IjoxLCJoIjoiaG9tZSIsInciOiJ3MTUifQ

  URI (human-inspectable):
      tendril://host/<host-id>/workspace/<workspace-id>[?label=<label>]

  parsed dict: {"host": str, "workspace_id": str, "label": str|None}

The payload carries ONLY the host id, the workspace id and an optional
human label. Never keys, tokens, topics, shell text or free-form commands.
Hosts and ids are validated against strict regexes; labels are control-char
stripped, whitespace-collapsed and length-capped. Decoding rejects unknown
fields, wrong versions, oversized input and non-canonical encodings, so a
payload is safe to log, safe to embed in a URL and safe to treat as data
(not code) on the far side.

Import path: lives next to remote-agents / herdr-notify in ~/.local/bin.

CLI (for debugging on phone or host):
    tendril-link encode <host> <workspace-id> [label]
    tendril-link decode <payload-or-uri>
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
    """Malformed host, id, label, payload or URI. Never a security event."""


def clean_label(label):
    """Label-safe text: NFC, control chars dropped, whitespace collapsed,
    capped at MAX_LABEL. Returns None when nothing remains."""
    if label is None:
        return None
    text = unicodedata.normalize("NFC", str(label))
    text = re.sub(r"[\x00-\x1f\x7f\s]+", " ", text).strip()
    return text[:MAX_LABEL].strip() or None


def _validated(host, workspace_id, label=None):
    host = str(host)
    workspace_id = str(workspace_id)
    if not HOST_RE.match(host):
        raise LinkError(f"invalid host: {host!r}")
    if not ID_RE.match(workspace_id):
        raise LinkError(f"invalid workspace id: {workspace_id!r}")
    return host, workspace_id, clean_label(label)


def payload_b64(host, workspace_id, label=None):
    """Compact URL-safe payload (no '=' padding, ASCII only)."""
    host, workspace_id, label = _validated(host, workspace_id, label)
    doc = {"v": LINK_VERSION, "h": host, "w": workspace_id}
    if label:
        doc["l"] = label
    blob = json.dumps(doc, separators=(",", ":"), sort_keys=True,
                      ensure_ascii=True)
    return base64.urlsafe_b64encode(blob.encode("utf-8")).decode("ascii").rstrip("=")


def parse_payload_b64(token):
    """Strict decode. Returns {"host", "workspace_id", "label"}; raises
    LinkError on anything that is not exactly what payload_b64 produces."""
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
    if not isinstance(doc, dict) or set(doc) - {"v", "h", "w", "l"}:
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
    return {"host": host, "workspace_id": ws, "label": clean_label(label)}


def uri(host, workspace_id, label=None):
    """Human-inspectable canonical URI:
    tendril://host/<host-id>/workspace/<workspace-id>[?label=<label>]"""
    host, workspace_id, label = _validated(host, workspace_id, label)
    out = f"{SCHEME}://host/{quote(host, safe='')}/workspace/{quote(workspace_id, safe='')}"
    if label:
        out += "?label=" + quote(label, safe="")
    return out


def parse_uri(value):
    """Inverse of uri(). Strict: scheme must be tendril, netloc 'host',
    path /<host-id>/workspace/<id>, optional ?label=."""
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
    for key, val in (kv.split("=", 1) for kv in parts.query.split("&") if kv):
        if key == "label":
            if label is not None:
                raise LinkError("duplicate label parameter")
            label = unquote(val)
        else:
            raise LinkError(f"unknown uri parameter: {key!r}")
    parsed = {"host": unquote(segments[1]),
              "workspace_id": unquote(segments[3]),
              "label": clean_label(label)}
    _validated(parsed["host"], parsed["workspace_id"])
    return parsed


def describe(host, workspace_id, label=None):
    """All wire forms at once (what notifications embed)."""
    host, workspace_id, label = _validated(host, workspace_id, label)
    return {
        "host": host,
        "workspace_id": workspace_id,
        "label": label,
        "uri": uri(host, workspace_id, label),
        "payload_b64": payload_b64(host, workspace_id, label),
    }


def main(argv):
    usage = ("usage: tendril-link encode <host> <workspace-id> [label]\n"
             "       tendril-link decode <payload-or-uri>")
    if len(argv) >= 4 and argv[1] == "encode":
        try:
            link = describe(argv[2], argv[3],
                            argv[4] if len(argv) > 4 else None)
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
