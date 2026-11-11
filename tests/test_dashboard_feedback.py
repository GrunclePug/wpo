"""Native conversation snapshots and reply routing use supported, read-only T3 calls."""

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

from wpo.t3 import Bridge, T3Error, write_json
from wpo.transcript import native_request_id, render_session


class NativeFeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix="wpo-feedback-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.bridge = Bridge(SimpleNamespace(
            project=SimpleNamespace(root=self.root, runs_dir=self.root / "runs"),
            machine=SimpleNamespace(raw={"t3": {"cli": "fixture-t3", "base_dir": str(self.root / "t3")}}),
        ))
        self.request_id = "a" * 32
        write_json(self.bridge.directory / "coordinator.json", {"thread_id": "user-chat"})
        write_json(self.bridge.request_path(self.request_id), {
            "kind": "session", "thread_id": "builder", "coordinator_thread_id": "wp-coordinator",
        })

    def test_snapshot_reads_latest_messages_once_without_sending_or_polling(self) -> None:
        snapshot = {"thread": {"threadId": "builder"}, "messages": [{"role": "assistant", "text": "Received"}]}
        completed = SimpleNamespace(returncode=0, stdout=json.dumps(snapshot), stderr="")
        with mock.patch("wpo.t3.subprocess.run", return_value=completed) as run:
            self.assertEqual(self.bridge.thread_snapshot(self.request_id), snapshot)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["fixture-t3", "thread", "show", "builder"])
        self.assertIn("--messages", command)
        self.assertIn("--base-dir", command)
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_snapshot_rejects_failed_malformed_and_wrong_thread_responses(self) -> None:
        for completed in [
            SimpleNamespace(returncode=1, stdout="", stderr="offline"),
            SimpleNamespace(returncode=0, stdout="not json", stderr=""),
            SimpleNamespace(returncode=0, stdout=json.dumps({"thread": {"threadId": "other"}, "messages": []}), stderr=""),
            SimpleNamespace(returncode=0, stdout=json.dumps({"thread": {"threadId": "builder"}, "messages": ["bad"]}), stderr=""),
        ]:
            with (self.subTest(completed=completed), mock.patch("wpo.t3.subprocess.run", return_value=completed),
                  self.assertRaises(T3Error)):
                self.bridge.thread_snapshot(self.request_id)
        with (mock.patch("wpo.t3.subprocess.run", side_effect=subprocess.TimeoutExpired("fixture", 15)),
              self.assertRaises(T3Error)):
            self.bridge.thread_snapshot(self.request_id)

    def test_report_back_targets_user_chat_without_changing_stored_message(self) -> None:
        original = "Please confirm reception to the orchestrator."
        request = {"id": self.request_id, "kind": "message", "coordinator_thread_id": "wp-coordinator",
                   "payload": {"thread_id": "builder", "message": original}}
        plan = self.bridge.tool_plan(request)
        self.assertEqual(plan["tool"], "t3_thread_send")
        self.assertEqual(plan["arguments"]["mode"], "steer")
        self.assertTrue(plan["arguments"]["message"].startswith(original))
        self.assertIn("user-chat", plan["arguments"]["message"])
        self.assertIn("user-facing", plan["arguments"]["message"])
        self.assertEqual(request["payload"]["message"], original)

    def test_native_view_shows_snapshot_and_errors_with_html_escaped(self) -> None:
        log = self.root / "attempt-1-builder.gpt-test.log"
        log.write_text(json.dumps({"type": "system", "subtype": "init", "backend": "t3",
                                   "t3": {"request_id": self.request_id}}) + "\n")
        snapshot = {"thread": {"threadId": "builder", "title": "Builder"},
                    "messages": [{"role": "user", "text": "Confirm"},
                                 {"role": "assistant", "text": "Received <script>bad()</script>"}]}
        page = render_session(log, native=snapshot, dashboard=True)
        self.assertIn("Received &lt;script&gt;bad()&lt;/script&gt;", page)
        self.assertNotIn("Received <script>", page)
        self.assertIn("Refresh conversation", page)
        self.assertIn("/theme.js", page)
        self.assertIn("Back to dashboard", page)
        error_page = render_session(log, native_error="T3 unavailable <offline>", dashboard=True)
        self.assertIn("T3 unavailable &lt;offline&gt;", error_page)
        self.assertIn("Local WPO log", error_page)

    def test_http_view_uses_log_generation_identity_and_retains_errors(self) -> None:
        from wpo.dashboard_server import DashboardHandler

        log = self.root / "attempt-1-builder.gpt-test.generation-1.log"
        log.write_text(json.dumps({"backend": "t3", "t3": {"request_id": self.request_id}}) + "\n")
        self.assertEqual(native_request_id(log), self.request_id)
        data = SimpleNamespace(context=self.bridge.context, run_file=lambda _: log)
        hub = SimpleNamespace(data=data, data_lock=threading.Lock())
        handler = SimpleNamespace(server=SimpleNamespace(hub=hub), send_body=mock.Mock())
        snapshot = {"thread": {"threadId": "builder"}, "messages": [{"role": "assistant", "text": "Received"}]}
        with mock.patch.object(Bridge, "thread_snapshot", return_value=snapshot) as read:
            DashboardHandler.send_view(handler, "WP-0001/archive.log")
        read.assert_called_once_with(self.request_id)
        self.assertIn(b"Received", handler.send_body.call_args.args[2])
        with mock.patch.object(Bridge, "thread_snapshot", side_effect=T3Error("offline")):
            DashboardHandler.send_view(handler, "WP-0001/archive.log")
        self.assertEqual(handler.send_body.call_args.args[0], 200)
        self.assertIn(b"offline", handler.send_body.call_args.args[2])
        self.assertIn(b"Local WPO log", handler.send_body.call_args.args[2])
