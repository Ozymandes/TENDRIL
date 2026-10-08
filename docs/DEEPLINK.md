# Deep links: tap a notification, land in the session

A TENDRIL session is an addressable object. The canonical id is the Herdr
`workspace_id` (`w15`; a pane id `w15:p1` resolves to its parent workspace).
A deep link carries exactly three facts — host, workspace id, optional
human label — and nothing else (never keys, tokens, topics or commands).
Serialization is centralized in `bin/tendril_link.py` (installed as
`~/.local/bin/tendril_link.py` next to `remote-agents`):

```
tendril_link.py encode home w15 my-proj
  uri:         tendril://host/home/workspace/w15?label=my-proj
  payload_b64: eyJoIjoiaG9tZSIsImwiOiJteS1wcm9qIiwidiI6MSwidyI6IncxNSJ9
               (= {"h":"home","l":"my-proj","v":1,"w":"w15"})
```

Decoding is strict: charset and length checks on host/id, unknown fields
rejected, oversized input rejected, labels cleaned (control chars stripped,
whitespace collapsed, ≤120 chars). A payload is safe to log and safe to
treat as data — not code — on the far side.

## Canonical deep-link contract (v1 — frozen)

The actionable link TENDRIL notifications carry — and the only shape
TENDRIL Link (Android) needs to claim — is the URI form:

```
tendril://host/<host>/workspace/<workspace-id>[?label=<label>]
```

Example (illustrative only — workspace ids are never assumed; use a real
canonical id from `tendril link` or the selector):
`tendril://host/omarchy/workspace/w16`

`bin/tendril_link.py` is the single definition of this contract. The rules
below are frozen; do not re-implement or extend them on either side:

| aspect | contract |
|---|---|
| scheme + netloc | `tendril://host/…` — the netloc is the literal string `host`; anything else is malformed |
| host syntax | `^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$` — Tailscale MagicDNS names, nodenames, ssh aliases |
| workspace syntax | `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`; a pane id (`w15:p1`) addresses its parent workspace |
| query | `?label=<label>` only — any other parameter is rejected |
| normalization | labels: NFC, control chars stripped, whitespace collapsed, capped at 120 chars; URI components percent-encoded; hosts and ids are never transformed |
| malformed link | reject, never sanitize: a clean non-zero exit (`link error: …`, exit 2) on the host; unknown fields, wrong payload versions, oversized payloads and non-canonical encodings are all rejected |
| stale workspace | `attach` / `focus` / `link` answer `not found` (exit 2) and print the closest live ids; a notification whose workspace vanished from the snapshot pushes WITHOUT Click/Actions headers |
| supported actions | exactly two, both resolved on the host against live Herdr state: `attach` (focus + interactive attach) and `focus` (focus only). No other actions, no arbitrary commands, no shell payloads — the link carries no executable content |
| security boundary | the payload whitelist is `{v,h,w,l}` (version, host, workspace id, label); links are data, never code; the ntfy topic remains the credential (docs/SECURITY.md) |

Backwards compatibility: the payload form (`{payload_b64}`) and the id-only
templates (`{workspace_id}`) remain valid; the URI is the canonical,
human-inspectable form. New integrations should build on the URI.

## Host CLI (`remote-agents <cmd> <token>`)

Machine interface: non-interactive, safe over plain SSH. Token resolution
order: exact workspace id → pane id (parent workspace) → pure digits
(workspace number) → exact unique label.

| command | behavior | exit codes |
|---|---|---|
| `attach <token>` | resolve, print `found session '<label>' (<id>)`, then the same interactive attach the selector uses (`herdr workspace focus` + `herdr session attach`; the default-on Ctrl+Home host bridge applies, `TENDRIL_DETACH_BRIDGE=0` opts out) | 0 ok · 1 Herdr unreachable · 2 unknown/stale token or usage · 3 ambiguous |
| `resolve <token>` | one-line JSON: `host, workspace_id, label, number, cwd, agent, agent_status, agents[], focused, link, payload_b64, herdr_status` | same |
| `focus <token>` | identical JSON plus `herdr workspace focus <id>` — no session attach; built for SSH one-shots (iOS Shortcut) | same |
| `link <token>` | prints the `tendril://` URI only | same |

Unknown/stale tokens print the closest matching ids (exit 2); a label shared
by several workspaces prints the candidates (exit 3).

## Phone CLI

`tendril [alias] attach <id>` — or `tendril attach <id>` using
`$TENDRIL_ALIAS`. Accepts the same tokens; validates `A-Za-z0-9._:-`
(max 128) on the phone and single-quotes the id into the mosh/ssh remote
command, so it is injection-proof.

## Notification plumbing

`notify.env` (copy `config/notify.env.example`): `NTFY_CLICK_TEMPLATE` is
rendered per push with these placeholders (unknown → empty; a workspace that
vanished from the snapshot pushes without Click/Actions headers):

| placeholder | value |
|---|---|
| `{host}` `{workspace_id}` `{label}` | raw link facts |
| `{label_uri}` | label percent-encoded (comma-safe in ntfy Actions) |
| `{payload_b64}` | canonical tendril-link payload |
| `{uri}` | `tendril://host/<h>/workspace/<w>[?label=…]` |

With `NTFY_ACTIONS=1` (default) herdr-notify also sends the ntfy short-form
Actions header: `view, Attach, <url>, clear=true`. Do not use Markdown — it
is ntfy web-app only.

To put the canonical deep link itself on every notification — the form
TENDRIL Link claims on Android once installed:

```
NTFY_CLICK_TEMPLATE={uri}
```

If TENDRIL Link (or any handler for `tendril://`) is not installed, nothing
breaks: the notification still displays; a tap just has no registered
handler. Rotation of the notification topic is always explicit — see the
installer flow in `./install` step 4.

## iOS: tap → Shortcut → focused session in Blink

```
NTFY_CLICK_TEMPLATE=shortcuts://run-shortcut?name=TENDRIL%20Attach&input=text&text={workspace_id}
```

ntfy iOS executes `UIApplication.open(<click url>)` on tap, so custom
schemes work. Create a Shortcuts shortcut named exactly `TENDRIL Attach`:

1. It receives the workspace id as shortcut input (text).
2. **Run Script Over SSH** — Host: your host alias, User, SSH-key auth. In
   the Script field type `remote-agents focus '` then insert the variable
   **Shortcut Input**, then `'`. (Single-quoted; the id charset makes this
   injection-proof.) This focuses the session on the host — exit 0.
3. **Text** action: `tendril attach '` + **Shortcut Input** + `'` →
   **Copy to Clipboard**.
4. **Open App** → Blink Shell.
5. Tap your Mosh host (Blink host config: startup command `tendril`) → the
   selector highlights the focused workspace → Enter. Or paste the
   clipboard command into any open session.

Status: every mechanism here is verified in current sources (ntfy-ios opens
the Click URL; `shortcuts://run-shortcut?name=…&input=text&text=…` is
Apple's documented form; Run Script Over SSH and Copy to Clipboard are
built-in; Blink still handles `ssh://` links but `blinkshell://run`
x-callback was REMOVED on Blink master — do not build on it). The
end-to-end flow needs a real device — test once and adjust the SSH action
to your host alias.

## Android

Honest state: **Termux registers no URL schemes and ACTION_VIEW does not
reach it**, so a bare notification tap cannot open Termux. Three real flows:

1. **Zero extra apps (manual).** The id is visible in the notification:
   title `TENDRIL · <label>`; to append the id to the body, point
   `NTFY_SUMMARIZER_BIN` at a one-liner (it receives the workspace JSON on
   stdin; its single output line is appended to the body as ` — <detail>`):
   ```sh
   #!/bin/sh  # ~/bin/print-id — surface the workspace id in the body
   sed -n 's/.*"workspace_id":"\([A-Za-z0-9._:-]*\)".*/\1/p'
   ```
   Open Termux → `tendril attach <id>`. `<id>` must be a live canonical
   workspace id (`w15` here is an example): read it off the selector
   (number + Enter shows it), print one on the host with `tendril link`,
   or check one with `tendril resolve <id>` — an unknown id exits with a
   clean "not found".

2. **Share → termux-url-opener (verified Termux contract).** Termux runs
   `~/bin/termux-url-opener <url>` for any URL *shared* to it
   (ACTION_SEND + EXTRA_TEXT matching Android's WEB_URL pattern). Install
   `phone/termux-url-opener` (next section) and share any
   `https://…/.tendril/<id>` URL to Termux.

3. **Automated one-tap (MacroDroid free, or Tasker).** ntfy Android itself
   can do neither RUN_COMMAND (declares no `com.termux.permission.RUN_COMMAND`)
   nor start a Service from its broadcast — hence an automation app:
   - ntfy app → enable **Broadcast messages** (settings). Each message is
     broadcast as `io.heckel.ntfy.MESSAGE_RECEIVED` with extras
     `id, topic, title, message, click, …`.
   - MacroDroid trigger: Intent Received / Broadcast — action
     `io.heckel.ntfy.MESSAGE_RECEIVED`.
   - **Option A — SEND intent to Termux (no special permission; lands in
     termux-url-opener):**
     ```
     Action:  android.intent.action.SEND
     Target:  package com.termux
              class  com.termux.app.api.file.FileShareReceiverActivity
     Type:    text/plain
     Extra:   android.intent.extra.TEXT = <broadcast extra `click`>
     ```
   - **Option B — RUN_COMMAND (needs a one-time grant):** Termux →
     `~/.termux/termux.properties` → `allow-external-apps=true`, restart
     Termux; grant MacroDroid the `com.termux.permission.RUN_COMMAND`
     permission (protectionLevel dangerous, user-grantable).
     ```
     Action:  com.termux.RUN_COMMAND
     Target:  package com.termux
              class  com.termux.app.RunCommandService
     Extras:  RUN_COMMAND_PATH     = /data/data/com.termux/files/home/bin/tendril
              RUN_COMMAND_ARGUMENTS = ["attach", "<id from the click extra>"]
              RUN_COMMAND_WORKDIR   = /data/data/com.termux/files/home
              RUN_COMMAND_BACKGROUND = true
     ```
     (`<id>`: last path segment of a `…/.tendril/<id>` click URL, or the
     value of its `?id=`/`?p=` pair.)

## Install `phone/termux-url-opener` (Android)

On the phone, in Termux (`HOST` = ssh alias of your host, `REPO` = checkout
path on it — see docs/ANDROID.md step 5):

```sh
set -e
HOST=home
REPO=/path/to/tendril
mkdir -p "$HOME/bin"
scp "$HOST:$REPO/phone/termux-url-opener" "$HOME/bin/termux-url-opener"
chmod 700 "$HOME/bin/termux-url-opener"
```

It must be a **regular executable file** at exactly `~/bin/termux-url-opener`
(not a symlink, not a directory). Test with: share any
`https://…/.tendril/w15` URL → Termux. Click templates the opener
understands (the host is parsed, never fetched — pick any name you own):

```
NTFY_CLICK_TEMPLATE=https://tendril.local/.tendril/{workspace_id}
NTFY_CLICK_TEMPLATE=https://tendril.local/open?id={workspace_id}
```

Accepted URL grammar (id = `A-Za-z0-9._:-`, 1..128 chars): path segment
after the **last** `/.tendril/` (up to `?`/`#`), any query/fragment pair
`p=<id>` or `id=<id>`, and `tendril://host/<h>/workspace/<id>` (accepted
for completeness even though Android cannot dispatch that scheme today).
Anything else passes through to `termux-open-url` / `termux-open`. A
recognized form with a malformed id is **rejected (exit 2), never executed
and never passed through**.

## Security

- The payload whitelist is `{v,h,w,l}` — host, workspace id, label. No
  commands, paths, keys, tokens or topics; the decoder rejects unknown
  fields, oversize and non-canonical input.
- Click targets are untrusted input on the phone. The whole chain is
  argv-style: the opener validates the id and hands it over as a single
  argv element; there is no `eval` and no string interpolation into shell
  anywhere (`tendril_link.py`, the opener and the phone launcher all
  enforce the same charset). Reject-not-sanitize.
- Never put secrets in links. The ntfy topic remains the real credential —
  anyone who knows it can push to your phone. See docs/SECURITY.md.

## Platform status matrix

| path | status |
|---|---|
| Android share sheet → `termux-url-opener` | verified Termux contract (ACTION_SEND + WEB_URL match) |
| Android one-tap, fully automated | requires MacroDroid/Tasker — recipe above; ntfy Android can neither RUN_COMMAND nor start services |
| Android tap on `tendril://` link | **not possible** — Termux registers no URL schemes |
| iOS tap → Shortcut → SSH `focus` → Blink | verified mechanism (ntfy-ios, Apple Shortcuts docs, Blink source); run once on a real device to confirm |
| `blinkshell://run` x-callback | removed upstream on Blink master — do not build on it |
| `ssh://user@host` links into Blink | supported |
