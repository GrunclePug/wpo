"""A project: its main checkout, its `.orchestration/config.json`, and where its run state lives.

The configuration is always read from the main checkout, never from a WP worktree, so a tooling
or policy change applies to every lane at once and no worktree carries a stale copy.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

CONFIG_RELATIVE_PATH = Path(".orchestration") / "config.json"
# Lead-authored task specs inside a worktree; projects gitignore it with `.orchestration/runs/`.
TASKS_RELATIVE_PATH = Path(".orchestration") / "tasks"

DEFAULTS: dict = {
    "wp_dir": "docs/plan/wp",
    "worktrees_root": None,  # default: ../<main checkout name>-worktrees
    "runs_dir": ".orchestration/runs",
    "main_branch": "main",
    "branch_prefix": "wp/",
    # The project's agent instructions, relative to the tree. Claude-CLI sessions get it in their
    # brief (their CLAUDE.md discovery is off); OpenCode reads AGENTS.md itself. Null for none.
    "instructions_file": "AGENTS.md",
    # Documents every builder reads before changing anything (the WP file is always added). The
    # instructions file is already in every session's context; do not list it.
    "builder_reading": [],
    # Documents every reviewer reads besides the WP file and the review brief.
    "reviewer_reading": [],
    # Skills loaded into every session that writes a WP's code (builders, workers, staged
    # executors and assemblers); a WP adds its own with `skills: [...]`. Reviewers load the
    # skills their agent definition declares; other roles load none.
    "builder_skills": [],
    # Skills review sessions load; null takes the ones the user's reviewer agent declares.
    "reviewer_skills": None,
    # Builders fill a WP's '## Try it' section (how the user sees it working in main) before review;
    # the runner checks it is there before verifying. `wpo whats-new` prints it with the evidence
    # files these globs find ("{wp}" is the id, "{wp_lower}" the id in lower case).
    "require_try_it": True,
    "try_it_evidence": [],
    # Mechanical fixes run on a WP's changed, owned files before every verification (autofix.py).
    # Named entries, merged per name: a project overrides one, adds its own, or sets one to null.
    # Default order: format, then lint, then hygiene. Projects can disable individual entries.
    "autofix": {
        "ruff-format": {"globs": ["*.py", "*.pyi"], "command": ["uvx", "ruff", "format", "--force-exclude"]},
        "ruff-check": {"globs": ["*.py", "*.pyi"], "command": ["uvx", "ruff", "check", "--force-exclude", "--fix"]},
        "trailing-whitespace": {"globs": ["*"], "exclude": ["*.md", "*.txt"], "builtin": "trailing-whitespace"},
        "end-of-file": {"globs": ["*"], "builtin": "end-of-file"},
    },
    # A Verify failure whose failing commands all start with one of these is mechanical (lint, format,
    # style): a fresh session on `repair_spec` fixes it from the diagnostics, without exploring, and
    # the builder gets the round only if that does not pass. Test and build failures never qualify.
    "mechanical_checks": ["cargo fmt", "cargo clippy", "rustfmt", "ruff", "black", "isort", "prettier", "eslint", "biome"],
    # Output markers of a compile or type error, which lint commands also report: not mechanical.
    "mechanical_rejects": ["error[E", "error TS"],
    "repair_spec": "code-simple",
    # Each failing command's output in a repair prompt is cut to this many characters (head and tail).
    "repair_output_chars": 16000,
    # The next builder round continues the previous builder session (`--resume`) instead of starting
    # over, when the round failed on verification, review or stopping short, or a re-evaluation
    # decided 'continue'; the tree's HEAD and the builder tier are unchanged; the route is still
    # available; and the session's context is below its harness's handoff point. Otherwise, when the
    # previous session ran on a Claude-CLI harness, it is resumed once to write a handoff, and the
    # next round starts fresh from that handoff. A session that ended more than `cold_after_minutes`
    # ago (past the prompt cache's lifetime) with at least `cold_context_tokens` of context also
    # hands off: continuing it would re-read that whole context uncached.
    "resume": {"harnesses": ["claude", "gpt", "xai", "local"], "cold_after_minutes": 60, "cold_context_tokens": 100000},
    # A builder session on a Claude-CLI harness is asked to write a handoff and end ('RESULT:
    # handoff') when its context reaches `context_tokens` (per harness, "default" for the others;
    # below each harness's compaction point, so the harness never compacts first), or when its
    # route's subscription is about to run out: a window forecast to reach its cap within the
    # routing policy's `burn_forecast.lead_minutes` at the recent burn rate, or at `quota_percent`
    # whatever the rate (so the summary is written while the route still answers). A fresh session
    # continues from the handoff without counting a round.
    "handoff": {"context_tokens": {"default": 180000, "gpt": 140000}, "quota_percent": 97},
    # Claude-CLI sessions compact at this many tokens when their route's window is larger, per
    # harness ("default" for the others). GPT turns are dearer in quota than Claude's cached reads,
    # so GPT sessions compact earlier.
    "autocompact_tokens": {"default": 275000, "gpt": 160000},
    # Changed paths (globs) whose diff the review brief leaves out, listing only their line counts.
    "review_diff_exclude": [],
    # Definition patterns per file extension for the review brief's symbol index, merged over the
    # built-in ones (review_brief.py); each needs a named group 'name'.
    "review_symbol_patterns": {},
    # Untracked files larger than this are listed by size, not shown (generated data, captures).
    "review_untracked_max_bytes": 1000000,
    # A symbol with more textual references than this is listed with counts per file only.
    "review_reference_limit": 40,
    # The review brief goes into the prompt up to this many characters; beyond it the prompt
    # carries the symbol index and the reviewer reads the diff from the brief file.
    "review_inline_chars": 120000,
    # Extra lines for review prompts (project rules a reviewer must enforce).
    "review_rules": [],
    # Extra lines appended to every builder, worker and dispatch prompt (hard project rules).
    "prompt_rules": [],
    # Checks every round runs after the WP's own Verify commands (wpo verify, the runner): cheap,
    # workspace-wide checks such as format and lint. WP files list only their own, scoped checks.
    "verify_round": [],
    # The full checks (the whole test suite) run only when a WP merges into main, on the merge.
    # They must leave every file as it was: a check that writes into the tree fails the merge.
    "verify_workspace": [],
    # Paths no build or test reads (plans, orchestration state). A branch whose verified tree only
    # gained main's changes to these paths is merged without verifying again.
    "verify_neutral_paths": [],
    # Runs workspace-wide checks only for the packages a change reaches (`scope.py`): {"kind":
    # "cargo"} replaces `--workspace` in verify_round and verify_workspace commands with one `-p`
    # per changed package and its dependents. Null checks the whole workspace every time.
    # "mentioned_paths" (globs) are files outside every package that only a package naming them in
    # its sources reads (`include_str!("../docs/x.ron")`): such a file reaches those packages, or none.
    # "changed_only" (regexes): a flagged command matching one selects only the changed packages,
    # not their dependents (slow suites such as install tests; the merge gate runs them all).
    "check_scope": None,
    # Commands started detached in a new worktree so its first build is warm by the time the builder
    # tests (it reads code first). `{packages}` becomes one `-p` per cargo package the WP owns,
    # `{reach}` the same plus every package depending on them (what scoped checks will build); a
    # command whose placeholder selects nothing is skipped. Not a check: failures are ignored.
    "worktree_prewarm": [],
    # Command prefixes a builder (and a delegated worker) may run; reviewers get reviewer_commands.
    "builder_commands": ["git status", "git diff"],
    "reviewer_commands": ["git status", "git diff"],
    # Files any WP may change besides its owns, with the rule a reviewer checks.
    "shared_files": [],
    # Environment for every session, verification and integration of a WP; "{wp}" is the WP id.
    "env": {},
    # Directories removed once a WP merges (per-WP build output); "{wp}" is the WP id.
    "remove_after_merge": [],
    # Free GiB required on a WP checkout's volume before its runner starts; null disables.
    "minimum_build_free_gib": None,
    # Commands run in a new worktree before its first session (generated build inputs).
    "worktree_setup": [],
    # Lock files that conflict on every parallel dependency change, and how to regenerate them.
    "lockfile_reconcilers": [],
    # Commands run in the main checkout after a merge that changed matching paths.
    "post_merge_hooks": [],
    # Per-lane settings: builder_override, families, mode ("staged"), staged {...}, pool (a pool lane,
    # SKILL.md), builds_in_pool (the lane's supervisor never builds: pool lanes build its WPs, and it
    # starts only to review them, so no second session reaches a pool's model).
    "lanes": {},
    "delegate_default": "code-simple",
    # A lead builder on a Claude-CLI hosted route (claude, gpt) may start in-session `worker`
    # subagents on this model and effort for small mechanical tasks; their reading and tool output
    # stay out of the lead's context. Null disables them.
    "session_workers": {"model": "haiku", "effort": "low"},
    # Workers never spend subscription quota.
    "worker_families": ["free", "agy", "xai", "local"],
    # Directories agy sessions may read besides the tree and the skill directories (its sandbox
    # denies every other read; Claude-CLI sessions read anywhere).
    "agy_read_dirs": [],
    # Directories outside the repository that builder and worker sessions on Claude-CLI harnesses
    # may read and write (`--add-dir`): tool checkouts and their builds, never installs.
    "session_dirs": [],
    # Facts about this machine every builder gets in its prompt (toolchains and where they are,
    # what is absent, data locations), so no session spends turns re-discovering them.
    "environment": [],
    "idle_minutes": 30,
    # A hosted subscription session that has produced no first model turn after this long is
    # stalled (a silent cap, a stuck proxy) and is stopped as hung, which cools its whole family
    # down. Local and free routes are exempt: a local model's prefill of a large prompt is slow.
    "first_output_minutes": 6,
    # A verify, setup or hook command running longer than this is killed and fails.
    "command_timeout_minutes": 120,
    # Machine-wide slots for verification runs (`wpo verify` in a session and the runner's checks):
    # one more waits until a slot frees, so parallel lanes do not run more full builds than the
    # disks and memory carry. Integration takes none (one runs at a time anyway). 0: no limit.
    "build_slots": 0,
    # Lanes `wpo lanes resume` keeps live at once, pool lanes not counted; lanes nearest to merging
    # (WPs in review or changes requested) start first. 0: no limit.
    "max_active_lanes": 0,
    # Output that marks a failed check as the machine's fault, not the code's (a killed command, a
    # full disk, a DLL that could not start under memory pressure, a full page file): when every
    # failure carries one, the verification is retried after `infrastructure_retry_minutes`
    # without counting a failed round.
    "infrastructure_failure_markers": ["(killed after", "STATUS_DLL_INIT_FAILED", "0xc0000142", "os error 1455", "paging file is too small",
                                       "There is not enough space on the disk", "os error 112", "No space left on device", "LNK1102", "LNK1201"],
    "infrastructure_retry_minutes": 20,
    # A builder session that ends short of review (no passing wpo verify) and not blocked is
    # continued at once without counting a failed round, up to this many times in a row; then the
    # round counts as stopped short.
    "interrupted_session_resumes": 3,
    "worker_idle_minutes": 20,
    # Free routes stall server-side far more often than paid ones (a reply that never comes), so
    # their sessions are given up on sooner. Tool-process CPU still counts as activity.
    "free_idle_minutes": 10,
    # A WP is re-evaluated (why is it not converging, what changes) once this many rounds failed
    # since the last re-evaluation; a builder that reports itself blocked is re-evaluated at once.
    "evaluate_after_failures": 2,
    "evaluation_spec": "code-hard",
    # Re-evaluations that did not make a WP converge before the orchestrator takes it over.
    "orchestrator_after_evaluations": 3,
    "integration_mutex": None,  # default: derived from the main checkout path
    "commit_footer": "",
    # Optional generated board, relative to the main checkout (keep it gitignored).
    "status_board": None,
    # The orchestrator's 5h usage must be under this before a supervisor reports "finished"; null disables.
    "orchestrator_room_percent": 70,
    # The model family the orchestrator runs on, whose room that is. Null: under T3, the family of the
    # provider its coordinator was attached from (`wpo t3 attach`), else claude.
    "orchestrator_family": None,
    # Overrides merged over the skill's routing policy (models/tiers.json).
    "routing": {},
    # Staged mode for small-context models (SKILL.md, "Staged mode").
    "staged": {"planner": "code-hard", "executor": "code-simple", "assembler": None, "context_tokens": 50000, "max_replans": 2},
    "gate": {"clean": None, "extra": []},
    # `wpo lint` (lint.py). `forbidden_verify`: Verify commands the project forbids, each
    # {"pattern": <regex searched in each command>, "message": <why, and what to run instead>,
    # "severity": "error" (default) or "warning"}. `similarity_warn`: a WP whose TF-IDF cosine with
    # another WP (title, Goal, Deliverables, Acceptance criteria; done WPs included) reaches this is
    # warned that its work may be done or planned there, naming the `similarity_top` most similar;
    # null disables it. Tune the example threshold against the project's WP corpus (similarity.py).
    "lint": {"forbidden_verify": [], "similarity_warn": 0.30, "similarity_top": 3},
    # `wpo audit` (audit.py): lint over every open WP into `<runs>/audits/<date>.md`, then a read-only
    # review session on `spec` (never a free or local route) for the judgment parts, which reads
    # `context_files` (the decisions log, the instructions file; paths in the main checkout) first.
    # `schedule`: explicit standalone OS scheduling (Windows task/Linux user timer; day MON..SUN,
    # local time HH:MM).
    "audit": {"spec": "code-hard", "context_files": [], "schedule": {"day": "MON", "time": "09:00"}},
    # How verify and hook commands are run.
    "shell": ["pwsh", "-NoProfile", "-Command"] if sys.platform == "win32" else ["bash", "-lc"],
}


class ProjectError(Exception):
    """A project that cannot be located or whose configuration is invalid."""


def _merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        merged[key] = _merge(base[key], value) if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return merged


def git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and completed.returncode != 0:
        raise ProjectError(f"git {' '.join(arguments)} in {root} failed: {(completed.stderr or completed.stdout).strip()}")
    return completed


def main_checkout_of(path: Path) -> Path:
    """The main working tree of the repository containing `path`: the first `git worktree list` entry."""
    listing = git(path, "worktree", "list", "--porcelain").stdout
    first = listing.splitlines()[0] if listing else ""
    if not first.startswith("worktree "):
        raise ProjectError(f"git worktree list named no main worktree for {path}")
    return Path(first.removeprefix("worktree ").strip()).resolve()


@dataclass(frozen=True)
class Project:
    """A configured repository; `root` is its main checkout."""

    root: Path
    config: dict
    execution_mode: str = "standalone"

    @classmethod
    def locate(cls, start: Path | None = None, machine_defaults: dict | None = None, execution_mode: str = "standalone") -> Project:
        root = main_checkout_of((start or Path.cwd()).resolve())
        config_path = root / CONFIG_RELATIVE_PATH
        if not config_path.exists():
            raise ProjectError(f"{config_path} does not exist; copy templates/config.example.json from the skill and adapt it")
        # Every key overrides its default on its own (objects merge per key), so `{}` is all defaults.
        override = json.loads(config_path.read_text(encoding="utf-8"))
        config = _merge(_merge({**DEFAULTS, "name": root.name}, machine_defaults or {}), override)
        unknown = (set(override) | set(machine_defaults or {})) - set(DEFAULTS) - {"name", "$comment"}
        if unknown:
            raise ProjectError(f"{config_path}: unknown keys {sorted(unknown)}")
        return cls(root, config, execution_mode)

    @property
    def name(self) -> str:
        return self.config["name"]

    @property
    def runs_dir(self) -> Path:
        directory = self.root / self.config["runs_dir"]
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def wp_run_dir(self, wp_id: str) -> Path:
        directory = self.runs_dir / wp_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @property
    def wp_dir_relative(self) -> str:
        return self.config["wp_dir"].replace("\\", "/").rstrip("/")

    def wp_dir(self, tree: Path | None = None) -> Path:
        return (tree or self.root) / self.wp_dir_relative

    @cached_property
    def worktrees_root(self) -> Path:
        """Resolved once per Project: every effective-WP listing asks for each WP's worktree."""
        configured = self.config["worktrees_root"]
        if configured:
            return (self.root / configured).resolve()
        return self.root.parent / f"{self.root.name}-worktrees"

    def worktree(self, wp_id: str) -> Path:
        if self.execution_mode == "t3":
            record = self.root / self.config["runs_dir"] / wp_id / "t3-workspace.json"
            if record.is_file():
                return Path(json.loads(record.read_text(encoding="utf-8"))["path"])
        return self.worktrees_root / wp_id

    def branch(self, wp_id: str) -> str:
        if self.execution_mode == "t3":
            record = self.root / self.config["runs_dir"] / wp_id / "t3-workspace.json"
            if record.is_file():
                return json.loads(record.read_text(encoding="utf-8"))["branch"]
        return f"{self.config['branch_prefix']}{wp_id}"

    @property
    def main_branch(self) -> str:
        return self.config["main_branch"]

    def mutex_name(self, kind: str, key: str = "") -> str:
        """A machine-wide mutex name unique to this project (its main checkout path)."""
        digest = hashlib.sha1(str(self.root).lower().encode()).hexdigest()[:12]
        return f"Global\\wpo-{kind}-{digest}" + (f"-{key}" if key else "")

    @property
    def integration_mutex(self) -> str:
        return self.config["integration_mutex"] or self.mutex_name("integrate")

    def env_for(self, wp_id: str | None) -> dict[str, str]:
        """The project environment for one WP's sessions and commands, "{wp}" substituted."""
        return {key: str(value).replace("{wp}", wp_id or "none") for key, value in self.config["env"].items()}

    def paths_removed_after_merge(self, wp_id: str) -> list[Path]:
        """The WP's configured build output, including its actual retained native checkout."""
        from .artifacts import cleanup_paths
        return cleanup_paths(self, wp_id)

    def lane(self, name: str) -> dict:
        return self.config["lanes"].get(name, {})

    @property
    def commit_footer(self) -> str:
        return self.config["commit_footer"]
