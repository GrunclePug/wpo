# Weekly audit schedules

`audit.schedule` remains `{ "day": "MON", "time": "09:00" }`: one local weekday
(`MON` through `SUN`) and a 24-hour `HH:MM` time. Scheduling never releases lanes.
`wpo audit` itself remains usable manually in either backend.

## T3 backend

Use native T3 scheduling from the existing repository coordinator thread. With
`execution_mode: t3`, `wpo audit --install-schedule --show-only` prints a native
plan; omitting `--show-only` returns an actionable error and changes nothing.
Removal prints guidance rather than guessing which native task to delete.

The current live `schedule_task` contract uses:

```json
{
  "bindToCurrentThread": true,
  "title": "Weekly WPO planning audit",
  "prompt": "Run wpo audit for this repository. Read the WPO skill and honor existing holds and pauses; do not resume lanes.",
  "schedule": { "type": "fixed_time", "timeOfDay": "09:00", "weekdays": [1] }
}
```

Weekdays use Sunday = 0 through Saturday = 6. Verify the live tool contract before
creating a task, retain its returned `scheduledTaskId`, and report `nextRunAt`.
Keep `bindToCurrentThread: true`; an audit schedule does not require a top-level
thread. Remove that specific task with native `delete_scheduled_task`, or pause it
with `schedule_task` and `enabled: false`. The CLI never calls those tools or
creates native schedules automatically. T3 controls native delivery and offline
behavior; do not promise systemd's missed-run catch-up semantics for T3 schedules.

## Standalone Linux

Select `execution_mode: standalone` explicitly, then inspect before installing:

```sh
wpo audit --install-schedule --show-only
wpo audit --install-schedule
wpo audit --remove-schedule --show-only
wpo audit --remove-schedule
```

The installer writes a user `.service`, `.timer`, and hidden ownership receipt in
`$XDG_CONFIG_HOME/systemd/user`, or `~/.config/systemd/user` when unset. Relative
`XDG_CONFIG_HOME` is rejected. It runs `systemctl --user daemon-reload` and
`systemctl --user enable --now <timer>`. The oneshot service runs the configured
absolute Python interpreter and WPO entrypoint with the absolute project root.
Tokens are quoted for systemd, including literal `%`, `$`, quotes and backslashes;
control characters are rejected. No shell interprets the command. Explicit
`WPO_MACHINE_CONFIG` is recorded in the service environment; otherwise normal
machine-config discovery applies. Runtime dependencies must be available to the
user service; interactive shell initialization is not run.

`Persistent=true` coalesces missed runs into a catch-up run when activated.
Installation can therefore start an audit immediately. Only explicit install does
this; loading WPO or the skill never installs a timer. The user systemd manager must
be available. Logout persistence depends on user-manager/linger policy; WPO never
enables linger or requests root.

Removal disables/stops only the timer, cleans its persistent timestamp, deletes
owned unit files/receipt, and reloads the user manager. It does not cancel an audit
service already running. Inspect it with `systemctl --user status <name>.service`
or `journalctl --user -u <name>.service`, using the name printed by the plan.

## Standalone Windows

The same commands register/remove a weekly current-user Task Scheduler task using
`schtasks`, retaining the existing weekday/local-time semantics and 261-character
command limit. First installation omits `/F`: a failed query cannot authorize
replacing an unknown task. Replacing/removing a task requires its matching WPO
ownership receipt. Receipts and serialized administration locks live under
`<machine.state_dir>/schedules`.

Windows tasks cannot inherit an invocation's `WPO_MACHINE_CONFIG`; automatic
installation with that override is refused. Use default machine configuration or
manage an explicit wrapper through Task Scheduler yourself. Never put credentials
in a task command. Earlier name-only tasks such as `wpo-audit-project` remain
untouched: inspect and retire them manually before enabling a replacement.

## Ownership, recovery and limits

Names combine a sanitized project name and a 16-character hash of the resolved
main checkout path, so equally named repositories have distinct schedules. Moving
a checkout creates a new identity; remove its old schedule before moving, or
inspect and remove it manually afterward.

Unowned files/tasks, symlinked owned files, missing/malformed receipts, edited unit
contents, changed Windows task XML, and shadowing systemd units are refused. A
receipt authorizes only this project's exact resources. WPO never adopts/deletes
arbitrary scheduler entries. An unchanged install is a no-op; it does not silently
re-enable a timer an operator disabled outside WPO.

Before replacement/removal, previous files/Windows XML are backed up under
`.wpo-schedule-backups/<schedule>/<timestamp>` beside Linux units or Windows
receipts. On failure WPO restores prior files/receipts and attempts prior manager
enable/active state; Windows rollback imports its previous XML. If manager recovery
also fails, the error requests manual attention and backups remain. Rollback cannot
undo an audit already triggered by activation or restore a cleaned catch-up
timestamp.

Generated OS commands carry an internal standalone marker. If settings later
switch to T3, invocation exits before loading run state or waking a coordinator.
Remove the OS schedule and configure native scheduling deliberately.

`--show-only` runs no scheduler commands and creates no units, receipts, locks, run
state, router state, dashboard or backups. It prints the selected platform's plan.
macOS/other OS installers are unsupported; use native T3 scheduling or configure
a scheduler manually.

Primary systemd references:
[command lines](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html#Command%20lines),
[unit load paths](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html#Unit%20File%20Load%20Path),
[timers](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html),
[calendar syntax](https://www.freedesktop.org/software/systemd/man/latest/systemd.time.html).
