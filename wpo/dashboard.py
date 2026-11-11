"""The progress dashboard's lifecycle: one local web server per project, always up while wpo is used.

    wpo dashboard            print the URL, starting the server when it is not running
    wpo dashboard --stop     stop it; no wpo command starts it again until `wpo dashboard`

Every other `wpo` command calls `autostart` first. Its common path is a file read and a process
check (well under a millisecond of work beyond the interpreter's start), so builder sessions running
`wpo log` in their worktrees pay nothing noticeable. Worktrees resolve to the main checkout, whose
runs directory holds the one state file, so one server serves the project whichever tree wpo runs in.

Starting is single-instance twice over: launches are serialized by a machine-wide mutex and
recorded in `<runs>/dashboard.json` before the launcher lets go, and the server itself holds the
project's dashboard mutex for its whole life (a second server exits at once). The server is started
through a short-lived intermediate process, so it belongs to no caller's process tree: a session's
hang detection counts its tree's CPU, and stopping a lane or a session kills its tree.

A server whose code is older than the skill's (`code_version`) is replaced on the next wpo command.
A launch that died or failed to bind is retried only after a pause, so a broken setup costs one
attempt per pause, not one per command. Disable autostart per machine with `"dashboard":
{"autostart": false}` (tests do) or per process with WPO_DASHBOARD_AUTOSTART=0.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import psutil

from . import PACKAGE_ROOT, SKILL_ROOT
from .machine import MachineConfig
from .platform import NamedMutex, pid_alive, replace_file, spawn_detached
from .project import Project

STATE_FILE = "dashboard.json"
STOPPED_FILE = "dashboard.stopped"
LOG_FILE = "dashboard.log"
AUTOSTART_ENVIRONMENT = "WPO_DASHBOARD_AUTOSTART"
SERVER_PROCESS_NAMES = ("python", "python3", "pythonw")
# Phases the state file records: launched (the intermediate runs), starting (the server process
# runs), serving (bound and answering), failed (it could not bind or crashed while starting).
LIVE_PHASES = ("launched", "starting", "serving")
# A launch whose process is gone is left alone this long before another is tried.
LAUNCH_RETRY_SECONDS = 60
FAILED_RETRY_SECONDS = 600
STOP_WAIT_SECONDS = 10
START_WAIT_SECONDS = 20


class DashboardError(Exception):
    """The dashboard could not be started or stopped as asked."""


# What a running server has loaded: the tooling's modules, its entry point and the routing policy.
# The frontend's files are read from disk per request and need no restart.
CODE_INPUTS = ((PACKAGE_ROOT, ".py"), (SKILL_ROOT / "bin", ".py"), (SKILL_ROOT / "models", ".json"))
# Numeric fields of the state file; a state with any other type in them is treated as absent.
STATE_NUMBERS = ("pid", "created", "launched_at", "failed_at", "serving_since")


def code_version() -> str:
    """Changes whenever the code a server loads changes, so a server running old code is replaced."""
    newest, count = 0, 0
    for directory, suffix in CODE_INPUTS:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.endswith(suffix):
                    count += 1
                    newest = max(newest, entry.stat().st_mtime_ns)
    return f"{count}-{newest}"


def state_path(project: Project) -> Path:
    return project.runs_dir / STATE_FILE


def read_state(project: Project) -> dict | None:
    """The recorded server state; None when absent or caught mid-write (the writer replaces it whole)."""
    try:
        state = json.loads(state_path(project).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    if any(state.get(key) is not None and not isinstance(state[key], (int, float)) for key in STATE_NUMBERS):
        return None  # not written by this tooling; a fresh launch replaces it
    return state


def write_state(project: Project, state: dict) -> None:
    path = state_path(project)
    staging = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    staging.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    replace_file(staging, path)


def process_created(pid: int) -> float | None:
    """The process's creation time, which tells it apart from a later process given the same pid."""
    try:
        return psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return None


def server_alive(state: dict | None) -> bool:
    """Whether the recorded launcher or server process still runs (that process, not a later one
    that was given its pid)."""
    if not state or state.get("phase") not in LIVE_PHASES or not isinstance(state.get("pid"), int):
        return False
    if not pid_alive(state["pid"], SERVER_PROCESS_NAMES):
        return False
    created = process_created(state["pid"])
    return created is not None and abs(created - float(state.get("created", 0))) < 1


def url_for(machine: MachineConfig, project: Project) -> str:
    return f"http://127.0.0.1:{machine.dashboard_port(project.root)}/"


def launch_pending(state: dict | None, now: float) -> str | None:
    """Why no new launch should be tried yet: a recent launch that died, or a recent failure."""
    if not state:
        return None
    if state.get("phase") == "failed" and now - float(state.get("failed_at", 0)) < FAILED_RETRY_SECONDS:
        return f"the last start failed ({state.get('error', 'unknown error')}); retried after {FAILED_RETRY_SECONDS // 60} min or by `wpo dashboard`"
    if state.get("phase") in LIVE_PHASES and state.get("phase") != "serving" and now - float(state.get("launched_at", 0)) < LAUNCH_RETRY_SECONDS:
        return "a launch is under way"
    return None


def ensure_running(project: Project, machine: MachineConfig, explicit: bool = False) -> str:
    """Makes sure the project's server runs current code; returns what it found or did. `explicit`
    (the operator's `wpo dashboard`) starts it whatever autostart, a stop or a recent failure says."""
    if not explicit:
        if not machine.dashboard_autostart or os.environ.get(AUTOSTART_ENVIRONMENT) == "0":
            return "autostart is off"
        if (project.runs_dir / STOPPED_FILE).exists():
            return "stopped by the operator (`wpo dashboard` starts it)"
    version = code_version()
    state = read_state(project)
    alive = server_alive(state)
    if alive and state.get("code") == version:
        return "running"
    if not explicit and not alive and (pending := launch_pending(state, time.time())):
        return pending
    mutex = NamedMutex(project.mutex_name("dashboard-start"))
    if not mutex.acquire(blocking=False):
        return "another process is starting it"
    try:
        state = read_state(project)
        if server_alive(state):
            if state.get("code") == version:
                return "running"
            stop_recorded_server(project)
        pid = spawn_detached([*machine.wpo_command(), "--project", str(project.root), "dashboard", "--detach-server"], project.root,
                             env=server_environment(), log_path=project.runs_dir / LOG_FILE)
        write_state(project, {"phase": "launched", "pid": pid, "created": process_created(pid) or 0, "launched_at": time.time(),
                              "code": version, "url": url_for(machine, project)})
        return "started"
    finally:
        mutex.release()


def autostart(project: Project, machine: MachineConfig) -> None:
    """Called by every wpo command. A dashboard problem never fails the command; it is logged to
    `<runs>/dashboard.log`, which `wpo dashboard` points at."""
    try:
        ensure_running(project, machine)
    except (OSError, TimeoutError, ValueError, DashboardError, psutil.Error) as error:
        log(project, f"autostart failed: {type(error).__name__}: {error}\n{traceback.format_exc()}")


def log(project: Project, message: str) -> None:
    """Appends to `<runs>/dashboard.log`; where that cannot be written (a read-only sandbox), the
    line goes to stderr instead, so a dashboard problem still never fails the caller."""
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} [{os.getpid()}] {message.rstrip()}\n"
    try:
        with (project.runs_dir / LOG_FILE).open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        sys.stderr.write(f"wpo dashboard: {line}")


def server_environment() -> dict[str, str]:
    """The caller's environment without git's repository overrides: a hook or session that sets
    GIT_DIR would point the server's git reads at another repository."""
    return {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}


def terminate(pid: int) -> None:
    """Ends a server process and waits until it is gone, so its port and mutex are free."""
    try:
        process = psutil.Process(pid)
        process.terminate()
        process.wait(STOP_WAIT_SECONDS)
    except psutil.NoSuchProcess:
        return
    except psutil.TimeoutExpired as error:
        raise DashboardError(f"the dashboard server (pid {pid}) did not end within {STOP_WAIT_SECONDS} s") from error


def stop_recorded_server(project: Project) -> int | None:
    """Ends the server the state file names and returns its pid. A state still naming the
    intermediate (it records the server within moments, then exits) is waited out first, so the
    server it started is the one ended, not left running unrecorded."""
    for _ in range(3):
        state = read_state(project)
        if not server_alive(state):
            return None
        if state.get("phase") != "launched":
            terminate(int(state["pid"]))
            return int(state["pid"])
        try:
            psutil.Process(int(state["pid"])).wait(STOP_WAIT_SECONDS)
        except psutil.NoSuchProcess:
            pass
        except psutil.TimeoutExpired:
            terminate(int(state["pid"]))
            return int(state["pid"])
    raise DashboardError("the dashboard's state kept changing while it was being stopped; run `wpo dashboard --stop` again")


def detach_server(project: Project, machine: MachineConfig) -> int:
    """The intermediate process: starts the server, records it, and exits, which leaves the server
    without a living parent in anyone's process tree."""
    pid = spawn_detached([*machine.wpo_command(), "--project", str(project.root), "dashboard", "--serve"], project.root,
                         env=server_environment(), log_path=project.runs_dir / LOG_FILE)
    state = read_state(project) or {}
    if state.get("pid") != pid:  # the server may already have recorded itself as serving
        write_state(project, {**state, "phase": "starting", "pid": pid, "created": process_created(pid) or 0})
    return 0


def stop(project: Project) -> str:
    """Stops the server and keeps autostart from starting it again until `wpo dashboard`."""
    (project.runs_dir / STOPPED_FILE).write_text(f"stopped {datetime.now():%Y-%m-%d %H:%M:%S}\n", encoding="utf-8")
    pid = stop_recorded_server(project)
    state_path(project).unlink(missing_ok=True)
    if pid is None:
        return "the dashboard was not running; autostart stays off until `wpo dashboard`"
    return f"stopped the dashboard (pid {pid}); `wpo dashboard` starts it again"


def start_and_report(project: Project, machine: MachineConfig) -> tuple[bool, str]:
    """`wpo dashboard`: lifts a stop, starts the server if needed, waits until it serves."""
    (project.runs_dir / STOPPED_FILE).unlink(missing_ok=True)
    outcome = ensure_running(project, machine, explicit=True)
    deadline = time.monotonic() + START_WAIT_SECONDS
    while time.monotonic() < deadline:
        state = read_state(project)
        if state and state.get("phase") == "serving" and server_alive(state):
            return True, f"{state['url']}  ({'started' if outcome == 'started' else 'running'}, pid {state['pid']})"
        if state and state.get("phase") == "failed":
            return False, f"the dashboard could not start: {state.get('error')} (log: {project.runs_dir / LOG_FILE})"
        time.sleep(0.2)
    return False, f"the dashboard did not start within {START_WAIT_SECONDS} s ({outcome}); see {project.runs_dir / LOG_FILE}"
