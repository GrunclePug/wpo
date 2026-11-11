"""The orchestrator's own tools: detached jobs, ad-hoc dispatches, committing to main, gate checks."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import wpfile
from .commands import run_commands
from .context import Context
from .platform import NamedMutex, spawn_detached
from .project import git
from .session import NoRouteError, SessionResult, describe_next, run_session

EXIT_NO_ROUTE = 4
# Routes of these cost classes never run a gate-like session (a placement, an audit): a free model's
# queue or a local model is not trusted with the plan the orchestrator works from.
EXCLUDED_COST_CLASSES = ("free", "local")
# The line a dispatched session that may write WP files gets, so every drafter sees lint's findings.
DRAFTING_RULE = ("After writing or changing a WP file, run `wpo lint <WP id>` and resolve what it reports. A similarity warning "
                 "names WPs whose work reads like this one's, done or planned: read them (a done WP's Handoff says what it "
                 "delivered), then merge this work into that WP, or cite it in Context and say what this WP adds.")


def start_job(context: Context, name: str, command: list[str]) -> int:
    """Runs a command detached (it outlives any shell time limit) and journals
    orchestrator-job-done with its exit code when it ends. `wpo` as the first word is this tool."""
    log_path = context.project.runs_dir / "jobs" / f"{name}-{datetime.now():%Y%m%d-%H%M%S}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid = spawn_detached([*context.machine.wpo_command(), "job", "exec", "--name", name, "--log", str(log_path), "--", *command],
                         context.project.root)
    print(f"job '{name}' started (pid {pid}); log {log_path}; it journals orchestrator-job-done")
    return pid


def exec_job(context: Context, name: str, log_path: Path, command: list[str]) -> int:
    arguments = [*context.machine.wpo_command(), *command[1:]] if command and command[0] == "wpo" else command
    with log_path.open("ab") as log:
        code = subprocess.run(arguments, cwd=str(context.project.root), stdout=log, stderr=subprocess.STDOUT).returncode
    context.journal.emit("orchestrator-job-done", f"{name}: exit {code} (log {log_path})", {"job": name})
    return code


class MainCommitError(RuntimeError):
    """The main checkout is not on the main branch, or git refused the commit."""


class NoTrustedFamilyError(RuntimeError):
    """The routing policy has no family whose routes are neither free nor local."""


def trusted_families(context: Context) -> list[str]:
    """The router's families a placement or an audit may run on: none whose routes are free or local."""
    cost_classes = context.router.policy.get("family_cost_class", {})
    families = [family for family in context.router.families if cost_classes.get(family, "subscription") not in EXCLUDED_COST_CLASSES]
    if not families:
        raise NoTrustedFamilyError("the routing policy has no family that is neither free nor local")
    return families


def writes_wp_files(context: Context, write_paths: list[str]) -> bool:
    """Whether a session's write paths reach the project's WP files."""
    from .lint import patterns_overlap
    sample = f"{context.project.wp_dir_relative}/WP-0000-draft.md"
    return any(patterns_overlap(path.replace("\\", "/"), sample) for path in write_paths)


def dispatch_session(context: Context, tier: str, prompt: str, access: str, write_paths: list[str], families: list[str], cwd: Path,
                     run_dir: Path, event_fields: dict[str, str]) -> SessionResult:
    """One ad-hoc session as `wpo dispatch` runs it: the project's prompt rules appended, the answer
    saved as `result.md` in `run_dir`, routed in the project's mode. Raises NoRouteError when nothing
    serves the tier."""
    from .routing import project_mode, with_mode
    tier = with_mode(tier, project_mode(context.project.runs_dir))
    run_dir.mkdir(parents=True, exist_ok=True)
    drafting = [DRAFTING_RULE] if access == "docs" and writes_wp_files(context, write_paths) else []
    rules = "\n".join([*context.project.config["prompt_rules"], *drafting])
    full_prompt = f"{prompt}\n\n{rules}\nEnd with your complete answer; it is saved as the task's result."
    result = run_session(context, tier, access, full_prompt, run_dir / "session", cwd, write_paths=write_paths,
                         families=families, event_fields=event_fields)
    (run_dir / "result.md").write_text(result.result_text, encoding="utf-8")
    return result


def dispatch(context: Context, tier: str, name: str, prompt: str, access: str, write_paths: list[str], families: list[str], cwd: Path | None) -> int:
    """Sends one self-contained task (drafting, research, triage) to whatever serves its tier;
    journals dispatch-done or dispatch-failed and prints the answer."""
    run_dir = context.project.runs_dir / "dispatch" / f"{name}-{datetime.now():%Y%m%d-%H%M%S}"
    try:
        result = dispatch_session(context, tier, prompt, access, write_paths, families, cwd or context.project.root, run_dir, {"dispatch": name})
    except NoRouteError as error:
        context.journal.emit("dispatch-failed", f"{name} ({tier}): {error}; next route at {describe_next(error.next_available_at)}", {"dispatch": name})
        print(f"NO ROUTE for {tier}; next route at {describe_next(error.next_available_at)}\n{error}")
        return EXIT_NO_ROUTE
    except Exception as error:
        context.journal.emit("dispatch-failed", f"{name} ({tier}): {type(error).__name__}: {error}", {"dispatch": name})
        raise
    result_path = run_dir / "result.md"
    context.journal.emit("dispatch-done", f"{name} ({tier}) finished on {result.route['id']}{result.fallback_note}; result {result_path}",
                         {"dispatch": name, "route": result.route["id"]})
    print(f"route: {result.route['id']} (score {result.route['score']}){result.fallback_note}\nresult: {result_path}\n\n{result.result_text}")
    return 0


def commit_main(context: Context, message: str, paths: list[str]) -> int:
    """Commits the orchestrator's edits to main under the integration mutex, so no fast-forward
    races the commit. Only the named paths are committed."""
    require_main_branch(context)
    with NamedMutex(context.project.integration_mutex):
        completed = commit_paths(context, message, paths)
    if completed.returncode != 0:
        print((completed.stderr or completed.stdout).strip())
        return completed.returncode
    print(git(context.project.root, "log", "--oneline", "-1").stdout.strip())
    report_committed_wp_lint(context)
    return 0


def committed_wp_ids(context: Context, revision: str = "HEAD") -> tuple[list[str], dict[str, str]]:
    """The ids of the WP files a commit added or changed (deleted ones left out), and the files
    among them that do not parse, with why."""
    root, wp_dir = context.project.root, context.project.wp_dir_relative
    names = git(root, "show", "--name-only", "--diff-filter=d", "--format=", revision).stdout.splitlines()
    ids, unparsed = [], {}
    for name in names:
        if not wpfile.is_wp_file(name, wp_dir) or name.endswith(f"/{wpfile.TEMPLATE_NAME}"):
            continue
        try:
            ids.append(wpfile.parse(root / name).id)
        except wpfile.WorkPackageError as error:
            unparsed[name] = str(error)
    return sorted(set(ids)), unparsed


def report_committed_wp_lint(context: Context) -> None:
    """Lints the WPs the last commit changed and prints what it found. Nothing here undoes or blocks
    the commit: warnings ask for a decision, errors are printed loudly for the orchestrator to judge."""
    from .lint import LintConfigError, lint, summary_lines
    wp_ids, unparsed = committed_wp_ids(context)
    for name, error in unparsed.items():
        print(f"LINT ERROR: {name} does not parse: {error}")
    if not wp_ids:
        return
    try:
        findings = lint(context, wp_ids)
    except LintConfigError as error:
        print(f"LINT NOT RUN: the project's lint settings are invalid: {error}")
        return
    except SystemExit as error:  # lint_targets: a committed WP the listing of main does not hold
        print(f"LINT NOT RUN: {error}")
        return
    print(f"wpo lint {' '.join(wp_ids)}:")
    for finding in findings:
        print(f"  {finding}")
    for line in summary_lines(findings):
        print(f"  {line}")
    errors = [finding for finding in findings if finding.severity == "error"]
    if errors:
        print(f"!! {len(errors)} LINT ERRORS in the committed WPs (the commit stands; fix them or decide): "
              + "; ".join(f"{finding.wp_id}: {finding.message}" for finding in errors))


def require_main_branch(context: Context) -> None:
    branch = git(context.project.root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != context.project.main_branch:
        raise MainCommitError(f"the main checkout is on '{branch}', not {context.project.main_branch}")


def commit_paths(context: Context, message: str, paths: list[str]) -> subprocess.CompletedProcess:
    """Commits the named paths of the main checkout with the project's footer. The caller holds the
    integration mutex."""
    root = context.project.root
    footer = f"\n\n{context.project.commit_footer}" if context.project.commit_footer else ""
    # Paths already removed (git rm) are only in the commit's pathspec; `git add` rejects them.
    present = [path for path in paths if (root / path).exists()]
    if present:
        git(root, "add", "-A", "--", *present)
    return git(root, "commit", "--quiet", "-m", message + footer, "--", *paths, check=False)


def edit_and_commit_main(context: Context, message: str, edit: Callable[[], list[str]]) -> str:
    """Edits files in the main checkout and commits them in one hold of the integration mutex, so no
    fast-forward meets the edit uncommitted (SKILL.md, Lessons: edit and commit in one step).
    `edit` returns the repository-relative paths it changed; returns the new commit's one-line log."""
    require_main_branch(context)
    with NamedMutex(context.project.integration_mutex):
        paths = edit()
        if not paths:
            raise MainCommitError("the edit changed no file; nothing to commit")
        completed = commit_paths(context, message, paths)
    if completed.returncode != 0:
        raise MainCommitError(f"git commit of {', '.join(paths)} failed: {(completed.stderr or completed.stdout).strip()}")
    return git(context.project.root, "log", "--oneline", "-1").stdout.strip()


def gate_verify(context: Context, milestone: str, skip_clean: bool) -> int:
    """Re-runs every Verify command of a milestone's WPs plus the project's gate extras, after a
    clean build; writes one log and a summary."""
    gate = context.project.config["gate"]
    run_dir = context.project.runs_dir / f"gate-{milestone}"
    run_dir.mkdir(parents=True, exist_ok=True)
    commands: list[str] = []
    for wp in context.main_wps():
        if wp.milestone == milestone:
            commands += [command for command in wp.verify_commands() if command not in commands]
    commands += [command for command in gate["extra"] if command not in commands]
    if gate["clean"] and not skip_clean:
        commands.insert(0, gate["clean"])
    shell = context.project.config["shell"]
    summary = [f"gate verify {milestone} started {datetime.now():%Y-%m-%d %H:%M}"]
    failed = 0
    for index, command in enumerate(commands, 1):
        started = datetime.now()
        # A gate builds from clean, so its commands get far longer than a WP's.
        failures = run_commands(shell, [command], context.project.root, context.project.env_for(None), run_dir / f"{index:02d}.log",
                                timeout_minutes=4 * float(context.project.config["command_timeout_minutes"]))
        failed += bool(failures)
        summary.append(f"{index:02d} {'FAIL' if failures else 'ok'} ({(datetime.now() - started).total_seconds() / 60:.1f} min): {command}")
    summary.append(f"gate verify finished {datetime.now():%Y-%m-%d %H:%M}")
    (run_dir / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n".join(summary))
    return 1 if failed else 0
