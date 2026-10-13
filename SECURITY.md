# Security model

## Network

- All traffic rides the tailnet (WireGuard). Nothing is exposed to the LAN
  or the internet: no port forwarding, no public listeners.
- Mosh uses UDP 60000-61000 but only ever on the `tailscale0` interface.

## Host access

- OpenSSH with `PermitRootLogin no` and `PubkeyAuthentication yes`.
- Password authentication stays enabled only until the phone's Ed25519 key
  is proven working, then `PasswordAuthentication no`.
- Keys are dedicated per device; private keys never leave the device they
  were generated on (`ssh-copy-id` moves public keys only).
- `remote-agents` and `herdr-notify` run as your user; there are no root
  components and no setuid bits.

## macOS specifics (Remote Login, launchd, PATH block)

- Host access is plain sshd via System Settings → General → Sharing →
  **Remote Login**. TENDRIL never uses `sudo` and does not need Full Disk
  Access (that optional toggle only matters for TCC-protected folders).
- The watcher is a per-user LaunchAgent
  (`~/Library/LaunchAgents/com.tendril.herdr-notify.plist`): it runs as
  your user in the `gui/<uid>` or `user/<uid>` launchd domain, logs to
  `~/Library/Logs/tendril/`, and adds no privileges beyond your own.
- The managed PATH block (`# >>> tendril remote PATH`) only prepends
  standard bin directories (`~/.local/bin`, `/opt/homebrew/bin`,
  `/usr/local/bin`) to your own shell startup files (`~/.zshenv` or
  `~/.bashrc`, plus `~/.profile`), after a timestamped backup. It changes
  nothing for other users and grants no new capability: it only makes your
  own binaries findable in non-interactive SSH/Mosh sessions, which macOS
  sshd starts with the bare PATH `/usr/bin:/bin:/usr/sbin:/sbin`. Remove
  it with `./uninstall` (it asks) or
  `python3 ~/.local/bin/tendril_host.py remote-path remove`.

## Notifications

- ntfy topics are the credential: the installer generates a long random
  name. **A random topic is obscurity, not access control** — anyone who
  learns it can read or send. For stronger guarantees use authenticated
  ntfy (`NTFY_TOKEN=` with an ntfy access token) or a self-hosted ntfy
  behind your tailnet.
- Notification payloads contain only fixed phrases (agent done / needs
  input, scheduled task started / waiting / failed, Herdr offline /
  restored) plus host, workspace, agent and task names and a `tendril://`
  deep link — never prompts, agent output, or secrets.

## Secrets at rest

- `~/.config/remote-agents/notify.env` is chmod 600 and is the only place
  ntfy credentials exist. Scripts warn if permissions drift.
- Nothing is committed to the repository: `notify.env` and `config` are
  user-machine files created by the installer, never shipped in the tree.

## The phone can only ask, never command (TENDRIL Link, v0.2)

- Every phone → host call is Termux `RUN_COMMAND` → `~/bin/tendril <fixed
  verb>` → plain SSH (BatchMode, short connect timeout) →
  `remote-agents <fixed verb>`. Arguments travel as an argv array — no shell
  string is ever built on the phone — and every variable argument (session
  ids, task ids, base64url schedule payloads, titles) is regex-validated on
  **both** ends before anything is dialed or run
  ([docs/LINK-CONTRACT-0.2.md](docs/LINK-CONTRACT-0.2.md) §0, §4).
- The complete verb set TENDRIL Link can send: `enter`, `resolve`,
  `snapshot`, `capacity`, `health`, `herdr restart`, `schedule
  add|show|cancel`, `rename`, `link-manifest`, `--upgrade`, plus Termux's own
  `test -x`/`test -s` file probes. There is no command field and no code
  path that could add one.
- From the phone, Herdr can only be **restarted** — and only through the one
  verified revive (refuses an intentional stop, rate limited, verified).
  `herdr stop` / `herdr start` are host-only.
- Deep links (`tendril://`) are data, never code: a closed grammar
  (host, workspace id, label, optional pane), checked against your paired
  host before anything runs ([docs/DEEPLINK.md](docs/DEEPLINK.md)).

### What TENDRIL Link may touch

| Permission | Why |
|---|---|
| `com.termux.permission.RUN_COMMAND` | the fixed verbs above; granted by you, revocable |
| `INTERNET`, `ACCESS_NETWORK_STATE` | the ntfy stream on the server from your pairing code — nothing else |
| `POST_NOTIFICATIONS`, `FOREGROUND_SERVICE*`, `RECEIVE_BOOT_COMPLETED`, `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` | the notification bridge |
| `CAMERA` | scanning a pairing QR; requested only inside the scanner |
| `REQUEST_INSTALL_PACKAGES` | opening Android's installer on a verified update |

No accounts, no analytics, no crash reporting, no TENDRIL cloud. TENDRIL
runs no service of its own; notifications go through the ntfy server you
configure (ntfy.sh or your own).

### Pairing codes are secrets

`tendril --pair` prints a `TENDRIL2:` code carrying the host id, SSH target
and user — and, when notifications are configured, the ntfy server, topic
and a **read-only** subscribe token. It never carries SSH keys. Treat the
code (and its QR) like the topic itself: don't paste it anywhere public.

## Updates

- `tendril --link-stage` (host) validates a TENDRIL Link build's manifest
  (package, versionCode, sha256, size, signing-certificate sha256) against
  the actual APK, pins the signer on first stage and **refuses a signer
  change or a versionCode downgrade**.
- On the phone, `tendril --upgrade` downloads over your existing SSH key
  auth and checks sha256 + size against the host manifest before keeping a
  byte. From the app, TENDRIL Link then re-verifies the exact bytes it will
  install — sha256, size, package, versionCode, and (where Android exposes
  an archive's signer) its own certificate and the host pin.
- Android's system installer always asks you to confirm. TENDRIL never
  installs silently, and Link needs your one-time "install unknown apps"
  grant.
- Host updates (`tendril --upgrade` on the host) only fast-forward a clean,
  recorded checkout — never reset, rebase or force — and abort if
  `config` or `notify.env` would change by a byte.

## Capacity credentials

Capacity reads local transcripts by default (no network, no secrets). The
provider usage adapters (`claude_oauth`, `codex_wham`, `zai_quota`) are
**off** until you run `tendril capacity enable <adapter>`; they read only
the credential subtree they need into memory, send it only as a Bearer
header to that provider, and never log, cache or print it. Neither
`capacity` nor `health --json` output contains secrets; `health` omits
paths and pids too.

## Reporting a vulnerability

Please don't file security problems as public issues. Open an issue that
asks for a private contact (no details), and we'll take it from there.

## Explicitly out of scope

- No web dashboard, no inbound ports besides tailnet-internal SSH/Mosh,
  no third-party binaries at runtime (Python stdlib only).
