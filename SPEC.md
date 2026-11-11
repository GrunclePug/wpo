# wp-orchestration: specification and plan

Status: built 2026-10-22, Python port; the original project cut over
the same day (`tools/workflow/` deleted, run state in `.orchestration/runs/`). Decisions on section
8: Python; `~/.claude/skills/wp-orchestration` in its own git repository; run state in
`.orchestration/runs/`; the board is `wpo status`, optionally also written to a gitignored file.
This document is the design record; SKILL.md is the working reference. Originally written from the
the original project's tooling (~2,600 lines) and three days of nine parallel lanes.

## 1. Purpose

A reusable, project-independent way for one orchestrator session to run many coding agents in
parallel on a git repository: work is cut into work packages (WPs), each built in its own git
worktree by a tier-routed agent, verified, reviewed, and fast-forwarded into `main`. The skill holds
the machinery and the lessons; a project holds only its configuration and its WP files.

Assumed: a git repository with a `main` branch and no requirement for a remote; git worktrees;
Claude Code CLI. Optional: CLIProxyAPI (GPT routes), OpenCode (free routes), a local model server,
an optional `usage_budget.py` adapter. Initial target Windows 11; keep Linux possible.

Non-goals: CI integration, pull requests, multi-machine scheduling, remote repositories.

## 2. Concepts

- **WP file**: markdown with front matter (`id, title, milestone, status, depends_on, builder,
  reviewer, owns, lane, delegate`) and fixed sections (Goal, Context, Deliverables, Steps,
  Acceptance criteria, Verify, Out of scope, Progress log, Review, Handoff). Status machine:
  todo → in_progress → review → done, with changes_requested and blocked.
- **Lane**: a named queue of WPs run one at a time by one supervisor; lanes run in parallel.
  Dependencies count only once merged into main.
- **Tier**: an intelligence requirement (bulk, research, code-simple, code, code-hard, frontier,
  each with a floor score; `+taste` prefers Claude; `pin:<route>` for experiments). The router maps a
  tier to routes, free first, then local, then subscriptions cheapest first, under usage ceilings.
- **Roles**: orchestrator (the user's session; drafts WPs, unblocks, merges by hand when needed),
  lead builder (owns a WP attempt and delegates), worker (one fenced task on a free or local model),
  reviewer (reads the diff against the WP and the user's reviewer standards), planner (staged mode).
- **Events**: an append-only journal; "attention" events wake the orchestrator's waiter, "info"
  events are context. Everything that needs the orchestrator is an event, so it never polls.

## 3. Layout

```
~/.claude/skills/wp-orchestration/
  SKILL.md                  when to use it, the orchestrator's routine, the lessons (section 6)
  SPEC.md                   this file
  bin/                      entry points (section 4); run from here, never copied into worktrees
  lib/                      shared code
  models/tiers.json         default routing policy; scores.json for free models
  templates/WP-TEMPLATE.md, orchestration.json, AGENTS-snippet.md
  tests/                    unit tests and a scratch-repo end-to-end test
<project>/
  .orchestration/config.json   the project's settings (section 5), committed
  .orchestration/runs/         logs, events, state, catalog cache (gitignored)
  <wp_dir>/WP-*.md             WP files (default docs/plan/wp)
```

Scripts take `--project <root>` (default: the git toplevel of the current directory) and resolve
the main checkout through `git worktree list`. Because nothing is copied into worktrees, tooling
changes never conflict with WP branches and never need syncing. This removes today's sync step,
the merge conflicts it caused, and the class-identity bug from reloading copied scripts.

**Language.** Port to Python 3.11+ (stdlib only, plus `psutil` if allowed) rather than keeping
PowerShell: portable, typed exceptions behave normally, and the router and events are Python
already. Process trees, mutexes and detached jobs need small per-OS shims (Windows named mutex via
`ctypes`/`msvcrt`, file locks on Linux). Decision point for the user; the PowerShell port is
faster to finish but stays Windows-only.

## 4. Components

1. **router** (`bin/route`): `status`, `candidates --spec`, `refresh [--force]`, `mark`, `classify`.
   Policy from `models/tiers.json`, overridable per project. Free catalog from OpenCode Zen
   (`opencode models`) and OpenRouter (`/api/v1/models`, key in a configured file), scores from
   `scores.json` overlaid by the Artificial Analysis API when a key exists; unrated models are never
   dispatched; minimum context size; cooldowns per route after caps, errors and hangs; usage
   ceilings per family from `usage_budget.py`. Keep the existing tests and add policy-override tests.
2. **session** (`lib/session`): runs one agent session on one route with fallback to the next.
   Harness adapters: Claude CLI, GPT through CLIProxyAPI, local model through a compaction shim,
   OpenCode. Sandboxes per access level (builder, worker, reviewer, read, docs) built from the
   project's command allowlist and write paths. Every Claude-harness session runs with
   `--strict-mcp-config` and a disallowed-tools list. Liveness: the log grows, or the session's tool
   processes use CPU (harness processes excluded); otherwise kill the tree after the idle limit.
3. **run** (`bin/run-wp`): build, verify, review for one WP until it passes, re-evaluating it when
   failures pile up (SKILL.md, "Status"); a round that finds no route stops the run without counting; a WP in review is re-verified and re-reviewed
   without a builder. Tree snapshot detects edits outside `owns` (plus the project's shared-file
   exceptions). Prompts: builder (with lead instructions when delegation is on), reviewer (the
   user's reviewer agent body prepended).
4. **delegate** (`bin/delegate start|wait|list`): a lead hands a fenced task to a worker in the same
   worktree, detached; workers use only free and local routes; write paths must lie inside `owns`;
   `wait` returns within 9 minutes; workers still running when the lead ends are stopped.
5. **staged mode** for small-context models (new): a planner session on a stronger tier writes
   `plan.md` with tasks, each naming the files it edits, the interface it provides and consumes, and
   its check command, sized to a context budget (config, default 50k tokens); the runner executes
   tasks one by one in fresh sessions on the small model (resumable from the plan file); a task whose
   session reaches compaction is stopped and returned to the planner to split; a final assembly
   session runs Verify and writes the Handoff. Enabled per lane or WP (`mode: staged`).
6. **supervise** (`bin/supervise --lane --wps`): runs a lane's WPs in id order as dependencies merge,
   creates worktrees from main, sets per-WP build environment from config (for Rust: one
   `CARGO_TARGET_DIR` per WP, `RUSTC_WRAPPER=sccache`, a jobs limit), sleeps until the next route
   frees on no-route, integrates finished WPs, removes build directories after merges, writes a
   state file and journals finished, crashed and died events.
7. **integrate** (`bin/integrate --wp`): commit the worktree, merge main into the branch, run
   lockfile reconcilers on lockfile-only conflicts (config: e.g. Cargo.lock via `cargo metadata`),
   re-verify when main brought changes, fast-forward main under a machine-wide mutex, keep run logs,
   remove the worktree and branch, run post-merge hooks (config: e.g. a sidecar rebuild when its
   paths changed). Exit codes: merged, conflict, verify failed, fast-forward refused.
8. **jobs** (`bin/job`): detached orchestrator jobs (integrations, rebuilds) that outlive shell time
   limits and journal `orchestrator-job-done`.
9. **lanes** (`bin/lanes resume|status`): start one supervisor per lane with runnable work, skip
   live ones, apply lane overrides; `status` renders the board (replaces STATUS.md generation).
10. **events** (`bin/events emit|wait|cursor|tail`): the journal and the waiter; event kinds and
    severities declared in one place; a `--quiet-kinds` option so the orchestrator can mute
    expected noise (no-route during a known cap) without losing it.
11. **commit-main** (`bin/commit-main -m ... paths`): the only way the orchestrator commits to main:
    takes the integration mutex, applies a staged edit, verifies the paths parse, commits, releases.
12. **draft** (`bin/dispatch`): ad-hoc tier-routed tasks (WP drafting, research) with docs access.

## 5. Project configuration (`.orchestration/config.json`)

```json
{
  "name": "ExampleProject",
  "wp_dir": "docs/plan/wp",
  "worktrees_root": "../ExampleProject-worktrees",
  "runs_dir": ".orchestration/runs",
  "agents_file": "AGENTS.md",
  "verify_workspace": ["cargo fmt --all --check", "cargo clippy --workspace --all-targets -- -D warnings", "cargo test --workspace"],
  "builder_commands": ["cargo", "rustc", "rustup", "dotnet", "git status", "git diff", "date", "grep", "findstr", "where", "which"],
  "builder_extra_commands": ["<tools-root>/ghidra/support/analyzeHeadless"],
  "shared_files": [{ "path": "Cargo.toml", "rule": "only [workspace.dependencies] entries" }, { "path": "Cargo.lock" }],
  "build_env": { "kind": "rust", "build_root": "<build-root>", "jobs": 4, "sccache": true },
  "lockfile_reconcilers": [{ "file": "Cargo.lock", "command": "cargo metadata --format-version 1" }],
  "post_merge_hooks": [{ "when_changed": "tools/sidecars/ue-export/**", "command": "dotnet build tools/sidecars/ue-export -c Release" }],
  "lanes": { "local": { "pool": true, "builder_override": "pin:my-local-route" } },
  "delegate_default": "code-simple",
  "idle_minutes": 30,
  "evaluate_after_failures": 2,
  "reviewer_agent": "~/.claude/agents/reviewer.md"
}
```

Machine-wide settings (proxy token path, local model shim URL, OpenRouter and AA key paths,
usage-budget script) live in `~/.claude/wp-orchestration.json`, not in projects.

## 6. Lessons to encode (SKILL.md and code)

Process
- One orchestrator commits to main, always under the integration mutex, edit and commit in one
  step. An uncommitted edit in the main checkout makes an integration's fast-forward fail.
- Long jobs run detached and journal completion; shell background limits (2 h) kill them.
- Retier or edit a WP before restarting its lane, and confirm the edit landed: the lane resumer
  restarts anything runnable.
- When a WP fails review three times on the same class of defect, stop patching: write a structural
  direction and an explicit contract (e.g. a precision contract) the reviewer judges against.
- Over-read goals are unblocked with scoped Context notes; sandbox limits (no delete) are done by
  the orchestrator and noted in the Progress log.
- A WP that only the user can judge (look, feel) ends with evidence made for comparison.

Agents
- Builders load no MCP servers (one server cost 55k tokens of context) and no unused harness tools.
- Small-context models need fresh, bounded tasks; long sessions churn in compaction re-reading files.
- Free models are good workers under a strong lead; give them interfaces, not open goals.
- Hang detection must count tool-process CPU: a tool call writes nothing until it ends.

Platform
- Write files with read sharing (editors hold handles); keep CRLF/LF as found.
- Never catch exceptions by type across reloaded scripts (PowerShell); match by name, or use Python.
- Builds: one target directory per worktree (shared directories reuse stale artifacts), sccache for
  dependencies, line-tables-only debug info, a jobs limit; the commit limit, not RAM, is what fails.
- Integrations re-verify after merging main; keep lockfile reconciliation automatic.

## 7. Plan

Each phase ends with its check. Sizes are orchestrator sessions at today's pace.

1. **Freeze and inventory** (small). List every script, flag and behaviour in `tools/workflow/`,
   mark project-specific values, decide Python vs PowerShell. Check: the inventory covers every
   entry point named in `docs/plan/03-workflow.md`.
2. **Skeleton and config** (small). Skill layout, config loader with validation and defaults,
   machine config, `commit-main`, `events`. Check: unit tests; `events` reads the existing journal.
3. **Port the router and session layer** (medium). Router as-is plus policy overrides; session with
   harness adapters, sandboxes, liveness. Check: existing router tests pass; smoke sessions on one
   free, one GPT, one Claude and the local route (the `dispatch` smoke set from 2026-10-19).
4. **Port run, delegate, supervise, integrate, jobs, lanes** (large). Check: an end-to-end test on a
   scratch repository with three toy WPs in two lanes (one dependency, one delegated task, one
   forced merge conflict on a lockfile, one review rejection), all in a scratch repository.
5. **Staged mode** (medium). Planner, task runner, assembly, compaction stop. Check: one small real
   WP on the local model with a code-hard planner finishes without a single compaction.
6. **Cut over the original project** (medium). Write `.orchestration/config.json`, move run state, point
   AGENTS.md and `03-workflow.md` at the skill, run one lane on it, then all lanes at a quiet moment;
   delete `tools/workflow/`. Check: a WP goes todo → merged on the skill; `git grep tools/workflow`
   finds only history notes.
7. **Write SKILL.md** (small). The routine (status check, waiter, unblock, integrate, resume), the
   lessons, and how to start a new project. Check: a fresh session given only the skill runs the
   status check correctly.

Quota: phases 3 to 5 are the expensive ones; run them as WPs of their own on code-hard with
delegation, the orchestrator reviewing, and keep at least 20% of the Claude week in reserve.

## 8. Possible improvements

- **OS-level sandbox for builder sessions (logged 2026-10-24).** Every harness confines sessions by
  command prefix and write rules, which stop accidents, not a session that runs code it wrote
  through an allowed build tool (a build script, a test, a cargo config). Only an OS boundary
  confines that: a restricted Windows user per session, a job object, or a low-integrity process
  that can write only its worktree and the build directories. The user's caution: sandboxing too
  much would stop agents doing out-of-the-box work they genuinely need, so any such boundary must
  be measured against real WP needs (installs read-only, cache and build dirs writable, tools
  reachable) before it is enforced. Two reviews of the agy hook (2026-10-24) list the gaps a
  prefix allowlist leaves.

## 9. Open questions for the user

- Python port (recommended) or PowerShell?
- Skill name and location (`~/.claude/skills/wp-orchestration`), and whether to keep it in its own
  git repository.
- Where run state lives in projects (`.orchestration/runs/` proposed, replacing `docs/plan/runs/`).
- Should the status board stay a generated markdown file in the repository or only a command?
