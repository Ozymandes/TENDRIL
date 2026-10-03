# Android (Termux) setup

## 1. Install the base

```sh
pkg update
pkg install openssh mosh
```

## 2. Dedicated SSH key (never reuse keys from other apps)

```sh
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" -C "termux"
```

## 3. SSH config — `~/.ssh/config`

```
Host <alias>                     # pick any short name, e.g. "home"
    HostName <tailscale-hostname-or-ip>
    User <your-linux-username>
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

## 4. Install the public key on the host

```sh
ssh-copy-id <alias>      # asks for your password exactly once
ssh <alias>              # must now log in without a password
```

After this works, disable password auth on the host (`PermitRootLogin no`,
`PubkeyAuthentication yes`, `PasswordAuthentication no` in
`/etc/ssh/sshd_config.d/*.conf`, then `sudo sshd -t` and restart sshd).

## 5. Install both canonical launchers

The repo provides two real launchers: `phone/tendril` is the official command;
`phone/agent` is the legacy compatibility wrapper. Copy both from the Linux
host checkout over SSH. Set `HOST` to the SSH config alias from step 3 and
`REPO` to the checkout path on that host. The temporary `tendril -> agent`
symlink is removed before installing the canonical files.

```sh
set -e
HOST=home
REPO=/path/to/tendril
mkdir -p "$HOME/bin" "$HOME/tendril-launchers"
scp "$HOST:$REPO/phone/tendril" "$HOME/tendril-launchers/tendril"
scp "$HOST:$REPO/phone/agent" "$HOME/tendril-launchers/agent"
rm -f "$HOME/bin/tendril" "$HOME/bin/agent"
mv "$HOME/tendril-launchers/tendril" "$HOME/bin/tendril"
mv "$HOME/tendril-launchers/agent" "$HOME/bin/agent"
chmod 700 "$HOME/bin/tendril" "$HOME/bin/agent"
rmdir "$HOME/tendril-launchers"
echo 'export PATH=$HOME/bin:$PATH' >> ~/.bashrc
source ~/.bashrc
```

Verify both resolve to the installed repo launchers:

```sh
command -v tendril
command -v agent
ls -l ~/bin/tendril ~/bin/agent
```

Set your alias once (or pass it every time: `tendril <alias>`;
an existing `AGENT_ALIAS` keeps working):

```sh
echo 'TENDRIL_ALIAS=<alias>' >> ~/.bashrc
```

## 6. Notifications

Install the [ntfy app](https://ntfy.sh), Subscribe → enter the topic from the
host's `~/.config/remote-agents/notify.env`. Test from the console with `T`.

## Detach: Ctrl+Home (Termux) via the TENDRIL bridge

In the extra-keys row, tap CTRL then HOME — the active session detaches and
the TENDRIL selector returns:

```
CTRL + HOME  →  ESC [ 1 ; 5 H  →  TENDRIL bridge  →  Alt+D (ESC d)  →  detach
```

Termux encodes Ctrl+Home as `ESC [ 1 ; 5 H` (xterm modifier encoding —
`KeyHandler.getCode` applies `transformForModifiers` for the HOME key).
Herdr's key grammar has no Home key, so on Termux `remote-agents` runs the
attach behind a small PTY bridge that rewrites exactly those six bytes to the
Alt+D detach Herdr already binds. Everything else is forwarded byte-for-byte:
a plain HOME tap (`ESC [ H`) keeps its normal meaning, Android's system Home
button is not touched, and Alt+D / Ctrl+B d keep working. Herdr's config is
never modified by the bridge. It is a TENDRIL convenience: automatic on
Termux, opt-in elsewhere with `TENDRIL_DETACH_BRIDGE=1`, and `=0` disables it.

Confirm the sequence on the phone (in a plain Termux shell, not inside
Herdr): run `cat -v`, tap CTRL then HOME — you should see `^[[1;5H`.
`Ctrl+C` exits.

## Daily use

```
tendril      → workspace menu → ↑/↓ or number + Enter → work
               → CTRL+HOME (bridge) or Alt+D → menu → close Termux
tendril ssh  → force SSH if Mosh UDP is blocked
```

Tap ALT then D to return to the menu (the bridge makes CTRL+HOME do the same
on Termux). If your keyboard does not send Alt+D correctly, use Ctrl+B then
D. Detaching keeps workspaces and agents running.
`N` asks for an optional name and defaults to the selected workspace's
directory — Enter accepts it, an absolute path overrides it, an empty name
keeps the directory-based label. `S` opens a shell in that directory too.
The `P` project picker is unchanged.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `key-auth check failed` | Tailscale app not connected, or key not installed yet — try `ssh <alias>` interactively |
| mosh connects then dies instantly | host firewall may block UDP 60000-61000 on `tailscale0`; use `tendril ssh` meanwhile |
| menu says "cannot reach Herdr server" | the host Herdr server is down — start it locally (`herdr`) |
| `Alt+←`/`→` does nothing on the phone | Termux sends arrows with an ESC prefix, which Herdr ignores by design — for tabs use `Ctrl+B` then `N`/`P`; for workspaces tap `ALT` then a digit (no prefix), or `Ctrl+B w` + digit |
| `Alt+D` does not detach | Tap ALT then D; if the keyboard encoding or a custom binding prevents it, use Ctrl+B then D. The installer adds these bindings only when conflict-free. |
| `Ctrl+Home` does not detach | On Termux the TENDRIL bridge rewrites `ESC [ 1 ; 5 H` to Alt+D — check `cat -v` shows `^[[1;5H` for the combo, and that you attached from `remote-agents` (the bridge lives there). `TENDRIL_DETACH_BRIDGE=0` turns the bridge off; then use Alt+D. Herdr itself rejects `ctrl+home`, so the installer only adds it natively on versions that validate it. |
| garbled glyphs | `pkg install font-firas-mono` or any Nerd Font, and ensure UTF-8 locale |
