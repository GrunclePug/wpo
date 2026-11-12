# Operating the native T3 bridge

The Python engine retains scheduling, validation, findings, events, integration and dashboard state.
It cannot call `delegate_task` as an external MCP client: T3 requires a live provider-owned parent
run. A durable queue therefore connects Python to a live T3 coordinator. Only the coordinator calls
native task tools. The supported `t3 thread send` CLI wakes an existing coordinator; it never runs
the builder/reviewer prompt itself. No provider credentials are scraped or copied.

## Attach without starting project work

From a T3 thread for the actual repository:

1. Read the live `orchestrator_capabilities` result. Its `parentThreadId` identifies this coordinator.
   If its short model list omits configured routes, use the supported search option and combine
   the returned model entries without changing their provider IDs or options.
2. Save its structured JSON and the live `t3_worktree_status` result to temporary files under the
   repository's ignored run state. The actual T3 project root must match this WPO repository.
3. Run `wpo t3 attach --thread <parentThreadId> --capabilities-file <file> --workspace-file <file>` from the repository.
   This records capability data and an existing thread ID; it starts no lanes and changes no holds.
4. `wpo t3 status` reports attachment and outstanding work. Resume lanes only when the user requests
   it. The normal `wpo lanes resume`, `run`, `dispatch`, `audit`, `message`, and dashboard commands
   use the selected backend. T3 is the default; use explicit `execution_mode: standalone` for CLI
   subprocess models.

Configure `t3.cli` when T3's supported launcher is outside PATH. On Windows, the desktop normally
installs `~/.t3/bin/t3.cmd`; set its actual absolute path in the private machine config. `t3.base_dir`
selects a nondefault T3 data directory. Attachment is stored per repository, not globally by home
directory. Do not attach a home-directory thread to production work merely because it is available.

## Processing a backend control wake

Run the exact `wpo --project <root> t3 next --coordinator <threadId>` command in the wake message.
It returns pending native tool plans, running task IDs, and attention events. Process pending work
only; running/stalled entries are not new launch requests.

- **Workspace request:** call its `delegate_task` plan with `isolation: worktree`. T3 prepares the
  checkout. The child is a lightweight WP coordinator, not a builder. It registers its actual T3
  checkout/thread using `register-workspace`. Registration verifies repository identity, branch,
  and the expected local-main ancestor. Do not precreate or rebind a checkout with shell commands.
  A repeated identical registration returns the recorded workspace; one for a request that already
  failed, was cancelled, or recorded another checkout is kept on the request and refused. A failed
  or cancelled workspace request is asked again, as a new request, the next time the WP needs it.
- **Session request:** call its `delegate_task` plan exactly. Every builder, reviewer, re-evaluation,
  repair, staged executor, assembler, worker, audit and placement is a fresh native task. Sessions
  use `isolation: shared` inside the WP coordinator's assigned checkout. Execute the returned target
  model/options; report unavailable settings instead of substituting. The coordinator must not
  perform the task itself. Do not use `t3_thread_send` to start a review or re-check.
  Session plans save the complete UTF-8 context beside the durable request as `<request-id>.prompt.md`
  and send a short instruction to read it, avoiding native task prompt-size limits. The child must
  read the entire file before acting, in chunks if its reader truncates output, and stop if the file
  cannot be read. The original request payload and prompt log retain the full context for recovery.
- **Message request:** call the provided `t3_thread_send` plan in steer mode for the existing active
  session. Record success only after native delivery succeeds. The plan names the project's
  user-facing coordinator for requested replies; a worker's immediate WP coordinator is a
  separate thread. When the user asks for a report back, deliver it to that user-facing thread.
- **Cancel request:** call the provided `task_cancel` plan. Do not interrupt a delegated child using
  `t3_thread_interrupt`. Record the cancellation callback; Python keeps its task IDs and files.
- **Cancelled before launch:** an entry with `cancelled_before_launch` instead of a tool plan was
  cancelled before WPO saw its launch. Do not launch it. If you never launched it, run the command
  it gives (a `t3 complete` with `state: cancelled`). If you did launch it, record the launch with
  `t3 started` as usual; WPO then queues its native cancel. Only your callback settles it.
- An operator's cancel (`wpo t3 cancel`, `wpo lanes stop`) or the runner's worker cleanup ends the
  session with no fallback to another route, whatever state the task reaches; a quota cut-off's
  cancel is a failed session, and the next route is tried. `completion outcome` values are
  validated: `ok`, `rate_limited`, `api_error`, `hang`, `session_error`, and `compacted` only for a
  staged session.
- **Attention event:** inspect the reported WPO event and relevant artifacts, take authorized action
  or report the decision needed, then acknowledge the request with a completion callback. Identical
  attention reasons are deduplicated while one is still pending; once it is acknowledged, the same
  reason recurring is a new request and wakes the coordinator again. The original journal keeps every row.

Before the first `delegate_task` in each thread, call `orchestrator_capabilities` as required by T3.
Save auxiliary response/callback JSON outside `<runs>/t3/requests/`, for example
under `<runs>/t3/receipts/`. The requests directory is reserved for canonical
32-hex request records; auxiliary filenames are ignored and malformed canonical
records stop processing with their path rather than being treated as launch plans.

Persist the native response using `wpo t3 started --request <id> --response-file <json>`, preferably
within the same code-mode tool execution as the native launch, before yielding control. Use the raw
tool response or its structured content. Then honor the returned `next`: end the turn when told.
Native completion notifications wake this thread. Never poll a running `task_status`, use shell
sleep/watch loops to monitor native tasks, or replace completion notifications with a WPO event waiter.

After a completion notification, retrieve the completed native result and write a callback file:

```json
{
  "state": "done",
  "outcome": "ok",
  "text": "The child's actual final report, unaltered",
  "task_id": "native taskId",
  "thread_id": "native childThreadId",
  "link": "native link returned by T3"
}
```

Run `wpo t3 complete --request <id> --result-file <file>`. For native launch/runtime failures use
`state: failed`, the actual detail, and an outcome such as `session_error`, `api_error`, or
`rate_limited`; do not label a failed task successful. Preserve any native usage fields only when
actually provided. Workspace success is recorded by the child's registration, not by replacing it
with the anchor's final prose. Message/cancel/attention completions need no invented task IDs.

Callbacks enforce native task identity and terminal state. A cancellation before the launch
callback still queues cancellation of the late native task. A late success cannot resurrect a
cancelled request. Repeated callbacks are idempotent. Stable invocation receipts reconnect a crashed
Python process to its existing task; a consumed continuation creates a new task generation and
archives the old log rather than replaying its result. A receipt names its request before the request is submitted, and a finished session's request records when its result was consumed (`consumed_at`); that record wins over the receipt. One completion path finishes every session: the writer is recorded, then the scope check, then the result is logged and consumed, each step safe to repeat after a crash.

A native task can outlive its runner (a T3 restart, a killed supervisor). When the WP runs again, its runner first settles every such session, oldest first, before it chooses a phase from the WP's status: it waits for the task, records its writer, checks its scope and consumes its result. No route is chosen and main is not merged into the checkout until then, and the supervisor's automatic integration settles them first too; `wpo integrate` refuses a WP that still has one. Recovery restores safety, not the session's control flow: an operator's cancel, a failure or a compaction is logged and the run continues; only a scope violation (the WP is blocked) ends it. A survivor that stalled stops the run until it is reconciled. Surviving workers are stopped by the worker barrier as at the end of a builder session.

## Activity, permissions and recovery

Session prompts carry an explicit `--session` prefix. Use it for WPO commands; native provider
processes do not inherit Python's `WPO_*` environment. `wpo log` records real activity. A native
task may also use its supplied `t3 heartbeat` command between long steps. Report actual provider
activity through `wpo t3 event --request <id> --event-file <file>` when available. The event format
accepts `events` containing existing WPO JSONL-compatible records, `context_tokens`, and `compacted`.
Do not fabricate token counts, context progress, compactions or tool events.

WPO verifies scoped mutations after read/reviewer/evaluator/docs/worker sessions. Read/reviewer
sessions must leave repository files unchanged; the others may change only their listed paths.
Within a checkout, these sessions serialize so the mutation check has a clear boundary: a scope
lease names each one's request before it is published, and the next may start once that request has
finished. `t3 complete` records the paths such a session changed, and the check judges it on those,
not on later edits; recording them writes nothing into the repository, so a coordinator needs no
write access to `.git`. A lead
must stop writing while delegated workers run; lanes in different checkouts remain parallel.
A scope violation preserves the changes, blocks the WP, and requires inspection. T3's native
permission mode still governs tools, external files, network access and approval prompts; WPO's
post-run gate is not an OS sandbox or a replacement for native approval.

Native permission requests are answered in T3. WPO command approvals keep their existing dashboard
queue and file formats; approving one does not grant permission to a separate native T3 prompt.

A launched session with no callback, heartbeat or WP progress for its idle limit (`idle_minutes`) raises one `t3-session-quiet` attention event per quiet period and is still waited for: a long review writes nothing. Only after `t3.hard_idle_minutes` (240) without real activity does it become **stalled**. A request never launched is woken once more at its idle limit, and becomes **stalled** if it is still unlaunched one idle period later. A stalled task may still be running: WPO starts no
replacement and does not silently fall back to a CLI model. A persistent
scope lease prevents another worker from writing in that checkout until the outstanding task is
reconciled. `wpo t3 recover`, or the dashboard's **Recover coordinator** button, wakes outstanding
coordinators once. It never resumes held lanes, polls running tasks, or blindly relaunches them.
Reconcile known native IDs after completion. Only after verifying no native launch occurred may
`wpo t3 retry-unlaunched --request <id>` requeue a stalled unlaunched request with the same retry key.

The dashboard shows native request states, task links, recorded pending native approvals and the
existing WPO board/log views. Opening a native session transcript reads its latest 100 conversation
messages once through the supported `t3 thread show` CLI. Refreshing the transcript explicitly
reads a new snapshot; it does not poll tasks or provide live tool activity. A CLI read failure
is displayed alongside the retained local log. Live provider tool/token telemetry depends on actual callbacks;
missing usage is explicitly unparsed, not a zero-cost session. T3 must be running, and its attached
coordinator provider must be usable. A stopped T3 app or exhausted coordinator quota requires
recovery after availability returns; a Python daemon cannot impersonate an active T3 agent.

## Existing projects and checkout lifecycle

WP files, project config, attempt/findings ledgers, lane holds, events, inbox files, usage caches
and old logs retain their formats. Native metadata is additive under `<runs>/t3/`,
`<runs>/<WP>/t3-workspace.json`, and per-log invocation receipts. `Project.worktree` and the dashboard
resolve the actual registered native checkout and branch.

On the first authorized run of an existing WP, WPO imports its legacy branch, staged changes,
unstaged changes and untracked files into the clean native checkout. It leaves the original
checkout and index intact, rejects linked/escaping destinations, and stops on conflicts. Import
checkpoints deliberately require manual reconciliation after partial application; never rerun an
already-applied patch blindly. This migration does not run when enabling T3 or attaching a thread.

The existing integration gate commits/verifies/fast-forwards the registered native branch under
the same mutex. It retains T3-owned checkout directories and branches; their cleanup belongs to T3.
Configured separate build-output cleanup still runs. Moving the current user thread uses
`t3_worktree_handoff` as the last call, not shell `cd`. Top-level thread APIs are only for an
explicitly requested new top-level thread, not substitutes for delegated tasks.
