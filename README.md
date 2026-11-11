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

Requirements: Python 3.11+, Git, and [uv](https://docs.astral.sh/uv/).
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

## Existing WPO projects

WP files, configuration, ledgers, events, holds, inboxes, and old session logs
keep their existing formats. Native identities use additive sidecars.

On the first authorized run of an old WP, the backend can import its committed,
staged, unstaged, and untracked work into the clean T3 checkout. It preserves
the original checkout and stops on conflicts, unsafe linked paths, or partial
imports requiring reconciliation. Enabling the backend does not import or run
work by itself. WPO retains T3-owned checkouts after integration; T3 owns cleanup.

For the older execution workflow, explicitly set `execution_mode: standalone`
and read [the standalone guide](references/standalone.md). Its optional model
clients, proxy, usage reader, and account state are separate dependencies.

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

See [AGENTS.md](AGENTS.md) for source layout and change-specific checks.
`SPEC.md` is historical design context; current behavior is documented in the
skill and references. Keep machine config, credentials, account-synced skills,
transcripts, run artifacts, and private historical Git objects out of releases.

## License

[MIT](LICENSE).
