<p align="center">
  <img src="docs/assets/readme/tendril-logo-transparent.png"
       alt="TENDRIL — The Edge Is Yours"
       width="640">
</p>

# TENDRIL

**A handheld control plane for persistent AI agents.**

<p align="center">
  <a href="https://github.com/Ozymandes/TENDRIL/releases/tag/v0.2.4"><img alt="Version v0.2.4" src="https://img.shields.io/badge/version-v0.2.4-CBFD75?style=flat-square&labelColor=111111"></a>
  <a href="https://github.com/Ozymandes/TENDRIL/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/Ozymandes/TENDRIL/ci.yml?branch=main&style=flat-square&label=tests&labelColor=111111"></a>
  <a href="LICENSE"><img alt="MIT licence" src="https://img.shields.io/badge/licence-MIT-CBFD75?style=flat-square&labelColor=111111"></a>
  <a href="#compatibility-v024"><img alt="Host: Linux" src="https://img.shields.io/badge/host-Linux-6FD3DE?style=flat-square&labelColor=111111&logo=linux&logoColor=white"></a>
  <a href="docs/ANDROID.md"><img alt="Phone: Android + Termux" src="https://img.shields.io/badge/phone-Android%20%C2%B7%20Termux-6FD3DE?style=flat-square&labelColor=111111&logo=android&logoColor=white"></a>
  <a href="https://herdr.dev"><img alt="Herdr 0.9.3+" src="https://img.shields.io/badge/runtime-Herdr%200.9.3%2B-F0B54A?style=flat-square&labelColor=111111"></a>
</p>

We’re entering a slightly strange new kind of computing.

Your machine can now keep working after you walk away from it. Agents stay alive. Sessions persist. Subagents branch off. Providers reset. Tasks finish, block, recover and continue while you’re somewhere else entirely.

That changes what “remote access” should mean.

I didn’t want another remote desktop. I didn’t want a miniature chat client pretending to be my terminal. And I definitely didn’t want another platform trying to replace the tools I already liked.

I wanted something much simpler:

**a beautiful little instrument in my pocket that understands what my agent machine is doing.**

TENDRIL is that instrument.

It sits on top of the stack I already wanted to use — Herdr, SSH, Mosh, Tailscale, Termux and the terminal itself — and turns them into one coherent handheld operating surface.

Your workstation stays plugged in, connected and doing the heavy lifting.

You don’t have to.

Take your phone to a meeting. Leave the laptop at home. Get on a train. Walk into a café with nothing but cellular data. Your agents are still working on the machine you trust, and TENDRIL gives you the context to understand what’s happening without hauling the machine around with you.

> **Leave the laptop. Keep the machine.**

---

## The real session, wherever you are

TENDRIL doesn’t recreate your terminal on the phone.

It doesn’t mirror a simplified version of your agent state either.

It takes you back to the real thing.

A notification, the LIVE view or a session card can resolve all the way down to the exact Herdr workspace, tab and pane that produced it. Tap once and TENDRIL hands you back to the actual persistent session over Mosh.

No reconstructed conversation.

No competing mobile copy.

No fake state.

Just the same computation, still alive on the host.

<p align="center">
  <img src="docs/assets/readme/board-05-exact-session-entry.png"
       alt="The TENDRIL selector opening the exact Herdr session from the control plane"
       width="100%">
</p>

---

## The session doesn’t move. You do.

Start an agent at your desk.

Check it twenty minutes later from your phone.

Answer it from the back of a cab.

Come home, open the laptop and carry on from exactly where you left off.

Nothing moved.

Nothing synced between two copies.

Nothing had to be rebuilt.

Your laptop and your phone are simply two entry points into the same persistent Herdr session.

**Same host. Same Herdr. Same session. Same state.**

That distinction is the whole product.

<p align="center">
  <img src="docs/assets/readme/board-06-same-session-anywhere.png"
       alt="The same Herdr session open on a phone and on a laptop"
       width="100%">
</p>

---

## One machine. Four questions.

TENDRIL Link is deliberately small.

It isn’t trying to become a new operating system for AI, a file manager, a cloud agent platform, an IDE and a chat client at the same time.

It answers four questions well.

**HOME — What matters right now?**

What’s alive, what needs you, what just finished, whether the system is healthy, and the one thing actually worth paying attention to.

**LIVE — What is happening right now?**

Every active Herdr session, agent and subagent, with truthful working, idle, failed, completed and needs-input states.

**CAPACITY — How much computation do I have left?**

Provider windows, reset times, pressure, token usage and reported spend across Claude, OpenAI/Codex and Z.AI.

**SCHEDULE — What should happen next?**

Work that can begin at a particular time, at a provider reset, or the moment useful capacity comes back.

That’s the control plane.

Everything else stays where it belongs.

<p align="center">
  <img src="docs/assets/readme/board-01-control-plane.png"
       alt="TENDRIL Link: HOME, LIVE and CAPACITY on a real phone"
       width="100%">
</p>

---

# Why TENDRIL exists

Remote access wasn’t the problem.

We already have excellent tools for that.

SSH gives you a shell. Mosh makes that shell survive dreadful mobile connections. Tailscale makes your machines reachable without turning networking into a weekend project. RustDesk gives you the desktop when you genuinely need a desktop.

And Herdr already gets the most important architectural decision right:

**the agents and their sessions live on your machine.**

TENDRIL is built around that idea, not against it.

Its philosophy is closer to UNIX than to the modern instinct to absorb everything into one enormous platform: keep the primitives small, let good tools keep doing what they’re good at, compose them cleanly, and avoid inventing a second source of truth unless you absolutely have to.

The problem starts one layer above remote access.

Once you’re running several agents across workspaces, providers and subagents, “Can I SSH into the machine?” becomes the least interesting question.

Of course you can.

The useful questions are different.

Which session is actually working?

Which one is waiting for me?

What finished while I was away?

Where did that notification come from?

Which provider is about to run out of room?

When does capacity return?

Did Herdr crash, or did I deliberately stop it?

Can I schedule something now and trust the host to run it later?

And when something really does need me, can I get into the exact pane that produced it without hunting around?

That’s the layer TENDRIL owns.

It watches the machine without pretending to become the machine. It keeps durable state beside the work. It understands just enough about Herdr, sessions, providers, scheduling and recovery to tell you what matters, then gets out of your way when you want the terminal itself.

That narrowness is intentional.

TENDRIL is built for AI-native engineers who already like working this way: persistent agents, terminals, machines they control themselves, Herdr, SSH, Mosh and small composable tools.

If that already sounds like your setup, TENDRIL shouldn’t feel like learning a new platform.

It should feel like your existing machine suddenly grew a very good handheld instrument.

That was the product I wanted for myself.

Not a general-purpose remote access app.

Not “AI from your phone.”

A purpose-built instrument for this weird new reality where the computer keeps thinking after you close the lid and walk away.

<p align="center">
  <img src="docs/assets/readme/board-02-sessions-scheduling.png"
       alt="Sessions, scheduling and live agent detail in TENDRIL Link"
       width="100%">
</p>

---

# Sessions and scheduling

TENDRIL isn’t a chat UI with a terminal buried somewhere behind it.

The terminal is still the real thing.

TENDRIL gives you the operational context around it.

You can schedule work for later, send it into a new or existing session, start it at a fixed time, wait for a provider reset, or let it begin when useful capacity returns.

The task lives on the host.

Your phone doesn’t have to stay awake. It doesn’t even have to stay online.

If the host disappears while a task is running, TENDRIL marks it **interrupted** rather than quietly running it twice later.

That principle runs through the whole system:

**truth before convenience.**

Open any session and TENDRIL shows you the parts that actually help you make a decision: state, model, harness, context usage, output, reported cost where available, and the subagents beneath it.

Then tap **Open session**.

TENDRIL focuses the exact Herdr workspace, tab and pane and attaches you over Mosh.

The pattern is simple:

**observe from a distance; enter only when you need to.**

---

# What TENDRIL gives you

### A live operational picture

Every Herdr workspace, agent and subagent, with honest working, idle, failed, completed and needs-input states.

### Exact session entry

A notification or card doesn’t dump you somewhere vaguely near the right project. It resolves to the pane that produced it.

### Work that can happen without you watching

Schedule prompts for a specific time, a provider reset or the return of capacity. Durable host-side state means your phone doesn’t have to babysit anything.

### Provider awareness

Claude, OpenAI/Codex and Z.AI capacity, reset windows, pressure, token usage and reported spend where the provider exposes it.

### Notifications that actually mean something

Finished work, blocked agents, scheduled tasks and recovery events arrive through your private notification channel. Tap one and TENDRIL takes you back to its source.

### Recovery without fiction

An unreachable host isn’t the same thing as Herdr being down. A process you intentionally stopped isn’t a crash. TENDRIL keeps those distinctions intact instead of flattening them into one generic red warning.

### A machine you can leave behind

This may be the simplest feature, but it’s the one that motivated the entire project.

Your workstation can stay plugged in, connected and running the agents.

You can go somewhere else.

---

# Built around good tools

TENDRIL’s product philosophy is deliberately compositional.

Herdr remains the runtime.

SSH remains SSH.

Mosh remains Mosh.

Tailscale remains the network.

Your terminal remains your terminal.

The host remains authoritative.

TENDRIL adds the missing operational layer between **having access to an agentic machine** and **understanding what that machine is doing**.

That distinction is important because TENDRIL isn’t really competing with SSH, Mosh or RustDesk.

Those tools solve different problems.

A remote desktop gives you pixels.

SSH gives you a shell.

Mosh gives you a resilient shell.

Herdr keeps the agent sessions alive.

TENDRIL gives you **situational awareness of the entire agent system**, then the shortest possible path back into the real underlying tool when you need it.

It doesn’t swallow the stack.

It conducts it.

---

## Three commands

One of the goals of v0.2.1 was to make all of that machinery disappear for the person installing it.

On the host:

```sh
tendril install
```

On the phone:

```sh
tendril install
```

Then on the host:

```sh
tendril --pair
```

Scan the QR.

That’s the product.

Underneath those three commands, TENDRIL handles host setup, its runtime services, private notification identity, one-shot SSH enrollment, host-key pinning, Termux integration, Link configuration and the signed update channel.

Those details still exist.

You just shouldn’t have to perform them by hand.

That’s an important part of the design philosophy too:

complexity is allowed inside the machine. It shouldn’t leak into the ritual of using it.

<sub>Android may ask for two permissions during first pairing: notifications, and permission for TENDRIL Link to run commands through Termux. Grant them and you’re done. On a server, also run `sudo loginctl enable-linger $USER` so the timers keep running after you log out.</sub>

<sub>**First bootstrap** — a completely fresh machine cannot run `tendril install` before the `tendril` command exists. Run the tested bootstrap once (it installs `tendril`, then does exactly `tendril install`): host `curl -fsSL https://raw.githubusercontent.com/Ozymandes/TENDRIL/main/setup.sh | sh` · phone `curl -fsSL https://raw.githubusercontent.com/Ozymandes/TENDRIL/main/phone/setup.sh | sh`. Other paths: [Android guide](docs/ANDROID.md), [step-by-step setup guide](docs/TENDRIL-SETUP-GUIDE.md).</sub>

## What v0.2.1 changed

v0.2.1 is the release where TENDRIL stopped feeling like several clever pieces that happened to work together and started behaving like one product.

Pairing became a one-shot trust flow with QR enrollment and host-key pinning.

Notifications configure themselves.

Android asks for notification and Termux command permissions when they’re actually needed instead of hiding setup requirements inside diagnostics.

The phone installer became idempotent and learned how to repair its own state.

TENDRIL Link became production-signed.

The host became the phone’s update server.

New builds appear as a small lime indicator on HOME, About explains what’s available, and the app can install the verified update in place without losing its pairing.

The operating model improved too.

HOME follows the active session instead of allowing provider pressure to dominate the story.

Recently completed work has its own state.

Capacity discovers providers dynamically, including OpenAI/Codex.

Notifications return you to the exact session that produced them.

Herdr finally scrolls naturally under your finger in Termux.

And because terminal mouse tracking can swallow the usual keyboard gesture, TENDRIL even makes sure you have a reliable keyboard control waiting in the extra-keys row.

A surprising amount of work went into making the final interaction feel simple.

That’s usually how it goes.

The details are in the [changelog](CHANGELOG.md) and the
[release notes](https://github.com/Ozymandes/TENDRIL/releases/tag/v0.2.1).

## The edge is yours

TENDRIL started with a question I couldn’t stop thinking about:

What should an engineer’s computer feel like once the work keeps going after the engineer walks away?

Not in ten years.

Now.

We already have the models.

We already have agents that can work for long stretches.

We already have persistent runtimes like Herdr.

What’s still being invented is the relationship between the engineer and all that ongoing computation.

I don’t think the answer is to cram the entire workstation onto a smaller screen.

I think the more interesting answer is to build better instruments.

Small ones.

Purposeful ones.

Things that tell you exactly enough.

Things that respect the tools underneath them instead of trying to replace them.

Things that let the machine stay powerful and complicated while the interface in your hand stays calm.

That’s what I want TENDRIL to become.

v0.2.1 is still early, and there’s a lot more I want to explore — richer ways of understanding groups of agents, a much more expressive handheld interface, deeper operational views, and what a genuinely beautiful instrument for agentic engineering should feel like as this entire field keeps changing underneath us.

But the principle is already clear.

Don’t replace good tools. Compose them beautifully.

Leave the workstation plugged in.

Let the agents work.

Put the phone in your pocket.

The edge is yours.

---

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

## Compatibility (v0.2.4)

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
