"""Fixtures shared by the native (T3) bridge tests: a scratch project with an attached coordinator."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wpo import session
from wpo.context import Context
from wpo.events import Journal
from wpo.machine import DEFAULTS as MACHINE_DEFAULTS
from wpo.machine import MachineConfig
from wpo.project import DEFAULTS as PROJECT_DEFAULTS
from wpo.project import Project, git
from wpo.routing import Router
from wpo.session_brief import SessionBrief
from wpo.t3 import Bridge

CAPABILITIES = {
    "parentThreadId": "coordinator", "providers": [
        {"providerInstanceId": "codex", "canRunChildTask": True, "models": [
            {"id": "gpt-6.1-sol", "options": [{"id": "reasoningEffort", "values": ["low", "high"]}]}
        ]},
        {"providerInstanceId": "claudeAgent", "canRunChildTask": False, "models": []},
    ],
}
ROUTE = {"id": "gpt-sol-high", "family": "gpt", "harness": "gpt", "model": "sol", "effort": "high", "cost_class": "subscription"}


def init_repository(root: Path, files: dict[str, str]) -> None:
    """A git repository at `root` whose first commit holds `files` (relative path: content)."""
    for arguments in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        git(root, *arguments)
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content, encoding="utf-8")
    git(root, "add", "--", *files)
    git(root, "commit", "-qm", "base")


class NativeTestCase(unittest.TestCase):
    """A scratch T3 project whose bridge is attached to the coordinator `coordinator`."""

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

    def settings(self, access: str = "builder", write_paths=("src/**",), stop_on_compaction: bool = False, **fields) -> session.SessionSettings:
        """A session's settings in the scratch root; `fields` are its event fields (default: its role)."""
        return session.SessionSettings(access, list(write_paths), self.root, SessionBrief(None, "", "rules"), {}, False, stop_on_compaction, 30,
                                       fields or {"role": access})

    def begin(self, log: Path, **payload) -> dict:
        """A session request begun under `log`, as `run_attempt` publishes it; `payload` overrides the defaults."""
        log.parent.mkdir(parents=True, exist_ok=True)
        defaults = {"wp": "WP-0001", "role": "builder", "prompt": "Task", "title": log.stem, "native_role": "implementation",
                    "target": self.bridge.target(ROUTE), "route": ROUTE, "log_path": str(log), "cwd": str(self.root), "access": "builder",
                    "write_paths": [], "before_tree": None, "stop_on_compaction": False}
        with mock.patch.object(Bridge, "wake"):
            return self.bridge.begin_session(log, {**defaults, **payload}, "coordinator")[0]
