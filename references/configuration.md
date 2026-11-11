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
  required by default. Configure your own headings and paths explicitly; ordered multi-file
  `user_rules` entries can also carry whole files, as documented below.
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
  `routes` replaces the list; `routes_extra` appends after that replacement at each layer,
  independently of JSON key order. Route restriction lists are described below.

### Route restrictions

All four settings below are lists of nonempty strings under `routing`. Lists replace the
previous layer, so project `routes_exclude: []` clears machine exclusions.

- `routes_exclude`: remove exact route IDs or every route matching `family:<name>`. Applies to
  static routes, dynamically discovered free routes, and explicit pins. Excluded routes never
  influence the family Pareto frontier, and pinning one raises an error. This changes execution
  selection, not stored catalog/history: `find_route` still resolves configured/cached metadata
  for reports and failure marks; `all_routes` returns only non-excluded routes.
- `gate_only_routes`: exact route IDs reserved for reviews and re-evaluations (`gate=True`).
  Builders cannot use them automatically or by pinning. Gates may rank them automatically;
  unavailable gate-only routes do not remove builder alternatives from its Pareto frontier.
- `pin_only_routes`: exact route IDs available only through explicit pins, including for gates.
  Combining this with `gate_only_routes` requires both a pin and a gate session.
- `pin_only_efforts`: efforts available only through pins; non-local gate sessions may never
  use these efforts, even when their route is gate-only and explicitly pinned.

Restrictions apply together. Neither a pin nor a gate overrides an exclusion, a usage ceiling,
or native provider availability. Family selectors are supported only in `routes_exclude`;
the other route lists require exact IDs. For example:

```json
{
  "routing": {
    "routes_exclude": ["family:agy", "gpt-sol-low"],
    "gate_only_routes": ["claude-opus-high"],
    "pin_only_routes": [],
    "pin_only_efforts": ["max"]
  }
}
```

### Carried user rules

`user_rules` accepts an ordered list of file objects. Each `file` is an explicit path (with
optional `~` expansion). Omit `sections` or set it to `null` to include the whole file; set
`sections` to a list of exact `##` heading names to include those sections in the chosen order.
Nested `###` content stays with its section. An empty `sections: []` disables that entry without
reading its file. An empty top-level list disables all carried rules.

```json
{
  "user_rules": [
    {"file": "~/.agents/AGENTS.md", "sections": ["Correctness", "Shell and links"]},
    {"file": "~/.agents/RTK.md"}
  ]
}
```

Entries and sections retain their order. Missing active files or headings and malformed values
raise errors naming the configuration field. The legacy single-object form remains supported:
`{"file": "...", "sections": ["Heading"]}` includes selected sections, while an empty,
omitted, or `null` `sections` disables it. Whole-file inclusion requires the new list form.
Existing single-object deployments keep their
disabled-by-default behavior.

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
