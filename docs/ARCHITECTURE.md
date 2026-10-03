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
  client exits (detach `Alt+D`, fallback Ctrl+B then D, lost connection,
  closed terminal) the menu redraws. Panes survive everything short of
  `herdr server stop`. For TTY attachments, the host enables a PTY bridge by
  default, translating only `ESC [ 1 ; 5 H` to Alt+D. This does not depend on
  `TERMUX_VERSION` being forwarded and works over SSH or Mosh; host-side
  `TENDRIL_DETACH_BRIDGE=0` disables it. Non-TTY attachments use direct
  passthrough. This bridge is separate from optional native Herdr bindings.
- The selector reads TTY keys in raw mode with guaranteed terminal restoration,
  decodes CSI and SS3 arrows for selection, and restores the chosen workspace
  across refreshes and attach returns. Its footer is exactly `N New`, `I Info`,
  `? Help`, `R Refresh`, and `Q Quit`; each action dispatches on one TTY key,
  without Enter. Enter remains for workspace selection and text prompts. Info
  returns on any key; Help paginates to terminal height with any-key advance
  and return. Paths yield when needed to keep the menu and prompt visible.
  Non-TTY input keeps its line-based menu.
- `N` immediately creates and attaches a shell workspace via `herdr workspace
  create --cwd <directory> --focus`, without `--label`, prompts, or agent startup.
  Herdr derives the name from the directory. Use the selected/focused workspace's
  active pane directory, preferring its foreground cwd over cached agent cwd;
  fall back to the console cwd (or HOME if that cwd was deleted).
- Alt+Up is not provisioned as a Herdr workspace key: Pi uses it to edit steering
  messages. Workspace digits and the Ctrl+B w picker remain available.
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
