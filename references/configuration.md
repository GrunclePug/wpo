# Portable configuration

## Build output and disk space

`remove_after_merge` lists per-WP build directories. `{wp}` expands to the WP ID and
`{worktree}` to its actual checkout, including the registered T3 checkout. For Rust projects,
`"remove_after_merge": ["{worktree}/target"]` cleans compiled output after a successful merge
while retaining T3's checkout, branch, source changes and orchestration history. External
per-WP paths such as `C:/Build/target-{wp}` remain supported. Paths must contain one of those
tokens; normalized external paths must stay within their WP-specific directory. Shared paths,
checkout roots, foreign/nested repositories, Git data, linked paths and tracked content are refused.
Output inside its own checkout must be Git-ignored; orchestration history is always protected.
An unsuccessful removal is reported as `cleanup-failed`; it does not undo the merge.

`wpo clean-builds` reports configured cleanup for WPs already done on main; `--wp WP-0001`
limits the report, and `--apply` removes that output. Open/pending WPs, held unfinished work,
claimed WPs and outstanding native sessions (including stalled ones) are protected. The command
does not remove migrated source checkouts or historical logs; retire those explicitly only after
verifying migration and preserving unique uncommitted work.

`minimum_build_free_gib` is an optional free-space floor on the checkout's volume before a WP
runner starts. For example, `20` requires 20 GiB free. `null` disables it. Insufficient space
blocks the WP and raises an attention event before a model launches, without cooling model routes
or deleting anything. After freeing space, unblock and resume the WP explicitly. This is a
pre-run check, not a reservation or continuous disk monitor; explicit external build volumes
may need their own space checks.

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
  `t3.native_instructions` says which native sessions load instruction files themselves, below.
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
- `dashboard`: autostart, listen address, allowed page addresses, port, and per-project ports.
  Both backends retain the dashboard. It listens on `127.0.0.1` by default. To open it from a
  phone on the same network, set `host` to `0.0.0.0` and list the computer's LAN IPv4 address in
  `allowed_hosts`, for example `{"host": "0.0.0.0", "allowed_hosts": ["192.168.1.20"]}`.
  `wpo dashboard` then prints both the loopback and LAN URLs. Permit that port through the host
  firewall only for the trusted local subnet. LAN clients have the dashboard's normal controls;
  exact Host/Origin matching and the per-start action token still apply. Wildcard binding without
  explicit allowed addresses is refused. Host/address changes replace the dashboard server on
  the next WPO command; they do not restart lanes. Keep the configured addresses and firewall
  scope current if the computer's LAN address changes.
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

### Quota groups

`routing.quota_groups` holds a provider's routes back before the provider itself refuses them.
`groups` maps a quota group (a route's `provider`, such as `agy-gemini`) to the usage `family`
whose windows hold its buckets and the `buckets` (window labels) it spends, as the usage adapter reports
them; `ceilings` gives `five_hour` and `weekly` limits in percent used. Once any bucket of a group
is at or past its ceiling, that group's routes wait until the bucket resets (a window of five
hours or less counts as five-hour). Unknown usage blocks nothing: a refused session then cools
down what the router's failure handling already cools (for agy, the whole provider). Malformed
settings fail with an error naming the setting.

```json
{
  "routing": {
    "quota_groups": {
      "groups": {"agy-gemini": {"family": "agy", "buckets": ["gemini-5h", "gemini-weekly"]}},
      "ceilings": {"five_hour": 70, "weekly": 85}
    }
  }
}
```

The usage adapter (machine `usage.script`, not bundled) writes the cache at `usage.cache`; wpo
reads it as JSON and runs the script when the cache is older than `usage.max_age_seconds`. A
refresh that leaves the cache stale is not retried by every waiting supervisor in turn, and for
routing a family's reading older than `usage.max_reading_age_seconds` (1800; its own `fetched_at`
when given, else the snapshot's) counts as unknown, which blocks Claude and GPT routes. Quota
groups still read stale windows, since there a reading can only block; the dashboard shows them all:

```json
{
  "fetched_at": 1762800000.0,
  "families": [
    {"family": "agy", "error": null, "windows": [
      {"label": "gemini-5h", "used_percent": 42.0, "resets_at": 1762810000.0, "window_seconds": 18000}
    ]}
  ]
}
```

`fetched_at` and `resets_at` are epoch seconds; a family with an `error` counts as unknown. The
subscription families (`claude`, `gpt`) are read the same way for ceilings, pace and headroom;
any other family's windows serve only quota groups.

### Inspecting routes

`wpo route candidates --spec <tier>` ranks a tier as a builder sees it; `--gate` ranks it as a
review or re-evaluation does (gate-only routes included, no free last resort). `wpo route status`
lists each tier's first choices, plus an `as a review` line wherever reviews rank differently.

### Listed tiers and routing modes

`routing.tier_routes` gives a tier an explicit route list instead of its score floors. Each entry
holds `routes`, ordered groups of exact route IDs; optional `fallback` groups, served as the tier's
downgrades (and all a family over its weekly pace may serve); and `free_last_resort` (default
`true`). Groups are tried in order. Within a group the subscription family with the most quota
headroom goes first (percentage points left before its tightest window stops it), routes that
spend no subscription before it, and Claude first for a `+taste` spec. Listed routes skip the
family Pareto frontier but keep every restriction above; IDs that name no route are skipped and
reported by `wpo route status`. An empty entry lists nothing. Tiers without an entry keep floors.
A listed tier still needs its `tiers` entry, for `writes_code`; its floors are read only where the
tier is not listed. A later layer (machine over bundled, project over machine) replaces a tier's
entry whole rather than merging into it. Malformed entries fail with an error naming the setting.

`routing.modes` maps a mode name to its own `tier_routes`, which replace the standard entries
tier by tier. `wpo route mode [standard|<mode>]` shows or sets a project's mode; each WP records
the mode in which its first route was chosen and keeps it for every later session, repairs and
delegated workers included. Specs then carry it as a suffix, such as `code-hard@hard`. Ad-hoc
sessions (`wpo dispatch`, placements, audits) and `wpo route candidates` use the project's mode
unless their spec names one. A mode the
configuration no longer defines routes as `standard` (`wpo route mode` says so); a mode name may
not be `standard` or contain `@`.

```json
{
  "routing": {
    "tier_routes": {
      "code-hard": {"routes": [["claude-opus-medium", "gpt-sol-high"]], "fallback": [["claude-opus-low", "gpt-sol-medium"]]}
    },
    "modes": {"hard": {"tier_routes": {"code-hard": {"routes": [["claude-opus-high", "gpt-sol-xhigh"]], "free_last_resort": false}}}}
  }
}
```

### Independent reviews

With `routing.independent_reviews: true`, a WP's review never runs on a model from the family
that last built it. A route's `model_family` names who made its model (an agy route can run a
Claude model: `"family": "agy", "model_family": "claude"`); without it, the route's `family`
is used. The runner records the model family of each builder session, and the review's
selection leaves out every route of that family, before ranking and before the family Pareto
frontier. When no route of another family is available, the review waits for one rather than
falling back to the builder's family. A pinned reviewer (`reviewer: pin:<route>`) is an explicit
choice and is exempt. Re-evaluations are not affected. A WP whose builder ran before the setting
existed is judged by its last recorded builder session; with none recorded, its review runs
unrestricted and the runner log says so. The default is `false`.

```json
{
  "routing": {
    "independent_reviews": true,
    "routes_extra": [
      {"id": "agy-opus-high", "family": "agy", "model_family": "claude", "harness": "agy",
       "model": "claude-opus-5-5", "effort": "high", "provider": "agy-thirdparty", "score": 54, "writes_code": true}
    ]
  }
}
```

### Native instruction files

A native T3 session may load instruction files on its own: Claude Code reads the user's global
`CLAUDE.md` and every `CLAUDE.md` from its checkout up, and Codex can be configured to read them
too. Its brief would then carry the same rules twice. `t3.native_instructions` names the route
`families` whose sessions do this, the file `names` they discover in their checkout and every
directory above it, and fixed `files` they always load. For a session on one of those families,
the brief leaves out the project's instructions file when the checkout has it, and any carried
`user_rules` entry or skill whose file is one of those (compared after resolving links). Other
families, and every harness outside T3, keep the whole brief. Each family's discovery must
really reach those directories; check what a native child session reports before listing it.

```json
{
  "t3": {
    "native_instructions": {"families": ["claude", "gpt"], "names": ["CLAUDE.md"], "files": ["~/.claude/CLAUDE.md"]}
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
