"""Tier-routed agent sessions with fallback, liveness watching and compaction stops.

    result = run_session(context, spec="code-hard", access="builder", prompt=text, log_stem=stem, cwd=tree)

The router orders the routes that can serve the spec (free first); they run in that order until one
completes. A session that fails on its route (usage cap, API error, crash, or no activity for
`idle_minutes`) is killed, its route cooled down, a route-fallback event journaled, and the next
route starts in the same tree, resuming from whatever the failed session left (a builder's next
route gets a recovery report: what the failed session wrote, and the Verify output). When no route
is left, NoRouteError carries the time the earliest skipped route frees up.

A usage cap ends every session of its family at once: a session whose family was cut off after it
started is killed and treated as failed mid-write, whatever its log shows. Sessions asked for a
handoff because their quota is about to run out are asked one at a time per family, so the last
of the quota finishes one handoff instead of cutting several off halfway.

Activity is log growth, or CPU used by the session's tool processes: a tool call (a long cargo
build) writes nothing to the log until it ends.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .commands import HEARTBEAT_FILE
from .context import Context
from .harness import CLAUDE_CLI_HARNESSES, GATE_ACCESS, HARNESS_PROCESS_NAMES, PROXY_HARNESSES, WRITES_LISTED_PATHS, HarnessError, agy_tool_steps, build_launch, handoff_limit, proxy_refusal, validate_compaction_limits
from .inbox import MESSAGE_PREFIX, clear_live, mark_delivered, pending_messages, write_live
from .platform import FileLock, NamedMutex, harness_environment, kill_tree, pid_alive, tool_cpu_seconds
from .routing import COMPACTION_MARKER, RATE_LIMIT_TEXT, SUBSCRIPTION_FAMILIES, SessionOutcome, classify, read_json, write_atomically
from .session_brief import SessionBrief, build_brief

LIVENESS_POLL_SECONDS = 15
# A Claude-CLI stream-json log shows this once the model's first turn arrived.
CLAUDE_FIRST_TURN_MARKER = b'"type":"assistant"'
# A second of tool CPU per poll separates a working build from idle helper processes.
TOOL_CPU_ACTIVITY_SECONDS = 1.0
COMPACTION_SCAN_BYTES = 1 << 20
# A session compacting this long without any other event is stuck: the CLI times its compaction
# request out and retries it, and the local model never answers one in time (2026-10-26: 80 min).
COMPACTION_STUCK_MINUTES = 30
# How often a running builder's route is checked for a usage window about to run out.
QUOTA_CHECK_SECONDS = 60
# How soon a session waiting for its family's handoff slot asks again.
HANDOFF_SLOT_RECHECK_SECONDS = 60
# A handoff slot older than this is abandoned (its holder died without releasing it).
HANDOFF_SLOT_MINUTES = 20
# How often a running session's family is checked for a cap that ended it.
CUT_OFF_CHECK_SECONDS = 60


class NoRouteError(Exception):
    """Every route that could serve a spec is unavailable or failed in this call."""

    def __init__(self, message: str, next_available_at: float | None, failures: list[str] | None = None) -> None:
        super().__init__(message)
        self.next_available_at = next_available_at
        # The sessions that ran and failed in this call; empty when no route could even start.
        self.failures = list(failures or [])


class ResumeFailed(Exception):
    """A session that may only continue a previous one (`resume_only`) could not continue it."""


class CompactionStop(Exception):
    """The session reached automatic compaction and was stopped (staged mode's context budget)."""

    def __init__(self, route_id: str, log_path: Path) -> None:
        super().__init__(f"session on {route_id} reached compaction ({log_path})")
        self.route_id = route_id
        self.log_path = log_path


class SessionCancelled(RuntimeError):
    """A native session was cancelled on purpose: by the operator (`wpo t3 cancel`, `wpo lanes
    stop`) or by the runner's worker cleanup. Unlike a failure, it marks no route and starts no
    other one."""

    def __init__(self, route_id: str, origin: str, detail: str = "") -> None:
        super().__init__(f"session on {route_id} cancelled by {origin}{f': {detail}' if detail else ''}")
        self.route_id = route_id
        self.origin = origin


@dataclass
class SessionResult:
    route: dict
    result_text: str
    log_path: Path
    failures: list[str] = field(default_factory=list)
    # Why the session was asked for its handoff while it ran, if it was.
    handoff_reason: str | None = None

    @property
    def fallback_note(self) -> str:
        return f" after fallbacks ({'; '.join(self.failures)})" if self.failures else ""


@dataclass
class WatchedExit:
    exit_code: int
    hung: bool = False
    # Hung before the model's first turn arrived (`first_output_minutes`).
    before_first_turn: bool = False
    compacted: bool = False
    fatal: str | None = None  # the stderr line that stopped the session
    # Why the session was asked to hand off (its context, its route's quota), if it was.
    handoff_reason: str | None = None
    # The CLI's compaction request kept failing (each attempt timed out and was retried).
    compaction_stuck: bool = False
    # Why the session's provider stopped answering (a usage cap); the session was killed.
    cut_off: str | None = None


@dataclass(frozen=True)
class HandoffTrigger:
    """When a builder session is asked to write a handoff and stop, so a fresh session continues
    from it: its context reached `context_tokens` (before the harness's own compaction, which on
    the local model never completes), or `quota_reason()` names a usage window about to run out
    (so the summary is written while the route can still answer)."""

    context_tokens: int
    quota_reason: Callable[[], str | None]
    message: Callable[[str], str]
    # The family's one handoff slot for quota handoffs; None asks without waiting for it.
    quota_slot: QuotaHandoffSlot | None = None


class QuotaHandoffSlot:
    """One quota handoff at a time per subscription family, machine-wide. Several sessions asked at
    once spend the family's last quota on several half-written handoffs; one at a time, each
    handoff completes before the next starts. A slot whose holder died or held it past
    HANDOFF_SLOT_MINUTES is free."""

    def __init__(self, state_dir: Path, family: str, holder: str) -> None:
        self.path = state_dir / f"quota-handoff-{family}.json"
        self.lock = state_dir / f"quota-handoff-{family}.lock"
        self.holder = holder
        self.held = False

    def try_claim(self) -> bool:
        with FileLock(self.lock):
            current = read_json(self.path, None)
            if current is not None and current["holder"] != self.holder and pid_alive(int(current["pid"])) \
                    and time.time() - float(current["since"]) < HANDOFF_SLOT_MINUTES * 60:
                return False
            write_atomically(self.path, {"holder": self.holder, "pid": os.getpid(), "since": time.time()})
            self.held = True
            return True

    def release(self) -> None:
        if not self.held:
            return
        with FileLock(self.lock):
            current = read_json(self.path, None)
            if current is not None and current["holder"] == self.holder:
                self.path.unlink(missing_ok=True)
        self.held = False


def turn_context_tokens(event: dict) -> int:
    """The context of one assistant turn of a Claude-CLI stream-json log, in tokens."""
    usage = event.get("message", {}).get("usage") or {}
    return sum(usage.get(key) or 0 for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def _log_size(*paths: Path) -> int:
    return sum(path.stat().st_size for path in paths if path.exists())


def _compacted(log_path: Path, scanned_to: int) -> tuple[bool, int]:
    """Scans the log's new bytes for the compaction marker; returns (found, new offset)."""
    if not log_path.exists():
        return False, scanned_to
    with log_path.open("rb") as handle:
        handle.seek(max(0, scanned_to - len(COMPACTION_MARKER)))
        chunk = handle.read(COMPACTION_SCAN_BYTES)
        offset = handle.tell()
    return COMPACTION_MARKER.encode() in chunk.replace(b" ", b""), offset


def _fatal_line(stderr_path: Path, pattern: re.Pattern[str] | None, scanned_to: int) -> tuple[str | None, int]:
    """Scans stderr's new bytes for a line matching `pattern`; returns (line, new offset)."""
    if pattern is None or not stderr_path.exists():
        return None, scanned_to
    with stderr_path.open("rb") as handle:
        handle.seek(scanned_to)
        chunk = handle.read()
    complete = chunk[:chunk.rfind(b"\n") + 1]
    for line in complete.decode("utf-8", errors="replace").splitlines():
        if pattern.search(line):
            return line.strip()[:400], scanned_to + len(complete)
    return None, scanned_to + len(complete)


class StreamInput:
    """Feeds a stream-json session (`--input-format stream-json`): its prompt, then the orchestrator's
    inbox messages between turns. Closing stdin ends the session, so it stays open until the session
    is idle: a result answered the last message with nothing queued, no background task is open, and
    the inbox is empty. A background task's completion starts a new turn, so a session waiting on
    one is not idle."""

    def __init__(self, process: subprocess.Popen, log_path: Path, inbox: Path | None) -> None:
        self.process = process
        self.log_path = log_path
        self.inbox = inbox
        self.delivery_log = log_path.with_name(log_path.name + ".inbox.log")
        self.open_tasks = 0
        self.awaiting_result = True
        self.scanned = 0
        self.closed = False
        self.initial: threading.Thread | None = None
        # The context of the session's latest own turn, in tokens (subagent turns not counted).
        self.context_tokens = 0
        # When the session began compacting, None while it is not: a compaction request that never
        # completes (the CLI times it out and retries) leaves only compacting statuses in the log.
        self.compacting_since: float | None = None

    def start(self, prompt: str) -> None:
        # The prompt can be large: written from a thread so a harness that dies first cannot block the watcher.
        self.initial = threading.Thread(target=self._write, args=(prompt,), daemon=True)
        self.initial.start()

    def _write(self, text: str) -> None:
        line = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"
        try:
            self.process.stdin.write(line.encode("utf-8"))
            self.process.stdin.flush()
        except OSError:
            self.closed = True  # the harness exited; its exit code reports why

    @property
    def answered(self) -> bool:
        """Whether a result answered the last message and nothing is queued."""
        return not self.awaiting_result

    def close(self) -> None:
        self.closed = True
        try:
            self.process.stdin.close()
        except OSError:
            pass

    def _scan(self) -> None:
        if not self.log_path.exists():
            return
        with self.log_path.open("rb") as handle:
            handle.seek(self.scanned)
            chunk = handle.read()
        complete = chunk[:chunk.rfind(b"\n") + 1]
        self.scanned += len(complete)
        for line in complete.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = (event.get("type"), event.get("subtype"))
            if kind == ("system", "status") and event.get("status") == "compacting":
                if self.compacting_since is None:
                    self.compacting_since = time.monotonic()
                continue
            self.compacting_since = None
            if event.get("type") == "assistant" and not event.get("parent_tool_use_id"):
                context = turn_context_tokens(event)
                if context:
                    self.context_tokens = context
            if kind == ("system", "task_started"):
                self.open_tasks += 1
            elif kind == ("system", "task_notification"):
                self.open_tasks -= 1
                # A background task that ends after the session answered starts its next turn:
                # closing stdin here would cut that turn off from the inbox.
                self.awaiting_result = True
            elif event.get("type") == "result" and not event.get("queued_turn_count"):
                self.awaiting_result = False

    def send(self, text: str) -> None:
        """Sends a message the session reads at its next tool-call boundary, or as its next turn."""
        self._write(text)
        self.awaiting_result = True

    @property
    def ready(self) -> bool:
        """Whether the prompt is written and the session is still open, so a message can follow."""
        return not self.closed and (self.initial is None or not self.initial.is_alive())

    def poll(self) -> None:
        if not self.ready:
            return
        self._scan()
        for message in pending_messages(self.inbox) if self.inbox is not None else []:
            text = message.read_text(encoding="utf-8")
            self.send(MESSAGE_PREFIX + text)
            mark_delivered(message)
            with self.delivery_log.open("a", encoding="utf-8") as record:
                record.write(f"{datetime.now().isoformat(timespec='seconds')} delivered {message.name}: {text.strip()[:300]}\n")
        if not self.awaiting_result and self.open_tasks <= 0:
            self.close()


class HandoffWatch:
    """Asks a stream-json session for its handoff once, when its trigger fires."""

    def __init__(self, trigger: HandoffTrigger) -> None:
        self.trigger = trigger
        self.reason: str | None = None
        self.quota_checked_at = float("-inf")

    def poll(self, stream: StreamInput) -> None:
        if self.reason is not None or not stream.ready:
            return
        reason = None
        if stream.context_tokens >= self.trigger.context_tokens:
            reason = f"your context reached {stream.context_tokens:,} tokens (the handoff point is {self.trigger.context_tokens:,})"
        elif time.monotonic() - self.quota_checked_at >= QUOTA_CHECK_SECONDS:
            self.quota_checked_at = time.monotonic()
            reason = self.trigger.quota_reason()
            if reason and self.trigger.quota_slot is not None and not self.trigger.quota_slot.try_claim():
                # Another session of the family is writing its handoff: ask again shortly.
                self.quota_checked_at = time.monotonic() - QUOTA_CHECK_SECONDS + HANDOFF_SLOT_RECHECK_SECONDS
                reason = None
        if reason:
            self.reason = reason
            stream.send(self.trigger.message(reason))


def watch_process(arguments: list[str], env: Mapping[str, str], stdin_text: str, cwd: Path, log_path: Path,
                  idle_minutes: float, stop_on_compaction: bool, fatal_stderr: re.Pattern[str] | None = None,
                  stream_input: bool = False, inbox: Path | None = None, background_minutes: float = 0.0,
                  first_turn_minutes: float = 0.0, handoff: HandoffTrigger | None = None,
                  cut_off: Callable[[], str | None] | None = None) -> WatchedExit:
    """Runs a harness to its end. With `stream_input` the prompt goes in as a stream-json message and
    `inbox` messages follow between turns (StreamInput); otherwise stdin is the prompt, then closed.
    `background_minutes` is how long a session that answered may wait on its own background task
    without visible activity (a long test run reading files uses little CPU). With
    `first_turn_minutes`, a Claude-CLI session whose model has not answered once by then is hung.
    With `handoff` (stream sessions only), the session is asked once for a handoff when it fires.
    `cut_off()`, checked every CUT_OFF_CHECK_SECONDS, names why the session's provider stopped
    answering; the session is then killed."""
    stderr_path = log_path.with_name(log_path.name + ".stderr")
    with log_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        creation = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        process = subprocess.Popen(arguments, cwd=str(cwd), env=dict(env), stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, creationflags=creation)
        stream = StreamInput(process, log_path, inbox) if stream_input else None
        if stream is not None:
            stream.start(stdin_text)
            if inbox is not None:
                write_live(inbox, log_path, process.pid)
        else:
            def feed() -> None:
                try:
                    process.stdin.write(stdin_text.encode("utf-8"))
                    process.stdin.close()
                except OSError:
                    pass  # the harness exited before reading its prompt; its exit code reports why

            threading.Thread(target=feed, daemon=True).start()
        handoff_watch = HandoffWatch(handoff) if handoff is not None and stream is not None else None
        try:
            exited = _watch(process, log_path, stderr_path, idle_minutes, stop_on_compaction, fatal_stderr, stream, background_minutes,
                            first_turn_minutes, handoff_watch, cut_off)
            exited.handoff_reason = handoff_watch.reason if handoff_watch is not None else None
            return exited
        finally:
            if handoff is not None and handoff.quota_slot is not None:
                handoff.quota_slot.release()
            if stream is not None and inbox is not None:
                clear_live(inbox)


def _first_turn_arrived(log_path: Path) -> bool:
    if not log_path.exists():
        return False
    with log_path.open("rb") as handle:
        return CLAUDE_FIRST_TURN_MARKER in handle.read().replace(b" ", b"")


def _heartbeat(log_path: Path) -> float:
    beat = log_path.parent / HEARTBEAT_FILE
    return beat.stat().st_mtime if beat.exists() else 0.0


def _watch(process: subprocess.Popen, log_path: Path, stderr_path: Path, idle_minutes: float, stop_on_compaction: bool,
           fatal_stderr: re.Pattern[str] | None, stream: StreamInput | None, background_minutes: float = 0.0,
           first_turn_minutes: float = 0.0, handoff: HandoffWatch | None = None,
           cut_off: Callable[[], str | None] | None = None) -> WatchedExit:
    idle_limit = idle_minutes * 60
    started_at = time.monotonic()
    cut_off_checked_at = started_at
    first_turn_seen = first_turn_minutes <= 0
    last_size, last_activity, last_cpu, scanned, stderr_scanned = -1, started_at, 0.0, 0, 0
    last_beat = _heartbeat(log_path)
    while True:
        try:
            return WatchedExit(process.wait(timeout=LIVENESS_POLL_SECONDS))
        except subprocess.TimeoutExpired:
            pass
        if stream is not None:
            stream.poll()
            if handoff is not None:
                handoff.poll(stream)
            if stream.compacting_since is not None and time.monotonic() - stream.compacting_since >= COMPACTION_STUCK_MINUTES * 60:
                kill_tree(process.pid)
                process.wait()
                return WatchedExit(-1, hung=True, compaction_stuck=True)
        if cut_off is not None and time.monotonic() - cut_off_checked_at >= CUT_OFF_CHECK_SECONDS:
            cut_off_checked_at = time.monotonic()
            if reason := cut_off():
                kill_tree(process.pid)
                process.wait()
                return WatchedExit(-1, cut_off=reason)
        if not first_turn_seen:
            first_turn_seen = _first_turn_arrived(log_path)
            if not first_turn_seen and time.monotonic() - started_at >= first_turn_minutes * 60:
                kill_tree(process.pid)
                process.wait()
                return WatchedExit(-1, hung=True, before_first_turn=True)
        beat = _heartbeat(log_path)
        if beat != last_beat:
            last_beat, last_activity = beat, time.monotonic()
            continue
        fatal, stderr_scanned = _fatal_line(stderr_path, fatal_stderr, stderr_scanned)
        if fatal:
            kill_tree(process.pid)
            process.wait()
            return WatchedExit(-1, fatal=fatal)
        if stop_on_compaction:
            found, scanned = _compacted(log_path, scanned)
            if found:
                kill_tree(process.pid)
                process.wait()
                return WatchedExit(-1, compacted=True)
        size = _log_size(log_path, stderr_path)
        if size != last_size:
            last_size, last_activity = size, time.monotonic()
            continue
        cpu = tool_cpu_seconds(process.pid, HARNESS_PROCESS_NAMES)
        if cpu - last_cpu >= TOOL_CPU_ACTIVITY_SECONDS:
            last_cpu, last_activity = cpu, time.monotonic()
            continue
        last_cpu = cpu
        if time.monotonic() - last_activity < idle_limit:
            continue
        if stream is not None and stream.answered:
            if stream.open_tasks > 0 and time.monotonic() - last_activity < max(idle_limit, background_minutes * 60):
                # Answered while its background task runs: the session is waiting for that task's
                # result (a verify run), which a command may take up to the project's timeout to give.
                continue
            # Only a background task it left behind (a server, a stuck command) keeps it open past
            # that: its work is done, so it ends rather than counting as hung.
            stream.close()
            try:
                return WatchedExit(process.wait(timeout=60))
            except subprocess.TimeoutExpired:
                kill_tree(process.pid)
                process.wait()
                return WatchedExit(0)
        kill_tree(process.pid)
        process.wait()
        return WatchedExit(-1, hung=True)


@dataclass(frozen=True)
class ResumeTarget:
    """A previous session to continue on its own route, and the prompt that continues it."""

    route_id: str
    session_id: str
    prompt: str


@dataclass(frozen=True)
class SessionSettings:
    """What every attempt of one run_session call shares."""

    access: str
    write_paths: list[str]
    cwd: Path
    brief: SessionBrief
    extra_env: Mapping[str, str]
    lead: bool
    stop_on_compaction: bool
    idle_minutes: float
    fields: dict
    # The WP's inbox, for the orchestrator's messages to this session (inbox.py); None for none.
    inbox: Path | None = None
    # The message asking a builder for its handoff, given why; None for sessions that hand off nothing.
    handoff_message: Callable[[str], str] | None = None


def attempt(context: Context, route: dict, prompt: str, log_path: Path, settings: SessionSettings,
            resume_session: str | None = None) -> SessionOutcome | None:
    """One session on one route; None when the local model is busy (skipped, not failed). Raises
    CompactionStop when the session reached compaction and was asked to stop there."""
    if context.machine.execution_mode == "t3":
        from .t3 import run_attempt
        return run_attempt(context, route, prompt, log_path, settings, resume_session)
    context.machine.require_standalone("launching a standalone model session")
    local_mutex = None
    if route["family"] == "local":
        # One local session at a time machine-wide; a busy local model is skipped, not awaited.
        local_mutex = NamedMutex(str(context.machine.local("mutex")))
        if not local_mutex.acquire(blocking=False):
            return None
    free = route["cost_class"] == "free"
    idle_minutes = min(settings.idle_minutes, float(context.project.config["free_idle_minutes"])) if free else settings.idle_minutes
    session_prompt = prompt.replace("{{ROUTE}}", route["id"])
    prompt_record = log_path.with_name(log_path.name + ".prompt.md")
    prompt_record.write_text(session_prompt, encoding="utf-8")
    try:
        launch = build_launch(context.machine, context.project.config, route, settings.access, settings.write_paths, session_prompt,
                              settings.cwd, settings.brief, log_path.with_name(log_path.name + ".system.md"), settings.lead, resume_session)
        # What the session reads on stdin, the brief included on harnesses that take it there.
        prompt_record.write_text(launch.stdin_text, encoding="utf-8")
        if route["harness"] in PROXY_HARNESSES and (refusal := proxy_refusal(launch.env)):
            return SessionOutcome("rate_limited", f"the proxy refused before launch: {refusal}", "")
        # The session's own identity, for `wpo log` (its Progress log key) and `wpo verify`.
        identity = {"WPO_ROUTE": route["id"], "WPO_ROLE": settings.fields.get("role", settings.access)}
        env = harness_environment({**settings.extra_env, **launch.env, **identity, "PWD": str(settings.cwd)})
        # Only hosted subscriptions: a local model's prefill and a free route's queue are slow by nature.
        first_turn_minutes = (float(context.project.config["first_output_minutes"])
                              if route["harness"] in CLAUDE_CLI_HARNESSES and route["family"] in SUBSCRIPTION_FAMILIES else 0.0)
        handoff = None
        if settings.handoff_message is not None and route["harness"] in CLAUDE_CLI_HARNESSES:
            slot = (QuotaHandoffSlot(context.router.state_dir, route["family"], str(log_path))
                    if route["family"] in SUBSCRIPTION_FAMILIES else None)
            handoff = HandoffTrigger(handoff_limit(context.project.config, route["harness"]), lambda: quota_running_out(context, route),
                                     settings.handoff_message, slot)
        launched_at = time.time()
        cut_off = ((lambda: context.router.family_cut_off(route["family"], launched_at))
                   if route["family"] in SUBSCRIPTION_FAMILIES else None)
        watched = watch_process(launch.arguments, env, launch.stdin_text, settings.cwd, log_path, idle_minutes,
                                settings.stop_on_compaction, launch.fatal_stderr, launch.stream_input, settings.inbox,
                                float(context.project.config["command_timeout_minutes"]), first_turn_minutes, handoff, cut_off)
    except (HarnessError, OSError) as error:
        # A route that cannot start here (missing key, proxy down, executable gone) is a failed
        # session on that route: cool it down and fall back.
        return SessionOutcome("session_error", f"could not launch: {type(error).__name__}: {error}", "")
    finally:
        if local_mutex is not None:
            local_mutex.release()
    fields = {**settings.fields, "route": route["id"]}
    if watched.handoff_reason:
        context.journal.emit("handoff-requested", f"{route['id']} session asked for its handoff: {watched.handoff_reason} ({log_path.name})", fields)
    if watched.compacted:
        context.journal.emit("session-compacted", f"{route['id']} reached compaction and was stopped ({log_path})", fields)
        raise CompactionStop(route["id"], log_path)
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    # Before any other outcome: an unguarded session must be reported however it ended.
    if launch.sandbox_log is not None:
        decisions = launch.sandbox_log.read_text(encoding="utf-8").count("\n") if launch.sandbox_log.exists() else 0
        steps = agy_tool_steps(log_text)
        if decisions < steps:
            # Some tool steps passed no hook (agy loaded no hooks, or stopped loading them): those ran
            # unguarded. Causes: hooks.json unreadable, or the policy path not reaching the hook.
            context.journal.emit("sandbox-unverified", f"{route['id']} ran {steps} tool steps but the sandbox decided {decisions}; "
                                 f"review what it changed ({log_path}; policy {launch.sandbox_log.parent})", fields)
            return SessionOutcome("session_error", f"sandbox decided {decisions} of {steps} tool steps", "")
    if watched.cut_off:
        context.journal.emit("session-cut-off", f"{route['id']} session killed: {watched.cut_off}; it is treated as failed mid-write ({log_path.name})", fields)
        return SessionOutcome("rate_limited", watched.cut_off, "")
    if watched.fatal:
        return SessionOutcome("rate_limited" if RATE_LIMIT_TEXT.search(watched.fatal) else "api_error", watched.fatal, "")
    if watched.hung:
        stalled = (f"gave no first model turn in {first_turn_minutes:g} min" if watched.before_first_turn
                   else f"kept failing to compact for {COMPACTION_STUCK_MINUTES} min" if watched.compaction_stuck
                   else f"showed no activity for {idle_minutes:g} min")
        context.journal.emit("session-hang", f"{route['id']} {stalled} and was killed ({log_path})", fields)
        if route["harness"] in PROXY_HARNESSES and (refusal := proxy_refusal(launch.env)):
            # The CLI was retrying a cap silently: a rate limit, which cools the whole family down.
            return SessionOutcome("rate_limited", f"hung while the proxy refused: {refusal}", "")
        return SessionOutcome("hang", f"{stalled}; process tree killed", "")
    outcome = classify(route["harness"], log_text, watched.exit_code)
    outcome.handoff_reason = watched.handoff_reason
    return outcome


def quota_running_out(context: Context, route: dict) -> str | None:
    """Why the route's subscription is about to stop answering, or None: the window that runs
    out first reaches its cap within the router's `burn_forecast.lead_minutes` at the recent burn
    rate (parallel sessions spend a window fast; the lead leaves time for the handoff), or a window
    is at or past the project's `handoff.quota_percent` whatever the rate."""
    if route["family"] not in SUBSCRIPTION_FAMILIES:
        return None
    if forecast := context.router.burning_out(route["family"]):
        return f"{forecast.text}, which will cut this session off"
    limit = float(context.project.config["handoff"]["quota_percent"])
    windows = context.router.usage_windows().get(route["family"]) or []
    full = [window for window in windows if float(window["used_percent"]) >= limit]
    return "; ".join(f"the {route['family']} {window['label']} usage window is at {window['used_percent']}% and will cut this session off soon"
                     for window in full) or None


def route_log_path(log_stem: Path, route: dict, suffix: str = "") -> Path:
    return Path(f"{log_stem}.{route['id'].replace('/', '_').replace(':', '_')}{suffix}.log")


def run_session(context: Context, spec: str, access: str, prompt: str, log_stem: Path, cwd: Path,
                write_paths: Iterable[str] = (), families: Iterable[str] = (), idle_minutes: float | None = None,
                event_fields: Mapping[str, str] | None = None, extra_env: Mapping[str, str] | None = None,
                lead: bool = False, stop_on_compaction: bool = False, skills: Iterable[str] = (),
                resume: ResumeTarget | None = None, inbox: Path | None = None, resume_only: bool = False,
                handoff_message: Callable[[str], str] | None = None, recovery: Callable[[str], str] | None = None,
                on_attempt: Callable[[], None] | None = None, exclude_model_families: Iterable[str] = ()) -> SessionResult:
    """Runs one session on the first route that completes it; `{{ROUTE}}` in the prompt becomes
    the route id. `skills` are loaded into the session's brief. With `resume`, that session is
    continued first when its route is still a candidate; a resume that does not complete falls
    back to a fresh session with `prompt`, without cooling the route down (a lost conversation is
    not the route's fault), or with `resume_only` raises ResumeFailed. With `handoff_message`, a
    Claude-CLI session is asked for its handoff when its context or its route's quota runs out
    (HandoffTrigger). With `recovery`, a session that fails on its route is assumed to have stopped
    mid-write: `recovery(why)` reports what it left, and the next route's prompt ends with that
    report. The report is made only once a next route is chosen: its Verify run can take an hour,
    wasted when no route is left. `exclude_model_families` leaves out the routes whose models those
    families made (an independent review). Raises NoRouteError when no route is left, CompactionStop when asked to. `on_attempt()` runs each
    time a chosen route is about to start a session."""
    if access in WRITES_LISTED_PATHS and not list(write_paths):
        raise ValueError(f"access '{access}' needs write paths")
    # A bad compaction point is a configuration error, not a failure of each route in turn.
    validate_compaction_limits(context.project.config)
    settings = SessionSettings(access, list(write_paths), cwd, build_brief(context, cwd, skills), dict(extra_env or {}), lead, stop_on_compaction,
                               idle_minutes if idle_minutes is not None else float(context.project.config["idle_minutes"]), dict(event_fields or {}),
                               inbox, handoff_message)
    if resume_only and resume is None:
        raise ValueError("resume_only needs a session to resume")
    families = list(families)
    failures: list[str] = []
    tried: set[str] = set()
    base_prompt = prompt
    log_stem.parent.mkdir(parents=True, exist_ok=True)
    free_last_resort = access not in GATE_ACCESS
    # Why the last session failed, until the next route's prompt carries the recovery report.
    recovery_due: str | None = None
    if resume is not None:
        selection = context.router.select(spec, families or None, free_last_resort=free_last_resort, gate=not free_last_resort,
                                          exclude_model_families=exclude_model_families)
        route = next((candidate for candidate in selection.candidates if candidate["id"] == resume.route_id), None)
        if route is None:
            if resume_only:
                raise ResumeFailed(f"{resume.route_id} is not available for '{spec}'")
            context.journal.emit("resume-failed", f"{resume.route_id} is not available for '{spec}'; starting a fresh session", settings.fields)
        else:
            log_path = route_log_path(log_stem, route, ".resumed")
            if on_attempt is not None:
                on_attempt()
            outcome = attempt(context, route, resume.prompt, log_path, settings, resume.session_id)
            if outcome is not None and outcome.outcome == "ok":
                context.journal.emit("session-resumed", f"continued session {resume.session_id} on {route['id']} ({log_path.name})",
                                     {**settings.fields, "route": route["id"]})
                return SessionResult(route, outcome.result_text, log_path, failures, outcome.handoff_reason)
            detail = "local model busy" if outcome is None else f"{outcome.outcome}: {outcome.detail}"
            if resume_only:
                raise ResumeFailed(f"continuing {resume.session_id} on {route['id']} failed ({detail})")
            context.journal.emit("resume-failed", f"continuing {resume.session_id} on {route['id']} failed ({detail}); starting a fresh session",
                                 {**settings.fields, "route": route["id"]})
            if outcome is not None:
                recovery_due = f"the continued session on {route['id']} failed ({detail})"
    while True:
        selection = context.router.select(spec, families or None, free_last_resort=free_last_resort, gate=not free_last_resort,
                                          exclude_model_families=exclude_model_families)
        route = next((candidate for candidate in selection.candidates if candidate["id"] not in tried), None)
        if route is None:
            skipped = "; ".join(f"{entry['id']}: {entry['reason']}" for entry in selection.skipped)
            raise NoRouteError(f"no route left for '{spec}'. Failed here: {'; '.join(failures) or 'none'}. Unavailable: {skipped or 'none'}",
                               selection.next_available_at, [failure for failure in failures if "busy with another session" not in failure])
        tried.add(route["id"])
        if recovery_due is not None and recovery is not None:
            prompt = f"{base_prompt}\n\n{recovery(recovery_due)}"
            recovery_due = None
        if route["downgrade"]:
            context.journal.emit("tier-downgrade", f"'{spec}' runs on {route['id']} (score {route['score']}) because nothing at the tier floor is available",
                                 {**settings.fields, "route": route["id"]})
        log_path = route_log_path(log_stem, route)
        if on_attempt is not None:
            on_attempt()  # a route was chosen: the session is about to start on it
        outcome = attempt(context, route, prompt, log_path, settings)
        if outcome is None:
            failures.append(f"{route['id']}: busy with another session")
            continue
        if outcome.outcome == "ok":
            return SessionResult(route, outcome.result_text, log_path, failures, outcome.handoff_reason)
        context.router.mark(route["id"], outcome.outcome, outcome.detail)
        failures.append(f"{route['id']}: {outcome.outcome} ({outcome.detail})")
        recovery_due = f"the session on {route['id']} failed ({outcome.outcome}: {outcome.detail})"
        context.journal.emit("route-fallback", f"{route['id']} failed for '{spec}' ({outcome.outcome}: {outcome.detail}); trying the next route",
                             {**settings.fields, "route": route["id"]})


def session_facts(log_path: Path) -> tuple[str | None, int]:
    """The session id from a Claude-CLI stream-json log and the largest context any of its own
    turns had, in tokens (0 when no turn reported usage). The maximum over turns, not the last turn:
    streams repeat a message's usage as it grows, and a final message may report none."""
    session_id, largest = None, 0
    with log_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init" and session_id is None:
                session_id = event.get("session_id")
            elif event.get("type") == "assistant" and not event.get("parent_tool_use_id"):
                largest = max(largest, turn_context_tokens(event))
    return session_id, largest


def describe_next(next_available_at: float | None) -> str:
    if next_available_at is None:
        return "unknown"
    return datetime.fromtimestamp(next_available_at, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
