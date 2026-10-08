# iOS (Blink Shell) setup

TENDRIL is first-class on iPhone through Blink Shell:
iPhone → Blink → Mosh/SSH → host (Linux or macOS) → TENDRIL TUI → Herdr.

Why Blink: it ships a native Mosh client. Sessions suspend when iOS
backgrounds or locks the app and reconnect when you return — even after the
phone reboots. Plain SSH has no keepalive on iOS and dies in the background,
so prefer Mosh hosts.

## Certification status

Re-certified against Blink's sources (branch `raw`) and TENDRIL's test
suite. No claim in this document rests on real-device validation.

| Category | Meaning | Examples in this doc |
|---|---|---|
| SOURCE VERIFIED | read in Blink/ntfy/Apple sources | Mosh client + per-host startup command, Alt+D = `ESC d`, reconnect machinery, `ssh://` handling |
| AUTOMATED TESTED | a TENDRIL test exercises it (host side) | palette choice, selector keys, Ctrl+Home bridge + resize, deep-link payload, stale ids |
| REAL DEVICE REQUIRED | mechanism verified; end-to-end needs an iPhone | two-tap ALT+D feel, rotation, suspend/resume, ntfy → Shortcut flow |
| BLOCKED BY BLINK | Blink provides no facility | opening an existing session or saved host via URL/Intent |

Automated coverage: `tests/test_terminal_caps.py`, `tests/test_remote_agents.py`
(KeyReader/Panels/Selection/CtrlHomeBridge), `tests/test_tendril_link.py`,
`tests/test_attach.py`, `tests/test_herdr_bindings.py`.

## 1. Prereqs

- Tailscale installed on iOS (it runs as a system VPN) and the host
  reachable on your tailnet.
- Herdr ≥ 0.9.3 + TENDRIL installed on the host (`./install`; macOS hosts:
  [docs/MACOS.md](MACOS.md)).
- Blink Shell from the App Store. Blink calls its saved connections
  "hosts" and its key manager the "key chain" (wording may differ by
  version).

## 2. Key and first login

Create a dedicated key inside Blink (never reuse keys from other apps):

```sh
ssh-keygen -t ed25519 -N "" -C "blink-ios"
```

Authorize it on the host — from Blink:

```sh
ssh-copy-id <your-user>@<tailscale-hostname-or-ip>
```

or paste the public key into the host's `~/.ssh/authorized_keys` (same as
the installer's Android key block). Then test plain SSH:

```sh
ssh <your-user>@<host>      # must log in without a password
```

## 3. Hosts: saved connections that run TENDRIL

Create a host in Blink (Settings → Hosts → +): alias, hostname (your
Tailscale name), user, port, key from step 2. Blink hosts are plain saved
fields that feed a `mosh`/`ssh` command — there is no "tap to connect" list
in current versions; you connect by typing (or autocompleting) the alias in
a Blink shell:

    mosh <alias>            # what you want: survives background/roaming
    ssh <alias>             # fallback for UDP-blocked networks

Give the host its **Mosh startup command** `tendril` (stored per host; the
UI also labels the mosh-server path "Server"). Blink runs that command as
the Mosh remote command (`mosh … -- tendril`), so connecting lands straight
in TENDRIL. `TENDRIL_TERMINAL=blink tendril` works as the startup command
too (section 6). SSH-only hosts can get the same effect with an attached
ssh-config `remotecommand` (Blink parses `remotecommand` from host ssh
config).

*Verified in Blink source (branch `raw`): saved-host fields
`BlinkConfig/BKHosts.h` (`moshServer`, `moshStartup`, …); startup command
consumed at `Sessions/MoshSession.m` (host.moshStartup → mosh-server
`--` command); `remotecommand` at `BlinkConfig/BKSSHHost.swift`.

## 4. Launching

Open Blink, type the host alias (e.g. `mosh home`), Enter — mosh-server
starts and TENDRIL opens. `ssh://user@host` links still open Blink from iOS
Shortcuts, but they always start a *new* ssh shell; they cannot re-open a
running Mosh session. Pair them with the notification deep links in
docs/DEEPLINK.md to focus the right workspace before you connect.

## 5. Keys

The on-screen Smart Keys row carries esc, ctrl, alt, cmd, tab, arrows,
copy/paste, hideKB plus a scrollable area. Per Blink's docs the arrows gain
Home/End/PgUp/PgDn with CMD, and modifiers can be chained — but what exact
bytes a chained on-screen Ctrl+Home emits is **not device-verified**; treat
Ctrl+Home as hardware-keyboard-only until tested. Inside TENDRIL:

| Action | Keys |
|---|---|
| Detach to the TENDRIL menu | tap ALT then D (Alt+D), or Ctrl+B then D |
| Switch workspace | Ctrl+B 1..9, or ALT+digit (no prefix) |
| Next / previous tab | Ctrl+B n / Ctrl+B p |
| Workspace picker | Ctrl+B w (digits work) |
| Move in the selector | ↑/↓ arrows + Enter, or type a number |
| Commands | N new shell · I info · R refresh · Q quit · ? help |

Alt+D is the primary detach on Blink: Blink's hterm sends Alt+key as the
standard `ESC d` (alt-sends-what=escape), which is exactly Herdr's detach
key. The default-on Ctrl+Home host bridge stays harmless: it rewrites only
`ESC [ 1 ; 5 H`, and a hardware keyboard that emits that sequence (hterm
does for Ctrl+Home) will detach. `TENDRIL_DETACH_BRIDGE=0` disables it.

*Source-verified: hterm alt/modifier handling (vendored hterm + chromium
hterm keyboard keymap: Home = `ESC[H`, modified CSI keys gain `;5`);
Blink Smart Keys layout (`Blink/SmarterKeys/KBLayout.swift`).

## 6. Terminal hints (opt-in)

Blink sends nothing over SSH/Mosh that identifies it — no COLORTERM, no
LC_TERMINAL, no TERM_PROGRAM; it just sets `TERM=xterm-256color`
(Blink `AppDelegate.m`, and the SSH PTY requests `xterm-256color`). So
TENDRIL cannot detect it host-side. To get Blink-specific notes in the `?`
help, set `TENDRIL_TERMINAL=blink` — in the host startup command:

    TENDRIL_TERMINAL=blink tendril

This only changes help text; TENDRIL works normally without it. Remove a
stale value to get the generic notes back.

## 7. Colors

Blink reports `TERM=xterm-256color` and never sets `COLORTERM`, so TENDRIL
uses its 256-color palette (chosen to survive mosh, including mosh < 1.4
which drops truecolor sequences). Blink's renderer (hterm) could show
24-bit color, but since Blink never advertises it, the 256 palette is
exactly right. `NO_COLOR` still disables color host-side.

## 8. Reconnect behavior

When iOS backgrounds or locks Blink, sessions suspend and Mosh reconnects
on return — Blink persists Mosh session state and restores it even after an
app restart or phone reboot. Plain SSH dies in the background (no
keepalive). Prefer Mosh hosts; keep an SSH host as UDP fallback. Blink's
optional `geo` toggle (background location) can keep the app alive longer;
TENDRIL does not require it.

*Source-verified: session suspend/restore (`AppDelegate.m` suspend
manager, `MCPSession.m` mosh-state restore, `SceneDelegate.swift`
resume-on-activate). Actual suspend/resume feel needs a real device.

## 9. Notifications

Push + one-tap focus go through the iOS Shortcuts path (docs/DEEPLINK.md):
ntfy tap → Shortcut runs `remote-agents focus <id>` over SSH → open Blink,
connect to the Mosh host, Enter in the selector. Blink itself offers
nothing better today: its only URL entries are `ssh://`, `vscode://` and
`http(s)://` (each starts a *new* shell command); the old
`blinkshell://run` x-callback was removed upstream (commit 796a878,
2026-04) and no Shortcuts-app actions exist in current Blink.

## Troubleshooting

| Symptom | Fix |
|---|---|
| mosh connects then dies instantly | the host firewall may block the mosh UDP range on `tailscale0`; use an SSH host meanwhile |
| `key-auth check failed` | Tailscale not connected on iOS, or the key is not installed — run `ssh <host>` interactively from Blink to see the real error |
| `?` help shows Ctrl+Home notes | that is the generic/Termux block; set `TENDRIL_TERMINAL=blink` (section 6) |
| help still shows Blink notes after removing the env | a stale `TENDRIL_TERMINAL` value is still set in the host's environment or startup command — clear it and reconnect |
| `Alt+D` does not detach | make sure you tapped the ALT modifier key (or use a hardware keyboard); Ctrl+B then D is the fallback |
| menu says "cannot reach Herdr server" | the host Herdr server is down — start it on the host |

## Limitations (honest)

- No host-side Blink detection (Blink sends no distinguishing env);
  Blink-aware help is opt-in via `TENDRIL_TERMINAL=blink`.
- Ctrl+Home: provable with a hardware keyboard (`ESC [ 1 ; 5 H`); on-screen
  producibility via chained Smart-Key modifiers is unverified — needs a
  real-device test. The host bridge is default-on and harmless either way.
- No Blink URL/Intent opens a specific session or host; one-tap attach from
  notifications must go through iOS Shortcuts + SSH `focus` (DEEPLINK.md),
  with a manual final step in Blink.
- `blinkshell://run` x-callback: removed upstream (commit 796a878, 2026-04);
  `blinkshell://` is still a registered scheme but dead — links to it
  silently do nothing. `ssh://user@host` opens Blink with a fresh ssh
  command only; it cannot attach to a running Mosh session.
- End-to-end suspend/resume, rotation, Smart-Key combos and the Shortcut
  flow have never been run on a physical iPhone in this repo; the host-side
  pieces are covered by automated tests (see tests/test_terminal_caps.py,
  tests/test_remote_agents.py::CtrlHomeBridge, tests/test_tendril_link.py).
