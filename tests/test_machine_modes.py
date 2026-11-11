"""Portable configuration and separation of T3 from the standalone engine."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wpo import SKILL_ROOT
from wpo.machine import ExecutionModeError, MachineConfig
from wpo.project import Project, ProjectError
from wpo.routing import Router


class MachineConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="wpo-config-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.config = self.root / "machine.json"

    def load(self, settings: dict) -> MachineConfig:
        self.config.write_text(json.dumps(settings), encoding="utf-8")
        return MachineConfig.load(self.config)

    def test_missing_file_has_no_personal_brief_requirements(self) -> None:
        machine = MachineConfig.load(self.config)
        self.assertEqual(machine.execution_mode, "t3")
        self.assertEqual(machine.raw["user_rules"]["sections"], [])
        self.assertIn("~/.agents/skills", machine.raw["skills"]["dirs"])
        with self.assertRaises(ExecutionModeError):
            machine.require_standalone("test")

    def test_machine_example_loads_and_nested_overrides_preserve_defaults(self) -> None:
        machine = MachineConfig.load(SKILL_ROOT / "templates" / "machine.example.json")
        self.assertEqual(machine.raw["project_defaults"]["builder_skills"], [])
        machine = self.load({"local_model": {"shim_port": 12345}})
        self.assertEqual(machine.local("shim_port"), 12345)
        self.assertEqual(machine.local("model_port"), 30000)

    def test_config_environment_override_is_resolved_at_load_time(self) -> None:
        self.load({"execution_mode": "t3"})
        with mock.patch.dict(os.environ, {"WPO_MACHINE_CONFIG": str(self.config)}):
            self.assertEqual(MachineConfig.load().execution_mode, "t3")

    def test_invalid_mode_is_reported_and_t3_refuses_legacy_execution(self) -> None:
        with self.assertRaisesRegex(ValueError, "execution_mode"):
            self.load({"execution_mode": "typo"})
        with self.assertRaisesRegex(ExecutionModeError, "isolation='worktree'"):
            self.load({"execution_mode": "t3"}).require_standalone("test")

    def test_proxy_executable_can_resolve_from_path_or_an_explicit_file(self) -> None:
        machine = self.load({"gpt_proxy": {"executable": "test-proxy"}})
        with mock.patch("wpo.machine.shutil.which", return_value=str(self.root / "proxy.exe")):
            self.assertEqual(machine.gpt("executable"), self.root / "proxy.exe")
        executable = self.root / "local-proxy.exe"
        executable.touch()
        machine = self.load({"gpt_proxy": {"executable": str(executable)}})
        self.assertEqual(machine.gpt("executable"), executable)

    def test_project_and_routing_precedence_preserves_machine_custom_routes(self) -> None:
        machine = self.load({
            "state_dir": str(self.root / "state"),
            "project_defaults": {"builder_skills": ["personal"], "routing": {"tiers": {"code": {"floor": 46}}}},
            "routing": {"tiers": {"code": {"floor": 44}}, "routes_extra": [
                {"id": "my-local-route", "family": "local", "harness": "local", "model": "my-model",
                 "score": 45, "writes_code": True}
            ]},
        })
        (self.root / ".orchestration").mkdir()
        project_json = self.root / ".orchestration" / "config.json"
        project_json.write_text(json.dumps({"builder_skills": ["project"], "routing": {"tiers": {"code": {"floor": 48}}}}))
        with mock.patch("wpo.project.main_checkout_of", return_value=self.root):
            project = Project.locate(self.root, machine.raw["project_defaults"])
        self.assertEqual(project.config["builder_skills"], ["project"])
        router = Router(machine, project.config["routing"])
        self.assertEqual(router.policy["tiers"]["code"]["floor"], 48)
        self.assertEqual(router.find_route("my-local-route").model, "my-model")
        with mock.patch("wpo.project.main_checkout_of", return_value=self.root), \
             self.assertRaisesRegex(ProjectError, "unknown keys"):
            Project.locate(self.root, {"misspelled_setting": True})

if __name__ == "__main__":
    unittest.main()
