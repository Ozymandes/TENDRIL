#!/usr/bin/env python3
"""tendril_phone — the host side of the TENDRIL phone channel.

Everything a phone needs is served over its existing SSH key auth, as
flags on remote-agents (binding contract §2):

    tendril --phone-manifest                 sh-parsable asset manifest
    tendril --phone-asset <name>             raw asset bytes (whitelisted)
    tendril --phone-script <setup|upgrade>   auditable POSIX sh for Termux
    tendril --link-stage <path>              stage a TENDRIL Link APK

Assets are served ONLY from the installed snapshot
(XDG_DATA_HOME honoured):

    ~/.local/share/tendril/phone/{tendril,termux-url-opener,agent}
    ~/.local/share/tendril/link/tendril-link.apk (+ tendril-link.json,
                                                    signer.sha256)

`install` and `install --upgrade` mirror phone/* into the snapshot dir.
The generated phone script embeds the host-computed manifest values, so
an unchanged phone needs one ssh (the script itself) and zero asset
downloads. Unknown --phone-asset names exit 2 with nothing on stdout.
A missing or incomplete snapshot (any of the three launchers) refuses
--phone-manifest / --phone-script with one stderr line and an empty
stdout - never a partial manifest or all-zero placeholder hashes.
Stdlib only, Python 3.9+.
"""
import getpass
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import tendril_apk           # v0.2.1: read the APK's real signer
except ImportError:              # stale install: manifest-only checks
    tendril_apk = None

USAGE = ("usage: tendril --phone-manifest\n"
         "       tendril --phone-asset <tendril|termux-url-opener|agent|link-apk>\n"
         "       tendril --phone-script <setup|upgrade>\n"
         "       tendril --link-stage <path>\n"
         "       tendril --link-fetch [<release-tag>]")

# The PRODUCTION signing certificate of TENDRIL Link (SHA-256 of the DER
# certificate, lowercase hex) - the same value as config/link-signer.sha256
# and the pin in phone/setup.sh (tests keep all three identical). Empty
# until the release key exists: then --link-fetch refuses and --link-stage
# keeps the per-host first-stage pin (development builds).
LINK_RELEASE_SIGNER = "44fa9365d6ac01ef94b8f462a0161f946c023c7377013d198d654f526333e03b"
LINK_RELEASE_BASE = "https://github.com/Ozymandes/TENDRIL/releases"

MANIFEST_HEADER = "tendril-phone-manifest 1"
PHONE_ASSETS = ("tendril", "termux-url-opener", "agent")
STREAM_ASSETS = PHONE_ASSETS + ("link-apk",)
SCRIPT_KINDS = ("setup", "upgrade")
LINK_PACKAGE = "app.tendril.link"
STAGED_KEYS = frozenset(("schema", "package", "versionCode", "versionName",
                         "build", "sha256", "size", "signer_sha256"))
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
REACH_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}@[A-Za-z0-9._-]{1,253}$")


class LinkError(Exception):
    """link_stage refusal: one clear line, non-zero exit, nothing done."""


# ---------------------------------------------------------------- paths
def data_root():
    """XDG_DATA_HOME when set, else ~/.local/share."""
    return os.environ.get("XDG_DATA_HOME") \
        or os.path.join(os.path.expanduser("~"), ".local", "share")


def phone_dir():
    return os.path.join(data_root(), "tendril", "phone")


def link_dir():
    return os.path.join(data_root(), "tendril", "link")


SNAPSHOT_MESSAGE = ("phone files not installed on this host - "
                    "run ./install (or tendril --upgrade) first")


def snapshot_ready():
    """True only when the installed snapshot holds every launcher asset.
    A partial or missing snapshot must never surface as a manifest with
    holes or a script with all-zero placeholder hashes: the channel
    refuses with one line instead."""
    return all(os.path.isfile(os.path.join(phone_dir(), name))
               for name in PHONE_ASSETS)


def refuse_snapshot():
    """One stderr line, exit 1, nothing on stdout."""
    sys.stderr.write(SNAPSHOT_MESSAGE + "\n")
    return 1


def sha256_file(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _config_map():
    """KEY=VALUE lines of ~/.config/remote-agents/config (loose parse,
    the same shape every other tool reads)."""
    cfg = {}
    path = os.path.join(os.path.expanduser("~"), ".config",
                        "remote-agents", "config")
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return cfg


def host_identity():
    """The canonical host id: tendril_link's resolver when importable,
    else config TENDRIL_HOST -> TAILSCALE_HOST -> hostname, sanitized."""
    cfg = _config_map()
    try:
        import tendril_link as tl
    except ImportError:
        tl = None
    if tl is not None and hasattr(tl, "host_identity"):
        try:
            return tl.host_identity(cfg)
        except Exception:                    # never break the manifest on it
            pass
    raw = (cfg.get("TENDRIL_HOST") or cfg.get("TAILSCALE_HOST")
           or os.uname().nodename).strip().lower()
    cleaned = re.sub(r"[^a-z0-9-]", "", raw.replace("_", "-"))
    return cleaned or "host"


def version_token():
    """Single-token version for the manifest line (values never contain
    spaces): install.json provenance first, the checkout's VERSION file
    second, `unknown` last."""
    ver = ""
    try:
        path = os.path.join(os.path.expanduser("~"), ".config",
                            "remote-agents", "install.json")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        if isinstance(doc, dict):
            ver = str(doc.get("version") or "").strip()
    except (OSError, ValueError):
        pass
    if not ver:
        repo = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        try:
            with open(os.path.join(repo, "VERSION"), encoding="utf-8") as f:
                ver = f.read().strip()
        except OSError:
            ver = ""
    ver = ver.split()[0] if ver.split() else ""
    return ver if TOKEN_RE.match(ver) else "unknown"


def ssh_reach():
    """user@target written to a fresh phone's ~/.config/tendril/alias by
    the setup script (config SSH_TARGET -> SSH_ALIAS). Empty when the
    host cannot name a boring, safe value."""
    cfg = _config_map()
    target = (cfg.get("SSH_TARGET") or cfg.get("SSH_ALIAS") or "").strip()
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    reach = "%s@%s" % (user, target) if user and target else ""
    return reach if REACH_RE.match(reach) else ""


# ---------------------------------------------------------------- staged APK
def validate_staged(doc, apk_path):
    """Contract §3 manifest, strict: exact key set, types (never bool),
    package, lowercase-hex digests, size and sha256 of the actual bytes.
    Returns a list of problems (empty = good)."""
    if not isinstance(doc, dict):
        return ["manifest is not a JSON object"]
    problems = []
    keys = set(doc)
    if keys != STAGED_KEYS:
        missing = sorted(STAGED_KEYS - keys)
        extra = sorted(keys - STAGED_KEYS)
        if missing:
            problems.append("manifest missing key(s): %s" % ", ".join(missing))
        if extra:
            problems.append("manifest has unknown key(s): %s" % ", ".join(extra))
    if type(doc.get("schema")) is not int or doc.get("schema") != 1:
        problems.append("schema must be the integer 1")
    if doc.get("package") != LINK_PACKAGE:
        problems.append("package must be %s" % LINK_PACKAGE)
    if type(doc.get("versionCode")) is not int or doc.get("versionCode") <= 0:
        problems.append("versionCode must be an integer > 0")
    if not isinstance(doc.get("versionName"), str) \
            or not TOKEN_RE.match(doc.get("versionName", "")):
        problems.append("versionName must be A-Za-z0-9._+- only")
    if not isinstance(doc.get("build"), str) \
            or not TOKEN_RE.match(doc.get("build", "")):
        problems.append("build must be A-Za-z0-9._+- only")
    for key in ("sha256", "signer_sha256"):
        if not isinstance(doc.get(key), str) or not HEX64_RE.match(doc[key]):
            problems.append("%s must be lowercase hex, 64 chars" % key)
    if type(doc.get("size")) is not int or doc.get("size") <= 0:
        problems.append("size must be an integer > 0")
    if problems:
        return problems
    try:
        actual = os.path.getsize(apk_path)
    except OSError:
        return ["APK unreadable: %s" % apk_path]
    if actual != doc["size"]:
        problems.append("size mismatch: manifest says %d, apk is %d"
                        % (doc["size"], actual))
    digest = sha256_file(apk_path)
    if digest != doc["sha256"]:
        problems.append("sha256 mismatch: manifest says %s, apk is %s"
                        % (doc["sha256"], digest or "unreadable"))
    return problems


def staged_manifest():
    """The staged manifest dict when both files exist and validate, else
    None (the stage step is the gatekeeper; a broken staged state simply
    reads as \"nothing staged\")."""
    apk = os.path.join(link_dir(), "tendril-link.apk")
    json_path = os.path.join(link_dir(), "tendril-link.json")
    try:
        with open(json_path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if validate_staged(doc, apk):
        return None
    return doc


# ---------------------------------------------------------------- manifest
def manifest_text():
    """The exact sh-parsable manifest (binding contract §2)."""
    lines = [MANIFEST_HEADER,
             "host %s" % host_identity(),
             "version %s" % version_token()]
    for name in PHONE_ASSETS:
        digest = sha256_file(os.path.join(phone_dir(), name))
        if digest:
            lines.append("file %s %s" % (name, digest))
    apk = staged_manifest()
    if apk:
        lines.append("apk %d %s %s %d %s"
                     % (apk["versionCode"], apk["versionName"],
                        apk["sha256"], apk["size"], apk["signer_sha256"]))
    lines.append("end")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- assets
def stream_asset(name, out):
    """Write one whitelisted snapshot asset, raw. Returns an exit code:
    0 served, 1 snapshot file missing, 2 name refused (never a byte on
    stdout for refused names)."""
    if name not in STREAM_ASSETS:
        return 2
    if name == "link-apk":
        path = os.path.join(link_dir(), "tendril-link.apk")
    else:
        path = os.path.join(phone_dir(), name)
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
        out.flush()
    except OSError:
        return 1
    return 0


# ---------------------------------------------------------------- script
def _script_values():
    vals = {
        "KIND": "",
        "HOST": host_identity(),
        "VERSION": version_token(),
        "REACH": ssh_reach(),
    }
    for name in PHONE_ASSETS:
        key = "SHA_" + name.upper().replace("-", "_")
        vals[key] = sha256_file(os.path.join(phone_dir(), name)) or ("0" * 64)
    apk = staged_manifest()
    vals["APK_CODE"] = str(apk["versionCode"]) if apk else ""
    vals["APK_NAME"] = apk["versionName"] if apk else ""
    vals["APK_SHA"] = apk["sha256"] if apk else ""
    vals["APK_SIZE"] = str(apk["size"]) if apk else ""
    vals["APK_SIGNER"] = apk["signer_sha256"] if apk else ""
    # Belt and braces: every embedded value must be quote-safe. A value
    # that is not falls back to a safe placeholder (the phone-side sha256
    # check then refuses anything it cannot trust).
    safe = re.compile(r"^[A-Za-z0-9._@+-]*$")
    for key, value in list(vals.items()):
        if not safe.match(value):
            vals[key] = {"HOST": "host", "VERSION": "unknown",
                         "REACH": ""}.get(key, "")
    return vals


_SCRIPT_TEMPLATE = r"""#!/bin/sh
# TENDRIL phone @KIND@ script - generated on the host by:
#     ssh <host> tendril --phone-script @KIND@
# Installs the phone launchers (and a staged TENDRIL Link APK, if any)
# over this phone's existing SSH key auth: fetch, verify sha256, atomic
# replace. Audit before running:  ssh <host> tendril --phone-script @KIND@ | less
# POSIX sh; never touches ~/.ssh; Termux-safe (sha256sum, $PREFIX/tmp).

set -u

MODE='@KIND@'
REACH='@REACH@'                 # user@target; setup writes it to the alias file when missing
# shellcheck disable=SC2034  # audit trail only
HOST_ID='@HOST@'
# shellcheck disable=SC2034  # audit trail only
TENDRIL_VERSION='@VERSION@'
SHA_TENDRIL='@SHA_TENDRIL@'
SHA_TERMUX_URL_OPENER='@SHA_TERMUX_URL_OPENER@'
SHA_AGENT='@SHA_AGENT@'
APK_CODE='@APK_CODE@'           # empty: no TENDRIL Link APK staged on the host
APK_NAME='@APK_NAME@'
APK_SHA='@APK_SHA@'
APK_SIZE='@APK_SIZE@'
APK_SIGNER='@APK_SIGNER@'       # host-pinned signing-cert sha256 (the Link app re-checks it)
LINK_PACKAGE='app.tendril.link'

command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || {
    echo "PHONE // neither sha256sum nor shasum found - refusing to install unverified." >&2
    exit 1
}

if [ -n "${PREFIX:-}" ] && [ -d "$PREFIX/tmp" ]; then
    TMPBASE="$PREFIX/tmp"                     # Termux: a writable $PREFIX/tmp
else
    TMPBASE="${TMPDIR:-/tmp}"
fi

tmpfile() {
    mktemp "$TMPBASE/tendril.XXXXXX"
}

# sha_file <path> - print the sha256 (empty when no hasher answers)
sha_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" 2>/dev/null | cut -d ' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" 2>/dev/null | cut -d ' ' -f1
    fi
}

CONF_ALIAS=""
[ -r "$HOME/.config/tendril/alias" ] && IFS= read -r CONF_ALIAS < "$HOME/.config/tendril/alias"
ALIAS="${TENDRIL_ALIAS:-${AGENT_ALIAS:-$CONF_ALIAS}}"
if [ -z "$ALIAS" ] && [ "$MODE" = setup ]; then
    ALIAS="$REACH"
fi
if [ -z "$ALIAS" ]; then
    echo "PHONE // no host alias: export TENDRIL_ALIAS=<alias> or write it to ~/.config/tendril/alias - nothing was changed." >&2
    exit 1
fi
case $ALIAS in
    -*)
        echo "PHONE // '$ALIAS' is not an SSH alias (leading '-') - nothing was changed." >&2
        exit 2
        ;;
esac

# fetch_asset <name> <sha> <dest> - fetch over ssh, verify, atomic replace
# (mode 700). Returns 0 updated, 2 already current; on any failure: one
# clear line and the old file is kept.
fetch_asset() {
    _fa_name=$1
    _fa_sha=$2
    _fa_dest=$3
    _fa_have=$(sha_file "$_fa_dest")
    [ "$_fa_have" = "$_fa_sha" ] && return 2
    _fa_dl=$(tmpfile) || {
        echo "PHONE // $_fa_name: no writable temp dir ($TMPBASE) - kept the old file." >&2
        return 1
    }
    if ! ssh -o BatchMode=yes "$ALIAS" "remote-agents --phone-asset $_fa_name" > "$_fa_dl"; then
        rm -f "$_fa_dl"
        echo "PHONE // $_fa_name: could not fetch from $ALIAS (ssh failed) - kept the old file." >&2
        return 1
    fi
    _fa_got=$(sha_file "$_fa_dl")
    if [ "$_fa_got" != "$_fa_sha" ]; then
        rm -f "$_fa_dl"
        echo "PHONE // $_fa_name: download failed the sha256 check - kept the old file." >&2
        return 1
    fi
    mkdir -p "${_fa_dest%/*}"
    _fa_stage="${_fa_dest}.new.$$"
    mv -f "$_fa_dl" "$_fa_stage"
    chmod 700 "$_fa_stage"
    mv -f "$_fa_stage" "$_fa_dest"
    return 0
}

# link_installed_code - the installed app.tendril.link versionCode
# (best effort; empty when `cmd` cannot answer).
link_installed_code() {
    command -v cmd >/dev/null 2>&1 || return 0
    cmd package list packages --show-versioncode "$LINK_PACKAGE" 2>/dev/null |
        sed -n "s/^package:$LINK_PACKAGE .*versionCode:\\([0-9][0-9]*\\).*$/\\1/p" |
        head -n 1
    return 0
}

# link_absent - ANDROID PACKAGE REALITY for app.tendril.link (exit 0 =
# ABSENT). Read-only Termux probes, in order: `pm path`, `cmd package
# path`, then the versionCode listing as a last resort. A cached APK and
# a recorded sha from a PAST install are NEVER install evidence: when
# every probe answers empty the package is not installed (installing
# over absent is always safe; "current" over absent is the bug this
# guards - a manual uninstall must be repaired by exactly this command).
link_absent() {
    [ -n "$(pm path "$LINK_PACKAGE" 2>/dev/null | head -n 1)" ] && return 1
    [ -n "$(cmd package path "$LINK_PACKAGE" 2>/dev/null | head -n 1)" ] && return 1
    [ -n "$(link_installed_code)" ] && return 1
    return 0
}

# ensure_external_apps - add `allow-external-apps = true` to
# ~/.termux/termux.properties idempotently; a false line is replaced in
# place, every other line stays byte-identical. Returns 0 when changed.
ensure_external_apps() {
    _ea_props="$HOME/.termux/termux.properties"
    if [ -f "$_ea_props" ] \
       && grep -Eq '^[[:space:]]*allow-external-apps[[:space:]]*=[[:space:]]*true([[:space:]]*#.*)?$' "$_ea_props"; then
        return 1
    fi
    mkdir -p "$HOME/.termux" || return 1
    _ea_new="$HOME/.termux/termux.properties.new.$$"
    if [ -f "$_ea_props" ] && grep -q '^[[:space:]]*allow-external-apps[[:space:]]*=' "$_ea_props"; then
        awk '{ if ($0 ~ /^[[:space:]]*allow-external-apps[[:space:]]*=/)
                   print "allow-external-apps = true"
               else print }' "$_ea_props" > "$_ea_new" \
            || { rm -f "$_ea_new"; return 1; }
    else
        { [ -f "$_ea_props" ] && cat "$_ea_props"; printf 'allow-external-apps = true\n'; } > "$_ea_new" \
            || { rm -f "$_ea_new"; return 1; }
    fi
    mv -f "$_ea_new" "$_ea_props"
    return 0
}

# ensure_extra_keys - put a KEYBOARD toggle on Termux's extra-keys
# toolbar when the user has no extra-keys layout of their own. Inside a
# TENDRIL session the host bridge turns mouse tracking on (touch
# scrolling), and Termux then consumes every terminal TAP as a mouse
# click: its onSingleTapUp shows the soft keyboard ONLY when tracking is
# off, so a collapsed keyboard can never be re-raised by tapping the
# screen (upstream termux-app#5184). The toolbar stays visible with the
# keyboard down and its KEYBOARD key raises it regardless of tracking.
# The user's own extra-keys layout is never touched; the default
# show/hide toggle behaviour is never overridden (this file never writes
# soft-keyboard-toggle-behaviour). Returns 0 when it wrote the row.
ensure_extra_keys() {
    _ek_props="$HOME/.termux/termux.properties"
    if [ -f "$_ek_props" ] \
       && grep -Eq '^[[:space:]]*extra-keys[[:space:]]*=' "$_ek_props"; then
        return 1                                 # the user's layout wins
    fi
    mkdir -p "$HOME/.termux" || return 1
    printf '%s\n' "extra-keys = [['ESC','/',{key: '-', popup: '|'},'HOME','UP','END','PGUP','KEYBOARD'], ['TAB','CTRL','ALT','LEFT','DOWN','RIGHT','PGDN']]" >> "$_ek_props"
    return 0
}

# ensure_rc_path - append the ~/bin PATH line to ~/.bashrc only when absent.
ensure_rc_path() {
    _ep_rc="$HOME/.bashrc"
    # shellcheck disable=SC2016  # the literal line written to ~/.bashrc
    _ep_line='export PATH="$HOME/bin:$PATH"'
    if [ -f "$_ep_rc" ] && grep -qF "$_ep_line" "$_ep_rc"; then
        return 1
    fi
    printf '\n%s\n' "$_ep_line" >> "$_ep_rc"
}

record_link_sha() {
    mkdir -p "$HOME/.config/tendril" || return 1
    _rl="$HOME/.config/tendril/link-apk.sha256.new.$$"
    if printf '%s\n' "$APK_SHA" > "$_rl" && chmod 600 "$_rl"; then
        mv -f "$_rl" "$HOME/.config/tendril/link-apk.sha256"
        return 0
    fi
    rm -f "$_rl"
    return 1
}

# download_link_apk - fetch the staged APK, verify sha256 AND size, park it
# at ~/.cache/tendril/tendril-link-<versionName>.apk; prints the path on
# stdout. Messages go to stderr; returns non-zero on any refusal.
download_link_apk() {
    _dl_dir="$HOME/.cache/tendril"
    mkdir -p "$_dl_dir" || {
        echo "PHONE // TENDRIL Link: cannot create $_dl_dir - nothing downloaded." >&2
        return 1
    }
    _dl_tmp=$(tmpfile) || {
        echo "PHONE // TENDRIL Link: no writable temp dir ($TMPBASE) - nothing downloaded." >&2
        return 1
    }
    if ! ssh -o BatchMode=yes "$ALIAS" "remote-agents --phone-asset link-apk" > "$_dl_tmp"; then
        rm -f "$_dl_tmp"
        echo "PHONE // TENDRIL Link: could not fetch the APK from $ALIAS (ssh failed) - nothing changed." >&2
        return 1
    fi
    _dl_sha=$(sha_file "$_dl_tmp")
    _dl_size=$(wc -c < "$_dl_tmp" | tr -d ' ')
    if [ "$_dl_sha" != "$APK_SHA" ] || [ "$_dl_size" != "$APK_SIZE" ]; then
        rm -f "$_dl_tmp"
        echo "PHONE // TENDRIL Link: download failed the sha256/size check - nothing changed." >&2
        return 1
    fi
    _dl_apk="$_dl_dir/tendril-link-$APK_NAME.apk"
    _dl_stage="${_dl_apk}.new.$$"
    mv -f "$_dl_tmp" "$_dl_stage"
    chmod 600 "$_dl_stage"
    mv -f "$_dl_stage" "$_dl_apk"
    printf '%s' "$_dl_apk"
}

# obtain_link_apk - the staged APK, verified and parked: a cached copy is
# reused ONLY when it passes the exact same sha256+size check (a cached
# APK is a download optimization, NEVER install evidence).
obtain_link_apk() {
    _ob_parked="$HOME/.cache/tendril/tendril-link-$APK_NAME.apk"
    if [ -s "$_ob_parked" ]; then
        _ob_sha=$(sha_file "$_ob_parked")
        _ob_size=$(wc -c < "$_ob_parked" | tr -d ' ')
        if [ "$_ob_sha" = "$APK_SHA" ] && [ "$_ob_size" = "$APK_SIZE" ]; then
            printf '%s' "$_ob_parked"
            return 0
        fi
        rm -f "$_ob_parked"
    fi
    download_link_apk
}

FAILED=0
ROW_LAUNCHER=current
ROW_URL=current
ROW_AGENT=current
LINK_ROW="not staged"
PENDING_APK=""

fetch_asset tendril "$SHA_TENDRIL" "$HOME/bin/tendril"; _rc=$?
[ "$_rc" -eq 0 ] && ROW_LAUNCHER=updated
[ "$_rc" -eq 1 ] && FAILED=1

fetch_asset termux-url-opener "$SHA_TERMUX_URL_OPENER" "$HOME/bin/termux-url-opener"; _rc=$?
[ "$_rc" -eq 0 ] && ROW_URL=updated
[ "$_rc" -eq 1 ] && FAILED=1

fetch_asset agent "$SHA_AGENT" "$HOME/bin/agent"; _rc=$?
[ "$_rc" -eq 0 ] && ROW_AGENT=updated
[ "$_rc" -eq 1 ] && FAILED=1

if [ "$MODE" = setup ]; then
    ROW_ALIAS=""
    ROW_TERMUX=""
    ROW_PATH=""
    if [ -n "$CONF_ALIAS" ]; then
        ROW_ALIAS="kept ($CONF_ALIAS)"
    elif [ -n "${TENDRIL_ALIAS:-}" ] || [ -n "${AGENT_ALIAS:-}" ]; then
        ROW_ALIAS="kept (environment)"
    else
        mkdir -p "$HOME/.config/tendril"
        _al="$HOME/.config/tendril/alias.new.$$"
        if printf '%s\n' "$REACH" > "$_al" && chmod 600 "$_al" \
           && mv -f "$_al" "$HOME/.config/tendril/alias"; then
            ROW_ALIAS="written ($REACH)"
        else
            rm -f "$_al"
            echo "PHONE // alias: could not write ~/.config/tendril/alias" >&2
            FAILED=1
        fi
    fi
    if ensure_extra_keys; then _ek_changed=1; else _ek_changed=0; fi
    if ensure_external_apps; then
        ROW_TERMUX="allow-external-apps = true"
    else
        ROW_TERMUX="allow-external-apps already true"
    fi
    if [ "$_ek_changed" -eq 1 ] || [ "$ROW_TERMUX" = "allow-external-apps = true" ]; then
        if command -v termux-reload-settings >/dev/null 2>&1; then
            termux-reload-settings >/dev/null 2>&1 || true
            ROW_TERMUX="$ROW_TERMUX, settings reloaded"
        fi
    fi
    if ensure_rc_path; then
        ROW_PATH="PATH line added to ~/.bashrc"
    else
        ROW_PATH="PATH already in ~/.bashrc"
    fi
fi

if [ -n "$APK_SHA" ]; then
    # ANDROID PACKAGE REALITY decides - never a cached APK and never a
    # recorded sha from a PAST install (a manual uninstall must be
    # repaired by exactly this command), and a newer build on the phone
    # is never silently downgraded:
    _inst=$(link_installed_code)
    case $_inst in
        "" | *[!0-9]*) _inst="" ;;
    esac
    _link_state=older
    if link_absent; then
        _link_state=absent
    elif [ -z "$_inst" ]; then
        _link_state=installed             # present; version unreadable
    elif [ "$_inst" -eq "$APK_CODE" ]; then
        _link_state=current
    elif [ "$_inst" -gt "$APK_CODE" ]; then
        _link_state=newer
    fi
    case $_link_state in
        current)
            LINK_ROW=current
            record_link_sha || true ;;
        newer)
            LINK_ROW="phone has $_inst > staged $APK_CODE - kept"
            record_link_sha || true ;;
        installed)
            LINK_ROW="installed" ;;
        absent | older)
            PENDING_APK=$(obtain_link_apk); _dl_rc=$?
            if [ "$_dl_rc" -eq 0 ] && [ -n "$PENDING_APK" ]; then
                if [ "$_link_state" = absent ]; then
                    LINK_ROW="not installed - installer ready"
                else
                    LINK_ROW="update ready"
                fi
            else
                LINK_ROW="download failed"
                FAILED=1
            fi ;;
    esac
fi

if [ "$MODE" = setup ]; then
    echo "PHONE // SETUP"
else
    echo "PHONE // UPGRADE"
fi
echo ""
echo "  launcher       $ROW_LAUNCHER"
echo "  url handler    $ROW_URL"
echo "  agent          $ROW_AGENT"
if [ "$MODE" = setup ]; then
    echo "  alias          $ROW_ALIAS"
    echo "  termux         $ROW_TERMUX"
    echo "  bash PATH      $ROW_PATH"
fi
echo "  TENDRIL Link   $LINK_ROW"

if [ -n "$PENDING_APK" ] && [ ! -t 0 ]; then
    # HEADLESS (stdin is not a terminal): the TENDRIL Link app's background
    # RUN_COMMAND. Nothing here may prompt or open an activity: Termux keeps
    # a background command's stdin open until the process exits (a `read`
    # would hang forever), and Termux is not the foreground app, so Android
    # suppresses its activity starts (termux-open would silently do
    # nothing). The verified APK stays parked; the Link app - foreground -
    # reads it through Termux's RUN_COMMAND-gated file provider, re-verifies
    # sha256/size/package/versionCode/signer itself, and opens the system
    # installer (whose confirmation always stays).
    echo "  APK saved: $PENDING_APK"
    echo "  APK verified: $APK_CODE $APK_SHA $APK_SIZE $APK_SIGNER"
    echo "  handoff: app"
elif [ -n "$PENDING_APK" ]; then
    if command -v termux-open >/dev/null 2>&1; then
        # Termux's content provider refuses APK opens unless external apps
        # are allowed - ensure the property (same idempotent logic as setup)
        # and reload the settings when it changed.
        if ensure_external_apps; then
            if command -v termux-reload-settings >/dev/null 2>&1; then
                termux-reload-settings >/dev/null 2>&1 || true
            fi
        fi
        echo "  opening Android installer..."
        termux-open --content-type application/vnd.android.package-archive "$PENDING_APK"
        _to_rc=$?
        if [ "$_to_rc" -ne 0 ]; then
            echo "PHONE // termux-open failed (rc $_to_rc) - open it from a file manager: $PENDING_APK" >&2
            FAILED=1
        else
            printf '  Tap Update, then press Enter here: '
            if read -r _ack; then
                _now=$(link_installed_code)
                case $_now in
                    "" | *[!0-9]*) _now="" ;;
                esac
                if [ -n "$_now" ] && [ "$_now" -lt "$APK_CODE" ]; then
                    echo "  not installed yet - run  tendril --upgrade  again after the update finishes."
                elif record_link_sha; then
                    if [ -n "$_now" ]; then
                        echo "  recorded (versionCode $_now installed)"
                    else
                        echo "  recorded the APK sha - Android finishes the install"
                    fi
                else
                    echo "PHONE // TENDRIL Link: could not write ~/.config/tendril/link-apk.sha256" >&2
                    FAILED=1
                fi
            else
                echo ""
                echo "PHONE // TENDRIL Link: not confirmed, sha not recorded - re-run tendril --upgrade after installing." >&2
            fi
        fi
    else
        echo "  APK saved: $PENDING_APK"
        echo "  termux-open is missing - open it from a file manager to install."
    fi
fi

if [ "$MODE" = setup ] && [ "$FAILED" -eq 0 ]; then
    echo ""
    echo "LINK READY"
    echo "  open the TENDRIL Link app on this phone, then pair it with the host:"
    echo "    run  tendril --pair  on the host and scan or paste the code"
    echo "  console now:  tendril"
fi

[ "$FAILED" -eq 1 ] && exit 1

# launch_link - open TENDRIL Link when this ran interactively: install,
# repair and update all end on the app. Headless (the Link app's own
# background RUN_COMMAND) never starts activities - Android suppresses
# them from the background anyway, and the handoff is the app's.
if [ -t 0 ] && command -v am >/dev/null 2>&1; then
    am start -n "$LINK_PACKAGE/.MainActivity" >/dev/null 2>&1 || true
fi
exit 0
"""


def phone_script(kind):
    """The generated POSIX sh script for 'setup' | 'upgrade', manifest
    values embedded. Passes `sh -n` and `shellcheck -s sh`."""
    if kind not in SCRIPT_KINDS:
        raise ValueError("kind must be one of %s" % (", ".join(SCRIPT_KINDS)))
    vals = _script_values()
    vals["KIND"] = kind
    out = _SCRIPT_TEMPLATE
    for key, value in vals.items():
        out = out.replace("@%s@" % key, value)
    return out


# ---------------------------------------------------------------- stage
def _atomic_copy(src, dst):
    """Copy src to dst via a temp file in the destination dir + os.replace."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".stage-", dir=os.path.dirname(dst))
    try:
        with open(src, "rb") as f, os.fdopen(fd, "wb") as g:
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                g.write(chunk)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write(text, dst, mode=0o644):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".stage-", dir=os.path.dirname(dst))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def link_stage(path):
    """`tendril --link-stage <path>` (host, dev path): verify the built
    TENDRIL Link manifest + APK, then copy both atomically into the link
    snapshot dir. Pins the signer on first stage; refuses a signer change
    or a versionCode downgrade with one clear line. Returns exit code."""
    p = os.path.expanduser(path)
    if os.path.isdir(p):
        json_path = os.path.join(p, "tendril-link.json")
        apk_path = os.path.join(p, "tendril-link.apk")
    elif os.path.isfile(p) and p.endswith(".json"):
        json_path = p
        apk_path = os.path.join(os.path.dirname(os.path.abspath(p)),
                                "tendril-link.apk")
    else:
        sys.stderr.write("tendril --link-stage: pass a directory holding "
                         "tendril-link.apk + tendril-link.json, or the json "
                         "itself\n")
        return 2
    try:
        with open(json_path, encoding="utf-8") as f:
            doc = json.load(f)
    except OSError:
        sys.stderr.write("tendril --link-stage: refusing to stage: no "
                         "tendril-link.json at %s\n"
                         % (os.path.dirname(os.path.abspath(json_path))))
        return 1
    except ValueError:
        sys.stderr.write("tendril --link-stage: refusing to stage: "
                         "tendril-link.json is not valid JSON\n")
        return 1
    if not os.path.isfile(apk_path):
        sys.stderr.write("tendril --link-stage: refusing to stage: no "
                         "tendril-link.apk next to %s\n" % json_path)
        return 1
    problems = validate_staged(doc, apk_path)
    if problems:
        sys.stderr.write("tendril --link-stage: refusing to stage: %s\n"
                         % problems[0])
        return 1

    # v0.2.1: the APK's ACTUAL signer must be the manifest's - a manifest
    # can say anything; the signing block cannot be faked without the key
    if tendril_apk is not None:
        try:
            actual = tendril_apk.signer_sha256s(apk_path)
        except (OSError, tendril_apk.ApkError) as exc:
            sys.stderr.write("tendril --link-stage: refusing to stage: "
                             "cannot read the APK signer (%s)\n" % exc)
            return 1
        if doc["signer_sha256"] not in actual:
            sys.stderr.write("tendril --link-stage: refusing to stage: the "
                             "APK is signed by %s, not the manifest's %s\n"
                             % (actual[0][:12], doc["signer_sha256"][:12]))
            return 1

    link = link_dir()
    pin_path = os.path.join(link, "signer.sha256")
    old_pin = None
    try:
        with open(pin_path, encoding="utf-8") as f:
            old_pin = f.read().strip()
    except OSError:
        old_pin = None
    rotated = False
    if old_pin and old_pin != doc["signer_sha256"]:
        # the ONE allowed change: a development pin -> the official release
        # key (pinned in this file). Phones on the old key must reinstall
        # once; Android refuses an in-place update across signers.
        if LINK_RELEASE_SIGNER and doc["signer_sha256"] == LINK_RELEASE_SIGNER:
            rotated = True
        else:
            sys.stderr.write("tendril --link-stage: refusing to stage: signing "
                             "key changed - Android would refuse an in-place "
                             "update\n")
            return 1
    if LINK_RELEASE_SIGNER and old_pin == LINK_RELEASE_SIGNER \
            and doc["signer_sha256"] != LINK_RELEASE_SIGNER:
        sys.stderr.write("tendril --link-stage: refusing to stage: not signed "
                         "by the TENDRIL release key\n")
        return 1

    staged_json = os.path.join(link, "tendril-link.json")
    try:
        with open(staged_json, encoding="utf-8") as f:
            prev = json.load(f)
        prev_code = prev.get("versionCode") if isinstance(prev, dict) else None
    except (OSError, ValueError):
        prev_code = None
    if type(prev_code) is int and doc["versionCode"] < prev_code and not rotated:
        sys.stderr.write("tendril --link-stage: refusing to stage: "
                         "versionCode %d is lower than the staged %d\n"
                         % (doc["versionCode"], prev_code))
        return 1

    _atomic_copy(apk_path, os.path.join(link, "tendril-link.apk"))
    _atomic_write(json.dumps(doc, sort_keys=True) + "\n", staged_json)
    if not old_pin or rotated:
        _atomic_write(doc["signer_sha256"] + "\n", pin_path)

    if rotated:
        print("  signer rotated to the TENDRIL release key - phones still on the "
              "development build reinstall TENDRIL Link once")
    print("TENDRIL // LINK STAGED")
    print("  version   %s" % doc["versionName"])
    print("  build     %s" % doc["build"])
    print("  sha256    %s" % doc["sha256"][:12])
    return 0


def link_fetch(tag=None, opener=None, out=sys.stdout):
    """`tendril --link-fetch [<tag>]` (v0.2.1): stage the OFFICIAL TENDRIL
    Link release for this host's phones - download the release manifest
    and APK from GitHub (latest release, or <tag>), require the pinned
    release signer in the manifest AND in the APK's signing block, the
    sha256 + size, then stage it through link_stage (versionCode and pin
    rules included). Idempotent: an already-staged build is a no-op.
    Returns an exit code; one line on any refusal."""
    if not LINK_RELEASE_SIGNER:
        sys.stderr.write("tendril --link-fetch: no release signer is pinned in "
                         "this TENDRIL version - nothing fetched\n")
        return 1
    if tag is not None and not TOKEN_RE.match(tag):
        sys.stderr.write("tendril --link-fetch: bad release tag\n")
        return 2
    base = ("%s/download/%s" % (LINK_RELEASE_BASE, tag) if tag
            else "%s/latest/download" % LINK_RELEASE_BASE)
    open_ = opener or urllib.request.urlopen

    def get(name, limit):
        req = urllib.request.Request("%s/%s" % (base, name),
                                     headers={"User-Agent": "tendril-link-fetch"})
        with open_(req, timeout=60) as resp:
            body = resp.read(limit + 1)
        if len(body) > limit:
            raise ValueError("%s is larger than expected" % name)
        return body

    try:
        doc = json.loads(get("tendril-link.json", 64 * 1024).decode("utf-8"))
    except (OSError, ValueError) as exc:
        sys.stderr.write("tendril --link-fetch: could not read the release "
                         "manifest (%s)\n" % exc)
        return 1
    if not isinstance(doc, dict) or doc.get("signer_sha256") != LINK_RELEASE_SIGNER:
        sys.stderr.write("tendril --link-fetch: refusing: the release manifest "
                         "does not name the pinned TENDRIL release key\n")
        return 1
    staged = staged_manifest()
    if staged and staged.get("sha256") == doc.get("sha256"):
        out.write("TENDRIL Link %s (%s) already staged\n"
                  % (doc.get("versionName"), doc.get("versionCode")))
        return 0
    size = doc.get("size")
    if type(size) is not int or not 0 < size <= 200 * 1024 * 1024:
        sys.stderr.write("tendril --link-fetch: refusing: implausible APK size\n")
        return 1
    workdir = tempfile.mkdtemp(prefix="tendril-link-fetch-")
    try:
        try:
            apk = get("tendril-link.apk", size)
        except (OSError, ValueError) as exc:
            sys.stderr.write("tendril --link-fetch: could not download the APK "
                             "(%s)\n" % exc)
            return 1
        with open(os.path.join(workdir, "tendril-link.apk"), "wb") as f:
            f.write(apk)
        with open(os.path.join(workdir, "tendril-link.json"), "w",
                  encoding="utf-8") as f:
            json.dump(doc, f)
        if tendril_apk is None:
            sys.stderr.write("tendril --link-fetch: tendril_apk.py is missing - "
                             "re-run ./install\n")
            return 1
        return link_stage(workdir)        # sha256/size/schema/signer/pin rules
    finally:
        for name in ("tendril-link.apk", "tendril-link.json"):
            try:
                os.remove(os.path.join(workdir, name))
            except OSError:
                pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass


# ---------------------------------------------------------------- main
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write(USAGE + "\n")
        return 2
    head = argv[0]
    if head == "--phone-manifest":
        if len(argv) != 1:
            sys.stderr.write(USAGE + "\n")
            return 2
        if not snapshot_ready():
            return refuse_snapshot()
        sys.stdout.write(manifest_text())
        return 0
    if head == "--phone-asset":
        if len(argv) != 2:
            sys.stderr.write(USAGE + "\n")
            return 2
        code = stream_asset(argv[1], sys.stdout.buffer)
        if code == 1:
            sys.stderr.write("tendril --phone-asset: %s is not in the "
                             "installed snapshot - re-run ./install\n"
                             % argv[1])
        return code
    if head == "--phone-script":
        if len(argv) != 2 or argv[1] not in SCRIPT_KINDS:
            sys.stderr.write(USAGE + "\n")
            return 2
        if not snapshot_ready():
            return refuse_snapshot()
        sys.stdout.write(phone_script(argv[1]))
        return 0
    if head == "--link-fetch":
        if len(argv) > 2:
            sys.stderr.write(USAGE + "\n")
            return 2
        return link_fetch(argv[1] if len(argv) == 2 else None)
    if head == "--link-stage":
        if len(argv) != 2:
            sys.stderr.write(USAGE + "\n")
            return 2
        return link_stage(argv[1])
    sys.stderr.write(USAGE + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
