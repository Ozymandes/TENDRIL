#!/bin/sh
# TENDRIL host install (v0.2.1). Public command:  tendril install
# First bootstrap on a machine without TENDRIL (installs `tendril`, then
# runs exactly this flow):
#
#     curl -fsSL https://raw.githubusercontent.com/Ozymandes/TENDRIL/main/setup.sh | sh
#
# Checks the platform and prerequisites, installs Herdr when it is missing
# (asking once - its official user-local installer), clones/updates TENDRIL
# into ~/.local/share/tendril/src, runs ./install --auto (every optional step
# takes its recommended default; existing config is kept, never rotated),
# verifies health, and ends on the phone pairing QR. Idempotent: run it again
# any time - it updates and re-checks. Never uses sudo: a missing SYSTEM
# package is ONE line telling you the exact command. Full log:
# ~/.cache/tendril/setup.log
#
# Environment (advanced): TENDRIL_REF (git ref, default main),
# TENDRIL_SRC (checkout dir), TENDRIL_SRC_PINNED=1 (use TENDRIL_SRC as it
# is - never fetch/switch it; `tendril install` sets this for developer
# checkouts), TENDRIL_REPO_URL, TENDRIL_YES=1 (answer yes to the Herdr
# install question without a terminal), TENDRIL_NO_PAIR=1.

set -u

REPO_URL=${TENDRIL_REPO_URL:-https://github.com/Ozymandes/TENDRIL.git}
REF=${TENDRIL_REF:-main}
SRC=${TENDRIL_SRC:-$HOME/.local/share/tendril/src}
LOGDIR="$HOME/.cache/tendril"
LOG="$LOGDIR/setup.log"
BIN="$HOME/.local/bin"
HERDR_MIN=0.9.3

if [ -t 1 ]; then
    G=$(printf '\033[38;5;191m'); D=$(printf '\033[38;5;245m'); R=$(printf '\033[38;5;203m'); N=$(printf '\033[0m')
else
    G=""; D=""; R=""; N=""
fi
ok()   { printf '%s✓%s %s%s%s\n' "$G" "$N" "$1" "${2:+ $D· $2}" "${2:+$N}"; }
note() { printf '  %s%s%s\n' "$D" "$1" "$N"; }
fail() {
    printf '%s✗%s %s\n' "$R" "$N" "$1"
    shift
    for line in "$@"; do printf '  %s\n' "$line"; done
    exit 1
}

mkdir -p "$LOGDIR" 2>/dev/null || LOG=/dev/null
: > "$LOG" 2>/dev/null || LOG=/dev/null

printf '\n%sTENDRIL%s\n%sInstalling control plane...%s\n\n' "$G" "$N" "$D" "$N"

# ask <question> - one yes/no question on the terminal even under curl | sh
ask() {
    [ "${TENDRIL_YES:-}" = 1 ] && return 0
    if [ -r /dev/tty ] && [ -w /dev/tty ]; then
        printf '%s [Y/n] ' "$1" > /dev/tty
        ans=""
        read -r ans < /dev/tty || ans=n
        case $ans in "" | y | Y | yes) return 0 ;; esac
    fi
    return 1
}

# pkg_line <packages...> - the ONE install command for this distro (printed, never run)
pkg_line() {
    if [ "$(uname -s)" = Darwin ]; then echo "brew install $*"; return; fi
    _id=""
    [ -r /etc/os-release ] && _id=$(. /etc/os-release; echo "${ID:-} ${ID_LIKE:-}")
    case " $_id " in
        *" arch "*) echo "sudo pacman -S --needed $*" ;;
        *" fedora "* | *" rhel "* | *" centos "*) echo "sudo dnf install $*" ;;
        *" opensuse"* | *" suse "*) echo "sudo zypper install $*" ;;
        *" alpine "*) echo "sudo apk add $*" ;;
        *) echo "sudo apt install $*" ;;
    esac
}

# ---------------------------------------------------------------- platform
OS=$(uname -s)
case $OS in
    Linux) ;;
    Darwin) note "macOS host: supported without the scheduler/guardian timers (docs/MACOS.md)" ;;
    *) fail "Unsupported platform: $OS" "TENDRIL hosts run on Linux (macOS with limits)." ;;
esac

missing=""
command -v git >/dev/null 2>&1 || missing="$missing git"
command -v ssh >/dev/null 2>&1 || missing="$missing openssh"
PY=$(command -v python3 || true)
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    missing="$missing python3"
fi
if [ -n "$missing" ]; then
    # shellcheck disable=SC2086
    fail "Missing system packages:$missing" "Install them, then run this command again:" "  $(pkg_line $missing)"
fi
ok "$OS $(uname -m)" "python $("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])') · git"

# sshd: phones reach this host over SSH - nothing works without it
sshd_up() {
    "$PY" - <<'EOF' 2>/dev/null
import socket, sys
s = socket.socket(); s.settimeout(2)
try:
    s.connect(("127.0.0.1", 22)); sys.exit(0)
except OSError:
    sys.exit(1)
EOF
}
if ! sshd_up; then
    if [ "$OS" = Darwin ]; then
        fail "SSH server is not running" "Enable it: System Settings > General > Sharing > Remote Login"
    fi
    _svc=sshd
    [ -e /lib/systemd/system/ssh.service ] && _svc=ssh
    command -v sshd >/dev/null 2>&1 || [ -x /usr/sbin/sshd ] \
        || fail "SSH server is not installed" "Install and start it, then run this command again:" \
                "  $(pkg_line openssh-server) && sudo systemctl enable --now $_svc"
    fail "SSH server is not running" "Start it, then run this command again:" \
         "  sudo systemctl enable --now $_svc"
fi
_reach="SSH"
if command -v mosh-server >/dev/null 2>&1; then _reach="$_reach · Mosh"; fi
_ts=""
if command -v tailscale >/dev/null 2>&1; then
    _ts=$(tailscale status --json 2>/dev/null | "$PY" -c 'import json,sys
try: print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))
except Exception: pass' 2>/dev/null)
fi
if [ -n "$_ts" ]; then _reach="$_reach · Tailscale $_ts"; fi
ok "Reachable" "$_reach"
command -v mosh-server >/dev/null 2>&1 \
    || note "Mosh is optional (smoother on mobile networks): $(pkg_line mosh)"
[ -n "$_ts" ] \
    || note "No Tailscale: your phone must reach this host another way (Tailscale recommended)."

# ---------------------------------------------------------------- herdr
herdr_bin() {
    for c in "$(command -v herdr 2>/dev/null)" "$BIN/herdr" \
             "$HOME/.local/share/mise/installs/herdr/latest/herdr"; do
        [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return 0; }
    done
    return 1
}
ver_ge() {   # ver_ge A B: A >= B (dotted numeric)
    "$PY" -c 'import sys
a = [int(x) for x in sys.argv[1].split(".")[:3]]; b = [int(x) for x in sys.argv[2].split(".")[:3]]
sys.exit(0 if a >= b else 1)' "$1" "$2" 2>/dev/null
}
HERDR=$(herdr_bin || true)
if [ -z "$HERDR" ]; then
    if ask "Herdr (the persistent agent runtime) is not installed. Install it now with its official installer (user-local, no sudo)?"; then
        note "installing Herdr (about a minute)..."
        for _try in 1 2; do         # one retry: a slow first download is common
            if curl -fsSL https://herdr.dev/install.sh 2>>"$LOG" | sh >>"$LOG" 2>&1; then
                HERDR=$(herdr_bin || true)
            fi
            [ -n "$HERDR" ] && break
        done
        [ -n "$HERDR" ] || fail "Herdr installation failed" "See $LOG, or install it yourself: https://herdr.dev"
    else
        fail "Herdr is required" "Install it (user-local):  curl -fsSL https://herdr.dev/install.sh | sh" \
             "then run this command again."
    fi
fi
HV=$("$HERDR" --version 2>/dev/null | sed -n 's/^herdr \([0-9][0-9.]*\).*/\1/p')
[ -n "$HV" ] && ver_ge "$HV" "$HERDR_MIN" \
    || fail "Herdr ${HV:-unknown} is too old (TENDRIL needs $HERDR_MIN or newer)" "Update it:  herdr update"
ok "Herdr $HV"

# ---------------------------------------------------------------- source
FIRST=1
[ -f "$HOME/.config/remote-agents/install.json" ] && FIRST=0
if [ "${TENDRIL_SRC_PINNED:-}" = 1 ] && [ -f "$SRC/install" ]; then
    :   # tendril install from a developer checkout: use it exactly as it is
elif [ -d "$SRC/.git" ]; then
    if [ -z "$(git -C "$SRC" status --porcelain 2>/dev/null)" ]; then
        git -C "$SRC" fetch --quiet origin "$REF" >>"$LOG" 2>&1 \
            && git -C "$SRC" checkout --quiet "$REF" >>"$LOG" 2>&1 \
            && git -C "$SRC" merge --quiet --ff-only "origin/$REF" >>"$LOG" 2>&1 \
            || note "could not fast-forward $SRC - continuing with the checkout as it is"
    else
        note "$SRC has local changes - not updating it"
    fi
else
    mkdir -p "$(dirname "$SRC")"
    git clone --quiet --branch "$REF" "$REPO_URL" "$SRC" >>"$LOG" 2>&1 \
        || fail "Could not download TENDRIL" "git clone $REPO_URL failed - see $LOG"
fi

# ---------------------------------------------------------------- install
if ! sh "$SRC/install" --auto </dev/null >>"$LOG" 2>&1; then
    printf '\n'
    tail -n 15 "$LOG" 2>/dev/null | sed 's/^/  /'
    fail "Install failed" "Full log: $LOG"
fi
VERSION=$("$BIN/tendril" --version 2>/dev/null | awk '{print $2}')
ok "Runtime" "tendril ${VERSION:-installed}"

# Herdr must be serving for LIVE, scheduling and recovery: start it once
HEALTH=$("$BIN/tendril" health --json 2>/dev/null || true)
hstate() { printf '%s' "$HEALTH" | "$PY" -c 'import json,sys
try: d = json.load(sys.stdin)
except Exception: d = {}
for k in sys.argv[1].split("."): d = d.get(k, {}) if isinstance(d, dict) else {}
print(d if isinstance(d, str) else "")' "$1" 2>/dev/null; }
if [ "$(hstate herdr.state)" != running ] && [ "$(hstate herdr.state)" != stopped ]; then
    "$BIN/tendril" herdr start >>"$LOG" 2>&1 || true
    HEALTH=$("$BIN/tendril" health --json 2>/dev/null || true)
fi
case $(hstate herdr.state) in
    running) ;;
    stopped) note "Herdr was stopped on purpose - start it with:  tendril herdr start" ;;
    *) note "Herdr is not serving yet - start it with:  tendril herdr start" ;;
esac
for svc in scheduler guardian; do
    case $svc in scheduler) _label=Scheduler ;; *) _label=Guardian ;; esac
    case $(hstate "$svc.state") in
        active) ok "$_label" ;;
        absent) note "$_label: no user systemd session here - see docs/FIRST-INSTALL.md" ;;
        *) note "$_label: $(hstate "$svc.state") - check: tendril service status" ;;
    esac
done
if grep -q '^NTFY_TOPIC=..*' "$HOME/.config/remote-agents/notify.env" 2>/dev/null; then
    ok "Notifications" "private topic"
else
    note "Notifications are not configured (run  tendril install  again)"
fi
case $(hstate notify.state) in
    active) ;;
    *) note "Notification watcher: $(hstate notify.state) - check: tendril service status" ;;
esac
case $(hstate capacity.state) in
    available) ok "Capacity" ;;
    adapters_disabled | absent) ok "Capacity" "local usage logs" ;;
    *) note "Capacity: not reporting yet - check: tendril capacity status" ;;
esac
if "$BIN/tendril" --phone-manifest 2>/dev/null | tail -n 1 | grep -qx end; then
    ok "Link services" "pairing · enrollment · updates"
else
    note "Link services: the phone channel did not answer - run  tendril install  again"
fi

# keep the user services alive after logout on headless hosts
if [ "$OS" = Linux ] && command -v loginctl >/dev/null 2>&1; then
    if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" = no ]; then
        note "Headless host? Keep TENDRIL running after logout:  sudo loginctl enable-linger $(id -un)"
    fi
fi
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) note "New terminals find 'tendril' after:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# ---------------------------------------------------------------- pair
printf '\n'
if [ "${TENDRIL_NO_PAIR:-}" = 1 ]; then
    printf '%sHost ready.%s Pair a phone any time:  tendril --pair\n' "$G" "$N"
    exit 0
fi
if [ "$FIRST" = 0 ] && grep -q 'tendril-phone:' "$HOME/.ssh/authorized_keys" 2>/dev/null; then
    printf '%sHost ready.%s A phone is already linked. Pair another:  tendril --pair\n' "$G" "$N"
    exit 0
fi
if [ -r /dev/tty ]; then
    exec "$BIN/tendril" --pair < /dev/tty
fi
"$BIN/tendril" --pair --no-wait
