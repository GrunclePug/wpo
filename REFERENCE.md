# wp-orchestration reference

Details behind [SKILL.md](SKILL.md): read a section when its situation comes up, not up front.

## Session context

Every session starts lean (`session_brief.py`, `harness.py`), because each turn re-sends the whole
conversation. Claude-CLI sessions run without CLAUDE.md discovery, the skill listing and Skill tool,
auto-memory, git instructions, MCP servers, the user's settings (plugins, personal agents, tool search:
`--setting-sources project,local`), built-in tools their role does not use (`--tools`, `ROLE_TOOLS`) and
the user's SessionStart hooks (`WPO_SESSION=1`); they
compact at `autocompact_tokens` (per harness: 275k, GPT 160k) when the route's window is larger. Instead an appended system
prompt (`<log>.system.md`) carries, each file under a `File path:` header (never `@path`, which the CLI expands into a whole-file attachment), the project's
`instructions_file` (AGENTS.md, from the main checkout), the user's `user_rules.sections` (machine
config), working rules (batch calls, Grep before Read, narrow edits, narrow tests, reuse evidence, wait
for background commands by ending the turn), and the role's skills: `builder_skills` (coding) plus the
WP's `skills` for builders, workers, staged executors and assemblers; `reviewer_skills`, else the reviewer agent's declared
`skills` for reviewers; none for evaluators, planners and dispatches. It names where other skills live.
The task prompt starts with the files the role needs, inlined so no turn is spent reading them:
`builder_reading` / `reviewer_reading` from the main checkout (`path` or `path#heading` for one `##`
section), then the WP file from the tree. Sessions record progress with `wpo log "<message>" [--step N]`
(it writes the dated, keyed Progress log line and ticks the step; `WPO_ROLE` and `WPO_ROUTE` are in the
session's environment) and check their work with `wpo verify` (fixers, the WP's Verify commands and the
owns check, printing only failures). OpenCode sessions read AGENTS.md themselves and get the rest atop
the prompt (`<log>.prompt.md` records the exact stdin).
Reviewers start from a **review brief** (`review_brief.py`, saved as `attempt-N-review-brief.md`): the
diff since the branch left main, untracked files included, and each symbol the diff adds, removes or
changes inside with its textual references (`file: lines`; common names give counts in changed files
only). Tune with `review_diff_exclude`, `review_symbol_patterns`, `review_reference_limit` and
`review_inline_chars`.

## agy routes

`agy` (Google Antigravity CLI) serves free routes from two quota groups that cool down as one each:
`agy-gemini` (Gemini 3.8 Flash high 40.9, medium 39.8, low 33.5) and `agy-thirdparty` (Claude Opus
4.6 31.9, Sonnet 4.6 30.1); scores from the Artificial Analysis API (2026-10-24). agy has no
per-invocation allowlist, so each session is sandboxed by a PreToolUse hook (`agy_hook.py`): the
harness writes the tree's `.agents/hooks.json` (generated, atomic, kept out of git) and a
per-session policy outside every tree (`<state_dir>/agy-sessions/<id>/policy.json`): commands by
whole-word prefix, from an allowed character set (no shell operators, substitutions, variables or
line breaks in cmd or PowerShell), without per-tool flags that redirect output, change directory,
load configuration or run other programs (`AGY_DENIED_ARGUMENTS`), and without absolute or `..`
paths outside the readable dirs; writes by case-insensitive glob (builders: the whole tree), never
`.git` or `.agents`; reads in the tree, the skill dirs and `agy_read_dirs`, and a read naming no path
is denied; everything else (web, browser, MCP, subagents) denied. Paths resolve through the file
system (short names, junctions, case) and aliasing spellings (streams, trailing dots, wildcards,
percent escapes, UNC, drive-relative) are refused. The hook fails closed and logs each decision
(`decisions.log` beside the policy); a session with fewer decisions than tool steps is refused and
journals `sandbox-unverified` (attention: check what it changed). The prompt carries these limits.
The hook gives agy sessions the same trust as Claude-CLI sessions, whose allowlist is also by
command prefix: it stops accidental and stray actions, not a determined session. An allowed build
tool runs code the session wrote (a build script, a test), which no hook sees. agy's own skills in
`~/.agents/skills` still load. agy sessions are not resumed.

## Check scope

With `check_scope: {"kind": "cargo"}` (`scope.py`), every `--workspace` in `verify_round` and
`verify_workspace` becomes one `-p` per package the change reaches: the changed packages and every
workspace package depending on them. A changed path in no package and not `verify_neutral_paths` checks
the whole workspace (root manifest, lock file, toolchain); `mentioned_paths` globs (gate evidence a test
`include_str!`s) reach only the packages whose sources name the file. A change reaching no package runs
no workspace command. Integration verifies outside the integration mutex; under it, main is merged in
again and the branch fast-forwards when main's new commits reach none of its packages, else it verifies
again (the third time under the mutex). `worktree_prewarm` starts commands detached in a new worktree
(`{packages}`: the packages the WP owns, `{reach}`: those and their dependents), so the builds the
builder, `wpo verify` and integration need are warm by then.

## Repair and resume

A failed round is routed by what failed. When every failing Verify command is a `mechanical_checks`
command (lint, format, style) and nothing is outside owns, a fresh session on `repair_spec`
(code-simple) gets a closed task: the diagnostics, the owned files, no exploration. Verification
re-runs (`attempt-N-verify-after-repair.log`); only what still fails goes to the builder. Test and
build failures, review findings and a builder that stopped short go to the builder, which
**continues its previous session** (`--resume`, `<log>.resumed.log`) when the round failed that
way or a re-evaluation decided `continue`, the tier and the tree's HEAD are unchanged, the route is
still available, and the session's context is below its harness's handoff point; otherwise it
starts fresh and logs why. A resume that does not complete falls back to a fresh session without
cooling the route down. Events: `repair-attempted`, `session-resumed`, `resume-failed`.

**Handoffs replace compaction for builders.** A builder on a Claude-CLI harness (claude, gpt, xai,
local) is sent a message at its next tool call when its context reaches `handoff.context_tokens`
(180k; 140k on gpt, below every harness's compaction point, which is validated) or a usage window
of its subscription reaches `handoff.quota_percent` (97%, checked every 5 min, so the summary is
written while the route still answers). It tidies up and records a handoff with `wpo handoff` (done,
half done, next, what the files do not say); a fresh session continues the same round
from it (`attempt-N-builder-handoff-K`), uncounted. When a round starts fresh after a recorded
session (its context passed the handoff point, a re-evaluation decided anything but `continue`,
the tier or HEAD changed, the WP was unblocked), that session is resumed once for its handoff
(`attempt-N-handoff`). A session whose compaction keeps failing for 30 min is stopped as hung (the
local model never answers the CLI's compaction request within its 300 s timeout). Harnesses that
cannot resume (OpenCode, agy) start fresh from the Progress log. Events: `handoff-requested`,
`session-handoff`.

## Autofix

Before every verification the runner applies the `autofix` fixers to the files the WP changed and
owns (`autofix.py`), logging what each fixed; Verify stays the gate. Defaults follow a common
project's CI: `ruff-format`, `ruff-check` (`--fix`), `trailing-whitespace` (not `.md`/`.txt`) and
`end-of-file`. Entries merge by name: a project adds one (`"rustfmt": {"globs": ["*.rs"], "command":
["rustfmt", "--edition", "2024"]}`), changes one field of one, or disables one with `null`. Builders are
told which fixers run, so they spend no turns on formatting.

## Model routing

Work names an **intelligence tier**, never a model: bulk 20, research 30, code-simple 40, code 42
(low effort), code-hard 48 (medium), frontier 50 (high) (Artificial Analysis Intelligence Index
floors, with downgrade floors). Subscription routes carry Artificial Analysis's price, output
tokens and output speed per Index run; the tiers rank only each family's Pareto frontier of score
against price, tokens and time, cheapest per task first (`routing.family_frontiers`).
No tier reaches max effort (`pin_only_efforts`): only a pin runs it, for large multi-part autonomous
work, and reviews and re-evaluations never do.
`+taste` puts Claude first (UI, look, game feel, physics). `pin:<route id>` runs one route, for
experiments. Order: free routes (best first), the local model, then subscriptions cheapest first;
then downgrades. Free routes include the `xai` family: Grok models on the user's xAI account through
the same CLIProxyAPI as GPT (`cli-proxy-api -xai-login`), one account-wide cooldown on a rate limit. Routes are skipped over usage ceilings (Claude 85% 5h / 92% 7d, GPT 92% / 95%),
during cooldowns after failures, without keys, or when the local model is down. `wpo route status`,
`wpo route candidates --spec code-hard`, `wpo route clear --prefix family:gpt` (a cap lifted early).
Policy: `models/tiers.json`, free scores `models/scores.json`; projects override in `routing`.
**Listed tiers.** `tier_routes` gives a tier explicit routes instead of floors:
`{"code-hard": {"routes": [["claude-opus-medium", "gpt-sol-high"]], "fallback": [[...]], "free_last_resort": true}}`.
Groups run in order; within a group the family with the most headroom (percentage points left
before its tightest window stops it: the five-hour ceiling, or the weekly one under its pace) goes
first, routes spending no subscription before it, Claude first for `+taste`. `fallback` groups are
the tier's downgrades, and all a family over its weekly pace may serve. Listed routes skip the
family frontier; gate-only, pin-only and family rules still apply; unknown ids are skipped and
shown by `wpo route status`; an empty entry lists nothing. **Routing modes:** `modes` maps a name to
its own `tier_routes`, which replace the standard ones tier by tier. `wpo route mode
[standard|<mode>]` shows or sets the project's mode (`<runs>/routing-mode.json`, journaled as
`routing-mode`); a WP records the mode in which its first route was chosen
(`<runs>/<WP>/routing-mode.json`) and keeps it for every role, staged ones, repairs and delegated
workers included, so a switch reaches only WPs that start afterwards. Its specs then carry the mode (`code-hard@hard`); the dashboard shows both.
A failed session (cap, API error, crash, no activity for `idle_minutes`) falls back to the next route
in the same tree; when nothing is left the run stops with exit 4 without counting the round. A
subscription session (claude, gpt) with no first model turn after `first_output_minutes`, or one that
hangs, cools its whole family down, not just its route: the family's service is stalling.
When every paid route is capped, paced or cooling down, builders still run on free routes at or
above `free_last_resort.floor` (last in every tier's order); reviews and re-evaluations never do.
The local model is never ranked among routes: only a pin reaches it. A **pool lane**
(`"lanes": {"local": {"pool": true, "builder_override": "pin:local-deepseek-max"}}`) keeps it busy
around the clock on runnable WPs from any unheld lane, except a WP that is the last unmerged
dependency of an open WP while a hosted route could serve it (a supervisor sleeping for a route
releases its WP's claim and leaves a `route-wait` marker, which lifts that exception). WPs nothing
depends on come first, then WPs whose dependents also wait
on other work, lanes without a live supervisor first within each. It hands a WP back to its own lane (starting that supervisor) once it reaches
review or is escalated. `wpo lanes resume` starts pool lanes; they never finish. A lane with
`"builds_in_pool": true` is work for the pool alone: its supervisor starts only to review, so a
resume never puts a second session on the pool's model.
`weekly_pace` (per family: `budget_percent`, `slack_percent`) spreads a project's share of each weekly
quota over the week, counting account-wide usage, so one project cannot spend the week in a day. The pace is graded: up to `cheaper_band_percent` (default the slack) past it, a family serves tiers only from their downgrade band, one step down, before it waits.

## Delegation and staged mode

- **Leads and workers.** Every builder is a lead: it writes precise task specs to
  `.orchestration/tasks/<WP>/<name>.md` and hands them to free/local workers with
  `wpo delegate start|wait|list` (the builder prompt explains it). Workers edit only paths inside
  `owns` that the lead names. `delegate: none` keeps a WP whole.
- **Worker subagents.** A lead on a claude or gpt route also gets the Agent tool with one `worker`
  subagent (`session_workers`: haiku, low effort; null disables it) for small mechanical pieces it can
  specify in a few lines; their reading and tool output stay out of the lead's context.
- **Staged mode** (`mode: staged`, or a lane's `mode`): for small-context models. A planner tier writes
  `plan.json` (tasks with files, provided and consumed interfaces, a check command, sized to the
  context budget); each task runs in a fresh executor session; a session that reaches compaction is
  stopped and its task sent back to be split; an assembler runs Verify, ticks steps, writes the
  Handoff. Configure `staged.planner`, `staged.executor`, `staged.context_tokens` per project or lane.

## Messages and wrap-up

Claude-CLI sessions read their prompt as a stream-json conversation, so the orchestrator can reach a
running one between turns (`inbox.py`): `wpo message --wp <id> "text"` (or `--lane <lane>` for the WP
the lane runs now) queues it in `<runs>/<WP>/inbox/`, the watcher delivers it within one poll, and the
session answers it on its next turn (`<log>.inbox.log` records deliveries). `wpo wrap-up --wp <id>` or
`--lane <lane>` (which also holds the lane) asks the session to log its progress and stop at a safe
point; the runner then ends the WP's run without counting the round, and the next run continues from
the Progress log (`wp-paused`). OpenCode and agy sessions cannot read messages: wrap-up pauses them when
their session ends. A held lane's supervisor takes no new work.

## Session views

`wpo show --wp <id> [--open]` renders every log of a WP (`transcript.py`) into `<runs>/<WP>/view/`,
with an index in run order: each Claude-CLI session as a conversation (its prompt and system prompt,
assistant text, visible thinking, tool calls with their inputs and edits, results with their
duration, subagent turns, compactions, background tasks, orchestrator messages, the result) and
verification, review and other harness logs as text. `wpo show <log>` renders one log.

## Dashboard

A live progress page per project at `http://127.0.0.1:8470/` (`wpo dashboard` prints it; `--stop`
stops it until the next `wpo dashboard`). Nobody starts it: every `wpo` command checks that the
project's server runs current code (a state-file read and a process check, under a millisecond)
and otherwise starts one, detached through a short-lived intermediate so it sits in no session's
or supervisor's process tree. One server per project whichever worktree wpo runs in (state in
`<runs>/dashboard.json`, a start mutex and a lifetime mutex); a launch that died or could not bind
is retried after a pause, logged to `<runs>/dashboard.log`. It shows every lane (supervisor state,
hold, current WP or sleep reason), every open WP (status, steps, rounds/failures/re-evaluations,
the running role and route with its live context against the handoff point, latest Progress line,
open findings, last activity, unmerged dependencies), milestones, recent merges, usage windows with
their ceilings and burn forecast, route cooldowns and the event journal (attention events marked);
a WP's detail has its full Progress log, findings with answers and rulings, round history, and its
logs (rendered as in `wpo show`, or raw). Its views only read: files are re-read when they change, logs and
the journal followed by whole lines, usage taken from the cache, no tooling lock taken; it pushes
updates as server-sent events and does no work while no page is open. Bound to 127.0.0.1 and
answering only that host.

The user acts from the page (`user_actions.py`; POSTs to `/api/action/*` carrying the server's
per-start token from the served page, from the page's own Origin; a GET never acts). A **Needs you**
panel lists pending approvals (Approve with a note, Deny with a reason: `approvals.decide`, as `wpo
approve|deny`) and WPs that are blocked or were handed back while in review (Unblock: an optional
note becomes a dated `User note` bullet in the WP's Context, in its worktree copy or else in main,
committed under the integration mutex; status to todo, or review when it was left in review; a held
lane is released and `wpo lanes resume <lane>` runs; `wp-unblocked`). A WP's detail has the same
Unblock and a message box, done WPs included, and **Ask for work** takes a request about no WP. A
message goes to the WP's live session as `wpo message --wp` would (`user-message`); otherwise a
detached `wpo place-request` runs a placement agent (code-hard, never a free or local route, docs
access writing only `<wp_dir>/**`; `user-message-queued`) that answers with a JSON decision record
(`<runs>/placements/<id>/`): the open WP the request belongs to (the asked-about WP when it is not
done), or a new WP it drafted from the template. The tooling adds the request verbatim to that WP's
Context (unblocking a blocked one), commits main-checkout edits through the commit-main path, lints a
new WP and journals `user-message-placed`, or `user-message-unplaced` with the text when it could
not; both are attention events. Every message is journaled with its text.

Port and autostart: `dashboard` in the machine settings
(`project_ports` gives further projects their own port); `WPO_DASHBOARD_AUTOSTART=0` disables it per
process. The page is static files in `dashboard/` (no build step).

## Usage reports

`wpo usage report [--since DATE] [--until DATE] [--by family,role|route|wp|day]` reads every session
log (`usage.py`, cached in `<runs>/usage/index.json`) and tabulates sessions, turns, context per turn,
prompt tokens per session, cache read and write shares, output, the Claude CLI's cost estimate, the
share of wall time spent waiting on tools, compactions and subagent turns; then per-model totals and
where tool time goes. `wpo usage changes [--since DATE] [--merge-minutes N]` splits the timeline at each
tooling change (the skill's commits and the project's config or AGENTS.md commits, bursts merged) and
tabulates the sessions under each, so a change's effect reads against the segment before it. Both
write their Markdown to `<runs>/usage/`. Check a change's segment once enough sessions ran under it.

## Lint and audit

`lint.py` checks each WP against every WP the tooling reads (`effective_wps`: main's, and a WP with a
worktree from its worktree). Findings carry a check name (`sections`, `verify`, `criteria`,
`dependencies`, `owns`, `module-declaration`, `overlap`, `reverse-engineering`, `stale-reference`,
`similarity`); `wpo lint` ends with counts by check and exits 1 on any error. Open means todo, in
flight or blocked. An overlap is reported once per pair in a run (on the lower id). A blocked
dependency is reported only on a WP that could otherwise start. Milestones order only when both are one
prefix and a number (`M3 < M10`; `M?` orders with nothing). A new module is a `.../src/<dir>/**` or
`.../src/<name>.rs` owns pattern whose path does not exist; one of its declaring files (`lib.rs` or
`main.rs` at the crate root, else the parent's `mod.rs` or `<parent>.rs`) must be owned, already declare
it (`mod <name>;`), or come from a dependency; `src/bin/` is exempt.

`lint.forbidden_verify` entries are `{pattern, message, severity}`: a regex searched in each Verify
command, severity error unless set. A project can reserve `cargo test --workspace` for merge
verification and forbid unscoped install tests; configure those regexes in the project.

**Similarity** (`similarity.py`): TF-IDF cosine over the title (counted twice), Goal, Deliverables and
Acceptance criteria. Lowercase words of three letters or more, plurals folded; stopwords, WP ids,
per-WP test prefixes and the WP format's own words dropped; sublinear term frequency, smoothed IDF over
every WP. A WP at or above `lint.similarity_warn` (0.30) with another gets one warning naming the
`similarity_top` (3) closest, each with its status, score and whether this WP cites it. The example
threshold was calibrated on 188 WPs; known duplicates scored about 0.64 and 0.31 while 99% of pairs
scored under 0.23. Tune it against the project's corpus. Citing a WP does not silence the warning:
a follow-up can cite a dependency while restating its goal. Bigrams and the Context section were
tried; both ranked the calibration duplicates lower.

**Audit** (`audit.py`): `wpo audit` writes the lint part (counts, findings by check, pairs at the
threshold with an open side, every open WP with its live path) to `<runs>/audits/YYYY-MM-DD.md` (`-2`,
`-3` for further runs that day), then dispatches a read-only session (`audit.spec`, on families neither
free nor local, as placements) that reads `audit.context_files` and judges duplicates against done WPs'
Handoffs, contradictions with the decisions log and project rules, contradictory or uncheckable
criteria, oversize WPs and stale Context. Its answer is appended under `## Judgment review`, and
`audit-done` names the report (`review=done` or `no-route`). A crash journals `audit-failed`. The weekly
task (`schtasks /SC WEEKLY`, named `wpo-audit-<project>`) runs `wpo --project <root> audit` as the user,
only while they are logged on.

## Lessons (encoded, and to keep)

- One orchestrator commits to main, under the integration mutex; edit and commit in one step.
- Retier or edit a WP, and confirm the edit landed, before resuming its lane.
- After three reviews fail on the same class of defect, stop patching: write a structural direction
  and an explicit contract (a precision contract, an invariant) the reviewer judges against.
- A WP only the user can judge (look, feel) ends with evidence made for comparison.
- Builders load no MCP servers (one cost 55k tokens of context) and no unused harness tools.
- Never write `@path` in a prompt: the Claude CLI attaches the whole file (2026-10-29: a spec section
  inlined under an `@` header cost +16k tokens per session, the whole spec again).
- A test for "same as before this WP" must pin values recorded from the unchanged code; Opus low twice
  compared the new code path with itself (2026-10-29), and review rejected it.
- Small-context models need fresh bounded tasks; long sessions churn in compaction.
- Free models are good workers under a strong lead; give them interfaces, not open goals.
- Hang detection counts tool-process CPU: a tool call writes nothing until it ends; a command waiting
  for a build slot touches `<runs>/<WP>/heartbeat`, which counts too.
- Count only the work's failures as rounds: sessions cut off by the harness, machine failures in
  verify and stalled families are retried or waited out, never re-evaluated (efficiency review
  2026-10-26: 46% of failed rounds were infrastructure). Verify each tree once.
- Builds: one build directory per worktree (shared ones reuse stale artifacts across worktrees),
  a shared compiler cache, small debug info, a jobs limit; the commit limit, not RAM, fails first.
  Let each worktree use its own default `target/`: a per-worktree `CARGO_TARGET_DIR` (or any
  per-worktree `CARGO_*` variable) enters sccache's hash and leaves the shared cache with no hits.
  Keep build directories and the compiler cache on an SSD; a spinning array stalls parallel builds.
- Only WPs committed on the main branch are scheduled (worktrees come from main); a draft in the
  main checkout's working tree is invisible to lanes and the board until committed.
- Integration verifies unless the commit is exactly the tree the runner verified and main has not
  moved; it refuses a worktree with an unfinished merge. Rounds, failures and re-evaluations
  persist across runner restarts (`<runs>/<WP>/attempts.json`, old attempt ledgers migrate); a
  round interrupted mid-build is not counted; setting a WP back to `todo` starts the ledger afresh.

## Tests

`python -I -B tests/test_units.py` and `python -I -B tests/test_end_to_end.py` (scratch repository,
scripted agents: lanes, dependency, delegation, review rejection, lock-file reconciliation, staged
mode with a compaction replan). `SPEC.md` holds the design history.
