"""Lane supervisors: start them after a restart or a merge, stop and hold them, show the board.

    wpo lanes resume [LANE|WP ...] [--dry-run]
                                      one detached supervisor per lane with runnable work (or only
                                      the named lanes and the lanes of the named WPs)
    wpo lanes stop <lane>             kill a lane's supervisor and its sessions
    wpo lanes hold <lane> [--reason]  keep `resume` from starting a lane until `release`
    wpo lanes release <lane>
    wpo status                        the board: lanes, supervisors, WPs (and the board file)
"""

from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import wpfile
from .context import Context
from .platform import FileLock, NamedMutex, kill_tree, pid_alive, replace_file, spawn_detached

SUPERVISOR_PROCESS_NAMES = ("python", "python3")
STATUS_ORDER = ("in_progress", "review", "changes_requested", "blocked", "todo", "done")


def holds_path(context: Context) -> Path:
    return context.project.runs_dir / "lane-holds.json"


def read_holds(context: Context) -> dict[str, str]:
    path = holds_path(context)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_holds(context: Context, holds: dict[str, str]) -> None:
    """Replaces the holds file atomically, so a reader never sees it half written."""
    path = holds_path(context)
    staging = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    staging.write_text(json.dumps(holds, indent=2) + "\n", encoding="utf-8")
    replace_file(staging, path)


def update_holds(context: Context, change) -> dict[str, str]:
    """Reads, changes (`change(holds)` edits the dict in place) and writes the lane holds under one
    lock, so concurrent holds and releases (two coordinators, the dashboard and a CLI) lose nothing."""
    with FileLock(context.project.runs_dir / "lane-holds.lock"):
        holds = read_holds(context)
        change(holds)
        write_holds(context, holds)
        return holds


def supervisor_pid(context: Context, lane: str) -> int | None:
    """The live supervisor process of a lane, if any."""
    pid_file = context.project.runs_dir / f"supervisor-{lane}.pid"
    if not pid_file.exists():
        return None
    pid = int(pid_file.read_text(encoding="utf-8").strip() or 0)
    return pid if pid and pid_alive(pid, SUPERVISOR_PROCESS_NAMES) else None


def slots_lock(context: Context) -> FileLock:
    """Held while a lane takes one of the `max_active_lanes` slots, so two lanes never take the last one."""
    return FileLock(context.project.runs_dir / "lane-slots.lock")


def lane_active(context: Context, lane: str) -> bool:
    """A live supervisor that is working: one asleep until a route or a slot frees up takes no slot."""
    if supervisor_pid(context, lane) is None:
        return False
    state_file = context.project.runs_dir / f"supervisor-{lane}.state"
    return not state_file.exists() or state_file.read_text(encoding="utf-8-sig").strip() != "sleeping"


def active_lanes(context: Context, lanes: set[str]) -> set[str]:
    """The non-pool lanes among `lanes` whose supervisors are working."""
    pools = set(pool_lanes(context))
    return {lane for lane in lanes if lane not in pools and lane_active(context, lane)}


def lanes_with_open_work(context: Context) -> dict[str, list[wpfile.WorkPackage]]:
    grouped: dict[str, list[wpfile.WorkPackage]] = defaultdict(list)
    for wp in context.effective_wps():
        if wp.lane and wp.status in wpfile.OPEN_STATUSES:
            grouped[wp.lane].append(wp)
    return dict(sorted(grouped.items()))


def pool_lanes(context: Context) -> list[str]:
    return sorted(name for name, settings in context.project.config["lanes"].items() if settings.get("pool"))


def lane_outcome(context: Context, lane: str, wps: list[wpfile.WorkPackage], merged: set[str], holds: dict[str, str],
                 own_claims: frozenset[str] = frozenset()) -> tuple[str, list[str] | None]:
    """What resume does with a lane: (message, the WP ids to start it with, or None to leave it)."""
    ids = [wp.id for wp in sorted(wps, key=lambda wp: wp.id)]
    if lane in holds:
        return f"held ({holds[lane]})", None
    if supervisor_pid(context, lane):
        return "supervisor still running", None
    if lane in pool_lanes(context):
        return "starting the pool", ids
    runnable = [wp.id for wp in wps if set(wp.depends_on) <= merged]
    if not runnable:
        return f"waiting on dependencies ({', '.join(ids)})", None
    if context.project.lane(lane).get("builds_in_pool"):
        # Its supervisor only reviews; building would start a second session on the pool's model.
        from .supervisor import POOL_STATUSES
        runnable = [wp.id for wp in wps if wp.id in runnable and wp.status not in POOL_STATUSES]
        if not runnable:
            return "the pool builds its work; nothing to review", None
    if all(wp_id not in own_claims and claimed_elsewhere(context, wp_id) for wp_id in runnable):
        # A pool lane runs them; the lane would only find nothing to claim and finish at once.
        return f"its runnable work is claimed by another supervisor ({', '.join(runnable)})", None
    return f"starting with {', '.join(ids)}", ids


def claimed_elsewhere(context: Context, wp_id: str) -> bool:
    """Whether a supervisor holds this WP's claim (the mutex `Supervisor.claim` takes)."""
    mutex = NamedMutex(context.project.mutex_name("wp", wp_id))
    if not mutex.acquire(blocking=False):
        return True
    mutex.release()
    return False


def start_if_idle(context: Context, lane: str, own_claims: frozenset[str] = frozenset()) -> str:
    """Starts one lane's supervisor as resume would; returns what happened. `own_claims` are WPs the
    caller holds and is handing over, so they do not count as claimed by another supervisor."""
    wps = lanes_with_open_work(context).get(lane, [])
    message, ids = lane_outcome(context, lane, wps, context.merged_done_ids(), read_holds(context), own_claims)
    if ids is not None:
        start(context, lane, ids)
    return message


def resume_targets(context: Context, targets: list[str]) -> set[str]:
    """The lanes named by `targets`: lane names as given, WP ids as their WP's lane."""
    by_id = {wp.id: wp for wp in context.effective_wps()}
    lanes: set[str] = set()
    for target in targets:
        if target in by_id:
            if not by_id[target].lane:
                raise SystemExit(f"{target} has no lane; milestone runs start with `wpo supervise`")
            lanes.add(by_id[target].lane)
        elif target in context.project.config["lanes"] or any(wp.lane == target for wp in by_id.values()):
            lanes.add(target)
        else:
            raise SystemExit(f"{target} is neither a WP nor a lane")
    return lanes


MERGE_NEAR_STATUSES = ("review", "changes_requested", "in_progress")


def merge_distance(wps: list[wpfile.WorkPackage]) -> int:
    """0 for a lane with a WP in review, 1 with one being reworked or built, 2 otherwise."""
    statuses = {wp.status for wp in wps}
    return next((rank for rank, status in enumerate(MERGE_NEAR_STATUSES) if status in statuses), len(MERGE_NEAR_STATUSES) - 1)


def resume(context: Context, dry_run: bool, targets: list[str] | None = None) -> None:
    """Starts a supervisor for every selected lane with runnable work, nearest to merging first,
    while fewer than `max_active_lanes` non-pool lanes are working: more lanes than the machine and
    the quota serve only sleep, rebuild and wait on each other (efficiency review, 2026-10-26). A
    lane asleep until a route frees up takes no slot; when it wakes it waits for one."""
    merged = context.merged_done_ids()
    holds = read_holds(context)
    lanes = lanes_with_open_work(context)
    pools = set(pool_lanes(context))
    selected = resume_targets(context, targets) if targets else set(lanes) | pools
    limit = int(context.project.config["max_active_lanes"])
    with slots_lock(context):
        live = active_lanes(context, set(lanes) | selected | known_lanes(context))
        for lane in sorted(selected, key=lambda name: (name not in pools, merge_distance(lanes.get(name, [])), name)):
            message, ids = lane_outcome(context, lane, lanes.get(lane, []), merged, holds)
            if ids is not None and lane not in pools and limit and len(live) >= limit:
                message = f"not started: {len(live)} lanes are working, the most `max_active_lanes` allows ({message})"
                if not dry_run:
                    context.journal.emit("lane-deferred", f"lane {lane} not started: {len(live)} of {limit} lanes are working", {"supervisor": lane})
                ids = None
            print(f"{lane}: {message}")
            if ids is None:
                continue
            if not dry_run:
                pid = start(context, lane, ids)
                # Its slot is taken now, before the supervisor writes its own state.
                (context.project.runs_dir / f"supervisor-{lane}.pid").write_text(str(pid), encoding="utf-8")
                (context.project.runs_dir / f"supervisor-{lane}.state").write_text("running", encoding="utf-8")
            if lane not in pools:
                live.add(lane)


def known_lanes(context: Context) -> set[str]:
    """Every lane with a supervisor state file (a sleeping one may have no open WP left listed)."""
    return {path.stem.removeprefix("supervisor-") for path in context.project.runs_dir.glob("supervisor-*.state")}


def start(context: Context, lane: str, wp_ids: list[str]) -> int:
    if context.machine.execution_mode == "t3":
        from .t3 import Bridge
        Bridge(context).ready()
    runs = context.project.runs_dir
    listed = ["--wps", ",".join(wp_ids)] if wp_ids else []
    pid = spawn_detached([*context.machine.wpo_command(), "supervise", "--lane", lane, *listed], context.project.root,
                         log_path=runs / f"supervisor-{lane}.stdout.log")
    context.journal.emit("lane-started", f"lane {lane} started with {', '.join(wp_ids) or 'any WP it may take'} (pid {pid})", {"supervisor": lane})
    return pid


def stop(context: Context, lane: str) -> None:
    """Stops a lane: its native sessions are asked to cancel, its supervisor is killed, and its
    delegated workers are fenced, also when the supervisor or the coordinator is already gone. It
    does not wait for native sessions: the WP's next run settles them, and reports any violation."""
    from .runner import stop_delegated_workers
    from .t3 import Bridge, T3Error
    wps = [wp for wp in context.effective_wps() if wp.lane == lane]
    bridge = Bridge(context) if context.machine.execution_mode == "t3" else None
    # First, so a lead's cancel is queued before its workers are fenced. Each intent is on its
    # request before its wake, so a dead coordinator stops nothing that follows.
    for wp in wps if bridge else ():
        try:
            bridge.cancel_wp(wp.id, f"lane {lane} stopped by the operator", "operator")
        except T3Error as error:
            print(f"{lane}: {error}")
    pid = supervisor_pid(context, lane)
    if pid is None:
        print(f"{lane}: no live supervisor")
    else:
        other_supervisors = [other_pid for state_file in context.project.runs_dir.glob("supervisor-*.state")
                             if (other := state_file.stem.removeprefix("supervisor-")) != lane and (other_pid := supervisor_pid(context, other))]
        kill_tree(pid, spared=other_supervisors)
        (context.project.runs_dir / f"supervisor-{lane}.state").write_text("stopped", encoding="utf-8")
        context.journal.emit("lane-stopped", f"lane {lane} stopped by the orchestrator (pid {pid})", {"supervisor": lane})
        print(f"{lane}: stopped supervisor {pid} and its sessions; WPs keep their status and resume where they were")
    # Delegated workers run detached, outside the supervisor's process tree. A WP merged into main
    # has no checkout left to fence.
    merged = context.merged_done_ids()
    for wp in wps:
        if wp.id in merged:
            continue
        try:
            stop_delegated_workers(context, wp, wait=False)
        except T3Error as error:
            print(f"{lane}: {error}")
        if bridge and (outstanding := bridge.outstanding_sessions(wp.id)):
            print(f"{wp.id}: {len(outstanding)} native session(s) still to finish ({', '.join(request['id'] for request in outstanding)}); "
                  "its next run settles them and reports any scope violation")


def hold(context: Context, lane: str, reason: str) -> None:
    update_holds(context, lambda holds: holds.__setitem__(lane, reason))
    print(f"{lane}: held ({reason}); `wpo lanes release {lane}` lets resume start it again")


def release(context: Context, lane: str) -> None:
    update_holds(context, lambda holds: holds.pop(lane, None))
    print(f"{lane}: released")


def board(context: Context) -> list[str]:
    """The status board as markdown lines."""
    wps = context.effective_wps()
    merged = context.merged_done_ids()
    lines = ["# Work package status", "", f"Generated by `wpo status` on {datetime.now():%Y-%m-%d %H:%M}. Do not edit by hand.", ""]
    holds = read_holds(context)
    runs = context.project.runs_dir
    lane_rows = []
    for state_file in sorted(runs.glob("supervisor-*.state")):
        lane = state_file.stem.removeprefix("supervisor-")
        state = state_file.read_text(encoding="utf-8-sig").strip()
        live = "live" if supervisor_pid(context, lane) else "not running"
        lane_rows.append(f"| {lane} | {state} | {live} | {holds.get(lane, '')} |")
    if lane_rows:
        lines += ["| Supervisor | State | Process | Hold |", "|---|---|---|---|", *lane_rows, ""]
    milestones = sorted({wp.milestone for wp in wps})
    lines += ["| Milestone | " + " | ".join(STATUS_ORDER) + " |", "|---|" + "|".join("---:" for _ in STATUS_ORDER) + "|"]
    for milestone in milestones:
        counts = [sum(1 for wp in wps if wp.milestone == milestone and wp.status == status) for status in STATUS_ORDER]
        lines.append(f"| {milestone} | " + " | ".join(map(str, counts)) + " |")
    ready = [wp.id for wp in wps if wp.status in ("todo", "changes_requested") and set(wp.depends_on) <= merged]
    lines += ["", f"**Ready to start:** {', '.join(ready) or 'none'}", "",
              "| WP | Title | Milestone | Lane | Status | Depends on | Builder | Latest progress |", "|---|---|---|---|---|---|---|---|"]
    for wp in wps:
        latest = wp.latest_progress().replace("|", "/")
        lines.append(f"| {wp.id} | {wp.title} | {wp.milestone} | {wp.lane} | {wp.status} | {', '.join(wp.depends_on)} | {wp.builder} | {latest} |")
    return lines


def write_board(context: Context) -> Path | None:
    configured = context.project.config["status_board"]
    if not configured:
        return None
    path = context.project.root / configured
    path.write_text("\n".join(board(context)) + "\n", encoding="utf-8")
    return path
