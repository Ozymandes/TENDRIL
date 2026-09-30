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

## 5. Install the launcher

Copy `phone/tendril` from this repo to Termux (`agent`, the old name, stays
as an alias):

```sh
mkdir -p ~/bin
cp /path/to/tendril/phone/tendril ~/bin/tendril
chmod +x ~/bin/tendril && ln -sf tendril ~/bin/agent
echo 'export PATH=$HOME/bin:$PATH' >> ~/.bashrc && source ~/.bashrc
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
