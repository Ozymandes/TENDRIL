#!/data/data/com.termux/files/usr/bin/bash
# TENDRIL phone setup for Termux — run once:  bash tendril-phone-setup.sh
# Does: install ssh+mosh, create key, write ~/.ssh/config, copy key to host,
# set up the computer (Herdr + TENDRIL, no sudo),
# install the `tendril` launcher, and add a home-screen shortcut (Termux:Widget),
# then verify phone + computer.  Re-check any time:  bash tendril-phone-setup.sh --check
set -e

ask() { local v; read -r -p "$1 [$2]: " v; echo "${v:-$2}"; }

# ---- verification: phone side, connection, and the computer side ----
verify() {
    set +e
    local A="$1" pass=0 fail=0 warn=0
    ok()   { echo "   [ OK ] $1"; pass=$((pass+1)); }
    bad()  { echo "   [FAIL] $1"; [ -n "$2" ] && echo "          fix: $2"; fail=$((fail+1)); }
    note() { echo "   [WARN] $1"; [ -n "$2" ] && echo "          tip: $2"; warn=$((warn+1)); }
    # Run on the computer through a login shell, like tendril's mosh path does.
    remote() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$A" "sh -lc '$1'" 2>/dev/null; }

    echo; echo "== Verifying TENDRIL (alias: $A) =="
    echo " Phone:"
    [ -x ~/bin/tendril ] && ok "tendril launcher installed (~/bin/tendril)" \
        || bad "tendril launcher missing" "re-run this script"
    command -v mosh >/dev/null && ok "mosh installed" || bad "mosh missing" "pkg install mosh"

    echo " Connection:"
    if ssh -o BatchMode=yes -o ConnectTimeout=8 "$A" true 2>/dev/null; then
        ok "SSH key login to '$A' works (as $(ssh -G "$A" | awk '/^user /{print $2}'))"
    else
        bad "cannot log in to '$A' with the key" "turn on the Tailscale app, check the computer is awake, then: ssh $A"
        echo; echo " Result: $pass ok, $fail failed - stopping (computer checks need SSH)."
        return 1
    fi
    if timeout 20 mosh "$A" -- true >/dev/null 2>&1; then
        ok "mosh connection works"
    else
        note "mosh did not connect (tendril will fall back to SSH)" \
             "allow UDP 60000-61000 on the computer, e.g.: sudo ufw allow in on tailscale0 to any port 60000:61000 proto udp"
    fi

    echo " Computer:"
    local v
    v=$(remote 'herdr --version')
    [ -n "$v" ] && ok "herdr installed ($v)" \
        || bad "herdr not found for this user" "on the computer: curl -fsSL https://herdr.dev/install.sh | sh"
    if remote 'herdr status' | grep -q 'status: *running'; then
        ok "Herdr server is running"
    else
        note "Herdr server is not running (the menu will be empty)" "on the computer, run: herdr"
    fi
    if remote 'command -v remote-agents' >/dev/null; then
        ok "remote-agents (the menu) installed"
        v=$(remote 'remote-agents --detect' | sed -n 's/^chosen: //p' | grep -vx NONE)
        [ -n "$v" ] && ok "menu found a compatible herdr ($v)" \
            || bad "menu cannot use herdr" "on the computer: remote-agents --detect"
    else
        bad "remote-agents not installed on the computer" "on the computer: cd ~/TENDRIL && ./install"
    fi
    if remote 'test -f ~/.config/remote-agents/notify.env'; then
        ok "notifications configured (notify.env)"
        if remote 'systemctl --user is-active --quiet herdr-notify'; then
            ok "notification watcher is running"
        else
            note "notification watcher not running" "on the computer: systemctl --user enable --now herdr-notify"
        fi
    else
        note "notifications not set up (optional)" "on the computer: cd ~/TENDRIL && ./install  (answer yes to ntfy)"
    fi

    echo
    echo " Result: $pass ok, $warn warnings, $fail failed"
    [ "$fail" -eq 0 ] && echo " All required checks passed. Close and reopen Termux, then type: tendril"
    return "$fail"
}

if [ "$1" = "--check" ]; then
    verify "${2:-${TENDRIL_ALIAS:-home}}"
    exit $?
fi

echo "== TENDRIL phone setup =="
HOST_ADDR=$(ask "Computer's Tailscale name or 100.x IP" "")
[ -z "$HOST_ADDR" ] && { echo "A host address is required."; exit 1; }
HOST_USER=$(ask "Your Linux username on that computer" "")
[ -z "$HOST_USER" ] && { echo "A username is required."; exit 1; }
ALIAS=$(ask "Short name for this computer" "home")
REPO=$(ask "TENDRIL folder on the computer" "TENDRIL")

# These values are embedded in remote SSH commands below; spaces or quotes
# would break the hand-off (or the ssh config). Keep them simple.
case "$ALIAS$HOST_USER$REPO" in
    *[!A-Za-z0-9._/-]*)
        echo "Alias, username and folder must be simple: letters, digits,"
        echo "dot, dash, underscore, slash - no spaces or quotes. Got: '$ALIAS' / '$HOST_USER' / '$REPO'"
        exit 1 ;;
esac

echo; echo "1) Installing openssh + mosh..."
pkg update -y && pkg install -y openssh mosh

echo; echo "2) SSH key..."
mkdir -p ~/.ssh && chmod 700 ~/.ssh
if [ ! -f ~/.ssh/id_ed25519 ]; then
    ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" -C termux
else
    echo "   key already exists, keeping it"
fi

echo; echo "3) ~/.ssh/config entry '$ALIAS'..."
touch ~/.ssh/config && chmod 600 ~/.ssh/config
if grep -qiE "^[[:space:]]*Host[[:space:]].*\b$ALIAS\b" ~/.ssh/config; then
    # Replace any old entry for this alias (e.g. one with the wrong User).
    cp ~/.ssh/config ~/.ssh/config.bak
    awk -v a="$ALIAS" '
        tolower($1)=="host" || tolower($1)=="match" {
            skip=0
            if (tolower($1)=="host") for (i=2;i<=NF && $i !~ /^#/;i++) if ($i==a) skip=1
        }
        !skip' ~/.ssh/config.bak > ~/.ssh/config
    echo "   replaced old '$ALIAS' entry (backup: ~/.ssh/config.bak)"
fi
cat >> ~/.ssh/config <<EOF

Host $ALIAS
    HostName $HOST_ADDR
    User $HOST_USER
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
EOF
echo "   '$ALIAS' -> $HOST_USER@$HOST_ADDR"

echo; echo "4) Copying the key to the computer (enter your Linux password once)..."
if ssh -o BatchMode=yes -o ConnectTimeout=5 "$ALIAS" true 2>/dev/null; then
    echo "   key already works"
else
    ssh -o StrictHostKeyChecking=accept-new "$ALIAS" \
        'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys' \
        < ~/.ssh/id_ed25519.pub
    ssh -o BatchMode=yes "$ALIAS" true \
        && echo "   passwordless login works" \
        || { echo "   key login still fails — check Tailscale is on and try again"; exit 1; }
fi

echo; echo "5) Setting up the computer (Herdr + TENDRIL host side)..."
# Runs on the computer as your user: no sudo. Installs Herdr if missing,
# clones or updates TENDRIL, runs its installer the first time (interactive),
# and pins Herdr's full path so SSH/Mosh logins can always find it.
HOST_SCRIPT="${TMPDIR:-$HOME}/tendril-host-setup.sh"
cat > "$HOST_SCRIPT" <<'HOSTEOF'
set -e
REPO="$HOME/$1"
export PATH="$HOME/.local/bin:$PATH"
say() { echo "   $*"; }

# ~/.local/bin on PATH for future logins (Herdr and the menu live there)
for f in "$HOME/.profile" "$HOME/.bashrc"; do
    grep -q '\.local/bin' "$f" 2>/dev/null \
        || { echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$f"; say "added ~/.local/bin to PATH in $f"; }
done

command -v python3 >/dev/null || { say "MISSING python3 - on the computer run: sudo apt install python3"; exit 1; }

if command -v herdr >/dev/null; then
    say "herdr already installed ($(herdr --version 2>/dev/null))"
else
    say "installing Herdr..."
    curl -fsSL https://herdr.dev/install.sh | sh
    command -v herdr >/dev/null || { say "Herdr install failed"; exit 1; }
fi

if [ -d "$REPO/.git" ]; then
    git -C "$REPO" pull --ff-only -q && say "TENDRIL updated ($REPO)" \
        || say "could not update $REPO (local changes?) - keeping current version"
else
    command -v git >/dev/null || { say "MISSING git - on the computer run: sudo apt install git"; exit 1; }
    git clone -q https://github.com/Ozymandes/TENDRIL.git "$REPO"
    say "TENDRIL cloned to $REPO"
fi

if [ -x "$HOME/.local/bin/remote-agents" ]; then
    # Already installed: refresh the programs only, keep config (per TENDRIL README)
    for f in remote-agents herdr-notify tendril_link.py; do
        install -m 755 "$REPO/bin/$f" "$HOME/.local/bin/$f.new" \
            && mv -f "$HOME/.local/bin/$f.new" "$HOME/.local/bin/$f"
    done
    systemctl --user try-restart herdr-notify 2>/dev/null || true
    say "menu (remote-agents) refreshed"
else
    say "running the TENDRIL installer (press Enter for text questions; type y for every yes/no question)"
    echo
    (cd "$REPO" && ./install)
fi

CONF="$HOME/.config/remote-agents/config"
if [ -f "$CONF" ] && grep -q '^HERDR_BIN=$' "$CONF"; then
    sed -i "s|^HERDR_BIN=\$|HERDR_BIN=$(command -v herdr)|" "$CONF"
    say "pinned HERDR_BIN=$(command -v herdr) in $CONF"
fi
[ -x "$HOME/.local/bin/remote-agents" ] && say "computer side ready" \
    || { say "remote-agents still missing - run on the computer: cd $REPO && ./install"; exit 1; }
HOSTEOF
scp -q "$HOST_SCRIPT" "$ALIAS:.tendril-host-setup.sh"
rm -f "$HOST_SCRIPT"
ssh -t "$ALIAS" "bash ~/.tendril-host-setup.sh '$REPO'; rc=\$?; rm -f ~/.tendril-host-setup.sh; exit \$rc" \
    || { echo "   computer setup failed - see the messages above"; exit 1; }

echo; echo "6) Installing the phone launchers and deep-link helper..."
mkdir -p ~/bin
if scp -q "$ALIAS:$REPO/phone/tendril" "$ALIAS:$REPO/phone/agent" \
        "$ALIAS:$REPO/phone/termux-url-opener" ~/bin/ 2>/dev/null; then
    echo "   copied from $ALIAS:$REPO"
else
    echo "   not found at $ALIAS:~/$REPO - downloading from GitHub instead"
    for f in tendril agent termux-url-opener; do
        curl -fsSL "https://raw.githubusercontent.com/Ozymandes/TENDRIL/main/phone/$f" -o ~/bin/$f
    done
fi
chmod 700 ~/bin/tendril ~/bin/agent ~/bin/termux-url-opener
grep -q 'HOME/bin' ~/.bashrc 2>/dev/null || echo 'export PATH=$HOME/bin:$PATH' >> ~/.bashrc
sed -i '/^export TENDRIL_ALIAS=/d' ~/.bashrc 2>/dev/null || true
echo "export TENDRIL_ALIAS=$ALIAS" >> ~/.bashrc
# Full path to the menu, so it starts even when the login shell's PATH lacks ~/.local/bin
sed -i '/^export REMOTE_AGENTS_BIN=/d' ~/.bashrc 2>/dev/null || true
echo "export REMOTE_AGENTS_BIN='\$HOME/.local/bin/remote-agents'" >> ~/.bashrc

echo; echo "7) Home-screen shortcut (needs the Termux:Widget app)..."
mkdir -p ~/.shortcuts
# Widgets don't read ~/.bashrc, so set the menu path here too (for the SSH fallback)
printf '#!/data/data/com.termux/files/usr/bin/bash\nexport REMOTE_AGENTS_BIN='\''$HOME/.local/bin/remote-agents'\''\nexec ~/bin/tendril %s\n' "$ALIAS" > ~/.shortcuts/tendril
chmod 700 ~/.shortcuts/tendril

echo
echo "Tip: add the Termux:Widget to your home screen and tap 'tendril'."

echo; echo "8) Checking everything..."
verify "$ALIAS"
