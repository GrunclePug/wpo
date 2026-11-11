"""How one session starts on one route: the harness command line, its environment and sandbox.

Harnesses: `claude` (Claude Code CLI), `gpt` (the Claude CLI against a loopback CLIProxyAPI with
Codex OAuth), `local` (the Claude CLI against the local model's compaction shim), `opencode`
(OpenCode CLI, for free routes), `agy` (Google's Antigravity CLI, free routes, sandboxed by a
PreToolUse hook: agy_hook.py), and `command` (any command speaking Claude's stream-json; tests).

Access levels form the sandbox: `builder` edits anywhere in its working tree and runs the project's
allowlisted commands (plus the delegate command when it leads workers); `worker` and `docs` edit
only their write paths; `reviewer` and `read` edit nothing. No session gets an unrestricted shell.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .machine import MachineConfig
from .platform import NO_COLOUR_ENV, spawn_detached
from .session_brief import SessionBrief

ACCESS_LEVELS = ("builder", "worker", "reviewer", "evaluator", "read", "docs")
# Access levels that may write exactly the paths a session is given. An evaluator also runs the
# reviewer's read-only commands, and edits only its WP file.
WRITES_LISTED_PATHS = ("docs", "worker", "evaluator")
# Gate sessions judge or redirect work on the strongest available models; free routes never stand in.
GATE_ACCESS = ("reviewer", "evaluator")
# How long the GPT proxy probe waits; a capped account answers 429 at once.
GPT_PROXY_PROBE_SECONDS = 20
# No Skill tool: each role's skills are loaded into its brief (session_brief.py).
READ_TOOLS = ("Read", "Glob", "Grep", "WebSearch", "WebFetch")
# Claude-CLI context that sessions here never use: CLAUDE.md discovery (the brief carries the
# project's instructions), auto-memory (worktree paths have none), and the commit and PR
# instructions (the runner commits). The skill listing is off by --disable-slash-commands.
CLAUDE_SESSION_ENV = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
}
# Marks every session this tooling launches, with colour off for every tool it runs.
WPO_SESSION_ENV = {"WPO_SESSION": "1", **NO_COLOUR_ENV}
# The user's own hooks (an output-rewriting shell proxy, editor checkers) never run in a session:
# they changed command output under the model (a test's printed output summarized away) and its
# allowlist cannot see them.
NO_USER_HOOKS = json.dumps({"disableAllHooks": True})
# Harnesses that run the Claude CLI (and so take an appended system prompt and --resume).
CLAUDE_CLI_HARNESSES = ("claude", "gpt", "xai", "local")
# Harnesses that reach their models through the loopback CLIProxyAPI.
PROXY_HARNESSES = ("gpt", "xai")
# The range the Claude CLI accepts for --autocompact.
AUTOCOMPACT_RANGE = (100_000, 1_000_000)
# Harness features no workflow session uses. PowerShell is off so every command goes through the
# scoped Bash rules.
UNUSED_CLAUDE_TOOLS = (
    "Agent Task PowerShell DesignSync Monitor Workflow CronCreate CronDelete CronList ScheduleWakeup "
    "PushNotification SendMessage ListAgents EnterWorktree ExitWorktree EnterPlanMode ExitPlanMode "
    "NotebookEdit RemoteTrigger ReportFindings"
)
# The built-in tools each role's Claude-CLI session loads (`--tools`): every other tool's schema
# stays out of the prompt, which each turn re-sends. Web tools only for research and docs.
ROLE_TOOLS = {
    "builder": ("Bash", "Edit", "Write", "Read", "Grep", "Glob"),
    "worker": ("Bash", "Edit", "Write", "Read", "Grep", "Glob"),
    "reviewer": ("Bash", "Read", "Grep", "Glob"),
    "evaluator": ("Bash", "Edit", "Write", "Read", "Grep", "Glob"),
    "read": ("Bash", "Read", "Grep", "Glob", "WebSearch", "WebFetch"),
    "docs": ("Bash", "Edit", "Write", "Read", "Grep", "Glob", "WebSearch", "WebFetch"),
}
# The user's settings add plugins, agents and tool search no session uses (the user's agents alone
# list ~3k tokens in the Agent tool); sessions read only the project's settings.
SETTING_SOURCES = "project,local"
# The in-session subagent a lead builder may start (session_workers), and the harnesses whose
# endpoint serves its Claude model (the local model's endpoint does not).
SESSION_WORKER_AGENT = "worker"
SESSION_WORKER_HARNESSES = ("claude", "gpt", "xai")
SESSION_WORKER_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write", "Bash")
SESSION_WORKER_PROMPT = (
    "You are a worker inside a builder's session. Do exactly the task you are given, in as few turns as possible: "
    "issue independent reads, searches, edits and commands together in one message. Change only the files the task "
    "names and run only the commands it names, chained with '&&' where they belong together; no 'cd'. Do not explore beyond what the "
    "task needs. End with a short report: what you changed, each command you ran and its result, and anything you "
    "could not do and why.")
# How every session prompt states the shell's chaining rule. The Claude CLI runs a chain when every
# part is allowed (probed 2026-10-26); agy's sandbox refuses shell operators and says so.
SHELL_CHAINING = ("Batch: chain allowed commands with '&&' or ';' in one call; a chain runs when every part is allowed. "
                  "'cd' is not allowed: pass paths, or `git -C <dir>`. Run a command you need; do not ask in advance. Ask for "
                  "approval only when a command you expected to work was denied and no allowed tool does the same, or when the "
                  "work clearly needs a command the rules explicitly forbid: `wpo ask-approval '<exact command>' '<why the work "
                  "needs it>'`, then carry on with other work. The answer arrives as a message, and an approved command runs once "
                  "with `wpo run-approved <id>`. Never ask for `wpo verify`, `wpo log`, `wpo finding` or an edit to your own WP "
                  "file, its Verify block included: those are always yours. Do not reason about why a rule denied a command; "
                  "ask or work around it. Always denied, so never try them: `sed -i`, environment prefixes (`VAR=x cmd`), `$VAR`, "
                  "and long heredocs mixing braces and quotes. Edit files with Edit or Write; for a scripted or bulk edit, Write a "
                  "script under .orchestration/tasks/ (gitignored) and run `python <script>`. Pass parameters in code or a file, "
                  "never through the environment.")
# Session commands (session_commands.py): the Progress log line with its date and key, and the
# runner's checks run from inside a session.
LOG_COMMAND = "wpo log"
# A drafting session (docs access) lints the WP files it writes.
LINT_COMMAND = "wpo lint"
VERIFY_COMMAND = "wpo verify"
# A session asks for a command its sandbox denies, and runs it once approved (approvals.py).
APPROVAL_COMMANDS = ["wpo ask-approval", "wpo run-approved"]
FINDING_COMMAND = "wpo finding"
# A builder hands its successor the state of the work (session_commands.handoff).
HANDOFF_COMMAND = "wpo handoff"
# The command a lead builder runs to hand tasks to workers (`wpo install-shim` puts it on PATH).
DELEGATE_COMMAND = "wpo delegate"
# Names of harness processes; their CPU is not activity (a session waiting on its API idles).
HARNESS_PROCESS_NAMES = ("pwsh", "powershell", "claude", "node", "opencode", "bun", "conhost", "bash", "sh", "python", "cmd")


class HarnessError(Exception):
    """A route that cannot be launched on this machine (missing pins, token, executable)."""


@dataclass
class Launch:
    """Everything needed to start the harness process."""

    arguments: list[str]
    env: dict[str, str] = field(default_factory=dict)
    stdin_text: str = ""
    # A stderr line matching this means the harness will make no further progress (it retries a
    # refused request forever, writing nothing to stdout); the session is stopped at once.
    fatal_stderr: re.Pattern[str] | None = None
    # A sandboxed harness's decision log: a session whose tool steps left no decisions ran unguarded.
    sandbox_log: Path | None = None
    # stdin is a stream-json conversation (Claude CLI): the prompt, then the orchestrator's messages.
    stream_input: bool = False


# OpenCode keeps retrying a provider that refuses it, silently on stdout; these are the refusals
# that do not clear within a session.
OPENCODE_FATAL_STDERR = re.compile(r"Free usage limit reached|AI_RetryError.*Failed after [0-9]+ attempts")


def command_prefixes(access: str, project_config: dict, lead: bool) -> list[str]:
    if access == "builder":
        return (list(project_config["builder_commands"]) + [LOG_COMMAND, VERIFY_COMMAND, FINDING_COMMAND, HANDOFF_COMMAND, *APPROVAL_COMMANDS]
                + ([DELEGATE_COMMAND] if lead else []))
    if access == "worker":
        return list(project_config["builder_commands"]) + [VERIFY_COMMAND, *APPROVAL_COMMANDS]
    if access in ("reviewer", "evaluator"):
        return list(project_config["reviewer_commands"])
    if access == "docs":
        # Drafting sessions check the WP files they write (orchestrator.DRAFTING_RULE).
        return [LINT_COMMAND]
    return []


def compaction_limit(project_config: dict, harness: str) -> int:
    """The compaction point for one harness: its own entry in `autocompact_tokens`, else "default"."""
    limits = project_config["autocompact_tokens"]
    if not isinstance(limits, dict) or "default" not in limits:
        raise HarnessError(f"autocompact_tokens must be an object with a 'default' entry (per harness), not {limits!r}")
    return int(limits.get(harness, limits["default"]))


def handoff_limit(project_config: dict, harness: str) -> int:
    """The context at which a builder on this harness is asked for its handoff: its own entry in
    `handoff.context_tokens`, else "default"."""
    limits = project_config["handoff"]["context_tokens"]
    if not isinstance(limits, dict) or "default" not in limits:
        raise HarnessError(f"handoff.context_tokens must be an object with a 'default' entry (per harness), not {limits!r}")
    return int(limits.get(harness, limits["default"]))


def validate_compaction_limits(project_config: dict) -> None:
    """Raises HarnessError for any compaction point the Claude CLI would refuse, and for a handoff
    point at or past a harness's compaction point (the harness would compact before the handoff)."""
    compaction_limit(project_config, "default")
    for harness in project_config["autocompact_tokens"]:
        autocompact_arguments(compaction_limit(project_config, harness), None)
    for harness in {"default", *project_config["autocompact_tokens"], *project_config["handoff"]["context_tokens"]}:
        if (harness == "default" or harness in CLAUDE_CLI_HARNESSES) and handoff_limit(project_config, harness) >= compaction_limit(project_config, harness):
            raise HarnessError(f"the {harness} handoff point ({handoff_limit(project_config, harness):,} tokens) must be below its compaction point "
                               f"({compaction_limit(project_config, harness):,})")


def autocompact_arguments(limit_tokens: int, window_tokens: int | None) -> list[str]:
    """`--autocompact` when the route's window is larger than the project's compaction point; a
    smaller window compacts near its own end. `window_tokens` None is an unknown window, taken as
    larger: Claude routes declare `context_tokens` only when below the 1M of the Claude 5 models."""
    low, high = AUTOCOMPACT_RANGE
    if not low <= limit_tokens <= high or limit_tokens % 1000:
        raise HarnessError(f"autocompact_tokens {limit_tokens} must be a multiple of 1000 within {low}-{high}")
    if window_tokens is not None and window_tokens <= limit_tokens:
        return []
    return ["--autocompact", f"{limit_tokens // 1000}k"]


def session_worker_definition(workers: dict) -> str:
    """The `--agents` JSON that defines the in-session worker subagent."""
    return json.dumps({SESSION_WORKER_AGENT: {
        "description": "Does one small, fully specified mechanical task (edits across files, tests for given code, running a command and summarizing its result) and reports back briefly.",
        "prompt": SESSION_WORKER_PROMPT, "model": workers["model"], "effort": workers["effort"], "tools": list(SESSION_WORKER_TOOLS)}})


def bash_rule(prefix: str) -> str:
    """The Claude CLI allow rule for a command prefix. `prefix:*` matches only at a word boundary,
    so a prefix ending inside a word (a directory, `git -C E:/Tools/`) needs the glob form, which
    matches any continuation (probed 2026-10-26: `git -C <dir>/:*` denied `git -C <dir>/repo log`)."""
    return f"Bash({prefix}:*)" if prefix[-1:].isalnum() else f"Bash({prefix}*)"


def claude_arguments(access: str, write_paths: Iterable[str], prefixes: Iterable[str], system_file: Path | None = None,
                     compaction: Iterable[str] = (), resume_session: str | None = None, workers: dict | None = None,
                     stream_input: bool = True, session_dirs: Iterable[str] = ()) -> list[str]:
    bash_rules = [bash_rule(prefix) for prefix in prefixes]
    write_rules = [rule for path in write_paths for rule in (f"Edit({path})", f"Write({path})")] if access in WRITES_LISTED_PATHS else []
    # Only the defined worker may be started; the user's own agent definitions stay unavailable.
    agent_rules = [f"Agent({SESSION_WORKER_AGENT})"] if workers else []
    agents = ["--agents", session_worker_definition(workers)] if workers else []
    disallowed = " ".join(tool for tool in UNUSED_CLAUDE_TOOLS.split() if not (workers and tool in ("Agent", "Task")))
    allowed = " ".join([*READ_TOOLS, *bash_rules, *write_rules, *agent_rules])
    permission = ["--permission-mode", "acceptEdits"] if access == "builder" else []
    tools = ["--tools", ",".join([*ROLE_TOOLS[access], *(["Agent"] if workers else [])]), "--setting-sources", SETTING_SOURCES]
    # Tool checkouts outside the repository that builders and workers build and run in.
    extra_dirs = [argument for directory in session_dirs for argument in ("--add-dir", directory)] if access in ("builder", "worker") else []
    system = ["--append-system-prompt-file", str(system_file)] if system_file else []
    # A resumed conversation replays its recorded system prompt, so its cached prefix still matches.
    resume = ["--resume", resume_session] if resume_session else []
    # --strict-mcp-config without --mcp-config loads no MCP servers (one server cost ~55k tokens of
    # context). stream-json writes every message as it happens: the liveness signal and the result.
    return ["-p", *resume, *permission, *tools, *extra_dirs, "--settings", NO_USER_HOOKS, "--allowedTools", allowed, "--strict-mcp-config", "--disallowedTools", disallowed,
            "--disable-slash-commands", *agents, *system, *compaction, *(["--input-format", "stream-json"] if stream_input else []),
            "--output-format", "stream-json", "--verbose"]


def opencode_config(route: dict, access: str, write_paths: Iterable[str], prefixes: Iterable[str]) -> str:
    # Rules are last-match-wins; edit globs are relative to the git root and '*' crosses '/'.
    # Zen's free tier rejects sessions offering no tools, so read tools stay on. external_directory
    # stays denied: an OpenCode session can neither read nor write outside the repository.
    # No skill tool: the brief at the top of the prompt carries the role's skills.
    permission: dict = {
        "*": "deny", "read": "allow", "glob": "allow", "grep": "allow", "list": "allow", "skill": "deny",
        "webfetch": "allow", "websearch": "allow", "todowrite": "allow", "todoread": "allow", "external_directory": "deny",
    }
    if access == "builder":
        permission["edit"] = "allow"
    elif access in WRITES_LISTED_PATHS:
        permission["edit"] = {"*": "deny", **{path.replace("**", "*"): "allow" for path in write_paths}}
    prefixes = list(prefixes)
    if prefixes:
        permission["bash"] = {"*": "deny", **{rule: "allow" for prefix in prefixes for rule in (prefix, f"{prefix} *")}}
    # OpenCode picks its own "small model" from the route's provider for titles and summaries; on
    # OpenRouter it chose a paid image model (google/gemini-nano-banana-2.1) and billed the key.
    # The route's own model serves every agent instead.
    config: dict = {"$schema": "https://opencode.ai/config.json", "autoupdate": False, "share": "disabled", "permission": permission,
                    "small_model": route["model"]}
    if route.get("provider") == "openrouter":
        # A ':free' model newer than OpenCode's models.dev snapshot is unknown unless declared.
        config["provider"] = {"openrouter": {"models": {route["model"].removeprefix("openrouter/"): {}}}}
    return json.dumps(config, separators=(",", ":"))


# agy's tools by kind, under both naming sets it uses (its init event lists view_file and
# write_to_file; its hooks see read_file and write_file). Any other tool is denied.
AGY_TOOLS = {
    "command_tools": ["run_command"],
    "read_tools": ["view_file", "read_file", "list_dir", "list_directory", "find_by_name", "grep_search", "glob"],
    "write_tools": ["write_to_file", "write_file", "replace_file_content", "multi_replace_file_content", "sed_file", "edit_file",
                    "create_file", "delete_file", "move_file", "notebook_edit"],
    "neutral_tools": ["command_status", "finish", "wait", "wait_5_seconds"],
}
AGY_HOOK = Path(__file__).resolve().parent / "agy_hook.py"
AGY_HOOKS_FILE = Path(".agents") / "hooks.json"
AGY_HOOKS_KEY = "wpo-sandbox"
# agy prints failures as `AGY_ERROR: {...}` on stderr and exits 3; a spent plan says so in words.
AGY_FATAL_STDERR = re.compile(r"AGY_ERROR|credits balance is too low")


# Arguments that make an allowed command write elsewhere, run elsewhere or load other settings
# (git diff --output, git -C, cargo --target-dir, cargo --config with a rustc wrapper, ...).
# Per command word; flags mean different things to different tools (grep -C is context lines).
AGY_DENIED_ARGUMENTS = {
    "git": ["--output", "-o", "-C", "-c", "--config-env", "--git-dir", "--work-tree", "--exec-path", "--namespace"],
    "cargo": ["--target-dir", "--config", "-C", "-Z", "--out-dir", "--artifact-dir", "--manifest-path"],
    "rustc": ["-o", "--out-dir", "-C", "-Z"],
    "rustup": ["run"],
    "dotnet": ["-o", "--output", "exec", "run"],
}


def agy_policy(access: str, write_paths: list[str], prefixes: list[str], cwd: Path, read_dirs: list[str], log: Path) -> dict:
    """What one agy session may do; the hook enforces it. Builders write anywhere in the tree (the
    owns check reads git afterwards, as for Claude builders); writers of listed paths write those;
    everyone else writes nothing."""
    if access == "builder":
        write_globs = ["**"]
    elif access in WRITES_LISTED_PATHS:
        write_globs = list(write_paths)
    else:
        write_globs = []
    return {**AGY_TOOLS, "repo_root": cwd.as_posix(), "command_prefixes": list(prefixes), "write_globs": write_globs,
            "denied_arguments": AGY_DENIED_ARGUMENTS,
            "read_dirs": [Path(directory).expanduser().as_posix() for directory in read_dirs], "log": log.as_posix()}


def agy_sandbox_note(policy: dict) -> str:
    """The session's limits, in its prompt: a refused call should change the plan, not the syntax."""
    commands = ", ".join(policy["command_prefixes"]) or "none: this session runs no commands"
    writes = ", ".join(policy["write_globs"]) or "none: this session is read-only"
    reads = ", ".join(["the repository", *policy["read_dirs"]])
    return f"""## Your sandbox (agy)
A hook checks every tool call against this session's policy and refuses the rest with
"tool call denied by pre-tool hook: <reason>".
- Shell commands: one command per call, starting with one of: {commands}. Only letters, digits, spaces, quotes and
  _ - . / \\ : = , + @ * are accepted: no shell operators, redirection, substitutions, variables or line breaks. No
  {'; '.join(f"{tool} {' '.join(flags)}" for tool, flags in policy['denied_arguments'].items())}; and no absolute or '..'
  paths outside the readable directories.
  Commands run in the repository.{chr(10) + "- `wpo log` takes any message in single quotes, as long as it has no single quote or line break in it." if "wpo log" in policy["command_prefixes"] else ""}
- Writes (with your file tools): {writes}; never .git or .agents.
- Reads: {reads}.
- Not available: web search, URLs, the browser, MCP tools and subagents.
A refused call will not pass in another form, so do not retry it with different syntax: do the work an allowed way,
or say in your final report what you could not do and why."""


def ensure_agy_hooks(machine: MachineConfig, cwd: Path) -> None:
    """Writes the tree's `.agents/hooks.json` (generated, so it always parses: a file agy cannot
    parse loads no hooks and leaves the session unguarded) and keeps `.agents/` out of git."""
    tracked = subprocess.run(["git", "-C", str(cwd), "ls-files", "--", ".agents"], capture_output=True, text=True)
    if tracked.returncode != 0:
        raise HarnessError(f"git ls-files in {cwd} failed: {tracked.stderr.strip()}")
    if tracked.stdout.strip():
        raise HarnessError(f"{cwd} tracks files under .agents/; the agy sandbox would overwrite them")
    python, hook = Path(machine.python).as_posix(), AGY_HOOK.as_posix()
    if " " in python or " " in hook:
        raise HarnessError(f"the agy hook command cannot quote paths with spaces ({python}, {hook})")
    # cmd expands %AGY_POLICY%; quoting it would put the quotes into argv.
    hooks = {AGY_HOOKS_KEY: {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": f"{python} -I {hook} %AGY_POLICY%", "timeout": 30}]}]}}
    content = json.dumps(hooks, indent=2)
    hooks_file = cwd / AGY_HOOKS_FILE
    if hooks_file.exists():
        existing_hooks = hooks_file.read_text(encoding="utf-8")
        try:
            ours = AGY_HOOKS_KEY in json.loads(existing_hooks)
        except json.JSONDecodeError:
            ours = False
        if not ours:
            raise HarnessError(f"{hooks_file} exists and is not the sandbox's; it is left alone and agy cannot run here")
    else:
        existing_hooks = None
    if existing_hooks != content:
        # Atomic and only on change: a session starting beside a running one must never read a
        # half-written file (agy would load no hooks and run unguarded).
        hooks_file.parent.mkdir(exist_ok=True)
        staging = hooks_file.with_name(f"hooks.{os.getpid()}.tmp")
        staging.write_text(content, encoding="utf-8")
        os.replace(staging, hooks_file)
    common = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True)
    if common.returncode != 0:
        raise HarnessError(f"git rev-parse in {cwd} failed: {common.stderr.strip()}")
    exclude = Path(common.stdout.strip()) / "info" / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if ".agents/" not in existing.splitlines():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(existing + ("" if not existing or existing.endswith("\n") else "\n") + "# wp-orchestration agy sandbox\n.agents/\n", encoding="utf-8")


def agy_tool_steps(log_text: str) -> int:
    """Tool steps an agy stream-json log shows; with a guard, each has a hook decision."""
    steps = set()
    for line in log_text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        update = event.get("step_update") or {}
        if event.get("event") == "step_update" and update.get("step_type") == "tool":
            steps.add(update.get("step_index"))
    return len(steps)


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def _read_secret(path: Path, what: str) -> str:
    if not path.exists():
        raise HarnessError(f"{what} not found at {path}")
    return path.read_text(encoding="utf-8").strip()


def ensure_gpt_proxy(machine: MachineConfig) -> int:
    port = int(machine.gpt("port"))
    if _port_open(port):
        return port
    executable = machine.gpt("executable")
    spawn_detached([str(executable), "-config", str(machine.gpt("config"))], executable.parent)
    for _ in range(30):
        if _port_open(port):
            return port
        time.sleep(0.4)
    raise HarnessError(f"CLIProxyAPI did not start on port {port}")


def proxy_refusal(env: Mapping[str, str]) -> str | None:
    """The proxy's rate-limit error for a session's model, or None when it would serve it.

    A capped account makes the CLI retry silently before its first turn, so the session looks hung
    for the whole idle limit; the usage readout can also lag the cap. A one-token request answers
    in a fraction of a second. Errors other than 429 are left to the session itself."""
    request = urllib.request.Request(
        f"{env['ANTHROPIC_BASE_URL']}/v1/messages", method="POST",
        data=json.dumps({"model": env["ANTHROPIC_MODEL"], "max_tokens": 1, "messages": [{"role": "user", "content": "ok"}]}).encode(),
        headers={"content-type": "application/json", "x-api-key": env["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(request, timeout=GPT_PROXY_PROBE_SECONDS):
            return None
    except urllib.error.HTTPError as error:
        if error.code != 429:
            return None
        body = error.read().decode("utf-8", errors="replace")
        try:
            return str(json.loads(body)["error"]["message"])
        except (ValueError, KeyError, TypeError):
            return f"HTTP 429: {body[:300]}"
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def gpt_environment(machine: MachineConfig, tier: str, effort: str) -> tuple[str, dict[str, str]]:
    pins = json.loads(_read_secret(machine.gpt("pins_file"), "model pins"))
    tier_pin = pins.get("gpt_tiers", {}).get(tier)
    background_tier = machine.gpt("background_tier")
    background_pin = pins.get("gpt_tiers", {}).get(background_tier)
    if not tier_pin or not background_pin:
        raise HarnessError(f"model pins lack gpt_tiers.{tier} or gpt_tiers.{background_tier}; rerun update_model_pins.py")
    if effort not in tier_pin["efforts"]:
        raise HarnessError(f"{tier_pin['id']} does not support effort '{effort}' (supported: {', '.join(tier_pin['efforts'])})")
    port = ensure_gpt_proxy(machine)
    token = _read_secret(machine.gpt("token_file"), "proxy client token")
    model = f"{tier_pin['id']}({effort})"
    aliases = pins.get("claude_aliases", {})
    env = {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_API_KEY": token,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_SMALL_FAST_MODEL": f"{background_pin['id']}({machine.gpt('background_effort')})",
        "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT": "1",
        # The CLI does not know gpt-* ids and would assume a 200k window for auto-compaction.
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(machine.gpt("context_tokens").get(tier, tier_pin["context_tokens"])),
        **{f"ANTHROPIC_DEFAULT_{alias.upper()}_MODEL": aliases[alias] for alias in ("haiku", "sonnet", "opus") if alias in aliases},
    }
    return model, env


def xai_environment(machine: MachineConfig, model: str, effort: str, context_tokens: int) -> tuple[str, dict[str, str]]:
    """Grok models on the user's xAI account, through the same proxy as GPT (its xAI OAuth login).
    The CLI's background calls run on the same model at low effort."""
    port = ensure_gpt_proxy(machine)
    token = _read_secret(machine.gpt("token_file"), "proxy client token")
    session_model = f"{model}({effort})"
    env = {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_API_KEY": token,
        "ANTHROPIC_MODEL": session_model,
        "ANTHROPIC_SMALL_FAST_MODEL": f"{model}(low)",
        "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT": "1",
        # The CLI does not know grok-* ids and would assume a 200k window for auto-compaction.
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(context_tokens),
    }
    return session_model, env


def local_environment(machine: MachineConfig, model: str) -> dict[str, str]:
    # The compaction shim summarizes in chunks, because the local model returns empty single-shot
    # summaries; context plus output must fit its KV pool.
    token = _read_secret(machine.local("token_file"), "local model token")
    return {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{machine.local('shim_port')}",
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_API_KEY": token,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_SMALL_FAST_MODEL": model,
        "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT": "1",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(machine.local("max_context_tokens")),
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(machine.local("max_output_tokens")),
    }


def build_launch(machine: MachineConfig, project_config: dict, route: dict, access: str, write_paths: Iterable[str],
                 prompt: str, cwd: Path, brief: SessionBrief, system_file: Path, lead: bool = False,
                 resume_session: str | None = None) -> Launch:
    """The command, environment and stdin for one session; the prompt always goes through stdin
    (command lines are limited to 32k characters on Windows). Claude-CLI harnesses get the brief as
    an appended system prompt written to `system_file`; the others get it atop the prompt.
    `resume_session` continues that conversation (Claude-CLI harnesses only)."""
    if access not in ACCESS_LEVELS:
        raise HarnessError(f"unknown access level '{access}'")
    write_paths = list(write_paths)
    prefixes = command_prefixes(access, project_config, lead)
    harness = route["harness"]
    limit = compaction_limit(project_config, harness) if harness in CLAUDE_CLI_HARNESSES else 0
    if resume_session and harness not in CLAUDE_CLI_HARNESSES:
        raise HarnessError(f"harness '{harness}' cannot resume a session")
    workers = project_config["session_workers"] if lead and access == "builder" and harness in SESSION_WORKER_HARNESSES else None
    if harness in CLAUDE_CLI_HARNESSES:
        # Only a session that has the subagent is told about it.
        worker_note = (f"\n\n## Subagents\nYou can start `{SESSION_WORKER_AGENT}` subagents with the Agent tool ({workers['model']}, "
                       f"{workers['effort']} effort).") if workers else ""
        system_file.write_text(brief.system_text() + worker_note, encoding="utf-8")
    if harness == "claude":
        compaction = autocompact_arguments(limit, route.get("context_tokens"))
        arguments = [machine.executable("claude"), "--model", route["model"], "--effort", route["effort"],
                     *claude_arguments(access, write_paths, prefixes, system_file, compaction, resume_session, workers,
                                       session_dirs=project_config["session_dirs"])]
        return Launch(arguments, {**CLAUDE_SESSION_ENV, **WPO_SESSION_ENV}, prompt, stream_input=True)
    if harness == "gpt":
        model, env = gpt_environment(machine, route["model"], route["effort"])
        compaction = autocompact_arguments(limit, int(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"]))
        return Launch([machine.executable("claude"), "--model", model,
                       *claude_arguments(access, write_paths, prefixes, system_file, compaction, resume_session, workers,
                                       session_dirs=project_config["session_dirs"])],
                      {**env, **CLAUDE_SESSION_ENV, **WPO_SESSION_ENV}, prompt, stream_input=True)
    if harness == "xai":
        model, env = xai_environment(machine, route["model"], route["effort"], int(route["context_tokens"]))
        compaction = autocompact_arguments(limit, int(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"]))
        return Launch([machine.executable("claude"), "--model", model,
                       *claude_arguments(access, write_paths, prefixes, system_file, compaction, resume_session, workers,
                                       session_dirs=project_config["session_dirs"])],
                      {**env, **CLAUDE_SESSION_ENV, **WPO_SESSION_ENV}, prompt, stream_input=True)
    if harness == "local":
        env = local_environment(machine, route["model"])
        compaction = autocompact_arguments(limit, int(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"]))
        arguments = [machine.executable("claude"), "--model", route["model"], "--effort", route["effort"],
                     *claude_arguments(access, write_paths, prefixes, system_file, compaction, resume_session,
                                       session_dirs=project_config["session_dirs"])]
        return Launch(arguments, {**env, **CLAUDE_SESSION_ENV, **WPO_SESSION_ENV}, prompt, stream_input=True)
    prompt = f"{brief.prompt_prefix()}\n\n{prompt}"
    if harness == "agy":
        ensure_agy_hooks(machine, cwd)
        # Outside every tree (a run directory can sit inside the main checkout): no session may
        # rewrite the policy the hook reads or forge the decisions the harness checks.
        session_dir = machine.state_dir / "agy-sessions" / hashlib.sha1(str(system_file).encode()).hexdigest()[:16]
        session_dir.mkdir(parents=True, exist_ok=True)
        policy_path, hook_log = session_dir / "policy.json", session_dir / "decisions.log"
        hook_log.unlink(missing_ok=True)
        read_dirs = [*project_config["agy_read_dirs"], *machine.raw["skills"]["dirs"]]
        policy = agy_policy(access, write_paths, prefixes, cwd, read_dirs, hook_log)
        policy_path.write_text(json.dumps(policy, indent=2), encoding="utf-8")
        effort = ["--effort", route["effort"]] if route.get("effort") else []
        # Stream-json input carries the prompt on stdin; a plain -p takes it only as an argument,
        # which a long prompt would overflow. Closing stdin after the one message ends the session.
        arguments = [machine.executable("agy"), "--input-format", "stream-json", "--output-format", "stream-json", "--model", route["model"],
                     *effort, "--dangerously-skip-permissions", "-p="]
        message = json.dumps({"event": "user", "message": {"content": f"{agy_sandbox_note(policy)}\n\n{prompt}"}})
        return Launch(arguments, {"AGY_POLICY": policy_path.as_posix(), **WPO_SESSION_ENV}, message + "\n", fatal_stderr=AGY_FATAL_STDERR,
                      sandbox_log=hook_log)
    if harness == "opencode":
        env = {"OPENCODE_CONFIG_CONTENT": opencode_config(route, access, write_paths, prefixes), **WPO_SESSION_ENV}
        if route.get("provider") == "openrouter":
            from .routing import Router  # local import: routing imports nothing from here
            key = Router(machine).openrouter_key()
            if key is None:
                raise HarnessError("OpenRouter route without an OpenRouter key")
            env["OPENROUTER_API_KEY"] = key
        variant = ["--variant", route["variant"]] if route.get("variant") else []
        # --dir is explicit: OpenCode otherwise takes its project from an inherited PWD variable.
        # Warnings and errors go to stderr, where provider refusals are the only trace they leave.
        arguments = [machine.executable("opencode"), "run", "-m", route["model"], "--format", "json", "--dir", str(cwd),
                     "--print-logs", "--log-level", "WARN", *variant]
        return Launch(arguments, env, prompt, fatal_stderr=OPENCODE_FATAL_STDERR)
    if harness == "command":
        # Test and scripted routes: `model` is a command line; it gets the access level and
        # write paths in the environment and must print Claude stream-json events.
        env = {"WPO_ACCESS": access, "WPO_WRITE_PATHS": json.dumps(write_paths), **WPO_SESSION_ENV}
        return Launch(shlex.split(route["model"], posix=True), env, prompt)
    raise HarnessError(f"unknown harness '{harness}' for route {route['id']}")
