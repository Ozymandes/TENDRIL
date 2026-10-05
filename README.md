![TENDRIL launch banner](assets/branding/TENDRIL_BANNER.png)

[![Python](https://img.shields.io/badge/Python-stdlib-3776AB?style=for-the-badge&logo=python&logoColor=white)](#host-installation) [![Linux](https://img.shields.io/badge/Linux-host-1F2937?style=for-the-badge&logo=linux&logoColor=F0C94A)](#host-installation) [![Android](https://img.shields.io/badge/Android-Termux-197A46?style=for-the-badge&logo=android&logoColor=white)](docs/ANDROID.md) [![Herdr](https://img.shields.io/badge/Herdr-0.9%2B-8A5CF6?style=for-the-badge)](https://herdr.dev) [![Remote agents](https://img.shields.io/badge/remote-agents-1F1F1F?style=for-the-badge&logo=github&logoColor=white)](#what-it-does) [![Mosh%20%2F%20SSH](https://img.shields.io/badge/Mosh%20%2F%20SSH-private_network-238DB5?style=for-the-badge)](#how-it-works) [![CLI%20%2F%20TUI](https://img.shields.io/badge/CLI%20%2F%20TUI-terminal-16A6A1?style=for-the-badge)](#what-it-does) [![Version tag](https://img.shields.io/badge/version-v0.1.1-3974AD?style=for-the-badge&logo=github&logoColor=white)](https://github.com/Ozymandes/TENDRIL/tree/v0.1.1) [![MIT](https://img.shields.io/badge/licence-MIT-D6A62E?style=for-the-badge&logo=opensourceinitiative&logoColor=white)](LICENSE)

TENDRIL puts a small, direct control surface for persistent AI-agent sessions
in your pocket. From Android, check the live workspaces on a Linux workstation,
move to the session you need, and leave the host to keep working when you put
the phone away.

## Why TENDRIL exists

Long-running work deserves a connection that can come and go. I wanted to
check on a living little agent lab from my phone: see what's running, switch
to the right workspace, and steer it without hauling the workstation around.
There is a particular pleasure in doing that over the first cup of Earl Grey,
before the morning has quite started.

I admired the polish of tools like Moshi, then found that some of the features
I most wanted were behind a paywall. I took that as a design prompt. TENDRIL
is my own small, MIT-licensed system, shaped around the balance of beauty,
control, simplicity, and openness I wanted from the start.

## What it does

- `tendril` connects from Termux to your Linux host, preferring Mosh and falling
  back to SSH.
- `remote-agents` presents Herdr's live workspaces in a terminal selector. Pick
  a workspace to attach, open a shell, or view host info.
- Sessions are addressable: `remote-agents attach <id>` (and
  `tendril attach <id>` on the phone) jumps straight to a workspace by
  Herdr workspace id, pane id, number, or exact label — no selector needed.
- `herdr-notify` can watch session state and send an ntfy alert when work
  finishes or an agent needs input.

The agents run in Herdr on the workstation. The phone joins their sessions; it
does not host them. Your work survives a dropped connection, a closed terminal,
and the walk to the kettle.

## How it works

```text
ANDROID / TERMUX                         LINUX WORKSTATION
`tendril` ── Tailscale ── Mosh / SSH ──> Herdr
                                           └─ `remote-agents` TUI
                                                ├─ Pi / Claude Code / Codex
                                                └─ persistent workspaces

                                           `herdr-notify` ── ntfy ──> Android
```

Herdr owns the persistent sessions. TENDRIL supplies the mobile launcher and
workspace switchboard; the optional notifier reports meaningful state changes.
The console and watcher use Herdr's live snapshot, and the host install stays
user-local with Python's standard library.

## Quick start

### Guided setup (recommended for beginners)

[`docs/scripts/tendril-phone-setup.sh`](docs/scripts/tendril-phone-setup.sh)
sets up both sides from the phone. It installs SSH and Mosh in Termux, creates
the phone's key and `~/.ssh/config` alias, copies the key to the host, installs
Herdr and TENDRIL on the host (no `sudo`), adds the `tendril` command and a
Termux:Widget shortcut, then checks the whole chain. The
[setup guide](docs/TENDRIL-SETUP-GUIDE.md) covers every step.

```sh
bash tendril-phone-setup.sh            # first run, and later updates
bash tendril-phone-setup.sh --check    # read-only check
```

Notes:

- Run host steps as your normal user, not root.
- When the host installer runs, press Enter for the text questions, but type
  `y` for every yes/no question. They default to No, and skipping the
  keybinding questions leaves Alt+D unbound.
- Install Termux and Termux:Widget from F-Droid. Termux:Widget is not on
  Google Play.
- On a server or VPS, run `sudo loginctl enable-linger $USER` so the
  notification watcher keeps running after you log out.

### Host installation

On Linux, install Herdr first, then clone TENDRIL and run its interactive
installer. It checks compatibility, previews changes with `--dry-run`, and
never uses `sudo`.

```sh
git clone https://github.com/Ozymandes/TENDRIL.git
cd TENDRIL
./install --doctor
./install
```

To update only the host selector from the checkout root, replace its executable:

```sh
install -m 755 bin/remote-agents "$HOME/.local/bin/remote-agents.new" &&
mv -f "$HOME/.local/bin/remote-agents.new" "$HOME/.local/bin/remote-agents"
```

This changes only `remote-agents`; it leaves host configuration and ntfy
subscriptions alone. Do not rerun the interactive installer just to update the
binary. An already-running Mosh selector keeps using its old process until you
quit and start `tendril` again; see the [Android guide](docs/ANDROID.md) for the
safe exit sequence.

### Android / Termux

Install OpenSSH and Mosh, configure an SSH host alias, and add the phone's key
to the host. The [Android guide](docs/ANDROID.md) walks through pairing and
copies both canonical launchers from the host checkout into `~/bin`.

```sh
pkg update && pkg install openssh mosh
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" -C termux
ssh-copy-id <host-alias>
tendril
```

## Why it feels different

A terminal tool can be beautiful and still be precise. TENDRIL keeps the
interface close to the work: legible at phone width, quick to navigate, and
free of a dashboard layer between you and the sessions. The name fits that
shape. A tendril reaches into a larger living system and gives you a thread
back to it. The engineering stays thin; the tool still gets to have a point
of view.

## Commands and compatibility

`tendril` is the official phone command. Install `phone/tendril` as
`~/bin/tendril`. `phone/agent` remains a real wrapper for existing installs of
`agent`; it is supported for backwards compatibility. The older
`AGENT_ALIAS` setting also continues to work.

In the selector, use ↑/↓ to highlight a workspace and Enter to attach; typing
its number followed by Enter still works, including multi-digit numbers. The
footer actions are `N New`, `I Info`, `? Help`, `R Refresh`, and `Q Quit`; each
runs on a single keypress, without Enter. Enter remains for workspace selection
and text fields. Help pages fit the screen; any key advances or returns.
`N` immediately opens a shell in the selected/focused workspace's directory.
Herdr names it after that directory; there are no name, directory, agent, or
confirmation prompts. Without a usable workspace directory, it uses the
console's current directory.

`Alt+D` detaches back to the menu (tap ALT then D on the phone); `Ctrl+B` then
`D` is the fallback. Detaching leaves workspaces and agents running. The host
console's Ctrl+Home bridge translates `ESC [ 1 ; 5 H` to Alt+D by default for
TTY attachments, including over Mosh or SSH. It does not require `TERMUX_VERSION`
to be forwarded from the phone. Set host-side `TENDRIL_DETACH_BRIDGE=0` to
disable it; non-TTY attachments pass input through unchanged. Inside a
workspace, `Ctrl+B 1..9` jumps to a workspace, `Ctrl+B w` opens the picker, and
`Ctrl+B n`/`p` cycles tabs. Alt+Up is left to Pi for editing steering messages,
not bound to Herdr workspace switching. Run `herdr-notify --test` on the host to send a test
notification. See the [Android guide](docs/ANDROID.md) for setup and troubleshooting.

Sessions are addressable objects. `remote-agents attach|resolve|focus|link <id>`
attach directly and expose machine-readable metadata plus canonical
tendril-link payloads (host + workspace id + label, nothing else) for
notification deep links — see [docs/DEEPLINK.md](docs/DEEPLINK.md). iPhone
via Blink Shell is a first-class client ([docs/IOS_BLINK.md](docs/IOS_BLINK.md)):
Alt+D detach, Mosh hosts with a `tendril` startup command, and the automatic
256-colour palette. Notifications stay deterministic — no LLM, no API cost.

## Further reading

- [Beginner setup guide](docs/TENDRIL-SETUP-GUIDE.md)
- [Android / Termux setup](docs/ANDROID.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Security model](docs/SECURITY.md)
- [License](LICENSE)

Run `./install --doctor` for a read-only host check. Run `./uninstall` to remove
TENDRIL's installed files; it asks before removing your configuration.
