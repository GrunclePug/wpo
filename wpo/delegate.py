"""Lets a WP's builder lead workers on free (and local) models, from inside its own session.

    wpo delegate start --name <slug> --write-paths <globs> '<task>'   (or --prompt-file <tree-relative .md>)
    wpo delegate wait --name <slug> [--minutes N]
    wpo delegate list

`start` returns at once; the worker runs detached in the same worktree, edits limited to its write
paths (each inside the WP's owns) and the builder command allowlist. Only free and local routes serve
workers: when none is available the task ends `no-route` and the lead does the piece itself. Workers
have no time limit. `wait` returns when the worker finishes; a lead runs it in the background and is
woken then (`--minutes` bounds a foreground wait). The WP and tree come from WPO_WORK_PACKAGE and
WPO_TREE, set by the runner.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import wpfile
from .context import Context
from .platform import FileLock, replace_file, spawn_detached
from .t3 import Bridge, is_legacy, read_json, write_json
from .routing import with_mode, wp_mode
from .runner import delegate_lock, delegation_open, session_env, worker_pid, worker_runs
from .session import NoRouteError, SessionCancelled, run_session
from .harness import SHELL_CHAINING, command_prefixes
from .session_brief import builder_skills, included_files

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


class DelegateError(Exception):
    """A delegation request that cannot be honoured; the message tells the lead what to fix."""


def _environment() -> tuple[str, Path]:
    wp_id, tree = os.environ.get("WPO_WORK_PACKAGE"), os.environ.get("WPO_TREE")
    if not wp_id or not tree:
        raise DelegateError("WPO_WORK_PACKAGE and WPO_TREE are not set; delegation runs inside a WP builder session")
    return wp_id, Path(tree)


class Delegation:
    def __init__(self, context: Context) -> None:
        self.context = context
        self.wp_id, self.tree = _environment()
        self.wp = context.wp_in_tree(self.wp_id, self.tree)
        self.directory = context.project.runs_dir / self.wp_id / "delegate"

    def task_dir(self, name: str) -> Path:
        if not NAME_PATTERN.match(name):
            raise DelegateError("--name must be a lowercase slug of letters, digits and '-' (at most 48 characters)")
        return self.directory / name

    @staticmethod
    def read_state(task_dir: Path) -> dict:
        return json.loads((task_dir / "state.json").read_text(encoding="utf-8"))

    @staticmethod
    def write_state(task_dir: Path, state: dict) -> None:
        # Written aside and moved into place, so `wait` never reads a half-written state.
        temporary = task_dir / f"state.json.{os.getpid()}.tmp"
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        replace_file(temporary, task_dir / "state.json")

    @staticmethod
    def running(task_dir: Path, state: dict) -> bool:
        """'running' with a dead worker process (a killed lead takes its workers along) is over.
        Until the worker records its own pid, the pid `start` spawned stands for it."""
        if state["state"] != "running":
            return False
        pid = worker_pid(task_dir, state)
        return not pid or worker_runs(pid, state.get("token"))

    def check_write_paths(self, paths: list[str]) -> None:
        if not paths:
            raise DelegateError("--write-paths is required: the repo-relative files or globs the worker may edit")
        wp_dir = self.context.project.wp_dir_relative
        for path in paths:
            if ".." in path or path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", path):
                raise DelegateError(f"write path '{path}' must be repo-relative with no '..'")
            # The glob's fixed prefix stands for every file it can match.
            representative = re.sub(r"\*\*.*$", "x/x.rs", path).replace("*", "x")
            if wpfile.is_wp_file(representative, wp_dir) or not wpfile.path_owned(representative, self.wp, wp_dir, ()):
                raise DelegateError(f"write path '{path}' is not inside {self.wp_id}'s owns ({', '.join(self.wp.owns)})")

    def start(self, name: str, task: str | None, prompt_file: str | None, write_paths: list[str], tier: str | None) -> None:
        task_dir = self.task_dir(name)
        paths = [part.strip().replace("\\", "/") for entry in write_paths for part in entry.split(",") if part.strip()]
        self.check_write_paths(paths)
        if (task is None) == (prompt_file is None):
            raise DelegateError("give the task either as text or with --prompt-file, not both")
        source = self.tree / prompt_file if prompt_file is not None else None
        if source is not None and not source.is_file():
            raise DelegateError(f"prompt file '{prompt_file}' not found in the worktree")
        if task is not None and not task.strip():
            raise DelegateError("the task text is empty")
        tier = tier or self.wp.delegate_tier(self.context.project.config["delegate_default"]) or self.context.project.config["delegate_default"]
        tier = with_mode(tier, wp_mode(self.context.project.runs_dir, self.wp_id))  # workers route in their WP's mode
        self.context.router.select(tier, self.context.project.config["worker_families"])  # validates the tier now, not in the detached worker
        self.uncollected_session(task_dir, retire=True)
        with delegate_lock(self.context, self.wp_id):
            if not delegation_open(self.context, self.wp_id):
                raise DelegateError("delegation is closed: this WP's builder session has ended")
            if (task_dir / "state.json").exists() and self.running(task_dir, self.read_state(task_dir)):
                raise DelegateError(f"task '{name}' is still running; wait for it or pick another name")
            # Checked again here: an earlier run of the name may have published since.
            if self.uncollected_session(task_dir):
                raise DelegateError(f"task '{name}' still has a native session to collect; pick another name")
            # The receipts and their archived logs stay, so the name's next session is a new generation.
            for file in ("state.json", "spawn.pid", "task.md", "result.md", "worker-process.log"):
                (task_dir / file).unlink(missing_ok=True)
            task_dir.mkdir(parents=True, exist_ok=True)
            if source is not None:
                shutil.copyfile(source, task_dir / "task.md")
            else:
                (task_dir / "task.md").write_text(task, encoding="utf-8")
            # The token ties the worker process to this run of the name: a delayed child of an
            # earlier run finds another token and exits.
            token = uuid.uuid4().hex
            pid = spawn_detached([*self.context.machine.wpo_command(), "delegate", "run", "--name", name, "--token", token], self.tree,
                                 env={**os.environ}, log_path=task_dir / "worker-process.log")
            (task_dir / "spawn.pid").write_text(str(pid), encoding="utf-8")
            # Last, so a reader under the lock never sees a running task without its pid.
            self.write_state(task_dir, {"state": "running", "tier": tier, "write_paths": paths, "started": datetime.now().isoformat(timespec="seconds"),
                                        "route": None, "detail": None, "worker_pid": None, "token": token})
        print(f"started '{name}' ({tier}, free and local routes only); writes limited to {', '.join(paths)}")
        print(f"next: wpo delegate wait --name {name}, in the background; you are woken when the worker finishes")

    def uncollected_session(self, task_dir: Path, retire: bool = False) -> bool:
        """Whether the name's native session is still to be collected: a receipt names a request
        that is published, unconsumed and not legacy. With `retire` (under each receipt's lock, so
        never while holding the delegate lock), a receipt naming a legacy request (finished before
        completions recorded their changes) is retired, so the name gets a new generation."""
        if self.context.machine.execution_mode != "t3":
            return False
        bridge = Bridge(self.context)
        for receipt in task_dir.glob("*.t3-invocation.json"):
            with FileLock(receipt.with_suffix(".lock")) if retire else contextlib.nullcontext():
                recorded = read_json(receipt)
                request = read_json(bridge.request_path(recorded["request_id"])) if recorded and not recorded["consumed"] else None
                if request is None or request.get("consumed_at"):
                    continue
                if not is_legacy(request):
                    return True
                if retire:
                    write_json(receipt, {**recorded, "consumed": True, "retired": "legacy"})
        return False

    def admitted(self, name: str, token: str | None, gate: bool = False) -> dict | None:
        """The task's state when the worker holding `token` may still run, and with `gate` may also
        publish a session (call under the lock). A token missing on both sides (a task started
        before tokens) matches."""
        task_dir = self.task_dir(name)
        state = self.read_state(task_dir) if (task_dir / "state.json").exists() else None
        if state is None or state["state"] != "running" or state.get("stop_requested") or state.get("token") != token:
            return None
        return state if not gate or delegation_open(self.context, self.wp_id) else None

    def run_worker(self, name: str, token: str | None = None) -> None:
        """The detached worker process that `start` launches."""
        task_dir = self.task_dir(name)
        with delegate_lock(self.context, self.wp_id):
            state = self.admitted(name, token)
            if state is None or state.get("worker_pid"):
                return
            state["worker_pid"] = os.getpid()
            self.write_state(task_dir, state)

        @contextlib.contextmanager
        def admission(route: dict):
            # Held while the native request is published: the worker barrier either finds it, or
            # has closed the gate first and the worker never publishes.
            with delegate_lock(self.context, self.wp_id):
                if self.admitted(name, token, gate=True) is None:
                    raise SessionCancelled(route["id"], "cleanup", "stopped by the worker barrier")
                yield
        write_list = "\n".join(f"- {path}" for path in state["write_paths"])
        rules = "\n".join(self.context.project.config["prompt_rules"])
        wp_file = included_files(self.tree, [f"{self.context.project.wp_dir_relative}/{self.wp.file_name}"])
        prompt = f"""{wp_file}

You are a worker on work package {self.wp_id}, whose file is above (for context only: never edit it). Its lead gave you the one task below.
You may change only these paths:
{write_list}
Shell commands run from the repository root. {SHELL_CHAINING} Allowed commands: {', '.join(command_prefixes('worker', self.context.project.config, False))}.
Other sessions may be building in this tree at the same time: if a build waits on a lock, let it wait.
{rules}
End with a short report: the files you changed, the commands you ran to check your work and whether they passed, and anything you could not finish.

# Task

{(task_dir / 'task.md').read_text(encoding='utf-8')}"""
        try:
            result = run_session(self.context, state["tier"], "worker", prompt, task_dir / "session", self.tree, write_paths=state["write_paths"],
                                 families=self.context.project.config["worker_families"], idle_minutes=float(self.context.project.config["worker_idle_minutes"]),
                                 event_fields={"wp": self.wp_id, "role": "worker", "task": name}, extra_env=session_env(self.context, self.wp_id, self.tree),
                                 skills=builder_skills(self.context, self.wp.skills), admission=admission)
            (task_dir / "result.md").write_text(result.result_text, encoding="utf-8")
            state.update(state="done", route=result.route["id"], detail=f"after fallbacks: {'; '.join(result.failures)}" if result.failures else None)
        except NoRouteError as error:
            state.update(state="no-route", detail=str(error))
        except SessionCancelled as cancelled:
            state.update(state="failed", detail=f"stopped: {cancelled}")
        except Exception as error:  # the lead must learn of any failure, whatever its type
            if getattr(error, "stalled_wp", None):
                self.context.journal.emit("delegate-worker-stalled", f"delegated worker '{name}' of {self.wp_id} stalled: {error}",
                                          {"wp": self.wp_id, "task": name})
            state.update(state="failed", detail=f"{type(error).__name__}: {error}")
        state["finished"] = datetime.now().isoformat(timespec="seconds")
        with delegate_lock(self.context, self.wp_id):
            # A worker the barrier stopped leaves the barrier's outcome in place.
            if self.admitted(name, token) is not None:
                self.write_state(task_dir, state)

    def wait(self, name: str, minutes: float | None) -> None:
        task_dir = self.task_dir(name)
        if not (task_dir / "state.json").exists():
            raise DelegateError(f"no task '{name}'; start it first")
        deadline = time.monotonic() + minutes * 60 if minutes is not None else None
        while self.running(task_dir, self.read_state(task_dir)) and (deadline is None or time.monotonic() < deadline):
            time.sleep(10)
        state = self.read_state(task_dir)
        if state["state"] == "running" and not self.running(task_dir, state):
            state.update(state="failed", detail="the worker process is gone")
        route = f" on {state['route']}" if state.get("route") else ""
        detail = f" ({state['detail']})" if state.get("detail") else ""
        print(f"task '{name}': {state['state']}{route}{detail}")
        if state.get("stop_requested"):
            print("delegation was stopped: end your session without further edits.")
        elif state["state"] == "running":
            print("still running; wait again (in the background) to be woken when it finishes.")
        elif state["state"] == "done":
            print("Worker report (check it against git diff before relying on it):")
            print((task_dir / "result.md").read_text(encoding="utf-8"))
        else:
            print("No worker finished this task; do it yourself (its partial edits, if any, are in the working tree).")

    def list(self) -> None:
        if not self.directory.exists():
            print("no delegated tasks")
            return
        for task_dir in sorted(path for path in self.directory.iterdir() if (path / "state.json").is_file()):
            state = self.read_state(task_dir)
            route = f" on {state['route']}" if state.get("route") else ""
            print(f"{task_dir.name}: {state['state']}{route} [{', '.join(state['write_paths'])}]")
