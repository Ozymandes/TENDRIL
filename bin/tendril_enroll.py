#!/usr/bin/env python3
"""tendril_enroll — one-shot phone enrollment for first-time pairing (v0.2.1).

The problem it solves: a brand-new phone must reach the host over SSH, but
until its key is in ~/.ssh/authorized_keys it cannot — and the two-command
install promises no password typing and no laptop-to-phone file transfer.

The rendezvous (all over the ntfy server TENDRIL already uses):

  host   `tendril --pair` mints a SESSION: a random enrollment topic +
         secret, valid ENROLL_TTL_S, stored 0600 in the state dir, and puts
         both in the pairing QR (code v3) next to the host's ed25519
         host-key fingerprint.
  phone  TENDRIL Link scans the QR and runs the fixed Termux verb
         `tendril enroll <target> <url> <topic> <secret> <fp>`: it pins the
         host key (keyscan + fingerprint match), then publishes ONE line
             tendril-enroll-1 ssh-ed25519 <base64> <label> <proof>
         with proof = sha256(secret + "\\n" + "ssh-ed25519 <base64>").
  host   polls the topic (the `--pair` waiter every 2 s, or the guardian
         timer every 30 s when nobody is waiting), checks the proof in
         constant time, appends the key to authorized_keys, and CONSUMES
         the session — one phone per QR, never twice.

Security posture: whoever holds the QR can enroll a key while the session
lives, so the QR is a short-lived credential exactly like an SSH password
prompt shown on your own screen: 10 minutes, one use, never printed as
text by default. The topic is random (unguessable); public keys are not
secret; the secret never leaves the QR. Only ssh-ed25519 keys with a
strict base64 body are accepted; labels are sanitized; nothing from the
message reaches a shell.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ENROLL_TTL_S = 600
POLL_TIMEOUT_S = 10
MSG_RE = re.compile(
    r"^tendril-enroll-1 (ssh-ed25519) (AAAAC3NzaC1lZDI1NTE5AAAAI[A-Za-z0-9+/]{43}=?) "
    r"([A-Za-z0-9._-]{1,32}) ([0-9a-f]{64})$")
TOPIC_PREFIX = "tendril-enroll-"
HOSTKEY_PATHS = ("/etc/ssh/ssh_host_ed25519_key.pub",
                 "/usr/local/etc/ssh/ssh_host_ed25519_key.pub",
                 "/opt/homebrew/etc/ssh/ssh_host_ed25519_key.pub")


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "tendril")


def state_path():
    return os.path.join(state_dir(), "enroll.json")


# ---------------------------------------------------------------- host key
def fingerprint_of_pubkey_line(line):
    """OpenSSH `SHA256:<base64 no padding>` of a `type base64 [comment]`
    public-key line, or None. Pure Python — no ssh-keygen needed."""
    parts = (line or "").split()
    if len(parts) < 2:
        return None
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (ValueError, TypeError):
        return None
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def host_key_fingerprint(paths=None):
    """The host's ed25519 host-key fingerprint, or None when the host has
    none (the phone then falls back to accept-new, said out loud).
    TENDRIL_HOSTKEY_PUB overrides the search (tests, unusual sshd layouts)."""
    if paths is None:
        override = os.environ.get("TENDRIL_HOSTKEY_PUB")
        paths = (override,) if override else HOSTKEY_PATHS
    for path in paths:
        try:
            with open(path, encoding="ascii") as f:
                line = f.readline().strip()
        except (OSError, UnicodeDecodeError):
            continue
        if line.startswith("ssh-ed25519 "):
            return fingerprint_of_pubkey_line(line)
    return None


# ---------------------------------------------------------------- session
def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def start(ntfy_url, now=None):
    """Mint and persist a fresh session (replacing any earlier one: only
    the newest QR is valid). Returns the session dict."""
    now = int(time.time() if now is None else now)
    session = {
        "v": 1,
        "url": ntfy_url.rstrip("/"),
        "topic": TOPIC_PREFIX + secrets.token_urlsafe(18),   # 144-bit topic
        "secret": secrets.token_urlsafe(24),                 # 192-bit secret
        "created": now,
        "expires": now + ENROLL_TTL_S,
    }
    _atomic_write(state_path(), json.dumps(session) + "\n")
    return session


def pending(now=None):
    """The live session, or None (absent, malformed or expired — an
    expired one is removed)."""
    now = time.time() if now is None else now
    try:
        with open(state_path()) as f:
            session = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(session, dict) or not all(
            isinstance(session.get(k), str) for k in ("url", "topic", "secret")):
        return None
    if not isinstance(session.get("expires"), int) or session["expires"] < now:
        clear()
        return None
    return session


def clear():
    try:
        os.remove(state_path())
    except OSError:
        pass


# ---------------------------------------------------------------- proof
def proof_for(secret, key_type, key_b64):
    msg = ("%s\n%s %s" % (secret, key_type, key_b64)).encode("ascii")
    return hashlib.sha256(msg).hexdigest()


def verify_message(text, secret):
    """(key_line, label) when [text] is a well-formed enrollment carrying
    a valid proof for [secret]; else None. Constant-time comparison."""
    m = MSG_RE.match((text or "").strip())
    if not m:
        return None
    key_type, key_b64, label, proof = m.groups()
    try:
        blob = base64.b64decode(key_b64, validate=True)
    except (ValueError, TypeError):
        return None
    if len(blob) != 51:                      # 4+11 'ssh-ed25519' + 4+32 key
        return None
    if not hmac.compare_digest(proof, proof_for(secret, key_type, key_b64)):
        return None
    return "%s %s" % (key_type, key_b64), label


# ---------------------------------------------------------------- authorized_keys
def authorized_keys_path():
    return os.path.join(os.path.expanduser("~"), ".ssh", "authorized_keys")


def install_key(key_line, label, when=None):
    """Append the key once (idempotent) with a `tendril-phone` comment;
    ~/.ssh 0700, authorized_keys 0600. Returns "added" or "present"."""
    path = authorized_keys_path()
    ssh_dir = os.path.dirname(path)
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(ssh_dir, 0o700)
    except OSError:
        pass
    body = key_line.split()[1]
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if body in parts:
                    return "present"
    except OSError:
        pass
    stamp = time.strftime("%Y-%m-%d", time.localtime(when))
    with open(path, "a", encoding="ascii") as f:
        f.write("%s tendril-phone:%s:%s\n" % (key_line, label, stamp))
    os.chmod(path, 0o600)
    return "added"


# ---------------------------------------------------------------- ntfy poll
def fetch_messages(session, opener=None):
    """Message bodies published to the session topic since it was minted
    (ntfy poll API). Network errors raise OSError for the caller."""
    url = "%s/%s/json?poll=1&since=%d" % (
        session["url"], urllib.parse.quote(session["topic"], safe=""),
        int(session["created"]) - 5)
    req = urllib.request.Request(url, headers={"User-Agent": "tendril-enroll"})
    open_ = opener or urllib.request.urlopen
    with open_(req, timeout=POLL_TIMEOUT_S) as resp:
        raw = resp.read(256 * 1024).decode("utf-8", "replace")
    out = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "message" \
                and isinstance(event.get("message"), str):
            out.append(event["message"])
    return out


def process_once(session=None, opener=None, now=None):
    """One poll. Returns {"state": "enrolled", "label", "fingerprint",
    "result"} after installing a verified key (session consumed), or
    {"state": "waiting"|"expired"|"none"|"unreachable"}."""
    session = session or pending(now)
    if session is None:
        return {"state": "none"}
    if session["expires"] < (time.time() if now is None else now):
        clear()
        return {"state": "expired"}
    try:
        bodies = fetch_messages(session, opener)
    except (OSError, urllib.error.URLError, ValueError):
        return {"state": "unreachable"}
    for body in bodies:
        hit = verify_message(body, session["secret"])
        if hit is None:
            continue
        key_line, label = hit
        result = install_key(key_line, label)
        clear()
        return {"state": "enrolled", "label": label, "result": result,
                "fingerprint": fingerprint_of_pubkey_line(key_line)}
    return {"state": "waiting"}


def tick():
    """Guardian hook: enroll a pending phone even when no `tendril --pair`
    is waiting in a terminal. Silent; never raises."""
    try:
        if pending() is not None:
            return process_once()
    except Exception:                         # a tick must never fail
        pass
    return {"state": "none"}


if __name__ == "__main__":
    if sys.argv[1:] == ["fingerprint"]:
        print(host_key_fingerprint() or "none")
    elif sys.argv[1:] == ["tick"]:
        print(json.dumps(tick()))
    else:
        print("usage: tendril_enroll.py fingerprint|tick", file=sys.stderr)
        sys.exit(2)
