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

## Daily use

```
tendril      → workspace menu → number → work → Ctrl+] → menu → close Termux
tendril ssh  → force SSH if Mosh UDP is blocked
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `key-auth check failed` | Tailscale app not connected, or key not installed yet — try `ssh <alias>` interactively |
| mosh connects then dies instantly | host firewall may block UDP 60000-61000 on `tailscale0`; use `tendril ssh` meanwhile |
| menu says "cannot reach Herdr server" | the host Herdr server is down — start it locally (`herdr`) |
| `Alt+←`/`→` does nothing on the phone | Termux on-screen Alt is unreliable — use `Ctrl+B` then `N`/`P` (prefix chords), or tap the extra-keys `CTRL` then a digit (host needs `keys.indexed.tabs = "ctrl"` in `~/.config/herdr/config.toml`) |
| garbled glyphs | `pkg install font-firas-mono` or any Nerd Font, and ensure UTF-8 locale |
