<div align="center">

# TENDRIL

*A thin mobile control plane for persistent AI agents.*

**THE EDGE IS YOURS**

![TENDRIL launch banner](assets/branding/TENDRIL_BANNER.png)

[![Python](https://img.shields.io/badge/Python-stdlib-3776AB?style=for-the-badge&logo=python&logoColor=white)](#host-installation) [![Linux](https://img.shields.io/badge/Linux-host-1F2937?style=for-the-badge&logo=linux&logoColor=F0C94A)](#host-installation) [![Android](https://img.shields.io/badge/Android-Termux-197A46?style=for-the-badge&logo=android&logoColor=white)](docs/ANDROID.md) [![Herdr](https://img.shields.io/badge/Herdr-0.9%2B-8A5CF6?style=for-the-badge)](https://herdr.dev) [![Remote agents](https://img.shields.io/badge/remote-agents-1F1F1F?style=for-the-badge&logo=github&logoColor=white)](#daily-workflow) [![Mosh%20%2F%20SSH](https://img.shields.io/badge/Mosh%20%2F%20SSH-private_network-238DB5?style=for-the-badge)](#security) [![CLI%20%2F%20TUI](https://img.shields.io/badge/CLI%20%2F%20TUI-terminal-16A6A1?style=for-the-badge)](#daily-workflow) [![Version tag](https://img.shields.io/badge/version-v0.1.1-3974AD?style=for-the-badge&logo=github&logoColor=white)](https://github.com/Ozymandes/TENDRIL/tree/v0.1.1) [![MIT](https://img.shields.io/badge/licence-MIT-D6A62E?style=for-the-badge&logo=opensourceinitiative&logoColor=white)](LICENSE)

</div>

TENDRIL turns an Android phone into a thin control surface for persistent
AI-agent sessions running on a Linux workstation.

The agents do not run on the phone. Their state does not depend on the
mobile connection. TENDRIL connects over Tailscale and Mosh/SSH into
[Herdr](https://herdr.dev), where Pi, Claude Code and Codex keep working
after the phone disappears. A lightweight watcher notifies Android through
ntfy when an agent finishes — or when it is waiting for you.

**The phone is the control surface. The work stays on the host.**

```
ANDROID                              LINUX HOST

Termux                               Herdr
  │                                    ├─ Pi
  │ tendril                            ├─ Claude
  ▼                                    └─ Codex
Tailscale                               │
  │                                     │
  └───── Mosh / SSH ───────► remote-agents
                                        │
                                   herdr-notify
                                        │
                                       ntfy
                                        │
                                        ▼
                                  Android push
```

## Why TENDRIL exists

Interactive coding agents are long-running processes. A phone connection is
not. TENDRIL separates the two: agents live inside Herdr's persistent panes
on the host, the phone only ever attaches to them. Detach is instant, churn
on the mobile network is irrelevant, and results accumulate whether or not
any device is watching. When an agent needs you — done or blocked — the
watcher taps you on the shoulder.

No tmux. No web dashboard. No paid service. Python stdlib only.

## Quick start

Host (Linux, user-local, no sudo):

```sh
git clone https://github.com/Ozymandes/TENDRIL.git && cd TENDRIL
./install --doctor     # read-only system check
./install              # interactive; backs up everything it touches
```

Android ([full guide](docs/ANDROID.md)): install both canonical launchers from the host checkout; use `tendril` (`agent` remains only for backwards compatibility):

```sh
pkg update && pkg install openssh mosh
ssh-keygen -t ed25519                       # dedicated Termux key
# configure ~/.ssh/config and install the key; see docs/ANDROID.md
ssh-copy-id <alias>
# copy phone/tendril and phone/agent from the host checkout; see docs/ANDROID.md
tendril
```

## Host installation

`./install` is interactive and user-local. It probes Herdr clients and
selects one that is **protocol-compatible with the running server**
(incompatible clients are rejected with reasons), asks which project roots
to scan, writes `~/.config/remote-agents/config`, installs `remote-agents`
and `herdr-notify` into `~/.local/bin`, and optionally:

- configures ntfy push (generates a private topic, writes `notify.env`,
  chmod 600),
- enables `herdr-notify.service` as a **user** systemd unit,
- ensures the detach set `prefix+d`, `alt+d`, `ctrl+]` in Herdr — showing the exact
  proposed change, creating a timestamped backup, validating with
  `herdr config check`, and applying it with `reload-config` (never a
  server restart).

Missing dependencies are reported with the exact package command. The
installer never installs packages, never touches the firewall, and never
runs sudo.

`./install --dry-run` previews every write. `./install --doctor` is a
read-only system check.

## Android setup

See [docs/ANDROID.md](docs/ANDROID.md). The installer prints a personalized
setup block (alias, hostname, username, launcher path) at the end — every
value is filled in for you. The phone generates its own dedicated
Ed25519 key; private keys are never copied between devices.

## Daily workflow

```
tendril      → live workspace menu → number → work
Ctrl+]       → back to the menu (agents keep running)
close Termux → agents keep running
ntfy push    → "Task complete" / "Input required"
tendril      → everything is exactly where you left it
```

`N` new workspace · `P` project launcher · `S` quick shell ·
`I` host status · `L` last workspace · `T` test push · `?` full key reference.

## Mobile Herdr controls

| Keys | Action |
|---|---|
| `Alt+←` / `Alt+→` | previous / next tab (desktop; unreliable in Termux) |
| `Ctrl+B`, then `N` / `P` | previous / next tab (prefix chords — Termux-safe) |
| `Ctrl+B`, then `1..9` | jump directly to tab (Termux-safe) |
| `Ctrl+1..9` | jump directly to tab, no prefix (needs `keys.indexed.tabs = "ctrl"` in the host Herdr config) |
| `Alt+1..9` | jump directly to tab (desktop) |
| `Alt+↑` / `Alt+↓` | previous / next workspace |
| `Ctrl+]` | detach back to TENDRIL (Termux-safe) |
| `Alt+D` | detach (desktop alternative) |
| `Alt+Enter` / `Alt+Shift+Enter` | split pane horizontal / vertical |
| `Alt+Esc` | close pane |
| `Ctrl+Space`, then `c` / `r` / `k` / `?` | new / rename / close tab, native help |

Bindings are read from your Herdr config; the installer only ever *adds*
`ctrl+]` and `alt+d`, never overwrites anything. In Termux, `Alt+` combos
need the extra-keys ALT toggle and can be mangled by the on-screen keyboard
or Mosh's client-side input handling — prefer the `Ctrl`-chord rows
(Volume Down acts as Ctrl).

## Notifications

`herdr-notify` polls Herdr's snapshot every 8 s and pushes only meaningful
state transitions:

| transition | push |
|---|---|
| `working → idle` / `working → done` | Task complete |
| any tracked state → `blocked` | Input required (high) |

Baselines silently on start and after outages, deduplicates per pane, and
never sends prompts or model output. Configure it any time:

```sh
cp ~/.config/remote-agents/notify.env.example ~/.config/remote-agents/notify.env
chmod 600 ~/.config/remote-agents/notify.env    # set NTFY_TOPIC (+ optional token)
systemctl --user restart herdr-notify
```

## Doctor / troubleshooting

```sh
./install --doctor
```

```
TENDRIL // SYSTEM CHECK

Python          OK
Herdr           OK 0.9.1 / protocol 22 (server running)
Tailscale       ACTIVE (100.x.y.z)
SSH             OK
Mosh            OK
Pi              AVAILABLE
Claude          AVAILABLE
Codex           AVAILABLE
Push            ACTIVE
Watcher         RUNNING
```

Read-only — doctor never fixes anything. Common issues: Tailscale app not
connected (`tendril` tells you), Mosh UDP blocked on the network (`tendril ssh`
falls back), "cannot reach Herdr server" (start Herdr on the host).

## Security

- No public SSH. TENDRIL is designed for Tailscale / private networking;
  Mosh UDP is confined to the tailnet interface.
- Dedicated per-device SSH keys. Private keys are never copied; only
  public keys travel (`ssh-copy-id`).
- Disable SSH password auth **after** key auth is verified.
- No root daemon, no setuid bits, user-level installation only.
- ntfy topics are unguessable by default — that is obscurity, not access
  control. Use authenticated ntfy (`NTFY_TOKEN`) or a self-hosted relay for
  stronger guarantees.
- The notifier never transmits prompts, model output, or secrets.

Full model: [docs/SECURITY.md](docs/SECURITY.md).

## Architecture notes

Two stdlib Python programs and one POSIX shell launcher. The console renders
from a single live Herdr snapshot; the watcher polls the same snapshot.
Herdr clients are probed and protocol-verified before use — an incompatible
client (say, an old distro package) is never silently chosen.
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Uninstall

```sh
./uninstall
```

Removes only what the package installed. Restoring the Herdr keybinding
backup is offered explicitly.

## Limitations

- "Agent error" notifications are not possible: Herdr exposes no error
  state (`working`, `blocked`, `done`, `idle`, `unknown` only).
- Tasks that complete entirely between 8-second polls produce no push
  (real agent turns almost always span multiple polls).
- Tested against Herdr 0.9.x with Pi, Claude Code and Codex.

## License

MIT — see [LICENSE](LICENSE).

Copyright (c) 2026 Ozymandes.
