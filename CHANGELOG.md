# Changelog

All notable changes to TENDRIL are documented here.

## v0.1.2 — the mark survives phone geometry

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
