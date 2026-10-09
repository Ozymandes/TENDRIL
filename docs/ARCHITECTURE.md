# TENDRIL architecture

```
PHONE (thin control surface)              HOST (where the work lives)
Android Termux                            Linux (systemd user) / macOS (launchd)
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

- Polls the same snapshot every 8 s; tracks every detected agent pane as
  `pane_id → (status, status_since, last_push)`, where `status_since` is
  when the pane entered its current status (set on baseline and on every
  transition).
- Alert classes (Herdr semantics — `idle` and `done` both mean "ready for
  input"; screen-detected agents settle to `idle`):

  | class         | trigger                          | body                                  | priority | tags              |
  |---------------|----------------------------------|---------------------------------------|----------|-------------------|
  | needs input   | any tracked → blocked            | `PI needs input · 18m · ~/research`   | high     | rotating_light    |
  | done          | working → idle / done            | `PI finished · 22m · ~/research`      | default  | white_check_mark  |
  | still working | working ≥ NTFY_WORKING_NUDGE_MINUTES (default 45, 0 disables), once per stint | `PI still working · 47m · ~/research` | min      | hourglass         |

  Everything else is silent. Titles are `TENDRIL · <label>` (workspace
  label else workspace id, sanitized via tendril-link and capped at 60
  chars). Bodies carry only deterministic snapshot state — agent kind,
  humanized time in status (`45s` / `18m` / `1h02m`) and a `~`-collapsed
  cwd shortened to its last two components past 28 chars and omitted when
  unknown — never agent output, prompts, or anything not in the snapshot.
- First sighting baselines silently; after any Herdr outage the state is
  re-baselined (no false completion storms). Per-pane repeat window (90 s)
  deduplicates flaps; the still-working nudge fires exactly once per
  working stint (at the first poll past the threshold).
- Click plumbing (optional): `NTFY_CLICK_TEMPLATE` renders a URL from the
  placeholders `{host} {workspace_id} {label} {label_uri} {payload_b64}
  {uri}`; `{payload_b64}` is the canonical tendril-link payload (host +
  workspace id + label only). The rendered URL goes out as the ntfy
  `Click` header and — unless `NTFY_ACTIONS=0` — as an ntfy action button
  (`view, Attach, <url>, clear=true`). Templates are total (unknown
  placeholders render empty); a pane whose workspace vanished pushes
  without click headers. Host comes from `TAILSCALE_HOST` in the
  remote-agents config, else the machine nodename.
- Optional body detail: `NTFY_SUMMARIZER_BIN` names a local executable
  that receives one JSON object on stdin and may append a one-line
  (≤200 chars) ` — <detail>` to the body. Off by default; any failure is
  silently ignored. Zero LLM cost by default: no API keys, no external
  calls, no network beyond the ntfy POST itself.
- Push transport: ntfy over HTTPS with `Title`/`Priority`/`Tags` (plus
  optional `Click`/`Actions`) headers, plain text only (no Markdown — the
  mobile apps do not render it reliably). Credentials only from
  `~/.config/remote-agents/notify.env` (chmod 600).

### tendril_host (host facts + watcher lifecycle)

- `bin/tendril_host.py` — installed as `~/.local/bin/tendril_host.py` —
  holds the few host facts and lifecycle verbs that differ by OS, chosen by
  capability, not OS name: launchd where `launchctl` exists, a systemd user
  manager where `systemctl --user` answers; stdlib only, Python 3.9+.
- Host facts for the `I` info panel and `./install --doctor`: Tailscale CLI
  (PATH first, then the macOS app-bundle CLI), MagicDNS short name or the
  nodename without its `.local` suffix, load, memory (`/proc/meminfo`, else
  `sysctl` + `vm_stat`), battery (`/sys`, else `pmset`), uptime.
- Watcher lifecycle (`tendril service status|start|stop|restart|logs`, plus
  `service install|uninstall` for the installer):
  - macOS: a per-user LaunchAgent `com.tendril.herdr-notify`
    (`~/Library/LaunchAgents/com.tendril.herdr-notify.plist`, logs to
    `~/Library/Logs/tendril/herdr-notify.log`) with `RunAtLoad` and
    `KeepAlive` only on unsuccessful exit — crashes are restarted (5 s
    throttle), hangs are not: launchd has no watchdog. The launchd domain
    is `gui/<uid>` in a desktop login, `user/<uid>` over bare SSH, chosen
    by probing `launchctl print`; `stop` bootouts the agent (a kill would
    be undone by KeepAlive) until the next login.
  - Linux: the `herdr-notify.service` user unit with `WatchdogSec=60` —
    the poll loop pings every cycle, so a hung watcher is SIGABRT'd and
    restarted within the window.
  - Neither manager present: the verbs report it and suggest running
    `herdr-notify` by hand.
- The `remote-path check|plan|apply|remove` verbs manage the
  `# >>> tendril remote PATH` block in `~/.zshenv`/`~/.bashrc` plus
  `~/.profile`, so non-interactive SSH (`$SHELL -c`) and Mosh (`sh -lc`)
  sessions find `remote-agents` and `mosh-server` despite sshd's bare
  default PATH (`/usr/bin:/bin:/usr/sbin:/sbin` on macOS).

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
   user service unit (systemd or launchd). No tmux, no web dashboard, no
   always-new services.
3. Every screen is generated from live state at invocation time.
4. Nothing public: the only network exposure is your tailnet.
