"""Portable schedule plans and rollback; never call a real OS scheduler."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from wpo import audit, cli
from wpo.machine import MachineConfig
from wpo.project import DEFAULTS, Project


class ReconfigurableOutput(io.StringIO):
    def reconfigure(self, **_kwargs):
        pass


class UserManager:
    """A mocked user manager with one-shot activation failure support."""

    def __init__(self):
        self.commands = []
        self.enabled = False
        self.active = False
        self.fail_activation = False

    def __call__(self, command, **_kwargs):
        self.commands.append(command)
        action = command[2]
        if action in ("is-enabled", "is-active"):
            value = self.enabled if action == "is-enabled" else self.active
            return subprocess.CompletedProcess(command, 0 if value else 1, "", "")
        if action == "enable":
            if self.fail_activation:
                self.fail_activation = False
                return subprocess.CompletedProcess(command, 1, "", "fixture activation failure")
            self.enabled = True
            if "--now" in command:
                self.active = True
        if action == "disable":
            self.enabled = False
            if "--now" in command:
                self.active = False
        if action in ("start", "stop"):
            self.active = action == "start"
        return subprocess.CompletedProcess(command, 0, "", "")


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        config = {**copy.deepcopy(DEFAULTS), "name": "same project"}
        self.project = Project(self.directory / "repo one", config, "standalone")
        self.machine = MachineConfig({"execution_mode": "standalone", "python": str(self.directory / "python"),
                                      "state_dir": str(self.directory / "state")})
        self.context = SimpleNamespace(project=self.project, machine=self.machine)
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.directory / "config")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.machine_environment = os.environ.pop("WPO_MACHINE_CONFIG", None)
        if self.machine_environment is not None:
            self.addCleanup(os.environ.__setitem__, "WPO_MACHINE_CONFIG", self.machine_environment)
        self.manager = UserManager()
        self.processes = mock.patch("wpo.audit.subprocess.run", side_effect=self.manager)
        self.processes.start()
        self.addCleanup(self.processes.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def change(self, install=True, show_only=False, platform="linux"):
        with mock.patch("wpo.audit.sys.platform", platform):
            return audit.change_schedule(self.context, install, show_only)

    def test_project_identity_prevents_same_name_collisions(self):
        other = SimpleNamespace(project=Project(self.directory / "repo two", self.project.config), machine=self.machine)
        self.assertNotEqual(audit.task_name(self.context), audit.task_name(other))
        self.assertEqual(audit.task_name(self.context), audit.task_name(self.context))
        self.assertLess(len(audit.task_name(self.context)), 100)

    def test_time_and_day_are_validated_without_newline_framing(self):
        for day, time in [("MONDAY", "09:00"), ("MON\n", "09:00"), ([], "09:00"), ("MON", "24:00"),
                          ("MON", "09:00\n"), ("MON", "9:00"), ("MON", None)]:
            with self.subTest(day=day, time=time):
                self.project.config["audit"]["schedule"] = {"day": day, "time": time}
                with self.assertRaises(audit.AuditError):
                    audit.linux_plan(self.context, True)
        self.assertEqual(self.manager.commands, [])

    def test_systemd_tokens_escape_quotes_percent_variables_and_backslashes(self):
        self.assertEqual(audit.systemd_quote('a "b"\\c%$HOME', command=True), '"a \\"b\\"\\\\c%%$$HOME"')
        self.assertEqual(audit.systemd_quote("a%$HOME"), '"a%%$HOME"')
        for value in ("a\nb", "a\rb", "a\x00b", "a\tb"):
            with self.subTest(value=value), self.assertRaises(audit.AuditError):
                audit.systemd_quote(value)

    def test_linux_plan_has_persistent_weekly_timer_and_explicit_backend_marker(self):
        plan = audit.linux_plan(self.context, True)
        timer = plan.files[f"{plan.name}.timer"]
        service = plan.files[f"{plan.name}.service"]
        self.assertIn("OnCalendar=Mon *-*-* 09:00:00\nPersistent=true\n", timer)
        self.assertIn("WantedBy=timers.target", timer)
        self.assertIn("Type=oneshot", service)
        self.assertIn('"--from-os-schedule"', service)
        self.assertIn(audit.systemd_quote(str(self.project.root)), service)
        self.assertEqual(plan.directory, self.directory / "config" / "systemd" / "user")

    def test_linux_preserves_explicit_machine_configuration(self):
        with mock.patch.dict(os.environ, {"WPO_MACHINE_CONFIG": str(self.directory / "config %file.json")}):
            plan = audit.linux_plan(self.context, True)
        self.assertIn("Environment=\"WPO_MACHINE_CONFIG=", plan.files[f"{plan.name}.service"])
        self.assertIn("config %%file.json", plan.files[f"{plan.name}.service"])

    def test_relative_xdg_config_is_rejected(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative"}), self.assertRaises(audit.AuditError):
            audit.linux_plan(self.context, True)

    def test_control_character_command_is_rejected(self):
        with mock.patch.object(self.machine.__class__, "wpo_command", return_value=["/python", "bad\ncommand"]), self.assertRaises(audit.AuditError):
            audit.linux_plan(self.context, True)

    def test_show_only_writes_nothing_and_invokes_no_scheduler(self):
        before = list(self.directory.rglob("*"))
        self.assertEqual(self.change(show_only=True), 0)
        self.assertEqual(list(self.directory.rglob("*")), before)
        self.assertEqual(self.manager.commands, [])
        self.assertEqual(self.change(install=False, show_only=True), 0)
        self.assertEqual(list(self.directory.rglob("*")), before)

    def test_linux_install_is_idempotent_and_remove_is_bounded(self):
        self.assertEqual(self.change(), 0)
        plan = audit.linux_plan(self.context, True)
        unrelated = plan.directory / "unrelated.timer"
        unrelated.write_text("do not touch", encoding="utf-8")
        command_count = len(self.manager.commands)
        self.assertEqual(self.change(), 0)
        self.assertEqual(len(self.manager.commands), command_count)
        self.assertEqual(self.change(install=False), 0)
        self.assertEqual(unrelated.read_text(), "do not touch")
        self.assertFalse(plan.receipt.exists())
        self.assertFalse((plan.directory / f"{plan.name}.timer").exists())
        self.assertFalse(self.manager.enabled)
        self.assertFalse(self.manager.active)
        self.assertEqual(self.change(install=False), 0)

    def test_linux_drift_and_unowned_files_are_never_overwritten_or_removed(self):
        self.change()
        plan = audit.linux_plan(self.context, True)
        timer = plan.directory / f"{plan.name}.timer"
        timer.write_text("personal edit", encoding="utf-8")
        calls = len(self.manager.commands)
        for install in (True, False):
            with self.subTest(install=install), self.assertRaisesRegex(audit.AuditError, "drifted"):
                self.change(install=install)
        self.assertEqual(len(self.manager.commands), calls)
        plan.receipt.unlink()
        with self.assertRaisesRegex(audit.AuditError, "unowned"):
            self.change()
        self.assertEqual(timer.read_text(), "personal edit")

    def test_failed_reinstall_restores_files_receipt_and_manager_state(self):
        self.change()
        plan = audit.linux_plan(self.context, True)
        originals = {name: (plan.directory / name).read_bytes() for name in [*plan.files, plan.receipt.name]}
        self.project.config["audit"]["schedule"]["time"] = "10:30"
        self.manager.fail_activation = True
        with self.assertRaisesRegex(audit.AuditError, "prior files restored"):
            self.change()
        for name, data in originals.items():
            self.assertEqual((plan.directory / name).read_bytes(), data)
        self.assertTrue(self.manager.enabled)
        self.assertTrue(self.manager.active)
        backups = plan.directory / ".wpo-schedule-backups" / plan.name
        self.assertEqual(len(list(backups.iterdir())), 1)

    def test_failed_first_install_leaves_no_registration(self):
        self.manager.fail_activation = True
        with self.assertRaises(audit.AuditError):
            self.change()
        plan = audit.linux_plan(self.context, True)
        self.assertFalse(plan.receipt.exists())
        self.assertFalse(any((plan.directory / name).exists() for name in plan.files))
        self.assertFalse(self.manager.enabled)
        self.assertFalse(self.manager.active)

    def test_failed_removal_restores_previous_files_and_manager_state(self):
        self.change()
        plan = audit.linux_plan(self.context, True)
        originals = {name: (plan.directory / name).read_bytes() for name in [*plan.files, plan.receipt.name]}
        failed = False

        def fail_reload(command, **kwargs):
            nonlocal failed
            if command[2] == "daemon-reload" and not failed:
                failed = True
                return subprocess.CompletedProcess(command, 1, "", "fixture reload failure")
            return self.manager(command, **kwargs)

        with mock.patch("wpo.audit.subprocess.run", side_effect=fail_reload), self.assertRaises(audit.AuditError):
            self.change(install=False)
        for name, data in originals.items():
            self.assertEqual((plan.directory / name).read_bytes(), data)
        self.assertTrue(self.manager.enabled)
        self.assertTrue(self.manager.active)

    def test_owned_symlink_is_not_followed_or_removed(self):
        self.change()
        plan = audit.linux_plan(self.context, True)
        timer = plan.directory / f"{plan.name}.timer"
        target = self.directory / "personal.txt"
        target.write_text("keep me", encoding="utf-8")
        timer.unlink()
        try:
            timer.symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        with self.assertRaisesRegex(audit.AuditError, "symlink"):
            self.change(install=False)
        self.assertTrue(timer.is_symlink())
        self.assertEqual(target.read_text(), "keep me")

    def test_shadowed_user_unit_is_rejected(self):
        def shadow(command, **kwargs):
            if command[2] == "list-unit-files":
                return subprocess.CompletedProcess(command, 0, f"{command[3]} enabled", "")
            if command[2] == "show":
                return subprocess.CompletedProcess(command, 0, str(self.directory / "another.timer"), "")
            return self.manager(command, **kwargs)
        with mock.patch("wpo.audit.subprocess.run", side_effect=shadow), self.assertRaisesRegex(audit.AuditError, "shadows"):
            self.change()
        self.assertFalse((self.directory / "config").exists())

    def test_t3_plan_uses_native_schedule_and_never_changes_os_resources(self):
        self.context.machine = MachineConfig({**self.machine.raw, "execution_mode": "t3"})
        before = list(self.directory.rglob("*"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self.change(show_only=True), 0)
        self.assertIn("type:'fixed_time'", output.getvalue())
        self.assertIn("weekdays:[1]", output.getvalue())
        self.assertIn("bindToCurrentThread=true", output.getvalue())
        for install in (True, False):
            with self.assertRaisesRegex(audit.AuditError, "native T3"):
                self.change(install=install)
        self.assertEqual(self.manager.commands, [])
        self.assertEqual(list(self.directory.rglob("*")), before)

    def test_unsupported_platform_has_actionable_error(self):
        with self.assertRaisesRegex(audit.AuditError, "unsupported on darwin"):
            self.change(platform="darwin", show_only=True)
        self.assertEqual(self.manager.commands, [])

    def test_windows_first_install_never_overwrites_an_unknown_task(self):
        xml = "<Task>fixture</Task>"
        responses = [subprocess.CompletedProcess([], 1, "", "not found"), subprocess.CompletedProcess([], 0, "", ""),
                     subprocess.CompletedProcess([], 0, xml, "")]
        with mock.patch("wpo.audit.subprocess.run", side_effect=responses) as run:
            self.assertEqual(self.change(platform="win32"), 0)
        self.assertNotIn("/F", run.call_args_list[1].args[0])
        with mock.patch("wpo.audit.subprocess.run", return_value=subprocess.CompletedProcess([], 0, xml + "edit", "")), self.assertRaisesRegex(audit.AuditError, "drifted"):
            self.change(install=False, platform="win32")

    def test_windows_unowned_task_is_not_removed(self):
        with mock.patch("wpo.audit.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "<Task/>", "")) as run, self.assertRaisesRegex(audit.AuditError, "unowned"):
            self.change(install=False, platform="win32")
        self.assertEqual(run.call_count, 1)

    def test_windows_failed_replacement_restores_previous_task_xml(self):
        xml = "<Task>original</Task>"
        success = subprocess.CompletedProcess([], 0, "", "")
        responses = [subprocess.CompletedProcess([], 1, "", "missing"), success,
                     subprocess.CompletedProcess([], 0, xml, "")]
        with mock.patch("wpo.audit.subprocess.run", side_effect=responses):
            self.change(platform="win32")
        receipt = self.directory / "state" / "schedules" / f".{audit.task_name(self.context)}.json"
        previous_receipt = receipt.read_bytes()
        self.project.config["audit"]["schedule"]["time"] = "11:20"
        seen_xml = []

        def failed_replace(command, **_kwargs):
            if command[1] == "/Query":
                return subprocess.CompletedProcess(command, 0, xml, "")
            if "/XML" in command:
                seen_xml.append(Path(command[command.index("/XML") + 1]).read_text(encoding="utf-16"))
                return success
            return subprocess.CompletedProcess(command, 1, "", "fixture failed replacement")

        with mock.patch("wpo.audit.subprocess.run", side_effect=failed_replace), self.assertRaisesRegex(audit.AuditError, "registration restored"):
            self.change(platform="win32")
        self.assertEqual(seen_xml, [xml])
        self.assertEqual(receipt.read_bytes(), previous_receipt)

    def test_windows_environment_override_cannot_make_a_misleading_plan(self):
        with mock.patch.dict(os.environ, {"WPO_MACHINE_CONFIG": "private.json"}), self.assertRaisesRegex(audit.AuditError, "cannot inherit"):
            self.change(show_only=True, platform="win32")
        self.assertEqual(self.manager.commands, [])

    def test_cli_schedule_path_avoids_context_router_and_dashboard(self):
        with mock.patch("wpo.machine.MachineConfig.load", return_value=self.machine), \
             mock.patch("wpo.project.Project.locate", return_value=self.project), \
             mock.patch("wpo.context.Context.load", side_effect=AssertionError("must not load Context")), \
             mock.patch("wpo.dashboard.autostart", side_effect=AssertionError("must not start dashboard")), \
             mock.patch("sys.stdout", ReconfigurableOutput()), mock.patch("wpo.audit.sys.platform", "linux"):
            self.assertEqual(cli.main(["audit", "--install-schedule", "--show-only"]), 0)
        self.assertEqual(self.manager.commands, [])
        self.assertEqual(list(self.directory.rglob("*")), [])

    def test_old_os_schedule_cannot_switch_to_t3_on_next_run(self):
        machine = MachineConfig({"execution_mode": "t3"})
        with mock.patch("wpo.machine.MachineConfig.load", return_value=machine), \
             mock.patch("wpo.context.Context.load", side_effect=AssertionError("must not load Context")), \
             mock.patch("sys.stdout", ReconfigurableOutput()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["audit", "--from-os-schedule"]), 2)

    def test_boot_error_is_actionable_at_entrypoint(self):
        from wpo.boot import BootError
        output = io.StringIO()
        with mock.patch("wpo.cli.main", side_effect=BootError("fixture boot changed")), contextlib.redirect_stderr(output):
            self.assertEqual(cli.entrypoint(), 8, "not 4, the no-route code a supervisor sleeps on")
        self.assertIn("Boot safety: fixture boot changed", output.getvalue())

    def test_receipt_cannot_authorize_another_project(self):
        self.change()
        plan = audit.linux_plan(self.context, True)
        receipt = json.loads(plan.receipt.read_text())
        receipt["project"] = str(self.directory / "other")
        plan.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(audit.AuditError, "ownership"):
            self.change(install=False)


if __name__ == "__main__":
    unittest.main()
