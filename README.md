# WPO — Work-package orchestration

WPO coordinates coding agents through work packages with explicit ownership,
dependencies, verification, review, and integration. Its local dashboard shows
lanes, progress, findings, approvals, sessions, and run history.

T3 Code is the default execution backend. T3 creates the checkouts and runs
native delegated agents; WPO keeps the scheduler and project state. An explicit
standalone backend supports the older subprocess-based workflow.

This is an early release. Native bridge regression tests pass, but the complete
live T3 worktree lifecycle still needs acceptance in a real project. See the
[compatibility matrix](references/t3.md) for supported behavior and limitations.

## Install

Requirements: Windows or Linux, Python 3.11+, Git, and [uv](https://docs.astral.sh/uv/).
Other operating systems cannot currently establish the boot fence required by
commands that load project runtime state.
T3 execution also requires a running T3 Code installation and an available
native provider account. WPO does not supply provider authentication.

From a clone of this repository:

```sh
uv tool install .
wpo --help
```

For development, use `uv sync --locked` and `uv run wpo --help`.
The installed CLI includes its dashboard, templates, model snapshots, and skill
references. `python -m wpo` is also supported in an environment with WPO installed.

To expose the skill to both Claude and Codex, place or link the cloned repository
at `~/.agents/skills/wp-orchestration`, then link that same folder from
`~/.claude/skills/wp-orchestration`. Keep one source directory. Restart the native
agent session after changing skill discovery. A skill link does not install the
CLI or connect MCP servers.

## Configure a project

From your project's main Git checkout:

```sh
wpo init --name my-project
```

Edit `.orchestration/config.json` for the WP directory, lanes, verification
commands, owned/shared paths, and integration policy. Create WPs using
[the template](templates/WP-TEMPLATE.md), then commit the project configuration,
WP files, and `.gitignore` before scheduling work.

Machine settings belong outside the repository. Copy
[machine.example.json](templates/machine.example.json), configure only the
integrations you use, and select it with `WPO_MACHINE_CONFIG`. Without that
variable, WPO reads `~/.claude/wp-orchestration.json` for compatibility.
The default `execution_mode` is `t3`.

If T3's CLI is outside PATH, set `t3.cli` to its actual launcher. On Windows,
the desktop normally installs `~/.t3/bin/t3.cmd`; use an absolute path in JSON.
Provider IDs, model aliases, effort, executable paths, quota policy, optional
usage adapters, and dashboard ports are configurable. Model scores/prices in
`models/` are dated example snapshots, not current availability guarantees.

See [configuration](references/configuration.md) for precedence and all settings.
Machine routing can exclude individual routes or whole families and reserve
routes for reviews. Ordered `user_rules` entries can include selected sections
or whole instruction files from the shared source.

## Use with T3 Code

Open a T3 thread in the actual repository and ask it to use `wp-orchestration`.
The agent reads [the bridge operations](references/t3-bridge.md), records live
T3 capabilities, and attaches that existing repository thread as coordinator.
Attachment starts no lanes and changes no holds.

When you authorize a run, the coordinator delegates a T3-owned checkout for each
WP and fresh native builder/reviewer/worker tasks in that checkout. WPO retains
its verification, findings, dependency scheduling, and integration gates.
Native completion notifications drive callbacks; the coordinator must honor
T3's returned `next` and must not poll running tasks.

Useful commands after attachment:

```sh
wpo t3 status
wpo lint
wpo dashboard
```

Dashboard startup is local to the configured loopback port. Starting a dashboard
does not authorize resuming lanes. `wpo t3 recover` wakes outstanding coordinators
once without blindly relaunching tasks or releasing holds.

Important boundaries:

- T3 must be running, and its attached coordinator needs provider availability.
  A Python daemon cannot impersonate a live native provider thread.
- Native tool permissions and approval prompts belong to T3. WPO carries role
  scopes and checks repository mutations after scoped tasks; this is not the
  old per-command CLI sandbox. Scoped workers serialize within a WP checkout.
- Continuous tool, usage, context, and compaction telemetry depends on real
  callbacks. Missing usage stays unknown. Hard pre-compaction interception is
  not available universally.
- Disabled or unsupported native providers are skipped with an explanation.
  WPO never silently falls back to standalone subprocess models.

## Continuous operation

Continuous mode is opt-in. It uses the existing router, lane limits, build slots,
claims, native bridge and verification gates. The Python scheduler checks local
WPO state; it never polls a native task or spends a model turn on each tick.

```sh
wpo continuous start --lane physics --lane contact
wpo continuous status
wpo continuous wait WP-0001 --reason "Choose between two product behaviors"
wpo continuous continue WP-0001 --reason "Reviewed the evidence" --milestone "Deliver one measured vertical slice"
wpo continuous progress WP-0001 --summary "Saved the actual before/after recording"
wpo continuous stop --reason "User stopped project work"
```

Omit `--lane` to authorize all WP lanes. Explicit lane holds and unrelated pause
markers remain binding. A human decision blocks only its WP; independent ready
work can fill freed capacity within quota and ownership limits. A stopped project
requires an explicit `start`. `continue` requires the old native task and claim
to be reconciled, a concrete smaller milestone and the reason for the decision.
It preserves intervention history and does not waive blocked status, dependency,
ownership, review or integration checks.

Elapsed time, repeated failures and missing useful checkpoints trigger a safe
wrap-up and durable coordinator attention. The coordinator inspects actual
results, changes/splits stuck work under the user's authorization, or asks a
material product question while other work proceeds. Heartbeats alone are not
evidence of useful progress. Progress reports do not extend the time boundary.
No available quota, ready work or live native coordinator means progress can
wait; `status` records the scheduling reasons. Keep T3 and the coordinator usable.

See [continuous configuration](references/configuration.md#continuous-operation)
for limits and restart behavior.

## Existing WPO projects

WP files, configuration, ledgers, events, holds, inboxes, and old session logs
keep their existing formats. Native identities use additive sidecars.

WPO records the host and OS boot identity before inspecting persisted process
records. After a host change or reboot, it archives and invalidates known local
PID records and interrupts stale worker state. Holds, pauses, history, and native
T3 task identities remain intact; no process is stopped by this cleanup. The
first adoption records a baseline and preserves legacy PIDs whose origin cannot
be verified. Keep one active host per runs directory. This fence currently
requires Windows or Linux and does not detect PID reuse within the same boot.

On the first authorized run of an old WP, the backend can import its committed,
staged, unstaged, and untracked work into the clean T3 checkout. It preserves
the original checkout and stops on conflicts, unsafe linked paths, or partial
imports requiring reconciliation. Enabling the backend does not import or run
work by itself. WPO retains T3-owned checkouts after integration; T3 owns cleanup.

For the older execution workflow, explicitly set `execution_mode: standalone`
and read [the standalone guide](references/standalone.md). Its optional model
clients, proxy, usage reader, and account state are separate dependencies.
Weekly audit installation supports standalone Windows tasks and Linux systemd
user timers. T3 projects use native scheduling in the existing coordinator
thread. Inspect [scheduling](references/scheduling.md) before installing; a Linux
persistent timer can immediately run a missed audit.

## Development and verification

```sh
uv sync --locked
uv run python -I -B -m unittest discover -s tests
uv build
uv run --isolated --no-project --with dist/wp_orchestration-0.1.0-py3-none-any.whl python -I tools/check_install.py
```

Tests use scripted agents and temporary repositories. They do not spend model
quota or establish hosted-provider/worktree readiness. Windows is the primary
development platform; Linux has matching platform primitives and CI coverage.
The installed-artifact check verifies assets, both CLI entry paths, and project
initialization outside the source checkout.

For dashboard changes, run `uv run python -I -B tools/check_dashboard.py` and
open its loopback URL in the browser. It serves the real dashboard assets with
synthetic snapshots and an in-page transport: actions are recorded in memory,
and no project, lane, coordinator, provider or model is contacted. In the browser
evaluate `runDashboardChecks()` (it returns a promise), then inspect the rendered
page and details at 360, 480, 640, 768, 1024 and 1440 px, including a viewport
480 px tall. The checks cover reflow, filtering and grouping, refreshes during
typing, focus and selection, disclosures, cooldowns, action payloads and errors,
and empty/loading/detail states. Stop the fixture server when finished.

To run these checks with exact CSS viewports and real keyboard input, install a
Playwright Chromium browser and run
`uv run --with playwright python -I -B tools/check_dashboard.py --check --port 0`.
Use `--browser-executable <path>` for an existing Chromium installation,
`--evidence-dir <directory>` to choose where screenshots/results go, and
`--baseline <revision>` to capture the same synthetic data with older Git assets.
Evidence defaults to a temporary directory outside the checkout. The browser
check can also run against an installed wheel using the isolated command above
with `--with playwright` and `tools/check_dashboard.py --check --port 0`.

See [AGENTS.md](AGENTS.md) for source layout and change-specific checks.
`SPEC.md` is historical design context; current behavior is documented in the
skill and references. Keep machine config, credentials, account-synced skills,
transcripts, run artifacts, and private historical Git objects out of releases.

## License

[MIT](LICENSE).
