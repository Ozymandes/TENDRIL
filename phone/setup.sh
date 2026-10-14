#!/data/data/com.termux/files/usr/bin/sh
# TENDRIL phone install (v0.2.1). Public command, in Termux:  tendril install
# First bootstrap on a phone without TENDRIL (installs `tendril`, then runs
# exactly this flow):
#
#     curl -fsSL https://raw.githubusercontent.com/Ozymandes/TENDRIL/main/phone/setup.sh | sh
#
# Run it in Termux from F-Droid. It installs only what the phone needs
# (openssh + mosh), the TENDRIL launchers (~/bin/tendril, the `agent` alias
# and the deep-link helper), turns on Termux's external-apps setting (so
# TENDRIL Link may ask Termux to run its fixed commands), downloads the
# OFFICIAL release-signed TENDRIL Link APK, verifies it, and opens Android's
# installer. Then: scan the QR on your computer. Idempotent - re-running
# updates everything and skips what is current. Full log: ~/.cache/tendril/
# phone-setup.log
#
# Trust: the launchers and the APK come from github.com/Ozymandes/TENDRIL
# over HTTPS (the same trust as this script). The APK must match the
# release manifest's sha256 + size, and BOTH the manifest's signer and the
# APK's actual signing certificate (read from its v3/v2 signature block)
# must equal the production certificate pinned below; Android then
# enforces that signer for every update.
#
# Environment (advanced): TENDRIL_REF (git ref for the launchers, default
# main), TENDRIL_RAW (launcher base URL), TENDRIL_LINK_RELEASE (release tag
# for the APK, default: latest), TENDRIL_LINK_URL (APK + manifest base URL,
# e.g. a local mirror - the pinned signer still decides).

set -u

REF=${TENDRIL_REF:-main}
RAW=${TENDRIL_RAW:-https://raw.githubusercontent.com/Ozymandes/TENDRIL/$REF/phone}
if [ -n "${TENDRIL_LINK_URL:-}" ]; then
    LINK_BASE=$TENDRIL_LINK_URL
elif [ -n "${TENDRIL_LINK_RELEASE:-}" ]; then
    LINK_BASE="https://github.com/Ozymandes/TENDRIL/releases/download/$TENDRIL_LINK_RELEASE"
else
    LINK_BASE="https://github.com/Ozymandes/TENDRIL/releases/latest/download"
fi
# The production signing certificate of TENDRIL Link (SHA-256 of the DER
# cert, lowercase hex). Public by nature; pinned here so a substituted
# manifest cannot introduce a different signer.
LINK_SIGNER=44fa9365d6ac01ef94b8f462a0161f946c023c7377013d198d654f526333e03b
LINK_PACKAGE=app.tendril.link

LOGDIR="$HOME/.cache/tendril"
LOG="$LOGDIR/phone-setup.log"
mkdir -p "$LOGDIR" "$HOME/bin" 2>/dev/null
: > "$LOG" 2>/dev/null || LOG=/dev/null

G=$(printf '\033[38;5;191m'); D=$(printf '\033[38;5;245m'); R=$(printf '\033[38;5;203m'); N=$(printf '\033[0m')
ok()   { printf '%s✓%s %s%s%s\n' "$G" "$N" "$1" "${2:+ $D· $2}" "${2:+$N}"; }
note() { printf '  %s%s%s\n' "$D" "$1" "$N"; }
fail() {
    printf '%s✗%s %s\n' "$R" "$N" "$1"
    shift
    for line in "$@"; do printf '  %s\n' "$line"; done
    exit 1
}

printf '\n%sTENDRIL LINK%s\n%sPreparing Android...%s\n\n' "$G" "$N" "$D" "$N"

# ---------------------------------------------------------------- termux
case "${PREFIX:-}" in
    */com.termux/*) ;;
    *) fail "This is not Termux" "Install Termux from F-Droid (https://f-droid.org/packages/com.termux/)," \
            "open it, and run this command there." ;;
esac
command -v termux-open >/dev/null 2>&1 \
    || fail "Termux is too old (termux-tools missing)" "Update Termux from F-Droid, then run this again."
ok "Termux ${TERMUX_VERSION:-}"

export DEBIAN_FRONTEND=noninteractive
APT_OPTS="-y -o Dpkg::Options::=--force-confnew"
need=""
command -v ssh >/dev/null 2>&1 || need="$need openssh"
command -v mosh >/dev/null 2>&1 || need="$need mosh"
if [ -n "$need" ]; then
    note "installing$need (about a minute)..."
    # shellcheck disable=SC2086
    pkg install $APT_OPTS $need >>"$LOG" 2>&1 || {
        # a fresh bootstrap can be too old to install into: upgrade once
        # shellcheck disable=SC2086
        pkg upgrade $APT_OPTS >>"$LOG" 2>&1
        # shellcheck disable=SC2086
        pkg install $APT_OPTS $need >>"$LOG" 2>&1
    } || fail "Could not install$need" "See $LOG (is the phone online?)"
fi
if ! ssh -V >>"$LOG" 2>&1; then
    # shellcheck disable=SC2086
    pkg upgrade $APT_OPTS >>"$LOG" 2>&1
    ssh -V >>"$LOG" 2>&1 || fail "ssh does not run" "Run  pkg upgrade  once, then this command again."
fi
ok "SSH / Mosh"

# ---------------------------------------------------------------- launchers
fetch() {   # fetch <url> <dest> - download fully, then replace atomically
    _tmp="$2.new.$$"
    if curl -fsSL --max-time 60 -o "$_tmp" "$1" >>"$LOG" 2>&1 && [ -s "$_tmp" ]; then
        mv -f "$_tmp" "$2"
        return 0
    fi
    rm -f "$_tmp"
    return 1
}
for f in tendril agent termux-url-opener; do
    fetch "$RAW/$f" "$HOME/bin/$f" || fail "Could not download the launcher ($f)" "See $LOG"
    chmod 700 "$HOME/bin/$f"
done
sh -n "$HOME/bin/tendril" || fail "The downloaded launcher is damaged" "Run this command again."
grep -qF 'export PATH="$HOME/bin:$PATH"' "$HOME/.bashrc" 2>/dev/null \
    || printf '\nexport PATH="$HOME/bin:$PATH"\n' >> "$HOME/.bashrc"
ok "Launcher" "tendril · agent"

# ---------------------------------------------------------------- integration
PROPS="$HOME/.termux/termux.properties"
if ! grep -Eq '^[[:space:]]*allow-external-apps[[:space:]]*=[[:space:]]*true' "$PROPS" 2>/dev/null; then
    mkdir -p "$HOME/.termux"
    if [ -f "$PROPS" ] && grep -q '^[[:space:]]*allow-external-apps[[:space:]]*=' "$PROPS"; then
        sed 's/^[[:space:]]*allow-external-apps[[:space:]]*=.*/allow-external-apps = true/' "$PROPS" > "$PROPS.new.$$" \
            && mv -f "$PROPS.new.$$" "$PROPS"
    else
        printf 'allow-external-apps = true\n' >> "$PROPS"
    fi
    termux-reload-settings >>"$LOG" 2>&1 || true
fi
ok "Deep links" "TENDRIL Link may run its fixed commands"

# The extra-keys toolbar's KEYBOARD toggle: inside a TENDRIL session the
# host bridge turns mouse tracking on (touch scrolling) and Termux then
# consumes every terminal tap as a mouse click - its onSingleTapUp shows
# the soft keyboard only when tracking is off, so a collapsed keyboard
# cannot be re-raised by tapping the screen (upstream termux-app#5184).
# The toolbar stays visible with the keyboard down and its KEYBOARD key
# raises it regardless of tracking. The user's own extra-keys layout is
# never replaced; the default show/hide toggle behaviour is never
# overridden here.
if ! grep -Eq '^[[:space:]]*extra-keys[[:space:]]*=' "$PROPS" 2>/dev/null; then
    mkdir -p "$HOME/.termux"
    printf '%s\n' "extra-keys = [['ESC','/',{key: '-', popup: '|'},'HOME','UP','END','PGUP','KEYBOARD'], ['TAB','CTRL','ALT','LEFT','DOWN','RIGHT','PGDN']]" >> "$PROPS"
    termux-reload-settings >>"$LOG" 2>&1 || true
fi

# ---------------------------------------------------------------- phone key
# Prepared here, enrolled by pairing: TENDRIL Link scans the host QR and the
# host installs this PUBLIC key (one-time, proof-checked). Never copied by hand.
(umask 077 && mkdir -p "$HOME/.ssh")
if [ ! -f "$HOME/.ssh/id_ed25519" ]; then
    ssh-keygen -q -t ed25519 -N '' -C termux -f "$HOME/.ssh/id_ed25519" </dev/null >>"$LOG" 2>&1 \
        || fail "Could not create this phone's SSH key" "See $LOG"
fi
ok "Phone key" "ed25519 · enrolled when you scan the QR"

# >>> apk_signer (tests extract this block)
# apk_signer <apk> - SHA-256 (lowercase hex) of the first signer certificate
# in the APK's v3 (else v2) signature block; nothing + status 1 when the
# block cannot be read. POSIX sh + od/tail/head/sha256sum (Termux coreutils).
apk_u32() { od -An -tu4 -j "$2" -N4 "$1" 2>/dev/null | tr -d ' \n'; }
apk_u64() {   # little-endian u64 below 2^32 only (APK offsets/lengths)
    _lo=$(apk_u32 "$1" "$2"); _hi=$(apk_u32 "$1" $(($2 + 4)))
    [ "$_hi" = 0 ] && [ -n "$_lo" ] && printf '%s' "$_lo"
}
apk_signer() {
    _f=$1
    _size=$(wc -c < "$_f" | tr -d ' ') || return 1
    _eocd=$((_size - 22))
    [ "$(apk_u32 "$_f" "$_eocd")" = 101010256 ] || return 1      # 0x06054b50, no zip comment
    _cd=$(apk_u32 "$_f" $((_eocd + 16))); [ -n "$_cd" ] || return 1
    [ "$(tail -c +$((_cd - 15)) "$_f" | head -c 16)" = "APK Sig Block 42" ] || return 1
    _bs=$(apk_u64 "$_f" $((_cd - 24))) || return 1
    _pos=$((_cd - _bs))                    # first pair (block start + 8)
    _end=$((_cd - 24))
    _v2="" _v3=""
    while [ "$_pos" -lt "$_end" ]; do
        _len=$(apk_u64 "$_f" "$_pos") || return 1
        _id=$(apk_u32 "$_f" $((_pos + 8)))
        case $_id in
            4031998144) _v3=$((_pos + 12)) ;;  # 0xf05368c0 APK Signature Scheme v3
            1896449818) _v2=$((_pos + 12)) ;;  # 0x7109871a APK Signature Scheme v2
        esac
        _pos=$((_pos + 8 + _len))
    done
    _v=${_v3:-$_v2}; [ -n "$_v" ] || return 1
    # signers(len) > signer(len) > signed data(len) > digests(len+bytes) > certificates(len) > cert(len+bytes)
    _sd=$((_v + 12))
    _dl=$(apk_u32 "$_f" "$_sd"); [ -n "$_dl" ] || return 1
    _certs=$((_sd + 4 + _dl))
    _cl=$(apk_u32 "$_f" $((_certs + 4))); [ -n "$_cl" ] && [ "$_cl" -gt 0 ] || return 1
    tail -c +$((_certs + 9)) "$_f" | head -c "$_cl" | sha256sum | cut -d ' ' -f1
}
# <<< apk_signer

# ---------------------------------------------------------------- TENDRIL Link
case $LINK_SIGNER in
    *[!0-9a-f]* | "") fail "This setup script has no release signer pinned" \
                           "Use the script from a published release (README Quick Start)." ;;
esac
installed_code() {
    cmd package list packages --show-versioncode "$LINK_PACKAGE" 2>/dev/null \
        | sed -n 's/^package:app\.tendril\.link .*versionCode:\([0-9][0-9]*\).*$/\1/p' | head -n 1
}
# link_state - the same strict three-state probe as the host-served
# installer: present | absent | unknown. A FAILED probe (transaction
# errors, SecurityException / INTERACT_ACROSS_USERS, unparseable output)
# is UNKNOWN on some devices (Samsung + Termux) and must never be read
# as installed or absent.
link_state() {
    _ls_saw=0
    _out=$(pm path "$LINK_PACKAGE" 2>/dev/null); _rc=$?
    if [ "$_rc" -eq 0 ]; then
        case $_out in
            package:*) printf present; return 0 ;;
            "") _ls_saw=1 ;;
            *) printf unknown; return 0 ;;
        esac
    fi
    _out=$(cmd package path "$LINK_PACKAGE" 2>/dev/null); _rc=$?
    if [ "$_rc" -eq 0 ]; then
        case $_out in
            package:*) printf present; return 0 ;;
            "") _ls_saw=1 ;;
            *) printf unknown; return 0 ;;
        esac
    fi
    _out=$(cmd package list packages --show-versioncode "$LINK_PACKAGE" 2>/dev/null); _rc=$?
    if [ "$_rc" -eq 0 ]; then
        # presence needs PARSEABLE evidence: a listed versionCode with
        # actual digits - an empty-version line proves nothing
        _ls_code=$(printf '%s\n' "$_out" |
            sed -n "s/^package:$LINK_PACKAGE .*versionCode:\\([0-9][0-9]*\\).*$/\\1/p" |
            head -n 1)
        if [ -n "$_ls_code" ]; then
            printf present; return 0
        fi
        _ls_saw=1
    fi
    if [ "$_ls_saw" -eq 1 ]; then printf absent; else printf unknown; fi
}
MANIFEST="$LOGDIR/tendril-link.json"
fetch "$LINK_BASE/tendril-link.json" "$MANIFEST" \
    || fail "Could not download the TENDRIL Link release manifest" "See $LOG (is the phone online?)"
# flat schema-1 manifest: one "key": value pair per field
mval() { sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([A-Za-z0-9._+-]*\)\"\{0,1\}.*/\1/p" "$MANIFEST" | head -n 1; }
M_PKG=$(mval package); M_CODE=$(mval versionCode); M_NAME=$(mval versionName)
M_SHA=$(mval sha256); M_SIZE=$(mval size); M_SIGNER=$(mval signer_sha256)
[ "$M_PKG" = "$LINK_PACKAGE" ] || fail "The release manifest is not for TENDRIL Link" "Refusing to install."
[ "$M_SIGNER" = "$LINK_SIGNER" ] \
    || fail "The release manifest names a different signing key" "Refusing to install (expected the pinned TENDRIL release key)."
NOW=$(installed_code)
LINK_STATE=$(link_state)
if [ "$LINK_STATE" = unknown ]; then
    fail "Could not verify whether TENDRIL Link is installed" \
        "This device refused the Android package probes (pm path / cmd)." \
        "Nothing was changed or launched. Run this command again later."
fi
if [ -n "$NOW" ] && [ -n "$M_CODE" ] && [ "$NOW" -ge "$M_CODE" ] 2>/dev/null && [ "$LINK_STATE" = present ]; then
    ok "TENDRIL Link $M_NAME" "already installed"
    INSTALLED=1
else
    INSTALLED=0
    APK="$LOGDIR/tendril-link-$M_NAME-$M_CODE.apk"
    note "downloading TENDRIL Link $M_NAME..."
    fetch "$LINK_BASE/tendril-link.apk" "$APK" || fail "Could not download TENDRIL Link" "See $LOG"
    GOT_SHA=$(sha256sum "$APK" | cut -d ' ' -f1)
    GOT_SIZE=$(wc -c < "$APK" | tr -d ' ')
    if [ "$GOT_SHA" != "$M_SHA" ] || [ "$GOT_SIZE" != "$M_SIZE" ]; then
        rm -f "$APK"
        fail "The download failed verification (sha256/size)" "Nothing was installed. Run this command again."
    fi
    # the APK's REAL signing certificate, not just the manifest's word
    GOT_SIGNER=$(apk_signer "$APK" || true)
    if [ "$GOT_SIGNER" != "$LINK_SIGNER" ]; then
        rm -f "$APK"
        fail "The APK is not signed by the TENDRIL release key" "Nothing was installed (signer ${GOT_SIGNER:-unreadable})."
    fi
    ok "Link APK verified" "$M_NAME ($M_CODE) · sha256 · signer ${LINK_SIGNER%"${LINK_SIGNER#????????????}"}"
    printf '\n%sOpening Android installer...%s\n' "$G" "$N"
    note "Tap Install. First time only: Android asks to allow Termux to"
    note "install apps - allow it, go back, then tap Install."
    termux-open --content-type application/vnd.android.package-archive "$APK" >>"$LOG" 2>&1 \
        || fail "Could not open the Android installer" "Open this file from a file manager: $APK"
    # wait for Android to finish (no Enter needed): poll the installed build
    _w=0
    while [ "$_w" -lt 300 ]; do
        NOW=$(installed_code)
        if [ -n "$NOW" ] && [ "$NOW" -ge "$M_CODE" ] 2>/dev/null; then INSTALLED=1; break; fi
        sleep 2; _w=$((_w + 2))
    done
    if [ "$INSTALLED" = 1 ]; then
        ok "TENDRIL Link $M_NAME" "installed"
    else
        note "TENDRIL Link is not installed yet - finish the Android installer, then run  tendril install  again."
    fi
fi

printf '\n'
if [ "$INSTALLED" = 1 ]; then
    am start -n "$LINK_PACKAGE/.MainActivity" >>"$LOG" 2>&1 || true
    printf '%sPhone ready.%s In TENDRIL Link: tap %sScan QR%s and scan the code on your computer.\n' \
        "$G" "$N" "$G" "$N"
    note "No QR on screen? On the computer run:  tendril --pair"
fi
