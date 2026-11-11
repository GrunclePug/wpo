"""What the dashboard shows, assembled from a project's run state without disturbing it.

Everything here only reads. Runners, supervisors and sessions append to logs and journals and
replace ledgers while the dashboard looks, so:

- a file is read again only when its modification time or size changed, in one read that closes at
  once: on Windows an open handle makes the runner's atomic replace of that file fail, so reads
  happen right after a writer finished and never sit in a loop over unchanged files;
- a file caught half written (a ledger written in place, a pid file being written) keeps its last
  whole value until the next pass;
- logs and the journal are followed incrementally, whole lines only;
- no lock the tooling takes is ever taken here, and usage is read from the cache, never fetched.

The modules that own each piece of state are reused for its meaning: `lanes` (holds, supervisor
pids, pool lanes), `context` (effective WP files), `runner` (the round ledger), `findings` (open
findings), `inbox` (live sessions, the lane's running WP), `routing` (usage windows, burn forecast,
cooldowns), `usage` (session log names), `whats_new` (merges).
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from . import findings, wpfile
from .approvals import APPROVALS_DIRECTORY
from .context import Context
from .events import JournalReader
from .harness import CLAUDE_CLI_HARNESSES, handoff_limit
from .inbox import LIVE_FILE, RUNNING_LINE, inbox_of, running_wp_in_log
from .lanes import pool_lanes, read_holds, supervisor_pid
from .machine import MachineConfig
from .platform import pid_alive
from .project import CONFIG_RELATIVE_PATH
from .routing import SUBSCRIPTION_FAMILIES, Route, from_timestamp, now_utc, paced_weekly_ceiling, static_routes
from .runner import LEDGER_FILE, NO_ROUTE_FILE, RoundLedger
from .session import route_log_path, turn_context_tokens
from .supervisor import ROUTE_WAIT_FILE
from .usage import SESSION_LOG, route_family
from .whats_new import merged_wps

T = TypeVar("T")

# Statuses the WP table lists; done WPs appear only as merges and milestone counts.
LISTED_STATUSES = ("in_progress", "review", "changes_requested", "blocked", "todo")
# Journal kinds that hand a WP to the orchestrator (a WP left in review keeps status `review`), and
# kinds after which it is moving again; the latest of either says whether it waits on someone.
HELD_KINDS = frozenset({"wp-blocked", "wp-needs-orchestrator"})
MOVING_KINDS = frozenset({"wp-unblocked", "wp-done", "wp-merged", "wp-changes-requested", "wp-reevaluated", "wp-paused", "session-resumed"})
# States a supervisor writes while its process should be alive.
LIVE_STATES = ("running", "sleeping")
# The journal's recent events kept in memory for new clients.
EVENT_HISTORY = 400
RECENT_MERGES = 15
# Insurance against a moved main the ref stamps missed (a ref rewritten within one clock tick).
MAIN_RECHECK_SECONDS = 60
MERGES_RECHECK_SECONDS = 300
# The burn forecast counts from now, so it is recomputed this often even when no reading arrived.
FORECAST_RECHECK_SECONDS = 30
# A supervisor log is read from its end: a lane's current run fits in far less.
SUPERVISOR_LOG_TAIL_BYTES = 64 * 1024
# A session log's first read scans at most this much from its end; later reads follow it.
SESSION_LOG_FIRST_SCAN_BYTES = 32 * 1024 * 1024
SLEEP_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) sleeping until (\d{4}-\d{2}-\d{2} \d{2}:\d{2}): (.*)$")
LOG_LINE_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$")
CHECKBOX = re.compile(r"^\s*[-*] \[([ xX])\]\s*(.*)$")
PROGRESS_ENTRY = re.compile(r"^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (?:\[([^\]]*)\] )?(.*)$")
ATTEMPT_PREFIX = re.compile(r"^attempt-\d+-")
ASSISTANT_LINE_PREFIX = b'{"type":"assistant"'
# Run-directory files the detail view links to.
LINKED_SUFFIXES = (".log", ".md", ".json", ".stderr", ".txt")


class DashboardDataError(Exception):
    """A request the dashboard cannot answer (an unknown WP, a path outside the runs directory)."""


Stamp = tuple[int, int]


def file_stamp(path: Path) -> Stamp | None:
    """(modification time in ns, size), or None when the file does not exist."""
    try:
        status = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return status.st_mtime_ns, status.st_size


class StampedCache:
    """Values derived from files, recomputed only when a file's stamp changes. A computation that
    fails on a torn read (bad JSON, a partial number) keeps the previous value and is retried on
    the next pass, since a writer is mid-write."""

    def __init__(self) -> None:
        self._entries: dict[Path, tuple[Stamp | None, object]] = {}

    def get(self, path: Path, compute: Callable[[Path], T], missing: T) -> T:
        stamp = file_stamp(path)
        if stamp is None:
            self._entries.pop(path, None)
            return missing
        entry = self._entries.get(path)
        if entry is not None and entry[0] == stamp:
            return entry[1]  # type: ignore[return-value]
        try:
            value = compute(path)
        except (ValueError, UnicodeDecodeError, KeyError, TypeError):
            if entry is not None:
                # A repaired same-size file can reuse an earlier Windows timestamp.
                self._entries[path] = (None, entry[1])
            return entry[1] if entry is not None else missing  # type: ignore[return-value]
        except FileNotFoundError:
            self._entries.pop(path, None)
            return missing
        self._entries[path] = (stamp, value)
        return value


def read_json_file(path: Path) -> object:
    return json.loads(path.read_bytes().decode("utf-8-sig"))


def read_text_file(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig", errors="replace")


def read_tail(path: Path, max_bytes: int) -> str:
    """The last `max_bytes` of a text log, starting at a whole line."""
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        start = max(0, size - max_bytes)
        handle.seek(start)
        data = handle.read()
    if start:
        data = data[data.find(b"\n") + 1:]
    return data.decode("utf-8", errors="replace")


def local_epoch(text: str, pattern: str) -> float | None:
    """A local wall-clock time written by the tooling, as epoch seconds."""
    try:
        return datetime.strptime(text, pattern).astimezone().timestamp()
    except ValueError:
        return None


@dataclass
class LogFollow:
    """How far a session log has been read and what its own turns showed."""

    offset: int
    context_tokens: int
    largest_context: int
    compactions: int
    last_turn_at: float | None


class SessionLogTracker:
    """Follows Claude-CLI stream-json session logs incrementally: the context of the session's latest
    own turn (what the runner's handoff check compares) and its largest. Only lines that start as
    an assistant event are parsed; tool results, often megabytes, are skipped by their prefix."""

    def __init__(self) -> None:
        self._follows: dict[Path, LogFollow] = {}

    def follow(self, path: Path) -> LogFollow | None:
        stamp = file_stamp(path)
        if stamp is None:
            self._follows.pop(path, None)
            return None
        size = stamp[1]
        state = self._follows.get(path)
        if state is None or size < state.offset:
            state = LogFollow(max(0, size - SESSION_LOG_FIRST_SCAN_BYTES), 0, 0, 0, None)
            self._follows[path] = state
            first_scan = state.offset > 0
        else:
            first_scan = False
        if size == state.offset:
            return state
        with path.open("rb") as handle:
            handle.seek(state.offset)
            chunk = handle.read(size - state.offset)
        if first_scan:
            chunk = chunk[chunk.find(b"\n") + 1:]
            state.offset = size - len(chunk)
        complete = chunk[:chunk.rfind(b"\n") + 1]
        state.offset += len(complete)
        for line in complete.split(b"\n"):
            if line.startswith(b'{"type":"system","subtype":"compact_boundary"'):
                state.compactions += 1
                continue
            if not line.startswith(ASSISTANT_LINE_PREFIX):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("parent_tool_use_id"):
                continue  # a subagent's turn, not the session's own context
            tokens = turn_context_tokens(event)
            if tokens:
                state.context_tokens = tokens
                state.largest_context = max(state.largest_context, tokens)
                state.last_turn_at = time.time()
        return state

    def forget_except(self, keep: set[Path]) -> None:
        for path in list(self._follows):
            if path not in keep:
                del self._follows[path]


def session_kind(stem: str) -> str:
    """The role a session log's stem names (`attempt-3-review`, `evaluation-1`, ...), in the
    dashboard's words."""
    role = ATTEMPT_PREFIX.sub("", stem)
    if role.startswith("review"):
        return "reviewer"
    if role.startswith("evaluation"):
        return "evaluator"
    if role.startswith("repair"):
        return "repair"
    if role.startswith("plan"):
        return "planner"
    if role.startswith("assemble"):
        return "assembler"
    if role.startswith("handoff"):
        return "builder (handoff)"
    return "builder"


def session_log_parts(name: str) -> tuple[str, str] | None:
    """(stem, route as written in the file name) of a session log, None for any other file. The
    session's companions (`.log.prompt.md`, `.log.inbox.log`) are not session logs."""
    match = SESSION_LOG.match(name)
    if match is None or ".log." in name or match.group("stem") in ("worker-process", "integrate", "orchestrator-verify"):
        return None
    return match.group("stem"), match.group("route")


def milestone_key(name: str) -> tuple:
    digits = re.sub(r"\D", "", name)
    return (int(digits) if digits else 10**6, name)


@dataclass
class RunDirScan:
    """One pass over a WP's run directory: the files, newest first, and the newest change."""

    entries: list[tuple[str, float, int]]  # (name, mtime, size), newest first
    latest_change: float | None


def scan_run_dir(run_dir: Path) -> RunDirScan:
    entries: list[tuple[str, float, int]] = []
    latest: float | None = None
    try:
        with os.scandir(run_dir) as listing:
            for entry in listing:
                try:
                    status = entry.stat()
                except FileNotFoundError:
                    continue
                latest = max(latest or 0.0, status.st_mtime)
                if entry.is_file():
                    entries.append((entry.name, status.st_mtime, status.st_size))
    except FileNotFoundError:
        return RunDirScan([], None)
    entries.sort(key=lambda item: item[1], reverse=True)
    return RunDirScan(entries, latest)


class DashboardData:
    """Assembles snapshots, WP details and the event feed for one project. One instance lives in the
    dashboard server; it reloads the project's configuration when config.json changes."""

    def __init__(self, project_start: Path, machine: MachineConfig | None = None) -> None:
        self.project_start = project_start
        self.machine = machine
        self.files = StampedCache()
        self.session_logs = SessionLogTracker()
        self._context: Context | None = None
        self._config_stamp: Stamp | None = None
        self._main: list[wpfile.WorkPackage] = []
        self._main_stamp: tuple | None = None
        self._main_read_at = 0.0
        self._merges: list[dict] = []
        self._merges_stamp: tuple | None = None
        self._merges_read_at = 0.0
        self._forecasts: dict[str, dict | None] = {}
        self._forecast_key: tuple | None = None
        self._forecast_read_at = 0.0
        self._route_harness: dict[str, str] = {}
        self._runs = Path()
        self._worktrees_root = Path()
        self._route_stamp: Stamp | None = None
        self._journal_reader: JournalReader | None = None
        self.events: deque[dict] = deque(maxlen=EVENT_HISTORY)

    # ----------------------------------------------------------------------------------- context

    def refresh_context(self) -> Context:
        """Loads the project, again whenever its config.json changed; called once per pass."""
        if self._context is not None:
            config_path = self._context.project.root / CONFIG_RELATIVE_PATH
            if file_stamp(config_path) == self._config_stamp:
                return self._context
        context = Context.load(self.project_start, self.machine)
        self._context = context
        # Resolved once per load: the project's properties create and resolve paths on every call.
        self._runs = context.project.runs_dir
        self._worktrees_root = context.project.worktrees_root
        self._config_stamp = file_stamp(context.project.root / CONFIG_RELATIVE_PATH)
        self._main_stamp = None
        self._merges_stamp = None
        self._route_harness = {}
        if self._journal_reader is None or self._journal_reader.path != context.journal.path:
            self._journal_reader = JournalReader(context.journal)
            self.events.clear()
        return context

    @property
    def context(self) -> Context:
        if self._context is None:
            return self.refresh_context()
        return self._context

    @property
    def runs(self) -> Path:
        self.context  # noqa: B018  (loads the project on first use)
        return self._runs

    def worktree(self, wp_id: str) -> Path:
        """The WP's worktree path, as `Project.worktree` names it."""
        self.context  # noqa: B018
        return self.context.project.worktree(wp_id)

    def main_ref_stamp(self) -> tuple:
        """Changes whenever the main branch moves: the stamps of its loose ref and of packed-refs."""
        project = self.context.project
        git_dir = project.root / ".git"
        if git_dir.is_file():
            git_dir = Path(read_text_file(git_dir).split("gitdir:", 1)[1].strip())
        return (file_stamp(git_dir / "refs" / "heads" / project.main_branch), file_stamp(git_dir / "packed-refs"))

    def main_listing(self) -> list[wpfile.WorkPackage]:
        """The WP files committed on main, listed again only when main moved."""
        stamp = self.main_ref_stamp()
        if stamp != self._main_stamp or time.monotonic() - self._main_read_at > MAIN_RECHECK_SECONDS:
            self._main = self.context.main_wps()
            self._main_stamp = stamp
            self._main_read_at = time.monotonic()
        return self._main

    def recent_merges(self, titles: dict[str, str]) -> list[dict]:
        stamp = self._main_stamp
        if stamp != self._merges_stamp or time.monotonic() - self._merges_read_at > MERGES_RECHECK_SECONDS:
            entries = merged_wps(self.context.project, None, RECENT_MERGES)
            self._merges = [{"id": entry.wp_id, "merged_at": entry.merged_at.timestamp(), "commit": entry.commit[:10]} for entry in entries]
            self._merges_stamp = stamp
            self._merges_read_at = time.monotonic()
        return [{**merge, "title": titles.get(merge["id"], "")} for merge in self._merges]

    # ----------------------------------------------------------------------------------- routes

    def route_harnesses(self) -> dict[str, str]:
        """Route ids as session log names spell them -> the route's harness."""
        router = self.context.router
        stamp = file_stamp(router.catalog_path)
        if self._route_harness and stamp == self._route_stamp:
            return self._route_harness
        catalog = self.files.get(router.catalog_path, read_json_file, {"routes": []})
        routes = static_routes(router.policy)
        for entry in catalog.get("routes", []):
            try:
                routes.append(Route(**entry))
            except TypeError:
                continue  # a catalog entry from another version of the tooling
        self._route_harness = {route_log_path(Path("_"), {"id": route.id}).name[2:-4]: route.harness for route in routes}
        self._route_stamp = stamp
        return self._route_harness

    # ------------------------------------------------------------------------------------ events

    def poll_events(self) -> list[dict]:
        """Events appended since the last poll (the first poll returns the whole journal's tail)."""
        self.refresh_context()
        new = self._journal_reader.read_new()
        self.events.extend(new)
        return new

    def recent_events(self, limit: int = 200) -> list[dict]:
        return list(self.events)[-limit:]

    # ------------------------------------------------------------------------------------- lanes

    def supervisor_lines(self, lane: str) -> str:
        path = self.runs / f"supervisor-{lane}.log"
        return self.files.get(path, lambda log: read_tail(log, SUPERVISOR_LOG_TAIL_BYTES), "")

    def lanes(self, open_by_lane: dict[str, list[wpfile.WorkPackage]]) -> tuple[list[dict], dict[str, str]]:
        """Every lane with its supervisor; and WP id -> the lane whose live supervisor runs it now."""
        context = self.context
        runs = self.runs
        holds = self.files.get(runs / "lane-holds.json", lambda path: read_holds(context), {})
        pools = set(pool_lanes(context))
        names = {path.stem.removeprefix("supervisor-") for path in runs.glob("supervisor-*.state")}
        names |= set(open_by_lane) | set(context.project.config["lanes"])
        lanes, running_here = [], {}
        for lane in sorted(names):
            state = self.files.get(runs / f"supervisor-{lane}.state", lambda path: read_text_file(path).strip(), None)
            try:
                pid = supervisor_pid(context, lane)
            except ValueError:
                pid = None  # the pid file is being written
            text = self.supervisor_lines(lane)
            lines = text.splitlines()
            current = running_wp_in_log(text) if pid else None
            if current:
                running_here[current] = lane
            last_started = next((match.group(1) for line in reversed(lines) if (match := RUNNING_LINE.search(line))), None)
            last_line, last_line_at = "", None
            if lines and (match := LOG_LINE_TIME.match(lines[-1])):
                last_line, last_line_at = match.group(2), local_epoch(match.group(1), "%Y-%m-%d %H:%M:%S")
            sleep = SLEEP_LINE.match(lines[-1]) if lines and pid else None
            display = state or "never started"
            if state in LIVE_STATES and pid is None:
                display = "died"
            wps = sorted(wp.id for wp in open_by_lane.get(lane, []))
            lanes.append({
                "name": lane,
                "state": display,
                "recorded_state": state,
                "live": pid is not None,
                "pid": pid,
                "pool": lane in pools,
                "hold": holds.get(lane),
                "current_wp": current,
                "last_wp": last_started,
                "sleep_until": local_epoch(sleep.group(2), "%Y-%m-%d %H:%M") if sleep else None,
                "sleep_reason": sleep.group(3) if sleep else None,
                "last_line": last_line,
                "last_line_at": last_line_at,
                "open_wps": wps,
            })
        return lanes, running_here

    # --------------------------------------------------------------------------------------- WPs

    def wp_sections(self, wp: wpfile.WorkPackage) -> dict:
        """The parts of a WP file the table needs, parsed once per change of the file."""
        def parse(path: Path) -> dict:
            text = read_text_file(path)
            steps = [CHECKBOX.match(line) for line in wpfile.section_lines(text, "Steps")]
            steps = [match for match in steps if match]
            progress = [line[2:] for line in wpfile.section_lines(text, "Progress log") if line.startswith("- ")]
            return {"steps_done": sum(1 for match in steps if match.group(1) in "xX"), "steps_total": len(steps),
                    "latest_progress": progress[-1] if progress else "", "progress_entries": len(progress)}
        return self.files.get(wp.path, parse, {"steps_done": 0, "steps_total": 0, "latest_progress": "", "progress_entries": 0})

    def ledger(self, run_dir: Path) -> dict:
        state = self.files.get(run_dir / LEDGER_FILE, read_json_file, None)
        return RoundLedger.migrated(state) if isinstance(state, dict) else RoundLedger.fresh()

    def findings_ledger(self, run_dir: Path) -> dict:
        ledger = self.files.get(run_dir / findings.FINDINGS_FILE, read_json_file, None)
        return ledger if isinstance(ledger, dict) else {"reviews": 0, "findings": []}

    def live(self, run_dir: Path) -> dict | None:
        """The streaming session running for the WP, as `inbox.live_session` defines it; the file
        is re-read only when it changes (the runner deletes it when the session ends)."""
        live = self.files.get(inbox_of(run_dir) / LIVE_FILE, read_json_file, None)
        if not isinstance(live, dict) or not isinstance(live.get("pid"), int) or not isinstance(live.get("log"), str):
            return None
        return live if pid_alive(live["pid"]) else None

    def session_view(self, run_dir: Path, scan: RunDirScan, live: dict | None, active: bool) -> dict | None:
        """What session the WP has now (or had last): role, route, its log, its context."""
        config = self.context.project.config
        if live is not None:
            log_path = Path(live["log"])
        else:
            newest = next((name for name, _, _ in scan.entries if session_log_parts(name)), None)
            if newest is None:
                return None
            log_path = run_dir / newest
        log_name = log_path.name
        stem, route = session_log_parts(log_name) or ("", "")
        modified = next((mtime for name, mtime, _ in scan.entries if name == log_name), None)
        newest_log = next(((name, mtime) for name, mtime, _ in scan.entries if name.endswith(".log")), None)
        phase = session_kind(stem)
        if active and live is None and newest_log and newest_log[0] != log_name and "verify" in newest_log[0]:
            phase = "verify"
        harness = self.route_harnesses().get(route)
        context_tokens = largest = compactions = None
        if (live is not None or active) and harness in CLAUDE_CLI_HARNESSES:
            follow = self.session_logs.follow(log_path)
            if follow is not None and follow.largest_context:
                context_tokens, largest, compactions = follow.context_tokens, follow.largest_context, follow.compactions
        native_task = None
        if self.context.machine.execution_mode == "t3":
            receipt = self.files.get(log_path.with_name(log_path.name + ".t3-invocation.json"), read_json_file, {})
            if receipt.get("request_id"):
                request = self.files.get(self.runs / "t3" / "requests" / f"{receipt['request_id']}.json", read_json_file, {})
                native_task = {"state": request.get("state"), "link": request.get("link"), "task_id": request.get("task_id")}
        return {
            "role": phase,
            "route": route,
            "family": route_family(route),
            "harness": harness,
            "log": log_path.relative_to(self.runs).as_posix() if log_path.is_relative_to(self.runs) else None,
            "live": live is not None,
            "started": float(live["started"]) if live is not None else None,
            "log_modified": modified,
            "context_tokens": context_tokens,
            "largest_context": largest,
            "compactions": compactions,
            "handoff_tokens": handoff_limit(config, harness) if harness in CLAUDE_CLI_HARNESSES else None,
            "native_task": native_task,
        }

    def held_wps(self) -> dict[str, str]:
        """WP id -> the journal kind that handed it to the orchestrator, for WPs whose latest
        hand-over or move in the recent journal is a hand-over."""
        latest: dict[str, str] = {}
        for event in self.events:
            if event.get("wp") and event.get("kind") in HELD_KINDS | MOVING_KINDS:
                latest[event["wp"]] = event["kind"]
        return {wp_id: kind for wp_id, kind in latest.items() if kind in HELD_KINDS}

    def pending_approvals(self) -> list[dict]:
        """Every approval request still waiting for a decision (approvals.py), oldest first."""
        pending = []
        for path in self.runs.glob(f"*/{APPROVALS_DIRECTORY}/*.json"):
            approval = self.files.get(path, read_json_file, None)
            if isinstance(approval, dict) and approval.get("state") == "pending":
                pending.append({key: str(approval.get(key, "")) for key in ("id", "wp", "command", "why", "requested", "route", "role")})
        return sorted(pending, key=lambda approval: (approval["requested"], approval["id"]))

    def wp_row(self, wp: wpfile.WorkPackage, merged: set[str], running_here: dict[str, str], held: dict[str, str]) -> dict:
        run_dir = self.runs / wp.id
        scan = scan_run_dir(run_dir)
        ledger = self.ledger(run_dir)
        open_findings = [finding for finding in self.findings_ledger(run_dir)["findings"] if findings.is_open(finding)]
        live = self.live(run_dir)
        running_lane = running_here.get(wp.id)
        active = live is not None or running_lane is not None
        sections = self.wp_sections(wp)
        wp_file_stamp = file_stamp(wp.path)
        last_activity = max(filter(None, (scan.latest_change, wp_file_stamp[0] / 1e9 if wp_file_stamp else None)), default=None)
        route_wait = self.files.get(run_dir / ROUTE_WAIT_FILE, lambda path: datetime.fromisoformat(read_text_file(path).strip()).timestamp(), None)
        last_builder = ledger.get("last_builder") or {}
        return {
            "id": wp.id,
            "title": wp.title,
            "milestone": wp.milestone,
            "lane": wp.lane,
            "status": wp.status,
            "builder_spec": ledger.get("escalated_to") or wp.builder,
            "steps_done": sections["steps_done"],
            "steps_total": sections["steps_total"],
            "latest_progress": sections["latest_progress"],
            "rounds": int(ledger["rounds"]),
            "failures": int(ledger["failures"]),
            "evaluations": int(ledger["evaluations"]),
            "escalated_to": ledger.get("escalated_to"),
            "handoffs": int(ledger.get("handoffs") or 0),
            "round_open": bool(ledger.get("open")),
            "last_failure": (ledger["history"][-1]["kind"] if ledger.get("history") else None),
            "active": active,
            "running_lane": running_lane,
            "session": self.session_view(run_dir, scan, live, active) if scan.entries else None,
            "last_context_tokens": last_builder.get("context_tokens"),
            "open_findings": [{"id": finding["id"], "answered": findings.is_answered(finding), "location": finding.get("location", "")}
                              for finding in open_findings],
            "last_activity": last_activity,
            "unmerged_deps": sorted(set(wp.depends_on) - merged),
            "route_wait_until": route_wait if isinstance(route_wait, float) else None,
            "worktree": self.worktree(wp.id).exists(),
            "held_by": held.get(wp.id),
        }

    # ------------------------------------------------------------------------------------- usage

    def usage(self) -> dict:
        router = self.context.router
        cache = self.files.get(router.machine.usage_cache, read_json_file, None)
        windows = router.usage_windows(refresh=False) if cache is not None else {family: None for family in SUBSCRIPTION_FAMILIES}
        policy = router.policy
        now = now_utc()
        forecasts = self.forecasts()
        families = []
        cached_families = {entry.get("family"): entry for entry in (cache or {}).get("families", [])}
        for family in SUBSCRIPTION_FAMILIES:
            ceilings = policy["ceilings"][family]
            pace = policy.get("weekly_pace", {}).get(family)
            rows = []
            for window in windows.get(family) or []:
                if window["window_seconds"] <= 18000:
                    ceiling, paced = float(ceilings["five_hour"]), False
                elif pace:
                    ceiling, paced = paced_weekly_ceiling(window, float(ceilings["weekly"]), pace, now)[0], True
                else:
                    ceiling, paced = float(ceilings["weekly"]), False
                rows.append({"label": window["label"], "used_percent": float(window["used_percent"]), "resets_at": float(window["resets_at"]),
                             "window_seconds": int(window["window_seconds"]), "ceiling": round(ceiling, 1), "paced": paced})
            source = cached_families.get(family, {})
            families.append({"family": family, "windows": rows, "error": source.get("error"), "notes": source.get("notes") or [],
                             "plan": source.get("plan"), "forecast": forecasts.get(family)})
        cooldowns = []
        for key, entry in self.files.get(router.health_path, read_json_file, {"cooldowns": {}}).get("cooldowns", {}).items():
            if from_timestamp(float(entry["until"])) > now:
                cooldowns.append({"key": key, "until": float(entry["until"]), "reason": str(entry.get("reason", ""))[:300]})
        return {"fetched_at": float(cache["fetched_at"]) if cache else None, "families": families,
                "lead_minutes": float(policy["burn_forecast"]["lead_minutes"]), "cooldowns": sorted(cooldowns, key=lambda entry: entry["until"])}

    def forecasts(self) -> dict[str, dict | None]:
        router = self.context.router
        key = file_stamp(router.machine.usage_history)
        if key == self._forecast_key and time.monotonic() - self._forecast_read_at < FORECAST_RECHECK_SECONDS:
            return self._forecasts
        result: dict[str, dict | None] = {}
        for family in SUBSCRIPTION_FAMILIES:
            try:
                forecast = router.cap_forecast(family)
            except (ValueError, KeyError):
                result[family] = self._forecasts.get(family)
                continue
            result[family] = None if forecast is None else {"label": forecast.label, "used_percent": forecast.used_percent,
                                                            "percent_per_minute": forecast.percent_per_minute,
                                                            "minutes_to_cap": forecast.minutes_to_cap, "text": forecast.text}
        self._forecasts, self._forecast_key, self._forecast_read_at = result, key, time.monotonic()
        return result

    # ---------------------------------------------------------------------------------- snapshot

    def native_backend(self) -> dict | None:
        if self.context.machine.execution_mode != "t3":
            return None
        directory = self.runs / "t3"
        coordinator = self.files.get(directory / "coordinator.json", read_json_file, {})
        requests = [self.files.get(path, read_json_file, {}) for path in (directory / "requests").glob("*.json")]
        active = [request for request in requests if request.get("state") not in ("done", "failed", "cancelled")]
        return {"coordinator_thread_id": coordinator.get("thread_id"), "pending": len(active),
                "stalled": sum(request["state"] == "stalled" for request in active),
                "tasks": [{"id": request["id"], "kind": request["kind"], "state": request["state"],
                           "wp": request["payload"].get("wp"), "role": request["payload"].get("role"),
                           "link": request.get("link"), "detail": request.get("detail"),
                           "native_approval": (request.get("native_response") or {}).get("pendingApproval")}
                          for request in active[-50:]]}

    def snapshot(self) -> dict:
        """Everything the board shows, as JSON-ready data. Times are epoch seconds; the client
        turns them into ages, so an unchanged board yields an identical snapshot."""
        context = self.refresh_context()
        context.parse_errors.clear()
        main = self.main_listing()
        merged = context.merged_done_ids(main)
        effective = context.effective_wps(main)
        open_wps = [wp for wp in effective if wp.status in LISTED_STATUSES]
        open_by_lane: dict[str, list[wpfile.WorkPackage]] = {}
        for wp in open_wps:
            if wp.lane:
                open_by_lane.setdefault(wp.lane, []).append(wp)
        lanes, running_here = self.lanes(open_by_lane)
        held = self.held_wps()
        rows = [self.wp_row(wp, merged, running_here, held) for wp in open_wps]
        self.session_logs.forget_except({self.runs / row["session"]["log"] for row in rows if row["session"] and row["session"]["log"]})
        milestones: dict[str, dict[str, int]] = {}
        for wp in effective:
            counts = milestones.setdefault(wp.milestone, {status: 0 for status in wpfile.STATUSES})
            counts[wp.status] += 1
        titles = {wp.id: wp.title for wp in main}
        return {
            "project": {"name": context.project.name, "root": str(context.project.root)},
            "lanes": lanes,
            "wps": rows,
            "milestones": [{"name": name, "counts": counts, "total": sum(counts.values())}
                           for name, counts in sorted(milestones.items(), key=lambda item: milestone_key(item[0]))],
            "merged": self.recent_merges(titles),
            "usage": self.usage(),
            "approvals": self.pending_approvals(),
            "parse_errors": dict(context.parse_errors),
            "native_backend": self.native_backend(),
        }

    # ------------------------------------------------------------------------------------ detail

    def find_wp(self, wp_id: str) -> wpfile.WorkPackage:
        context = self.refresh_context()
        for wp in context.effective_wps(self.main_listing()):
            if wp.id == wp_id:
                return wp
        raise DashboardDataError(f"no WP {wp_id} on {context.project.main_branch}")

    def wp_detail(self, wp_id: str) -> dict:
        """The WP's full record: its file's sections, the round ledger, the findings ledger, the
        logs of its run directory."""
        wp = self.find_wp(wp_id)
        text = read_text_file(wp.path) if wp.path.exists() else ""
        run_dir = self.runs / wp.id
        ledger = self.ledger(run_dir)
        progress = []
        for line in wpfile.section_lines(text, "Progress log"):
            if not line.startswith("- "):
                if progress and line.strip():
                    progress[-1]["text"] += "\n" + line.strip()
                continue
            match = PROGRESS_ENTRY.match(line)
            progress.append({"at": match.group(1), "actor": match.group(2) or "", "text": match.group(3)} if match
                            else {"at": "", "actor": "", "text": line[2:]})
        steps = [{"done": match.group(1) in "xX", "text": match.group(2)}
                 for match in (CHECKBOX.match(line) for line in wpfile.section_lines(text, "Steps")) if match]
        no_route = self.files.get(run_dir / NO_ROUTE_FILE, read_json_file, None)
        return {
            "id": wp.id,
            "title": wp.title,
            "fields": {key: value for key, value in wp.fields.items() if key not in ("id", "title")},
            "file": str(wp.path),
            "worktree": str(self.worktree(wp.id)) if self.worktree(wp.id).exists() else None,
            "steps": steps,
            "progress": progress,
            "sections": {heading: "\n".join(wpfile.section_lines(text, heading)).strip()
                         for heading in ("Goal", "Review", "Re-evaluation", "Handoff", "Try it")},
            "ledger": {key: ledger.get(key) for key in ("rounds", "failures", "evaluations", "escalated_to", "open", "interruptions",
                                                        "handoffs", "history", "feedback", "guidance", "handoff", "last_builder", "writing")},
            "findings": self.findings_ledger(run_dir),
            "no_route": no_route if isinstance(no_route, dict) else None,
            # Whether a message reaches a running session (else it goes to a placement agent).
            "reads_messages": self.live(run_dir) is not None,
            "logs": self.run_files(run_dir),
        }

    def run_files(self, run_dir: Path) -> list[dict]:
        """The run directory's logs and records, newest first, delegated workers' included."""
        files = []
        directories = [run_dir] + sorted(path for path in (run_dir / "delegate").glob("*") if path.is_dir())
        for directory in directories:
            for name, mtime, size in scan_run_dir(directory).entries:
                if not name.endswith(LINKED_SUFFIXES):
                    continue
                parts = session_log_parts(name)
                relative = (directory / name).relative_to(self.runs).as_posix()
                files.append({"path": relative, "name": (directory / name).relative_to(run_dir).as_posix(), "modified": mtime, "size": size,
                              "session": parts is not None, "role": session_kind(parts[0]) if parts else None,
                              "route": parts[1] if parts else None})
        return sorted(files, key=lambda entry: entry["modified"], reverse=True)

    def run_file(self, relative: str) -> Path:
        """A file inside the runs directory named by a client; nothing outside it."""
        runs = self.runs.resolve()
        candidate = (runs / relative).resolve()
        if not candidate.is_relative_to(runs) or not candidate.is_file() or not candidate.name.endswith(LINKED_SUFFIXES):
            raise DashboardDataError(f"not a run file: {relative}")
        return candidate
