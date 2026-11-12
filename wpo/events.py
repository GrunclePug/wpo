"""The orchestrator event journal: tooling reports here, the orchestrator waits on it.

An append-only JSON-lines file in the project's runs directory. Runners, supervisors, jobs and the
router append events; the orchestrator session blocks on new ones from a background shell, so its
harness wakes it the moment something needs a decision and it never has to poll.

Every kind has a fixed severity: "attention" events end a wait, "info" events are printed with the
next attention event (or the timeout heartbeat), so nothing is lost and nothing wakes needlessly.
The cursor is a line count.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

from .platform import FileLock, pid_alive

ATTENTION_KINDS = frozenset({
    "progress-intervention",
    "wp-blocked",
    "no-route",
    "integration-failed",
    "supervisor-finished",
    "supervisor-crashed",
    "supervisor-died",
    "dispatch-done",
    "dispatch-failed",
    # A detached job the orchestrator started (an integration, a rebuild) ended; the message says how.
    "orchestrator-job-done",
    # A WP merged but its worktree or branch could not be removed.
    "cleanup-failed",
    # `wpo run` ended with an error (not a verdict, not a no-route stop); the WP is not progressing.
    "runner-failed",
    # Re-evaluation could not make a WP converge, proposed a split, or gave no decision.
    "wp-needs-orchestrator",
    # A sandboxed session's tool steps left no sandbox decisions: it ran unguarded; check its changes.
    "sandbox-unverified",
    # A session asks to run a command its sandbox denies (`wpo approve|deny <id>`).
    "approval-requested",
    # A review asked for a change only the orchestrator can make (the WP file, owns, a split).
    "review-orchestrator-note",
    # The user's request from the dashboard was placed by a placement agent: in a WP's Context, or a new WP file.
    "user-message-placed",
    # A placement agent could not place the user's request (no route, no decision, an invalid one); the text is in the event.
    "user-message-unplaced",
    # `wpo audit` wrote its report (lint over every open WP, then the review session's judgment); the event names it.
    "audit-done",
    # `wpo audit` crashed before finishing its report; the message says where.
    "audit-failed",
    # A native T3 session has been quiet for `idle_minutes` (no callback, heartbeat or file write);
    # wpo keeps waiting until `t3.hard_idle_minutes`, then marks it stalled.
    "t3-session-quiet",
})
INFO_KINDS = frozenset({
    "boot-changed",
    "wp-done",
    "wp-merged",
    "wp-changes-requested",
    "wp-reevaluated",
    "wp-handed-back",
    "route-fallback",
    # A run ended at the orchestrator's request (`wpo wrap-up`), its round not counted.
    "wp-paused",
    # The orchestrator approved or denied a session's command request.
    "approval-decided",
    # A builder round continued its previous session, or could not and started fresh.
    "session-resumed",
    "resume-failed",
    # A mechanical Verify failure went to a repair session before the builder.
    "repair-attempted",
    "session-hang",
    "session-compacted",
    "tier-downgrade",
    # `wpo route mode` switched the project's routing mode.
    "routing-mode",
    "catalog-refreshed",
    "catalog-unrated",
    "catalog-refresh-failed",
    "staged-plan",
    "staged-task",
    "lane-started",
    "lane-stopped",
    # A builder session ended short of review (no passing wpo verify, not blocked) and is continued without counting a round.
    "session-interrupted",
    # A builder's context or its route's quota reached its handoff point; it was asked for its handoff.
    "handoff-requested",
    # A builder session wrote its handoff and ended; a fresh session continues from it.
    "session-handoff",
    # A usage cap ended its family: a session still running on it was killed and counts as failed mid-write.
    "session-cut-off",
    # Every failed check was the machine's fault (timeout, disk, memory); verification is retried later.
    "verify-deferred",
    # `wpo lanes resume` left a lane unstarted because `max_active_lanes` lanes are live.
    "lane-deferred",
    # The user unblocked a WP from the dashboard (status reset, optional note, lane released and resumed).
    "wp-unblocked",
    # The user's message from the dashboard went to the WP's live session.
    "user-message",
    # The user's message or request from the dashboard went to a placement agent (no live session reads it).
    "user-message-queued",
})
POLL_SECONDS = 5
SUPERVISOR_NAMES = ("python", "python3", "pythonw")
# States a live supervisor writes: working, or asleep until a route or a lane slot frees up.
LIVE_SUPERVISOR_STATES = ("running", "sleeping")


class UnknownEventKindError(ValueError):
    """A kind with no declared severity; declare it above before emitting it."""


def severity_of(kind: str) -> str:
    if kind in ATTENTION_KINDS:
        return "attention"
    if kind in INFO_KINDS:
        return "info"
    raise UnknownEventKindError(f"event kind '{kind}' has no declared severity")


class Journal:
    """The event journal of one project's runs directory."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir
        self.path = runs_dir / "events.jsonl"
        self.on_attention = None

    def emit(self, kind: str, message: str, fields: Mapping[str, object] | None = None) -> dict:
        event = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "kind": kind,
            "severity": severity_of(kind),
            "message": message,
            **{key: str(value) for key, value in (fields or {}).items()},
        }
        # ASCII-escaped JSON never contains a line separator, so one event is exactly one "\n" line.
        with FileLock(self.path.with_suffix(".lock")), self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=True) + "\n")
        if event["severity"] == "attention" and self.on_attention is not None:
            self.on_attention(event)
        return event

    def complete_lines(self) -> list[str]:
        """Every fully written line; a line still being appended (no newline yet) is left out."""
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8", newline="") as handle:
            return handle.read().split("\n")[:-1]

    def read(self, after: int) -> list[dict]:
        return [json.loads(line) for line in self.complete_lines()[after:]]

    def cursor(self) -> int:
        return len(self.complete_lines())

    def report_dead_supervisors(self) -> None:
        """A supervisor killed without writing its state leaves "running" or "sleeping"; report it once."""
        for state_file in self.runs_dir.glob("supervisor-*.state"):
            state = state_file.read_text(encoding="utf-8-sig").strip()
            if state not in LIVE_SUPERVISOR_STATES:
                continue
            pid_file = state_file.with_suffix(".pid")
            if not pid_file.exists():
                continue
            text = pid_file.read_text(encoding="utf-8-sig").strip()
            if not text.isdigit():
                continue  # being written
            pid = int(text)
            if pid_alive(pid, SUPERVISOR_NAMES):
                continue
            name = state_file.stem.removeprefix("supervisor-")
            state_file.write_text("died", encoding="utf-8")
            self.emit("supervisor-died", f"supervisor {name} (pid {pid}) is gone with state '{state}'; its WPs are not progressing", {"supervisor": name})

    def wait(self, after: int, timeout_minutes: float, quiet_kinds: Iterable[str] = ()) -> int:
        """Blocks until an attention event follows `after`; returns 0 on events, 3 on timeout.
        Quiet kinds are attention kinds demoted to info for this wait (expected noise)."""
        quiet = frozenset(quiet_kinds)
        deadline = time.monotonic() + timeout_minutes * 60
        while True:
            self.report_dead_supervisors()
            events = self.read(after)
            if any(event["severity"] == "attention" and event["kind"] not in quiet for event in events):
                print_events(events)
                print(f"cursor={after + len(events)}")
                return 0
            if time.monotonic() >= deadline:
                print_events(events)
                print(f"timeout after {timeout_minutes:g} min with no attention event; cursor={after + len(events)}")
                return 3
            time.sleep(POLL_SECONDS)


class JournalReader:
    """Follows the journal incrementally for a long-running reader (the dashboard): each call reads
    only the bytes appended since the last one, and only whole lines, so an event being appended is
    picked up on the next call. Every event gets its line index, which is the `wait` cursor it
    follows. A journal that shrank (replaced or truncated) is read again from the start."""

    def __init__(self, journal: Journal) -> None:
        self.path = journal.path
        self.offset = 0
        self.lines_read = 0

    def read_new(self) -> list[dict]:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []
        if size < self.offset:
            self.offset, self.lines_read = 0, 0
        if size == self.offset:
            return []
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read(size - self.offset)
        complete = chunk[:chunk.rfind(b"\n") + 1]
        self.offset += len(complete)
        events = []
        for line in complete.decode("utf-8", errors="replace").split("\n")[:-1]:
            index = self.lines_read
            self.lines_read += 1
            try:
                event = json.loads(line)
            except ValueError:
                continue  # a corrupt line keeps its index, as `wait` counts it too
            events.append({**event, "index": index})
        return events


def format_event(event: Mapping[str, object]) -> str:
    extras = " ".join(f"{key}={value}" for key, value in event.items() if key not in {"time", "kind", "severity", "message"})
    return f"{event['time']} [{event['severity']}] {event['kind']}: {event['message']}" + (f" ({extras})" if extras else "")


def print_events(events: Iterable[Mapping[str, object]]) -> None:
    for event in events:
        print(format_event(event))
