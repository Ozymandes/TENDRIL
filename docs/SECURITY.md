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

## Notifications

- ntfy topics are the credential: the installer generates a long random
  name. **A random topic is obscurity, not access control** — anyone who
  learns it can read or send. For stronger guarantees use authenticated
  ntfy (`NTFY_TOKEN=` with an ntfy access token) or a self-hosted ntfy
  behind your tailnet.
- Notification payloads contain only fixed phrases (`Task complete`,
  `Input required`) plus workspace/agent names — never prompts, agent
  output, or secrets.

## Secrets at rest

- `~/.config/remote-agents/notify.env` is chmod 600 and is the only place
  ntfy credentials exist. Scripts warn if permissions drift.
- Nothing is committed to the repository: `notify.env` and `config` are
  user-machine files created by the installer, never shipped in the tree.

## Explicitly out of scope

- No web dashboard, no inbound ports besides tailnet-internal SSH/Mosh,
  no third-party binaries at runtime (Python stdlib only).
