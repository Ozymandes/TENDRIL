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
host's `~/.config/remote-agents/notify.env`. Send a test from the host with
`herdr-notify --test`.

## Detach: Ctrl+Home through the host bridge

When a workspace is attached from `remote-agents` in a TTY, the host enables a
small PTY bridge by default. It rewrites only `ESC [ 1 ; 5 H` (the modified-Home
sequence) to Herdr's Alt+D detach key (`ESC d`):

```
Ctrl+Home  →  ESC [ 1 ; 5 H  →  host bridge  →  Alt+D (ESC d)  →  detach
```

This default applies over Mosh and SSH and does not depend on `TERMUX_VERSION`
being forwarded from Termux. Plain Home (`ESC [ H`) and other input pass through
unchanged; non-TTY attachments use direct passthrough. The bridge does not
change Herdr's config or require a phone-launcher update. Set
`TENDRIL_DETACH_BRIDGE=0` in the host environment to disable it. If Herdr's
Alt+D binding has been customized, the translated key may no longer detach;
use Ctrl+B then D or restore the Alt+D detach binding.

To check what your Termux keyboard sends, run `cat -v` in a plain Termux shell
(not inside Herdr), then tap CTRL followed by HOME. The bridge recognizes
`^[[1;5H`; `Ctrl+C` exits.

## Daily use

```
tendril      → workspace menu → ↑/↓ or number + Enter → work
               → Ctrl+Home (bridge) or Alt+D → menu → close Termux
tendril ssh  → force SSH if Mosh UDP is blocked
```

The selector footer is `N New`, `I Info`, `? Help`, `R Refresh`, `Q Quit`.
Press a footer key once; no Enter is needed. Enter remains for workspace
selection and text fields. Info returns on any key; Help pages fit the screen,
with any key advancing to the next page or returning from the last. `N` opens the
workspace-creation chooser, including a shell-only option. Existing behavior
that defaults to the selected/focused workspace's directory remains; an
explicit absolute path overrides it, and the directory chooser remains the
fallback when there is no usable current directory. Detaching keeps workspaces
and agents running.

## Updating the host selector

From the TENDRIL checkout root on the Linux host, update only the selector
executable:

```sh
install -m 755 bin/remote-agents "$HOME/.local/bin/remote-agents.new" &&
mv -f "$HOME/.local/bin/remote-agents.new" "$HOME/.local/bin/remote-agents"
```

This preserves host configuration and the ntfy topic/subscription settings.
Do not run the interactive installer merely to update the binary; no phone
launcher update is needed for this change. Replacing an installed file does not
replace a `remote-agents` process already running under Mosh. Detach from the
workspace normally (Alt+D, Ctrl+B then D as fallback), then quit the selector:
older selectors need `Q` then Enter; the updated selector quits with `Q` alone.
Run `tendril` again to start the updated selector; reconnecting Mosh alone is
not enough.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `key-auth check failed` | Tailscale app not connected, or key not installed yet — try `ssh <alias>` interactively |
| mosh connects then dies instantly | host firewall may block UDP 60000-61000 on `tailscale0`; use `tendril ssh` meanwhile |
| menu says "cannot reach Herdr server" | the host Herdr server is down — start it locally (`herdr`) |
| `Alt+←`/`→` does nothing on the phone | Termux sends arrows with an ESC prefix, which Herdr ignores by design — for tabs use `Ctrl+B` then `N`/`P`; for workspaces tap `ALT` then a digit (no prefix), or `Ctrl+B w` + digit |
| `Alt+D` does not detach | Tap ALT then D; a custom Alt+D Herdr binding can replace detach. Use Ctrl+B then D or restore the Alt+D detach binding. The installer adds bindings only when conflict-free. |
| `Ctrl+Home` does not detach | Confirm you attached through `remote-agents` in a TTY and check the sequence with `cat -v` (`^[[1;5H`). The host bridge is on by default; it needs no forwarded `TERMUX_VERSION`. Host-side `TENDRIL_DETACH_BRIDGE=0` disables it. If Alt+D is custom-bound, the translated key may not detach; use Ctrl+B then D or restore Alt+D. |
| garbled glyphs | `pkg install font-firas-mono` or any Nerd Font, and ensure UTF-8 locale |
