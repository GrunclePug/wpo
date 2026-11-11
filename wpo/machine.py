"""Machine-wide settings: executables, the model proxy, the local model, API keys, router state.

They live in `~/.claude/wp-orchestration.json` because they describe this computer, not a project.
Every key is optional; missing keys take the defaults below. Paths may use `~`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import ENTRY_POINT

# WPO_MACHINE_CONFIG points tests (and their subprocesses) at a scratch machine configuration.
MACHINE_CONFIG_PATH = Path(os.environ.get("WPO_MACHINE_CONFIG", Path.home() / ".claude" / "wp-orchestration.json"))

DEFAULTS: dict = {
    # Standalone runs the legacy CLI supervisors. T3 uses its own tools and worktree ownership.
    "execution_mode": "t3",
    "t3": {
        "cli": "t3",
        "base_dir": None,
        "coordinator_thread_id": None,
        "project_id": None,
        "providers": {"gpt": "codex", "claude": "claudeAgent", "free": "opencode", "agy": "antigravity", "xai": "grok"},
        "models": {"sol": "gpt-6.1-sol", "terra": "gpt-6.1-terra", "luna": "gpt-6-luna", "astra": "gpt-6-astra",
                   "haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"},
        "options": {},
        "wake_debounce_seconds": 5,
        "callback_interval_seconds": 0.5,
        "coordinator_idle_minutes": 30,
    },
    # The interpreter that runs the tooling (needs psutil); detached jobs and workers use it too.
    "python": sys.executable,
    "claude": "claude",
    "agy": "agy",
    "opencode": "opencode",
    # Router catalog, cooldowns and other state shared by every project on this machine.
    "state_dir": "~/.local/state/wp-orchestration",
    "reviewer_agent": "~/.config/wp-orchestration/reviewer.md",
    # Machine-wide preferences; project configuration takes precedence over both objects.
    "project_defaults": {},
    "routing": {},
    # Where session briefs find skills by name: `paths` maps a name to its SKILL.md (plugin skills
    # live in nested directories), else `<dir>/<name>/SKILL.md` in the first dir that has it.
    "skills": {"dirs": ["~/.agents/skills", "~/.codex/skills", "~/.claude/skills"], "paths": {}},
    # Sections of the user's global instructions carried into every session brief, since
    # sessions here skip CLAUDE.md discovery.
    "user_rules": {"file": "~/.config/wp-orchestration/AGENTS.md", "sections": []},
    # Where `wpo install-shim` puts the `wpo` command (must be on PATH for agents and the user).
    "shim_dir": "~/.local/bin",
    "usage": {
        "script": "~/.config/wp-orchestration/usage_budget.py",
        "cache": "~/.local/state/wp-orchestration/usage-budget-cache.json",
        # Every reading usage_budget.py fetched, one JSON row per window: the burn-rate source.
        "history": "~/.local/state/wp-orchestration/usage-budget-history.jsonl",
        "max_age_seconds": 300,
    },
    "keys": {
        "openrouter": "~/.config/wp-orchestration/keys/openrouter",
        "artificial_analysis": "~/.config/wp-orchestration/keys/artificial-analysis",
    },
    # GPT routes run the Claude CLI against a loopback CLIProxyAPI with Codex OAuth.
    "gpt_proxy": {
        "port": 8317,
        "token_file": "~/.config/wp-orchestration/proxy/client-token",
        "pins_file": "~/.config/wp-orchestration/proxy/model-pins.json",
        "executable": "cli-proxy-api",
        "config": "~/.config/wp-orchestration/proxy/config.yaml",
        "background_tier": "luna",
        "background_effort": "low",
        # Optional context window corrections per GPT tier when the pins registry lags a backend.
        "context_tokens": {},
    },
    # The progress dashboard (dashboard.py): every `wpo` command makes sure one server runs per
    # project, bound to 127.0.0.1. `port` serves the first project; give each further project on
    # this machine its own entry in `project_ports` (main checkout path -> port).
    "dashboard": {
        "autostart": True,
        "port": 8470,
        "project_ports": {},
    },
    # The local model: a server behind a compaction shim speaking the Anthropic API. A server that
    # speaks the Anthropic API itself and compacts on its own uses the same port for both.
    "local_model": {
        "model_port": 30000,
        "shim_port": 8318,
        "token_file": "~/.config/wp-orchestration/local-model-token",
        "max_context_tokens": 140000,
        "max_output_tokens": 32768,
        # One session at a time across every project and supervisor.
        "mutex": "Global\\WpoLocalModel",
    },
}


def _expand(value: str) -> Path:
    return Path(value).expanduser()


def _merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        merged[key] = _merge(base[key], value) if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return merged


class ExecutionModeError(RuntimeError):
    """A standalone engine operation was requested while T3 owns execution."""


@dataclass(frozen=True)
class MachineConfig:
    """Resolved machine settings; `raw` keeps the merged JSON for the less common keys."""

    raw: dict

    @classmethod
    def load(cls, path: Path | None = None) -> MachineConfig:
        path = path or Path(os.environ.get("WPO_MACHINE_CONFIG", MACHINE_CONFIG_PATH))
        override = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        config = cls(_merge(DEFAULTS, override))
        if config.execution_mode not in ("standalone", "t3"):
            raise ValueError(f"{path}: execution_mode must be 'standalone' or 't3'")
        return config

    @property
    def execution_mode(self) -> str:
        return self.raw.get("execution_mode", "t3")

    def require_standalone(self, action: str) -> None:
        if self.execution_mode == "t3":
            raise ExecutionModeError(
                f"{action} belongs to WPO's standalone engine; execution_mode=t3 requires "
                "T3 tools with isolation='worktree'. See references/t3.md."
            )

    @property
    def python(self) -> str:
        return str(_expand(self.raw["python"]))

    def executable(self, name: str) -> str:
        """Resolves the `claude` or `opencode` executable to an absolute path."""
        configured = self.raw[name]
        candidate = _expand(configured)
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(configured)
        if found is None:
            raise FileNotFoundError(f"'{configured}' ({name}) is neither a file nor on PATH; set '{name}' in {MACHINE_CONFIG_PATH}")
        return found

    @property
    def state_dir(self) -> Path:
        directory = _expand(self.raw["state_dir"])
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @property
    def reviewer_agent(self) -> Path:
        return _expand(self.raw["reviewer_agent"])

    @property
    def shim_dir(self) -> Path:
        return _expand(self.raw["shim_dir"])

    @property
    def usage_script(self) -> Path:
        return _expand(self.raw["usage"]["script"])

    @property
    def usage_cache(self) -> Path:
        return _expand(self.raw["usage"]["cache"])

    @property
    def usage_history(self) -> Path:
        return _expand(self.raw["usage"]["history"])

    @property
    def usage_max_age_seconds(self) -> float:
        return float(self.raw["usage"]["max_age_seconds"])

    def key_file(self, name: str) -> Path:
        return _expand(self.raw["keys"][name])

    def gpt(self, key: str) -> object:
        value = self.raw["gpt_proxy"][key]
        if key == "executable":
            candidate = _expand(value)
            return candidate if candidate.is_file() else Path(shutil.which(value) or candidate)
        return _expand(value) if key in ("token_file", "pins_file", "config") else value

    def local(self, key: str) -> object:
        value = self.raw["local_model"][key]
        return _expand(value) if key == "token_file" else value

    @property
    def dashboard_autostart(self) -> bool:
        return bool(self.raw["dashboard"]["autostart"])

    def dashboard_port(self, project_root: Path) -> int:
        """The dashboard's port for one project: its `project_ports` entry, else `port`."""
        settings = self.raw["dashboard"]
        for root, port in settings["project_ports"].items():
            if _expand(root).resolve() == project_root.resolve():
                return int(port)
        return int(settings["port"])

    def wpo_command(self) -> list[str]:
        """How to run this tooling as a subprocess, from any working directory."""
        return [self.python, "-I", "-B", str(ENTRY_POINT)]
