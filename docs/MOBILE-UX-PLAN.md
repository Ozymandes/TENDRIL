# Mobile UX fix plan

## Root causes

- `d4216fb` added a Ctrl+Home bridge, but enabled it only when the **host** sees `TERMUX_VERSION`. The actual Mosh process on horus has neither that variable nor `TENDRIL_DETACH_BRIDGE`, so the bridge is bypassed. The installed host executable already matches the checkout: this is a behavior bug, not a stale file.
- `4851417` is an ancestry-only merge, with no implementation tree change.
- The footer advertises letter shortcuts but `_read_tty_action()` buffers them until Enter. It also exposes redundant Project, Shell, Last, and TestPush actions.
- Replacing the executable does not replace a running selector. Mosh reconnect resumes the old process.

## Locked behavior

1. Enable the Ctrl+Home bridge for host TTY attachments by default, without relying on phone environment forwarding. Existing phone launchers must work unchanged over SSH and Mosh. Preserve `TENDRIL_DETACH_BRIDGE=0` (and existing false-like values) as an explicit host-side opt-out; `=1` remains supported. Non-TTY attachments retain direct passthrough. The inside-Herdr focus-only path remains unchanged.
2. Rewrite only `ESC [ 1 ; 5 H` to Herdr's bound Alt+D (`ESC d`). Preserve plain Home, other input, split-sequence handling, resize, EOF, child exit, and terminal restoration. Do not modify Herdr bindings.
3. Show only `N New`, `I Info`, `? Help`, `R Refresh`, and `Q Quit`, wrapping through one footer list at narrow widths.
4. Those five footer shortcuts dispatch immediately on a TTY, case-insensitively where applicable, without Enter. If digits are buffered, a footer shortcut discards them and dispatches. Multi-digit workspace numbers, backspace, arrows, and Enter attachment remain supported. Enter is for workspace selection and text fields, not footer actions. Non-TTY input remains line-oriented.
5. Remove P/S/L/T menu dispatches, not merely their labels. N opens a shell immediately in the selected/focused existing workspace directory, falling back to the console cwd (HOME if that cwd is unavailable). No name, directory, agent, or confirmation prompts. Omit `--label` and let Herdr name the shell workspace after its directory. Prefer the active pane's foreground cwd over cached agent cwd. Retain tested legacy helpers if useful; no broader cleanup is needed.
6. Info returns on any key. Help paginates to terminal height; any key advances or returns from the final page.
7. Do not provision Alt+Up as a Herdr workspace key; Pi uses it to edit steering messages. Back up the current Herdr config, remove only its previous-workspace Alt+Up assignment, validate, and reload config without restarting sessions.

## Implementation

- In `bin/remote-agents`, fix `_bridge_enabled()`, footer rendering, `_read_tty_action()` and its prompt, `menu_loop()` dispatch, `info()` return handling, and `help_screen()`.
- Update `tests/test_remote_agents.py` for default-on bridge without Termux variables, explicit opt-out, isolated environment-sensitive attach tests, immediate shortcuts (including buffered digits), removed dispatches, N shell-only creation, and any-key panels. Retain bridge PTY and workspace-directory regressions.
- Update `tests/test_masthead.py` to assert the exact new footer and phone geometry. Reduced footer height can legitimately leave more room for paths; assertions must test fit and masthead visibility rather than require needless path suppression.
- Update README, `docs/ANDROID.md`, `docs/ARCHITECTURE.md`, installer hints, and Unreleased changelog to match actual behavior. Document `herdr-notify --test` instead of the removed T shortcut and host-side bridge opt-out. Help must show both selector shortcuts and in-session detach keys.

## Verification criteria

```sh
python3 -m unittest discover -s tests -v
git diff --check
sh -n install phone/tendril phone/agent
```

- Tests pass both inside the current Herdr environment and with `HERDR_ENV` removed; attach unit tests isolate their environment rather than accidentally waiting for real input.
- A PTY attachment with neither `TERMUX_VERSION` nor `TENDRIL_DETACH_BRIDGE` enables the bridge and translates Ctrl+Home. Explicit bridge-off uses the direct attach path. Preserve the existing split-sequence, byte-transparency, resize, child-exit, EOF, signal, and terminal-restoration tests.
- Each supported footer key returns without an Enter byte, in either letter case and after buffered digits. Numbers plus Enter and arrow/Enter selection still work. P/S/L/T cannot create, attach, or send a test push through the menu.
- Footer labels are exactly the five supported actions, wrap without overflow, and fit phone widths. The prompt fits 34 columns without an extra blank row. Masthead and path behavior are checked against actual available height, including the prompt at 48×22. Help pages and any-key prompts fit 34-column screens at 22/31 rows.
- N opens a native shell without any input/agent chooser/confirmation/startup calls, using selected/focused directory or console cwd fallback. Test active-pane cwd priority and Herdr CLI invocation without `--label`; confirm native naming in a named scratch session.
- The installer no longer adds previous_workspace=alt+up; help/docs no longer advertise Alt+Up as workspace switching. The live config validates and reloads with only that binding removed.
- Documentation/help describe the same shortcuts and default-on host bridge. No claim of real phone acceptance unless physically tested.

## Deployment, commit, and push

The user has authorized implementation, installation, commit, and push to the existing `main` branch; no additional approval gate or dependency upgrade is implied.

1. Review the complete diff and run automated tests plus a clean-environment PTY smoke test.
2. Back up `~/.local/bin/remote-agents`; stage and rename the tested executable into that location. Confirm it matches the checkout. Do not rerun the interactive installer, overwrite config, change the ntfy topic, or restart Herdr/the notification watcher. The phone launcher needs no update for this fix.
3. Compare pre/post checksums of host configuration, notification configuration, and watcher executable; confirm watcher remains active and existing workspaces/panes remain present. For the authorized Alt+Up correction, verify Herdr config differs only by removal of the previous-workspace assignment, validates, and reloads live.
4. Commit the plan, implementation, tests, and updated documentation to `main`; push to `origin/main`. GitHub port 22 is blocked on this host, so use SSH to `ssh.github.com:443` via a per-command `GIT_SSH_COMMAND` override, without changing the repository remote.
5. Tell the user to detach with Alt+D or Ctrl+B then D, quit the old selector with Q (old builds require Q then Enter), and run `tendril` again. Mosh reconnect alone cannot reload it. The new build uses Q alone.

## Phone acceptance still requiring the device

After restarting, check Ctrl+Home over both `tendril` (Mosh) and `tendril ssh`, immediate footer keys, and layout with the keyboard open/closed. Ctrl+Home must emit `^[[1;5H` in a plain Termux `cat -v` test. If the device emits different bytes or Alt+D is custom-bound, record that evidence rather than broadening translation blindly. Detaching must leave agent workspaces running.

## Initial implementation review and verification (8346747)

- Luna Max workflows produced the initial diagnosis/plan, implementation, documentation updates, and independent review. Review identified keyboard-open layout overflow, missing clean-environment end-to-end bridge coverage, and a wrapping prompt; these were corrected before deployment.
- The complete suite runs 85 tests successfully (one pre-masthead origin comparison is skipped) with both inherited `HERDR_ENV=1` and `HERDR_ENV` removed. `git diff --check` and launcher/installer shell syntax checks pass.
- An independent PTY smoke test against actual Herdr 0.9.3, in a named scratch session, confirms Ctrl+Home detaches without Termux/bridge environment variables and restores terminal settings. The scratch session was stopped and deleted; existing workspaces were not used for this test.
- A read-only live-selector PTY smoke confirms the simplified footer, Info/Help/Refresh/Quit without Enter, any-key help pagination, and terminal restoration.
- The final Luna Max claim audit independently verified the bridge gate, keyboard-open layout, immediate shortcuts, clean-host end-to-end PTY regression, and paginated help; no release blocker was found.
- The tested executable was installed atomically to `~/.local/bin/remote-agents`, with the previous executable backed up. Installed bytes match the checkout. Configuration/notification/binding/watcher checksums are unchanged, the watcher remains active, all eight existing workspaces and 27 panes are preserved, and the installed selector passes the read-only PTY smoke. `install --doctor` reports the expected healthy dependencies and services.
- Physical Android/Mosh/SSH acceptance remains a device-side check after the old selector is restarted; it has not been claimed as performed.

## Follow-up corrections

The user clarified that New must use Herdr's standard shell behavior, without a wizard, and reported Alt+Up intercepting Pi's steering-message edit key. These supersede the initial chooser behavior and the initial requirement to leave every Herdr binding untouched. Native Herdr 0.9.3 was checked in a named scratch session: creating a workspace with only `--cwd` derives its label from that directory. The current `previous_workspace = "alt+up"` assignment was backed up, removed, validated, and live-reloaded successfully; other bindings and running workspaces were preserved. An independent named-scratch smoke also called the real new-workspace implementation against Herdr: it created a plain shell without prompts or agent startup, named it after the selected directory, and retained that cwd. The scratch session was stopped and deleted.

- Follow-up suite: 87 tests ran successfully, one skipped, with both inherited and unset `HERDR_ENV`; diff and shell syntax checks pass.
- The updated selector is installed and matches the checkout. Its native-shell and live-menu PTY smoke checks pass.
- Only the authorized Alt+Up assignment was removed from Herdr config. All eight existing workspaces and 27 panes, other config/notification files, and the active watcher were preserved.
