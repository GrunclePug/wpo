"""Native bridge behavior: durable callbacks, model settings, workspace mapping and legacy data."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
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

    def test_antigravity_effort_is_its_thinking_level(self) -> None:
        capabilities = json.loads(json.dumps(CAPABILITIES))
        capabilities["providers"].append({"providerInstanceId": "antigravity", "canRunChildTask": True, "models": [
            {"id": "gemini-3.8-flash", "options": [{"id": "thinking_level", "values": ["low", "medium", "high"]}]}]})
        self.bridge.attach("coordinator", capabilities)
        # The bundled agy routes name T3's model with the effort apart (agy's CLI takes the same as --model/--effort).
        bundled = dataclasses.asdict(self.context.router.find_route("agy-gemini-flash-medium"))
        target = self.bridge.target(bundled)
        self.assertEqual(target, {"providerInstanceId": "antigravity", "model": "gemini-3.8-flash", "options": {"thinking_level": "medium"}})

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

    def test_large_session_context_is_read_from_a_lossless_utf8_file(self) -> None:
        prompt = "Complete instructions: 🧟 café\r\n" * 6000
        request = self.submit(payload={"wp": "WP-0001", "prompt": prompt, "title": "large context",
                                       "native_role": "implementation", "target": self.bridge.target(ROUTE)})
        plan = self.bridge.tool_plan(request)
        self.assertGreater(len(prompt), 120_000)
        self.assertLess(len(plan["arguments"]["task"]), 120_000)
        context_file = self.bridge.request_path(request["id"]).with_suffix(".prompt.md")
        self.assertEqual(context_file.read_bytes(), prompt.encode("utf-8"))
        self.assertIn(context_file.resolve().as_posix(), plan["arguments"]["task"])
        self.assertIn("entire file", plan["arguments"]["task"])
        self.assertIn("stop", plan["arguments"]["task"])
        self.assertIn("t3 heartbeat --request " + request["id"], plan["arguments"]["task"])
        self.assertEqual(plan["arguments"]["target"], request["payload"]["target"])
        self.assertEqual(plan["arguments"]["clientRequestId"], request["id"])
        self.assertEqual(plan["arguments"]["isolation"], "shared")

    def test_session_context_files_survive_replanning_and_keep_requests_separate(self) -> None:
        self.bridge.directory = self.root / "run state with spaces" / "t3"
        target = {"providerInstanceId": "codex", "model": "gpt-6.1-sol", "options": {"reasoningEffort": "high"}}
        first = self.submit(key="first", payload={"wp": "WP-0001", "prompt": "First task", "title": "first",
                                                  "native_role": "implementation", "target": target})
        second = self.submit(key="second", payload={"wp": "WP-0001", "prompt": "Independent review", "title": "second",
                                                    "native_role": "review", "target": target})
        first_plan = self.bridge.tool_plan(first)
        self.bridge.tool_plan(second)
        self.assertEqual(self.bridge.tool_plan(first), first_plan)
        for request in (first, second):
            context_file = self.bridge.request_path(request["id"]).with_suffix(".prompt.md")
            self.assertEqual(context_file.read_text(encoding="utf-8"), request["payload"]["prompt"])
            self.assertIn(context_file.resolve().as_posix(), self.bridge.tool_plan(request)["arguments"]["task"])
        self.assertEqual(len(self.bridge.pending("coordinator")), 2)

    def test_session_plan_fails_if_context_cannot_be_published(self) -> None:
        request = self.submit()
        with mock.patch("wpo.t3.replace_file", side_effect=PermissionError("context unavailable")), \
                self.assertRaisesRegex(PermissionError, "context unavailable"):
            self.bridge.tool_plan(request)

    def test_session_context_publication_replaces_links_without_writing_their_targets(self) -> None:
        request = self.submit()
        outside = self.root / "outside-context.md"
        outside.write_text("Original contents", encoding="utf-8")
        context_file = self.bridge.request_path(request["id"]).with_suffix(".prompt.md")
        try:
            context_file.symlink_to(outside)
        except OSError:
            self.skipTest("This account cannot create a file symlink")
        plan = self.bridge.tool_plan(request)
        self.assertEqual(outside.read_text(encoding="utf-8"), "Original contents")
        self.assertFalse(context_file.is_symlink())
        self.assertEqual(context_file.read_text(encoding="utf-8"), request["payload"]["prompt"])
        self.assertIn(context_file.resolve().as_posix(), plan["arguments"]["task"])

    def test_wake_is_debounced_and_a_later_request_waits_out_the_window(self) -> None:
        response = SimpleNamespace(returncode=0, stderr="", stdout="{}")
        clock = [100.0]
        with mock.patch("wpo.t3.subprocess.run", return_value=response) as send, \
                mock.patch("wpo.t3.time.time", side_effect=lambda: clock[0]), \
                mock.patch("wpo.t3.time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            self.bridge.wake("coordinator", created_after=99)
            self.bridge.wake("coordinator", created_after=99)
            self.assertEqual(send.call_count, 1, "a request older than the last send is covered by it")
            self.bridge.wake("coordinator", created_after=101)
            self.assertEqual(send.call_count, 2, "a newer request inside the window waits it out and is sent, not dropped")
            self.assertGreaterEqual(clock[0], 105)
            self.bridge.wake("coordinator", created_after=101)
            self.assertEqual(send.call_count, 2)
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
        def complete(bridge, request, callback=None):
            self.assertIn("--session", request["payload"]["prompt"])
            self.assertEqual(request["payload"]["target"]["options"], {"reasoningEffort": "high"})
            return bridge.update(request["id"], state="done", task_id="native-task", thread_id="native-child", result={"text": "Done", "outcome": "ok"})
        with mock.patch.object(Bridge, "wake"), mock.patch.object(Bridge, "wait", complete):
            outcome = run_attempt(self.context, ROUTE, "Run wpo verify", log, settings)
        self.assertEqual(outcome.result_text, "Done")
        records = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([record["type"] for record in records], ["system", "assistant", "result"])
        self.assertEqual(session.session_facts(log)[0], records[0]["session_id"])
        self.assertFalse(records[-1]["usage_available"])

    def test_families_that_load_instructions_natively_get_the_native_brief(self) -> None:
        brief = SessionBrief(None, "", "full rules", native_text="native rules")
        settings = session.SessionSettings("builder", ["src/**"], self.root, brief, {}, False, False, 30, {"role": "builder"})
        prompts = []
        def complete(bridge, request, callback=None):
            prompts.append(request["payload"]["prompt"])
            return bridge.update(request["id"], state="done", task_id=f"native-{len(prompts)}", thread_id="child", result={"text": "Done", "outcome": "ok"})
        with mock.patch.object(Bridge, "wake"), mock.patch.object(Bridge, "wait", complete):
            run_attempt(self.context, ROUTE, "Task", self.root / "full.log", settings)
            self.machine.raw["t3"]["native_instructions"] = {"families": ["gpt"], "names": ["CLAUDE.md"], "files": []}
            run_attempt(self.context, ROUTE, "Task", self.root / "native.log", settings)
        self.assertTrue(prompts[0].startswith("full rules") and "native rules" not in prompts[0])
        self.assertTrue(prompts[1].startswith("native rules") and "full rules" not in prompts[1])

    def test_cancellation_is_native_and_message_receipt_is_confirmed_before_delivery(self) -> None:
        request = self.submit()
        self.bridge.update(request["id"], state="running", task_id="native-task")
        with mock.patch.object(Bridge, "wake"):
            self.bridge.cancel_wp("WP-0001", "stop", "operator")
        plans = [self.bridge.tool_plan(r) for r in self.bridge.pending("coordinator") if r["kind"] == "cancel"]
        self.assertEqual(plans[0], {"tool": "task_cancel", "arguments": {"taskId": "native-task"}})

    def test_late_launch_after_pending_cancellation_is_cancelled_natively(self) -> None:
        request = self.submit()
        with mock.patch.object(Bridge, "wake"):
            self.bridge.cancel_request(request, "stop before launch callback", "operator")
        # Only the coordinator knows whether it launched it: not settled locally.
        self.assertEqual(read_json(self.bridge.request_path(request["id"]))["state"], "pending")
        with mock.patch.object(Bridge, "wake"):
            self.bridge.record_started(request["id"], {"taskId": "late-native", "childThreadId": "late-child"})
        original = read_json(self.bridge.request_path(request["id"]))
        self.assertEqual((original["state"], original["cancel_origin"]), ("running", "operator"))
        cancels = [r for r in self.bridge.pending("coordinator") if r["kind"] == "cancel"]
        self.assertEqual(cancels[0]["payload"]["task_id"], "late-native")
        result = self.root / "cancel-done.json"
        write_json(result, {"state": "done"})
        cli_command(self.context, SimpleNamespace(t3_command="complete", request=cancels[0]["id"], result_file=result))
        self.assertEqual(read_json(self.bridge.request_path(request["id"]))["state"], "cancelled")
        self.bridge.complete(request["id"], {"state": "done", "task_id": "late-native", "text": "late output"})
        self.assertEqual(read_json(self.bridge.request_path(request["id"]))["state"], "cancelled")

    def test_backend_and_boot_failures_exit_8_not_the_no_route_code(self) -> None:
        from wpo import cli
        from wpo.boot import BootError
        from wpo.runner import EXIT_BACKEND, EXIT_NO_ROUTE
        self.assertNotEqual(EXIT_BACKEND, EXIT_NO_ROUTE)
        for error in (T3Error("coordinator gone"), BootError("foreign host")):
            with mock.patch("wpo.cli.main", side_effect=error), mock.patch("sys.stderr"):
                self.assertEqual(cli.entrypoint(), EXIT_BACKEND)

    def finished_attempt(self, completed: dict, settings=None):
        """Runs a native attempt whose request comes back as `completed` (merged over the request)."""
        settings = settings or session.SessionSettings("builder", ["src/**"], self.root, SessionBrief(None, "", "rules"), {}, False, False, 30, {"role": "builder"})
        with mock.patch.object(Bridge, "wake"), mock.patch.object(Bridge, "wait", lambda bridge, request, callback=None: bridge.update(request["id"], task_id="t", **completed)):
            return run_attempt(self.context, ROUTE, "Task", self.root / f"attempt-{uuid.uuid4().hex}.log", settings)

    def test_an_operator_cancel_ends_the_session_whatever_state_the_task_reached(self) -> None:
        for state in ("cancelled", "failed", "done"):
            with self.assertRaises(session.SessionCancelled) as raised:
                self.finished_attempt({"state": state, "cancel_origin": "operator", "result": {"outcome": "session_error"}})
            self.assertEqual((raised.exception.origin, raised.exception.route_id), ("operator", ROUTE["id"]))
        with self.assertRaises(session.SessionCancelled) as raised:
            self.finished_attempt({"state": "cancelled", "cancel_origin": "cleanup"})
        self.assertEqual(raised.exception.origin, "cleanup")
        outcome = self.finished_attempt({"state": "cancelled", "cancel_origin": "cutoff"})
        self.assertEqual(outcome.outcome, "session_error", "a quota cut-off keeps the failure path")

    def test_a_staged_compaction_stop_is_not_a_cleanup_cancel(self) -> None:
        settings = session.SessionSettings("builder", ["src/**"], self.root, SessionBrief(None, "", "rules"), {}, False, True, 30, {"role": "executor"})
        with self.assertRaises(session.CompactionStop):
            self.finished_attempt({"state": "cancelled", "compacted": True, "cancel_origin": "cleanup"}, settings)

    def test_the_stronger_cancel_origin_wins_in_either_order(self) -> None:
        for first, second in (("cutoff", "operator"), ("operator", "cutoff"), ("cleanup", "operator"), ("operator", "cleanup")):
            request = self.submit(key=f"{first}-{second}")
            self.bridge.update(request["id"], state="running", task_id="native-task")
            with mock.patch.object(Bridge, "wake"):
                self.bridge.cancel_request(request, "first", first)
                self.bridge.cancel_request(request, "second", second)
            self.assertEqual(read_json(self.bridge.request_path(request["id"]))["cancel_origin"], "operator")

    def test_an_operator_cancel_starts_no_other_route(self) -> None:
        other = {**ROUTE, "id": "gpt-sol-xhigh", "effort": "xhigh"}
        selection = SimpleNamespace(candidates=[{**ROUTE, "downgrade": False}, {**other, "downgrade": False}], skipped=[], next_available_at=None)
        with mock.patch.object(self.context.router, "select", return_value=selection), \
                mock.patch("wpo.session.build_brief", return_value=SessionBrief(None, "", "rules")), \
                mock.patch.object(self.context.router, "mark") as mark, \
                mock.patch("wpo.session.attempt", side_effect=session.SessionCancelled(ROUTE["id"], "cleanup")) as attempt, \
                self.assertRaises(session.SessionCancelled):
            session.run_session(self.context, "code", "builder", "Task", self.root / "stem", self.root, write_paths=["src/**"])
        attempt.assert_called_once()
        mark.assert_not_called()

    def test_a_cancel_before_launch_waits_for_the_coordinator(self) -> None:
        import argparse
        request = self.submit()
        with mock.patch.object(Bridge, "wake"):
            self.bridge.cancel_request(request, "lane stopped", "operator")
        stored = read_json(self.bridge.request_path(request["id"]))
        self.assertEqual((stored["state"], stored["cancel_requested"]), ("pending", True))
        with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as output:
            cli_command(self.context, SimpleNamespace(t3_command="next", coordinator="coordinator"))
        entry = next(e for e in json.loads(output.getvalue()) if e["request"]["id"] == request["id"])
        self.assertNotIn("native_tool", entry, "no launch plan for a cancelled request")
        command = entry["cancelled_before_launch"].split("t3 complete ", 1)[1].split(". If", 1)[0]
        parser = argparse.ArgumentParser()
        from wpo.t3 import add_cli
        add_cli(parser.add_subparsers(dest="command"))
        arguments = parser.parse_args(["t3", "complete", *__import__("shlex").split(command)])
        cli_command(self.context, arguments)
        self.assertEqual(read_json(self.bridge.request_path(request["id"]))["state"], "cancelled")

    def test_completion_outcomes_are_validated(self) -> None:
        builder = self.submit(key="builder")
        with self.assertRaisesRegex(T3Error, "not one of"):
            self.bridge.complete(builder["id"], {"state": "done", "outcome": "bogus"})
        with self.assertRaisesRegex(T3Error, "not one of"):
            self.bridge.complete(builder["id"], {"state": "done", "outcome": "compacted"})
        staged = self.submit(key="staged", payload={**builder["payload"], "stop_on_compaction": True})
        self.assertEqual(self.bridge.complete(staged["id"], {"state": "done", "outcome": "compacted"})["state"], "done")

    def native_session(self, log: Path, access: str = "builder", before_tree: str | None = None, **completion) -> dict:
        """A session request begun under `log` and completed with `completion`, as the bridge records it."""
        payload = {**self.submit()["payload"], "route": ROUTE, "log_path": str(log), "cwd": str(self.root), "access": access,
                   "write_paths": [], "before_tree": before_tree, "wp": None, "stop_on_compaction": False}
        with mock.patch.object(Bridge, "wake"):
            request, _ = self.bridge.begin_session(log, payload, "coordinator")
        return self.bridge.update(request["id"], state="done", task_id="native", result={"text": "Done", "outcome": "ok"}, **completion)

    def test_two_finishes_of_one_request_consume_it_once(self) -> None:
        from wpo.t3 import finish_attempt
        log = self.root / "twice.log"
        request = self.native_session(log)
        self.assertEqual(finish_attempt(self.context, request, recovery=True), "ok")
        first = read_json(self.bridge.request_path(request["id"]))["consumed_at"]
        writers = []
        self.assertEqual(finish_attempt(self.context, request, writers.append, recovery=True), "ok")
        self.assertEqual(read_json(self.bridge.request_path(request["id"]))["consumed_at"], first)
        self.assertEqual(writers, [], "an already consumed request only returns what was recorded")
        self.assertEqual(sum('"type": "result"' in line for line in log.read_text().splitlines()), 1)
        self.assertTrue(read_json(log.with_name(log.name + ".t3-invocation.json"))["consumed"])

    def test_a_finish_interrupted_before_consumption_is_replayed_once(self) -> None:
        from wpo import t3
        log = self.root / "replay.log"
        request = self.native_session(log)
        writers = []

        def crash(route):
            writers.append(route["id"])
            raise RuntimeError("killed")
        with self.assertRaises(RuntimeError):
            t3.finish_attempt(self.context, request, crash, recovery=True)
        self.assertIsNone(read_json(self.bridge.request_path(request["id"]))["consumed_at"])
        real = t3.write_json
        def crash_on_request(path, value):
            if path == self.bridge.request_path(request["id"]) and value.get("consumed_at"):
                raise RuntimeError("killed after the log write")
            real(path, value)
        with mock.patch("wpo.t3.write_json", side_effect=crash_on_request), self.assertRaises(RuntimeError):
            t3.finish_attempt(self.context, request, lambda route: writers.append(route["id"]), recovery=True)
        self.assertEqual(t3.finish_attempt(self.context, request, lambda route: writers.append(route["id"]), recovery=True), "ok")
        self.assertEqual(writers, [ROUTE["id"]] * 3, "the writer is recorded first on every attempt")
        self.assertEqual(sum('"type": "result"' in line for line in log.read_text().splitlines()), 1, "the result is logged once")
        self.assertIsNotNone(read_json(self.bridge.request_path(request["id"]))["consumed_at"])

    def test_a_scope_violation_is_logged_as_an_error_live_and_in_recovery(self) -> None:
        from wpo.commands import worktree_tree_id
        from wpo.t3 import finish_attempt
        for arguments in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            git(self.root, *arguments)
        code = self.root / "code.txt"
        code.write_text("original\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")
        before = worktree_tree_id(self.root)
        code.write_text("a reviewer's edit\n")
        for recovery in (False, True):
            log = self.root / f"review-{recovery}.log"
            request = self.native_session(log, access="reviewer", before_tree=before)
            if recovery:
                self.assertEqual(finish_attempt(self.context, request, recovery=True), "scope_violation")
            else:
                with self.assertRaisesRegex(T3Error, "outside its access contract"):
                    finish_attempt(self.context, request)
            result = [json.loads(line) for line in log.read_text().splitlines() if '"type": "result"' in line][-1]
            self.assertTrue(result["is_error"])
            self.assertIn("outside its access contract", result["result"])

    def test_a_cancelled_builder_is_recorded_as_the_writer_before_the_stop(self) -> None:
        settings = session.SessionSettings("builder", ["src/**"], self.root, SessionBrief(None, "", "rules"), {}, False, False, 30, {"role": "builder"})
        seen = []

        def record(route):
            requests = [read_json(path) for path in (self.bridge.directory / "requests").glob("*.json")]
            seen.append((route["id"], [r.get("consumed_at") for r in requests if r["kind"] == "session"]))
        settings = dataclasses.replace(settings, on_finished=record)
        with self.assertRaises(session.SessionCancelled):
            self.finished_attempt({"state": "cancelled", "cancel_origin": "operator"}, settings)
        self.assertEqual(seen, [(ROUTE["id"], [None])], "recorded before the request was consumed")

    def test_a_receipt_names_its_request_before_the_submit(self) -> None:
        log = self.root / "interrupted.log"
        payload = self.submit()["payload"]
        with mock.patch.object(Bridge, "submit", side_effect=RuntimeError("killed before the submit")), self.assertRaises(RuntimeError):
            self.bridge.begin_session(log, payload, "coordinator")
        receipt = read_json(log.with_name(log.name + ".t3-invocation.json"))
        self.assertFalse(receipt["consumed"])
        self.assertIsNone(read_json(self.bridge.request_path(receipt["request_id"])), "nothing was submitted")
        with mock.patch.object(Bridge, "wake"):
            request, _ = self.bridge.begin_session(log, payload, "coordinator")
        self.assertNotEqual(request["id"], receipt["request_id"], "an interrupted start is not reconnected to")
        self.assertEqual(read_json(log.with_name(log.name + ".t3-invocation.json"))["generation"], 2)

    def test_a_consumed_request_wins_over_its_stale_receipt(self) -> None:
        log = self.root / "stale.log"
        request = self.native_session(log)
        self.bridge.update(request["id"], consumed_at=time.time())  # the receipt write after it was lost
        with mock.patch.object(Bridge, "wake"):
            again, receipt = self.bridge.begin_session(log, request["payload"], "coordinator")
        self.assertNotEqual(again["id"], request["id"], "a new generation, not a replayed result")
        self.assertEqual(read_json(receipt)["generation"], 2)

    def test_a_wp_file_write_killed_midway_keeps_the_previous_content(self) -> None:
        from wpo import wpfile
        path = self.root / "WP-0001-x.md"
        path.write_text("---\nid: WP-0001\n---\nprevious\n", encoding="utf-8")
        with mock.patch("wpo.platform.replace_file", side_effect=OSError("killed")), self.assertRaises(OSError):
            wpfile.write_text(path, "---\nid: WP-0001\n---\nnew\n")
        self.assertEqual(path.read_text(encoding="utf-8"), "---\nid: WP-0001\n---\nprevious\n")

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
        def complete(bridge, request, callback=None):
            self.assertEqual(request["payload"]["native_role"], "research")
            self.assertEqual(request["payload"]["access"], "read")
            code.write_text("unauthorized native edit\n")
            return bridge.update(request["id"], state="done", result={"text": "finished"})
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

    def stub_sends(self, block=None):
        """Stubs only the T3 CLI send, so the real wake and debounce logic runs; returns the sends made."""
        sends = []

        def send(command, **kwargs):
            if block is not None and not sends:
                sends.append(kwargs["input"])
                block.wait(5)
            else:
                sends.append(kwargs["input"])
            return subprocess.CompletedProcess(command, 0, "", "")
        patcher = mock.patch("wpo.t3.subprocess.run", side_effect=send)
        patcher.start()
        self.addCleanup(patcher.stop)
        return sends

    def test_attention_recurring_after_acknowledgement_wakes_again(self) -> None:
        self.bridge.settings["wake_debounce_seconds"] = 0.3
        sends = self.stub_sends()
        event = {"kind": "supervisor-finished", "supervisor": "core", "message": "core finished; not done: none"}
        self.bridge.attention(event)
        self.bridge.attention(event)  # still pending: the same request, no second wake
        first = [r for r in self.bridge.pending("coordinator") if r["kind"] == "attention"]
        self.assertEqual((len(first), len(sends)), (1, 1))
        self.bridge.complete(first[0]["id"], {"state": "done"})
        self.bridge.attention(event)  # inside the debounce window: waits it out, then sends
        second = [r for r in self.bridge.pending("coordinator") if r["kind"] == "attention"]
        self.assertEqual(len(second), 1)
        self.assertNotEqual(second[0]["id"], first[0]["id"])
        self.assertEqual(len(sends), 2)

    def test_a_request_created_during_a_send_is_not_covered_by_it(self) -> None:
        self.bridge.settings["wake_debounce_seconds"] = 0.2
        release = threading.Event()
        sends = self.stub_sends(block=release)
        first = threading.Thread(target=self.bridge.attention, args=({"kind": "wp-blocked", "wp": "WP-0001", "message": "one"},))
        first.start()
        while not sends:
            time.sleep(0.01)
        second = threading.Thread(target=self.bridge.attention, args=({"kind": "wp-blocked", "wp": "WP-0002", "message": "two"},))
        second.start()
        time.sleep(0.1)  # the second request now exists and waits for the wake lock
        release.set()
        first.join(5)
        second.join(5)
        self.assertEqual(len(sends), 2)

    def test_the_wake_lock_outlasts_a_send(self) -> None:
        from wpo import t3
        locks = []
        real = t3.FileLock
        with mock.patch("wpo.t3.FileLock", side_effect=lambda path, **kwargs: locks.append(kwargs) or real(path, **kwargs)):
            self.stub_sends()
            self.bridge.wake("coordinator", force=True)
        self.assertGreater(locks[0]["timeout_seconds"], t3.WAKE_SEND_TIMEOUT_SECONDS + float(self.bridge.settings.get("wake_debounce_seconds", 5)))

    def test_failed_workspace_request_is_asked_again(self) -> None:
        first = self.submit("workspace", "WP-0001", {"wp": "WP-0001", "base_commit": "x"})
        self.bridge.complete(first["id"], {"state": "failed", "detail": "T3 could not prepare it"})
        second = self.submit("workspace", "WP-0001", {"wp": "WP-0001", "base_commit": "y"})
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual((second["state"], second["generation"], second["payload"]["base_commit"]), ("pending", 2, "y"))
        self.assertEqual(self.submit("workspace", "WP-0001", {"wp": "WP-0001"})["id"], second["id"], "a pending one is reused")

    def test_request_ids_keep_generation_one_and_never_collide(self) -> None:
        import hashlib

        from wpo.t3 import request_id_for
        legacy = hashlib.sha256(b"attention:K").hexdigest()[:32]
        self.assertEqual(request_id_for("attention", "K"), legacy)
        ids = {request_id_for("attention", key, generation) for key in ("K", "K#2", "K#2#2") for generation in (1, 2, 3)}
        self.assertEqual(len(ids), 9)
        first = self.submit("attention", "K", {"event": {}})
        self.assertEqual(first["id"], legacy, "an existing generation-1 file is found under its old id")
        self.bridge.complete(first["id"], {"state": "done"})
        again, other = self.submit("attention", "K", {"event": {}}), self.submit("attention", "K#2", {"event": {}})
        self.assertNotEqual(again["id"], other["id"])

    def native_checkout(self) -> tuple[Path, str]:
        """A real repository with a separate checkout on its own branch, as T3 prepares one."""
        for arguments in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            git(self.root, *arguments)
        (self.root / "README").write_text("x\n")
        git(self.root, "add", "README")
        git(self.root, "commit", "-qm", "base")
        tree = self.root.parent / f"{self.root.name}-native"
        git(self.root, "worktree", "add", "-q", "-b", "t3/native", str(tree))
        self.addCleanup(lambda: __import__("shutil").rmtree(tree, ignore_errors=True))
        return tree.resolve(), git(self.root, "rev-parse", "main").stdout.strip()

    def test_workspace_registration_is_idempotent_and_refuses_terminal_requests(self) -> None:
        tree, base = self.native_checkout()
        request = self.submit("workspace", "WP-0001", {"wp": "WP-0001", "base_commit": base})
        recorded = self.bridge.register_workspace(request["id"], "wp-thread", tree)
        self.assertEqual(self.bridge.register_workspace(request["id"], "wp-thread", tree), recorded, "a repeated callback is idempotent")
        with self.assertRaisesRegex(T3Error, "already done"):
            self.bridge.register_workspace(request["id"], "another-thread", tree)
        self.assertEqual(self.bridge.workspace("WP-0001"), recorded)
        cancelled = self.submit("workspace", "WP-0002", {"wp": "WP-0002", "base_commit": base})
        self.bridge.complete(cancelled["id"], {"state": "cancelled", "detail": "lane stopped"})
        with self.assertRaisesRegex(T3Error, "already cancelled"):
            self.bridge.register_workspace(cancelled["id"], "wp-thread", tree)
        state = read_json(self.bridge.request_path(cancelled["id"]))
        self.assertEqual(state["state"], "cancelled")
        self.assertIn("late_result", state)
        self.assertIsNone(self.bridge.workspace("WP-0002"))

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
