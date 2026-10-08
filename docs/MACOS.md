# macOS setup (host and client)

TENDRIL runs on a Mac in both roles: as the **host** (Herdr and the agents
live there; your phone or another Mac attaches) and as a **client** (a
`tendril` command that dials a TENDRIL host elsewhere). One installer covers
Linux and macOS; it never uses `sudo` and never installs packages.

Status: supported, real-Mac sign-off pending. The macOS paths are covered
by automated Darwin-simulation tests (fake `uname`/`launchctl`/`sysctl` on
PATH, HOMEs with spaces) that run on every change, and CI runs the whole
suite on a macOS runner with the stock `/usr/bin/python3`. Nobody has yet
reported a full run on a physical Mac; the manual checklist is at the end.

## A. Mac as host

### Prerequisites

| What | Why / how |
|---|---|
| Remote Login (sshd) | System Settings → General → Sharing → **Remote Login** must be on. `./install --doctor` reports it as "SSH server". |
| python3 ≥ 3.9 | `xcode-select --install` — the Command Line Tools ship python3 (stdlib only, no pip). On a fresh Mac `/usr/bin/python3` is only a stub until then. |
| Herdr ≥ 0.9.3 | `curl -fsSL https://herdr.dev/install.sh \| sh` (→ `~/.local/bin/herdr`), or `brew install herdr`, or mise. Config and socket stay in `~/.config/herdr` (no `~/Library` paths). |
| Tailscale (optional, recommended) | App Store / standalone app (TENDRIL finds the CLI inside `Tailscale.app` automatically), its optional "Install CLI integration" (`/usr/local/bin/tailscale`), or Homebrew's open-source `tailscaled`. Never required. |
| Mosh (optional, recommended) | `brew install mosh`. `mosh-server` lands in `/opt/homebrew/bin` (Apple silicon) or `/usr/local/bin` (Intel) — invisible to sshd's PATH; step 3b fixes that. |
| Agents | `pi`, `claude`, `codex` are optional; missing ones are skipped in the menus. |
| Not required | sudo, Homebrew (used only for install hints), Full Disk Access. |

### Install

```sh
git clone https://github.com/Ozymandes/TENDRIL.git
cd TENDRIL
./install --doctor      # read-only system check, changes nothing
./install               # interactive; add --dry-run to preview every write
```

Steps: dependency probe (Herdr candidates are protocol-checked against the
running server), a few questions, files into `~/.local/bin` plus
`~/.config/remote-agents/config` (chmod 600), the remote-session PATH block
(3b), optional ntfy push (4), the notification watcher under launchd (5),
and Herdr keybindings (6). `./install --help` lists all modes. Re-running
is idempotent: existing files are backed up (`.bak.<epoch>`), binaries
replaced, the PATH block refreshed in place.

### Remote session PATH (step 3b)

SSH runs remote commands as `zsh -c` (zsh reads only `~/.zshenv`) and the
Mosh path runs `sh -lc` (reads only `~/.profile`); sshd hands them the bare
PATH `/usr/bin:/bin:/usr/sbin:/sbin`. None of that sees `~/.local/bin` or
Homebrew, so `remote-agents` and `mosh-server` would be "command not
found". The installer appends one managed block

```sh
# >>> tendril remote PATH (managed by tendril install) >>>
# ... prepends ~/.local/bin, /opt/homebrew/bin, /usr/local/bin (no dupes)
# <<< tendril remote PATH <<<
```

to `~/.zshenv` (zsh), `~/.profile` (always; the Mosh login shell), and
`~/.bashrc` for bash users — each file backed up first. Check, preview,
apply or undo it at any time:

```sh
python3 bin/tendril_host.py remote-path check|plan|apply|remove
```

If you decline the block, clients must set
`export REMOTE_AGENTS_BIN='$HOME/.local/bin/remote-agents'` (single quotes:
`$HOME` expands on the host), and Mosh needs `mosh --server=/opt/homebrew/bin/mosh-server` (Intel:
`/usr/local/bin/mosh-server`).

### Notification watcher on launchd (step 5)

`herdr-notify` runs as a per-user LaunchAgent:

- plist: `~/Library/LaunchAgents/com.tendril.herdr-notify.plist`
- log: `~/Library/Logs/tendril/herdr-notify.log` (stdout + stderr)
- `RunAtLoad`, and `KeepAlive` only on unsuccessful exit: a *crashed*
  watcher is restarted (5 s throttle); a *hung* one is not — launchd has
  no watchdog. Linux systemd uses `WatchdogSec=60` for exactly that.
- launchd domain: `gui/<uid>` in a desktop login, `user/<uid>` over a bare
  SSH login — picked automatically when installing.

Manage it (on the Mac, with `~/.local/bin` on PATH):

```sh
tendril service status|start|stop|restart|logs
```

`stop` bootouts the agent: the watcher stays off until your next login
(killing the process would not stick — KeepAlive would restart it). After
editing `~/.config/remote-agents/notify.env`, run `tendril service restart`.

### Daily use

From the phone: Android/Termux ([docs/ANDROID.md](ANDROID.md)) or
iPhone/Blink ([docs/IOS_BLINK.md](IOS_BLINK.md)) — same SSH alias, same
commands. Direct attach without the selector from any client:
`tendril attach <id>` (workspace id `w15`, pane id `w15:p1`, workspace
number, or exact label). On the Mac itself, `tendril` (a symlink to
`remote-agents`) is the same console; the `I` info panel shows Tailscale,
load, memory, battery and watcher state.

### Updates and uninstall

```sh
git pull && ./install    # re-running the installer is idempotent
./uninstall              # removes package files only; asks first
```

The uninstaller removes the LaunchAgent, the host binaries, both client
launchers (`tendril` / `tendril-remote`) and the alias file, asks before
removing `~/.config/remote-agents` (your push topic), asks before removing
the managed PATH block, and offers to restore the newest Herdr config
backup. Herdr, Tailscale, Mosh and your agents are untouched.

### Linux vs macOS lifecycle

| | Linux | macOS |
|---|---|---|
| Service manager | systemd `--user` | launchd (LaunchAgent) |
| Unit | `~/.config/systemd/user/herdr-notify.service` | `~/Library/LaunchAgents/com.tendril.herdr-notify.plist` |
| Hung watcher | `WatchdogSec=60` → restarted | not restarted (crashes only) |
| Logs | `journalctl --user -u herdr-notify -f` | `tail -f ~/Library/Logs/tendril/herdr-notify.log` |
| SSH server setup | your sshd (e.g. `systemctl enable --now sshd`) | Remote Login |
| Remote PATH files | `~/.bashrc` + `~/.profile` | `~/.zshenv` (zsh) + `~/.profile` |
| Package hints | pacman / apt / dnf | brew |

### Troubleshooting (host)

| Symptom | Fix |
|---|---|
| doctor: "SSH server NOT LISTENING" | enable Remote Login (prerequisites above) |
| `mosh-server: command not found` over SSH | re-run `./install` (step 3b), or connect with `mosh --server=/opt/homebrew/bin/mosh-server` |
| mosh hangs: "Nothing received from server on UDP port 60001" | the macOS application firewall silently drops mosh UDP 60000-61000 until `mosh-server` is allowed: run `mosh localhost` once at the console and click Allow; repeat after brew upgrades |
| "locale requested by LC_CTYPE=UTF-8 isn't available" (Linux host) | Mac clients with Terminal's "Set locale environment variables on startup" break a Linux mosh-server: `export LANG=en_US.UTF-8 LC_CTYPE=en_US.UTF-8` before connecting; TENDRIL also falls back to SSH automatically |
| installer: "no protocol-compatible herdr client" | install Herdr ≥ 0.9.3 (`~/.local/bin` or brew), or set `HERDR_BIN=` in `~/.config/remote-agents/config`, then re-run |
| watcher STOPPED | `tendril service restart`, then read `~/Library/Logs/tendril/herdr-notify.log`; launchd restarts crashes, not hangs |
| Herdr rejects `ctrl+home` in `config check` | 0.9.3 refuses that key name; the installer probes it in an isolated config and keeps yours valid — the host-side Ctrl+Home bridge works regardless (default on; `TENDRIL_DETACH_BRIDGE=0` disables) |

## B. Mac as client

### Install

```sh
cd TENDRIL
./install --client
```

Asks for the host's SSH alias (pre-filled from `TENDRIL_ALIAS` or a
previous choice), installs the launcher as `~/.local/bin/tendril`, and
writes the default alias to `~/.config/tendril/alias`. Requires `ssh`;
`mosh` is optional (`brew install mosh`) and preferred when present. When
this Mac is *also* a host, the host console keeps the name `tendril` and
the client is installed as `tendril-remote` instead (same for a foreign
`tendril` file). `TENDRIL_ALIAS` overrides the stored alias; the installer
ends with a key-login check and prints `ssh-keygen` / `ssh-copy-id` hints
if it fails.

### SSH setup (once)

`~/.ssh/config`:

```sshconfig
Host home                       # any short alias
    HostName <tailscale-name-or-ip>
    User <you>
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    ServerAliveInterval 30
```

```sh
ssh-keygen -t ed25519           # if you have no key yet
ssh-copy-id home                # afterwards: ssh home must need no password
```

### Daily commands

| Command | Does |
|---|---|
| `tendril` | console on the default host (Mosh preferred, SSH fallback) |
| `tendril <alias>` | another host |
| `tendril ssh <alias>` | force plain SSH, skip Mosh |
| `tendril attach w15` | straight into a session (workspace id, `w15:p1`, number, or label; ids come from the selector or `tendril link` on the host, and an unknown id exits cleanly) |
| `tendril <alias> attach w15` | same, explicit host |
| `tendril [<alias>] resolve\|focus\|link <id>` | JSON / headless focus / canonical link — always plain SSH, no TTY |

Multiple hosts: pass the alias per call, or change the default by editing
`~/.config/tendril/alias` (or `export TENDRIL_ALIAS=<alias>` per shell).

### Terminals and detach keys (client side)

Over SSH/Mosh the host normally sees `TERM=xterm-256color` and no
`COLORTERM`, so every Mac terminal gets the safe 256-colour palette;
iTerm2/Ghostty/WezTerm/kitty advertise truecolor only in local sessions.

| Terminal | Alt+D detach | Ctrl+Home detach |
|---|---|---|
| Apple Terminal | enable "Use Option as Meta key" (Settings → Profiles → Keyboard) | sends nothing |
| iTerm2 | set Left Option = Esc+ | sends nothing |
| Ghostty | `macos-option-as-alt = true` | `Fn+Ctrl+Left` expected (unverified) |
| WezTerm | `send_composed_key_when_left_alt_is_pressed = false` | expected (unverified) |
| kitty | `macos_option_as_alt yes` | expected (unverified) |

`Ctrl+Home` is `Fn+Ctrl+Left` on laptop keyboards and only arrives from
terminals that encode modified Home (Ghostty/WezTerm/kitty); the host
bridge rewrites exactly that sequence to Alt+D. **Ctrl+B then D always
detaches**, on every terminal. `TENDRIL_DETACH_BRIDGE=0` on the host
disables the bridge.

### Troubleshooting (client)

| Symptom | Fix |
|---|---|
| `key-auth check failed for '<alias>'` | Tailscale down or key not installed: `ssh <alias>` once interactively, then `ssh-copy-id <alias>` |
| `mosh: command not found` | `brew install mosh` on the Mac, or use `tendril ssh <alias>` |
| connects, then `remote-agents: command not found` | host-side PATH block missing — re-run `./install` on the host (step 3b) |
| mosh hangs with "Nothing received from server" | host application firewall (see host troubleshooting) |
| wrong default host | edit `~/.config/tendril/alias` or `export TENDRIL_ALIAS=<alias>` |
