"""The `wpo` command line. Every subcommand finds its project from the working directory (any
worktree of the repository works) unless --project names one."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from . import ENTRY_POINT, SKILL_ROOT


def entrypoint() -> int:
    """Run the CLI with actionable errors for an unavailable T3 backend."""
    from .boot import BootError
    from .t3 import T3Error

    from .runner import EXIT_BACKEND

    try:
        return main()
    except T3Error as error:
        print(f"T3 backend: {error}", file=sys.stderr)
        return EXIT_BACKEND
    except BootError as error:
        print(f"Boot safety: {error}", file=sys.stderr)
        return EXIT_BACKEND


def split_list(values: list[str] | None) -> list[str]:
    return [part.strip() for value in values or [] for part in value.split(",") if part.strip()]


def command_names() -> frozenset[str]:
    """wpo's own subcommand names, read from the parser so other code cannot drift from it."""
    return frozenset(name for action in build_parser()._actions if isinstance(action, argparse._SubParsersAction)
                     for name in action.choices)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wpo", description="Parallel work-package orchestration on git worktrees (SKILL.md).")
    parser.add_argument("--project", type=Path, help="any directory inside the project's repository (default: the working directory)")
    parser.add_argument("--session", help="T3 bridge session identity (provided in native task prompts)")
    commands = parser.add_subparsers(dest="command", required=True)

    from .t3 import add_cli
    add_cli(commands)
    from .continuous import add_cli as add_continuous_cli
    add_continuous_cli(commands)

    route = commands.add_parser("route", help="the tier router")
    route_commands = route.add_subparsers(dest="route_command", required=True)
    candidates = route_commands.add_parser("candidates")
    candidates.add_argument("--spec", required=True)
    candidates.add_argument("--families", action="append")
    candidates.add_argument("--refresh", action="store_true")
    candidates.add_argument("--gate", action="store_true", help="rank as a review or re-evaluation: gate-only routes, no free fallback")
    route_commands.add_parser("status")
    refresh = route_commands.add_parser("refresh")
    refresh.add_argument("--force", action="store_true")
    mark = route_commands.add_parser("mark")
    mark.add_argument("--route", required=True)
    mark.add_argument("--outcome", required=True)
    mark.add_argument("--detail", default="")
    mode = route_commands.add_parser("mode", help="show or set the project's routing mode (WPs keep the mode they started in)")
    mode.add_argument("mode", nargs="?", help="standard, or a mode the routing policy defines")
    clear = route_commands.add_parser("clear", help="drop cooldowns (a cap lifted early)")
    clear.add_argument("--prefix", default="", help="e.g. family:gpt; empty clears every cooldown")

    events = commands.add_parser("events", help="the orchestrator's event journal")
    event_commands = events.add_subparsers(dest="events_command", required=True)
    emit = event_commands.add_parser("emit")
    emit.add_argument("--kind", required=True)
    emit.add_argument("--message", required=True)
    emit.add_argument("--field", action="append", default=[])
    wait = event_commands.add_parser("wait")
    wait.add_argument("--after", type=int, required=True)
    wait.add_argument("--timeout-minutes", type=float, default=110)
    wait.add_argument("--quiet", action="append", help="attention kinds to treat as info for this wait (e.g. no-route)")
    tail = event_commands.add_parser("tail")
    tail.add_argument("--after", type=int, default=0)
    event_commands.add_parser("cursor")

    run = commands.add_parser("run", help="build, verify and review one WP in its worktree")
    run.add_argument("--wp", required=True)
    run.add_argument("--tree", type=Path, help="default: the WP's worktree")
    run.add_argument("--lane")
    run.add_argument("--builder", help="override the builder spec (a tier or pin:<route>)")
    run.add_argument("--reviewer", help="override the reviewer spec")
    run.add_argument("--families", action="append")
    run.add_argument("--idle-minutes", type=float)

    delegate = commands.add_parser("delegate", help="a lead builder's workers (inside a builder session)")
    delegate_commands = delegate.add_subparsers(dest="delegate_command", required=True)
    start = delegate_commands.add_parser("start")
    start.add_argument("--name", required=True)
    start.add_argument("task", nargs="?", help="the task, in full (or --prompt-file)")
    start.add_argument("--prompt-file")
    start.add_argument("--write-paths", action="append", required=True)
    start.add_argument("--tier")
    delegate_wait = delegate_commands.add_parser("wait")
    delegate_wait.add_argument("--name", required=True)
    delegate_wait.add_argument("--minutes", type=float, help="return after this long even if the worker still runs (default: when it finishes)")
    delegate_commands.add_parser("list")
    delegate_run = delegate_commands.add_parser("run")
    delegate_run.add_argument("--name", required=True)

    supervise = commands.add_parser("supervise", help="run a lane or milestone unattended (normally via `lanes resume`)")
    target = supervise.add_mutually_exclusive_group(required=True)
    target.add_argument("--lane")
    target.add_argument("--milestone")
    supervise.add_argument("--wps", action="append")
    supervise.add_argument("--families", action="append")
    supervise.add_argument("--builder")
    supervise.add_argument("--idle-minutes", type=float)

    integrate = commands.add_parser("integrate", help="merge a done WP into main")
    integrate.add_argument("--wp", required=True)
    integrate.add_argument("--verify", action="store_true", help="verify even if the branch already contains main")

    lanes = commands.add_parser("lanes", help="lane supervisors")
    lane_commands = lanes.add_subparsers(dest="lanes_command", required=True)
    resume = lane_commands.add_parser("resume")
    resume.add_argument("targets", nargs="*", metavar="LANE|WP", help="only these lanes, or the lanes of these WPs (default: every lane)")
    resume.add_argument("--dry-run", action="store_true")
    for name in ("stop", "release"):
        lane_commands.add_parser(name).add_argument("lane")
    hold = lane_commands.add_parser("hold")
    hold.add_argument("lane")
    hold.add_argument("--reason", default="held by the orchestrator")

    commands.add_parser("status", help="print the board (and write the board file when configured)")
    clean_builds = commands.add_parser("clean-builds", help="plan build-output cleanup for WPs done on main; preserves checkout/source/history")
    clean_builds.add_argument("--wp", action="append", default=[], help="only these WPs (repeatable; default: all)")
    clean_builds.add_argument("--apply", action="store_true", help="remove the reported output; default is a dry run")

    job = commands.add_parser("job", help="detached orchestrator jobs")
    job_commands = job.add_subparsers(dest="job_command", required=True)
    job_start = job_commands.add_parser("start")
    job_start.add_argument("--name", required=True)
    job_start.add_argument("job_arguments", nargs=argparse.REMAINDER)
    job_exec = job_commands.add_parser("exec")
    job_exec.add_argument("--name", required=True)
    job_exec.add_argument("--log", type=Path, required=True)
    job_exec.add_argument("job_arguments", nargs=argparse.REMAINDER)

    dispatch = commands.add_parser("dispatch", help="an ad-hoc tier-routed task")
    dispatch.add_argument("--tier", required=True)
    dispatch.add_argument("--name", required=True)
    prompt = dispatch.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file", type=Path)
    dispatch.add_argument("--access", choices=("read", "docs", "builder"), default="read")
    dispatch.add_argument("--write-paths", action="append")
    dispatch.add_argument("--families", action="append")
    dispatch.add_argument("--cwd", type=Path)

    commit = commands.add_parser("commit-main", help="commit orchestrator edits to main under the integration mutex")
    commit.add_argument("-m", "--message", required=True)
    commit.add_argument("paths", nargs="+")

    gate = commands.add_parser("gate", help="re-run a milestone's Verify commands on a clean build")
    gate.add_argument("--milestone", required=True)
    gate.add_argument("--skip-clean", action="store_true")

    news = commands.add_parser("whats-new", help="WPs merged into main lately, with how to try each")
    news.add_argument("--since", type=datetime.fromisoformat, help="ISO date or time, e.g. 2026-10-23 or 2026-10-23T18:00")
    news.add_argument("--last", type=int, help="only the N most recent merges")

    progress = commands.add_parser("log", help="(in a WP session) append a dated Progress log line, optionally ticking a step")
    progress.add_argument("--step", type=int, help="tick this step's checkbox (1 is the first step)")
    progress.add_argument("message", help="what was done; what is next; blockers")
    commands.add_parser("verify", help="(in a WP session) apply the fixers, run the WP's Verify commands and owns check; print what failed")
    handoff = commands.add_parser("handoff", help="(in a WP builder session) hand the work to a fresh session: the handoff text, or - to read it from stdin")
    handoff.add_argument("text")
    finding = commands.add_parser("finding", help="(in a WP session) answer an open BLOCKING review finding")
    finding_commands = finding.add_subparsers(dest="finding_command", required=True)
    fixed = finding_commands.add_parser("fixed", help="the finding is fixed")
    fixed.add_argument("finding_id", metavar="id")
    fixed.add_argument("text", help="what changed, with file:line")
    veto = finding_commands.add_parser("veto", help="the finding breaks the review protocol; name the ground")
    veto.add_argument("finding_id", metavar="id")
    veto.add_argument("ground", help="no-basis, misquoted, outside-wp, not-reproduced or contradicts-wp")
    veto.add_argument("text", help="the evidence")
    ask = commands.add_parser("ask-approval", help="(in a WP session) ask the orchestrator to approve a command the sandbox denies")
    ask.add_argument("approval_command", metavar="command", help="the exact command")
    ask.add_argument("why", help="why the work needs it")
    run_approved = commands.add_parser("run-approved", help="(in a WP session) run an approved command once")
    run_approved.add_argument("approval_id", metavar="id")
    approve = commands.add_parser("approve", help="approve a session's command request; the session is told")
    approve.add_argument("approval_id", metavar="id")
    approve.add_argument("--note", default="", help="a note for the session")
    deny = commands.add_parser("deny", help="deny a session's command request; the session is told")
    deny.add_argument("approval_id", metavar="id")
    deny.add_argument("--reason", required=True)
    lint = commands.add_parser("lint", help="check WP files before scheduling: sections, Verify, owns, overlaps, dependencies, similar WPs")
    lint.add_argument("wps", nargs="*", help="WP ids (default: every open WP: todo, in flight or blocked)")
    lint.add_argument("--all", action="store_true", dest="include_done", help="every WP, done ones included")
    lint.add_argument("--json", action="store_true", help="print the findings and counts as JSON")
    audit = commands.add_parser("audit", help="lint every open WP into <runs>/audits/<date>.md, then a review session for the judgment parts")
    audit_mode = audit.add_mutually_exclusive_group()
    audit_mode.add_argument("--mechanical-only", action="store_true", help="only the lint report: no review session, no event")
    audit_mode.add_argument("--install-schedule", action="store_true", help="standalone: register the weekly Windows task or Linux user timer; T3: show native scheduling guidance")
    audit_mode.add_argument("--remove-schedule", action="store_true", help="standalone: remove this project's owned OS schedule; T3: show native removal guidance")
    audit.add_argument("--show-only", action="store_true", help="with a schedule action: print the actual platform plan without writes or service commands")
    audit.add_argument("--from-os-schedule", action="store_true", help=argparse.SUPPRESS)

    show = commands.add_parser("show", help="render session logs as readable HTML conversations")
    show.add_argument("log", nargs="?", type=Path, help="one session or verification log")
    show.add_argument("--wp", help="every log of this WP, with an index")
    show.add_argument("--open", action="store_true", help="open the page in the browser")

    message = commands.add_parser("message", help="send a message to the session running for a WP, between its turns")
    message.add_argument("--wp", help="the WP whose running session receives it")
    message.add_argument("--lane", help="the WP the lane's supervisor is running now")
    message.add_argument("text")
    wrap_up = commands.add_parser("wrap-up", help="ask running sessions to log their progress and stop; their rounds are not counted")
    wrap_up.add_argument("--wp", action="append", default=[], help="a WP to pause after its current session (repeatable)")
    wrap_up.add_argument("--lane", action="append", default=[], help="hold the lane and pause the WP it runs (repeatable)")
    wrap_up.add_argument("--reason", default="the orchestrator paused it")

    usage = commands.add_parser("usage", help="token, cache and time reports from the session logs, and the effect of tooling changes")
    usage_commands = usage.add_subparsers(dest="usage_command", required=True)
    usage_report = usage_commands.add_parser("report", help="sessions grouped by family, role, route, WP or day")
    usage_report.add_argument("--since", type=datetime.fromisoformat, help="ISO date or time (default: 7 days ago)")
    usage_report.add_argument("--until", type=datetime.fromisoformat)
    usage_report.add_argument("--by", default="family,role", help="comma-separated: family, role, route, wp, day")
    usage_changes = usage_commands.add_parser("changes", help="each tooling change with the sessions that ran under it")
    usage_changes.add_argument("--since", type=datetime.fromisoformat, help="ISO date or time (default: 7 days ago)")
    usage_changes.add_argument("--by", default="family,role")
    usage_changes.add_argument("--merge-minutes", type=int, default=120, help="commits this close together count as one change")

    dashboard = commands.add_parser("dashboard", help="the live progress dashboard: print its URL, starting it when it is not running")
    dashboard_mode = dashboard.add_mutually_exclusive_group()
    dashboard_mode.add_argument("--stop", action="store_true", help="stop it; no wpo command starts it again until `wpo dashboard`")
    # The server itself, and the short-lived process that starts it detached (dashboard.py).
    dashboard_mode.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    dashboard_mode.add_argument("--detach-server", action="store_true", help=argparse.SUPPRESS)

    place = commands.add_parser("place-request", help="(started by the dashboard) run the placement agent on a queued user request")
    place.add_argument("--id", required=True, dest="placement_id", help="the placement id under <runs>/placements/")

    commands.add_parser("install-shim", help="put the `wpo` command on PATH")
    init = commands.add_parser("init", help="add .orchestration/ to a repository")
    init.add_argument("--name", required=True)
    return parser


def install_shim() -> int:
    from .machine import MachineConfig
    machine = MachineConfig.load()
    directory = machine.shim_dir
    directory.mkdir(parents=True, exist_ok=True)
    python, entry = Path(machine.python).as_posix(), ENTRY_POINT.as_posix()
    shell_python = f"/{python[0].lower()}{python[2:]}" if len(python) > 1 and python[1] == ":" else python
    shell_entry = f"/{entry[0].lower()}{entry[2:]}" if len(entry) > 1 and entry[1] == ":" else entry
    (directory / "wpo").write_text(f'#!/bin/sh\nexec "{shell_python}" -I -B "{shell_entry}" "$@"\n', encoding="utf-8", newline="\n")
    if sys.platform == "win32":
        (directory / "wpo.cmd").write_text(f'@"{machine.python}" -I -B "{ENTRY_POINT}" %*\r\n', encoding="utf-8", newline="")
    else:
        (directory / "wpo").chmod(0o755)
    print(f"installed wpo in {directory}")
    return 0


def init_project(start: Path, name: str) -> int:
    from .project import CONFIG_RELATIVE_PATH, main_checkout_of
    root = main_checkout_of(start.resolve())
    config_path = root / CONFIG_RELATIVE_PATH
    if config_path.exists():
        print(f"{config_path} exists; nothing done")
        return 1
    config_path.parent.mkdir(parents=True, exist_ok=True)
    template = json.loads((SKILL_ROOT / "templates" / "config.example.json").read_text(encoding="utf-8"))
    template["name"] = name
    config_path.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    missing = [line for line in (".orchestration/runs/", ".orchestration/tasks/") if line not in existing.splitlines()]
    if missing:
        gitignore.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + "\n".join(["# wp-orchestration run state", *missing]) + "\n", encoding="utf-8")
    print(f"wrote {config_path}; edit it, then commit it with .gitignore")
    return 0


def messaging(context, arguments) -> int:
    """`wpo message` and `wpo wrap-up`: inbox messages to running sessions, pause requests, lane holds."""
    from .inbox import WRAP_UP_MESSAGE, deliver_if_live, lane_running_wp, live_session, post_message, request_pause
    from .lanes import hold
    runs = context.project.runs_dir
    if arguments.command == "message":
        if bool(arguments.wp) == bool(arguments.lane):
            print("give exactly one of --wp and --lane")
            return 2
        wp_id = arguments.wp or lane_running_wp(runs, arguments.lane)
        if wp_id is None:
            print(f"lane {arguments.lane} is running no WP now")
            return 1
        if not deliver_if_live(runs / wp_id, arguments.text):
            print(f"{wp_id}: no session that reads messages is running (it started before messaging existed, or runs on "
                  "OpenCode or agy); nothing sent")
            return 1
        print(f"{wp_id}: queued; the session reads it at its next turn (within about 15 s of it)")
        return 0
    targets = list(arguments.wp)
    for lane in arguments.lane:
        hold(context, lane, f"wrap-up: {arguments.reason}")
        running = lane_running_wp(runs, lane)
        if running is None:
            print(f"{lane}: running no WP; held")
        else:
            targets.append(running)
    if not targets and not arguments.lane:
        print("give --wp or --lane")
        return 2
    for wp_id in dict.fromkeys(targets):
        request_pause(runs / wp_id, arguments.reason)
        if live_session(runs / wp_id) is not None:
            post_message(runs / wp_id, WRAP_UP_MESSAGE)
            print(f"{wp_id}: asked to log its progress and stop; the run pauses when the session ends")
        else:
            print(f"{wp_id}: its session cannot receive messages; the run pauses when that session ends (or before the next one starts)")
    return 0


def dashboard_command(context, arguments) -> int:
    """`wpo dashboard [--stop]`, and the detached server processes it starts."""
    from . import dashboard
    project, machine = context.project, context.machine
    if arguments.serve:
        from .dashboard_server import serve
        return serve(project, machine)
    if arguments.detach_server:
        return dashboard.detach_server(project, machine)
    try:
        if arguments.stop:
            print(dashboard.stop(project))
            return 0
        started, message = dashboard.start_and_report(project, machine)
    except dashboard.DashboardError as error:
        print(error)
        return 1
    print(message)
    return 0 if started else 1


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    arguments = build_parser().parse_args(argv)
    if arguments.command == "install-shim":
        return install_shim()
    if arguments.command == "init":
        return init_project(arguments.project or Path.cwd(), arguments.name)

    from .machine import MachineConfig
    machine = MachineConfig.load()

    if arguments.command == "audit" and arguments.from_os_schedule and machine.execution_mode != "standalone":
        print("audit: this OS schedule requires execution_mode=standalone; replace it with native T3 scheduling", file=sys.stderr)
        return 2

    if arguments.command == "audit" and (arguments.install_schedule or arguments.remove_schedule or arguments.show_only):
        # Schedule planning must not instantiate router/run state or autostart a dashboard.
        from types import SimpleNamespace

        from .audit import AuditError, change_schedule
        from .project import Project
        if arguments.show_only and not (arguments.install_schedule or arguments.remove_schedule):
            print("--show-only goes with --install-schedule or --remove-schedule", file=sys.stderr)
            return 2
        if arguments.session:
            print("schedule administration cannot run inside a delegated session", file=sys.stderr)
            return 2
        project = Project.locate(arguments.project, machine.raw.get("project_defaults"), machine.execution_mode)
        try:
            return change_schedule(SimpleNamespace(project=project, machine=machine), arguments.install_schedule, arguments.show_only)
        except (AuditError, OSError) as error:
            print(f"audit: {error}", file=sys.stderr)
            return 2

    from .context import Context
    context = Context.load(arguments.project, machine)
    if arguments.session:
        from .t3 import Bridge, T3Error, read_json
        bridge = Bridge(context)
        identity = read_json(bridge.directory / "sessions" / f"{bridge.request_path(arguments.session).stem}.json")
        if identity is None or Path(identity["project"]).resolve() != context.project.root:
            raise T3Error("Unknown T3 session identity for this project")
        os.environ.update(identity["env"])

    if arguments.command == "t3":
        from .t3 import cli_command
        return cli_command(context, arguments)

    if arguments.command == "continuous":
        from .continuous import cli_command
        try:
            return cli_command(context, arguments)
        except (TypeError, ValueError) as error:
            print(f"continuous: {error}", file=sys.stderr)
            return 2

    if arguments.command == "dashboard":
        return dashboard_command(context, arguments)
    if arguments.command == "clean-builds":
        from .artifacts import clean_merged
        from .project import ProjectError
        try:
            print(json.dumps(clean_merged(context, arguments.wp, arguments.apply), indent=2))
        except (ProjectError, OSError, subprocess.CalledProcessError) as error:
            print(f"Build cleanup refused: {error}", file=sys.stderr)
            return 2
        return 0
    from .dashboard import autostart
    autostart(context.project, context.machine)

    if arguments.command == "whats-new":
        from .whats_new import whats_new
        since = arguments.since if arguments.since is not None or arguments.last is not None else datetime.now() - timedelta(days=1)
        print(whats_new(context.project, since, arguments.last))
        return 0

    if arguments.command in ("log", "verify", "handoff"):
        from .session_commands import SessionCommandError, handoff, log, verify
        try:
            if arguments.command == "log":
                print(log(context, arguments.message, arguments.step))
                return 0
            if arguments.command == "handoff":
                print(handoff(context, sys.stdin.read() if arguments.text == "-" else arguments.text))
                return 0
            passed, report = verify(context)
        except SessionCommandError as error:
            print(error)
            return 2
        print(report)
        print("Verify passed." if passed else "Verify failed.")
        return 0 if passed else 1

    if arguments.command == "finding":
        from .findings import answer
        from .session_commands import SessionCommandError
        try:
            print(answer(context, arguments.finding_id, arguments.finding_command, getattr(arguments, "ground", None), arguments.text))
        except SessionCommandError as error:
            print(error)
            return 2
        return 0

    if arguments.command in ("ask-approval", "run-approved", "approve", "deny"):
        from .approvals import ApprovalError, decide, request, run_approved
        from .session_commands import SessionCommandError
        try:
            if arguments.command == "ask-approval":
                approval = request(context, arguments.approval_command, arguments.why)
                print(f"Request {approval['id']} sent. Carry on with other work; the answer arrives as a message.")
                return 0
            if arguments.command == "run-approved":
                return run_approved(context, arguments.approval_id)
            print(decide(context, arguments.approval_id, arguments.command == "approve",
                         arguments.note if arguments.command == "approve" else arguments.reason))
            return 0
        except (ApprovalError, SessionCommandError) as error:
            print(error)
            return 2

    if arguments.command == "lint":
        from .lint import LintConfigError, findings_json, lint, summary_lines
        try:
            findings = lint(context, arguments.wps, arguments.include_done)
        except LintConfigError as error:
            print(f"lint settings: {error}", file=sys.stderr)
            return 2
        if arguments.json:
            print(json.dumps(findings_json(findings), indent=2))
        else:
            for finding in findings:
                print(finding)
            for line in summary_lines(findings):
                print(line)
        return 1 if any(finding.severity == "error" for finding in findings) else 0

    if arguments.command == "audit":
        from .audit import AuditError, run_audit
        try:
            return run_audit(context, arguments.mechanical_only)
        except AuditError as error:
            print(f"audit: {error}", file=sys.stderr)
            return 2

    if arguments.command == "show":
        from .transcript import render_one, render_wp
        if bool(arguments.log) == bool(arguments.wp):
            print("give a log path or --wp")
            return 2
        target = render_wp(context.project.runs_dir / arguments.wp) if arguments.wp else render_one(arguments.log.resolve())
        print(target)
        if arguments.open:
            import webbrowser
            webbrowser.open(target.as_uri())
        return 0

    if arguments.command in ("message", "wrap-up"):
        return messaging(context, arguments)

    if arguments.command == "usage":
        from .usage import changes_report, report, write_report
        since = arguments.since or datetime.now() - timedelta(days=7)
        by = tuple(name.strip() for name in arguments.by.split(",") if name.strip())
        if arguments.usage_command == "report":
            text = report(context.project, since, arguments.until, by)
        else:
            text = changes_report(context.project, since, by, timedelta(minutes=arguments.merge_minutes))
        path = write_report(context.project, arguments.usage_command, text)
        print(text)
        print(f"written to {path}")
        return 0

    if arguments.command == "route":
        from .routing import RoutingError, project_mode, routing_modes, set_project_mode
        router = context.router
        try:
            if arguments.route_command == "candidates":
                from .routing import with_mode
                # As the project's next session would rank it; outside a project, as standard.
                spec = with_mode(arguments.spec, project_mode(context.project.runs_dir)) if context.project else arguments.spec
                print(json.dumps(asdict(router.select(spec, split_list(arguments.families) or None, arguments.refresh,
                                                      free_last_resort=not arguments.gate, gate=arguments.gate)), indent=2))
            elif arguments.route_command == "status":
                print(router.status_text())
            elif arguments.route_command == "refresh":
                catalog = router.load_catalog(force_refresh=arguments.force)
                print(json.dumps({key: catalog[key] for key in ("refreshed_at", "unrated", "errors")} | {"routes": len(catalog["routes"])}, indent=2))
            elif arguments.route_command == "mark":
                print(json.dumps(router.mark(arguments.route, arguments.outcome, arguments.detail)))
            elif arguments.route_command == "mode":
                if context.project is None:
                    raise RoutingError("a routing mode belongs to a project: run wpo route mode inside one, or pass --project")
                runs_dir, modes = context.project.runs_dir, routing_modes(router.policy)
                if arguments.mode is None:
                    current = project_mode(runs_dir)
                    unconfigured = "" if current in modes else ", not configured: routes as standard"
                    print(f"{current} (modes: {', '.join(modes)}{unconfigured})")
                    return 0
                if arguments.mode not in modes:
                    raise RoutingError(f"unknown routing mode '{arguments.mode}' (modes: {', '.join(modes)})")
                previous = project_mode(runs_dir)
                set_project_mode(runs_dir, arguments.mode)
                context.journal.emit("routing-mode", f"routing mode {previous} -> {arguments.mode}; WPs that already ran keep theirs")
                print(arguments.mode)
            else:
                print(f"cleared: {', '.join(router.clear_cooldowns(arguments.prefix)) or 'nothing'}")
        except RoutingError as error:
            print(f"routing error: {error}", file=sys.stderr)
            return 2
        return 0

    if arguments.command == "events":
        from .events import format_event, print_events
        journal = context.journal
        if arguments.events_command == "emit":
            fields = dict(pair.split("=", 1) for pair in arguments.field)
            print(format_event(journal.emit(arguments.kind, arguments.message, fields)))
            return 0
        if arguments.events_command == "wait":
            return journal.wait(arguments.after, arguments.timeout_minutes, split_list(arguments.quiet))
        if arguments.events_command == "tail":
            events = journal.read(arguments.after)
            print_events(events)
            print(f"cursor={arguments.after + len(events)}")
            return 0
        print(journal.cursor())
        return 0

    if arguments.command == "run":
        from .lanes import write_board
        from .runner import RunOptions, run_wp
        tree = arguments.tree or context.project.worktree(arguments.wp)
        if machine.execution_mode == "t3" and arguments.tree is None:
            from .t3 import Bridge
            tree = Bridge(context).ensure_workspace(arguments.wp)
        options = RunOptions(arguments.builder, arguments.reviewer, tuple(split_list(arguments.families)), arguments.idle_minutes, arguments.lane)
        code = run_wp(context, arguments.wp, tree.resolve(), options)
        write_board(context)
        return code

    if arguments.command == "delegate":
        from .delegate import DelegateError, Delegation
        try:
            delegation = Delegation(context)
            if arguments.delegate_command == "start":
                delegation.start(arguments.name, arguments.task, arguments.prompt_file, arguments.write_paths, arguments.tier)
            elif arguments.delegate_command == "wait":
                delegation.wait(arguments.name, arguments.minutes)
            elif arguments.delegate_command == "list":
                delegation.list()
            else:
                delegation.run_worker(arguments.name)
        except DelegateError as error:
            print(f"delegate error: {error}", file=sys.stderr)
            return 2
        return 0

    if arguments.command == "supervise":
        from .supervisor import Supervisor
        options = {"families": split_list(arguments.families), "builder_override": arguments.builder, "idle_minutes": arguments.idle_minutes}
        name = arguments.lane or arguments.milestone
        Supervisor(context, name, bool(arguments.lane), split_list(arguments.wps), options).run()
        return 0

    if arguments.command == "integrate":
        from .integrate import EXIT_MEANINGS, integrate
        from .lanes import write_board
        code = integrate(context, arguments.wp, arguments.verify)
        print(f"{arguments.wp}: {EXIT_MEANINGS.get(code, 'failed')} (exit {code})")
        write_board(context)
        return code

    if arguments.command == "lanes":
        from . import lanes
        if arguments.lanes_command == "resume":
            lanes.resume(context, arguments.dry_run, arguments.targets)
        elif arguments.lanes_command == "stop":
            lanes.stop(context, arguments.lane)
        elif arguments.lanes_command == "hold":
            lanes.hold(context, arguments.lane, arguments.reason)
        else:
            lanes.release(context, arguments.lane)
        return 0

    if arguments.command == "status":
        from .lanes import board, write_board
        print("\n".join(board(context)))
        written = write_board(context)
        if written:
            print(f"\nwrote {written}")
        return 0

    if arguments.command == "job":
        from .orchestrator import exec_job, start_job
        command = arguments.job_arguments[1:] if arguments.job_arguments[:1] == ["--"] else arguments.job_arguments
        if not command:
            print("job: give the command after --", file=sys.stderr)
            return 2
        if arguments.job_command == "start":
            start_job(context, arguments.name, command)
            return 0
        return exec_job(context, arguments.name, arguments.log, command)

    if arguments.command == "dispatch":
        from .orchestrator import dispatch
        prompt = arguments.prompt if arguments.prompt is not None else arguments.prompt_file.read_text(encoding="utf-8")
        return dispatch(context, arguments.tier, arguments.name, prompt, arguments.access, split_list(arguments.write_paths),
                        split_list(arguments.families), arguments.cwd)

    if arguments.command == "commit-main":
        from .orchestrator import commit_main
        return commit_main(context, arguments.message, arguments.paths)

    if arguments.command == "place-request":
        from .user_actions import UserActionError, place_request
        try:
            return place_request(context, arguments.placement_id)
        except UserActionError as error:
            print(error)
            return 2

    if arguments.command == "gate":
        from .orchestrator import gate_verify
        return gate_verify(context, arguments.milestone, arguments.skip_clean)
    raise AssertionError(f"unhandled command {arguments.command}")
