#!/data/data/com.termux/files/usr/bin/sh
# TENDRIL phone setup - the one public Termux command (v0.2.1):
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
# release manifest's sha256 + size, and the manifest's signer must equal
# the production signing certificate pinned below (verified when the
# release was built); Android then enforces that signer for every update.
#
# Environment (advanced): TENDRIL_REF (git ref for the launchers, default
# main), TENDRIL_LINK_RELEASE (release tag for the APK, default: latest).

set -u

REF=${TENDRIL_REF:-main}
RAW="https://raw.githubusercontent.com/Ozymandes/TENDRIL/$REF/phone"
if [ -n "${TENDRIL_LINK_RELEASE:-}" ]; then
    LINK_BASE="https://github.com/Ozymandes/TENDRIL/releases/download/$TENDRIL_LINK_RELEASE"
else
    LINK_BASE="https://github.com/Ozymandes/TENDRIL/releases/latest/download"
fi
# The production signing certificate of TENDRIL Link (SHA-256 of the DER
# cert, lowercase hex). Public by nature; pinned here so a substituted
# manifest cannot introduce a different signer.
LINK_SIGNER=@LINK_SIGNER_SHA256@
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
tty_read() {   # wait for Enter on the real terminal, even under curl | sh
    if [ -r /dev/tty ]; then read -r _ < /dev/tty; else sleep 20; fi
}

printf '\n%sTENDRIL LINK%s\n%sSetting up this phone...%s\n\n' "$G" "$N" "$D" "$N"

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
ok "SSH + Mosh"

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
ok "Launcher" "tendril, agent, deep-link helper"

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
ok "Termux integration" "TENDRIL Link may run its fixed commands"

# ---------------------------------------------------------------- TENDRIL Link
case $LINK_SIGNER in
    *[!0-9a-f]* | "") fail "This setup script has no release signer pinned" \
                           "Use the script from a published release (README Quick Start)." ;;
esac
installed_code() {
    cmd package list packages --show-versioncode "$LINK_PACKAGE" 2>/dev/null \
        | sed -n 's/^package:app\.tendril\.link .*versionCode:\([0-9][0-9]*\).*$/\1/p' | head -n 1
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
if [ -n "$NOW" ] && [ -n "$M_CODE" ] && [ "$NOW" -ge "$M_CODE" ] 2>/dev/null; then
    ok "TENDRIL Link $M_NAME" "already installed"
else
    APK="$LOGDIR/tendril-link-$M_NAME-$M_CODE.apk"
    note "downloading TENDRIL Link $M_NAME..."
    fetch "$LINK_BASE/tendril-link.apk" "$APK" || fail "Could not download TENDRIL Link" "See $LOG"
    GOT_SHA=$(sha256sum "$APK" | cut -d ' ' -f1)
    GOT_SIZE=$(wc -c < "$APK" | tr -d ' ')
    if [ "$GOT_SHA" != "$M_SHA" ] || [ "$GOT_SIZE" != "$M_SIZE" ]; then
        rm -f "$APK"
        fail "The download failed verification (sha256/size)" "Nothing was installed. Run this command again."
    fi
    ok "APK verified" "$M_NAME ($M_CODE) · signer ${LINK_SIGNER%"${LINK_SIGNER#????????????}"}"
    printf '%s→%s Android installer\n' "$G" "$N"
    note "Tap Install. First time only: Android asks to allow Termux to"
    note "install apps - allow it, go back, then tap Install."
    termux-open --content-type application/vnd.android.package-archive "$APK" >>"$LOG" 2>&1 \
        || fail "Could not open the Android installer" "Open this file from a file manager: $APK"
    printf '  %sWhen it says "App installed", come back and press Enter.%s ' "$D" "$N"
    tty_read
    NOW=$(installed_code)
    if [ -n "$NOW" ] && [ "$NOW" -lt "$M_CODE" ] 2>/dev/null; then
        note "TENDRIL Link does not look installed yet - run this command again after installing."
    fi
fi

am start -n "$LINK_PACKAGE/.MainActivity" >>"$LOG" 2>&1 || true
printf '\n%sPhone ready.%s In TENDRIL Link: tap %sScan QR%s and scan the code on your computer.\n' \
    "$G" "$N" "$G" "$N"
note "No QR on screen? On the computer run:  tendril --pair"
