# Changelog

All notable changes to TENDRIL are documented here.

## Unreleased

- Android real-device certification (Samsung Galaxy S21 Ultra, F-Droid Termux
  0.118, TENDRIL Link full integration): ntfy notification tap → TENDRIL Link →
  Termux RUN_COMMAND → `tendril enter` lands in the exact emitting pane, across
  workspaces and with alphanumeric pane ids (`w7:p2`, `w8:p1`, `wX:pC`);
  several notifications each land in their own pane; workspace-only links keep
  their old behaviour; Ctrl+Home / Alt+D detach returns to the TENDRIL
  selector. Stale-pane handling (no attach, selector notice) is covered by
  automated tests and was not part of the device run. TENDRIL Link is an
  optional companion; TENDRIL works fully without it. macOS still lacks a
  physical-Mac sign-off and iOS/Blink a real-iPhone sign-off.

- Canonical host identity: one value,
  `TENDRIL_HOST` in the host config, is what every deep link, notification and
  pairing code calls this machine. Readers resolve it in one place
  (`tendril_link.host_identity`): config `TENDRIL_HOST` → legacy
  `TAILSCALE_HOST` (lowercased, so old `OMARCHY` backups read as `omarchy`) →
  short hostname, HOST_RE-gated and sanitized, never rewritten on disk.
  `remote-agents` (HOST_LABEL) and both `herdr-notify` sites use the resolver;
  existing installs keep resolving to their configured name with zero config
  churn.
- `tendril --pair [--code]`: prints the phone pairing code for the TENDRIL
  Link app — `TENDRIL1:` + unpadded base64url over compact JSON with the
  sorted keys `{"h","s","u","v":1}`, byte-identical to the app's decoder
  (pinned against its test fixture) — plus the identity summary, a QR of the
  code when `qrencode` is on PATH (never a dependency), the ntfy subscribe
  URL, and a paste-ready Termux setup block with every value filled in.
  Flags-only like `--upgrade` (a bare word is a session-token position);
  `--code` prints only the code. The payload carries nothing but host, ssh
  target and user — no keys, tokens, topics, paths or commands — and invalid
  values produce one clear error, never a code. SSH target resolution:
  config `SSH_TARGET` → Tailscale MagicDNS FQDN → `SSH_ALIAS` → identity.
- Installer (`./install`): discovery first, questions last. The installer now
  detects Tailscale (`tailscale status --json`), resolves the canonical
  identity, and shows one summary (identity, Tailscale name/FQDN, SSH/Mosh
  availability, project roots) with a single `Use this host? [Y/n]` — a name
  is asked only when Tailscale and the hostname disagree. `--yes` or a
  non-tty run takes every discovered default without a question. Fresh
  installs write `TENDRIL_HOST`/`SSH_TARGET` (and keep `TAILSCALE_HOST`/
  `SSH_ALIAS` for compatibility); reinstalls keep configured values and ask
  nothing about identity; the separate project-roots question is gone
  (default: the standard roots that exist, editable in the config). Step 2's
  interactive prompt count drops from 3 asks to 1 confirmation (2 only in the
  genuinely ambiguous case). The `read` helpers now tolerate a vanished tty
  (EIO) instead of dying under `set -u`.
- `docs/scripts/tendril-phone-setup.sh` accepts all four answers as env
  (`TENDRIL_HOST_ADDR`, `TENDRIL_HOST_USER`, `TENDRIL_ALIAS_NAME`,
  `TENDRIL_REPO_DIR`) and skips its questions when all four are set.

- `tendril --upgrade [--verbose]`, plus `tendril --version` and `--help`:
  safe self-update for hosts. Every install now records provenance
  (`~/.config/remote-agents/install.json`: source checkout, commit,
  branch, version, sha256 of the installed phone launchers); the upgrade
  re-reads it — fallback: the realpath of the running command, but only
  inside a real checkout containing ./install, never a guessed path — and
  refuses (one line, exit non-zero, nothing touched) on: non-git source,
  uncommitted/untracked changes (`git status --porcelain`), a
  merge/rebase/cherry-pick in progress, detached HEAD, missing upstream,
  local or diverged commits, or a branch switch since install. Otherwise:
  `git fetch --quiet` + `git merge --ff-only @{u}` (never reset/rebase/
  force), then the new `./install --upgrade` non-interactive mode
  (binaries incl. the new `tendril_upgrade.py`, entrypoint symlink, PATH
  block only when missing, idempotent Herdr-binding merge, watcher unit
  refresh) and a hard abort if `config` or `notify.env` changed by a byte
  (the ntfy topic can never rotate here). The watcher restarts only when
  its binary or unit actually changed (or it was not running); systemd
  daemon-reload happens only when the unit content differs. Closes with
  non-destructive health checks (python, herdr `api snapshot`, ssh/mosh,
  service status, config parse, remote PATH) and a compact summary block;
  when `phone/tendril` / `phone/termux-url-opener` changed upstream it
  prints `android client update available` with the exact Termux refresh
  commands. Version comes from the new repo `VERSION` file (`0.2.0-dev`
  until the next tag; a tag on HEAD overrides, `git describe` covers older
  checkouts without the file). Upgrade is flags-only by design — a bare
  word is a session-token position and a workspace may be labeled
  `upgrade`.

- Exact targets and the notification entry lifecycle. Deep links gain an
  additive `?pane=<pane-id>` query (contract stays v1; workspace-only
  links are byte-identical, payload whitelist is now `{v,h,w,l,p}`).
  herdr-notify aims every click URI at the EMITTING agent's own pane
  (new `{pane_id}` and `{target}` placeholders; `{uri}` carries the
  pane), never the workspace's currently focused tab — the wrong-tab root
  cause. New `remote-agents enter <target>`: resolve, exact focus
  (`workspace focus` + `tab focus` + `agent focus <pane_id>`, verified
  against the snapshot's `focused_pane_id`), the same Ctrl+Home-bridged
  attach the selector uses, then the selector loop in-process on detach —
  no re-exec, no recursion, no orphaned mosh. A stale pane never attaches:
  `enter` opens the selector with a one-line notice and the parent
  workspace preselected; `resolve`/`attach`/`focus` take the existing
  not-found path (exit 2) with `resolve` gaining `pane_id`/`tab_id`/
  `agent` fields. `attach` stays one-shot. Phone side: `tendril enter`
  (same validation/quoting/mosh-ssh fallback as attach) and the
  termux-url-opener now routes recognized links to `tendril enter <id>`.
  Closed pane/tab ids are not reused within a server lifetime (Herdr
  documented; cross-restart ids are a new server's scope — hence the
  verification + stale handling).

- Reinstalling no longer rotates the ntfy topic. `./install` used to
  mint a fresh random topic every time the notification question was
  answered yes, silently invalidating every subscribed phone. An existing
  configuration (non-empty `NTFY_TOPIC`) is now detected and preserved by
  default — `git pull && ./install` leaves `notify.env` byte-for-byte
  unchanged — and rotation is a separate, explicit, default-no question
  that warns about resubscribing. Rotation restarts the watcher only when
  it is actually running; a custom `NTFY_URL`/`NTFY_TOKEN` is carried over
  instead of being reset. The canonical `tendril://host/<host>/workspace/<id>`
  deep-link contract (host/id charset, normalization, malformed/stale
  behavior, the two supported actions `attach` and `focus`, security
  boundary) is now frozen in docs/DEEPLINK.md, with `{uri}` as the
  click-template placeholder that puts that link on every notification.

- Android real-device certification passed on an S21 Ultra / Termux
  against this branch: launcher refresh, restored masthead and colour
  hierarchy, selector, Alt+D / Ctrl+B d, direct attach and resolve on a
  live workspace id, and the `termux-url-opener` deep link.

- Fix (predates the cross-platform work): the installer's Herdr switching
  merge looked conflicts up by action name in a map keyed by chord, so it
  never noticed a `switch_tab` that already claims `prefix+1..9`/`alt+1..9`;
  it proposed all three switching bindings, Herdr rejected the file, and the
  valid one was rolled back too. `herdr-bindings.py switch` now decides each
  proposal alone (exact chords after range and modifier expansion, then an
  isolated `herdr config check` that ignores pre-existing warnings), appends
  only conflict-free bindings, never edits existing lines, and the installer
  and `?` help report what Ctrl+B/Alt/Ctrl + digit actually do.
- Android fixes from a real S21 Ultra: the guided setup wrote the `agent`
  wrapper through an old `agent -> tendril` symlink into `~/bin/tendril`,
  where it exec'd itself forever; setup now stages and replaces files, and
  the wrapper refuses a second hop. With the keyboard open and 7+
  workspaces the masthead vanished entirely; paths now yield before the
  brand does. A launcher with no alias lists `~/.ssh/config` hosts and exits.

- macOS as a host and as a client. `./install` now runs unchanged on macOS
  (Apple silicon and Intel, stock `/bin/sh`, Xcode Command Line Tools
  Python 3.9+): no GNU `readlink -f`/`sed -i`/`\+` greps, package hints from
  whichever manager is present (brew, pacman, apt, dnf), the Tailscale
  app-bundle CLI is found automatically, and the default host name drops the
  mDNS `.local` suffix. Herdr is found in `~/.local/bin` (install.sh) and
  Homebrew prefixes even from SSH/launchd PATHs.
- New `bin/tendril_host.py`: the few host facts and lifecycle verbs that
  differ by OS, chosen by capability. The notification watcher runs as a
  launchd LaunchAgent (`~/Library/LaunchAgents/com.tendril.herdr-notify.plist`,
  log in `~/Library/Logs/tendril/`) where `launchctl` exists, and as the
  existing systemd user unit otherwise. One command on both:
  `tendril service status|start|stop|restart|logs`. launchd restarts a
  crashed watcher; it has no watchdog, so hung-process recovery stays
  Linux-only. The Info panel no longer reads `/proc` (it crashed on macOS).
- Installer step 3b, "Remote session PATH": detects when SSH/Mosh commands
  cannot find `remote-agents` or `mosh-server` (the macOS default: sshd's
  `zsh -c` reads only `~/.zshenv`, Mosh's `sh -lc` only `~/.profile`) and
  offers one small marked block, backed up first and removed by `./uninstall`.
- `./install --client`: installs the launcher as `~/.local/bin/tendril` on a
  Mac or Linux desktop (or `tendril-remote` when the machine is also a host)
  with the default host in `~/.config/tendril/alias`. The launcher also gains
  `resolve|focus|link <id>` over plain SSH and rejects aliases that start
  with `-`.
- iOS/Blink re-certified against current Blink source; `docs/IOS_BLINK.md`
  now marks each feature as source-verified, automated-tested, real-device
  required, or blocked by Blink. `docs/MACOS.md` covers both Mac roles.
- Fixes: the Android setup block now `export`s `TENDRIL_ALIAS`; the guided
  phone setup no longer uses GNU `sed -i` on the host, refreshes
  `tendril_host.py`, and checks/restarts the watcher on launchd too; the
  suite passes on Python 3.9.
- CI: GitHub Actions runs the suite on Ubuntu and macOS (Python 3.9 and
  current) plus shell syntax checks.

- Guided phone-first setup (PR #1 by @bakrianoo): a one-shot Termux script
  (`docs/scripts/tendril-phone-setup.sh`) bootstraps both ends from the
  phone — SSH key and alias, host-side Herdr/TENDRIL install or atomic
  binary refresh (including `tendril_link.py`), `tendril`/`agent`
  launchers, the `termux-url-opener` deep-link helper, and a Termux:Widget
  shortcut — with `--check` verification of the whole chain. A beginner
  walkthrough with expected output and troubleshooting lives in
  `docs/TENDRIL-SETUP-GUIDE.md`.
- For host TTY attachments, `remote-agents` enables the Ctrl+Home PTY bridge
  by default, independently of `TERMUX_VERSION` or phone environment
  forwarding. It rewrites only `ESC [ 1 ; 5 H` to Herdr's Alt+D detach key;
  other input passes through, and non-TTY attachments keep direct passthrough.
  Set host-side `TENDRIL_DETACH_BRIDGE=0` to disable it. The bridge does not
  change Herdr config or require a phone-launcher update; translated detach
  depends on Alt+D still being bound to detach.
- The selector footer is `N New`, `I Info`, `? Help`, `R Refresh`, `Q Quit`.
  These actions dispatch immediately on one TTY keypress, without Enter; Enter
  remains for workspace selection and text fields. Info returns on any key;
  Help paginates to the phone screen with any-key advance/return. Workspace
  paths yield when needed to fit the menu and prompt with the keyboard open.
  `N` immediately opens a shell in the selected/focused workspace directory,
  with Herdr's native directory-based name: no wizard, prompts, confirmation,
  or automatic agent startup. Active-pane cwd takes priority over cached agent
  cwd, with the console cwd as fallback. Use `herdr-notify --test` on the host
  to send a test notification.
- Stop provisioning Herdr's Alt+Up previous-workspace binding so Pi can use the
  key to edit steering messages. Existing installs must remove the old binding
  and reload Herdr config; workspace digits/picker remain available.

- Sessions are addressable end to end. `remote-agents attach <id>` (and
  `tendril [alias] attach <id>` on the phone) attach directly by canonical
  Herdr workspace id (`w15`), pane id (`w15:p1` → parent), workspace number,
  or exact unique label — no selector. Unknown/stale ids exit 2 with
  closest-match hints; ambiguous labels exit 3 with the candidates.
  `resolve`/`focus`/`link` print canonical JSON and tendril-link payloads
  (`bin/tendril_link.py`: host + workspace id + label ONLY, strict decode,
  URL-safe) for notification deep links.
- `herdr-notify` sends composed operator alerts built from snapshot state
  only — zero LLM/API cost by default: "PI needs input · 18m · ~/research"
  (priority high), "PI finished · 22m · …" (default), and a once-per-stint
  "still working" nudge after `NTFY_WORKING_NUDGE_MINUTES` (45, 0 disables).
  `NTFY_CLICK_TEMPLATE` renders ntfy Click/Actions headers from the
  canonical payload so a tap can land on the exact session
  (docs/DEEPLINK.md). Optional `NTFY_SUMMARIZER_BIN` local plugin, off by
  default; no API keys, no network beyond the ntfy POST.
- iPhone via Blink Shell is first-class (docs/IOS_BLINK.md): Alt+D detach
  verified against Blink's key encoding, Mosh hosts with a `tendril`
  startup command, automatic 256-colour palette, and a deliberately small
  terminal capability model (`terminal_name()`; Blink-specific help via
  opt-in `TENDRIL_TERMINAL=blink`; the default-on Ctrl+Home bridge is harmless there — Blink cannot produce the combo).
- Android deep links: `phone/termux-url-opener` handles shared
  `https://…/.tendril/<id>` URLs under Termux's verified share contract;
  docs/DEEPLINK.md documents the honest one-tap recipes (MacroDroid/Tasker
  via ntfy's message broadcast on Android, iOS Shortcuts → `focus` → Blink
  on iPhone) and the platform limits (Termux registers no URL schemes;
  Blink removed its x-callback scheme).
- Ctrl+Home detaches on Termux without any Herdr config change: when attached
  from `remote-agents` on Termux (auto; `TENDRIL_DETACH_BRIDGE=1` to force,
  `=0` to disable), `herdr session attach` runs behind a transparent PTY
  bridge that rewrites exactly Termux's `ESC [ 1 ; 5 H` to the Alt+D detach
  Herdr already binds. Every other byte is forwarded unchanged — plain HOME
  (`ESC [ H`), Ctrl+], Alt+D, mouse, paste, resize (SIGWINCH is propagated),
  EOF, and the child's exit are all preserved; termios is restored on every
  exit path including SIGTERM/SIGHUP. Alt+D / Ctrl+B d remain the documented,
  always-working detach keys; the bridge only adds a key Herdr cannot natively
  express (0.9.3 rejects `ctrl+home`).
- `N` (new workspace) and `S` (quick shell) now default to the selected or
  focused workspace's directory: Enter accepts it, an explicit absolute path
  overrides it, and the full directory menu only appears when no usable
  directory exists. An explicit name is used verbatim; an empty name keeps
  the derived directory label (the CLI does not promise a label default).
  The `P` project flow is unchanged.

- The selector supports ↑/↓ highlighting and Enter-to-attach while preserving
  numeric workspace selection; raw TTY mode restores terminal settings on
  every exit path.
- Detach recommends Alt+D (tap ALT then D on the phone), with Ctrl+B then D as
  the fallback. Native Ctrl+Home bindings remain optional; the installer
  validates them where supported, preserves unrelated bindings, and adds only
  conflict-free shortcuts.
- New shell workspaces leave `--label` unset, using Herdr's native cwd-based
  name. Users can start agents directly from the shell.

## v0.1.2 — the mark survives phone geometry

- in-session switching that matches the muscle memory: the installer now also
  provisions `switch_workspace = "prefix+1..9"` (Ctrl+B 1..9 jumps to a
  workspace), Alt+1..9 indexed workspace jumps, prefix-free Alt+←/→
  next/previous tab, Alt+↑/↓ workspace cycling — added only when missing,
  never clobbering existing bindings, with the same conflict guard as the
  detach merge. Requires herdr >= 0.9.3 (0.9.1 ignores these keys);
  `herdr update --handoff` upgrades live
- mobile key reference corrected: herdr's prefix is Ctrl+B (was documented
  as Ctrl+Space), tab chords are prefix+n/p and prefix+shift+T/X, and
  Alt+1..9 jumps workspaces, not tabs
- Android docs: Alt+arrows stay desktop-only (Termux sends the ESC-prefix
  form Herdr deliberately ignores); phone paths are Ctrl+B chords,
  ALT+digit, and Ctrl+B w + digit

- the selector now yields per-row path lines when the terminal is too short
  to fit the masthead mark above the menu (phone, keyboard closed): the
  compact TENDRIL mark renders at 48x31 instead of silently degrading to a
  bare menu; with the keyboard open the menu still wins, never taller than
  before
- truecolour only where the terminal advertises it (`COLORTERM=truecolor`);
  otherwise the nearest 256-colour entries. mosh < 1.4 (Ubuntu 22.04 ships
  1.3.2) drops `38;2;r;g;b` sequences entirely, which rendered the whole
  phone UI monochrome — the fallback palette survives every transport
- the title row clamps a polluted `TAILSCALE_HOST` (captured prompt text
  from pre-0.1.1 installers) to the box width while keeping the
  `// REMOTE AGENTS` suffix readable
- configs written by pre-0.1.1 installers hold captured prompt text in
  `TAILSCALE_HOST`/`SSH_ALIAS`/`PROJECT_ROOTS` — re-run `install` (or edit
  `~/.config/remote-agents/config` by hand) to replace them with bare values
- tests: phone-geometry mark regression, no-room path preservation,
  polluted-label clamp, origin-main pin updated for the intentional
  body change at tight heights

## v0.1.1 — reliable Android/Termux detach

- reliable Android/Termux Herdr detach via `Ctrl+]` — a single control byte
  that cannot suffer ESC-sequence splitting across remote transports
  (`Alt+D` kept as the desktop-friendly alternative, `prefix+d` universal)
- installer merges/preserves the full detach set, never duplicates entries,
  leaves every unrelated binding untouched, and only claims `ctrl+]` when
  no other action already uses it
- mobile help, attach hints, and docs updated to match verified behavior

## v0.1.0 — initial release

- **remote-agents**: live Herdr workspace console for narrow terminals —
  dynamically generated workspace list with per-agent lifecycle chips,
  numeric attach, new-workspace wizard, project launcher, quick shell,
  host status screen, last-workspace reconnect, full mobile key reference.
- **herdr-notify**: 8-second snapshot watcher pushing `Task complete`
  (`working → idle/done`) and `Input required` (`→ blocked`) transitions to
  ntfy; silent baselining on start and after outages, per-pane dedup.
- **Protocol-verified Herdr client selection**: PATH / mise / versioned
  candidates probed; incompatible clients rejected with reasons.
- **install**: interactive user-local installer (no sudo) with `--dry-run`
  and `--doctor` (read-only system check); optional ntfy configuration with
  generated private topic; optional user-level systemd watcher; Herdr
  `Alt+D` detach binding offered with backup + validation + live reload.
- **uninstall**: removes only package-owned files; keybinding backup
  restore requires explicit confirmation.
- **phone/agent**: Termux launcher — Mosh preferred, SSH fallback,
  connectivity guidance, no hardcoded host/user.
- Docs: Android guide, security model, architecture notes.
