# iOS (Blink Shell) setup

TENDRIL is first-class on iPhone through Blink Shell:
iPhone → Blink → Mosh/SSH → Linux host → TENDRIL TUI → Herdr.

Why Blink: it ships a native Mosh client. Sessions suspend when iOS
backgrounds or locks the app and reconnect when you return — even after the
phone reboots. Plain SSH has no keepalive on iOS and dies in the background,
so prefer Mosh hosts.

## 1. Prereqs

- Tailscale installed on iOS (it runs as a system VPN) and the host
  reachable on your tailnet.
- Herdr + TENDRIL installed on the host (see install/).
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

## 3. Hosts: make a tap launch TENDRIL

Create a host in Blink (Hosts → +): your Tailscale hostname, user, and the
key from step 2. Keep Mosh enabled — Blink hosts are Mosh-first. Give the
host the Mosh startup command `tendril` (per-host "startup command";
wording may differ by version): tapping the host starts mosh-server, then
TENDRIL opens. SSH-only hosts work too — an ssh-config `remotecommand` is
also honored.

## 4. Launching

- Open Blink → tap the host → TENDRIL opens. That is the whole flow.
- `ssh://user@host` links still open Blink from iOS Shortcuts; pair that
  with the notification deep links in docs/DEEPLINK.md to jump straight to
  a workspace.

## 5. Keys

Blink's on-screen SmarterKeys row is: cmd, alt, ctrl, esc, tab, arrows,
copy, paste, hideKB. There are no Home/End/PgUp/PgDn keys on it; those need
a hardware keyboard. Inside TENDRIL:

| Action | Keys |
|---|---|
| Detach to the TENDRIL menu | tap ALT then D (Alt+D), or Ctrl+B then D |
| Switch workspace | Ctrl+B 1..9, or ALT+digit (no prefix) |
| Next / previous tab | Ctrl+B n / Ctrl+B p |
| Workspace picker | Ctrl+B w (digits work) |
| Move in the selector | ↑/↓ arrows + Enter, or type a number |
| Commands | N new · P project · S shell · I info · L last · ? help |

There is no Ctrl+Home on the on-screen keyboard: the combo cannot be
produced by tapping, so the default-on Ctrl+Home host bridge never fires
from it. (The bridge is enabled host-side for all TTY attachments;
`TENDRIL_DETACH_BRIDGE=0` disables it. A hardware keyboard that does send
`ESC [ 1 ; 5 H` will detach.) Alt+D is the primary detach on Blink — it
sends the standard `ESC d`.

## 6. Terminal hints (opt-in)

Blink sends nothing over SSH that identifies it, so TENDRIL cannot detect
it host-side. To get Blink-specific notes in the `?` help (instead of the
Termux Ctrl+Home notes), set `TENDRIL_TERMINAL=blink` — either in Blink's
per-host settings/environment (Blink calls this the host's settings;
wording may differ by version) or by prefixing the startup command:

```
TENDRIL_TERMINAL=blink tendril
```

This only changes help text; TENDRIL works normally without it. Remove a
stale value to get the generic notes back.

## 7. Colors

Blink reports `TERM=xterm-256color` and no COLORTERM, so TENDRIL
automatically uses its 256-color palette (chosen to survive mosh and every
transport). Truecolor is only used when a terminal explicitly advertises
`COLORTERM=truecolor` — Blink users get the 256 palette, which is exactly
right here.

## 8. Reconnect behavior

Mosh suspends/archives the session when Blink goes to the background and
reconnects on return — across app switches, locks, and reboots. Plain SSH
sessions die when iOS suspends the app (no keepalive). Prefer Mosh hosts;
keep an SSH host as fallback for networks that block UDP.

## 9. Notifications

To get push notifications and one-tap attach, see docs/DEEPLINK.md.

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

- No host-side Blink detection: Blink sends no distinguishing environment,
  so Blink-aware help is opt-in via `TENDRIL_TERMINAL=blink`.
- No Home/End/PgUp/PgDn on the on-screen keyboard (hardware keyboard only),
  and no on-screen Ctrl+Home (the default-on host bridge is harmless
  there; `TENDRIL_DETACH_BRIDGE=0` disables it).
- `blinkshell://` external-command URLs (x-callback `run`) have been
  removed by Blink upstream as of this research on blinksh/blink master;
  `ssh://user@host` links still work.
- One-tap attach from notifications goes through the iOS Shortcuts path
  (docs/DEEPLINK.md), not a Blink URL scheme.
