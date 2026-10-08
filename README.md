![TENDRIL launch banner](assets/branding/TENDRIL_BANNER.png)

[![Python](https://img.shields.io/badge/Python-stdlib-3776AB?style=for-the-badge&logo=python&logoColor=white)](#host-installation) [![Linux](https://img.shields.io/badge/Linux-host-1F2937?style=for-the-badge&logo=linux&logoColor=F0C94A)](#host-installation) [![macOS](https://img.shields.io/badge/macOS-host%20%2F%20client-8A8A8F?style=for-the-badge&logo=apple&logoColor=white)](docs/MACOS.md) [![Android](https://img.shields.io/badge/Android-Termux-197A46?style=for-the-badge&logo=android&logoColor=white)](docs/ANDROID.md) [![Herdr](https://img.shields.io/badge/Herdr-0.9%2B-8A5CF6?style=for-the-badge)](https://herdr.dev) [![Remote agents](https://img.shields.io/badge/remote-agents-1F1F1F?style=for-the-badge&logo=github&logoColor=white)](#what-it-does) [![Mosh%20%2F%20SSH](https://img.shields.io/badge/Mosh%20%2F%20SSH-private_network-238DB5?style=for-the-badge)](#how-it-works) [![CLI%20%2F%20TUI](https://img.shields.io/badge/CLI%20%2F%20TUI-terminal-16A6A1?style=for-the-badge)](#what-it-does) [![Version tag](https://img.shields.io/badge/version-v0.1.1-3974AD?style=for-the-badge&logo=github&logoColor=white)](https://github.com/Ozymandes/TENDRIL/tree/v0.1.1) [![MIT](https://img.shields.io/badge/licence-MIT-D6A62E?style=for-the-badge&logo=opensourceinitiative&logoColor=white)](LICENSE)

TENDRIL puts a small, direct control surface for persistent AI-agent sessions
in your pocket. From your phone, check the live workspaces on your
workstation (Linux or macOS), move to the session you need, and leave the
host to keep working when you put the phone away.

## Platforms

| Role | Platform | Status |
|---|---|---|
| Host | Linux | ✅ daily use, full test suite |
| Host | macOS (Apple silicon, Intel) | 🟡 Darwin-simulation tests pass; real-Mac sign-off pending |
| Client | Android (Termux) | ✅ real-device certified (launcher, masthead, selector, direct attach/resolve, deep links) |
| Client | macOS terminal | 🟡 launcher tests pass; real-Mac sign-off pending |
| Client | iOS (Blink Shell) | 🟡 source-verified against Blink + host-side tests; real-iPhone sign-off pending |

✅ marks combinations in daily use with the full test suite (Android is
additionally certified on a real S21 Ultra / Termux device). 🟡 means
supported and covered by automated tests (CI runs the suite on Linux and
on a macOS runner), but not yet signed off on a real device. Any host
works with any client: SSH everywhere, Mosh where both ends have it. See [docs/MACOS.md](docs/MACOS.md) and
[docs/IOS_BLINK.md](docs/IOS_BLINK.md).

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

- `tendril` connects from Termux or a desktop to your Linux or macOS host,
  preferring Mosh and falling back to SSH.
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
ANDROID / TERMUX                         LINUX / MACOS HOST
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
Herdr and TENDRIL on the host (no `sudo`), adds the `tendril` and `agent`
commands, a Termux:Widget shortcut and the deep-link helper
(`termux-url-opener`), then checks the whole chain. The
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
  notification watcher keeps running after you log out (Linux/systemd hosts;
  on macOS the watcher is a launchd agent that starts at login —
  [docs/MACOS.md](docs/MACOS.md)).

### Host installation

On the host — Linux or macOS — install Herdr first, then clone TENDRIL and
run its interactive installer. It checks compatibility, previews changes
with `--dry-run`, and never uses `sudo`.

```sh
git clone https://github.com/Ozymandes/TENDRIL.git
cd TENDRIL
./install --doctor
./install
```

On a Mac, enable Remote Login first and expect Homebrew-flavoured hints;
the watcher installs as a launchd LaunchAgent rather than a systemd unit.
Prerequisites, firewall and Mosh quirks, and the Linux/macOS lifecycle
table are in [docs/MACOS.md](docs/MACOS.md). A Mac or Linux desktop can
also be a pure *client* instead of a host: `./install --client` installs a
`tendril` command that dials your host over SSH/Mosh. On either kind of
host, the installer can set up the push watcher as a user service — check
it with `tendril service status`, manage with
`tendril service restart|stop|logs`.

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

| Command | What it does |
|---|---|
| `tendril --upgrade [--verbose]` | self-update the host: fetch + fast-forward the recorded checkout, then a non-interactive refresh; ends in a health check + summary |
| `tendril --version` | installed version and commit (`VERSION` in the repo; `0.2.0-dev` until the next tag) |
| `tendril --help` | the full command list, including the above |

**Upgrading.** `tendril --upgrade` reads the provenance every `./install`
records (`~/.config/remote-agents/install.json`: source checkout, commit,
branch, version, phone-launcher hashes), refuses — one line, nothing
touched — unless that checkout is clean, on the same branch, and in sync
with its upstream (no uncommitted changes, no merge/rebase in progress, no
detached HEAD, no missing upstream, no local or diverged commits), then
runs `git fetch --quiet` + `git merge --ff-only @{u}` (never reset, rebase
or force) followed by the installer's non-interactive `./install --upgrade`
(binaries, entrypoint symlink, PATH block only when missing, idempotent
Herdr-binding merge, watcher unit). `config` and `notify.env` are asserted
byte-identical, so the ntfy topic never rotates here; the watcher restarts
only when its binary or unit changed (or it was not running). The run ends
with non-destructive health checks and a summary block (`host updated |
already current`, `watcher running | restarted | NOT RUNNING`, `android
client current | client refresh recommended` — the exact Termux commands
are printed when the phone launchers changed). Upgrade is flags-only on
purpose: a bare word is a session-token position, and a workspace may
legitimately be labeled `upgrade`. Phone side, see the refresh note in
[docs/ANDROID.md](docs/ANDROID.md).

## Further reading

- [Beginner setup guide](docs/TENDRIL-SETUP-GUIDE.md)
- [Android / Termux setup](docs/ANDROID.md)
- [macOS host & client](docs/MACOS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Security model](docs/SECURITY.md)
- [License](LICENSE)

Run `./install --doctor` for a read-only host check. Run `./uninstall` to remove
TENDRIL's installed files; it asks before removing your configuration.
