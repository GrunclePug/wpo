"""Running project commands (Verify blocks, workspace verification, hooks) and reading a tree's changes."""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .platform import NO_COLOUR_ENV, NamedMutex, environment_with, kill_tree
from .project import git

FAILURE_TAIL_LINES = 40
DEFAULT_TIMEOUT_MINUTES = 120
BUILD_SLOT_POLL_SECONDS = 15
# Named mutexes <prefix><index> machine-wide; tests use their own prefix so live lanes do not hold theirs.
BUILD_SLOT_MUTEX_PREFIX = "wpo-build-slot-"
# Touched by a session's own waiting command (`wpo verify` queueing for a build slot), so the
# session watcher counts the wait as activity; it lives in the WP's run directory, next to the logs.
HEARTBEAT_FILE = "heartbeat"
# What a failing command's output loses before a model reads it (the log keeps everything): colour
# escapes, and the build tool's progress lines, which a workspace clippy prints by the hundred.
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PROGRESS_LINE = re.compile(r"^\s*(Compiling|Checking|Fresh|Downloading|Downloaded|Updating|Locking|Adding|Blocking|Building|Finished)\s")


def condensed(output: str) -> str:
    """A command's output as a model should read it: no colour escapes, no build progress lines."""
    return "\n".join(line for line in ANSI_ESCAPE.sub("", output).splitlines() if not PROGRESS_LINE.match(line))


@contextmanager
def build_slot(slots: int, heartbeat: Path | None, on_wait: Callable[[str], None]) -> Iterator[None]:
    """Holds one of `slots` machine-wide build slots for the block, waiting while all are taken;
    0 slots means no limit. While waiting it touches `heartbeat`, so a session whose command
    waits is not taken for hung."""
    if slots <= 0:
        yield
        return
    mutexes = [NamedMutex(f"{BUILD_SLOT_MUTEX_PREFIX}{index}") for index in range(slots)]
    waiting_since: float | None = None
    while True:
        for mutex in mutexes:
            if mutex.acquire(blocking=False):
                if waiting_since is not None:
                    on_wait(f"build slot free after {(time.monotonic() - waiting_since) / 60:.0f} min")
                try:
                    yield
                finally:
                    mutex.release()
                return
        if waiting_since is None:
            waiting_since = time.monotonic()
            on_wait(f"all {slots} build slots are busy; waiting for one")
        if heartbeat is not None:
            heartbeat.parent.mkdir(parents=True, exist_ok=True)
            heartbeat.touch()
        time.sleep(BUILD_SLOT_POLL_SECONDS)


def infrastructure_failure(failures: list["CommandFailure"], markers: Iterable[str]) -> bool:
    """Whether every failure carries a marker of the machine's fault (a killed command, a full disk,
    memory pressure), so the code itself was never judged."""
    markers = list(markers)
    return bool(failures) and all(any(marker in failure.output for marker in markers) for failure in failures)


@dataclass(frozen=True)
class CommandFailure:
    """One failing command with its whole output."""

    command: str
    exit_code: int
    output: str

    @property
    def text(self) -> str:
        tail = "\n".join(self.output.splitlines()[-FAILURE_TAIL_LINES:])
        return f"`{self.command}` exited {self.exit_code}. Last output:\n{tail}"


def run_commands(shell: Iterable[str], commands: Iterable[str], cwd: Path, env: Mapping[str, str], log_path: Path,
                 timeout_minutes: float = DEFAULT_TIMEOUT_MINUTES) -> list[str]:
    """Runs each command through the project shell, logging output; returns one failure text per
    failing command (all commands run, so one report shows every failure)."""
    return [failure.text for failure in run_commands_detailed(shell, commands, cwd, env, log_path, timeout_minutes)]


def run_commands_detailed(shell: Iterable[str], commands: Iterable[str], cwd: Path, env: Mapping[str, str], log_path: Path,
                          timeout_minutes: float = DEFAULT_TIMEOUT_MINUTES) -> list[CommandFailure]:
    """`run_commands` with each failure's command and whole output. A command running past the
    timeout is killed with its process tree and fails: a hung test must not hold the integration
    mutex forever."""
    failures = []
    environment = environment_with({**NO_COLOUR_ENV, **env})
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for command in commands:
        process = subprocess.Popen([*shell, command], cwd=str(cwd), env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace")
        try:
            output, _ = process.communicate(timeout=timeout_minutes * 60)
            code = process.returncode
        except subprocess.TimeoutExpired:
            kill_tree(process.pid)
            output, _ = process.communicate()
            output = (output or "") + f"\n(killed after {timeout_minutes:g} min)"
            code = -1
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"### {command}\n{output}\n### exit {code}\n")
        if code != 0:
            failures.append(CommandFailure(command, code, condensed(output or "")))
    return failures


def worktree_tree_id(tree: Path) -> str:
    """The git tree id of the working tree as it would be committed (`git add -A`), computed in a
    scratch index so the real index is untouched. Integration compares it with what it commits."""
    with tempfile.TemporaryDirectory() as scratch:
        env = environment_with({"GIT_INDEX_FILE": str(Path(scratch) / "index")})
        for arguments in (["read-tree", "HEAD"], ["add", "-A"], ["write-tree"]):
            completed = subprocess.run(["git", "-C", str(tree), *arguments], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if completed.returncode != 0:
                raise RuntimeError(f"git {' '.join(arguments)} in {tree} failed: {completed.stderr.strip()}")
        return completed.stdout.strip()


def matches_main(tree: Path, main_branch: str, changed_path: str) -> bool:
    """Whether a changed path (as `changed_paths` lists it) is exactly as main has it now: main
    moved since the branch point and the tree holds main's version (a catch-up merge, or a shared
    setting the orchestrator copied in). Such a path is not part of the WP's change."""
    path = changed_path.removesuffix(" (deleted)")
    in_main = git(tree, "rev-parse", "-q", "--verify", f"{main_branch}:{path}", check=False)
    if changed_path.endswith(" (deleted)"):
        return in_main.returncode != 0
    if in_main.returncode != 0 or not (tree / path).is_file():
        return False
    return git(tree, "hash-object", "--", path).stdout.strip() == in_main.stdout.strip()


def changed_paths(tree: Path, main_branch: str) -> list[str]:
    """Every path the worktree changed relative to where it left main: committed or not, tracked or
    untracked (ignored files excluded). Deleted paths carry a ' (deleted)' suffix."""
    base = git(tree, "merge-base", "HEAD", main_branch).stdout.strip()
    changed: dict[str, bool] = {}
    for line in git(tree, "-c", "core.quotepath=off", "diff", "--name-status", "--no-renames", base).stdout.splitlines():
        status, _, path = line.partition("\t")
        changed[path] = status.startswith("D")
    for path in git(tree, "-c", "core.quotepath=off", "ls-files", "--others", "--exclude-standard").stdout.splitlines():
        changed[path] = False
    return sorted(f"{path} (deleted)" if deleted else path for path, deleted in changed.items())
