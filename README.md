<p align="center">
  <img src="docs/assets/readme/tendril-logo-transparent.png"
       alt="TENDRIL — The Edge Is Yours"
       width="640">
</p>

<p align="center">
  <strong>A handheld control plane for persistent AI agents.</strong>
</p>

<p align="center">
  <a href="https://github.com/Ozymandes/TENDRIL/releases/tag/v0.2.1"><img alt="Version v0.2.1" src="https://img.shields.io/badge/version-v0.2.1-CBFD75?style=flat-square&labelColor=111111"></a>
  <a href="https://github.com/Ozymandes/TENDRIL/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/Ozymandes/TENDRIL/ci.yml?branch=main&style=flat-square&label=tests&labelColor=111111"></a>
  <a href="LICENSE"><img alt="MIT licence" src="https://img.shields.io/badge/licence-MIT-CBFD75?style=flat-square&labelColor=111111"></a>
  <a href="#compatibility-v021"><img alt="Host: Linux" src="https://img.shields.io/badge/host-Linux-6FD3DE?style=flat-square&labelColor=111111&logo=linux&logoColor=white"></a>
  <a href="docs/ANDROID.md"><img alt="Phone: Android + Termux" src="https://img.shields.io/badge/phone-Android%20%C2%B7%20Termux-6FD3DE?style=flat-square&labelColor=111111&logo=android&logoColor=white"></a>
  <a href="https://herdr.dev"><img alt="Herdr 0.9.3+" src="https://img.shields.io/badge/runtime-Herdr%200.9.3%2B-F0B54A?style=flat-square&labelColor=111111"></a>
</p>

---

Your agents keep running on your machine.

TENDRIL gives you a handheld surface for seeing what they are doing,
knowing what needs you, scheduling what happens next, watching provider
capacity, recovering failures, and entering the exact persistent session
that matters.

It does not replace Herdr, SSH, Mosh, Tailscale or your terminal.

It makes them behave like one coherent system you can hold in your hand.

> SSH gets you into the machine. TENDRIL tells you what the machine is doing.

### From overview to exact context

**TENDRIL does not replace your terminal. It gets you to the right one.**

A notification, a session card or the LIVE view can resolve all the way
down to the exact Herdr workspace, tab and pane.

One tap hands you back to the real session — not a reconstructed mobile
copy of it.

<p align="center">
  <img src="docs/assets/readme/board-05-exact-session-entry.png"
       alt="The TENDRIL selector opening the exact Herdr session from the control plane"
       width="100%">
</p>

### The session doesn't move. You do.

The laptop and phone are two entry points into the same persistent Herdr
session.

The computation stays on the host. TENDRIL gives you secure remote control,
state and exact-session entry without pretending the terminal became a
mobile dashboard.

Same host.
Same Herdr.
Same session.
Same state.

<p align="center">
  <img src="docs/assets/readme/board-06-same-session-anywhere.png"
       alt="The same Herdr session open on a phone and on a laptop"
       width="100%">
</p>

### Four surfaces. One control plane.

**HOME** is system synthesis — what is alive, what needs you, what is under
pressure, and the one action that matters now.

**LIVE** is present computation — every session and subagent, with truthful
working, idle, failed and needs-input states.

**CAPACITY** is available computation — provider windows, reset times,
pressure, tokens and reported spend across Claude, OpenAI/Codex and Z.AI.

**SCHEDULE** is future computation — prompts that live on the host and start
later, at a provider reset, or when capacity returns.

<p align="center">
  <img src="docs/assets/readme/board-01-control-plane.png"
       alt="TENDRIL Link: HOME, LIVE and CAPACITY on a real phone"
       width="100%">
</p>

## Why TENDRIL exists

Starting an agent is easy.

Keeping several long-running agents honest over an entire day is not.

Once work is spread across providers, workspaces and subagents, "just SSH in
and look" stops being a useful operating model.

One session is waiting for input.
Another finished twenty minutes ago.
A provider is near its limit.
A scheduled task is waiting for capacity.
Herdr may be down while the host itself is perfectly reachable.

The primitives were already good.

Herdr keeps the work alive.
SSH and Mosh carry the session.
Tailscale makes the machine reachable.
Termux puts a real terminal on Android.

What was missing was the layer that knows what all of that means.

**TENDRIL is that layer.**

It observes the host, keeps durable state where the work lives, tells you
what needs attention, and gets out of the way when you decide to enter the
real session.

It began as a way to check on a small agent lab from a phone over the
first cup of Earl Grey. Version 0.1 was a launcher and a terminal
selector. It kept growing until it needed an app of its own.

<p align="center">
  <img src="docs/assets/readme/board-02-sessions-scheduling.png"
       alt="TENDRIL Link: scheduling a task, LIVE detail and a session's context, output and cost"
       width="100%">
</p>

## Sessions and scheduling

TENDRIL is not a chat UI. It doesn't put a text box between you and your
agents; it shows you what they are doing and gets you to the terminal
where they're doing it.

**Schedule work for later.** Give a task a title and the exact prompt the
agent will receive, then pick where it lands: a new session in a
workspace, or an agent pane that is already running. It can start at a
set time, at a provider's next reset, or as soon as capacity comes back.
Tasks live on the host, so your phone can be asleep when they start. A
task that was running when the host crashed is marked *interrupted*, never
silently launched twice.

**Inspect a session.** Open any session to see its state, model, how much
of the context window it has used, output tokens, cost where the harness
reports it, and its subagents. Then **Open session** — TENDRIL focuses that
exact workspace, tab and pane and attaches you over Mosh.

The session lives on the host, so both of you are always looking at the
same thing — start a job on the laptop, check it from the phone on the
train, finish it at the desk. Nothing is mirrored or synced; there is one
session and you are simply where it is.

## What it does

**Live operations.**
Every Herdr workspace, agent and subagent, with honest working, idle, failed
and needs-input states; session titles you can rename from the phone; and
one-tap entry into the exact pane.

**Scheduling.**
Future tasks for new or existing sessions, including time, on-reset and
when-capacity-returns triggers. Durable host state survives reboots and a
sleeping phone.

**Capacity.**
Claude, OpenAI/Codex and Z.AI provider state, including available quota
windows, reset times, pressure, token usage and reported spend where the
provider exposes it.

**Notifications.**
Finished work, needs-input events, scheduled-task state and Herdr recovery
arrive through the user's private notification channel. Tapping one opens
the exact session it belongs to.

**Recovery.**
Herdr outage detection, truthful intentional-stop semantics and a verified,
rate-limited revive path.

**Host management.**
QR pairing, health diagnostics, release/update state and direct host
maintenance from the handheld control surface.

<p align="center">
  <img src="docs/assets/readme/board-03-reliability-recovery.png"
       alt="TENDRIL Link: NEEDS YOU, HERDR OFFLINE with Revive Herdr, diagnostics and host pairing"
       width="100%">
</p>

## It tells the truth about the system

A control plane that guesses is worse than none. TENDRIL keeps the
failure states apart, because they need different things from you:

- **Unreachable is not offline.** If your phone can't reach the host,
  that's a network problem. If the host answers but Herdr doesn't, the
  phone says **HERDR OFFLINE** — *host reachable, Herdr stopped 21 s
  ago* — and offers **Revive Herdr**.
- **Stopped on purpose is not crashed.** `tendril herdr stop` sets a
  guard. The guardian stays quiet, and the phone won't offer to undo your
  decision.
- **Recovery is explicit.** A guardian checks Herdr every 30 seconds and
  notifies you once when it goes down and once when it's back. It never
  restarts anything by itself. Revive is your call: one rate-limited
  restart that is only reported as done once Herdr is verifiably serving
  again.
- **Human attention comes first.** **NEEDS YOU** lists every agent
  waiting on input, and how long it has waited. Otherwise HOME shows the
  most recently active session; tapping it opens that exact pane's dossier.
- **Diagnostics show the real boundaries.** Termux, its permission, the
  launcher, the SSH alias, deep links, the notification bridge, the host,
  Herdr, TENDRIL and the notifier — each with its own state, so a broken
  link is named rather than implied.

## Architecture

<p align="center">
  <img src="docs/assets/readme/architecture.png"
       alt="TENDRIL system architecture: TENDRIL Link, secure connection, TENDRIL host control plane, Herdr runtime, model providers"
       width="100%">
</p>

1. **TENDRIL Link** — the phone surface: monitor, schedule, recover,
   inspect, update, enter sessions.
2. **Secure connection** — Tailscale for reachability; Mosh for
   interactive sessions, with SSH as the fallback; plain SSH in BatchMode
   for every background call the app makes.
3. **TENDRIL host** — `remote-agents` and a handful of standard-library
   Python modules: the live snapshot, scheduler, capacity, health
   guardian, notifier, pairing and the phone update channel. Two user
   systemd timers and one watcher service. No database, no daemon you
   have to babysit.
4. **Herdr** — the persistent runtime: workspaces, sessions, agents (Pi,
   Claude Code, Codex) and their subagents.
5. **Model providers** — identities discovered from current/recent host
   activity; authoritative quota metrics appear only where a provider
   adapter can report them.

Three flows connect them: *control* (you direct the agents), *state*
(live sessions and activity flow back), *notify / recovery* (events,
alerts and revives).

**TENDRIL is the control plane. Herdr is the runtime.** The host is the
authority on everything durable; the phone renders what the host reports
and asks it to act. Details in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
and the binding phone ↔ host contract,
[docs/LINK-CONTRACT-0.2.md](docs/LINK-CONTRACT-0.2.md).

A few opinions hold it together:

- **Use the primitives.** If SSH, Mosh, Tailscale, Herdr or Termux already
  do something well, TENDRIL calls them rather than reinventing them.
- **The host is authoritative.** Durable state lives where the work does.
- **Exact context matters.** A notification about pane `w15:p3` should
  land you in `w15:p3`.
- **Failure is a state, not a surprise.** Degraded is something you can
  see, name and fix.

<p align="center">
  <img src="docs/assets/readme/board-04-release-loop.png"
       alt="TENDRIL Link release loop: update available, installed, link established"
       width="100%">
</p>

## Updates

The host is the update server for its phones.

When a newer production-signed TENDRIL Link build is staged, Link notices
it after connecting and marks HOME with a small lime update indicator.

About shows the available build and runs the trusted update flow.

The APK travels over the user's existing host connection, is checked against
its release manifest, SHA-256, package identity and pinned production signer,
then handed to Android's installer.

Android still asks for confirmation.

After installation, Link keeps its pairing and returns to **up to date**.

`tendril --upgrade` remains available from Termux as the CLI fallback.

The mechanics, for the record: the host stages a build with
`tendril --link-stage`, which checks the build's manifest against the APK,
pins its signing key, and refuses a signer change or a downgrade. Link
re-verifies the same bytes — package, version, signer — before handing them
to Android, and a newer build stays marked until it is installed, even if a
later check fails. On the host, `tendril --upgrade` updates TENDRIL itself
by fast-forwarding a clean checkout; your configuration and notification
topic are left byte-for-byte untouched.

## Compatibility (v0.2.1)

| | Supported | Status |
|---|---|---|
| Host — Linux | systemd user session, Herdr 0.9.3+ / current supported runtime, Python 3.9+ (stdlib only), OpenSSH | ✅ Full TENDRIL feature set; physically exercised daily |
| Host — macOS | selector, attach and notifications where the underlying dependencies work | 🟡 Terminal/SSH use is supported; the Linux systemd scheduler/guardian integration is not available in the same form |
| Phone — Android | Android 8+, [Termux from F-Droid](https://f-droid.org/packages/com.termux/) (or a supported classic Termux distribution), TENDRIL Link | ✅ Full handheld experience physically certified end-to-end on the S21: pairing, sessions, exact entry, notifications, scheduling, capacity, recovery and production-signed updates |
| Phone — iPhone / iPad | any terminal client capable of connecting to the host over SSH, with Mosh preferred where available | 🟡 terminal access today; full TENDRIL Link experience coming soon |
| Network | Tailscale recommended; SSH required; Mosh preferred for interactive mobile sessions | ✅ |

**iPhone / iPad.** TENDRIL's terminal interface does not require Android.
From an iPhone or iPad, a terminal client that can reach the host over SSH
(or Mosh, preferred for long-lived mobile sessions) can run:

```sh
tendril
```

The TUI itself runs on the host, so the mobile terminal is simply another
window into the same control plane. Clients with Mosh support are
preferable; plain SSH remains usable. No ntfy setup is required for this:
the private notification integration belongs to the richer asynchronous
TENDRIL Link experience — push notifications, exact-session deep links and
background handheld awareness — not to the terminal.

Full TENDRIL Link support for iOS is planned. Today, iPhone and iPad users
can use the complete terminal-native TENDRIL interface over SSH/Mosh;
native handheld notifications and Link-style deep integration are coming
later. See [docs/IOS_BLINK.md](docs/IOS_BLINK.md) for a worked example
with Blink Shell.

## Quick start

TENDRIL is designed around one memorable command on each side.

**Host**

```sh
tendril install
```

**Phone / Termux**

```sh
tendril install
```

**Then, on the host:**

```sh
tendril --pair
```

Open TENDRIL Link and scan the QR.

Android may ask for two permissions during first pairing:

- notifications
- permission for TENDRIL Link to run commands through Termux

Grant them and you're done.

TENDRIL handles the host runtime, private notification identity, one-shot
SSH enrollment, phone launcher, Link integration and trusted update channel.
On a server, also run `sudo loginctl enable-linger $USER` so the timers
keep running after you log out.

<sub>**First bootstrap** — a completely fresh machine cannot run
`tendril install` before the `tendril` command exists. Run the tested
bootstrap once (it installs `tendril`, then does exactly
`tendril install`): host
`curl -fsSL https://raw.githubusercontent.com/Ozymandes/TENDRIL/main/setup.sh | sh`
· phone
`curl -fsSL https://raw.githubusercontent.com/Ozymandes/TENDRIL/main/phone/setup.sh | sh`.
Other paths: [Android guide](docs/ANDROID.md),
[step-by-step setup guide](docs/TENDRIL-SETUP-GUIDE.md).</sub>

The memorable command after bootstrap is always: `tendril install`.

## Day to day

```sh
tendril                       # the selector (from the phone: Termux or Link → Open Tendril)
tendril enter <id>            # focus + attach an exact workspace or pane (w15, w15:p3)
tendril snapshot --json       # the live workspace/agent mirror the app renders
tendril capacity status       # provider windows, resets and usage as a table
tendril schedule list         # scheduled tasks, newest first
tendril health --json         # Herdr, TENDRIL and service states (no secrets)
tendril herdr restart         # the verified revive
tendril herdr stop | start    # intentional stop (sets the guard) / start (clears it)
tendril install               # establish, repair or verify the supported installation (host or phone)
tendril --pair                # pairing QR + the same code as text for TENDRIL Link
tendril --upgrade             # refresh or upgrade an existing TENDRIL installation (host: TENDRIL itself · phone: launchers + Link)
herdr-notify --test           # send a test notification
```

In the selector, every key is a single press: ↑/↓ and Enter (or a number
and Enter) attach, **N** opens a new shell, **I** shows host info, **R**
refreshes, **Q** quits, **L** returns to TENDRIL Link, and **?** shows the
key reference. Inside a session, **Alt+D** (or **Ctrl+B d**) detaches back
to the selector; your agents keep running.

`tendril --help` lists everything else.

## Security

TENDRIL lives inside the trust you already set up: your tailnet, your SSH
keys, your user account. No root, no inbound ports beyond tailnet SSH and
Mosh, no accounts, no analytics, no TENDRIL cloud.

- **Link can only ask, never command.** It sends a fixed set of verbs to
  `~/bin/tendril` as argument arrays. Every variable argument is
  validated on the phone *and* on the host. There is no field anywhere
  that becomes a shell command.
- **The phone can revive Herdr but can't stop it.** Stop and start are
  host-only.
- **Updates are pinned and confirmed.** Host-staged builds are checked
  against their manifest and signing key, re-verified by the app, and
  installed only after Android asks you.
- **Credentials stay put.** Capacity's network adapters are off until
  you enable them, and they never print or store tokens. `health` and
  `capacity` output carries no secrets.
- **Pairing codes are secrets.** They can carry your read-only
  notification token.

The full model is in [SECURITY.md](SECURITY.md).

## What TENDRIL isn't

TENDRIL is not a cloud agent platform, a chat front end, a remote desktop
or a file manager.

It is not a replacement for SSH, Mosh, Tailscale, Herdr or your terminal.

Your agents still run on your machine, under your account.

TENDRIL is the control layer that tells you what they are doing, what
needs you, and where to go next.

## v0.2.1 — Interaction + Installation Polish

v0.2.1 is the release where TENDRIL stops feeling like a collection of
working pieces and starts behaving like one product.

Pairing is now a one-shot trust flow with QR enrollment and host-key
pinning. Notifications configure themselves. Android asks for permissions
at the moment they are actually needed. The phone installer is idempotent,
and TENDRIL Link now has a production-signed, physically verified in-place
update path.

The operating surface became more truthful too.

HOME follows the active session instead of turning provider pressure into
the main story. Recently completed work gets its own state. Capacity
discovers providers dynamically, including OpenAI/Codex. Herdr sessions
scroll naturally under your finger in Termux, and the phone keeps a
keyboard-recovery control available even while terminal mouse tracking is
active.

The release also tightens exact-session entry, notification routing,
recovery semantics and the install/repair path across the host and phone.

Most importantly, TENDRIL now has a simple product contract:

install → pair → operate → update

The details are in the [changelog](CHANGELOG.md) and the
[release notes](https://github.com/Ozymandes/TENDRIL/releases/tag/v0.2.1).

## Contributing

The host is plain Python standard library and POSIX `sh`. The test suite
needs nothing else:

```sh
python3 -m unittest discover -s tests
```

CI runs it on Ubuntu and macOS. Issues and pull requests are welcome; for
anything touching what the phone may ask the host to do, read
[docs/LINK-CONTRACT-0.2.md](docs/LINK-CONTRACT-0.2.md) first.

More reading: [Android](docs/ANDROID.md) ·
[setup guide](docs/TENDRIL-SETUP-GUIDE.md) · [macOS](docs/MACOS.md) ·
[iOS / Blink](docs/IOS_BLINK.md) · [deep links](docs/DEEPLINK.md) ·
[architecture](docs/ARCHITECTURE.md) · [security](SECURITY.md).
`./install --doctor` checks a host without changing anything;
`./uninstall` removes TENDRIL and asks before touching your configuration.

## Licence

[MIT](LICENSE). The edge is yours.
