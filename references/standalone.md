# Standalone WPO engine

Read this only when the user wants the legacy CLI engine. For T3-managed work, use
[t3.md](t3.md); this engine creates its own worktrees and subprocesses.

One orchestrator session (you) keeps many agent sessions busy on one repository. Work is cut into
**work packages** (WP files); each runs in its own git worktree on branch `wp/<id>`, built by a
tier-routed agent, verified, reviewed, and fast-forwarded into `main`. Lanes run in parallel, one
WP at a time each. The machinery lives here; a project holds `.orchestration/config.json` and its
WP files. Everything is the `wpo` command (`python -I -B <skill>/bin/wpo.py`; `wpo install-shim`
puts it on PATH). Run it from the main checkout or any worktree.

## The orchestrator's routine

1. **Wake on events, never poll.** Keep one waiter running in a background shell; the harness wakes
   you when it exits:
   `wpo events wait --after <cursor> --timeout-minutes 110` (background, timeout 7200000).
   It prints new events and `cursor=<n>`; exit 0 on an attention event, 3 on timeout (heartbeat).
   Start with `wpo events cursor`. Mute expected noise for one wait with `--quiet no-route`.
2. **React to attention events:**
   - `wp-blocked`: read the WP's Progress log and Review in its worktree. An over-read goal gets a
     scoped Context note (in the worktree's WP file) and the status reset to `todo` or `review`;
     a real block (a rule, a missing tool, a user decision) goes to the user.
   - `integration-failed` / `supervisor-finished ... needs-orchestrator`: resolve in the worktree
     (`git merge main`, fix, commit the merge), then `wpo job start --name integrate-<id> -- wpo integrate --wp <id> --verify`.
   - `no-route`: usage caps; the lane sleeps and resumes by itself. Nothing to do.
   - `supervisor-died` / `supervisor-crashed`: read `supervisor-<lane>.log`, fix, `wpo lanes resume`.
   - `runner-failed`: `wpo run` crashed on a WP (read `supervisor-<lane>-<WP>.log`); the lane moved on.
   - `approval-requested`: a session asks to run a command its sandbox denies (`wpo ask-approval`).
     `wpo approve <id> [--note]` or `wpo deny <id> --reason`; the session is told and runs an approved
     command once with `wpo run-approved <id>`. Ask the user when the command touches anything a hard
     rule protects; a request that recurs belongs in the project's command allowlist.
   - `review-orchestrator-note`: a reviewer asks for a change only you can make (the WP file's
     contract, owns, a split, another WP's paths). Judge it; edit the WP (in its worktree) or drop it.
   - `cleanup-failed`: the WP merged but its worktree or branch could not be removed; remove by hand.
   - `orchestrator-job-done`: read the job's log line; on merge, `wpo lanes resume` starts lanes
     whose dependencies just merged.
   - `audit-done`: the weekly planning audit (`wpo audit`) wrote `<runs>/audits/<date>.md`: lint over
     every open WP, then a review session's judgment (duplicates of done work, contradictions with the
     decisions log, criteria that cannot be checked, oversize WPs, stale Context). Decide each finding:
     edit the WP (in its worktree when it has one) and commit with `wpo commit-main`, draft a split, or
     drop it; take what needs the user to them. `review=no-route` means only the lint part ran: rerun
     later with `wpo job start --name audit -- wpo audit`. `audit-failed`: read the message, fix, rerun.
   - `user-message-placed` / `user-message-unplaced`: the user asked for work from the dashboard and a
     placement agent put it in a WP's Context or drafted a new WP (check the draft and its lint line), or
     could not (place the text by hand). The user also unblocks WPs and answers approvals there
     (`wp-unblocked`, `approval-decided`); REFERENCE.md, Dashboard.
3. **Keep lanes fed.** `wpo lanes resume` starts one supervisor per lane with runnable work (skips
   live and held ones), nearest to merging first, up to `max_active_lanes` working non-pool lanes
   (`lane-deferred` for the rest; a lane asleep for a route takes no slot and waits for one on waking); `wpo lanes resume <lane|WP> ...` starts only those lanes. `wpo status` prints the board. `wpo lanes stop|hold|release <lane>`.
   Before starting many lanes on one family, estimate their 5-hour cost; lanes beyond what the binding
   family and `build_slots` serve only sleep and wait.
4. **Commit your own edits to main promptly** with `wpo commit-main -m "..." <paths>`: it holds the
   integration mutex, so no fast-forward races it. When the commit holds WP files it lints them and
   prints the findings (errors marked `!!`); nothing blocks the commit, so read them and fix or decide. An uncommitted edit in the main checkout makes an
   integration's fast-forward fail (exit 7). A WP that already has a worktree is edited in the worktree.
5. **Long work runs detached.** Background shells die after 2 hours; `wpo job start --name <n> -- <command>`
   outlives them and journals `orchestrator-job-done`.
6. **Lint WPs before committing them:** `wpo lint [WP ...]` (default: every open WP, blocked and in-flight
   ones included; `--all` adds done ones, `--json` for machines) checks sections, an empty Verify block,
   Verify commands the project forbids (`lint.forbidden_verify`), numbered criteria, dependencies (unknown,
   cycles, on a blocked WP, on a later milestone), `owns` paths that exist (or that a dependency creates),
   new Rust modules whose declaring file is not owned, overlaps with open WPs of other lanes with no order
   between them, WP ids that no longer exist, reverse-engineering goals without a method or contract, and
   WPs that read like another one, done or planned (similarity). A similarity warning means: read the
   named WP (a done one's Handoff), then merge into it or cite it in Context and say what this WP adds.
   By hand: grep the callers of every type or function a criterion changes and put them in `owns` (or
   scope them out), and check every number in a criterion against the source data.
7. **Ad-hoc work** (drafting WPs, research, triage): `wpo dispatch --tier research --name <n> --prompt-file <md>`
   (`--access docs --write-paths 'docs/plan/wp/**'` to let it write). From a background shell. A docs
   session whose write paths reach the WP files is told to run `wpo lint <id>` on each WP it writes and
   resolve the similarity warnings, and may run that command.
8. **Weekly planning audit.** `wpo audit` lints every open WP into `<runs>/audits/<date>.md`, then a
   read-only review session (`audit.spec`, code-hard, never free or local) appends the judgment findings,
   and `audit-done` is journaled. `wpo audit --install-schedule` registers it as a Windows task (Monday
   09:00 by default, `audit.schedule`); `--remove-schedule` removes it; `--show-only` prints the schtasks
   command. `--mechanical-only` writes only the lint part and journals nothing.

Report to the user briefly: what merged, what is blocked on them, what they can try in main
(`wpo whats-new [--since DATE | --last N]` prints each merged WP's `## Try it` section and its
evidence files; builders must fill Try it before review, `require_try_it`).

## Work packages

`builder` and `reviewer` name a tier (`bulk`, `research`, `code-simple`, `code`, `code-hard`, `frontier`; `+taste`
puts Claude first) or `pin:<route id>`, never a model.
Front matter (inline YAML subset): `id, title, milestone, status, depends_on, builder, reviewer, owns`,
optional `lane`, `delegate` (workers' tier or `none`), `mode` (`single` or `staged`), `skills` (extra
skills its code-writing sessions load, e.g. `[frontend-design]` for original frontend work, not for copying or
recreating a source game's look). Sections in order:
Goal, Context, Deliverables, Steps (checkboxes), Acceptance criteria (numbered, checkable), Verify (a
fenced block, one command per line, all must exit 0: the WP's own scoped checks; the project's
`verify_round` checks (format, lint) run after them every round, so a WP never repeats them; the full
`verify_workspace` checks, the whole test suite, run only on the merge into main, and fail it if they
change any file), Out of scope, Progress log (one line per step,
`- YYYY-MM-DD HH:MM [builder <route>] ...`), Review, Handoff. Template: `templates/WP-TEMPLATE.md`.

Status: todo -> in_progress -> review -> done, with changes_requested and blocked. Dependencies count
only once merged into main. A builder may change only `owns`, its WP file and the project's shared
files; changes are read from git, so anything outside is caught.

Failures are signals, not a budget. A failed round (builder stopped short, verify failed, changes
requested) goes back to the builder with its feedback. Failures of the machine are not rounds: a
builder session that ends short of review (only a passing `wpo verify` sets review) without
blocking is continued at once without counting (`session-interrupted`, up to `interrupted_session_resumes` in a row); a
verification whose every failure carries an `infrastructure_failure_markers` text (killed at the
timeout, full disk, DLL init failure, page file) is retried after `infrastructure_retry_minutes`
with the WP left in review (`verify-deferred`). The runner does not verify again a tree the
builder's own `wpo verify` passed on (apart from the WP file). Verification runs, the session's and
the runner's, queue on `build_slots` machine-wide slots (integration takes none). After `evaluate_after_failures` (2) failed
rounds since the last re-evaluation, or at once when the builder reports itself blocked, a
re-evaluation session (`evaluation_spec`, code-hard; never a free route) reads the WP, its logs and
diff and decides: `continue`, `revise` (it edits the WP file: a scoped note, smaller steps, a
prototype first, never weaker criteria), `escalate <tier>` (the builder tier, persisted), `split`
(the orchestrator drafts the WPs) or `needs-user`. Its reasoning lands in the WP's `## Re-evaluation`
section, which later builders read. After two review rejections the re-evaluation is told to check
whether they reject the same class of defect, and if so to revise with a structural direction and an
explicit contract. After `orchestrator_after_evaluations` (3) the WP is handed to
the orchestrator (`wp-needs-orchestrator`).

**Caps and cut-offs.** A 5-hour or weekly cap ends every session of its family at once: a running
session whose family hit 100% (or that another session saw rate-limited) after it started is killed
(`session-cut-off`). A family whose 5-hour or weekly window will reach its cap within
`burn_forecast.lead_minutes` (15) at the recent burn rate (the slope of `usage-budget-history.jsonl`
over `lookback_minutes`, 20) starts no new session, and its running sessions are asked for their
handoffs, one session at a time per family; `handoff.quota_percent` (97) asks whatever the rate. A builder session that fails
on its route without a handoff (cut off, crashed, runner died) is assumed to have stopped
mid-write: the next builder session, on the fallback route or in the next run, gets a recovery
report listing every file written since the cut-off chain began (last edited 'x ago'), deletions,
the diff of the most recently edited file, and the WP's Verify output on the tree as left.

**Review protocol** (`wpo/findings.py`, `REVIEW_PROTOCOL`; reviewer, builder and re-evaluation read the
same text). A BLOCKING finding rests on a verbatim-quoted criterion or project rule, a failing
Verify/Try it command, or a defect reproduced on real data; anything hypothetical is NON-BLOCKING.
Findings have ids `R<review>-F<n>` (review numbers never restart) and live in
`<runs>/<WP>/findings.json`. The builder answers each open one with `wpo finding fixed <id> '...'` or
`wpo finding veto <id> <ground> '<evidence>'` (no-basis, misquoted, outside-wp, not-reproduced,
contradicts-wp); `wpo verify` refuses until all are answered. The next review rules on each
(RESOLVED, STILL-OPEN, VETO-ACCEPTED, VETO-REJECTED); a veto can only be rejected by meeting the basis
rule. After two rejected vetoes the dispute goes to the re-evaluation. A rejection with no open
BLOCKING finding is left in review (`wp-blocked`). Changes only the orchestrator can make are
`ORCHESTRATOR:` notes: they never block and journal `review-orchestrator-note`.

## Reference (read the section when it applies)

[REFERENCE.md](../REFERENCE.md) holds the details; each line says when to read its section.

- **Session context**: a session's prompt, tools or skills look wrong, or you change what sessions load.
- **agy routes**: an agy session misbehaves, or `sandbox-unverified` fires.
- **Check scope**: verification ran more or fewer packages than expected (`check scope:` log lines), or setting up `check_scope` / `worktree_prewarm`.
- **Repair and resume**: a round's routing (repair, resume, handoff) surprised you.
- **Autofix**: a formatter or fixer entry needs adding or changing.
- **Model routing**: picking or overriding a tier, `pin:`, `+taste`, pool lanes, weekly pace, ceilings, `wpo route ...`.
- **Delegation and staged mode**: a WP for small-context models, or workers misbehave.
- **Messages and wrap-up**: reaching a running session (`wpo message`, `wpo wrap-up`).
- **Session views / Dashboard / Usage reports**: inspecting what a session did (`wpo show`), the dashboard and the user's actions from it (unblock, approve/deny, messages, request placement), or spend (`wpo usage report|changes`).
- **Lint and audit**: a lint finding surprises you, or you tune the `lint` or `audit` settings or the similarity threshold.
- **Lessons**: before drafting WPs or changing the tooling.
- **Tests**: changing the tooling itself.

## Setting up a project

1. `wpo init --name <Project>` writes `.orchestration/config.json` from `templates/config.example.json`
   and gitignores `.orchestration/runs/` and `.orchestration/tasks/`.
2. Fill in `verify_round`, `verify_workspace`, `builder_commands`, `reviewer_commands`, `shared_files`, `env` (per-WP
   build directories with `{wp}`), `remove_after_merge`, `worktree_setup`, `lockfile_reconcilers`,
   `post_merge_hooks`, `prompt_rules`, `lanes`, `commit_footer`. Every key is documented in
   `wpo/project.py` (DEFAULTS).
3. Write an AGENTS.md with the project's hard rules and standards; every session gets it in its brief.
4. Draft WP files with `lane:` fields, commit, `wpo lanes resume`, arm the waiter.

Machine settings (executables, proxy, local model, keys, router state, model preferences) are in
`~/.claude/wp-orchestration.json` or `WPO_MACHINE_CONFIG`; see
[configuration.md](configuration.md) for portable defaults and precedence.

