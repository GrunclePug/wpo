"""Fence persisted OS PIDs by host and boot without discarding durable work.

First adoption records a baseline and leaves legacy records untouched: their origin cannot be
proved retrospectively. Later host/boot changes archive and invalidate only known PID records.
An interrupted transition is resumed before any command receives its Context. Identity, parse,
path or write failures stop that command; they never fall back to trusting foreign PIDs.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path

from .events import Journal
from .platform import FileLock, replace_file


class BootError(RuntimeError):
    """Boot safety could not be established; no PID-based action may follow."""


def _windows_identity() -> dict[str, str]:
    import ctypes
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                        access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        host = str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip()
    # SYSTEM_BOOT_ENVIRONMENT_INFORMATION starts with the OS boot GUID (phnt/ntexapi.h,
    # SystemBootEnvironmentInformation = 90). Unlike uptime-derived
    # timestamps, this stays byte-identical across all callers throughout the boot.
    query = ctypes.WinDLL("ntdll").NtQuerySystemInformation
    query.argtypes = (ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32))
    query.restype = ctypes.c_int32
    buffer = ctypes.create_string_buffer(64)
    returned = ctypes.c_uint32()
    if query(90, buffer, len(buffer), ctypes.byref(returned)) < 0 or returned.value < 16:
        raise BootError("Windows did not return its boot GUID")
    boot = uuid.UUID(bytes_le=buffer.raw[:16])
    if not boot.int:
        raise BootError("Windows returned an empty boot GUID")
    return {"platform": "win32", "host": host, "boot": str(boot)}


def current_identity() -> dict[str, str]:
    """Read persistent host identity and the operating system's current boot identity."""
    try:
        if sys.platform == "win32":
            identity = _windows_identity()
        elif sys.platform.startswith("linux"):
            host = next((path.read_text(encoding="ascii").strip() for path in
                         (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")) if path.is_file()), "")
            identity = {"platform": "linux", "host": host,
                        "boot": str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()))}
        else:
            raise BootError(f"boot identity is unsupported on {sys.platform}; PID actions are disabled")
        return _identity(identity)
    except (OSError, ValueError, AttributeError) as error:
        raise BootError(f"cannot determine host/boot identity; PID actions are disabled: {error}") from error


def _identity(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"platform", "host", "boot"}:
        raise BootError("invalid host/boot identity")
    if any(not isinstance(part, str) or not part.strip() for part in value.values()):
        raise BootError("empty host/boot identity")
    return value


def _safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(part in ("..", ".") for part in path.parts):
        raise BootError(f"unsafe boot record path: {relative}")
    candidate = root
    for part in path.parts:
        candidate /= part
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise BootError(f"linked boot record path is not allowed: {candidate}")
    if not candidate.resolve().is_relative_to(root):
        raise BootError(f"boot record escapes runs directory: {relative}")
    return candidate


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write(root: Path, relative: str, value: bytes) -> None:
    target = _safe_path(root, relative)
    temporary = _safe_path(root, relative + ".boot-tmp")
    with temporary.open("wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    replace_file(temporary, target)


def _json(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode()


def _record_allowed(relative: str) -> bool:
    return bool(re.fullmatch(r"supervisor-[\w.-]+\.(?:pid|state)|dashboard\.json|"
                             r"WP-[\w.-]+/inbox/live\.json|"
                             r"WP-[\w.-]+/delegate/[a-z0-9][a-z0-9-]{0,47}/(?:spawn\.pid|state\.json)", relative))


def _changes(root: Path) -> list[dict]:
    records = set(root.glob("supervisor-*.pid")) | set(root.glob("supervisor-*.state"))
    records.add(root / "dashboard.json")
    records.update(root.glob("WP-*/inbox/live.json"))
    records.update(root.glob("WP-*/delegate/*/spawn.pid"))
    records.update(root.glob("WP-*/delegate/*/state.json"))
    changes = []
    for record in sorted(records):
        relative = record.relative_to(root).as_posix()
        if not _record_allowed(relative):
            continue
        before = _read(_safe_path(root, relative))
        if before is None:
            continue
        after = None
        if relative.endswith(".state"):
            if before.decode("utf-8-sig").strip() not in ("running", "sleeping"):
                continue
            after = b"died"
        elif relative.endswith("/state.json"):
            state = json.loads(before)
            if not isinstance(state, dict):
                raise BootError(f"invalid worker state: {relative}")
            state.pop("worker_pid", None)
            if state.get("state") == "running":
                state.update(state="interrupted", detail="host/boot changed; local worker process cannot be resumed")
            after = _json(state)
            if after == before:
                continue
        changes.append({"path": relative, "before": base64.b64encode(before).decode(),
                        "after": base64.b64encode(after).decode() if after is not None else None})
    return changes


def _decode(value: object) -> bytes:
    if not isinstance(value, str):
        raise BootError("invalid boot transition content")
    return base64.b64decode(value, validate=True)


def _apply(root: Path, transition: dict, journal: Journal) -> None:
    identifier = str(uuid.UUID(transition["id"]))
    _identity(transition["from"])
    _identity(transition["to"])
    changes = transition["changes"]
    if not isinstance(changes, list):
        raise BootError("invalid boot transition changes")
    _safe_path(root, "events.jsonl")
    _safe_path(root, "events.lock")
    prepared = []
    seen = set()
    for change in changes:
        relative = change["path"]
        if not isinstance(relative, str) or not _record_allowed(relative):
            raise BootError("boot transition names an unknown PID record")
        path = _safe_path(root, relative)
        if path in seen:
            raise BootError(f"duplicate PID record in boot transition: {relative}")
        seen.add(path)
        before = _decode(change["before"])
        after = _decode(change["after"]) if change["after"] is not None else None
        current = _read(path)
        if current not in (before, after):
            raise BootError(f"PID record changed during boot recovery: {relative}; inspect boot.json before retrying")
        prepared.append((relative, path, after))
    # Archive the entire original transition before the first mutation. The pending marker and
    # byte comparisons make retries idempotent if any later write, journal append or process fails.
    archive = _safe_path(root, ".boot-recovery")
    archive.mkdir(exist_ok=True)
    _write(root, f".boot-recovery/{identifier}.json", _json(transition))
    for relative, path, after in prepared:
        if after is None:
            path.unlink(missing_ok=True)
        else:
            _write(root, relative, after)
    if not any(event.get("kind") == "boot-changed" and event.get("transition") == identifier for event in journal.read(0)):
        journal.emit("boot-changed", "host/boot changed; invalidated local PID records without stopping processes or changing held work",
                     {"transition": identifier, "records": len(changes)})


def ensure_boot(runs_dir: Path, journal: Journal) -> None:
    """Reconcile known OS PID records before a command can inspect or signal processes.

    This fence assumes one active host per runs directory. It does not cancel native T3 tasks,
    recover coordinator queues, validate legacy PID provenance, or detect PID reuse within a boot.
    """
    identity = current_identity()
    root = runs_dir.resolve()
    try:
        with FileLock(_safe_path(root, "boot.lock")):
            content = _read(_safe_path(root, "boot.json"))
            if content is None:
                _write(root, "boot.json", _json({"schema": 1, "identity": identity, "legacy_baseline": True}))
                return
            marker = json.loads(content)
            if not isinstance(marker, dict) or marker.get("schema") != 1:
                raise BootError("unsupported boot.json; inspect it before PID actions")
            previous = _identity(marker["identity"])
            if "pending" in marker:
                pending = marker["pending"]
                if not isinstance(pending, dict):
                    raise BootError("invalid pending boot transition")
                if pending["from"] != previous:
                    raise BootError("boot transition does not match its baseline")
                _apply(root, pending, journal)
                previous = pending["to"]
                _write(root, "boot.json", _json({"schema": 1, "identity": previous}))
            if previous == identity:
                return
            transition = {"id": str(uuid.uuid4()), "from": previous, "to": identity, "changes": _changes(root)}
            _write(root, "boot.json", _json({"schema": 1, "identity": previous, "pending": transition}))
            _apply(root, transition, journal)
            _write(root, "boot.json", _json({"schema": 1, "identity": identity}))
    except (OSError, ValueError, KeyError, TypeError, TimeoutError) as error:
        raise BootError(f"cannot establish PID safety: {error}") from error
