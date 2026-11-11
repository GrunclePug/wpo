"""Exercise an installed wheel, including resources and subprocess entrypoints."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import wpo


def run(*arguments: str, cwd: Path, environment: dict[str, str]) -> str:
    result = subprocess.run(arguments, cwd=cwd, env=environment, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(f"Installed CLI failed ({result.returncode}): {result.stderr or result.stdout}")
    return result.stdout


def main() -> None:
    source = Path(__file__).resolve().parents[1]
    if wpo.PACKAGE_ROOT.is_relative_to(source):
        raise RuntimeError("Run this check against an installed wheel, outside the source import path")
    for name in ("SKILL.md", "dashboard/index.html", "dashboard/dashboard.js", "dashboard/dashboard.css",
                 "models/tiers.json", "models/scores.json", "templates/config.example.json",
                 "templates/WP-TEMPLATE.md", "references/t3-bridge.md", "bin/wpo.py"):
        if not (wpo.SKILL_ROOT / name).is_file():
            raise RuntimeError(f"Missing installed resource: {name}")
    with tempfile.TemporaryDirectory(prefix="wpo-installed-") as temporary:
        root = Path(temporary)
        machine = root / "machine.json"
        machine.write_text(json.dumps({"dashboard": {"autostart": False}}), encoding="utf-8")
        environment = {**os.environ, "WPO_MACHINE_CONFIG": str(machine)}
        run("git", "init", "-q", "-b", "main", cwd=root, environment=environment)
        for command in ([sys.executable, "-I", "-m", "wpo"], [sys.executable, "-I", "-B", str(wpo.ENTRY_POINT)]):
            if "t3" not in run(*command, "--help", cwd=root, environment=environment):
                raise RuntimeError("Installed CLI omitted the native backend")
        run(sys.executable, "-I", "-m", "wpo", "init", "--name", "installed-check", cwd=root, environment=environment)
        config = json.loads((root / ".orchestration/config.json").read_text(encoding="utf-8"))
        if config["name"] != "installed-check":
            raise RuntimeError("Installed project template was not applied")
    print("Installed wheel passed: resources, module/helper CLIs, and project initialization.")


if __name__ == "__main__":
    main()
