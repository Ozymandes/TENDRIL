# Changelog

All notable changes to TENDRIL are documented here.

## Unreleased

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
  terminal capability model (`terminal_name()`; the Ctrl+Home bridge stays
  Termux-only; Blink-specific help via opt-in `TENDRIL_TERMINAL=blink`).
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
  numeric and command-letter input; raw TTY mode restores terminal settings on
  every exit path.
- Detach now recommends Alt+D (tap ALT then D on the phone), with Ctrl+B then D
  as the fallback. Ctrl+Home remains optional: Herdr 0.9.3 rejects it. The
  installer validates it before migrating legacy `ctrl+]`, preserves unrelated
  bindings, and adds only conflict-free shortcuts.
- New-workspace labels are derived from the selected cwd (the installed CLI's
  docs do not specify an automatic label default); agent names are sanitized
  from the resulting label.

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
