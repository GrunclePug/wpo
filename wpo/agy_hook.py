"""The PreToolUse hook that sandboxes agy sessions: `python -I agy_hook.py <policy.json>`.

agy has no per-invocation tool allowlist, so every tool call of a session is checked here against
the policy the harness wrote for that session (harness.py, `agy_policy`), stored outside every
tree so no session can edit it. It fails closed: any error, a missing policy or an unknown tool
denies, and a crash exits non-zero, which agy treats as a refused call.

- Commands: a chain ('&&', ';') is checked part by part. Each part: whole-word prefix match, then every character from a small allowed set (no shell
  operators, substitutions, variables, escapes or non-ASCII, in cmd or PowerShell), then no
  argument that redirects output, changes directory or loads configuration (`denied_arguments`),
  and no absolute or `..` path argument outside the readable directories.
- Writes: by repo-relative glob, never `.git`, `.agents` or anything under them.
- Reads: inside the repository and the policy's extra directories; a read tool naming no path is denied.
- Paths are resolved through the file system (8.3 short names, junctions, case) before any
  comparison, and spellings Windows treats as aliases that resolving cannot see (trailing dots or
  spaces, streams, wildcards, percent escapes, drive-relative, UNC) are refused outright.

The payload on stdin is agy's: {"toolCall": {"name": ..., "args": {...}}, ...}. The reply on stdout
is {"decision": "allow" | "deny", "reason": ...}. Each decision is appended to the policy's `log`,
which the harness reads to prove the hook ran for every tool step.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# Argument names that carry a file system path in agy's tools (TargetFile, AbsolutePath, DirectoryPath, Cwd, ...).
PATH_ARGUMENT = re.compile(r"path|file|dir|target|uri|cwd|source|dest|root|folder|location", re.IGNORECASE)
# Every character a command may contain. Absent on purpose: ; & | < > ` $ % ^ ! ( ) { } [ ] # ~ ?,
# line breaks of any kind, tabs and everything beyond ASCII.
COMMAND_CHARACTERS = re.compile(r"[A-Za-z0-9_\-./\\:=,+@ '\"*]*")
# A Progress log line (`wpo log`) is free text. Single-quoted, with no single quote or line break in
# it, it is literal in both bash and PowerShell: no substitution, variable or operator can run.
PROGRESS_LOG_COMMAND = re.compile(r"wpo log(?: --step [0-9]+)? '[^'\r\n]*'")
# An answer to a review finding carries free text the same way.
FINDING_ANSWER_COMMAND = re.compile(r"wpo finding (?:fixed R[0-9]+-F[0-9]+|veto R[0-9]+-F[0-9]+ [a-z-]+) '[^'\r\n]*'")
# Spellings Windows resolves to another name that the file system cannot resolve for us first:
# streams (a:b), wildcards, percent escapes, and components ending in a dot or a space (".git."
# opens ".git"). 8.3 short names (AGENTS~1) need no rule: resolving an existing one yields the long
# name, which the protected-name check sees, and one that does not exist names nothing else.
ALIAS_COMPONENT = re.compile(r"[:*?%\"<>|]|[. ]$")
PROTECTED_NAMES = (".git", ".agents")
# Separators of a command chain; any other operator character still fails COMMAND_CHARACTERS.
CHAIN_SEPARATOR = re.compile(r"\s*(?:&&|;)\s*")


class Refused(Exception):
    """A path or command the sandbox will not accept; the message is the reason."""


def glob_regex(pattern: str) -> re.Pattern[str]:
    """`**` crosses directories, `*` does not; matched case-insensitively, as NTFS compares names."""
    escaped = re.escape(pattern.replace("\\", "/"))
    return re.compile("^" + escaped.replace(r"\*\*", ".*").replace(r"\*", "[^/]*") + "$", re.IGNORECASE)


def real(path: str) -> str:
    """The path as the file system knows it: absolute, long names, junctions resolved, '/'-separated."""
    return os.path.realpath(path).replace("\\", "/").rstrip("/")


def resolve(raw: str, root: str) -> str:
    """A tool's path argument as a real absolute path; Refused for any aliasing spelling."""
    path = raw.strip()
    if path.lower().startswith("file:///"):
        path = path[len("file:///"):]
    path = path.replace("\\", "/")
    if not path:
        raise Refused("an empty path")
    if path.startswith("//"):
        raise Refused(f"network paths are not allowed ({raw})")
    drive = re.match(r"^[A-Za-z]:", path)
    if drive and not re.match(r"^[A-Za-z]:/", path):
        raise Refused(f"drive-relative paths are not allowed ({raw})")
    body = path[2:] if drive else path
    for component in body.split("/"):
        if component not in ("", ".", "..") and ALIAS_COMPONENT.search(component):
            raise Refused(f"the path component '{component}' can name another file on Windows ({raw})")
    absolute = path if drive or path.startswith("/") else f"{root}/{path}"
    return real(absolute)


def inside(path: str, directory: str) -> str | None:
    """`path` relative to `directory` when inside it (case-insensitive), else None."""
    if path.lower() == directory.lower():
        return ""
    prefix = directory.rstrip("/") + "/"
    return path[len(prefix):] if path.lower().startswith(prefix.lower()) else None


def path_arguments(arguments: dict) -> list[str]:
    found: list[str] = []

    def walk(key: str, value: object) -> None:
        if isinstance(value, dict):
            for inner_key, inner in value.items():
                walk(inner_key, inner)
        elif isinstance(value, list):
            for inner in value:
                walk(key, inner)
        elif isinstance(value, str) and PATH_ARGUMENT.search(key):
            found.append(value)

    walk("", arguments)
    return found


def readable_dirs(policy: dict, root: str) -> list[str]:
    return [root, *(real(directory) for directory in policy["read_dirs"])]


def decide(policy: dict, payload: dict) -> tuple[str, str]:
    """The decision and its reason for one tool call."""
    call = payload["toolCall"]
    tool, arguments = call["name"], call.get("args") or {}
    root = real(policy["repo_root"])
    try:
        if tool in policy["command_tools"]:
            return decide_command(policy, root, str(arguments.get("CommandLine") or ""), arguments.get("Cwd"))
        if tool in policy["write_tools"]:
            return decide_write(policy, root, tool, arguments)
        if tool in policy["read_tools"]:
            paths = path_arguments(arguments)
            if not paths:
                return "deny", f"{tool} names no path this sandbox can check"
            directories = readable_dirs(policy, root)
            for raw in paths:
                if not any(inside(resolve(raw, root), directory) is not None for directory in directories):
                    return "deny", f"reads are allowed only inside the repository and {', '.join(policy['read_dirs']) or 'no other directory'}, not {raw}"
            return "allow", "read within the session's directories"
    except Refused as refused:
        return "deny", str(refused)
    if tool in policy["neutral_tools"]:
        return "allow", "a tool that touches nothing"
    return "deny", f"the tool '{tool}' is not available in this sandbox"


def decide_write(policy: dict, root: str, tool: str, arguments: dict) -> tuple[str, str]:
    paths = path_arguments(arguments)
    if not paths:
        return "deny", f"{tool} names no path this sandbox can check"
    for raw in paths:
        relative = inside(resolve(raw, root), root)
        if relative is None:
            return "deny", f"writes are allowed only inside the repository, not {raw}"
        if any(part.lower() in PROTECTED_NAMES for part in relative.split("/")):
            return "deny", f"{relative} is part of the repository's or the sandbox's own files"
        if not any(glob_regex(pattern).match(relative) for pattern in policy["write_globs"]):
            allowed = ", ".join(policy["write_globs"]) or "nothing (read-only session)"
            return "deny", f"{relative} is outside this session's write paths ({allowed})"
    return "allow", "write within the session's paths"


def decide_command(policy: dict, root: str, command: str, cwd: object) -> tuple[str, str]:
    """A chain ('&&' or ';' between commands) is allowed when every command in it is: sessions are
    asked to batch, and each part passes the same checks as a lone command."""
    parts = CHAIN_SEPARATOR.split(command.strip())
    # A Progress log message is free text and may hold a ';' inside its quotes.
    if len(parts) == 1 or PROGRESS_LOG_COMMAND.fullmatch(command.strip()) or FINDING_ANSWER_COMMAND.fullmatch(command.strip()):
        return decide_single_command(policy, root, command, cwd)
    for part in parts:
        decision, reason = decide_single_command(policy, root, part, cwd)
        if decision != "allow":
            return decision, f"in the chained command '{part}': {reason}"
    return "allow", f"a chain of {len(parts)} allowlisted commands"


def decide_single_command(policy: dict, root: str, command: str, cwd: object) -> tuple[str, str]:
    command = command.strip()
    if not command:
        return "deny", "empty command"
    if "wpo log" in policy["command_prefixes"] and PROGRESS_LOG_COMMAND.fullmatch(command):
        if cwd and inside(resolve(str(cwd), root), root) is None:
            return "deny", "commands run inside the repository"
        return "allow", "a Progress log line"
    if "wpo finding" in policy["command_prefixes"] and FINDING_ANSWER_COMMAND.fullmatch(command):
        if cwd and inside(resolve(str(cwd), root), root) is None:
            return "deny", "commands run inside the repository"
        return "allow", "an answer to a review finding"
    if not COMMAND_CHARACTERS.fullmatch(command):
        return "deny", ("commands may use only letters, digits, spaces, quotes and _ - . / \\ : = , + @ *; no shell operators, "
                        "substitutions, variables or line breaks")
    words = command.split(" ")
    prefix = next((prefix for prefix in policy["command_prefixes"] if [w for w in words if w][:len(prefix.split())] == prefix.split()), None)
    if prefix is None:
        return "deny", f"commands must start with one of: {', '.join(policy['command_prefixes']) or 'none (no commands)'}"
    all_words = [word.strip("'\"") for word in words if word]
    arguments = all_words[len(prefix.split()):]
    denied = policy["denied_arguments"].get(all_words[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe"), [])
    for argument in arguments:
        flag = argument.split("=", 1)[0]
        if flag in denied:
            return "deny", f"{all_words[0]} {flag} is not allowed here (it redirects output, changes directory, loads configuration or runs other programs)"
    directories = readable_dirs(policy, root)
    for argument in arguments:
        value = argument.split("=", 1)[-1]
        if re.match(r"^([A-Za-z]:|/|\\)", value) or ".." in value.replace("\\", "/").split("/"):
            if not any(inside(resolve(value, root), directory) is not None for directory in directories):
                return "deny", f"the path {value} is outside the repository and the readable directories"
    if cwd and inside(resolve(str(cwd), root), root) is None:
        return "deny", "commands run inside the repository"
    return "allow", "allowlisted command"


def main() -> int:
    """Always answers: a decision on stdout, or a non-zero exit when even that fails."""
    policy: dict | None = None
    decision, reason, tool = "deny", "the hook failed before deciding", None
    try:
        policy = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        payload = json.loads(sys.stdin.read())
        tool = payload["toolCall"]["name"]
        decision, reason = decide(policy, payload)
    except Exception as error:  # fail closed on anything: a missing policy, a malformed payload, a bug
        decision, reason = "deny", f"sandbox hook error, denied: {type(error).__name__}: {error}"
    try:
        if policy and policy.get("log"):
            with open(policy["log"], "a", encoding="utf-8") as log:
                log.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": tool, "decision": decision, "reason": reason}) + "\n")
    except OSError:
        pass  # a missing decision makes the harness refuse the session; the decision itself still stands
    sys.stdout.write(json.dumps({"decision": decision, "reason": reason}))
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except BaseException:  # noqa: BLE001 - anything that escapes must still refuse the call
        exit_code = 1
    sys.exit(exit_code)
