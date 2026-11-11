"""Native bridge behavior: durable callbacks, model settings, workspace mapping and legacy data."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wpo import dashboard, session
from wpo.context import Context
from wpo.events import Journal
from wpo.integrate import Integration
from wpo.machine import DEFAULTS as MACHINE_DEFAULTS
from wpo.machine import MachineConfig
from wpo.project import DEFAULTS as PROJECT_DEFAULTS
from wpo.project import Project, git
from wpo.routing import AvailabilityOracle, Route, Router
from wpo.session_brief import SessionBrief
from wpo.t3 import Bridge, T3Error, cli_command, read_json, run_attempt, write_json

CAPABILITIES = {
    "parentThreadId": "coordinator", "providers": [
        {"providerInstanceId": "codex", "canRunChildTask": True, "models": [
            {"id": "gpt-6.1-sol", "options": [{"id": "reasoningEffort", "values": ["low", "high"]}]}
        ]},
        {"providerInstanceId": "claudeAgent", "canRunChildTask": False, "models": []},
    ],
}
ROUTE = {"id": "gpt-sol-high", "family": "gpt", "harness": "gpt", "model": "sol", "effort": "high", "cost_class": "subscription"}


class T3BackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="wpo-native-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.project = Project(self.root, {**PROJECT_DEFAULTS, "name": "scratch", "autofix": {}}, "t3")
        self.machine = MachineConfig({**MACHINE_DEFAULTS, "state_dir": str(self.root / "state"), "dashboard": {"autostart": False},
                                      "t3": {**MACHINE_DEFAULTS["t3"], "cli": sys.executable, "callback_interval_seconds": 0.001}})
        self.context = Context(self.project, self.machine, Router(self.machine, project=self.project), Journal(self.project.runs_dir))
        self.bridge = Bridge(self.context)
        self.bridge.attach("coordinator", CAPABILITIES)

    def submit(self, kind="session", key="one", payload=None):
        payload = payload or {"wp": "WP-0001", "prompt": "Do the task", "title": "test", "native_role": "implementation", "target": self.bridge.target(ROUTE)}
        with mock.patch.object(Bridge, "wake"):
            return self.bridge.submit(kind, key, payload, "coordinator")

    def test_target_uses_exact_native_provider_model_and_effort_option(self) -> None:
        self.assertEqual(self.bridge.target(ROUTE), {"providerInstanceId": "codex", "model": "gpt-6.1-sol", "options": {"reasoningEffort": "high"}})
        with self.assertRaisesRegex(T3Error, "unavailable or disabled"):
            self.bridge.target({**ROUTE, "family": "claude"})
        with self.assertRaisesRegex(T3Error, "does not advertise effort"):
            self.bridge.target({**ROUTE, "effort": "max"})
        with self.assertRaisesRegex(T3Error, "absent from recorded"):
            self.bridge.target({**ROUTE, "model": "missing-model"})

    def test_explicit_legacy_aliases_resolve_but_disabled_candidates_are_skipped(self) -> None:
        capabilities = json.loads(json.dumps(CAPABILITIES))
        capabilities["providers"][1].update(canRunChildTask=True, models=[{"id": "claude-haiku-5-5", "options": [{"id": "effort", "values": ["high"]}]}])
        self.bridge.attach("coordinator", capabilities)
        target = self.bridge.target({**ROUTE, "family": "claude", "model": "haiku"})
        self.assertEqual(target["model"], "claude-haiku-5-5")
        self.assertEqual(target["options"], {"effort": "high"})
        unavailable = Route("local-private", "local", "local", "private-model", "high", 42, True, "local")
        self.assertFalse(AvailabilityOracle(self.context.router).route(unavailable).available)

    def test_attach_rejects_a_thread_from_another_actual_repository(self) -> None:
        with self.assertRaisesRegex(T3Error, "actual T3 project root"):
            self.bridge.attach("coordinator", CAPABILITIES, {"projectWorkspaceRoot": str(self.root / "another-repo"), "worktreePath": None})

    def test_attach_and_ready_fail_without_forging_provider_credentials(self) -> None:
        with self.assertRaisesRegex(T3Error, "parentThreadId"):
            self.bridge.attach("another-thread", CAPABILITIES)
        (self.bridge.directory / "coordinator.json").unlink()
        with self.assertRaisesRegex(T3Error, "No T3 coordinator"):
            self.bridge.ready()

    def test_restart_reuses_request_and_completed_results_without_new_delegate(self) -> None:
        first = self.submit()
        self.bridge.complete(first["id"], {"text": "finished", "task_id": "native-task", "thread_id": "native-child"})
        with mock.patch.object(Bridge, "wake") as wake:
            restored = Bridge(self.context).submit("session", "one", first["payload"], "coordinator")
        wake.assert_not_called()
        self.assertEqual(restored["id"], first["id"])
        self.assertEqual(restored["result"]["text"], "finished")
        self.assertEqual(len(list((self.bridge.directory / "requests").glob("*.json"))), 1)

    def test_new_reviews_are_new_native_tasks_in_the_assigned_checkout(self) -> None:
        builder, review = self.submit(key="builder"), self.submit(key="review")
        for request in (builder, review):
            plan = self.bridge.tool_plan(request)
            self.assertEqual(plan["tool"], "delegate_task")
            self.assertEqual(plan["arguments"]["isolation"], "shared")
        self.assertNotEqual(builder["id"], review["id"])
        workspace = self.submit("workspace", "WP-0001", {"wp": "WP-0001", "base_commit": "main-sha"})
        plan = self.bridge.tool_plan(workspace)
        self.assertEqual(plan["tool"], "delegate_task")
        self.assertEqual(plan["arguments"]["isolation"], "worktree")
        self.assertNotIn("worktree add", plan["arguments"]["task"])

    def test_wake_is_debounced_but_later_requests_get_their_own_wake(self) -> None:
        response = SimpleNamespace(returncode=0, stderr="", stdout="{}")
        with mock.patch("wpo.t3.subprocess.run", return_value=response) as send, mock.patch("wpo.t3.time.time", return_value=100):
            self.bridge.wake("coordinator", created_after=99)
            self.bridge.wake("coordinator", created_after=99)
            self.bridge.wake("coordinator", created_after=101)
            self.assertEqual(send.call_count, 1)
        with mock.patch("wpo.t3.subprocess.run", return_value=response) as send, mock.patch("wpo.t3.time.time", return_value=106):
            self.bridge.wake("coordinator", created_after=101)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[0][1:5], ["thread", "send", "coordinator", "-"])
            self.assertIn("coordinator wake", send.call_args.kwargs["input"])

    def test_callback_wait_receives_result_without_polling_native_status(self) -> None:
        request = self.submit()
        def complete():
            self.bridge.complete(request["id"], {"text": "result", "task_id": "task"})
        timer = threading.Timer(0.02, complete)
        timer.start()
        with mock.patch.object(Bridge, "wake"), mock.patch("wpo.t3.subprocess.run") as native:
            self.assertEqual(self.bridge.wait(request)["result"]["text"], "result")
        timer.join()
        native.assert_not_called()

    def test_callback_read_retries_a_transient_sharing_violation(self) -> None:
        path = self.root / "callback.json"
        path.write_text('{"state":"done"}', encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=[PermissionError("sharing violation"), '{"state":"done"}']), \
                mock.patch("wpo.t3.time.sleep"):
            self.assertEqual(read_json(path), {"state": "done"})

    def test_callback_read_preserves_persistent_permission_errors(self) -> None:
        path = self.root / "callback.json"
        path.touch()
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("access denied")) as reader, \
                mock.patch("wpo.t3.time.sleep"), self.assertRaises(PermissionError):
            read_json(path)
        self.assertGreater(reader.call_count, 1)
        self.assertLessEqual(reader.call_count, 50)

    def test_callback_write_retries_a_transient_sharing_violation(self) -> None:
        path = self.root / "callback.json"
        replace = Path.replace
        calls = []
        def interrupted(source, destination):
            calls.append(source)
            if len(calls) == 1:
                raise PermissionError("sharing violation")
            return replace(source, destination)
        with mock.patch.object(Path, "replace", interrupted), mock.patch("wpo.platform.time.sleep"):
            write_json(path, {"state": "done"})
        self.assertEqual(read_json(path), {"state": "done"})
        self.assertEqual(len(calls), 2)

    def test_started_callback_cannot_overwrite_fast_workspace_completion(self) -> None:
        request = self.submit("workspace", "workspace", {"wp": "WP-0001"})
        self.bridge.update(request["id"], state="done", result={"path": "native-checkout"})
        response = self.root / "response.json"
        write_json(response, {"taskId": "task", "childThreadId": "child", "next": "end your turn"})
        cli_command(self.context, SimpleNamespace(t3_command="started", request=request["id"], response_file=response))
        self.assertEqual(read_json(self.bridge.request_path(request["id"]))["state"], "done")

    def test_workspace_mapping_uses_actual_native_branch_without_rewriting_legacy_config(self) -> None:
        path = self.project.wp_run_dir("WP-0001") / "t3-workspace.json"
        write_json(path, {"path": str(self.root / "native-assigned"), "branch": "t3/assigned-branch"})
        self.assertEqual(self.project.worktree("WP-0001"), self.root / "native-assigned")
        self.assertEqual(self.project.branch("WP-0001"), "t3/assigned-branch")
        standalone = Project(self.root, self.project.config, "standalone")
        self.assertEqual(standalone.branch("WP-0001"), "wp/WP-0001")

    def test_t3_integration_cleanup_does_not_remove_native_checkout(self) -> None:
        integration = Integration(self.context, "WP-0001", False)
        with mock.patch("wpo.integrate.git") as command:
            integration.cleanup()
        command.assert_not_called()
        self.assertIn("retained T3-owned", integration.log_path.read_text())

    def test_dashboard_is_backend_neutral_and_honors_existing_stop_state(self) -> None:
        with mock.patch.object(dashboard, "ensure_running") as start:
            dashboard.autostart(self.project, self.machine)
        start.assert_called_once()

    def test_session_dispatch_never_launches_standalone_client_in_t3_mode(self) -> None:
        with mock.patch("wpo.t3.run_attempt", return_value="native-result") as native, mock.patch("wpo.session.build_launch") as legacy:
            self.assertEqual(session.attempt(self.context, ROUTE, "prompt", self.root / "log", None), "native-result")
        native.assert_called_once()
        legacy.assert_not_called()

    def test_native_result_preserves_existing_jsonl_and_session_identity(self) -> None:
        settings = session.SessionSettings("builder", ["src/**"], self.root, SessionBrief(None, "", "rules"), {}, False, False, 30, {"role": "builder"})
        log = self.root / "session.log"
        def complete(_bridge, request, callback=None):
            self.assertIn("--session", request["payload"]["prompt"])
            self.assertEqual(request["payload"]["target"]["options"], {"reasoningEffort": "high"})
            return {**request, "state": "done", "task_id": "native-task", "thread_id": "native-child", "result": {"text": "Done", "outcome": "ok"}}
        with mock.patch.object(Bridge, "wake"), mock.patch.object(Bridge, "wait", complete):
            outcome = run_attempt(self.context, ROUTE, "Run wpo verify", log, settings)
        self.assertEqual(outcome.result_text, "Done")
        records = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([record["type"] for record in records], ["system", "assistant", "result"])
        self.assertEqual(session.session_facts(log)[0], records[0]["session_id"])
        self.assertFalse(records[-1]["usage_available"])

    def test_cancellation_is_native_and_message_receipt_is_confirmed_before_delivery(self) -> None:
        request = self.submit()
        self.bridge.update(request["id"], state="running", task_id="native-task")
        with mock.patch.object(Bridge, "wake"):
            self.bridge.cancel_wp("WP-0001", "stop")
        plans = [self.bridge.tool_plan(r) for r in self.bridge.pending("coordinator") if r["kind"] == "cancel"]
        self.assertEqual(plans[0], {"tool": "task_cancel", "arguments": {"taskId": "native-task"}})

    def test_late_launch_after_pending_cancellation_is_cancelled_natively(self) -> None:
        request = self.submit()
        self.bridge.cancel_request(request, "stop before launch callback")
        with mock.patch.object(Bridge, "wake"):
            self.bridge.record_started(request["id"], {"taskId": "late-native", "childThreadId": "late-child"})
        original = read_json(self.bridge.request_path(request["id"]))
        self.assertEqual(original["state"], "cancelled")
        cancels = [r for r in self.bridge.pending("coordinator") if r["kind"] == "cancel"]
        self.assertEqual(cancels[0]["payload"]["task_id"], "late-native")
        self.bridge.complete(request["id"], {"state": "done", "task_id": "late-native", "text": "late output"})
        self.assertEqual(read_json(self.bridge.request_path(request["id"]))["state"], "cancelled")

    def test_terminal_and_wrong_task_callbacks_cannot_replace_results(self) -> None:
        request = self.submit()
        self.bridge.record_started(request["id"], {"taskId": "native", "childThreadId": "child"})
        with self.assertRaisesRegex(T3Error, "task ID"):
            self.bridge.complete(request["id"], {"task_id": "unrelated"})
        self.bridge.complete(request["id"], {"state": "done", "task_id": "native", "text": "first"})
        with self.assertRaisesRegex(T3Error, "contradictory"):
            self.bridge.complete(request["id"], {"state": "failed", "task_id": "native"})

    def test_resume_generation_is_fresh_but_unconsumed_invocation_reconnects(self) -> None:
        log = self.root / "resume.log"
        payload = self.submit()["payload"]
        with mock.patch.object(Bridge, "wake"):
            first, receipt = self.bridge.begin_session(log, payload, "coordinator")
            same, _ = self.bridge.begin_session(log, payload, "coordinator")
            self.assertEqual(first["id"], same["id"])
            self.bridge.complete(first["id"], {"text": "done"})
            write_json(receipt, {**read_json(receipt), "consumed": True})
            second, _ = self.bridge.begin_session(log, payload, "coordinator")
        self.assertNotEqual(first["id"], second["id"])

    def test_missing_callback_stalls_without_native_polling_or_automatic_relaunch(self) -> None:
        request = self.submit(payload={"wp": "WP-0001", "idle_minutes": 0.001})
        write_json(self.bridge.request_path(request["id"]), {**request, "updated_at": 1, "created_at": 1})
        with mock.patch.object(Bridge, "wake") as wake, self.assertRaisesRegex(T3Error, "marked stalled"):
            self.bridge.wait(read_json(self.bridge.request_path(request["id"])))
        wake.assert_not_called()
        self.assertEqual(read_json(self.bridge.request_path(request["id"]))["state"], "stalled")

    def test_readonly_mutations_are_retained_and_rejected_without_fallback(self) -> None:
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text(".orchestration/\nstate/\n")
        code = self.root / "code.txt"
        code.write_text("original\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")
        settings = session.SessionSettings("read", [], self.root, SessionBrief(None, "", "rules"), {}, False, False, 30, {"role": "read"})
        def complete(_bridge, request, callback=None):
            self.assertEqual(request["payload"]["native_role"], "research")
            self.assertEqual(request["payload"]["access"], "read")
            code.write_text("unauthorized native edit\n")
            return {**request, "state": "done", "result": {"text": "finished"}}
        with mock.patch.object(Bridge, "wake"), mock.patch.object(Bridge, "wait", complete), self.assertRaisesRegex(T3Error, "outside its access contract"):
            run_attempt(self.context, ROUTE, "Read only", self.project.runs_dir / "read.log", settings)
        self.assertEqual(code.read_text(), "unauthorized native edit\n")

    def test_legacy_dirty_and_staged_data_import_is_non_destructive(self) -> None:
        source = self.project.worktrees_root / "WP-0001"
        source.mkdir(parents=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(source.parent, ignore_errors=True))
        git(source, "init", "-q", "-b", "main")
        git(source, "config", "user.name", "Test")
        git(source, "config", "user.email", "test@example.com")
        (source / "staged.txt").write_text("base\n")
        (source / "dirty.txt").write_text("base\n")
        git(source, "add", ".")
        git(source, "commit", "-qm", "base")
        target = self.root / "native"
        subprocess.run(["git", "clone", "-q", str(source), str(target)], check=True, capture_output=True)
        (source / "staged.txt").write_text("staged\n")
        git(source, "add", "staged.txt")
        (source / "dirty.txt").write_text("dirty\n")
        (source / "untracked.txt").write_text("private note\n")
        before = git(source, "status", "--porcelain").stdout
        write_json(self.project.wp_run_dir("WP-0001") / "t3-workspace.json", {"path": str(target), "branch": "main"})
        self.bridge.migrate_legacy("WP-0001", target)
        self.assertEqual(git(source, "status", "--porcelain").stdout, before)
        self.assertEqual(git(target, "status", "--porcelain").stdout, before)
        self.assertEqual((target / "untracked.txt").read_text(), "private note\n")

    def test_legacy_import_rejects_linked_destination_ancestors(self) -> None:
        source = self.project.worktrees_root / "WP-0001"
        source.mkdir(parents=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(source.parent, ignore_errors=True))
        git(source, "init", "-q", "-b", "main")
        git(source, "config", "user.name", "Test")
        git(source, "config", "user.email", "test@example.com")
        (source / "base.txt").write_text("base")
        git(source, "add", ".")
        git(source, "commit", "-qm", "base")
        target = self.root / "native-linked"
        subprocess.run(["git", "clone", "-q", str(source), str(target)], check=True, capture_output=True)
        (source / "data").mkdir()
        (source / "data" / "file.txt").write_text("must stay inside")
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (target / "data").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("This account cannot create a directory symlink")
        write_json(self.project.wp_run_dir("WP-0001") / "t3-workspace.json", {"path": str(target), "branch": "main"})
        # The linked directory is an untracked setup artifact; isolate path validation from the
        # earlier clean-checkout check to exercise the escaping-copy regression directly.
        real_git = git
        def with_setup_tree(root, *arguments, **kwargs):
            if Path(root) == target and arguments[:2] == ("status", "--porcelain"):
                return SimpleNamespace(stdout="")
            return real_git(root, *arguments, **kwargs)
        with mock.patch("wpo.project.git", side_effect=with_setup_tree), self.assertRaisesRegex(T3Error, "escapes its checkout"):
            self.bridge.migrate_legacy("WP-0001", target)
        self.assertFalse((outside / "file.txt").exists())

    def test_attention_callbacks_keep_old_journal_and_deduplicate_native_wakes(self) -> None:
        self.context.journal.on_attention = self.bridge.attention
        with mock.patch.object(Bridge, "wake"):
            self.context.journal.emit("wp-blocked", "needs decision", {"wp": "WP-0001"})
            self.context.journal.emit("wp-blocked", "needs decision", {"wp": "WP-0001"})
        self.assertEqual(self.context.journal.cursor(), 2)
        attention = [r for r in self.bridge.pending("coordinator") if r["kind"] == "attention"]
        self.assertEqual(len(attention), 1)

    def test_recovery_without_requests_does_not_resume_or_change_existing_holds(self) -> None:
        holds = self.project.runs_dir / "lane-holds.json"
        holds.write_text('{"core":"paused by user"}')
        before = holds.read_bytes()
        with mock.patch.object(Bridge, "wake") as wake:
            self.assertEqual(self.bridge.recover(), 0)
        wake.assert_not_called()
        self.assertEqual(holds.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
