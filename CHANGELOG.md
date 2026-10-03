# Changelog

All notable changes to TENDRIL are documented here.

## v0.1.2 — Ctrl+Home detach + current-directory defaults

- **Ctrl+Home is the primary detach**: pressing CTRL then HOME on Termux's
  extra-keys row returns to the TENDRIL selector. Termux encodes the combo
  as `ESC [ 1 ; 5 H` (verified in termux-app's `KeyHandler`); Herdr's key
  grammar has no Home key in any modifier combination (verified via
  `herdr config check` and the v0.9.x config docs), so remote-agents runs
  `herdr session attach` behind a transparent PTY bridge that rewrites
  exactly those six bytes to Herdr's existing Alt+D detach. All other bytes
  are forwarded unchanged — plain HOME (`ESC [ H`) keeps its normal
  meaning, Android's system Home is untouched, and Alt+D / Ctrl+] remain
  working compatibility bindings. Herdr's own config is unchanged
  (`prefix+d`, `alt+d`, `ctrl+]`).
- installer's detach merge now skips only the conflicting direct key
  (token-exact match, so `alt+d` no longer substring-matches `alt+down`)
- **remote-agents**: `N` (new workspace) and `S` (quick shell) inherit the
  focused workspace's directory — Enter accepts it, an explicit absolute
  path overrides it; an empty name reuses Herdr's default naming (dir
  basename); the full directory menu only appears when no usable current
  directory exists; the `P` project flow is unchanged
- attach hints, mobile help, and ANDROID docs updated to the Ctrl+Home flow

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
