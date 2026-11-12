"""Durable agent-mediated T3 execution.

Only a live T3 provider thread may call delegate_task. Python therefore queues work and wakes an
attached coordinator through T3's supported CLI. The coordinator executes native tools and records
their callbacks here. No provider credentials are copied and no model CLI is launched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from .platform import FileLock, NamedMutex, replace_file

if TYPE_CHECKING:
    from .context import Context


# The model option that carries a route's effort, by the name each T3 provider gives it.
EFFORT_OPTIONS = ("effort", "reasoningEffort", "thinking_level")
TERMINAL_STATES = ("done", "failed", "cancelled")
# Kinds asked again once their last request finished: the same attention reason can recur, and a
# failed or cancelled checkout must be preparable again. Sessions have their own receipts;
# cancels and messages are one-shot.
REARMED_KINDS = ("attention", "workspace")
# Who asked for a cancellation. `operator` and `cleanup` end the session without a fallback route
# (SessionCancelled); `cutoff` (quota) keeps the failure path; `compaction` is decided by the
# request's `compacted` flag. A stronger origin replaces a weaker one on the original request.
CANCEL_ORIGIN_RANK = {"compaction": 0, "cutoff": 1, "cleanup": 2, "operator": 3}
# `t3 thread send` may take this long; the wake lock covers it, the debounce and a margin.
WAKE_SEND_TIMEOUT_SECONDS = 45


def request_id_for(kind: str, key: str, generation: int = 1) -> str:
    """A request's id. Generation 1 keeps the original derivation, so existing request files stay
    valid; later generations hash an encoding no generation-1 input (which starts with a kind name)
    and no other key's generation can equal."""
    text = f"{kind}:{key}" if generation == 1 else json.dumps(["generation", generation, kind, key])
    return hashlib.sha256(text.encode()).hexdigest()[:32]


class T3Error(RuntimeError):
    """The native coordinator is unavailable or a native operation failed."""


def read_json(path: Path, default=None):
    # Windows can briefly deny an open while a completion callback replaces the file.
    for attempt in range(50):
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.01)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    staging.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    replace_file(staging, path)


class Bridge:
    def __init__(self, context: Context) -> None:
        self.context = context
        self.project = context.project
        self.settings = context.machine.raw.get("t3", {})
        self.directory = self.project.runs_dir / "t3"

    @property
    def coordinator(self) -> str | None:
        attached = read_json(self.directory / "coordinator.json", {})
        return attached.get("thread_id") or self.settings.get("coordinator_thread_id")

    def workspace(self, wp_id: str) -> dict | None:
        return read_json(self.project.wp_run_dir(wp_id) / "t3-workspace.json")

    def validate_workspace(self, wp_id: str) -> Path:
        from .project import git

        workspace = self.workspace(wp_id)
        if workspace is None:
            raise T3Error(f"{wp_id} has no registered T3 checkout; prepare it through its native coordinator first")
        tree = Path(workspace["path"])
        if not tree.is_dir():
            raise T3Error(f"Registered native checkout is missing: {tree}")
        actual = Path(git(tree, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        ours = Path(git(self.project.root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()).resolve()
        theirs = Path(git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()).resolve()
        branch = git(tree, "symbolic-ref", "--short", "HEAD").stdout.strip()
        if tree.resolve() != actual or ours != theirs or branch != workspace["branch"]:
            raise T3Error("The recorded T3 workspace no longer matches its repository, checkout path, or native branch; reconnect it explicitly")
        return tree

    def coordinator_for(self, wp_id: str | None) -> str:
        workspace = self.workspace(wp_id) if wp_id else None
        thread = workspace.get("coordinator_thread_id") if workspace else self.coordinator
        if not thread:
            raise T3Error("No T3 coordinator is attached. In a T3 thread for this repository, load wp-orchestration and run wpo t3 attach with its live capability result. No CLI fallback is permitted.")
        return thread

    def capabilities(self) -> dict:
        cached = read_json(self.directory / "capabilities.json")
        if not cached:
            raise T3Error("No live T3 capabilities recorded; attach the coordinator with --capabilities-file from orchestrator_capabilities.")
        return cached

    def target(self, route: dict) -> dict:
        capabilities = self.capabilities()
        mappings = self.settings.get("providers", {})
        provider_id = mappings.get(route["family"])
        if isinstance(provider_id, dict):
            provider_id = provider_id.get("providerInstanceId")
        provider = next((p for p in capabilities["providers"] if p["providerInstanceId"] == provider_id), None)
        if not provider or not provider.get("canRunChildTask"):
            raise T3Error(f"T3 provider for family {route['family']} is unavailable or disabled; configure t3.providers and refresh capabilities")
        requested = self.settings.get("models", {}).get(route["model"], route["model"])
        model = next((m for m in provider.get("models", []) if m["id"] == requested), None)
        if model is None:
            raise T3Error(f"T3 model {requested!r} is absent from recorded capabilities for {provider_id}; refresh with orchestrator_capabilities(search=...) or configure t3.models")
        options = dict(self.settings.get("options", {}).get(provider_id, {}))
        if effort := route.get("effort"):
            # Providers name the effort option differently: Claude `effort`, Codex `reasoningEffort`,
            # Antigravity `thinking_level`.
            field = next((o for o in model.get("options", []) if o["id"] in EFFORT_OPTIONS), None)
            if not field or effort not in field["values"]:
                raise T3Error(f"T3 model {requested} does not advertise effort {effort!r}")
            options[field["id"]] = effort
        for key, value in options.items():
            field = next((o for o in model.get("options", []) if o["id"] == key), None)
            if field is None or value not in field["values"]:
                raise T3Error(f"T3 model {requested} does not advertise option {key}={value!r}")
        return {"providerInstanceId": provider_id, "model": requested, "options": options}

    def ready(self) -> None:
        self.coordinator_for(None)
        self.capabilities()
        configured = self.settings.get("cli", "t3")
        if not Path(configured).expanduser().is_file() and not shutil.which(configured):
            raise T3Error(f"T3 CLI {configured!r} is unavailable; configure t3.cli. No model CLI fallback is permitted.")

    def thread_snapshot(self, request_id: str) -> dict:
        """Read a session's latest conversation once, on an explicit transcript request."""
        request = read_json(self.request_path(request_id), {})
        thread_id = request.get("thread_id")
        if request.get("kind") != "session" or not isinstance(thread_id, str) or not thread_id:
            raise T3Error("This native session has no recorded T3 thread yet. Refresh after it starts.")
        command = [str(Path(self.settings.get("cli", "t3")).expanduser()), "thread", "show", thread_id,
                   "--messages", "100", "--json"]
        if self.settings.get("base_dir"):
            command += ["--base-dir", str(Path(self.settings["base_dir"]).expanduser())]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise T3Error(f"Could not read the T3 conversation: {error}") from error
        if completed.returncode:
            raise T3Error(f"Could not read the T3 conversation: {(completed.stderr or completed.stdout).strip()[:1000]}")
        try:
            snapshot = json.loads(completed.stdout)
        except ValueError as error:
            raise T3Error("T3 returned an unreadable conversation response.") from error
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("thread"), dict)
                or snapshot["thread"].get("threadId") != thread_id or not isinstance(snapshot.get("messages"), list)
                or any(not isinstance(message, dict) or message.get("role") not in ("user", "assistant")
                       or not isinstance(message.get("text"), str) for message in snapshot["messages"])):
            raise T3Error("T3 returned an unexpected conversation response.")
        return snapshot

    def attention(self, event: dict) -> None:
        if not self.coordinator:
            return  # The original event journal remains the source of truth before attachment.
        key = ":".join(str(event.get(name, "")) for name in ("kind", "wp", "supervisor", "message"))
        try:
            self.submit("attention", key, {"event": event}, self.coordinator)
        except (T3Error, OSError, subprocess.TimeoutExpired) as error:
            # A notification failure must not prevent the actual journaled result from being saved.
            with (self.directory / "wake-errors.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{time.time()}: {error}\n")

    def attach(self, thread_id: str, capabilities: dict, workspace: dict | None = None) -> None:
        if capabilities.get("parentThreadId") != thread_id:
            raise T3Error("Capability parentThreadId must match the coordinator's actual T3 thread ID")
        if not isinstance(capabilities.get("providers"), list):
            raise T3Error("The capability result has no providers list")
        if workspace is not None and Path(workspace["projectWorkspaceRoot"]).resolve() != self.project.root:
            raise T3Error("The coordinator's actual T3 project root does not match this WPO repository")
        write_json(self.directory / "capabilities.json", capabilities)
        write_json(self.directory / "coordinator.json", {"version": 1, "thread_id": thread_id, "project": str(self.project.root), "attached_at": time.time(),
                                                       "checkout_path": (workspace.get("worktreePath") or workspace["projectWorkspaceRoot"]) if workspace is not None else str(self.project.root)})

    def request_path(self, request_id: str) -> Path:
        if len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
            raise T3Error("Invalid bridge request ID")
        return self.directory / "requests" / f"{request_id}.json"

    def wake(self, thread_id: str, force: bool = False, created_after: float | None = None) -> None:
        """Sends the coordinator a control wake. The stamp holds when the last send started: a
        request created before that is covered by it (the coordinator's `t3 next` runs after the
        send arrives). A wake for a newer request inside the debounce window waits out the window
        and sends, rather than being dropped: an attention request has no waiter to wake it again."""
        self.directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(thread_id.encode()).hexdigest()[:16]
        stamp = self.directory / f"wake-{key}.json"
        debounce = float(self.settings.get("wake_debounce_seconds", 5))
        with FileLock(self.directory / f"wake-{key}.lock", timeout_seconds=WAKE_SEND_TIMEOUT_SECONDS + debounce + 10):
            previous = read_json(stamp, {})
            if not force and created_after is not None and previous.get("at", 0) >= created_after:
                return
            remaining = debounce - (time.time() - previous.get("at", 0))
            if not force and remaining > 0:
                if created_after is None:
                    return
                # The lock is held, so no other send can start meanwhile.
                time.sleep(remaining)
            prompt = (f"WPO T3 backend control: pending native requests for coordinator {thread_id}. "
                      f"Read the wp-orchestration T3 bridge instructions and run wpo --project \"{self.project.root.as_posix()}\" "
                      f"t3 next --coordinator {thread_id}. Execute the returned native tool plan; do not perform the requested worker/review task yourself. "
                      "Honor delegate_task.next and completion notifications; do not poll running tasks. This is a coordinator wake, not a request to resume held lanes.")
            command = [str(Path(self.settings.get("cli", "t3")).expanduser()), "thread", "send", thread_id, "-", "--json"]
            if self.settings.get("base_dir"):
                command += ["--base-dir", str(Path(self.settings["base_dir"]).expanduser())]
            started = time.time()
            completed = subprocess.run(command, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                       timeout=WAKE_SEND_TIMEOUT_SECONDS, check=False)
            if completed.returncode:
                raise T3Error(f"T3 coordinator wake failed: {(completed.stderr or completed.stdout).strip()[:1000]}")
            write_json(stamp, {"at": started, "thread_id": thread_id})

    def submit(self, kind: str, key: str, payload: dict, coordinator: str, initial: dict | None = None) -> dict:
        generation = 1
        while True:
            request_id = request_id_for(kind, key, generation)
            path = self.request_path(request_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(path.with_suffix(".lock")):
                request = read_json(path)
                if request is None:
                    request = {"version": 1, "id": request_id, "kind": kind, "state": "pending", "coordinator_thread_id": coordinator,
                               "created_at": time.time(), "payload": payload, "task_id": None, "thread_id": None, "link": None,
                               **(initial or {})}
                    if generation > 1:
                        request["generation"] = generation
                    write_json(path, request)
                    break
            if kind in REARMED_KINDS and request["state"] in TERMINAL_STATES:
                generation += 1
                continue
            break
        # Stable keys make restarts reconnect to an existing request instead of launching another child.
        if request["state"] in ("pending", "claimed"):
            self.wake(coordinator, created_after=request["created_at"])
        return request

    def pending(self, coordinator: str) -> list[dict]:
        return [request for path in sorted((self.directory / "requests").glob("*.json"))
                if (request := read_json(path)) and request["coordinator_thread_id"] == coordinator
                and request["state"] in ("pending", "claimed", "running", "waiting", "stalled")]

    def update(self, request_id: str, **fields) -> dict:
        path = self.request_path(request_id)
        with FileLock(path.with_suffix(".lock")):
            request = read_json(path)
            if request is None:
                raise T3Error(f"Unknown bridge request {request_id}")
            request.update(fields, updated_at=time.time())
            write_json(path, request)
            return request

    def wait(self, request: dict, callback=None) -> dict:
        # This waits on local durable callbacks, never polls native task_status. Native completion
        # wakes the host agent, which records the result using `t3 complete`.
        while True:
            current = read_json(self.request_path(request["id"]))
            if current["state"] in ("done", "failed", "cancelled"):
                return current
            if current["state"] == "stalled":
                raise T3Error(current["detail"])
            limit = float(current["payload"].get("idle_minutes", self.settings.get("coordinator_idle_minutes", 30))) * 60
            hard_limit = float(self.settings.get("hard_idle_minutes", 240)) * 60
            # Only real activity counts: creation, the launch, callbacks and heartbeats, and the
            # session's own files. Bookkeeping (`update` moves updated_at) never resets the clock.
            # A request launched before `started_at` existed falls back to its last update, pinned
            # once below so the notice's own write cannot move it.
            started = current.get("started_at") or (current.get("updated_at", 0) if current.get("task_id") else 0)
            activity = max(current["created_at"], started, current.get("last_activity_at", 0))
            for name in ("heartbeat_file", "progress_file"):
                if current["payload"].get(name) and Path(current["payload"][name]).is_file():
                    activity = max(activity, Path(current["payload"][name]).stat().st_mtime)
            idle = time.time() - activity
            if not current.get("task_id"):
                if limit > 0 and idle >= limit:
                    # Never launched: a wake may have been lost. Ask once more, then give up.
                    if not current.get("rewoken_at"):
                        self.wake(current["coordinator_thread_id"], force=True)
                        self.update(current["id"], rewoken_at=time.time())
                    elif time.time() - current["rewoken_at"] >= limit:
                        self.stall(current, f"was never launched, though the coordinator was woken again {limit / 60:g} minutes ago")
            elif hard_limit > 0 and idle >= hard_limit:
                # The hard deadline stands on its own: a shorter one than the idle limit, or no
                # quiet notices at all (idle_minutes 0), still stalls on time.
                self.stall(current, f"has had no callback or progress for {hard_limit / 60:g} minutes")
            elif limit > 0 and idle >= limit and current.get("quiet_reported_for") != activity:
                # A long session that writes nothing (a review) is not a failure: say so once per
                # quiet period, and keep waiting.
                self.update(current["id"], quiet_reported_for=activity, started_at=started or None)
                wp = current["payload"].get("wp")
                self.context.journal.emit("t3-session-quiet", f"native {current['payload'].get('role', current['kind'])} session {current['id']}"
                                          f"{f' of {wp}' if wp else ''} has been quiet for {idle / 60:.0f} minutes; still waiting "
                                          f"(stalled after {hard_limit / 60:g})", {"wp": wp} if wp else {})
            if current["state"] == "pending":
                self.wake(current["coordinator_thread_id"], created_after=current["created_at"])
            if callback:
                callback(current)
            time.sleep(float(self.settings.get("callback_interval_seconds", 0.5)))

    def stall(self, request: dict, why: str) -> None:
        detail = (f"Native request {request['id']} {why}; marked stalled. The native task may still be running. "
                  "Use wpo t3 recover to reconcile completion; no replacement or CLI fallback was started.")
        self.update(request["id"], state="stalled", detail=detail)
        raise T3Error(detail)

    def tool_plan(self, request: dict) -> dict:
        payload = request["payload"]
        if request["kind"] == "workspace":
            prompt = (
                f"You are WPO's coordinator for {payload['wp']}, not its builder. This T3-owned checkout must be preserved. "
                f"First run wpo --project \"{self.project.root.as_posix()}\" t3 register-workspace --request {request['id']} "
                "--thread <your actual T3 thread ID from orchestrator_capabilities>. "
                "Register only your actual assigned checkout, and do not create a checkout in the shell. "
                "Then read wp-orchestration references/t3-bridge.md. When woken with native session requests, "
                "dispatch each via a NEW delegate_task with shared isolation in this checkout. Never implement or review the WP yourself. "
                "Do not start lanes or alter holds. Report the registration result and wait for coordinator wakes."
            )
            arguments = {"task": prompt, "title": f"WPO {payload['wp']} coordinator", "isolation": "worktree", "mode": "async",
                         "role": "general", "clientRequestId": request["id"]}
            if self.settings.get("project_id"):
                arguments["target"] = {"projectId": self.settings["project_id"]}
            return {"tool": "delegate_task", "arguments": arguments}
        if request["kind"] == "session":
            context_path = self.request_path(request["id"]).with_suffix(".prompt.md")
            # Replace a linked context file itself rather than following its target on a retry.
            context_path = context_path.parent.resolve() / context_path.name
            staging = context_path.with_name(f".{context_path.name}.{uuid.uuid4().hex}.tmp")
            staging.write_text(payload["prompt"], encoding="utf-8", newline="\n")
            replace_file(staging, context_path)
            task = (f"Read the complete WPO session context from this UTF-8 file before doing any work: \"{context_path.as_posix()}\". "
                    "Follow its instructions as the task for this session. Read the entire file; if output is truncated, "
                    "continue reading in chunks until all of it has been read. If the file cannot be read, stop and report the error."
                    f"\n\nRecord real progress between long steps with wpo --project \"{self.project.root.as_posix()}\" "
                    f"t3 heartbeat --request {request['id']}. Do not invent heartbeat activity when no work is progressing.")
            arguments = {"task": task, "title": payload["title"], "isolation": "shared", "mode": "async",
                         "role": payload["native_role"], "target": payload["target"], "clientRequestId": request["id"]}
            return {"tool": "delegate_task", "arguments": arguments}
        if request["kind"] == "message":
            message = payload["message"]
            if self.coordinator:
                message += (f"\n\nWPO reply routing: the project's user-facing orchestrator is T3 thread {self.coordinator}. "
                            "If the user's message asks you to report back to the user or orchestrator, send that requested reply "
                            "to this user-facing thread using t3_thread_send. Your immediate WP coordinator is a different thread; "
                            "an acknowledgement there alone does not reach the user's chat. Continue the assigned work after replying.")
            return {"tool": "t3_thread_send", "arguments": {"threadId": payload["thread_id"], "message": message,
                                                            "mode": "steer", "clientRequestId": request["id"]}}
        if request["kind"] == "cancel":
            return {"tool": "task_cancel", "arguments": {"taskId": payload["task_id"]}}
        raise T3Error(f"Unknown request kind {request['kind']}")

    def register_workspace(self, request_id: str, thread_id: str, tree: Path) -> dict:
        from .project import git

        path = self.request_path(request_id)
        request = read_json(path)
        if not request or request["kind"] != "workspace":
            raise T3Error("Workspace registration needs a pending workspace request")
        tree = Path(git(tree, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        ours = Path(git(self.project.root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()).resolve()
        theirs = Path(git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()).resolve()
        if ours != theirs or tree == self.project.root:
            raise T3Error("The native workspace must be a separate T3-owned checkout of this exact repository")
        expected = request["payload"]["base_commit"]
        if git(tree, "merge-base", "--is-ancestor", expected, "HEAD", check=False).returncode:
            raise T3Error("The T3 checkout does not contain WPO's expected local main commit; correct T3's base selection before registering")
        branch = git(tree, "symbolic-ref", "--short", "HEAD").stdout.strip()
        with FileLock(path.with_suffix(".lock")):
            request = read_json(path)
            if request["state"] in TERMINAL_STATES:
                recorded = request.get("result") or {}
                if request["state"] == "done" and (recorded.get("path"), recorded.get("branch"), recorded.get("coordinator_thread_id")) == (str(tree), branch, thread_id):
                    return recorded  # a repeated callback is idempotent
                request["late_result"] = {"path": str(tree), "branch": branch, "thread_id": thread_id, "at": time.time()}
                write_json(path, request)
                raise T3Error(f"Workspace request {request_id} is already {request['state']}; this registration was recorded but not applied")
            workspace = {"version": 1, "backend": "t3", "wp": request["payload"]["wp"], "path": str(tree), "branch": branch,
                         "coordinator_thread_id": thread_id, "request_id": request_id, "registered_at": time.time()}
            write_json(self.project.wp_run_dir(workspace["wp"]) / "t3-workspace.json", workspace)
            request.update(state="done", result=workspace, thread_id=thread_id, updated_at=time.time())
            write_json(path, request)
        return workspace

    def ensure_workspace(self, wp_id: str) -> Path:
        from .project import git

        existing = self.workspace(wp_id)
        if existing:
            tree = self.validate_workspace(wp_id)
            self.migrate_legacy(wp_id, tree)
            return tree
        self.ready()
        request = self.submit("workspace", wp_id, {"wp": wp_id, "base_commit": git(self.project.root, "rev-parse", self.project.main_branch).stdout.strip()}, self.coordinator_for(None))
        completed = self.wait(request)
        if completed["state"] != "done":
            raise T3Error(completed.get("detail", "Native worktree preparation failed"))
        tree = Path(completed["result"]["path"])
        self.migrate_legacy(wp_id, tree)
        return tree

    def migrate_legacy(self, wp_id: str, tree: Path) -> None:
        """Import an existing WP without deleting its old worktree, dirty files, or staged edits."""
        from .project import git

        workspace_path = self.project.wp_run_dir(wp_id) / "t3-workspace.json"
        workspace = read_json(workspace_path)
        source = self.project.worktrees_root / wp_id
        if not source.is_dir() or source.resolve() == tree.resolve() or workspace.get("legacy_imported_at"):
            return
        if workspace.get("legacy_import_started_at"):
            raise T3Error(f"Legacy import for {wp_id} was interrupted; inspect both checkouts before recording legacy_imported_at. The original checkout is preserved at {source}")
        if git(tree, "status", "--porcelain", "--untracked-files=all").stdout.strip():
            raise T3Error(f"The native checkout must be clean before importing {source}; original data is untouched")
        workspace["legacy_import_started_at"] = time.time()
        workspace["legacy_source"] = str(source)
        write_json(workspace_path, workspace)
        head = git(source, "rev-parse", "HEAD").stdout.strip()
        if git(tree, "merge", "--no-edit", head, check=False).returncode:
            raise T3Error(f"Legacy branch import needs conflict resolution in {tree}; {source} is untouched")
        for staged in (True, False):
            arguments = ["git", "-C", str(source), "diff", "--binary", *(["--cached", "HEAD"] if staged else [])]
            patch = subprocess.run(arguments, check=True, capture_output=True).stdout
            if patch:
                applied = subprocess.run(["git", "-C", str(tree), "apply", *(["--index"] if staged else []), "--binary", "-"],
                                         input=patch, capture_output=True, check=False)
                if applied.returncode:
                    raise T3Error(f"Legacy dirty changes need manual import into {tree}; original files and index remain at {source}")
        untracked = git(source, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")
        for relative in filter(None, untracked):
            original, target = source / relative, tree / relative
            if not original.resolve().is_relative_to(source.resolve()) or not target.resolve().is_relative_to(tree.resolve()):
                raise T3Error(f"Legacy untracked path escapes its checkout through a link: {relative}; original data is untouched")
            for parent in (target.parent, *target.parent.parents):
                if parent == tree:
                    break
                if parent.exists() and (parent.is_symlink() or getattr(parent.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                    raise T3Error(f"Legacy import refuses linked destination ancestor {parent}")
            if target.exists() or target.is_symlink():
                raise T3Error(f"Native setup and legacy checkout both contain untracked {relative}; resolve without overwriting the original")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target, follow_symlinks=False)
        workspace["legacy_imported_at"] = time.time()
        write_json(workspace_path, workspace)

    def complete(self, request_id: str, result: dict) -> dict:
        state = result.get("state", "done")
        if state not in ("done", "failed", "cancelled"):
            raise T3Error("A completion state must be done, failed or cancelled")
        path = self.request_path(request_id)
        with FileLock(path.with_suffix(".lock")):
            request = read_json(path)
            if request is None:
                raise T3Error("Unknown bridge request")
            if request.get("task_id") and result.get("task_id") and request["task_id"] != result["task_id"]:
                raise T3Error("Completion task ID does not match the recorded native task")
            if result.get("outcome") is not None:
                from .routing import OUTCOMES
                # A staged session handles compaction; for any other, `Router.mark` would reject it.
                allowed = ("ok", *OUTCOMES, *(("compacted",) if request["payload"].get("stop_on_compaction") else ()))
                if result["outcome"] not in allowed:
                    raise T3Error(f"Completion outcome {result['outcome']!r} is not one of: {', '.join(allowed)}")
            if request["state"] == "cancelled":
                request["late_result"] = result
                write_json(path, request)
                return request
            if request["state"] in ("done", "failed"):
                if request["state"] != state:
                    raise T3Error("A terminal native result cannot be replaced by a contradictory callback")
                return request
            request.update(state=state, result=result, detail=result.get("detail"), updated_at=time.time(),
                           task_id=result.get("task_id", request.get("task_id")), thread_id=result.get("thread_id", request.get("thread_id")),
                           link=result.get("link", request.get("link")))
            write_json(path, request)
            return request

    def record_started(self, request_id: str, response: dict) -> dict:
        path = self.request_path(request_id)
        with FileLock(path.with_suffix(".lock")):
            request = read_json(path)
            if request is None:
                raise T3Error("Unknown request")
            if request["kind"] in ("session", "workspace") and not response.get("taskId"):
                raise T3Error("The native response has no taskId; record its error as a failed completion")
            if request.get("task_id") and request["task_id"] != response.get("taskId"):
                raise T3Error("A second native task was returned for one request; cancel the duplicate explicitly")
            state = request["state"] if request["state"] in ("done", "failed", "cancelled") else "running"
            request.update(state=state, task_id=response.get("taskId"), thread_id=response.get("childThreadId"), started_at=time.time(),
                           link=response.get("link"), native_response=response, updated_at=time.time())
            write_json(path, request)
        if request.get("task_id") and (state == "cancelled" or request.get("cancel_requested")):
            # Cancellation can win before delegate_task returns. The late task still must be stopped.
            self.cancel_request(request, request.get("cancel_detail") or "request was cancelled before the native launch callback arrived",
                                request.get("cancel_origin") or "operator")
        return request

    def begin_session(self, log_path: Path, payload: dict, coordinator: str) -> tuple[dict, Path]:
        receipt = log_path.with_name(log_path.name + ".t3-invocation.json")
        with FileLock(receipt.with_suffix(".lock")):
            previous = read_json(receipt)
            if previous is not None and not previous["consumed"]:
                current = read_json(self.request_path(previous["request_id"]))
                if current is not None and not current.get("consumed_at"):
                    return current, receipt
                # No request (a start interrupted before its submit) or one already finished whose
                # receipt write was lost: the request's `consumed_at` is authoritative.
                previous = {**previous, "consumed": True}
                write_json(receipt, previous)
            generation = previous["generation"] + 1 if previous else 1
            key = f"{log_path}:{generation}"
            # The receipt names the request before it is submitted (and may wake a coordinator), so a
            # crash in between never leaves a launched request without a receipt.
            write_json(receipt, {"version": 1, "generation": generation, "request_id": request_id_for("session", key), "consumed": False})
            if log_path.is_file():
                archived = log_path.with_name(f"{log_path.stem}.generation-{generation - 1}{log_path.suffix}")
                log_path.replace(archived)
            request = self.submit("session", key, payload, coordinator, initial={"consumed_at": None})
            return request, receipt

    def outstanding_sessions(self, wp_id: str) -> list[dict]:
        """The WP's native sessions no runner has finished, oldest first, read from the request store
        (a request made before receipts were written first can lack one): every non-worker session
        not consumed, and every worker session not terminal (today's worker barrier stops those).
        A request with `consumed_at` is consumed exactly when it is set. One from before that field
        is consumed when its receipt is, or names a later request; without a receipt it counts only
        while it is not terminal: finishing a long-ended one now would scope-check it against a
        checkout that has moved on since."""
        found = []
        for path in (self.directory / "requests").glob("*.json"):
            request = read_json(path)
            if not request or request.get("kind") != "session" or request["payload"].get("wp") != wp_id:
                continue
            if request["payload"].get("role") == "worker":
                if request["state"] not in TERMINAL_STATES:
                    found.append(request)
                continue
            if "consumed_at" in request:
                if not request["consumed_at"]:
                    found.append(request)
                continue
            log_path = request["payload"].get("log_path")
            receipt = read_json(Path(log_path).with_name(Path(log_path).name + ".t3-invocation.json")) if log_path else None
            if receipt is None:
                if request["state"] not in TERMINAL_STATES:
                    found.append(request)
            elif receipt["request_id"] == request["id"] and not receipt["consumed"]:
                found.append(request)
        return sorted(found, key=lambda request: request["created_at"])

    def cancel_request(self, request: dict, why: str, origin: str) -> dict:
        """Asks for a request to be cancelled; `origin` is one of CANCEL_ORIGIN_RANK. The origin is
        recorded on the request itself, under its lock (cancel requests keep their first payload,
        so a later, stronger origin could not be carried there). A request not launched yet stays
        pending with `cancel_requested`: only the coordinator can say whether it launched it, so it
        is settled by the coordinator's completion (or by its launch callback, which then queues
        the native cancel), never locally."""
        if origin not in CANCEL_ORIGIN_RANK:
            raise ValueError(f"unknown cancel origin {origin!r}")
        path = self.request_path(request["id"])
        with FileLock(path.with_suffix(".lock")):
            current = read_json(path)
            if current["state"] in TERMINAL_STATES:
                return current
            if CANCEL_ORIGIN_RANK[origin] > CANCEL_ORIGIN_RANK.get(current.get("cancel_origin"), -1):
                current["cancel_origin"] = origin
            if not current.get("task_id"):
                current.update(cancel_requested=True, cancel_detail=why)
            write_json(path, current)
        if current.get("task_id"):
            return self.submit("cancel", current["id"], {"task_id": current["task_id"], "reason": why,
                                                       "original_request_id": current["id"]}, current["coordinator_thread_id"])
        self.wake(current["coordinator_thread_id"], created_after=time.time())
        return current

    def cancel_wp(self, wp_id: str, why: str, origin: str, role: str | None = None) -> None:
        for path in (self.directory / "requests").glob("*.json"):
            request = read_json(path)
            if request["state"] in ("done", "failed", "cancelled"):
                continue
            if request["payload"].get("wp") == wp_id and (role is None or request["payload"].get("role") == role):
                self.cancel_request(request, why, origin)

    def recover(self, coordinator: str | None = None) -> int:
        requests = [read_json(path) for path in (self.directory / "requests").glob("*.json")]
        outstanding = [request for request in requests if request["state"] not in ("done", "failed", "cancelled")
                       and (coordinator is None or request["coordinator_thread_id"] == coordinator)]
        for target in sorted({request["coordinator_thread_id"] for request in outstanding}):
            self.wake(target, force=True)
        return len(outstanding)


def add_cli(commands) -> None:
    parser = commands.add_parser("t3", help="native T3 coordinator bridge and durable callbacks")
    sub = parser.add_subparsers(dest="t3_command", required=True)
    attach = sub.add_parser("attach", help="attach this repository to an existing T3 coordinator; starts no lanes")
    attach.add_argument("--thread", required=True)
    attach.add_argument("--capabilities-file", type=Path, required=True)
    attach.add_argument("--workspace-file", type=Path, required=True, help="live t3_worktree_status result for this coordinator")
    sub.add_parser("status")
    pending = sub.add_parser("next", help="show native tool plans; never polls a running task")
    pending.add_argument("--coordinator", required=True)
    started = sub.add_parser("started", help="record native delegate_task response before ending the turn")
    started.add_argument("--request", required=True)
    started.add_argument("--response-file", type=Path, required=True)
    complete = sub.add_parser("complete", help="record a completed native result")
    complete.add_argument("--request", required=True)
    complete.add_argument("--result-file", type=Path, required=True)
    register = sub.add_parser("register-workspace", help="register the actual native assigned checkout")
    register.add_argument("--request", required=True)
    register.add_argument("--thread", required=True)
    register.add_argument("--tree", type=Path, default=Path.cwd())
    recover = sub.add_parser("recover", help="wake a coordinator for outstanding callbacks after restart")
    recover.add_argument("--coordinator")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("--request", required=True)
    cancel.add_argument("--reason", default="cancelled by the operator")
    event = sub.add_parser("event", help="record native activity/usage callbacks without polling")
    event.add_argument("--request", required=True)
    event.add_argument("--event-file", type=Path, required=True)
    heartbeat = sub.add_parser("heartbeat", help="record real native task progress")
    heartbeat.add_argument("--request", required=True)
    retry = sub.add_parser("retry-unlaunched", help="retry a stalled request only after verifying no native launch occurred")
    retry.add_argument("--request", required=True)


def unwrap_result(value: dict) -> dict:
    if value.get("isError"):
        raise T3Error("Native tool returned an error; record a failed completion with its message")
    if "structuredContent" in value:
        return value["structuredContent"]
    if "content" in value:
        text = next((item["text"] for item in value["content"] if item.get("type") == "text"), None)
        if text is not None:
            return json.loads(text)
    return value


def cli_command(context: Context, arguments) -> int:
    bridge = Bridge(context)
    command = arguments.t3_command
    if command == "attach":
        bridge.attach(arguments.thread, unwrap_result(read_json(arguments.capabilities_file)), unwrap_result(read_json(arguments.workspace_file)))
        print("Attached T3 coordinator; existing WP statuses, holds, and supervisors were not resumed.")
    elif command == "status":
        requests = [read_json(path) for path in (bridge.directory / "requests").glob("*.json")]
        print(json.dumps({"backend": context.machine.execution_mode, "coordinator_thread_id": bridge.coordinator,
                          "capabilities_recorded": (bridge.directory / "capabilities.json").is_file(),
                          "requests": {state: sum(r["state"] == state for r in requests) for state in ("pending", "running", "waiting", "stalled", "done", "failed", "cancelled")},
                          "recovery": "wpo t3 recover wakes the coordinator once; running/stalled tasks are reconciled, never automatically relaunched"}, indent=2))
    elif command == "next":
        plans = []
        for request in bridge.pending(arguments.coordinator):
            entry = {"request": {key: request.get(key) for key in ("id", "kind", "state", "task_id", "thread_id", "link")}}
            if request["state"] in ("pending", "claimed") and request.get("cancel_requested"):
                result_file = bridge.directory / "requests" / f"{request['id']}.cancelled-before-launch"
                write_json(result_file, {"state": "cancelled", "detail": request.get("cancel_detail")})
                entry["cancelled_before_launch"] = (
                    f"Do not launch this request: it was cancelled ({request.get('cancel_origin')}: {request.get('cancel_detail')}). "
                    f"If you never launched it, record that with wpo --project \"{context.project.root.as_posix()}\" "
                    f"t3 complete --request {request['id']} --result-file \"{result_file.as_posix()}\". If you already launched it, "
                    "record the launch with t3 started as usual; WPO then queues its native cancel.")
            elif request["state"] in ("pending", "claimed"):
                if request["kind"] == "attention":
                    entry["attention"] = request["payload"]["event"]
                else:
                    entry["native_tool"] = bridge.tool_plan(request)
            else:
                entry["next"] = "Wait for native completion notification; do not relaunch or poll task_status. Record completion with wpo t3 complete."
            plans.append(entry)
        print(json.dumps(plans, indent=2))
    elif command == "started":
        response = unwrap_result(read_json(arguments.response_file))
        bridge.record_started(arguments.request, response)
        print("Native task recorded. Honor its returned next; completion wakes the coordinator.")
    elif command == "complete":
        result = read_json(arguments.result_file)
        completed = bridge.complete(arguments.request, result)
        if completed["kind"] == "cancel" and completed["state"] == "done":
            original = completed["payload"].get("original_request_id")
            if original:
                current = read_json(bridge.request_path(original))
                if current["state"] not in ("done", "failed", "cancelled"):
                    bridge.complete(original, {"state": "cancelled", "detail": completed["payload"].get("reason"), "task_id": current.get("task_id")})
        if completed["kind"] == "message" and completed["state"] == "done" and completed["payload"].get("message_path"):
            from .inbox import mark_delivered
            source = Path(completed["payload"]["message_path"])
            if source.is_file():
                mark_delivered(source)
        print(f"Recorded {completed['state']} for {arguments.request}")
    elif command == "register-workspace":
        print(json.dumps(bridge.register_workspace(arguments.request, arguments.thread, arguments.tree), indent=2))
    elif command == "recover":
        count = bridge.recover(arguments.coordinator)
        print(f"{count} outstanding requests; existing native task IDs are retained, no tasks relaunched.")
    elif command == "cancel":
        request = read_json(bridge.request_path(arguments.request))
        if request is None:
            raise T3Error("Unknown request")
        bridge.cancel_request(request, arguments.reason, "operator")
        print("Cancellation queued for the native coordinator.")
    elif command == "event":
        event = read_json(arguments.event_file)
        request = read_json(bridge.request_path(arguments.request))
        if request is None or request["kind"] != "session":
            raise T3Error("Activity callbacks require a session request")
        bridge.update(arguments.request, last_activity_at=time.time())
        if event.get("context_tokens") is not None:
            bridge.update(arguments.request, context_tokens=int(event["context_tokens"]))
        if event.get("compacted"):
            bridge.update(arguments.request, compacted=True)
        if "events" in event:
            with Path(request["payload"]["log_path"]).open("a", encoding="utf-8") as handle:
                for item in event["events"]:
                    handle.write(json.dumps(item) + "\n")
        print("Recorded native activity callback.")
    elif command == "heartbeat":
        bridge.update(arguments.request, last_activity_at=time.time())
        print("Recorded native progress.")
    elif command == "retry-unlaunched":
        request = read_json(bridge.request_path(arguments.request))
        if request is None or request["state"] != "stalled" or request.get("task_id"):
            raise T3Error("Only a stalled request with no recorded native task can be retried; reconcile existing task IDs through native completion")
        bridge.update(arguments.request, state="pending", created_at=time.time(), detail=None)
        bridge.wake(request["coordinator_thread_id"], force=True)
        print("Retried the unlaunched request using the same native clientRequestId.")
    return 0


def run_attempt(context: Context, route: dict, prompt: str, log_path: Path, settings, resume_session=None):
    if settings.access != "builder":
        key = hashlib.sha256(str(settings.cwd.resolve()).encode()).hexdigest()[:16]
        with NamedMutex(context.project.mutex_name("t3-scope", key)):
            bridge = Bridge(context)
            lease = read_json(bridge.directory / "scope" / f"{key}.json")
            if lease and lease["log_path"] != str(log_path):
                previous = read_json(bridge.request_path(lease["request_id"]))
                if previous and previous["state"] not in ("done", "failed", "cancelled"):
                    raise T3Error("A previous native scope-checked task has no terminal callback; reconcile it before starting another worker in this checkout")
            return _run_attempt(context, route, prompt, log_path, settings, resume_session)
    return _run_attempt(context, route, prompt, log_path, settings, resume_session)


def _run_attempt(context: Context, route: dict, prompt: str, log_path: Path, settings, resume_session=None):
    from .inbox import clear_live, write_live
    from .session_brief import loads_instructions_natively

    bridge = Bridge(context)
    bridge.ready()
    wp_id = settings.fields.get("wp") or settings.extra_env.get("WPO_WORK_PACKAGE")
    if wp_id and bridge.validate_workspace(wp_id).resolve() != settings.cwd.resolve():
        raise T3Error("The requested native session cwd does not match its registered WP checkout")
    if not wp_id:
        coordinator = read_json(bridge.directory / "coordinator.json", {})
        if Path(coordinator.get("checkout_path", str(context.project.root))).resolve() != settings.cwd.resolve():
            raise T3Error("A shared native ad-hoc task must run in its attached coordinator's actual checkout; shell cwd cannot rebind a T3 task")
    target = bridge.target(route)
    role = settings.fields.get("role", settings.access)
    identity = {**settings.extra_env, "WPO_ROLE": role, "WPO_ROUTE": route["id"], "WPO_TREE": str(settings.cwd)}
    native = loads_instructions_natively(context, route["family"])
    prompt_text = settings.brief.system_text(native=native) + "\n\n" + prompt.replace("{{ROUTE}}", route["id"])
    identity_key = hashlib.sha256(str(log_path).encode()).hexdigest()[:32]
    identity_path = bridge.directory / "sessions" / f"{identity_key}.json"
    write_json(identity_path, {"version": 1, "env": identity, "project": str(context.project.root)})
    prefix = f'wpo --project "{context.project.root.as_posix()}" --session {identity_key}'
    prompt_text = with_session_prefix(prompt_text, prefix)
    if resume_session:
        prompt_text += (f"\n\nContinuation of recorded WPO session {resume_session}. This is a fresh native task; "
                        "read the WP progress, handoff, and current diff to continue the same round. Do not assume provider context was resumed.")
    prompt_text += (f"\n\nNative T3 session. Work ONLY in the assigned checkout {settings.cwd.as_posix()}. "
                    f"Use the exact {prefix!r} prefix for all WPO commands so identity does not depend on inherited environment variables. "
                    "T3 enforces its own permissions; the WPO verify/ownership/findings gates remain required. "
                    "Do not create worktrees, launch standalone model CLIs, commit, change branches, merge, or remove this checkout. "
                    "A lead must stop writing while its delegated workers run. WPO serializes native workers within this checkout so scope checks can identify their mutations.")
    allowed = list(settings.write_paths)
    if settings.access == "builder" and wp_id:
        wp = context.wp_in_tree(wp_id, settings.cwd)
        allowed = list(dict.fromkeys([*allowed, *wp.owns, f"{context.project.wp_dir_relative}/{wp.file_name}",
                                     *(entry["path"] for entry in context.project.config["shared_files"])]))
    prompt_text += (f"\n\nWPO access contract: access={settings.access}; writable repository paths={json.dumps(allowed)}. "
                    "read/reviewer access permits NO file edits; evaluator/docs/worker access permits ONLY the listed paths. "
                    f"Additional configured session directories={json.dumps(context.project.config['session_dirs'])}; "
                    f"additional read directories={json.dumps(context.project.config['agy_read_dirs'])}. "
                    "T3's provider permissions are separate: this contract is checked against repository mutations after completion, not a substitute OS sandbox.")
    helpers = context.project.config.get("session_workers") if settings.lead else None
    if helpers:
        from .routing import AvailabilityOracle
        helper_model = helpers["model"]
        helper_family = "gpt" if helper_model.startswith("gpt-") or helper_model in ("sol", "terra", "luna", "astra") else "claude"
        try:
            helper_target = bridge.target({"family": helper_family, "model": helper_model, "effort": helpers["effort"]})
            availability = AvailabilityOracle(context.router).family(helper_family)
            if not availability.available:
                raise T3Error(availability.reason)
            prompt_text += (f"\n\nFor the project's optional small in-session helpers, use a native delegate_task with shared isolation and target "
                            f"{json.dumps(helper_target)}. Give each helper exact paths/commands and collect every result. "
                            "These helpers remain part of the lead's ownership/verification responsibility; do not use legacy CLI agent definitions.")
        except T3Error as error:
            prompt_text += f"\n\nConfigured in-session helper is unavailable ({error}); complete those small pieces yourself rather than substituting a model."
    elif settings.lead:
        prompt_text += "\n\nOptional in-session helpers are disabled by project configuration. Use only the explicitly configured WPO worker interface."
    before_tree = None
    if settings.access != "builder":
        from .commands import worktree_tree_id
        before_tree = worktree_tree_id(settings.cwd)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.with_name(log_path.name + ".prompt.md").write_text(prompt_text, encoding="utf-8")
    request, receipt = bridge.begin_session(log_path, {"wp": wp_id, "role": role, "title": f"{wp_id or context.project.name}: {role}",
                            "native_role": {"reviewer": "review", "read": "research", "docs": "research", "evaluator": "general", "worker": "implementation", "builder": "implementation"}[settings.access],
                            "prompt": prompt_text, "target": target, "route": route, "log_path": str(log_path),
                            "cwd": str(settings.cwd), "access": settings.access, "write_paths": allowed, "before_tree": before_tree,
                            "resume_session": resume_session, "stop_on_compaction": settings.stop_on_compaction,
                            "idle_minutes": settings.idle_minutes, "heartbeat_file": str(log_path.parent / "heartbeat"),
                            "progress_file": str(context.wp_in_tree(wp_id, settings.cwd).path) if wp_id else None},
                            bridge.coordinator_for(wp_id))
    write_json(identity_path, {"version": 1, "env": {**identity, "WPO_T3_REQUEST_ID": request["id"]}, "project": str(context.project.root)})
    if settings.access != "builder":
        key = hashlib.sha256(str(settings.cwd.resolve()).encode()).hexdigest()[:16]
        write_json(bridge.directory / "scope" / f"{key}.json", {"request_id": request["id"], "log_path": str(log_path)})
    if not log_path.exists():
        log_path.write_text(json.dumps({"type": "system", "subtype": "init", "session_id": request["id"], "backend": "t3",
                                       "t3": {"request_id": request["id"], "target": target}}) + "\n", encoding="utf-8")
    if settings.inbox:
        write_live(settings.inbox, log_path, os.getpid())
    controls = SessionControls(context, bridge, request, route, settings.stop_on_compaction, settings.fields,
                               inbox=settings.inbox, handoff_message=settings.handoff_message)
    try:
        bridge.wait(request, controls)
    except T3Error as error:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "result", "is_error": True, "result": str(error), "backend": "t3", "recoverable_request_id": request["id"]}) + "\n")
        if wp_id:
            from . import wpfile
            wpfile.set_status(context.wp_in_tree(wp_id, settings.cwd), "blocked")
            context.journal.emit("wp-blocked", str(error), {"wp": wp_id})
        raise
    finally:
        controls.close()
        if settings.inbox:
            clear_live(settings.inbox)
    return finish_attempt(context, request, settings.on_finished, handoff_reason=controls.handoff_reason)


def with_session_prefix(text: str, prefix: str) -> str:
    """Gives every wpo command in a session's prompt the session's identity: `wpo` at a token
    boundary (the start of a line, whitespace, a backtick, a quote or an opening parenthesis before
    it) followed by one of wpo's subcommands becomes `prefix`. Prose ("wpo commits", "in a wpo
    session", "wpo-orchestration") is left alone, and text already prefixed is not prefixed again.
    A diff that holds a literal `wpo verify` command line is still rewritten; that matters only when
    reviewing wpo itself, and is harmless there."""
    from .cli import command_names
    names = "|".join(re.escape(name) for name in sorted(command_names(), key=len, reverse=True))
    return re.sub(rf"(?<![^\s`'\"(])wpo (?=(?:{names})(?![\w-]))", lambda _match: prefix + " ", text)


class SessionControls:
    """What runs while a native session is waited on: the orchestrator's inbox messages, the staged
    compaction cancel, the quota cut-off and the handoff request. A recovered session (PR 3b) gets
    the compaction cancel and the cut-off; inbox delivery and handoff need a live builder."""

    def __init__(self, context: Context, bridge: Bridge, request: dict, route: dict, stop_on_compaction: bool, fields: dict,
                 inbox: Path | None = None, handoff_message=None) -> None:
        from .session import QuotaHandoffSlot
        self.context, self.bridge, self.request, self.route = context, bridge, request, route
        self.stop_on_compaction, self.fields, self.inbox, self.handoff_message = stop_on_compaction, fields, inbox, handoff_message
        self.launched_at = time.time()
        self.next_quota_check = 0.0
        self.handoff_reason: str | None = None
        self.cancellation_sent = False
        self.quota_slot = QuotaHandoffSlot(context.router.state_dir, route["family"], request["payload"]["log_path"])

    def close(self) -> None:
        self.quota_slot.release()

    def __call__(self, current: dict) -> None:
        from .inbox import pending_messages
        bridge, context, route = self.bridge, self.context, self.route
        if self.inbox and current.get("thread_id"):
            for message in pending_messages(self.inbox):
                bridge.submit("message", message.name, {"thread_id": current["thread_id"], "message": message.read_text(encoding="utf-8"), "message_path": str(message)},
                              current["coordinator_thread_id"])
        if current.get("task_id") and not self.cancellation_sent and current.get("compacted") and self.stop_on_compaction:
            bridge.cancel_request(current, "native compaction callback reached staged context boundary", "compaction")
            self.cancellation_sent = True
        if current.get("thread_id") and time.time() >= self.next_quota_check and not self.cancellation_sent:
            from .harness import handoff_limit
            from .session import quota_running_out
            self.next_quota_check = time.time() + 60
            if cut_off := context.router.family_cut_off(route["family"], self.launched_at):
                bridge.cancel_request(current, cut_off, "cutoff")
                self.cancellation_sent = True
                context.journal.emit("session-cut-off", f"T3 cancellation requested: {cut_off}", self.fields)
            elif self.handoff_message is not None and self.handoff_reason is None:
                quota_reason = quota_running_out(context, route)
                self.handoff_reason = quota_reason if quota_reason and self.quota_slot.try_claim() else None
                if current.get("context_tokens", 0) >= handoff_limit(context.project.config, route["harness"]):
                    self.handoff_reason = "native callback reached WPO context handoff threshold"
                if self.handoff_reason:
                    bridge.submit("message", self.request["id"] + ":handoff", {"thread_id": current["thread_id"],
                                  "message": self.handoff_message(self.handoff_reason)}, current["coordinator_thread_id"])
                    context.journal.emit("handoff-requested", f"T3 handoff requested: {self.handoff_reason}", self.fields)


def finish_attempt(context: Context, request: dict, on_finished=None, recovery: bool = False, handoff_reason: str | None = None):
    """Completes a terminal native session: the one path for a live attempt and for recovery.

    Under the request's finish lock: `on_finished(route)` (the caller's writer recording, so it is
    persisted before anything can fail, stop or consume), the scope check, the disposition (first
    match wins: scope_violation, cancelled_by_operator, compacted, cancelled_by_cleanup, else the
    outcome), the session log's result (once per request), then the disposition and `consumed_at`
    on the request and the receipt marked consumed, the last writes. Every step before them is
    idempotent, so a finish replayed after a crash gives the same result; a request already consumed
    only returns what was recorded. Live, it raises or returns per the disposition (T3Error,
    SessionCancelled, CompactionStop or the SessionOutcome); with `recovery` it returns the
    disposition and raises nothing."""
    from .routing import SessionOutcome
    from .session import CompactionStop, SessionCancelled
    bridge = Bridge(context)
    path = bridge.request_path(request["id"])
    with FileLock(path.with_name(f"{path.stem}.finish.lock"), timeout_seconds=600):
        completed = read_json(path)
        payload = completed["payload"]
        route, log_path, wp_id = payload["route"], Path(payload["log_path"]), payload.get("wp")
        result = completed.get("result") or {}
        text = result.get("text", "")
        outcome = result.get("outcome") or ("ok" if completed["state"] == "done" else "session_error")
        if completed.get("consumed_at"):
            disposition, violation = completed.get("disposition") or outcome, completed.get("violation")
        else:
            if on_finished is not None:
                on_finished(route)
            violation = scope_violation(context, payload)
            if violation and wp_id:
                from . import wpfile
                wp = context.wp_in_tree(wp_id, Path(payload["cwd"]))
                if wp.status != "blocked":
                    wpfile.set_status(wp, "blocked")
                    context.journal.emit("wp-blocked", violation, {"wp": wp_id})
            if violation:
                disposition = "scope_violation"
            elif completed.get("cancel_origin") == "operator":
                disposition = "cancelled_by_operator"
            elif (outcome == "compacted" or completed.get("compacted")) and payload.get("stop_on_compaction"):
                disposition = "compacted"
            elif completed.get("cancel_origin") == "cleanup":
                disposition = "cancelled_by_cleanup"
            else:
                disposition = outcome
            logged_outcome = "scope_violation" if violation else outcome
            if not session_result_logged(log_path, completed["id"]):
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
                                             "backend": "t3", "t3": {"task_id": completed.get("task_id"), "thread_id": completed.get("thread_id"), "link": completed.get("link")}}) + "\n")
                    handle.write(json.dumps({"type": "result", "subtype": "success" if logged_outcome == "ok" else "error", "is_error": logged_outcome != "ok",
                                             "result": text + ("\n\n" + violation if violation else ""), "session_id": completed["id"], "backend": "t3",
                                             "usage_available": bool(result.get("usage")), **({"usage": result["usage"]} if result.get("usage") else {})}) + "\n")
            with FileLock(path.with_suffix(".lock")):
                current = read_json(path)
                current.update(disposition=disposition, violation=violation, consumed_at=time.time())
                write_json(path, current)
            receipt = log_path.with_name(log_path.name + ".t3-invocation.json")
            with FileLock(receipt.with_suffix(".lock")):
                recorded = read_json(receipt)
                if recorded is not None and recorded["request_id"] == completed["id"] and not recorded["consumed"]:
                    write_json(receipt, {**recorded, "consumed": True})
    if recovery:
        return disposition
    detail = completed.get("cancel_detail") or completed.get("detail") or ""
    if disposition == "scope_violation":
        raise T3Error(violation)
    if disposition == "cancelled_by_operator":
        raise SessionCancelled(route["id"], "operator", detail)
    if disposition == "compacted":
        raise CompactionStop(route["id"], log_path)
    if disposition == "cancelled_by_cleanup":
        raise SessionCancelled(route["id"], "cleanup", detail)
    return SessionOutcome(outcome, result.get("detail") or completed.get("detail") or "", text,
                          handoff_reason=result.get("handoff_reason") or handoff_reason)


def scope_violation(context: Context, payload: dict) -> str | None:
    """What a read-only or limited-access session changed outside its access contract, if anything."""
    if not payload.get("before_tree"):
        return None
    from .commands import worktree_tree_id
    from .project import git
    from .wpfile import glob_to_regex
    cwd = Path(payload["cwd"])
    changed = git(cwd, "diff", "--name-only", "--no-renames", "-z", payload["before_tree"], worktree_tree_id(cwd)).stdout.split("\0")
    patterns = [] if payload["access"] in ("read", "reviewer") else payload["write_paths"]
    outside = [path for path in filter(None, changed) if not any(glob_to_regex(pattern).match(path) for pattern in patterns)]
    if not outside:
        return None
    return (f"Native {payload['access']} task changed paths outside its access contract: {', '.join(outside)}. "
            "Changes are preserved for manual review; no automatic rollback or fallback is safe.")


def session_result_logged(log_path: Path, request_id: str) -> bool:
    """Whether the session log already holds this request's result (a finish replayed after a crash)."""
    if not log_path.is_file():
        return False
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("{") and '"type": "result"' in line:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") == "result" and record.get("session_id") == request_id:
                return True
    return False
