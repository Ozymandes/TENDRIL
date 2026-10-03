# Changelog

All notable changes to TENDRIL are documented here.

## v0.1.2 — ⌂ detach button + current-directory defaults

- **phone**: new `tendril-keys` helper (Termux) adds a ⌂ button to the
  extra-keys row; its macro emits `ALT d`, so detaching is one tap:
  ⌂ → Alt+D → Herdr detach → TENDRIL menu. The helper backs up
  `~/.termux/termux.properties`, touches only the `extra-keys` value,
  preserves every existing button, falls back to printed instructions for
  layouts it cannot parse safely, and reloads via `termux-reload-settings`.
- **no Herdr key-grammar changes**: the detach set stays
  `prefix+d`, `alt+d`, `ctrl+]`; `Ctrl+Home` is not expressible in Herdr's
  verified grammar, so the phone button simply sends an already-supported
  sequence
- installer's Android setup block copies/runs `tendril-keys`; its detach
  merge now skips only the conflicting direct key (token-exact match)
- **remote-agents**: `N` (new workspace) and `S` (quick shell) inherit the
  focused workspace's directory — Enter accepts it, an explicit absolute
  path overrides it; an empty name reuses Herdr's default naming (dir
  basename); the full directory menu only appears when no usable current
  directory exists; the `P` project flow is unchanged
- attach hints, mobile help, and ANDROID docs updated to the ⌂ flow

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
