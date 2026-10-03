# Changelog

All notable changes to TENDRIL are documented here.

## v0.1.2 — home-row UX pass

- **Ctrl+Esc** is the new primary detach-to-selector binding: it passes
  Herdr's verified key grammar and avoids the `]` symbol layer on Termux
  (`Ctrl+Home` is not expressible — Herdr's parser accepts no home/end keys
  in any modifier combination, confirmed against `herdr config check` and
  the v0.9.x config grammar)
- `Ctrl+]` retained as the legacy single-byte detach; full detach set is now
  `prefix+d`, `alt+d`, `ctrl+esc`, `ctrl+]`, merged conflict-free by the
  installer as before
- **new workspace defaults to the current directory**: `N` and `S` inherit
  the focused workspace's directory (Enter accepts it, an explicit path
  overrides it); an empty name reuses Herdr's own default naming (dir
  basename) instead of forcing input; the full directory menu only appears
  when no usable current directory exists, and the `P` project flow is
  unchanged

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
