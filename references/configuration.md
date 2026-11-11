# Portable configuration

The skill is source and reusable guidance. Never commit your live machine config, API tokens,
model-proxy login state, session transcripts, run logs, or account usage history into it.

## Machine settings

`WPO_MACHINE_CONFIG` selects a JSON file; otherwise the CLI reads
`~/.claude/wp-orchestration.json` for compatibility with existing installations. The file is
outside the skill repository and every key is optional. Nested objects merge; lists replace.
Copy [templates/machine.example.json](../templates/machine.example.json) for a portable example.
The full defaults and accessors are in [wpo/machine.py](../wpo/machine.py).

- `execution_mode`: `t3` is the default native coordinator backend; `standalone` explicitly runs
  legacy model subprocesses. Changing the setting does not stop or start existing project work.
- `t3`: supported CLI path, optional T3 data directory, provider/model aliases and options, wake
  debounce, callback interval and optional coordinator-idle timeout. Repository attachment lives
  in its ignored run state. See [t3-bridge.md](t3-bridge.md) for attachment, callbacks and recovery.
- `python`, `claude`, `agy`, `opencode`, `shim_dir`: executable and installation locations.
  Commands resolve on PATH; paths may use `~`. `python` defaults to the running interpreter.
- `state_dir`, `usage`: local router state and an optional external usage-budget adapter, cache,
  and history. The adapter is not bundled and Claude hooks do not run under T3 automatically.
  Without verified subscription usage, the legacy router may withhold those routes.
- `skills.dirs` / `skills.paths`: skill discovery roots and explicit `SKILL.md` mappings.
  Portable defaults search `.agents`, `.codex`, then `.claude` under the home directory.
- `reviewer_agent`, `user_rules`: optional reviewer definition and selected sections of global
  instructions copied into legacy briefs. No personal skill or named instruction section is
  required by default. Configure your own headings and paths explicitly.
- `gpt_proxy`: proxy port, executable, config, token-file path, model-pins file, background tier,
  and optional context-window corrections. The existing adapter expects a loopback CLIProxyAPI
  speaking Anthropic messages with `gpt_tiers` pins; it is not a generic T3 provider adapter.
- `local_model`: loopback model/shim ports, token-file path, context/output limits, and mutex.
  Supply the actual local model route in `routing.routes_extra`; no local model is assumed.
- `keys`: token-file paths for optional OpenRouter and Artificial Analysis integrations.
  Configure file paths, not secret values. `OPENROUTER_API_KEY` is also supported.
- `dashboard`: autostart, loopback port, and per-project ports. Both backends retain the dashboard.
- `project_defaults`: personal project-wide defaults such as `builder_skills`, workers,
  autofix policy, context limits, or machine environment notes. Project JSON wins over these.
- `routing`: model routes, tier floors, family preference, effort policy, quota ceilings and
  weekly pace. Machine routing overlays the bundled snapshot; project `routing` overlays that.
  `routes` replaces the list; `routes_extra` appends at each layer.

Bundled model IDs, scores, and prices are example snapshots with dates, not promises of current
availability. Put your actual deployed model catalog and preferences in machine `routing`.
For a fully private catalog, override `routes`; verify T3 choices separately with its live
capability tool. Machine config is loaded for new WPO processes; restart any long-running legacy
supervisors to adopt changes when the user intends to do so.

## Project settings

`.orchestration/config.json` contains WP locations, verification commands, owned/shared paths,
lanes, integration hooks, and repository rules. Start with
[templates/config.example.json](../templates/config.example.json). Defaults are documented in
[wpo/project.py](../wpo/project.py). Use relative paths and keep local tool paths, account policy,
and private environment notes in machine configuration.

Precedence is built-in defaults → machine `project_defaults` → project JSON. Routing additionally
starts with `models/tiers.json`, then machine `routing`, then the resulting project's `routing`.
Project settings reject unknown keys, including unknown machine `project_defaults` keys.

## Sharing on GitHub

The source tree uses generic paths and optional integrations. `SPEC.md` is a historical design
example, not installation configuration. Review it, model snapshots, license and
dependencies, and all tracked files before publishing. Removing paths from the current tree
does not remove them from Git history: review history or publish a fresh repository when
history cannot be shared. WPO's source and skill are provided under the root MIT license;
external clients, account-synced skills, and integrations retain their own terms.

The Python CLI needs `psutil`; legacy live routes also need their corresponding external clients,
login state, proxy, skill definitions, and optional usage adapter. The tests use scripted agents
and scratch repositories. They do not prove a particular hosted provider or T3 integration.
