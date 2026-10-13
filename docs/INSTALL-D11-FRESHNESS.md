# D11 — LIVE state freshness: install procedure (sequenced)

Worktree branches (never merged/pushed by the worker):

- Host: `~/Projects/tendril-wt/d11` — branch `final-polish-d11-freshness`
  (from `feat/link-0.2` @ 9aba5ee)
- App: `~/Projects/tendril-link-wt/d11` — branch `final-polish-d11-freshness`
  (from `final-polish` @ 9d868ea)

## Order matters (exactly once, in this order)

1. **App first** (old host + new app must stay safe):
   ```
   cd ~/Projects/tendril-link-wt/d11 && ./gradlew assembleDebug
   # install the APK on the phone (adb install -r or the usual side-load)
   ```
   The new app is a no-op against the old host: it sends no `--if-none-match`
   (capability-gated), and `TENDRIL · SYNC` is opt-in on the host so the
   topic stays quiet. The 2 s LIVE cadence and the moderate 10 s foreground
   cadence take effect immediately; the `behind` freshness word appears in
   the LIVE header. Nothing else about notifications changes.

2. **Host second** (new code already verified from the worktree; this makes
   it the installed runtime):
   ```
   cd ~/Projects/tendril-wt/d11 && ./install        # the repo's standard installer
   tendril service restart                          # restarts herdr-notify (reads notify.env once)
   ```
   What changes on the host:
   - `bin/herdr-notify`: 2 s LOCAL poll (herdr api snapshot is a local
     socket call — no network traffic; real alerts are detected sooner,
     publish VOLUME unchanged). `TENDRIL · SYNC` pushes are **OFF BY
     DEFAULT** and are for **SELF-HOSTED ntfy servers ONLY**: public
     ntfy.sh's anonymous daily quota (~250 msgs/visitor) is reserved for
     the real needs-you/finished alerts. Do NOT enable SYNC against
     ntfy.sh. To enable it deliberately against your own instance, set
     both in `~/.config/remote-agents/notify.env`:
     `NTFY_URL=https://<your-ntfy-server>` AND `NTFY_SYNC=1`
     (see `config/notify.env.example`). SYNC pings are MIN-priority; the
     updated app never surfaces them — they only trigger the refresh.
     With the default config the watcher publishes EXACTLY the
     pre-D11 set (needs input / finished / nudge / schedule / health).
   - `bin/tendril_telemetry.py`: a child's explicit terminal stop is `done`
     the moment it is scanned (was: ≥ 20 s quiet gate).
   - `bin/remote-agents`: `snapshot` documents carry `etag`; the verb
     accepts `--if-none-match <16hex>`; `capabilities` gains `"etag"`.
   - `phone/tendril`: `tendril [<alias>] snapshot [etag]` (validated; the
     no-arg form is byte-for-byte the old behaviour).

3. **Restart nothing else.** The snapshot/health/launch paths are unchanged
   argv-compatible; the scheduler and guardian keep running.

## Verify after install (host, read-only)

```
remote-agents snapshot --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["etag"], "etag" in d["capabilities"])'
remote-agents snapshot --if-none-match "$(remote-agents snapshot --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["etag"])')"
# second command must print one tiny line: {"not_modified": true, ...}
python3 tests/certify_freshness.py     # one full subagent lifecycle, per-stage table
```

## Physical-phone certification (what the host cannot measure)

With LIVE open on the phone, run `tests/certify_freshness.py` on the host
and watch the map: the child row should appear within ~1 poll of the host
table's stage C, the parent state word within ~1–3 s of stage B (the 2 s
cadence; a SYNC event only if you deliberately enabled it against a
self-hosted ntfy), and `done` within ~1–2 s of stage C-done. The phone's
`~/.cache/tendril/launch.log` (LATENCY.md) adds the RUN_COMMAND + SSH stage
times for the refresh flights; expected worst-case change-to-pixel ≈ 2.7 s
(2 s cadence + ~0.7 s flight).
