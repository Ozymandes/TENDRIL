# LATENCY.md — launch-trace playbook (RC2 perf)

How to attribute the "OPEN TENDRIL / OPEN SESSION shows black Termux for
seconds" delay exactly, stage by stage, instead of by guess.

Both ends write the same one-line-per-stage format to a file called
`launch.log`, and both stamps are wall-clock epoch milliseconds, so the
phone log and the host log interleave into one timeline (the tailnet
clocks are NTP-synced; residual skew is well under the stages we care
about).

```
<epoch_ms> <stage> [rc]          e.g.  1791728563411 args-validated
                                       1791728563429 preflight-end 255
```

## Turn tracing on

Phone (Termux) — either one:

```sh
touch ~/.config/tendril/trace        # persistent
TENDRIL_TRACE=1 tendril …            # one-shot
```

Host — either one:

```sh
touch ~/.config/remote-agents/trace  # persistent
TENDRIL_TRACE=1 remote-agents …      # one-shot
```

Both switches are zero-cost when off (one env test + one stat). Files are
created 0600. The phone log rotates at 64 KB (one `.old` generation is
kept) — with the Link app polling every ~8 s a trace window lasts some
minutes, so take readings soon after enabling.

Log locations:

- phone: `~/.cache/tendril/launch.log` (Termux)
- host:  `~/.cache/tendril/launch.log`

## Stage vocabulary

Phone stages (`phone/tendril`):

| stage | meaning |
|---|---|
| `start` | launcher began (before anything else) |
| `args-validated` | alias/mode/id contract checks done — nothing dialed yet |
| `transport-chosen-mosh` / `transport-chosen-ssh` | which transport won |
| `mosh-exec` / `ssh-exec` | the transport process is about to run |
| `fallback` | mosh failed; ssh fallback path entered |
| `preflight-start` | diagnostic key probe begins — FAILURE PATH ONLY |
| `preflight-end <rc>` | probe finished with `<rc>` |

Host stages (`remote-agents`):

| stage | meaning |
|---|---|
| `process-start` | python began (before the local-module imports and the §7.4 Herdr resolver) |
| `resolve-done` | Herdr client resolved |
| `rows-done` | one `herdr api snapshot` turned into selector rows |
| `pane-lookup-done` | exact-pane snapshot lookup done (`w16:p2` verbs) |
| `focus-done` | exact focus commands + verify snapshot done |
| `attach-exec` | `herdr session attach` about to exec |
| `selector-first-paint` | first full selector frame flushed (once per process) |

## The S21 measurement procedure

1. Enable the trace on BOTH ends (the two `touch` commands above).
2. On the phone, tap **OPEN TENDRIL** three times and **OPEN SESSION**
   (or a LIVE MAP pane row) three times, waiting for each to settle.
3. Print the merged timeline — one command, run in Termux (it streams the
   phone log to the host over the warm connection and merges there):

```sh
ssh -o ConnectTimeout=6 "$TENDRIL_ALIAS" 'cat - ~/.cache/tendril/launch.log | sort -n | awk "NR==1{t=\$1} {printf \"%7.3fs  %s\n\", (\$1-t)/1000, \$0}"' < ~/.cache/tendril/launch.log
```

Pure-awk equivalent when both files sit on one machine (6 lines):

```awk
awk '{print $1, "PHONE", $2, $3}' ~/.cache/tendril/launch.log > /tmp/tl
awk '{print $1, "HOST ", $2}'     ~/.cache/tendril/launch.log >> /tmp/tl
sort -n /tmp/tl | awk 'NR==1 { t = $1 }
                       { printf "%7.3fs  %s\n", ($1 - t) / 1000,
                         substr($0, index($0, $2)) }'
```

## Stage budget (healthy path, after the RC2 fast-path fix)

Phone:

| boundary | budget |
|---|---|
| `start` → `args-validated` | < 20 ms (sh parse + two stats) |
| `args-validated` → `mosh-exec` | < 5 ms |
| `mosh-exec` → first host `process-start` | ~0 ms with a warm control socket (single-digit ms mux), ~160–260 ms cold (one handshake). RC2 removed the second serial handshake the old preflight cost, and the ~5 s stall `ConnectTimeout=5` produced when a dead resolved address was tried first |
| failure only: `fallback` → `preflight-end` | ≤ ~5 s, bounded by the probe's `ConnectTimeout=5` (diagnostics only, never on the healthy path) |

Host:

| boundary | budget |
|---|---|
| `process-start` → `resolve-done` | ~20–60 ms (imports + §7.4 resolver) |
| `resolve-done` → `rows-done` | 5–6 ms (`herdr api snapshot`) |
| `rows-done` → `pane-lookup-done` | 5–6 ms (second snapshot, pane verbs) |
| `pane-lookup-done` → `focus-done` | ~10–15 ms (focus commands + verify snapshot) |
| `focus-done` → `attach-exec` | < 5 ms |
| selector: `rows-done` → `selector-first-paint` | < 25 ms (render + the tailnet chip's `tailscale ip -4`, ~10 ms) |

Expected end-to-end after RC2: roughly 0.1–0.4 s tap-to-content with a
warm socket, ~0.3–0.7 s cold. If the merged timeline shows the gap
between `mosh-exec` and host `process-start` at seconds, the problem is
phone-side dialing (addresses/handshake); if host stages stack up
unexpectedly, it is host-side.

## Design notes (why it looks like this)

- **Healthy fast path first.** The interactive path dials the transport
  directly. The key-auth probe runs ONLY after a transport failure; the
  old behavior (first-time-key guidance + degraded `exec ssh -t` so a
  password still works and the rc propagates) is preserved via that
  failed-attempt detection. At most ONE probe per launch.
- **Connection reuse.** Every launcher ssh — background verbs AND
  interactive, including mosh's bootstrap via
  `mosh --ssh="ssh -o ControlMaster=auto -o ControlPath=~/.ssh/tendril-cm-%C …"`
  — multiplexes over one control socket per destination (`%C` is the
  per-host hash; ssh expands the `~` itself, so the option string stays
  one token for mosh). `ControlMaster=auto` falls back to a direct
  connection when the socket is stale (verified on OpenSSH 10: dead
  master → direct connect in ~0.2 s). `ControlPersist=120` is
  BatchMode-only on purpose: the app's snapshot/health polling keeps the
  master warm exactly when a handoff is imminent, and interactive
  sessions never leave a background ssh running after the selector
  closes.
- **Bounded failure without stacked handshakes.** Interactive transports
  carry `-o ConnectionAttempts=1 -o ConnectTimeout=6` (the preflight's
  old job, moved onto the one handshake we actually make). If the alias
  resolves to more than one address (dual v4/v6 tailnet records),
  OpenSSH walks them in order and the timeout applies per address — a
  dead first address can still add up to `ConnectTimeout` per address.
  We deliberately do NOT force `-4`/`-6`; if the merged timeline shows
  that pattern, fix the alias's records instead.
- **Byte-identical safety surface.** All argument validation, quoting,
  fixed-verb rules and the id regexes are unchanged; the trace logs verb
  names only — never hostnames, ids, or prompt text.
