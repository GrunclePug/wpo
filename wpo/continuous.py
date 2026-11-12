"""Durable, opt-in scheduling and bounded coordinator interventions.

This process reads WPO state, never native task status. A coordinator supplies the
judgment and concrete follow-up plan; elapsed time and failure counts are signals.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from .inbox import (
    WRAP_UP_MESSAGE,
    clear_pause,
    live_session,
    pause_requested,
    post_message,
    request_pause,
)
from .platform import FileLock, NamedMutex, spawn_detached
from .t3 import read_json, write_json

BLOCKED_STATES = {"human_wait", "intervention"}
DEFAULTS = {"interval_seconds": 30, "checkpoint_seconds": 3600, "no_progress_seconds": 1800, "failed_rounds": 3}


def settings(context) -> dict:
    """Validate scheduling limits before any work starts."""
    values = {**DEFAULTS, **context.project.config.get("continuous", {})}
    if set(values) != set(DEFAULTS):
        raise ValueError("unknown continuous setting")
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 1:
            raise ValueError(f"continuous.{key} must be a finite positive number")
    if not isinstance(values["failed_rounds"], int):
        raise TypeError("continuous.failed_rounds must be an integer")
    return values


def directory(context) -> Path:
    return context.project.runs_dir / "continuous"


def status(context) -> dict:
    """Read durable authorization, per-WP reasons and last scheduling outcome."""
    return read_json(directory(context) / "state.json", {"version": 1, "enabled": False, "stopped": False,
                                                        "lanes": [], "records": {}, "waiting": {}})


def save(context, state: dict) -> None:
    write_json(directory(context) / "state.json", state)


def lock(context):
    return FileLock(directory(context) / "state.lock")


def native_busy(context) -> set[str]:
    """Outstanding durable requests protect scopes even before a launch receipt arrives."""
    if context.machine.execution_mode != "t3":
        return set()
    from .t3 import Bridge
    busy = set()
    for request in Bridge(context).requests():
        wp_id = request["payload"].get("wp")
        if request["kind"] not in ("workspace", "session") or not wp_id:
            continue
        if request["state"] not in ("done", "failed", "cancelled"):
            busy.add(wp_id)
        elif request["state"] == "cancelled" and request.get("task_id"):
            # Cancellation acknowledgement is not terminal completion of a late native task.
            terminal = request.get("late_result") or request.get("result") or {}
            if not (terminal.get("task_id") == request["task_id"] and terminal.get("outcome")
                    and isinstance(terminal.get("text"), str)):
                busy.add(wp_id)
    return busy


def _selected(state: dict, wp) -> bool:
    return bool(wp.lane) and (not state["lanes"] or wp.lane in state["lanes"])


def block_reason(context, wp_id: str) -> str | None:
    """A scheduling barrier applies even when an existing supervisor is resumed manually."""
    state = status(context)
    if state["stopped"]:
        wp = next((item for item in context.effective_wps() if item.id == wp_id), None)
        if wp is not None and _selected(state, wp):
            return state["stop_reason"]
    record = state["records"].get(wp_id, {})
    return record.get("reason") if record.get("state") in BLOCKED_STATES else None


def _pause(context, wp_id: str, record: dict, reason: str) -> None:
    run_dir = context.project.runs_dir / wp_id
    # Never clear or claim an unrelated operator's pause marker.
    if pause_requested(run_dir) is None:
        request_pause(run_dir, reason)
        record["owned_pause"] = reason
    message = WRAP_UP_MESSAGE + "\nReason: " + reason
    if live_session(run_dir) is not None:
        if not record.get("owned_message"):
            record["owned_message"] = str(post_message(run_dir, message))
    elif context.machine.execution_mode == "t3":
        from .t3 import Bridge
        bridge = Bridge(context)
        for request in bridge.requests():
            if (request["kind"] == "session" and request["payload"].get("wp") == wp_id
                    and request["state"] not in ("done", "failed", "cancelled") and request.get("thread_id")):
                bridge.submit("message", f"continuous:{wp_id}:{record.get('started_at')}:{reason}",
                              {"thread_id": request["thread_id"], "message": message}, request["coordinator_thread_id"])
    record.pop("pause_pending", None)


def enable(context, lanes: list[str]) -> dict:
    """Explicitly authorize continuous selection; existing human/manual barriers remain."""
    settings(context)
    known = {wp.lane for wp in context.effective_wps() if wp.lane}
    if set(lanes) - known:
        raise ValueError("unknown lane: " + ", ".join(sorted(set(lanes) - known)))
    with lock(context):
        state = status(context)
        for wp_id, record in state["records"].items():
            if record.pop("stop_pause", False):
                _clear_owned_pause(context, wp_id, record)
                if record.get("state") not in BLOCKED_STATES:
                    record["history"] = [*record.get("history", []), {"at": time.time(), "decision": "explicit project restart",
                                                                    "started_at": record.get("started_at")}]
                    for key in ("started_at", "progress_at", "failure_base", "pause_pending"):
                        record.pop(key, None)
                    record["state"] = "ready"
        state.update(enabled=True, stopped=False, lanes=sorted(set(lanes)))
        save(context, state)
        return state


def disable(context, reason: str) -> dict:
    """Stop selecting work and safely checkpoint authorized WPs without touching other lanes."""
    with lock(context):
        state = status(context)
        state.update(enabled=False, stopped=True, stop_reason=reason)
        targets = []
        for wp in context.effective_wps():
            if _selected(state, wp) and wp.status != "done":
                record = state["records"].setdefault(wp.id, {})
                record.update(stop_pause=record.get("state") not in BLOCKED_STATES, pause_pending=reason)
                targets.append((wp.id, record))
        save(context, state)
        try:
            for wp_id, record in targets:
                _pause(context, wp_id, record, reason)
        finally:
            save(context, state)
        return state


def wait(context, wp_id: str, reason: str) -> None:
    """Wait for a material user decision on one WP while other work remains eligible."""
    _require_wp(context, wp_id)
    with lock(context):
        state = status(context)
        record = state["records"].setdefault(wp_id, {})
        record.update(state="human_wait", reason=reason, pause_pending=reason)
        save(context, state)
        try:
            _pause(context, wp_id, record, reason)
        finally:
            save(context, state)


def _require_wp(context, wp_id: str):
    wp = next((wp for wp in context.effective_wps() if wp.id == wp_id), None)
    if wp is None or wp.status == "done":
        raise ValueError("expected an existing unfinished work package")
    return wp


def _clear_owned_pause(context, wp_id: str, record: dict) -> None:
    if record.get("owned_pause") and pause_requested(context.project.runs_dir / wp_id) == record["owned_pause"]:
        clear_pause(context.project.runs_dir / wp_id)
    record.pop("owned_pause", None)
    message = record.pop("owned_message", None)
    if message:
        path = Path(message)
        if path.parent.resolve() != (context.project.runs_dir / wp_id / "inbox").resolve():
            raise ValueError("owned message path escapes the WP inbox")
        path.unlink(missing_ok=True)


def continue_work(context, wp_id: str, milestone: str, reason: str) -> None:
    """Rearm one bounded WP with a concrete coordinator decision, never an extant native task."""
    from .lanes import claimed_elsewhere
    _require_wp(context, wp_id)
    with lock(context):
        if wp_id in native_busy(context) or claimed_elsewhere(context, wp_id):
            raise ValueError("work package still has an extant native task or WP claim; reconcile completion first")
        state = status(context)
        if state["stopped"]:
            raise ValueError("project is stopped; explicitly start continuous operation first")
        old = state["records"].get(wp_id, {})
        _clear_owned_pause(context, wp_id, old)
        history = [*old.get("history", []), {"at": time.time(), "state": old.get("state"), "reason": old.get("reason"),
                                           "milestone": old.get("milestone"), "decision": reason}]
        state["records"][wp_id] = {"state": "ready", "milestone": milestone, "history": history}
        save(context, state)


def progress(context, wp_id: str, summary: str) -> None:
    """Record a useful measurable/visible outcome; heartbeats alone are not progress."""
    _require_wp(context, wp_id)
    with lock(context):
        state = status(context)
        record = state["records"].get(wp_id)
        if record is None or "started_at" not in record or record.get("state") in BLOCKED_STATES:
            raise ValueError("no active continuous milestone for this work package")
        record.update(progress_at=time.time(), summary=summary)
        save(context, state)


def prompt(context, wp_id: str) -> str:
    """Attach the coordinator's current small milestone to fresh and resumed builders."""
    state = status(context)
    record = state["records"].get(wp_id, {})
    if not state["enabled"] or not record.get("milestone") or "started_at" not in record:
        return ""
    return ("\nCurrent bounded milestone: " + record["milestone"] +
            "\nCheckpoint at " + datetime.fromtimestamp(record["started_at"] + settings(context)["checkpoint_seconds"], UTC).isoformat() +
            "\nDeliver actual useful outputs, failures and next step. Do not repeat failed tuning indefinitely. "
            "Record a useful checkpoint with `wpo continuous progress " + wp_id + " --summary <outcome>`. "
            "A checkpoint is evidence, not permission to change acceptance gates or extend this milestone.\n")


def _failures(context, wp_id: str) -> int:
    from .runner import LEDGER_FILE
    ledger = read_json(context.project.runs_dir / wp_id / LEDGER_FILE, {})
    return int(ledger.get("nonprogress_total", int(ledger.get("failures", 0)) + int(ledger.get("interruptions", 0))))


def _intervene(context, wp_id: str, record: dict, now: float, limits: dict, state: dict) -> None:
    if "started_at" not in record or record.get("state") == "human_wait":
        return
    reason = None
    if now - record["started_at"] >= limits["checkpoint_seconds"]:
        reason = "bounded milestone checkpoint reached; inspect outcome and select a smaller follow-up"
    elif now - record["progress_at"] >= limits["no_progress_seconds"]:
        reason = "no recorded useful progress; investigate evidence before continuing"
    elif _failures(context, wp_id) - record["failure_base"] >= limits["failed_rounds"]:
        reason = "repeated failed or interrupted rounds; investigate and change the plan"
    if reason and record.get("state") != "intervention":
        record.update(state="intervention", reason=reason, attention_pending=True, pause_pending=reason)
        save(context, state)
        try:
            _pause(context, wp_id, record, reason)
        finally:
            save(context, state)


def _attention(context, wp_id: str, record: dict) -> None:
    if record.get("attention_pending"):
        context.journal.emit("progress-intervention", f"{wp_id} milestone {record['started_at']}: {record['reason']}. "
                             "Report useful outcome/visible failure, choose a concrete bounded follow-up within authorization, "
                             "or ask the user a material product question while independent work continues.", {"wp": wp_id})
        record["attention_pending"] = False


def checkpoint(context, wp_id: str) -> str | None:
    """Enforce failure/time boundaries between rounds, even before the scheduler's next tick."""
    with lock(context):
        state = status(context)
        if state["enabled"] and wp_id in state["records"]:
            record = state["records"][wp_id]
            _intervene(context, wp_id, record, time.time(), settings(context), state)
            save(context, state)
            _attention(context, wp_id, record)
            save(context, state)
    return block_reason(context, wp_id)


def admit(context, wp_id: str) -> bool:
    """Recheck every supervisor transition while its caller holds the admission lock."""
    from .lanes import claimed_elsewhere, read_holds
    from .lint import patterns_overlap
    if checkpoint(context, wp_id):
        return False
    state = status(context)
    if not state["enabled"]:
        return True
    wp = next(item for item in context.effective_wps() if item.id == wp_id)
    if not _selected(state, wp):
        return True
    record = state["records"].get(wp_id, {})
    if record.get("state") != "scheduled" or "started_at" not in record:
        return False
    if wp.lane in read_holds(context) or pause_requested(context.project.runs_dir / wp_id):
        return False
    busy = native_busy(context)
    if wp_id in busy:
        return False
    for other in context.effective_wps():
        if (other.id != wp_id and (other.id in busy or claimed_elsewhere(context, other.id))
                and any(patterns_overlap(left, right) for left in wp.owns for right in other.owns)):
            return False
    return True


def manages_lane(context, lane: str) -> bool:
    """Continuous lane supervisors exit when no individually admitted work remains."""
    state = status(context)
    return state["enabled"] and (not state["lanes"] or lane in state["lanes"])


def tick(context, now: float | None = None) -> dict:
    """Fill capacity and coalesce progress attention from local durable state only."""
    from .lanes import (
        claimed_elsewhere,
        lane_active,
        read_holds,
        resume,
        supervisor_pid,
    )
    now = time.time() if now is None else now
    limits = settings(context)
    with lock(context):
        state = status(context)
        if not state["enabled"]:
            for wp_id, record in state["records"].items():
                if record.get("pause_pending"):
                    try:
                        _pause(context, wp_id, record, record["pause_pending"])
                    finally:
                        save(context, state)
            return state
        wps = context.effective_wps()
        merged = context.merged_done_ids()
        busy = native_busy(context)
        holds = read_holds(context)
        state["waiting"] = {}
        for wp in wps:
            if not _selected(state, wp) or wp.id in merged or wp.status == "done":
                continue
            record = state["records"].get(wp.id, {})
            if record.get("state") in BLOCKED_STATES:
                if record.get("pause_pending"):
                    _pause(context, wp.id, record, record["pause_pending"])
                _attention(context, wp.id, record)
                continue
            if "started_at" in record:
                _intervene(context, wp.id, record, now, limits, state)
                save(context, state)
                _attention(context, wp.id, record)
                continue
            if wp.lane in holds or pause_requested(context.project.runs_dir / wp.id) is not None:
                state["waiting"][wp.id] = holds.get(wp.lane, "operator pause")
                continue
            if wp.status == "blocked" or not set(wp.depends_on) <= merged:
                state["waiting"][wp.id] = "blocked or dependencies unmerged"
                continue
            if wp.id in busy or claimed_elsewhere(context, wp.id):
                state["waiting"][wp.id] = "native request or WP claim already exists"
                record.update(state="scheduled", started_at=now, progress_at=now, failure_base=_failures(context, wp.id))
                state["records"][wp.id] = record
                continue
            if supervisor_pid(context, wp.lane):
                state["waiting"][wp.id] = "supervisor already owns this lane"
                continue
            from .supervisor import Supervisor
            spec = Supervisor(context, wp.lane, True, [wp.id], {}).session_spec(wp)
            selection = context.router.select(spec, context.project.lane(wp.lane).get("families") or None,
                                              free_last_resort=wp.status != "review", gate=wp.status == "review")
            if not selection.candidates:
                state["waiting"][wp.id] = "; ".join(entry["reason"] for entry in selection.skipped) or "no eligible route"
                continue
            maximum = context.project.config["max_active_lanes"]
            active = {item.lane for item in wps if item.lane and lane_active(context, item.lane)}
            active.update(item.lane for item in wps if item.id in busy or claimed_elsewhere(context, item.id))
            if maximum and len(active) >= maximum:
                state["waiting"][wp.id] = "active lane limit reached"
                continue
            from .lint import patterns_overlap
            occupied = [item for item in wps if item.id != wp.id and
                        (item.id in busy or claimed_elsewhere(context, item.id) or item.lane in active)]
            if any(patterns_overlap(left, right) for item in occupied for left in wp.owns for right in item.owns):
                state["waiting"][wp.id] = "owned paths overlap active work"
                continue
            record.update(state="scheduled", started_at=now, progress_at=now, failure_base=_failures(context, wp.id))
            record.setdefault("milestone", f"Deliver the next smallest useful measurable or visible slice of {wp.id}; "
                              "preserve its acceptance contracts. Checkpoint early with actual outcome and next step, "
                              "rather than repeating failed work or expanding into the whole unfinished WP.")
            state["records"][wp.id] = record
            save(context, state)  # never replay an uncertain launch after a crash
            resume(context, False, [wp.id])
        state["checked_at"] = now
        save(context, state)
        return state


def run(context) -> None:
    """One cheap Python scheduler per project; no model or native task status polling."""
    from .context import Context
    mutex = NamedMutex(context.project.mutex_name("continuous", "scheduler"))
    if not mutex.acquire(blocking=False):
        return
    runtime = directory(context) / "runtime.json"
    try:
        while status(context)["enabled"]:
            fresh = Context.load(context.project.root, context.machine)
            tick(fresh)
            write_json(runtime, {"state": "running", "checked_at": time.time()})
            time.sleep(settings(fresh)["interval_seconds"])
        write_json(runtime, {"state": "stopped", "at": time.time()})
    except Exception as error:
        write_json(runtime, {"state": "failed", "at": time.time(), "detail": f"{type(error).__name__}: {error}"})
        context.journal.emit("progress-intervention", f"Continuous scheduler stopped: {type(error).__name__}: {error}. "
                             "Inspect durable state and restart explicitly; do not replay uncertain native launches.")
        raise
    finally:
        mutex.release()


def add_cli(commands) -> None:
    parser = commands.add_parser("continuous", help="opt-in quota-aware scheduling and bounded progress")
    sub = parser.add_subparsers(dest="continuous_command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--lane", action="append", default=[])
    stop = sub.add_parser("stop")
    stop.add_argument("--reason", required=True)
    sub.add_parser("status")
    sub.add_parser("run")
    for name in ("wait", "continue", "progress"):
        command = sub.add_parser(name)
        command.add_argument("wp")
        if name == "progress":
            command.add_argument("--summary", required=True)
        else:
            command.add_argument("--reason", required=True)
        if name == "continue":
            command.add_argument("--milestone", required=True)


def cli_command(context, arguments) -> int:
    """Administer durable scheduling; delegated sessions may only report useful progress."""
    action = arguments.continuous_command
    if arguments.session and action != "progress":
        raise ValueError("continuous administration belongs to the coordinator")
    if action == "progress" and arguments.session and os.environ.get("WPO_WORK_PACKAGE") != arguments.wp:
        raise ValueError("a delegated session may report progress only for its own work package")
    if action == "start":
        enable(context, arguments.lane)
        spawn_detached([*context.machine.wpo_command(), "continuous", "run"], context.project.root,
                       log_path=context.project.runs_dir / "continuous.log")
    elif action == "stop":
        disable(context, arguments.reason)
    elif action == "run":
        run(context)
    elif action == "wait":
        wait(context, arguments.wp, arguments.reason)
    elif action == "continue":
        continue_work(context, arguments.wp, arguments.milestone, arguments.reason)
    elif action == "progress":
        progress(context, arguments.wp, arguments.summary)
    state = status(context)
    runtime = read_json(directory(context) / "runtime.json", {"state": "not_started"})
    if runtime["state"] == "running" and time.time() - runtime["checked_at"] > 2 * settings(context)["interval_seconds"]:
        runtime = {**runtime, "state": "heartbeat_stale"}
    print(json.dumps({"policy": state, "scheduler": runtime}, indent=2))
    return 0
