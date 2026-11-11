"""Operating-system primitives: locks, machine-wide mutexes, detached processes, process trees.

Windows is the primary target; Linux works through the same functions.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

import psutil

# Colour off for every tool a session or a check runs: escape codes cost a model several tokens per
# line and carry nothing it reads (git diffs came through as "[0m[32m+").
NO_COLOUR_ENV = {"NO_COLOR": "1", "CLICOLOR": "0", "CARGO_TERM_COLOR": "never",
                 "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "color.ui", "GIT_CONFIG_VALUE_0": "never"}

IS_WINDOWS = sys.platform == "win32"
LOCK_RETRY_SECONDS = 0.1

if IS_WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _WAIT_OBJECT_0, _WAIT_ABANDONED, _WAIT_TIMEOUT, _INFINITE = 0x0, 0x80, 0x102, 0xFFFFFFFF
    _DETACHED_PROCESS = 0x00000008
    _CREATE_NEW_PROCESS_GROUP = 0x00000200
    _CREATE_NO_WINDOW = 0x08000000
    _CREATE_BREAKAWAY_FROM_JOB = 0x01000000
else:
    import fcntl


class FileLock:
    """An exclusive lock on a sidecar file, held for a `with` block; serializes appends and updates."""

    def __init__(self, path: Path, timeout_seconds: float = 30) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.handle = None

    def __enter__(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                if IS_WINDOWS:
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    raise TimeoutError(f"could not lock {self.path} within {self.timeout_seconds:g} s")
                time.sleep(LOCK_RETRY_SECONDS)

    def __exit__(self, *_exc: object) -> None:
        if IS_WINDOWS:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


class NamedMutex:
    """A machine-wide mutex shared with any process using the same name (a Windows named mutex, a
    lock file elsewhere). Acquire and release on the same thread."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._handle = None
        self._file = None

    def acquire(self, blocking: bool = True) -> bool:
        if IS_WINDOWS:
            handle = _kernel32.CreateMutexW(None, False, self.name)
            if not handle:
                raise OSError(ctypes.get_last_error(), f"CreateMutexW({self.name}) failed")
            result = _kernel32.WaitForSingleObject(handle, _INFINITE if blocking else 0)
            if result in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
                self._handle = handle
                return True
            _kernel32.CloseHandle(handle)
            if result == _WAIT_TIMEOUT:
                return False
            raise OSError(ctypes.get_last_error(), f"WaitForSingleObject({self.name}) returned {result:#x}")
        path = Path("/tmp") / ("wpo-" + "".join(c if c.isalnum() else "_" for c in self.name) + ".lock")
        self._file = path.open("a+b")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            self._file.close()
            self._file = None
            return False
        return True

    def release(self) -> None:
        if self._handle is not None:
            _kernel32.ReleaseMutex(self._handle)
            _kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    def __enter__(self) -> NamedMutex:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def spawn_detached(arguments: Iterable[str], cwd: Path, env: Mapping[str, str] | None = None, log_path: Path | None = None) -> int:
    """Starts a process that outlives the caller and its shell (a background shell's time limit
    kills its whole tree); returns its pid. Output goes to `log_path` when given."""
    output = log_path.open("ab") if log_path else subprocess.DEVNULL
    try:
        options: dict = {"cwd": str(cwd), "env": dict(env) if env is not None else None, "stdin": subprocess.DEVNULL, "stdout": output, "stderr": subprocess.STDOUT}
        if not IS_WINDOWS:
            return subprocess.Popen(list(arguments), start_new_session=True, **options).pid
        # A hidden console of its own, not DETACHED_PROCESS: a process with no console makes every
        # console child it starts (git, cargo, claude) open a visible window.
        flags = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
        try:
            return subprocess.Popen(list(arguments), creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, **options).pid
        except PermissionError:
            # The caller's job object forbids breakaway; a detached process still survives its shell.
            return subprocess.Popen(list(arguments), creationflags=flags, **options).pid
    finally:
        if log_path:
            output.close()


def kill_tree(pid: int, spared: Iterable[int] = ()) -> None:
    """Kills a process and every descendant (harness children such as cargo and node included),
    except the `spared` processes and their own descendants: a pool supervisor that hands a WP back
    starts that lane's supervisor as its child, and stopping the pool must not stop that lane."""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    spared_pids = set(spared)
    processes = []
    for process in root.children(recursive=True):
        try:
            # A spared launcher can spawn children between subtree enumerations.
            # Check each target's ancestry before any parent is terminated.
            if process.pid not in spared_pids and not any(parent.pid in spared_pids for parent in process.parents()):
                processes.append(process)
        except psutil.NoSuchProcess:
            continue
    processes.append(root)
    for process in processes:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(processes, timeout=30)


def tool_cpu_seconds(root_pid: int, excluded_names: Iterable[str]) -> float:
    """CPU seconds used by a session's descendants other than the harness itself: a long build
    inside one tool call writes nothing to the session log, but keeps its tools busy."""
    excluded = {name.lower() for name in excluded_names}
    try:
        children = psutil.Process(root_pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return 0.0
    total = 0.0
    for child in children:
        try:
            if Path(child.name()).stem.lower() in excluded:
                continue
            times = child.cpu_times()
            total += times.user + times.system
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total


def pid_alive(pid: int, name_stems: Iterable[str] = ()) -> bool:
    """Whether `pid` runs, and, when names are given, runs one of those executables (pids are reused)."""
    try:
        process = psutil.Process(pid)
        if not process.is_running():
            return False
        # Windows has no zombies, and asking for the status there probes every thread (~20 ms).
        if not IS_WINDOWS and process.status() == psutil.STATUS_ZOMBIE:
            return False
        stems = {stem.lower() for stem in name_stems}
        return not stems or Path(process.name()).stem.lower() in stems
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def python_process_runs(pid: int, marker: str) -> bool:
    """Whether `pid` is a live Python process whose command line contains `marker`."""
    try:
        process = psutil.Process(pid)
        return process.is_running() and marker in " ".join(process.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def environment_with(overrides: Mapping[str, str]) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(overrides)
    return environment


# Variables an orchestrator session or its proxy sets for itself; a harness must not inherit them.
_HARNESS_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDECODE", "OPENCODE_", "OPENROUTER_API_KEY")


def harness_environment(overrides: Mapping[str, str]) -> dict[str, str]:
    """The caller's environment without harness settings, plus the session's own."""
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith(_HARNESS_PREFIXES)}
    environment.update(overrides)
    return environment


def replace_file(source: Path, target: Path, attempts: int = 50) -> None:
    """Atomic rename that retries while another process holds the target open (Windows)."""
    for attempt in range(attempts):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)
