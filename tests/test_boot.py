"""Boot fencing tests use temporary records and simulated identities, never live projects."""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wpo import boot
from wpo.events import Journal

OLD = {"platform": "linux", "host": "first-host", "boot": "first-boot"}
NEW = {**OLD, "boot": "second-boot"}


class BootTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.journal = Journal(self.root)

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def ensure(self, identity):
        with mock.patch.object(boot, "current_identity", return_value=identity):
            boot.ensure_boot(self.root, self.journal)

    def marker(self):
        return json.loads((self.root / "boot.json").read_text())

    def test_first_adoption_preserves_legacy_records_and_repeats_do_nothing(self):
        pid = self.write("supervisor-core.pid", "123")
        state = self.write("supervisor-core.state", "running")
        self.ensure(OLD)
        baseline = (self.root / "boot.json").read_bytes()
        self.ensure(OLD)
        self.assertEqual(pid.read_text(), "123")
        self.assertEqual(state.read_text(), "running")
        self.assertEqual((self.root / "boot.json").read_bytes(), baseline)
        self.assertTrue(self.marker()["legacy_baseline"])
        self.assertEqual(self.journal.read(0), [])

    def test_boot_change_invalidates_only_os_records_preserving_durable_work(self):
        self.ensure(OLD)
        removed = ["supervisor-core.pid", "dashboard.json", "WP-0042/inbox/live.json", "WP-0042/delegate/worker/spawn.pid"]
        for relative in removed:
            self.write(relative, '{"pid":123}')
        self.write("supervisor-core.state", "running")
        self.write("supervisor-idle.state", "sleeping")
        self.write("supervisor-done.state", "finished")
        worker = self.write("WP-0042/delegate/worker/state.json", json.dumps({"state": "running", "worker_pid": 123,
                                                                                 "tier": "code", "write_paths": ["src/**"]}))
        durable = {relative: self.write(relative, "preserve").read_bytes() for relative in (
            "lane-holds.json", "dashboard.stopped", "WP-0042/pause-requested", "WP-0042/rounds.json", "WP-0042/findings.json",
            "WP-0042/inbox/pending.md", "WP-0042/delegate/worker/task.md", "supervisor-core.log", "WP-0042/t3-workspace.json",
            "t3/requests/native.json", "t3/sessions/worker.json", "unknown.pid")}
        self.ensure(NEW)
        self.assertTrue(all(not (self.root / relative).exists() for relative in removed))
        self.assertEqual((self.root / "supervisor-core.state").read_text(), "died")
        self.assertEqual((self.root / "supervisor-idle.state").read_text(), "died")
        self.assertEqual((self.root / "supervisor-done.state").read_text(), "finished")
        state = json.loads(worker.read_text())
        self.assertEqual(state["state"], "interrupted")
        self.assertNotIn("worker_pid", state)
        self.assertEqual(state["write_paths"], ["src/**"])
        for relative, before in durable.items():
            self.assertEqual((self.root / relative).read_bytes(), before)
        event, = self.journal.read(0)
        self.assertEqual(event["kind"], "boot-changed")
        archive, = (self.root / ".boot-recovery").glob("*.json")
        self.assertEqual(json.loads(archive.read_text())["from"], OLD)
        self.assertEqual(self.marker()["identity"], NEW)
        self.ensure(NEW)
        self.assertEqual(len(self.journal.read(0)), 1)

    def test_host_change_with_same_boot_id_also_invalidates(self):
        self.ensure(OLD)
        pid = self.write("supervisor-core.pid", "123")
        self.ensure({**OLD, "host": "other-host"})
        self.assertFalse(pid.exists())

    def test_unknown_identity_does_not_touch_marker_or_pid_records(self):
        self.ensure(OLD)
        before = (self.root / "boot.json").read_bytes()
        pid = self.write("supervisor-core.pid", "123")
        with mock.patch.object(boot, "current_identity", side_effect=boot.BootError("unavailable")), \
                self.assertRaisesRegex(boot.BootError, "unavailable"):
            boot.ensure_boot(self.root, self.journal)
        self.assertEqual((self.root / "boot.json").read_bytes(), before)
        self.assertEqual(pid.read_text(), "123")

    def test_corrupt_marker_does_not_reset_records(self):
        self.write("boot.json", "{")
        pid = self.write("supervisor-core.pid", "123")
        with self.assertRaises(boot.BootError):
            self.ensure(NEW)
        self.assertEqual(pid.read_text(), "123")

    def test_corrupt_worker_state_fails_before_any_cleanup(self):
        self.ensure(OLD)
        pid = self.write("supervisor-core.pid", "123")
        self.write("WP-0042/delegate/worker/state.json", "{")
        with self.assertRaises(boot.BootError):
            self.ensure(NEW)
        self.assertEqual(pid.read_text(), "123")
        self.assertEqual(self.marker()["identity"], OLD)
        self.assertNotIn("pending", self.marker())

    def test_cleanup_failure_leaves_pending_marker_and_retry_is_idempotent(self):
        self.ensure(OLD)
        self.write("supervisor-core.pid", "123")
        self.write("supervisor-core.state", "running")
        real_write = boot._write
        def fail_state(root, relative, value):
            if relative == "supervisor-core.state":
                raise PermissionError("fixture write failure")
            real_write(root, relative, value)
        with mock.patch.object(boot, "_write", side_effect=fail_state), self.assertRaises(boot.BootError):
            self.ensure(NEW)
        self.assertEqual(self.marker()["identity"], OLD)
        self.assertIn("pending", self.marker())
        self.assertFalse((self.root / "supervisor-core.pid").exists())
        self.ensure(NEW)
        self.assertEqual(self.marker()["identity"], NEW)
        self.assertEqual(len(self.journal.read(0)), 1)

    def test_marker_commit_failure_does_not_duplicate_journal(self):
        self.ensure(OLD)
        self.write("supervisor-core.pid", "123")
        real_write = boot._write
        def fail_commit(root, relative, value):
            if relative == "boot.json" and "pending" not in json.loads(value):
                raise PermissionError("fixture commit failure")
            real_write(root, relative, value)
        with mock.patch.object(boot, "_write", side_effect=fail_commit), self.assertRaises(boot.BootError):
            self.ensure(NEW)
        self.assertEqual(len(self.journal.read(0)), 1)
        self.ensure(NEW)
        self.assertEqual(len(self.journal.read(0)), 1)

    def test_changed_record_during_pending_transition_refuses_overwrite(self):
        self.ensure(OLD)
        pid = self.write("supervisor-core.pid", "123")
        with mock.patch.object(boot, "_apply", side_effect=PermissionError("fixture interrupted")), self.assertRaises(boot.BootError):
            self.ensure(NEW)
        pid.write_text("456")
        with self.assertRaisesRegex(boot.BootError, "record changed"):
            self.ensure(NEW)
        self.assertEqual(pid.read_text(), "456")
        self.assertEqual(self.marker()["identity"], OLD)

    def test_concurrent_callers_perform_one_transition(self):
        self.ensure(OLD)
        self.write("supervisor-core.pid", "123")
        with mock.patch.object(boot, "current_identity", return_value=NEW), ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: boot.ensure_boot(self.root, self.journal), range(4)))
        self.assertEqual(len(self.journal.read(0)), 1)

    def test_symlink_ancestor_cannot_escape_runs(self):
        self.ensure(OLD)
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside)
            (target / "inbox").mkdir()
            live = target / "inbox/live.json"
            live.write_text('{"pid":123}')
            try:
                (self.root / "WP-0042").symlink_to(target, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlink unavailable: {error}")
            with self.assertRaisesRegex(boot.BootError, "linked boot record"):
                self.ensure(NEW)
            self.assertTrue(live.exists())
            self.assertEqual(self.marker()["identity"], OLD)

    def test_pending_manifest_cannot_target_arbitrary_file(self):
        self.ensure(OLD)
        marker = self.marker()
        marker["pending"] = {"id": "c6fdac9f-54db-4b43-bd4e-623a24015839", "from": OLD, "to": NEW,
                             "changes": [{"path": "../private", "before": "", "after": None}]}
        self.write("boot.json", json.dumps(marker))
        with self.assertRaisesRegex(boot.BootError, "unknown PID record"):
            self.ensure(NEW)

    def test_pending_manifest_rejects_duplicate_paths_before_cleanup(self):
        self.ensure(OLD)
        pid = self.write("supervisor-core.pid", "123")
        marker = self.marker()
        change = {"path": "supervisor-core.pid", "before": "MTIz", "after": None}
        marker["pending"] = {"id": "c6fdac9f-54db-4b43-bd4e-623a24015839", "from": OLD, "to": NEW,
                             "changes": [change, change]}
        self.write("boot.json", json.dumps(marker))
        with self.assertRaisesRegex(boot.BootError, "duplicate PID record"):
            self.ensure(NEW)
        self.assertEqual(pid.read_text(), "123")

    def test_second_reboot_finishes_pending_transition_before_current_boot(self):
        self.ensure(OLD)
        self.write("supervisor-core.pid", "123")
        with mock.patch.object(boot, "_apply", side_effect=PermissionError("fixture interrupted")), self.assertRaises(boot.BootError):
            self.ensure(NEW)
        newest = {**NEW, "boot": "third-boot"}
        self.ensure(newest)
        self.assertEqual(self.marker()["identity"], newest)
        self.assertEqual(len(self.journal.read(0)), 2)

    def test_invalid_pending_marker_does_not_pass_identity_check(self):
        self.write("boot.json", json.dumps({"schema": 1, "identity": OLD, "pending": None}))
        with self.assertRaisesRegex(boot.BootError, "invalid pending"):
            self.ensure(OLD)

    def test_linked_marker_refused_without_touching_target(self):
        target = self.write("elsewhere.json", json.dumps({"schema": 1, "identity": OLD}))
        try:
            (self.root / "boot.json").symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlink unavailable: {error}")
        before = target.read_bytes()
        with self.assertRaisesRegex(boot.BootError, "linked boot record"):
            self.ensure(NEW)
        self.assertEqual(target.read_bytes(), before)

    def test_linked_journal_refused_before_cleanup(self):
        self.ensure(OLD)
        pid = self.write("supervisor-core.pid", "123")
        target = self.write("elsewhere.jsonl", "")
        try:
            (self.root / "events.jsonl").symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlink unavailable: {error}")
        with self.assertRaisesRegex(boot.BootError, "linked boot record"):
            self.ensure(NEW)
        self.assertEqual(pid.read_text(), "123")
        self.assertEqual(self.marker()["identity"], OLD)

    def test_context_checks_boot_before_constructing_router(self):
        from wpo.context import Context
        with mock.patch("wpo.context.Project.locate", return_value=types.SimpleNamespace(runs_dir=self.root)), \
                mock.patch("wpo.context.ensure_boot", side_effect=boot.BootError("unsafe boot")), \
                mock.patch("wpo.context.Router") as router, self.assertRaisesRegex(boot.BootError, "unsafe boot"):
            Context.load(self.root, types.SimpleNamespace(raw={}, execution_mode="standalone"))
        router.assert_not_called()

    def test_windows_api_failure_is_explicit(self):
        with mock.patch.object(boot.sys, "platform", "win32"), \
                mock.patch.object(boot, "_windows_identity", side_effect=OSError("access denied")), \
                self.assertRaisesRegex(boot.BootError, "cannot determine"):
            boot.current_identity()

    def test_linux_reads_actual_machine_and_boot_files_without_windows_api(self):
        boot_id = "c6fdac9f-54db-4b43-bd4e-623a24015839"
        with mock.patch.object(boot.sys, "platform", "linux"), mock.patch.object(Path, "is_file", return_value=True), \
                mock.patch.object(Path, "read_text", side_effect=["machine-id\n", boot_id + "\n"]), \
                mock.patch.object(boot, "_windows_identity", side_effect=AssertionError("Windows API called")):
            self.assertEqual(boot.current_identity(), {"platform": "linux", "host": "machine-id", "boot": boot_id})

    def test_unsupported_platform_fails_closed(self):
        with mock.patch.object(boot.sys, "platform", "unknown"), self.assertRaisesRegex(boot.BootError, "unsupported"):
            boot.current_identity()


if __name__ == "__main__":
    unittest.main()
