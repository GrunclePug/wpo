"""A lead's delegated workers: admission, the worker barrier, and what it checks and reports."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wpo import session, wpfile
from wpo.commands import worktree_tree_id
from wpo.context import Context
from wpo.delegate import DelegateError, Delegation
from wpo.events import Journal
from wpo.machine import DEFAULTS as MACHINE_DEFAULTS
from wpo.machine import MachineConfig
from wpo.project import DEFAULTS as PROJECT_DEFAULTS
from wpo.project import Project, git
from wpo.routing import Router
from wpo.runner import (
    WorkPackageRun,
    delegation_unsettled,
    gate_path,
    open_delegation,
    settle_native_sessions,
    stop_delegated_workers,
)
from wpo.session import SessionCancelled
from wpo.session_brief import SessionBrief
from wpo.t3 import (
    Bridge,
    T3Error,
    finish_attempt,
    read_json,
    run_attempt,
    scope_key,
    write_json,
)

CAPABILITIES = {"parentThreadId": "coordinator", "providers": [{"providerInstanceId": "codex", "canRunChildTask": True, "models": [
    {"id": "gpt-6.1-sol", "options": [{"id": "reasoningEffort", "values": ["low", "high"]}]}]}]}
ROUTE = {"id": "gpt-sol-high", "family": "gpt", "harness": "gpt", "model": "sol", "effort": "high", "cost_class": "subscription"}
WP_TEXT = ("---\nid: WP-0001\ntitle: x\nmilestone: M1\nstatus: in_progress\ndepends_on: []\nbuilder: code\nreviewer: review\n"
           "owns: [src/**]\n---\n\n## Progress log\n")


def finish_wait(bridge, request, callback=None) -> dict:
    """A `Bridge.wait` whose native task then ends (cancelled, as the barrier asked), through `complete`."""
    current = read_json(bridge.request_path(request["id"]))
    if current["state"] in ("done", "failed", "cancelled"):
        return current
    return bridge.complete(request["id"], {"state": "cancelled", "task_id": current.get("task_id")})


class DelegationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="wpo-delegate-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.project = Project(self.root, {**PROJECT_DEFAULTS, "name": "scratch", "autofix": {}}, "t3")
        self.machine = MachineConfig({**MACHINE_DEFAULTS, "state_dir": str(self.root / "state"), "dashboard": {"autostart": False},
                                      "t3": {**MACHINE_DEFAULTS["t3"], "cli": sys.executable, "callback_interval_seconds": 0.001}})
        self.context = Context(self.project, self.machine, Router(self.machine, project=self.project), Journal(self.project.runs_dir))
        self.bridge = Bridge(self.context)
        self.bridge.attach("coordinator", CAPABILITIES)
        for arguments in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            git(self.root, *arguments)
        (self.root / ".gitignore").write_text(".orchestration/\nstate/\n")
        (self.root / "src").mkdir()
        (self.root / "src" / "a.txt").write_text("a\n")
        self.wp_path = self.project.wp_dir(self.root) / "WP-0001-x.md"
        self.wp_path.parent.mkdir(parents=True)
        self.wp_path.write_text(WP_TEXT, encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")
        environment = mock.patch.dict(os.environ, {"WPO_WORK_PACKAGE": "WP-0001", "WPO_TREE": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.delegation = Delegation(self.context)

    @property
    def wp(self) -> wpfile.WorkPackage:
        return wpfile.parse(self.wp_path)

    def events(self, kind: str) -> list[dict]:
        return [event for event in self.context.journal.read(0) if event.get("kind") == kind]

    def worker(self, name: str = "piece", write_paths=("src/a.txt",), state: str = "running") -> dict:
        """A worker's native request begun in its task directory, as `run_attempt` publishes it."""
        log = self.delegation.directory / name / f"session.{ROUTE['id']}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        payload = {"wp": "WP-0001", "role": "worker", "prompt": "Task", "title": name, "native_role": "implementation", "target": self.bridge.target(ROUTE),
                   "route": ROUTE, "log_path": str(log), "cwd": str(self.root), "access": "worker", "write_paths": list(write_paths),
                   "before_tree": worktree_tree_id(self.root), "stop_on_compaction": False}
        with mock.patch.object(Bridge, "wake"):
            request, _ = self.bridge.begin_session(log, payload, "coordinator")
        return self.bridge.update(request["id"], state=state, task_id=f"task-{name}")

    def lead_session(self, worker: dict) -> dict:
        """The lead's own builder session, pending, in the same checkout as `worker`."""
        log = self.project.runs_dir / "WP-0001" / "builder.log"
        payload = {**worker["payload"], "role": "builder", "access": "builder", "log_path": str(log), "before_tree": None, "write_paths": []}
        with mock.patch.object(Bridge, "wake"):
            return self.bridge.begin_session(log, payload, "coordinator")[0]

    def complete(self, request: dict) -> dict:
        return self.bridge.complete(request["id"], {"state": "done", "text": "done", "task_id": request["task_id"]})

    def record(self, name: str = "piece", token: str | None = "T1", pid: int | None = 4242, **fields) -> Path:
        task_dir = self.delegation.directory / name
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.md").write_text("Do it", encoding="utf-8")
        Delegation.write_state(task_dir, {"state": "running", "tier": "code", "write_paths": ["src/a.txt"], "route": None, "detail": None,
                                          "worker_pid": pid, "token": token, **fields})
        return task_dir

    def barrier(self, wait=None, alive=(), call=None, **kwargs) -> list[str]:
        """The barrier (or `call()`) with local processes faked: `alive` pids pass the identity check
        until they are killed."""
        live = set(alive)
        with mock.patch("wpo.runner.python_process_runs", side_effect=lambda pid, token: pid in live), \
                mock.patch("wpo.runner.pid_alive", side_effect=lambda pid, names: pid in live), \
                mock.patch("wpo.runner.kill_tree", side_effect=lambda pid: live.discard(pid)) as kill, \
                mock.patch.object(Bridge, "wake"), mock.patch.object(Bridge, "wait", wait or finish_wait):
            try:
                return call() if call else stop_delegated_workers(self.context, self.wp, **kwargs)
            finally:
                self.killed = [killed.args[0] for killed in kill.call_args_list]

    def lead(self, in_flight=None):
        """`WorkPackageRun.stop_workers`, the lead's own barrier in `session()`."""
        runner = SimpleNamespace(context=self.context, wp=lambda: self.wp, wp_id="WP-0001", emit=lambda *a: None)
        return lambda: WorkPackageRun.stop_workers(runner, in_flight)

    # Admission

    def admission(self, name: str = "piece", token: str = "T1"):
        """The admission `run_worker` passes to its session, or None when the worker never got that far."""
        captured = {}

        def run(*arguments, **keywords):
            captured.update(keywords)
            raise SystemExit  # stop the worker here, its record still running
        with mock.patch("wpo.delegate.run_session", side_effect=run):
            try:
                self.delegation.run_worker(name, token)
            except SystemExit:
                pass
        return captured.get("admission")

    def test_a_worker_publishing_before_the_barrier_is_found_and_finished(self) -> None:
        open_delegation(self.context, "WP-0001")
        self.record(pid=None)
        admission = self.admission()
        with admission(ROUTE):
            request = self.worker()
        (self.root / "src" / "a.txt").write_text("the worker's edit\n")
        self.assertEqual(self.barrier(), [])
        stored = read_json(self.bridge.request_path(request["id"]))
        self.assertEqual((stored["state"], stored["disposition"]), ("cancelled", "cancelled_by_cleanup"))
        self.assertIsNotNone(stored["consumed_at"])

    def test_a_worker_reaching_admission_after_the_barrier_publishes_nothing(self) -> None:
        open_delegation(self.context, "WP-0001")
        self.record(pid=None)
        admission = self.admission()
        self.barrier()
        requests = sorted((self.bridge.directory / "requests").glob("*.json"))
        with self.assertRaises(SessionCancelled) as raised, admission(ROUTE):
            self.fail("admitted after the barrier")
        self.assertEqual(raised.exception.origin, "cleanup")
        self.assertEqual(sorted((self.bridge.directory / "requests").glob("*.json")), requests)

    def test_admission_is_checked_where_the_request_is_published(self) -> None:
        open_delegation(self.context, "WP-0001")
        self.record(pid=None)
        admission = self.admission()
        self.barrier()
        settings = session.SessionSettings("worker", ["src/a.txt"], self.root, SessionBrief(None, "", "rules"), {}, False, False, 30,
                                           {"role": "worker"}, admission=admission)
        with mock.patch.object(Bridge, "wake"), self.assertRaises(SessionCancelled):
            run_attempt(self.context, ROUTE, "Task", self.project.runs_dir / "refused.log", settings)
        self.assertFalse(list((self.bridge.directory / "requests").glob("*.json")))

    # Spawning and reusing a name

    def start(self, name: str = "piece") -> None:
        self.spawned = None
        with mock.patch("wpo.delegate.spawn_detached", return_value=4242) as spawn, mock.patch.object(self.context.router, "select"):
            self.delegation.start(name, "Do it", None, ["src/a.txt"], "code")
        self.spawned = spawn.call_args

    def test_a_worker_without_its_state_or_with_another_token_never_runs(self) -> None:
        self.assertIsNone(self.admission("missing"), "the launcher died before writing the state")
        task_dir = self.record(token="T2", pid=None)
        before = (task_dir / "state.json").read_bytes()
        self.assertIsNone(self.admission(token="T1"), "a delayed child of the name's earlier run")
        self.assertEqual((task_dir / "state.json").read_bytes(), before)

    def test_start_is_refused_once_the_builder_session_ended(self) -> None:
        with self.assertRaisesRegex(DelegateError, "delegation is closed"):
            self.start()
        self.assertIsNone(self.spawned)
        open_delegation(self.context, "WP-0001")
        self.start()
        state = Delegation.read_state(self.delegation.directory / "piece")
        self.assertIn(state["token"], self.spawned.args[0])
        self.assertEqual((self.delegation.directory / "piece" / "spawn.pid").read_text(), "4242")

    def test_a_reused_name_runs_a_new_generation(self) -> None:
        open_delegation(self.context, "WP-0001")
        first = self.worker()
        self.complete(first)
        finish_attempt(self.context, first, recovery=True)
        self.record(state="done")
        self.start()
        second = self.worker()
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["state"], "running")
        self.assertEqual(read_json(Path(second["payload"]["log_path"] + ".t3-invocation.json"))["generation"], 2)

    def test_reuse_rechecks_for_a_session_published_while_it_waited_for_the_lock(self) -> None:
        open_delegation(self.context, "WP-0001")
        self.record(state="failed")
        real = Delegation.uncollected_session

        def publishing_meanwhile(delegation, task_dir, retire=False):
            if retire:
                self.worker()  # the name's earlier worker publishes just before `start` takes the lock
            return real(delegation, task_dir, retire)
        with mock.patch.object(Delegation, "uncollected_session", publishing_meanwhile), \
                self.assertRaisesRegex(DelegateError, "native session to collect"):
            self.start()

    def test_reuse_waits_for_an_uncollected_session_but_not_a_refused_one(self) -> None:
        open_delegation(self.context, "WP-0001")
        request = self.worker()
        self.record(state="failed")
        with self.assertRaisesRegex(DelegateError, "native session to collect"):
            self.start()
        receipt = Path(request["payload"]["log_path"] + ".t3-invocation.json")
        write_json(receipt, {**read_json(receipt), "request_id": "0" * 32})  # refused at admission: never published
        self.start()

    def test_a_legacy_receipt_is_retired_so_the_name_runs_again(self) -> None:
        open_delegation(self.context, "WP-0001")
        request = self.worker()
        self.bridge.update(request["id"], state="cancelled")  # stopped by the old barrier: no completion snapshot
        self.record(state="failed")
        self.start()
        receipt = read_json(Path(request["payload"]["log_path"] + ".t3-invocation.json"))
        self.assertEqual((receipt["consumed"], receipt["retired"]), (True, "legacy"))
        self.assertNotEqual(self.worker()["id"], request["id"])

    # What the barrier checks

    def test_a_stopped_workers_edit_outside_its_task_blocks_the_wp(self) -> None:
        open_delegation(self.context, "WP-0001")
        self.record(pid=4242)
        request = self.worker()
        (self.root / "src" / "b.txt").write_text("inside the WP's owns, outside the task\n")
        with self.assertRaisesRegex(T3Error, "outside their task"):
            self.barrier(alive=[4242], call=self.lead())
        self.assertEqual(self.wp.status, "blocked")
        self.assertEqual(self.killed, [4242])
        self.assertEqual(read_json(self.bridge.request_path(request["id"]))["disposition"], "scope_violation")
        self.assertEqual(self.barrier(), [], "reported once: the build's second pass finds nothing")

    def test_a_violation_the_worker_finished_itself_is_reported_and_reblocks(self) -> None:
        open_delegation(self.context, "WP-0001")
        request = self.worker()
        (self.root / "outside.txt").write_text("x\n")
        self.complete(request)
        self.assertEqual(finish_attempt(self.context, request, recovery=True), "scope_violation")
        wpfile.set_status(self.wp, "in_progress")  # the lead edited it away
        self.assertEqual(self.barrier(), [request["id"]])
        self.assertEqual(self.wp.status, "blocked")
        log = Path(request["payload"]["log_path"]).read_text()
        self.assertEqual(log.count('"type": "result"'), 1)

    def test_writes_after_a_clean_worker_completed_are_not_its_edits(self) -> None:
        open_delegation(self.context, "WP-0001")
        self.record(pid=4242)
        request = self.worker()
        (self.root / "src" / "a.txt").write_text("the worker's edit\n")
        self.complete(request)
        wpfile.log_progress(self.wp, "the runner's note", "runner")
        self.assertEqual(self.barrier(alive=[4242]), [])
        self.assertIn("stopped worker 'piece'", self.wp_path.read_text())
        self.assertEqual(read_json(self.bridge.request_path(request["id"]))["disposition"], "ok")

    def test_a_worker_editing_the_wp_file_before_completion_is_a_violation(self) -> None:
        open_delegation(self.context, "WP-0001")
        request = self.worker()
        wpfile.log_progress(self.wp, "a worker's own note")
        self.complete(request)
        self.assertEqual(self.barrier(), [request["id"]])

    def test_a_legacy_worker_request_is_neither_finished_nor_reported(self) -> None:
        open_delegation(self.context, "WP-0001")
        request = self.worker()
        self.bridge.update(request["id"], state="cancelled")
        (self.root / "outside.txt").write_text("a later edit\n")
        self.assertEqual(self.barrier(), [])
        self.assertIsNone(read_json(self.bridge.request_path(request["id"]))["consumed_at"])
        self.assertEqual(self.wp.status, "in_progress")

    # Reporting survives crashes, stalls and a live lead

    def settle(self) -> list[str]:
        with mock.patch("wpo.runner.python_process_runs", return_value=False), mock.patch("wpo.runner.pid_alive", return_value=False), \
                mock.patch.object(Bridge, "wake"), mock.patch.object(Bridge, "wait", finish_wait):
            return settle_native_sessions(self.context, self.wp, lambda role, route: None)

    def test_a_barrier_crashing_before_its_report_is_reported_by_the_next_settle(self) -> None:
        open_delegation(self.context, "WP-0001")
        request = self.worker()
        (self.root / "outside.txt").write_text("x\n")
        with mock.patch("wpo.runner.finish_attempt", side_effect=RuntimeError("killed")), self.assertRaises(RuntimeError):
            self.barrier()
        self.assertTrue(delegation_unsettled(self.context, "WP-0001"))
        self.assertEqual(self.settle(), [request["id"]])
        self.assertEqual(self.wp.status, "blocked")
        self.assertFalse(delegation_unsettled(self.context, "WP-0001"))

    def test_nothing_is_written_while_a_session_of_the_wp_runs(self) -> None:
        open_delegation(self.context, "WP-0001")
        self.record(pid=4242)
        stalled, finished = self.worker("stalled"), self.worker("finished")
        self.complete(finished)

        def wait(bridge, request, callback=None):
            raise T3Error("marked stalled")
        before = self.wp_path.read_text()
        with self.assertRaisesRegex(T3Error, "marked stalled"):
            self.barrier(wait=wait, alive=[4242])
        self.assertEqual(self.wp_path.read_text(), before)
        self.assertIsNone(read_json(self.bridge.request_path(finished["id"]))["consumed_at"])
        self.assertEqual(len(self.events("delegate-worker-stopped")), 1)
        self.assertIsNotNone(read_json(gate_path(self.context, "WP-0001"))["report_since"])
        self.complete(stalled)
        self.assertEqual(self.settle(), [])
        for request in (stalled, finished):
            self.assertIsNotNone(read_json(self.bridge.request_path(request["id"]))["consumed_at"])
        self.assertNotIn("WP-0001-x.md", " ".join(read_json(self.bridge.request_path(finished["id"]))["completed_changes"]))

    def test_a_live_lead_holds_the_report_until_it_has_finished(self) -> None:
        open_delegation(self.context, "WP-0001")
        worker = self.worker()
        (self.root / "outside.txt").write_text("x\n")
        self.complete(worker)
        lead = self.lead_session(worker)
        self.assertEqual(self.barrier(wait=False), [])
        self.assertEqual(self.wp.status, "in_progress")
        self.bridge.complete(lead["id"], {"state": "done", "text": "done"})
        wpfile.set_status(self.wp, "review")
        self.assertEqual(self.settle(), [worker["id"]])
        self.assertEqual(self.wp.status, "blocked")

    # Killing local workers

    def test_a_kill_that_crashes_or_leaves_a_survivor_is_retried(self) -> None:
        task_dir = self.record(pid=4242)
        native = self.worker()
        with mock.patch("wpo.runner.python_process_runs", return_value=True), mock.patch("wpo.runner.kill_tree", side_effect=OSError("crash")), \
                mock.patch.object(Bridge, "wake"), mock.patch.object(Bridge, "wait", finish_wait), self.assertRaisesRegex(T3Error, "crash"):
            stop_delegated_workers(self.context, self.wp)
        self.assertEqual(read_json(self.bridge.request_path(native["id"]))["state"], "cancelled", "native cleanup goes on")
        state = Delegation.read_state(task_dir)
        self.assertEqual((state["state"], state["stop_requested"]), ("running", True))
        self.assertIsNone(self.admission(), "a stopped worker is not admitted")
        with mock.patch("wpo.runner.python_process_runs", return_value=True), mock.patch("wpo.runner.kill_tree"), \
                self.assertRaisesRegex(T3Error, "local worker cleanup of WP-0001 failed"):
            stop_delegated_workers(self.context, self.wp)
        self.assertEqual(Delegation.read_state(task_dir)["state"], "running")
        self.barrier(alive=[4242])
        self.assertEqual(Delegation.read_state(task_dir)["state"], "failed")

    def test_only_the_workers_own_process_is_killed(self) -> None:
        task_dir = self.record(pid=4242)
        self.barrier()  # pid 4242 runs something else now
        self.assertEqual(self.killed, [])
        self.assertEqual(Delegation.read_state(task_dir)["state"], "failed")
        self.assertNotIn("stopped worker", self.wp_path.read_text(), "no note for a worker already gone")

    def test_standalone_settle_stops_a_left_worker_without_the_bridge(self) -> None:
        self.machine.raw["execution_mode"] = "standalone"
        task_dir = self.record(pid=4242)
        with mock.patch.object(Bridge, "cancel_wp") as cancel, \
                mock.patch("wpo.runner.python_process_runs", side_effect=[True, False]), mock.patch("wpo.runner.kill_tree") as kill:
            self.assertEqual(settle_native_sessions(self.context, self.wp, lambda role, route: None), [])
        kill.assert_called_once_with(4242)
        cancel.assert_not_called()
        self.assertEqual(Delegation.read_state(task_dir)["state"], "failed")
        self.assertFalse(delegation_unsettled(self.context, "WP-0001"))

    # Stalls

    def stall(self, role: str) -> T3Error:
        settings = session.SessionSettings("worker", ["src/a.txt"], self.root, SessionBrief(None, "", "rules"), {}, False, False, 30,
                                           {"wp": "WP-0001", "role": role})
        with mock.patch.object(Bridge, "validate_workspace", return_value=self.root), mock.patch.object(Bridge, "wake"), \
                mock.patch.object(Bridge, "wait", side_effect=T3Error("marked stalled")), self.assertRaisesRegex(T3Error, "marked stalled") as raised:
            run_attempt(self.context, ROUTE, "Task", self.project.runs_dir / f"{role}.log", settings)
        lease = read_json(next((self.bridge.directory / "scope").glob("*.json")))
        self.bridge.update(lease["request_id"], state="done", consumed_at=time.time())  # reconciled, so the next task is admitted
        return raised.exception

    def test_a_workers_stall_leaves_the_wp_file_alone(self) -> None:
        error = self.stall("worker")
        self.assertEqual(error.stalled_wp, "WP-0001")
        self.assertEqual(self.wp.status, "in_progress")
        self.record(pid=None)
        with mock.patch("wpo.delegate.run_session", side_effect=error):
            self.delegation.run_worker("piece", "T1")
        self.assertEqual(len(self.events("delegate-worker-stalled")), 1)
        self.assertIn("marked stalled", Delegation.read_state(self.delegation.directory / "piece")["detail"])
        self.stall("repair")
        self.assertEqual(self.wp.status, "blocked", "a repair session's stall still blocks")

    def test_a_lead_stall_blocks_only_once_no_worker_runs(self) -> None:
        open_delegation(self.context, "WP-0001")
        error = T3Error("marked stalled")
        error.stalled_wp = "WP-0001"
        self.worker()

        def stuck(bridge, request, callback=None):
            raise T3Error("still running")
        with self.assertRaisesRegex(T3Error, "still running"):
            self.barrier(wait=stuck, call=self.lead(error))
        self.assertEqual(self.wp.status, "in_progress", "a worker still runs: no write it could take as its own")
        # The cancel's wake now fails, and the worker then ends: the barrier raises, the rescan blocks.
        with mock.patch.object(Bridge, "wait", finish_wait), mock.patch.object(Bridge, "wake", side_effect=T3Error("wake failed")), \
                mock.patch("wpo.runner.python_process_runs", return_value=False), self.assertRaisesRegex(T3Error, "wake failed"):
            self.lead(error)()
        self.assertEqual(self.wp.status, "blocked")

    def test_one_failed_wake_does_not_stop_the_other_cancels(self) -> None:
        requests = [self.worker(f"w{index}") for index in range(3)]
        calls = []

        def wake(*arguments, **keywords):
            calls.append(1)
            if len(calls) == 1:
                raise T3Error("coordinator gone")
        with mock.patch.object(Bridge, "wake", side_effect=wake), self.assertRaisesRegex(T3Error, "coordinator gone"):
            self.bridge.cancel_wp("WP-0001", "stop", "cleanup", role="worker")
        for request in requests:
            self.assertEqual(read_json(self.bridge.request_path(request["id"]))["cancel_origin"], "cleanup")

    def test_a_failed_snapshot_holds_the_checkout_until_it_is_finished(self) -> None:
        previous = self.worker()
        with mock.patch("wpo.commands.changed_since", side_effect=RuntimeError("git broke")):
            self.complete(previous)
        write_json(self.bridge.directory / "scope" / f"{scope_key(self.root)}.json", {"request_id": previous["id"], "log_path": previous["payload"]["log_path"]})
        settings = session.SessionSettings("read", [], self.root, SessionBrief(None, "", "rules"), {}, False, False, 30, {"role": "read"})
        with self.assertRaisesRegex(T3Error, "is unfinished"):
            run_attempt(self.context, ROUTE, "Read", self.project.runs_dir / "next.log", settings)
        finish_attempt(self.context, previous, recovery=True)
        with mock.patch.object(Bridge, "wake"), mock.patch.object(Bridge, "wait", lambda b, r, c=None: b.update(r["id"], state="done", result={"text": "ok"})):
            run_attempt(self.context, ROUTE, "Read", self.project.runs_dir / "next.log", settings)


    # Stopping a lane, and what still counts as unsettled

    def stop_lane(self, *wps, supervisor=None, wake=None):
        from wpo import lanes
        output = io.StringIO()
        with mock.patch.object(self.context, "effective_wps", return_value=list(wps)), \
                mock.patch.object(self.context, "merged_done_ids", return_value={wp.id for wp in wps if wp.status == "merged"}), \
                mock.patch("wpo.lanes.supervisor_pid", return_value=supervisor), mock.patch("wpo.lanes.kill_tree") as kill, \
                mock.patch("wpo.runner.python_process_runs", return_value=False), mock.patch("wpo.runner.stop_delegated_workers", wraps=stop_delegated_workers) as fence, \
                mock.patch.object(Bridge, "wake", side_effect=wake), mock.patch.object(Bridge, "wait", side_effect=AssertionError("lanes stop never waits")), \
                contextlib.redirect_stdout(output):
            lanes.stop(self.context, "alpha")
        return output.getvalue(), kill, [call.args[1].id for call in fence.call_args_list]

    def lane_wp(self, wp_id: str, status: str = "in_progress") -> SimpleNamespace:
        return SimpleNamespace(id=wp_id, lane="alpha", status=status, path=self.wp_path)

    def test_lanes_stop_fences_workers_without_a_supervisor(self) -> None:
        open_delegation(self.context, "WP-0001")
        task_dir = self.record(pid=4242)
        output, kill, fenced = self.stop_lane(self.lane_wp("WP-0001"), self.lane_wp("WP-0002", "done"), self.lane_wp("WP-0003", "merged"))
        self.assertIn("no live supervisor", output)
        kill.assert_not_called()
        self.assertEqual(fenced, ["WP-0001", "WP-0002"], "a WP done but unmerged is fenced; one merged into main is not")
        self.assertEqual(Delegation.read_state(task_dir)["state"], "failed")
        self.assertFalse(read_json(gate_path(self.context, "WP-0001"))["open"])

    def test_lanes_stop_survives_a_dead_coordinator_and_cancels_the_lead_first(self) -> None:
        open_delegation(self.context, "WP-0001")
        task_dir = self.record(pid=4242)
        worker = self.worker()
        lead = self.bridge.update(self.lead_session(worker)["id"], state="running", task_id="task-lead")
        marked_at_first_cancel = []
        real = Bridge.cancel_request

        def cancel(bridge, request, why, origin):
            marked_at_first_cancel.append(Delegation.read_state(task_dir).get("stop_requested"))
            return real(bridge, request, why, origin)
        with mock.patch.object(Bridge, "cancel_request", cancel):
            output, _, fenced = self.stop_lane(self.lane_wp("WP-0001"), supervisor=None, wake=T3Error("coordinator gone"))
        self.assertIsNone(marked_at_first_cancel[0], "the lead's cancel is queued before any worker is fenced")
        self.assertEqual(fenced, ["WP-0001"])
        self.assertIn("coordinator gone", output)
        self.assertIn("still to finish", output)
        for request in (lead, worker):
            self.assertEqual(read_json(self.bridge.request_path(request["id"]))["cancel_origin"], "operator")
        self.assertTrue(Delegation.read_state(task_dir)["stop_requested"])

    def test_a_violation_found_after_lanes_stop_is_reported_by_the_next_run(self) -> None:
        open_delegation(self.context, "WP-0001")
        worker = self.worker()
        (self.root / "outside.txt").write_text("x\n")
        self.complete(worker)
        before = self.wp_path.read_text()
        self.stop_lane(self.lane_wp("WP-0001"))
        self.assertEqual(self.wp_path.read_text(), before)
        self.assertIsNotNone(read_json(gate_path(self.context, "WP-0001"))["report_since"])
        self.assertEqual([request["id"] for request in self.bridge.outstanding_sessions("WP-0001")], [worker["id"]])
        self.assertEqual(self.settle(), [worker["id"]])
        self.assertEqual(self.wp.status, "blocked")

    def test_integration_and_the_supervisor_see_unsettled_workers(self) -> None:
        from wpo.integrate import Integration
        from wpo.supervisor import Supervisor
        supervisor = SimpleNamespace(context=self.context)
        self.assertEqual(Supervisor.surviving_sessions(supervisor, "WP-0001"), 0)
        open_delegation(self.context, "WP-0001")  # a lead session runs, or its runner died
        self.assertGreater(Supervisor.surviving_sessions(supervisor, "WP-0001"), 0)
        with self.assertRaisesRegex(RuntimeError, "delegated workers are not settled"):
            Integration(self.context, "WP-0001", False).run()
        self.barrier()
        self.record(pid=None)  # a worker record still running after its barrier
        self.assertGreater(Supervisor.surviving_sessions(supervisor, "WP-0001"), 0)
        with self.assertRaisesRegex(RuntimeError, "delegated workers are not settled"):
            Integration(self.context, "WP-0001", False).run()

if __name__ == "__main__":
    unittest.main()
