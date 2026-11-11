"""Mechanical fixes applied to a WP's changed files before verification, so formatting and
auto-fixable lint never cost a builder round.

Fixers come from the project config's `autofix` object: one named entry per fixer, merged key by
key over the skill's defaults (project.py), so a project overrides one fixer, adds its own, or
disables one with `null`. Each entry names the files it applies to (`globs`, minus `exclude`) and
either a `command` (the matching files are appended as arguments, as pre-commit does) or a
`builtin`. Fixers run in config order: formatters before linters.

Only files the WP changed and owns are touched (never shared files such as lock files). A fixer
that fails is reported, not fatal: Verify, which runs next, is the gate.

    reports = run_fixers(tree, files, configured_fixers(config), env, timeout_minutes)
"""

from __future__ import annotations

import fnmatch
import hashlib
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .platform import environment_with, kill_tree
from .wpfile import glob_to_regex

# Windows command lines stop at 32,767 characters; files are passed in batches under this.
COMMAND_LINE_BUDGET = 24000
BINARY_SNIFF_BYTES = 8192
OUTPUT_TAIL_LINES = 15


class AutofixConfigError(Exception):
    """An `autofix` entry that cannot run as written."""


def strip_trailing_whitespace(content: bytes) -> bytes:
    """Removes spaces and tabs before each line end, keeping CRLF or LF as found."""
    lines = content.split(b"\n")
    stripped = []
    for line in lines:
        carriage = line.endswith(b"\r")
        body = line[:-1] if carriage else line
        stripped.append(body.rstrip(b" \t") + (b"\r" if carriage else b""))
    return b"\n".join(stripped)


def fix_end_of_file(content: bytes) -> bytes:
    """Ends a file with exactly one newline in the file's own style; a file of only newlines empties."""
    stripped = content.rstrip(b"\r\n")
    if not stripped:
        return b""
    return stripped + (b"\r\n" if b"\r\n" in content else b"\n")


BUILTINS: dict[str, Callable[[bytes], bytes]] = {
    "trailing-whitespace": strip_trailing_whitespace,
    "end-of-file": fix_end_of_file,
}


@dataclass(frozen=True)
class Fixer:
    name: str
    globs: tuple[str, ...]
    exclude: tuple[str, ...]
    command: tuple[str, ...] | None
    builtin: str | None

    def applies_to(self, path: str) -> bool:
        return matches(path, self.globs) and not matches(path, self.exclude)


def matches(path: str, patterns: Iterable[str]) -> bool:
    """A pattern without '/' matches the file name (`*.py`); one with '/' matches the repo-relative
    path, '**' crossing directories (`crates/**/*.rs`)."""
    name = PurePosixPath(path).name
    return any(glob_to_regex(pattern).match(path) if "/" in pattern else fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def configured_fixers(config: Mapping) -> list[Fixer]:
    """The enabled fixers in config order; a `null` entry is a fixer the project switched off."""
    fixers = []
    for name, entry in config["autofix"].items():
        if entry is None:
            continue
        command, builtin = entry.get("command"), entry.get("builtin")
        if (command is None) == (builtin is None):
            raise AutofixConfigError(f"autofix '{name}' needs exactly one of 'command' (a list) or 'builtin'")
        if command is not None and (not isinstance(command, list) or not command):
            raise AutofixConfigError(f"autofix '{name}': 'command' must be a non-empty list of arguments")
        if builtin is not None and builtin not in BUILTINS:
            raise AutofixConfigError(f"autofix '{name}': unknown builtin '{builtin}' (known: {', '.join(BUILTINS)})")
        globs = entry.get("globs")
        if not isinstance(globs, list) or not globs:
            raise AutofixConfigError(f"autofix '{name}': 'globs' must be a non-empty list")
        fixers.append(Fixer(name, tuple(globs), tuple(entry.get("exclude", [])), tuple(command) if command else None, builtin))
    return fixers


def digest(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest() if path.is_file() else ""


def batches(files: list[str], budget: int) -> Iterable[list[str]]:
    batch: list[str] = []
    used = 0
    for file in files:
        if batch and used + len(file) + 3 > budget:
            yield batch
            batch, used = [], 0
        batch.append(file)
        used += len(file) + 3
    if batch:
        yield batch


def run_builtin(tree: Path, fixer: Fixer, files: list[str]) -> str | None:
    """Rewrites each text file the transform changes; binary files are skipped. Never fails."""
    transform = BUILTINS[fixer.builtin]
    for file in files:
        path = tree / file
        content = path.read_bytes()
        if b"\0" in content[:BINARY_SNIFF_BYTES]:
            continue
        fixed = transform(content)
        if fixed != content:
            path.write_bytes(fixed)
    return None


def run_command(tree: Path, fixer: Fixer, files: list[str], env: Mapping[str, str], timeout_minutes: float) -> str | None:
    """Runs the command over the files in batches; returns a failure note, or None."""
    for batch in batches(files, COMMAND_LINE_BUDGET - sum(len(argument) + 1 for argument in fixer.command)):
        try:
            process = subprocess.Popen([*fixer.command, *batch], cwd=str(tree), env=environment_with(env), stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        except OSError as error:
            return f"could not start ({error})"
        try:
            output, _ = process.communicate(timeout=timeout_minutes * 60)
        except subprocess.TimeoutExpired:
            kill_tree(process.pid)
            process.communicate()
            return f"killed after {timeout_minutes:g} min"
        if process.returncode != 0:
            tail = "\n".join((output or "").splitlines()[-OUTPUT_TAIL_LINES:])
            return f"exited {process.returncode}: {tail}"
    return None


def run_fixers(tree: Path, files: Iterable[str], fixers: list[Fixer], env: Mapping[str, str], timeout_minutes: float) -> list[str]:
    """Applies each fixer to the files it covers; returns one report line per fixer that changed
    files or failed (nothing for a fixer with no work)."""
    files = sorted(set(files))
    reports = []
    for fixer in fixers:
        targets = [file for file in files if fixer.applies_to(file) and (tree / file).is_file()]
        if not targets:
            continue
        before = {file: digest(tree / file) for file in targets}
        failure = run_command(tree, fixer, targets, env, timeout_minutes) if fixer.command else run_builtin(tree, fixer, targets)
        changed = [file for file in targets if digest(tree / file) != before[file]]
        if changed:
            reports.append(f"{fixer.name} fixed {len(changed)} file(s): {', '.join(changed)}")
        if failure:
            reports.append(f"{fixer.name} {failure}")
    return reports
