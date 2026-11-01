# TENDRIL ↔ TENDRIL Link — 0.2.0 protocol contract (binding)

Owner: integration authority (Opus). Host = authoritative. Phone = renderer +
fixed-verb caller. Everything here is additive to snapshot v2; a 0.1.x phone
(parser accepts any version ≥ 1, ignores unknown keys) keeps working.

## 0 · Invariants

- Phone → host only through Termux RUN_COMMAND → `~/bin/tendril <fixed verb>`
  → plain SSH (BatchMode, ConnectTimeout=6) → `remote-agents <fixed verb>`.
  Argv arrays on the phone; on the host, SSH flattens args into a shell
  string, so **every variable argument is regex-validated on both ends and
  single-quoted** by the launcher.
- No raw prompt text, terminal content, cwd, titles, commands, paths, hostnames
  of other machines, credentials or account identifiers in the snapshot.
  Scheduled prompt text appears ONLY in `schedule show <id>` output (the user
  wrote it) — never in `snapshot`.
- Every optional field is individually validated; a malformed optional field
  is dropped alone (never the agent / provider / task).
- Never fabricate. Unknown ⇒ key absent.
- Snapshot document ≤ 64 KB (Termux result channel). Hosts cap lists.

## 1 · Snapshot v3 (`tendril snapshot`)

```jsonc
{
  "version": 3,
  "host": "omarchy",
  "generated_at": 1791583000,
  "workspaces": [ { /* v1/v2 unchanged */
    "agents": [ { /* v1/v2 unchanged. NOTE: telemetry keys are FLAT on the
                     agent object (as in v2): model, provider, effort,
                     started_at, context_tokens, output_tokens, cost_usd,
                     tool, tool_started_at, activity, last_event, trail —
                     and the v3 additions below sit beside them. */
        "phase": "thinking|tool|editing|testing|reviewing",   // NEW, optional
        "events": [                                            // NEW, ≤ 12, newest last
          {"kind": "prompt|reply|tool|error|subagent", "tool": "bash", "at": 1791582990}
        ],
        "children": [                                          // NEW, ≤ 8, newest first
          {"id": "a1b2c3d4", "kind": "subagent", "harness": "pi|claude",
           "name": "worker",                // agent TYPE name (e.g. "worker", "Explore"), sanitized ≤ 24 [A-Za-z0-9._-]
           "model": "glm-5.3-flash", "effort": "high",
           "status": "working|thinking|tool|waiting|done|failed|cancelled|unknown",
           "tool": "edit", "activity": "Editing CapacityScreen.kt",
           "started_at": 1791582000, "ended_at": 1791582900,
           "context_tokens": 38600, "output_tokens": 8000, "cost_usd": 0.01,
           "async": true}
        ]
    } ]
  } ],
  "capacity": {                                               // NEW, optional
    "generated_at": 1791583000,
    "providers": [
      {"id": "claude", "label": "Claude", "harness": "claude",
       "models": ["claude-opus-5-5"], "harnesses": ["claude","pi"],
       "discovered_from": ["active_sessions","configured_adapter"],
       "last_seen_at": 1791582990,
       "state": "ready|pressure|critical|exhausted|unavailable|unknown",
       "windows": [{"kind": "session|weekly|daily", "used_pct": 91.0,
                    "resets_at": 1791592000, "window_min": 300}],
       "tokens_today": 1288000,
       "tokens_by_day": [{"date": "2026-10-04", "tokens": 812000}],   // ≤ 7, oldest first
       "by_model": [{"model": "claude-opus-4-8", "tokens": 900000}], // ≤ 5
       "cost_today_usd": 0.0,
       "plan": "max",                       // short token only
       "observed_at": 1791582700,
       "source": "claude_oauth|claude_local|codex_rollout|codex_wham|pi_local|zai_quota"}
    ]
  },
  "schedule": {                                               // NEW, optional
    "available": true,                      // false ⇒ scheduler not installed on host
    "tasks": [                              // ≤ 30: all non-terminal + newest 20 terminal
      {"id": "t-20261010-a1b2c3d4", "title": "Architecture audit",
       "workspace": "w19", "workspace_label": "research", "harness": "claude",
       "trigger": {"kind": "at|on_reset|when_available", "at": 1791590000, "provider": "claude"},
       "state": "QUEUED|WAITING_FOR_RESET|WAITING_FOR_CAPACITY|READY|RUNNING|DONE|FAILED|CANCELLED",
       "due_at": 1791592000,                // host's best estimate (resets_at for on_reset)
       "created_at": 1791580000, "started_at": null, "finished_at": null,
       "pane_id": "w19:p4", "failure": "launch refused"}   // failure: short safe phrase ≤ 60
    ]
  }
}
```

Capacity state thresholds (worst window): ready < 70 · pressure 70–89 ·
critical 90–97 · exhausted ≥ 98 or an active 429 with `resets_at` in the
future · unavailable when a known adapter failed or its last authoritative
sample is stale · unknown when the provider is known but no quota classification
is available. Provider identity metadata may remain present in either state.

Phase (host-classified, never shows a command): `tool` when a tool is open;
`editing` for edit/write/apply_patch-class tools; `testing` when an open bash
call's argv[0] basename ∈ {pytest, gradle(w), npm/pnpm/yarn with `test`,
cargo test, go test, make test, unittest, run_suite.py} (classified on the
host, only the phase word leaves); `reviewing` for read/grep/find/ls-class
tools; `thinking` when working with no open tool; absent otherwise.

Hook signatures (host): `tendril_capacity.snapshot_section(budget_s, discovered=None)` and
`tendril_schedule.snapshot_section()` both return `(section_dict | None,
capability_string)`.

## 2 · Capacity (host)

`bin/tendril_capacity.py`, stdlib. A provider registry canonicalizes known
aliases (Anthropic/Claude, OpenAI/Codex, Z.AI) and passes previously unseen
provider ids through as generic records. Discovery uses already-sanitized
current/recent session metadata, bounded local usage evidence and adapter
configuration; credential-file presence is an identity signal only. A separate
adapter registry supplies metrics through `claude_local`, `codex_rollout`,
`pi_local` (default ON, no network, no secrets) and `claude_oauth`,
`codex_wham`, `zai_quota` (default OFF; enabled with
`tendril capacity enable <id>…`; read only the needed credential subtree into
memory, GET with Bearer only, 10 s timeout, response schema-projected, never
logged/cached as raw). Cache `~/.cache/tendril/capacity.json` (0600); min
intervals: local 60 s, network 300 s, 2× backoff on 429/401, never retry 401.
CLI: `remote-agents capacity [--json]`, `tendril capacity status|enable|disable`.
The snapshot embeds the cached capacity (never blocks > 1.5 s on it).

## 3 · Scheduler (host)

`bin/tendril_schedule.py` + verbs in `remote-agents schedule …`.
Storage `~/.local/state/tendril/schedule.json` (dir 0700, file 0600),
`{"schema_version":1,"tasks":[…]}`; all mutations under `flock` on
`schedule.json.lock`; atomic tmp+fsync+rename+dir-fsync.
Tick: systemd user `tendril-schedule.timer` (OnBootSec=45s,
OnUnitActiveSec=30s, Persistent=true) → `tendril-schedule.service`
(oneshot `remote-agents schedule tick`). macOS: documented later.

Triggers: `at` (absolute epoch; the phone converts "in 45 min" to `at`
using **host time** echoed by the snapshot `generated_at`), `on_reset`
(provider), `when_available` (provider).

| Event | From | To |
|---|---|---|
| add (validated) | — | QUEUED (`at`) · WAITING_FOR_RESET (`on_reset`, resets_at known) · WAITING_FOR_CAPACITY (`when_available`, or `on_reset` with unknown resets_at) |
| clock ≥ at, provider not exhausted | QUEUED | READY |
| clock ≥ at, provider exhausted | QUEUED | WAITING_FOR_CAPACITY |
| clock ≥ resets_at and fresh sample not exhausted (or no 429 newer than resets_at) | WAITING_FOR_RESET | READY |
| provider state ∈ {ready, pressure, critical} | WAITING_FOR_CAPACITY | READY |
| tick writes RUNNING + launch_token (fsync) **then** launches | READY | RUNNING |
| launch error | RUNNING | FAILED (`launch refused` / `workspace missing` / `prompt rejected`) |
| RUNNING with no pane after crash | RUNNING | FAILED (`interrupted`) — never relaunched |
| agent pane reports done/idle after prompt | RUNNING | DONE |
| pane disappears | RUNNING | DONE (`session ended`) |
| cancel | QUEUED, WAITING_*, READY | CANCELLED |
| cancel | RUNNING | refused (`running — open the session to stop it`) |
Terminal states are immutable. Duplicate add (same id or same fingerprint
within 60 s) is refused.

Launch (argv only): new tab/pane in the target workspace →
`herdr agent start <name> --kind <harness> --pane <pane>` →
`herdr agent prompt <pane> <PROMPT> --wait` (verify target semantics live;
fall back to the harness's positional prompt argv if needed). Notify via
herdr-notify `send()` + `tendril_link.describe()` deep link:
`TENDRIL · TASK STARTED` / `TASK WAITING` (only on entering WAITING_FOR_RESET
from an `at` trigger) / `TASK FAILED`; DONE is covered by the existing
herdr finished notification (no duplicate).

## 4 · Fixed verbs

| Phone argv (RUN_COMMAND) | Remote command | Validation (both ends) |
|---|---|---|
| `snapshot` | `remote-agents snapshot` | — |
| `schedule show <id>` | `remote-agents schedule show '<id>'` | id `^t-[0-9]{8}-[a-f0-9]{8}$` |
| `schedule add <b64>` | `remote-agents schedule add '<b64>'` | `^[A-Za-z0-9_-]{1,16384}$`; decoded JSON `{title ≤ 80, prompt 1..8000, workspace ID_RE, harness ∈ launchable, trigger}`; reply one JSON line `{"ok":true,"task":{…}}` or `{"ok":false,"error":"<safe phrase>"}` |
| `schedule cancel <id>` | `remote-agents schedule cancel '<id>'` | id regex |
| (Termux user) `tendril link` (NO args; `tendril [<alias>] link <id>` keeps its existing host meaning) | — local: `am start -n app.tendril.link/.MainActivity` (fixed component; no URI built from input) | no args accepted |

Snapshot `capabilities`: hosts that support these verbs add
`"capabilities": ["schedule", "capacity"]` at top level; the phone hides
CREATE / CANCEL when absent and says so (designed state).

## 5 · Android model (core/)

`core/topology` parses v3 additively: `AgentTelemetry.phase/events/children`,
`TopologySnapshot.capacity: CapacitySnapshot?`, `schedule: ScheduleSnapshot?`,
`capabilities: Set<String>`. New `core/schedule/ScheduleCommands` builds the
b64url payload and runs `schedule add|cancel|show` through the existing
TermuxProbe RUN_COMMAND background channel (20 s timeout, single-flight), then
triggers a snapshot refresh. Fixture data lives only in debug/preview/test.

## 6 · Herdr health & recovery (0.2 addendum — binding)

Problem: Herdr (the session server) can crash while the host and SSH
transport stay reachable. Link must tell "host unreachable" from "Herdr
offline", and offer one safe, verified revive.

### 6.1 Host verbs (fixed, allowlisted; no free arguments)

| Phone argv (RUN_COMMAND) | Remote command | Notes |
|---|---|---|
| `health` | `remote-agents health --json` | read-only; MUST NOT depend on a live Herdr server (no `herdr api` call that blocks; bounded probes ≤ 3 s total) |
| `herdr restart` | `remote-agents herdr restart` | starts Herdr via its supported headless lifecycle, then VERIFIES; one JSON line |
| (host user) `tendril herdr stop` | — | intentional stop: writes the stop guard, then stops Herdr |
| (host user) `tendril herdr start` | — | clears the guard, starts + verifies |

`health --json` → one line:
```jsonc
{"version":1,"host":"omarchy","generated_at":1791583000,
 "herdr":{"state":"running|down|stopped|unknown",   // stopped = intentional stop guard set
          "last_healthy_at":1791582880,             // optional
          "since":1791582900},                      // optional: when current state began
 "tendril":{"state":"healthy|degraded","version":"0.9.x"},  // install files present & runnable
 "notify":{"state":"active|inactive|failed|absent"},
 "scheduler":{"state":"active|inactive|absent"},
 "guardian":{"state":"active|inactive|absent"}}
```
No secrets, paths, usernames, hostnames of other machines, pids, or env.
Every key optional on the phone side; malformed ⇒ treated as unknown.

`herdr restart` reply: `{"ok":true,"herdr":{"state":"running"}}` only after
verification (server status reports running AND `herdr api snapshot`
answers within 5 s, retried ≤ 10 s total); otherwise
`{"ok":false,"error":"<safe phrase ≤ 60>"}` (e.g. "Herdr did not come up",
"Herdr is already running", "Stopped on purpose — start it on the host").
Restart REFUSES while the intentional-stop guard is set unless the user
runs `tendril herdr start` on the host (phone sees the designed STOPPED
state). Rate limit: one restart attempt per 30 s (flock + timestamp).

### 6.2 Guardian (crash notification)

Systemd user `tendril-guardian.timer` (OnBootSec=60s, OnUnitActiveSec=30s)
→ oneshot `remote-agents health tick`. State file
`~/.local/state/tendril/herdr-health.json` (0600, atomic, flock):
`{state, since, last_healthy_at, incident_id, notified_down, notified_up}`.
Transition running → down (and NOT guard-stopped) opens an incident and
sends exactly one `TENDRIL · HERDR OFFLINE` / "Host <h> is reachable, but
Herdr stopped." via the existing herdr-notify `send()` (click = the app,
no pane). down → running sends at most one `TENDRIL · HERDR RESTORED` per
incident. Requires 2 consecutive down observations (debounce ~30 s) before
notifying. The guardian NEVER restarts Herdr (notify + user-initiated
revive). Intentional stop ⇒ no incident, no notification.

### 6.3 Phone classification

On a snapshot failure (any TopologyFailure other than LAUNCHER_OUTDATED),
the phone runs `health` once (single-flight, 8 s timeout):
- health unreachable / transport failure ⇒ HOST_UNREACHABLE (existing state)
- herdr.state = down ⇒ HERDR_OFFLINE (recovery panel, primary REVIVE HERDR)
- herdr.state = stopped ⇒ HERDR_STOPPED (calm; no revive; instruction to
  run `tendril herdr start` on the host)
- herdr running but snapshot failed ⇒ keep the original failure
REVIVE HERDR runs `herdr restart`; success ⇒ immediate snapshot refresh
and the panel resolves into LIVE; failure ⇒ the safe phrase inline +
RETRY. Capability string: `health` (hosts without it ⇒ old behaviour).
## 7 · RC2 addendum (binding — physical-certification fixes)

Additive. Old phones ignore unknown keys; unknown enum words ⇒ UNKNOWN.

### 7.1 Agent & child provenance (flat keys, agents and children)

| key | meaning | rule |
|---|---|---|
| `provider` | upstream model vendor: `anthropic` `openai` `zai` `google` `mistral` … | canonical lowercase token; a bridge/proxy name is NEVER a provider |
| `adapter` | bridge/proxy between harness and provider, e.g. `claude-bridge` | only when the harness routes through one; `[a-z0-9._-]{1,24}` |
| `billing` | `metered` · `provider_managed` · `unknown` | `metered` only when the transcript's cost is a real per-call charge |
| `cost_usd` | dollars | emitted **only** when `billing = metered`. A bridge/subscription route (e.g. Pi via claude-bridge writes `cost.total = 0`; Claude Code on a claude.ai OAuth login) ⇒ `provider_managed`, no `cost_usd`. Zero cost with non-zero tokens and no explicit metering ⇒ `unknown`, no `cost_usd`. |
| `context_window` | tokens | only from an explicit local registry (Pi `~/.pi/agent/models-store.json` `contextWindow` for that provider+model, or bridge config that states it). **Never** a built-in table, never guessed from a model name. |
| `context_window_source` | `pi_models_store` · `bridge_config` · `transcript` | required whenever `context_window` is present |

Phone: `$x.xx` only for `metered`. `provider_managed` ⇒ "PROVIDER MANAGED" (+ "via <adapter>" when present). `unknown` / absent ⇒ "COST UNKNOWN". Context: `260K / 1M · 26%` when the window is present, else `260K · WINDOW UNKNOWN`.

### 7.2 State semantics (no inferred blockers)

Child `status` adds `stale`: no exit code, no terminal stop, quiet > `CHILD_ACTIVE_S`. `failed` ONLY on explicit evidence: non-zero `exitCode`, an explicit error stop reason, or an error as the child's final record with no later assistant turn. An earlier tool error followed by more work is NOT failure. Historical children are `done`, `stale` or `unknown` — never failed by inference.

Phone UI vocabulary: RUNNING · THINKING · TOOL · WAITING · NEEDS INPUT · DONE · FAILED · STALE · UNKNOWN · IDLE. `NEEDS INPUT` = herdr `status: blocked`. **BLOCKED is not derived from anything on the current wire** (a tool error is an event, never a state). It is reserved for a future explicit blocker field.

Events: `{"kind":"tool","tool":"bash","at":…}` = tool completed; `{"kind":"error","tool":"bash","at":…}` = that tool returned an error; `{"kind":"subagent","name":"worker","status":"done","at":…}` may carry the child name/status (optional).

### 7.3 Scheduler destination (existing vs new session)

`schedule add` JSON gains optional `"target"`:
- absent or `{"kind":"new"}` ⇒ current behaviour (new pane, `harness` required).
- `{"kind":"existing","pane":"w16:p5","harness":"pi","agent_started_at":1791580000}`. Host validates at add AND at launch: pane exists in `workspace`, hosts an agent of that harness, and (if given) the same `started_at` (pane-id reuse guard ⇒ FAILED `session changed`). At launch the agent must be idle or done; if working, the task stays READY and is retried each tick for ≤ 15 min, then FAILED `session busy`. herdr `status: blocked` ⇒ never dispatch (stays READY, same 15 min bound, then FAILED `session needs input`). Prompt via herdr's agent-prompt argv only; never typed into a terminal.
- Snapshot agents gain `"prompt_ready": true|false`, host-computed with the same rule (idle/done + known harness + agent present). The phone disables rows with a reason derived from status (Busy · Needs input · Not supported).
- Task objects echo `"target":"new"|"existing"`.

Pane ids are alphanumeric on real hosts (`w9:pB`, `wK:p1`): both ends validate `^[A-Za-z0-9]+:[A-Za-z0-9]+$`, never digit-only.

Latency: the snapshot's schedule section reconciles RUNNING tasks against the live herdr state it already fetched (RUNNING ⇒ DONE when the pane's agent is idle/done after `started_at`; ⇒ DONE `session ended` when the pane is gone), persisting under a non-blocking flock (skip if busy). The tick stays authoritative; the snapshot never launches.

### 7.4 Health v2 (`health --json`, additive)

```jsonc
"herdr": {"state":"running|down|stopped|mismatch|unknown",
          "client_version":"0.9.3", "server_version":"0.8.2",   // optional
          "binary":"recorded|detected|missing"},                // provenance word, never a path
"scheduler": {"state":"active|degraded|inactive|absent",
              "timer":"waiting|running|inactive|failed",
              "last_tick":"ok|failed", "last_tick_at":1791583000},
"guardian":  {"state":"active|degraded|inactive|absent", "timer":"…", "last_tick":"…"},
"capacity":  {"state":"available|adapters_disabled|error|absent",
              "enabled":["claude_local","claude_oauth"]}
```
A timer-driven oneshot whose service is inactive between ticks is HEALTHY (`state: active`, `timer: waiting`). `mismatch` = running server version ≠ recorded client version, or `herdr api` reports a protocol error.

Herdr binary: the installer resolves the user's real Herdr (`--detect`: protocol-verified newest, mise/asdf/brew/~/.local aware, `readlink -f`) and records `HERDR_BIN=<abs>` + `HERDR_VERSION=` in the TENDRIL config; every host component (snapshot, health, guardian, scheduler, restart, launch, notify) uses the recorded path. Services never depend on PATH. A recorded path that vanished ⇒ re-detect, `binary: detected`, health `degraded`. `/usr/bin/herdr` is used only if it IS the recorded binary.

`herdr restart` additionally: refuses on guard; stops only a TENDRIL-started stale/mismatched instance; starts the recorded binary; verifies server version == client version AND `api snapshot`; never leaves two servers.

### 7.5 Capacity provider status

When metrics cannot be classified, a provider carries
`"reason":"adapter_disabled|auth_error|network_error|stale|no_data|feature_missing"`.
Known adapter failures and stale samples use `state = unavailable`; partial
identity with no current quota uses `state = unknown`. For a disabled network
adapter, `enable` names the exact registered adapter id. The phone keeps the
provider, model, harness and last-seen facts visible, explains the unavailable/
unknown reason, and never invents a percentage.

### 7.6 Notification-driven invalidation (phone)

Any received TENDRIL notification (TASK STARTED/WAITING/FAILED, agent finished, HERDR OFFLINE/RESTORED) schedules one snapshot refresh (debounce 1.5 s, single-flight, skipped when the app is backgrounded beyond the existing policy; HERDR events also refresh `health`). Background polling cadence is unchanged.

## 8 · 0.2.6 addendum — session identity & failure evidence (binding, additive)

### 8.1 Session titles

`title_source` vocabulary adds `herdr`: `manual | herdr | native | scheduler | instruction | workspace` (`summary` is no longer emitted; phones keep parsing it). An unknown word is kept as unknown provenance — the title still stands.

Precedence (the host's `resolve_agent` is the single authority; first that yields wins):

1. `herdr` — the pane's explicit Herdr label (`herdr pane rename`; `panes[].label`). A scheduler routing token `tendril-<id8>` never counts.
2. `manual` | `native` — the MOST RECENT explicit human name: a TENDRIL rename (phone / `rename-session`) or the harness's own session name (Pi `/name`/`--name`, Claude custom title). A rename from the phone of a pane that already has a Herdr label also renames that label.
3. `scheduler` — the title of the scheduled task that CREATED the pane. A task sent to an existing session never names it.
4. `instruction` — the deterministic deriver on the first user turn (questions, statements, path-first and verb-only commands never derive). Cached per deriver version.
5. `workspace` — `<workspace label> · <Harness>`.
6. omitted — the phone falls back to the harness name.

A guess is never a name: the Pi extension no longer calls `setSessionName()` with a derived title; it only delivers host-queued names and appends a `{"type":"custom","customType":"tendril-title","data":{"source":"manual|scheduler","name":…}}` entry beside each. A Pi name that has the exact shape of a pre-0.2.6 extension guess (set once, no marker, the deriver's normal form, a contiguous run of the first instruction) is treated as a guess (`instruction`), never as `native`. Titles never contain the agent's current activity.

### 8.2 Child failure evidence

- A Pi `aborted` stop is `cancelled`, never `done`. `done` = ended normally with no error evidence.
- A `failed` child adds `"failure": "exit_code|error_stop|tool_error"` naming the evidence (§7.2 order), plus `"exit_code": <int>` for `exit_code`. Vocabulary only — never transcript text. Absent ⇒ the phone says "details unavailable", never a guessed cause.

### 8.3 Primary-session last-turn evidence

An agent row adds `"last_stop": "error|aborted"` when the session's LAST recorded assistant turn stopped with an error / was aborted, no newer prompt followed, and Herdr does not report it `working`/`blocked`. It is evidence about the last turn, not a claim that the session is dead; Herdr's own `status` is unchanged.
