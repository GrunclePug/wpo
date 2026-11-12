"""Commands a session runs inside its WP's tree: `wpo log` writes a dated Progress log line (and ticks
a step), `wpo verify` applies the runner's fixers and runs the WP's Verify commands and owns check,
printing only what failed. The session's environment names its WP, tree, role and route
(`WPO_WORK_PACKAGE`, `WPO_TREE`, `WPO_ROLE`, `WPO_ROUTE`), so the model never types a date or a key.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import wpfile
from .autofix import configured_fixers, run_fixers
from .scope import scoped_checks
from .commands import HEARTBEAT_FILE, build_slot, changed_paths, matches_main, run_commands_detailed, worktree_tree_id
from .context import Context
from .project import git

UNCHECKED_STEP = re.compile(r"^(\s*[-*] )\[ \]")
# The tree a session's `wpo verify` passed on; the runner skips its own verification of that tree.
SESSION_VERIFIED_TREE_FILE = "session-verified-tree.txt"
# A builder's handoff, written by `wpo handoff` and taken by the runner when the session ends.
HANDOFF_FILE = "handoff-submitted.md"


class SessionCommandError(Exception):
    """A session command run outside a WP session, or with arguments that do not fit the WP."""


def session_wp(context: Context) -> tuple[wpfile.WorkPackage, Path]:
    """The WP and tree of the session running this command."""
    wp_id = os.environ.get("WPO_WORK_PACKAGE")
    tree = Path(os.environ["WPO_TREE"]) if os.environ.get("WPO_TREE") else Path(git(Path.cwd(), "rev-parse", "--show-toplevel").stdout.strip())
    if not wp_id:
        branch = git(tree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        prefix = context.project.config["branch_prefix"]
        if not branch.startswith(prefix):
            raise SessionCommandError(f"not in a WP session or worktree (branch {branch})")
        wp_id = branch.removeprefix(prefix)
    return wpfile.find(context.project.wp_dir(tree), wp_id), tree


def tick_step(wp: wpfile.WorkPackage, number: int) -> str:
    """Ticks the `number`th checkbox (1-based) of the Steps section; returns the step's text."""
    text = wp.text()
    newline = wpfile.newline_of(text)
    lines = text.splitlines()
    try:
        start = lines.index("## Steps")
    except ValueError:
        raise SessionCommandError(f"{wp.id} has no '## Steps' section") from None
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    boxes = [index for index in range(start + 1, end) if re.match(r"^\s*[-*] \[[ xX]\]", lines[index])]
    if not 1 <= number <= len(boxes):
        raise SessionCommandError(f"{wp.id} has {len(boxes)} steps; there is no step {number}")
    index = boxes[number - 1]
    lines[index] = UNCHECKED_STEP.sub(r"\1[x]", lines[index], count=1)
    wpfile.write_text(wp.path, newline.join(lines) + newline)
    return lines[index].strip()


def log(context: Context, message: str, step: int | None) -> str:
    wp, _ = session_wp(context)
    if os.environ.get("WPO_T3_REQUEST_ID") and context.machine.execution_mode == "t3":
        from .t3 import Bridge
        Bridge(context).update(os.environ["WPO_T3_REQUEST_ID"], last_activity_at=time.time())
    if step is not None:
        tick_step(wp, step)
        wp = wpfile.parse(wp.path)
    actor = f"{os.environ.get('WPO_ROLE') or 'builder'} {os.environ.get('WPO_ROUTE') or 'unknown-route'}"
    wpfile.log_progress(wp, message.strip(), actor)
    return wpfile.parse(wp.path).latest_progress()


def handoff(context: Context, text: str) -> str:
    """Records the session's handoff; the runner starts a fresh session from it when this one ends."""
    wp, _ = session_wp(context)
    if not text.strip():
        raise SessionCommandError("the handoff is empty: say what is done, what is half done, what comes next and what you learned")
    path = context.project.wp_run_dir(wp.id) / HANDOFF_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return f"Handoff recorded ({len(text.strip()):,} characters). End your turn now; a fresh session continues from it."


def verify(context: Context) -> tuple[bool, str]:
    """The runner's checks, run by the session: the Try it section, fixers, the WP's Verify commands
    and the owns check. When all pass, the WP goes to review: the runner verifies and reviews it next."""
    from .findings import unanswered
    wp, tree = session_wp(context)
    record = context.project.wp_run_dir(wp.id) / SESSION_VERIFIED_TREE_FILE
    record.unlink(missing_ok=True)
    waiting = unanswered(context.project.wp_run_dir(wp.id))
    if waiting:
        return False, ("Answer every open BLOCKING review finding first, with `wpo finding fixed <id> '<what changed>'` or "
                       "`wpo finding veto <id> <ground> '<evidence>'`. Unanswered: " + ", ".join(finding["id"] for finding in waiting))
    # Taken before the checks run: a check that rewrites the WP's Verify cannot change what passed.
    fingerprint = checks_fingerprint(context, wp, tree)
    passed, report = run_checks(context, wp, tree)
    if passed and wp.status in ("in_progress", "changes_requested"):
        wpfile.set_status(wp, "review")
        report += "\nStatus set to review."
    if passed:
        # The fingerprint ties the pass to the checks it ran: editing the WP's Verify or Try it, or
        # the project's check settings, voids it even when the tree is unchanged.
        record.write_text(json.dumps({"tree": worktree_tree_id(tree), "checks": fingerprint}), encoding="utf-8")
    return passed, report


def checks_fingerprint(context: Context, wp: wpfile.WorkPackage, tree: Path) -> str:
    """What decides whether `run_checks` passes on a given tree, besides the tree and ownership
    (which `outside_owns` checks again): the WP's round checks, the Try it section when required,
    and every project setting the checks read (shell, timeout, fixers, environment, scoping)."""
    from .runner import session_env
    from .whats_new import try_it_text
    config = context.project.config
    inputs = {"round_checks": wp.round_checks(config["verify_round"], config["verify_workspace"]),
              "try_it": try_it_text(wp) if config["require_try_it"] else None,
              "env": session_env(context, wp.id, tree.resolve()),
              # Fixers run in their configured order, which the sorted dump below would otherwise lose.
              "autofix": list(config["autofix"].items()),
              **{key: config[key] for key in ("shell", "command_timeout_minutes", "verify_round", "verify_workspace",
                                              "check_scope", "verify_neutral_paths", "require_try_it")}}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()


def outside_owns(context: Context, wp: wpfile.WorkPackage, tree: Path) -> list[str]:
    """Paths the tree changed outside the WP's owns (its shared files and WP file aside), unless
    they are as main has them now. Git only, so cheap to run again."""
    config = context.project.config
    wp_dir, main = context.project.wp_dir_relative, context.project.main_branch
    shared = [entry["path"] for entry in config["shared_files"]]
    return [path for path in changed_paths(tree, main) if not wpfile.path_owned(path.removesuffix(" (deleted)"), wp, wp_dir, shared)
            and not wpfile.is_wp_file(path, wp_dir) and not matches_main(tree, main, path)]


def run_checks(context: Context, wp: wpfile.WorkPackage, tree: Path) -> tuple[bool, str]:
    from .runner import clipped, session_env
    from .whats_new import try_it_problem
    config = context.project.config
    if config["require_try_it"] and (problem := try_it_problem(wp)):
        return False, f"{problem} Then run wpo verify again."
    env = session_env(context, wp.id, tree)
    wp_dir = context.project.wp_dir_relative
    main = context.project.main_branch
    owned = [path for path in changed_paths(tree, main) if not path.endswith(" (deleted)") and wpfile.path_owned(path, wp, wp_dir, ())]
    lines = [f"fixers: {report}" for report in run_fixers(tree, owned, configured_fixers(config), env, config["command_timeout_minutes"])]
    commands, scope_line = scoped_checks(config, tree, wp.round_checks(config["verify_round"], config["verify_workspace"]), changed_paths(tree, main))
    if config["check_scope"]:
        lines.append(scope_line)
    if not commands:
        return False, "Nothing verifies this WP: no Verify commands (a fenced block under '## Verify') and no round checks."
    run_dir = context.project.wp_run_dir(wp.id)
    log_path = run_dir / "session-verify.log"
    with build_slot(int(config["build_slots"]), run_dir / HEARTBEAT_FILE, lambda message: print(message, flush=True)):
        failures = run_commands_detailed(config["shell"], commands, tree, env, log_path, config["command_timeout_minutes"])
    failed = {failure.command for failure in failures}
    lines += [f"{'FAIL' if command in failed else 'pass'}: {command}" for command in commands]
    limit = int(config["repair_output_chars"])
    for failure in failures:
        lines.append(f"\n### {failure.command} (exit {failure.exit_code})\n{clipped(failure.output, limit)}")
    outside = outside_owns(context, wp, tree)
    if outside:
        # Said in full, so a builder restores rather than spending a session investigating the gate.
        lines.append("\nChanged outside the WP's owns: " + ", ".join(outside)
                     + f"\nIf you did not mean to change them (a tool or test regenerated them), restore them and run verify again: "
                       f"`git restore --source={main} -- <path>` (no approval needed; delete an untracked file instead). "
                       "If the WP truly needs the change, say why with `wpo log`; the orchestrator decides.")
    return not failures and not outside, "\n".join(lines)
