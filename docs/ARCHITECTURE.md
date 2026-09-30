# TENDRIL architecture

```
PHONE (thin control surface)              HOST (where the work lives)
Android Termux                            Linux + systemd (user)
  └─ tendril (sh, ~40 lines; alias agent) ├─ Tailscale / OpenSSH / mosh-server
      └─ Tailscale                        ├─ ~/.local/bin/remote-agents
          └─ Mosh / SSH ─────────────────>│    └─ herdr api snapshot (1 call)
                                          ├─ Herdr server (persistent panes)
                                          │    └─ Pi / Claude / Codex agents
                                          └─ ~/.local/bin/herdr-notify
                                               └─ ntfy ──> Android push
```

## Components

### remote-agents (host console)

- One `herdr api snapshot` call per render: workspaces, panes, agents
  (kind, lifecycle state, cwd) — the menu is always live state, never a
  cached list.
- Renders a width-aware ANSI box (clamped 34–56 cols) using the host's
  Emergent-Abyss palette: lime frame, teal working dot, lime blocked/done,
  gray idle.
- Attach = `herdr workspace focus <id>` + `herdr session attach`; when the
  client exits (detach `Ctrl+]` / `Alt+D`, lost connection, closed terminal) the menu
  redraws. Panes survive everything short of `herdr server stop`.
- New workspace (`N`) / project launcher (`P`) / quick shell (`S`) create
  workspaces via the socket API, start agents with `herdr agent start`
  (waits for `interactive_ready`), then attach.
- Herdr client selection: PATH → explicit config → mise installs, newest
  first; every candidate is probed (`--version` + `status`) and only a
  client whose protocol is **compatible with the running server** is chosen.
  Incompatible clients (e.g. an old distro package) are rejected with the
  reason printed (`--detect` shows the full verdict list).

### herdr-notify (watcher)

- Polls the same snapshot every 8 s; tracks every detected agent pane.
- State semantics (from Herdr's documented model — `idle` and `done` both
  mean "ready for input"; screen-detected agents settle to `idle`):

  | transition            | notification     |
  |-----------------------|------------------|
  | working → idle / done | Task complete    |
  | any tracked → blocked | Input required   |
  | everything else       | silent           |

- First sighting baselines silently; after any Herdr outage the state is
  re-baselined (no false completion storms). Per-pane repeat window (90 s)
  deduplicates flaps.
- Push transport: ntfy over HTTPS with `Title`/`Priority`/`Tags` headers.
  Credentials only from `~/.config/remote-agents/notify.env` (chmod 600).

### install / uninstall

- User-local only; never sudo; never installs packages; reports missing
  dependencies with exact commands.
- Herdr keybinding changes: diff shown, timestamped backup, `herdr config
  check` validated, applied with `server reload-config` (never a restart),
  auto-restored if validation fails.
- Uninstaller removes only package-owned files; restoring the Herdr config
  backup requires explicit confirmation.

## Design principles

1. The phone is a thin control surface; the work lives on the host.
2. Fewest moving parts: two stdlib Python scripts, one sh launcher, one
   user systemd unit. No tmux, no web dashboard, no always-new services.
3. Every screen is generated from live state at invocation time.
4. Nothing public: the only network exposure is your tailnet.
